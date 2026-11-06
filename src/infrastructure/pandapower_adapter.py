# src/infrastructure/pandapower_adapter.py
import pandapower as pp
import numpy as np
import pandas as pd
from typing import Optional, Dict
import time
from pathlib import Path

from src.loaders.ts_loader import TimeSeriesDataLoader
from src.strategies.electricity.base import ElectricityPricingStrategy
from src.strategies.electricity.marginal import MarginalElectricityPricing
from src.infrastructure.continuation_power_flow import run_continuation_power_flow
from src.loaders.topology_loader import build_pandapower_network, load_topology
from src.optimization.power_dispatch import (
    GeneratorDispatchInput,
    PowerDispatchProblem,
    PowerDispatchResult,
    RenewableForecast,
    StorageDispatchInput,
)
from src.optimization.renewable_forecast import ShortTermRenewableForecaster


class PandapowerAdapter:
    """Implementation documentation."""

    # Implementation note.
    EMISSION_FACTORS = {
        'coal': 0.90,      # Implementation note.
        'gas': 0.45,
        'hydro': 0.0,
        'nuclear': 0.0,
        'wind': 0.0,
        'solar': 0.0,
    }

    # Implementation note.
    GEN_TYPE_MAP = {
        30: 'hydro', 31: 'coal', 32: 'coal', 33: 'gas', 34: 'gas',
        35: 'coal', 36: 'hydro', 37: 'coal', 38: 'nuclear', 39: 'gas',
    }

    def __init__(
        self,
        load_loader: Optional[TimeSeriesDataLoader] = None,
        pv_loader: Optional[TimeSeriesDataLoader] = None,
        wind_loader: Optional[TimeSeriesDataLoader] = None,
        renewable_node_loaders: Optional[list[Dict]] = None,
        base_generation_loader: Optional[TimeSeriesDataLoader] = None,
        topology: Optional[Dict] = None,
        topology_file: Optional[str] = None,
        pv_bus: int = 10,
        wind_bus: int = 20,
        pv_secondary_bus: Optional[int] = None,
        wind_secondary_bus: Optional[int] = None,
        pv_secondary_ratio: float = 0.0,
        wind_secondary_ratio: float = 0.0,
        load_factor: float = 1.0,
        base_mva: float = 100.0,
        load_capacity_mw: Optional[float] = None,
        base_generation_capacity_mw: Optional[float] = None,
        pv_capacity_mw: Optional[float] = None,
        wind_capacity_mw: Optional[float] = None,
        external_grid_max_import_mw: Optional[float] = None,
        use_topology_generators: bool = True,
        electricity_strategy: Optional[ElectricityPricingStrategy] = None,
        verbose: bool = False
    ):
        # Implementation note.
        if topology is None and topology_file:
            topology = load_topology(topology_file)
        if topology is None:
            default_topology = Path(__file__).resolve().parents[2] / "config" / "topology_ieee39.json"
            if default_topology.exists():
                topology = load_topology(default_topology)
        if topology is None:
            raise FileNotFoundError("no topology supplied and default topology JSON is missing")
        self.topology = topology
        self.renewable_forecaster = ShortTermRenewableForecaster(
            step_seconds=300,
            observation_step_seconds=1,
        )
        self.network, self.bus_map = build_pandapower_network(topology)
        self._topology_static_load_mw = float(self.network.load["p_mw"].sum()) if len(self.network.load) else 0.0

        # Implementation note.
        self.load_loader = load_loader
        # When a load time series is supplied, it is the complete background
        # demand for the simulation.  The topology's load values are retained
        # only as a spatial distribution template, not added on top of it.
        self._uses_load_timeseries = load_loader is not None
        profile_data = getattr(load_loader, "data", None) if load_loader is not None else None
        self._load_profile_reference_pu = (
            float(np.mean(profile_data))
            if profile_data is not None and len(profile_data) and np.isfinite(np.mean(profile_data))
            else None
        )
        self._base_static_load_mw = (
            0.0 if self._uses_load_timeseries else self._topology_static_load_mw
        )
        self.pv_loader = pv_loader
        self.wind_loader = wind_loader
        self.renewable_node_loaders = list(renewable_node_loaders or [])
        for node in self.renewable_node_loaders:
            if node.get("type") not in {"wind", "pv"}:
                raise ValueError("topology renewable node type must be wind or pv")
            if "bus_id" not in node or node.get("loader") is None:
                raise ValueError("topology renewable node requires bus_id and loader")
        self.base_generation_loader = base_generation_loader
        self.pv_bus = pv_bus
        self.wind_bus = wind_bus
        self.pv_secondary_bus = pv_secondary_bus
        self.wind_secondary_bus = wind_secondary_bus
        self.pv_secondary_ratio = float(pv_secondary_ratio)
        self.wind_secondary_ratio = float(wind_secondary_ratio)
        if not 0.0 <= self.pv_secondary_ratio <= 1.0 or not 0.0 <= self.wind_secondary_ratio <= 1.0:
            raise ValueError("secondary renewable ratios must be between 0 and 1")
        self.load_factor = load_factor
        self.base_mva = base_mva
        self.load_capacity_mw = float(
            load_capacity_mw if load_capacity_mw is not None else self._topology_static_load_mw or base_mva
        )
        self.base_generation_capacity_mw = float(
            base_generation_capacity_mw if base_generation_capacity_mw is not None else base_mva
        )
        self.pv_capacity_mw = float(pv_capacity_mw if pv_capacity_mw is not None else base_mva)
        self.wind_capacity_mw = float(wind_capacity_mw if wind_capacity_mw is not None else base_mva)
        self.external_grid_max_import_mw = float(
            external_grid_max_import_mw
            if external_grid_max_import_mw is not None
            else 10.0 * self.base_mva
        )
        if self.pv_capacity_mw <= 0 or self.wind_capacity_mw <= 0:
            raise ValueError("renewable capacities must be positive")

        # Implementation note.
        self.electricity_strategy = electricity_strategy or MarginalElectricityPricing(
            base_price=0.5,
            sensitivity=0.10,
            scale=1000.0
        )

        # Implementation note.
        self.dynamic_load_indices = []
        self.dynamic_gen_indices = []
        self.dynamic_sgen_indices = []
        self.renewable_gen_indices: Dict[str, list[int]] = {}
        self.renewable_gen_available_mw: Dict[str, Dict[int, float]] = {"wind": {}, "pv": {}}
        self.renewable_available_mw: Dict[str, float] = {"wind": 0.0, "pv": 0.0}
        self.renewable_dispatched_mw: Dict[str, float] = {"wind": 0.0, "pv": 0.0}
        # Implementation note.
        # Implementation note.
        self._base_generation_nominal = self.network.gen["p_mw"].copy()
        self.use_topology_generators = bool(use_topology_generators)
        external_grids = topology.get("external_grids", [])
        fallback_bus = topology.get("loads", [{}])[0].get("bus_id", 0)
        self.base_generation_bus = int(
            external_grids[0]["bus_id"] if external_grids else fallback_bus
        )
        if not self.use_topology_generators:
            # Keep generator buses for voltage support, but clear all
            # topology-defined active output.  User time-series generation
            # is distributed over these buses in _apply_base_generation.
            self.network.gen.loc[:, "p_mw"] = 0.0
        self._dispatch_generator_indices = {}
        generator_rows = self.network.gen.iterrows() if self.use_topology_generators else []
        for position, (index, _) in enumerate(generator_rows):
            topology_generator = topology.get("generators", [])[position]
            candidate = topology_generator.get("id") or topology_generator.get("name")
            generator_id = str(candidate) if candidate else f"gen_{position}"
            if generator_id in self._dispatch_generator_indices:
                generator_id = f"{generator_id}_{position}"
            self._dispatch_generator_indices[generator_id] = int(index)
        self.storage_models = {}
        self._optimized_dispatch_active = False
        self._dispatch_sequence = 0
        self.current_dispatch_id = None
        self._last_feasible_dispatch = None
        self._pre_candidate_dispatch = None
        self.compute_load_indices = []

        # Implementation note.
        self.extra_loads: Dict[int, float] = {}
        # Positive storage power discharges into the grid; negative power charges.
        self.storage_powers: Dict[int, float] = {}

        # Implementation note.
        self.load_buses = [int(bus) for bus in self.network.load.bus.tolist()]
        if not self.load_buses:
            self.load_buses = [int(bus["id"]) for bus in topology.get("buses", [])]

        # Implementation note.
        self._save_load_distribution()
        self._topology_load_indices = list(self.network.load.index)
        if self._uses_load_timeseries and self._topology_load_indices:
            self.network.load.loc[self._topology_load_indices, "p_mw"] = 0.0
            self.network.load.loc[self._topology_load_indices, "q_mvar"] = 0.0

        # Implementation note.
        self.current_second = 0
        self._last_time_series_load_mw = 0.0
        self._last_background_load_mw = 0.0
        self._last_time_series_generation_mw = 0.0
        self._last_base_generation_mw = 0.0
        self._last_wind_generation_mw = None
        self._last_pv_generation_mw = None
        self._power_flow_valid = False

        # Implementation note.
        self.bus_carbon_intensity: Dict[int, float] = {}

        # Implementation note.
        self.verbose = verbose

        # Implementation note.
        self.last_pp_time = 0.0
        self.last_cpf_time = 0.0

        # Implementation note.
        print(f"⚡ Grid topology adapter initialized: {topology.get('name', 'custom')}")
        print(f"   Base capacity: {self.base_mva} MVA")
        print(f"   PV connection bus: Bus {self.pv_bus}")
        print(f"   Wind connection bus: Bus {self.wind_bus}")
        print(f"   PV/Wind capacity: {self.pv_capacity_mw:.1f}/{self.wind_capacity_mw:.1f} MW")
        print(f"   Pricing strategy: {self.electricity_strategy.__class__.__name__}")

    # Implementation note.

    def _save_load_distribution(self):
        """Implementation documentation."""
        if len(self.network.load) == 0:
            self.load_distribution = np.ones(len(self.load_buses)) / len(self.load_buses)
            return
        load_p_mw = self.network.load['p_mw'].values
        total = load_p_mw.sum()
        if total > 0:
            self.load_distribution = load_p_mw / total
        else:
            self.load_distribution = np.ones(len(load_p_mw)) / len(load_p_mw)

    # Implementation note.

    def _clear_dynamic_elements(self):
        """Implementation documentation."""
        if self.dynamic_load_indices:
            self.network.load.drop(self.dynamic_load_indices, inplace=True, errors='ignore')
            self.dynamic_load_indices = []
            self.compute_load_indices = []
        if self.dynamic_gen_indices:
            self.network.gen.drop(self.dynamic_gen_indices, inplace=True, errors='ignore')
            self.dynamic_gen_indices = []
            self.renewable_gen_indices = {}
        if self.dynamic_sgen_indices:
            self.network.sgen.drop(self.dynamic_sgen_indices, inplace=True, errors='ignore')
            self.dynamic_sgen_indices = []

    # Implementation note.

    def update_time_series(self, second: int):
        """Implementation documentation."""
        self.current_second = second
        self._clear_dynamic_elements()
        self._last_pv_generation_mw = None
        self._last_wind_generation_mw = None
        self.renewable_available_mw = {"wind": 0.0, "pv": 0.0}
        self.renewable_dispatched_mw = {"wind": 0.0, "pv": 0.0}
        self.renewable_gen_indices = {}
        self.renewable_gen_available_mw = {"wind": {}, "pv": {}}

        # Implementation note.
        if self.load_loader is not None:
            load_pu = float(self.load_loader.get_value_at_second(second))
            total_load_mw = self._load_profile_to_mw(load_pu)
            self._apply_background_load(total_load_mw)
            self._last_background_load_mw = float(total_load_mw)
        else:
            self._last_background_load_mw = 0.0

        # Implementation note.
        self._apply_compute_loads()

        # Implementation note.
        self._apply_storage_powers()

        # 3. Topology-bound node MW profiles take precedence.  Each file is
        # already the physical node output, so no global capacity or split
        # coefficient is applied at runtime.
        if self.renewable_node_loaders:
            for node in self.renewable_node_loaders:
                source = node["type"]
                power_mw = max(0.0, float(node["loader"].get_value_at_second(second)))
                self.renewable_available_mw[source] += power_mw
                self._add_renewable_gen(int(node["bus_id"]), power_mw, source)
            self._last_pv_generation_mw = self.renewable_available_mw["pv"]
            self._last_wind_generation_mw = self.renewable_available_mw["wind"]
        else:
            # Backward-compatible aggregate PV/wind profile mode.
            if self.pv_loader is not None:
                pv_mw = self.pv_loader.get_value_at_second(second) * self.pv_capacity_mw
                self._last_pv_generation_mw = float(pv_mw)
                self.renewable_available_mw["pv"] = max(0.0, float(pv_mw))
                self._add_split_renewable_gen(self.pv_bus, self.pv_secondary_bus, pv_mw, "pv", self.pv_secondary_ratio)

            if self.wind_loader is not None:
                wind_mw = self.wind_loader.get_value_at_second(second) * self.wind_capacity_mw
                self._last_wind_generation_mw = float(wind_mw)
                self.renewable_available_mw["wind"] = max(0.0, float(wind_mw))
                self._add_split_renewable_gen(self.wind_bus, self.wind_secondary_bus, wind_mw, "wind", self.wind_secondary_ratio)

        # In data-driven mode, the conventional plan is a fixed upper bound
        # rather than an always-on injection: actual wind/PV serve the load
        # first and the conventional plan only fills the remaining shortfall.
        self._apply_base_generation(second)

        # Keep a stable telemetry fallback.  Some pandapower versions can
        # clear result tables when a dynamic element causes a failed power
        # flow; the time-series values are still valid for the dashboard.
        self._last_time_series_load_mw = float(self.network.load['p_mw'].sum()) if len(self.network.load) else 0.0
        generation = float(self.network.gen['p_mw'].sum()) if len(self.network.gen) else 0.0
        generation += float(self.network.sgen['p_mw'].sum()) if len(self.network.sgen) else 0.0
        self._last_time_series_generation_mw = generation

    def _apply_base_generation(self, second: int):
        """Implementation documentation."""
        self._last_base_generation_mw = 0.0
        if self.base_generation_loader is None:
            return
        generation_pu = float(self.base_generation_loader.get_value_at_second(second))
        if not np.isfinite(generation_pu):
            generation_pu = 0.0
        generation_pu = max(0.0, generation_pu)
        planned_mw = generation_pu * self.base_generation_capacity_mw
        if not self.use_topology_generators:
            # Data-driven conventional generation is a committed plan.  It
            # must not be silently reduced when wind/PV rises; any renewable
            # output above the remaining load is curtailed by dispatch.
            self._last_base_generation_mw = planned_mw
        else:
            self._last_base_generation_mw = planned_mw
        if not self.use_topology_generators and len(self.network.gen):
            weights = self._base_generation_nominal.clip(lower=0.0)
            total_weight = float(weights.sum())
            if total_weight > 1e-9:
                for index, weight in weights.items():
                    self.network.gen.at[index, "p_mw"] = (
                        self._last_base_generation_mw * float(weight) / total_weight
                    )
                return
        if self._last_base_generation_mw > 0.001 and self.base_generation_bus:
            idx = pp.create_sgen(
                self.network,
                self._map_bus_id(self.base_generation_bus),
                p_mw=self._last_base_generation_mw,
                q_mvar=0.0,
            )
            self.dynamic_sgen_indices.append(idx)

    def _apply_background_load(self, total_load_mw: float):
        """Implementation documentation."""
        if self._uses_load_timeseries and self._topology_load_indices:
            for i, index in enumerate(self._topology_load_indices):
                if i >= len(self.load_distribution):
                    break
                p_mw = total_load_mw * self.load_distribution[i]
                self.network.load.at[index, "p_mw"] = p_mw
                self.network.load.at[index, "q_mvar"] = p_mw * 0.3287
            return

        for i, bus in enumerate(self.load_buses):
            if i < len(self.load_distribution):
                p_mw = total_load_mw * self.load_distribution[i]
                if p_mw > 0.001:
                    q_mvar = p_mw * 0.3287  # Implementation note.
                    idx = pp.create_load(self.network, bus, p_mw=p_mw, q_mvar=q_mvar)
                    self.dynamic_load_indices.append(idx)

    def _load_profile_to_mw(self, profile_value: float) -> float:
        """Convert the user profile into the complete system background load.

        The CSV is a normalized profile, while the topology supplies the
        spatial layout and nominal MW scale.  With a profile file, its mean
        value is mapped to the topology's original total load, so the CSV
        controls the final injected load instead of being added to it.
        """
        value = max(0.0, float(profile_value)) * self.load_factor
        if not self._uses_load_timeseries:
            return value * self.base_mva
        reference = self._load_profile_reference_pu
        if reference is None or reference <= 0.0:
            reference = max(1e-9, value)
        nominal = self.load_capacity_mw or self.base_mva
        return nominal * value / reference

    def _apply_compute_loads(self):
        """Implementation documentation."""
        for bus_id, load_mw in self.extra_loads.items():
            bus_id = self._map_bus_id(bus_id)
            if load_mw > 0.001:
                q_mvar = load_mw * 0.3287
                idx = pp.create_load(self.network, bus_id, p_mw=load_mw, q_mvar=q_mvar)
                self.dynamic_load_indices.append(idx)
                self.compute_load_indices.append(idx)

    def _apply_storage_powers(self):
        for bus_id, net_power_mw in self.storage_powers.items():
            bus_id = self._map_bus_id(bus_id)
            if net_power_mw < -0.001:
                idx = pp.create_load(self.network, bus_id, p_mw=-net_power_mw, q_mvar=0.0)
                self.dynamic_load_indices.append(idx)
            elif net_power_mw > 0.001:
                idx = pp.create_sgen(self.network, bus_id, p_mw=net_power_mw, q_mvar=0.0)
                self.dynamic_sgen_indices.append(idx)

    def _add_renewable_gen(self, bus_id: int, power_mw: float, source: str):
        """Implementation documentation."""
        bus_id = self._map_bus_id(bus_id)
        if power_mw > 0.001:
            idx = pp.create_gen(
                self.network, bus_id,
                p_mw=power_mw, vm_pu=1.0,
                min_q_mvar=-100, max_q_mvar=100
            )
            self.dynamic_gen_indices.append(idx)
            self.renewable_gen_indices.setdefault(source, []).append(idx)
            self.renewable_gen_available_mw.setdefault(source, {})[idx] = float(power_mw)
            self.renewable_dispatched_mw[source] = self.renewable_dispatched_mw.get(source, 0.0) + float(power_mw)

    def _add_split_renewable_gen(self, primary_bus: int, secondary_bus: Optional[int], power_mw: float, source: str, secondary_ratio: float) -> None:
        ratio = secondary_ratio if secondary_bus is not None else 0.0
        self._add_renewable_gen(primary_bus, power_mw * (1.0 - ratio), source)
        if secondary_bus is not None:
            self._add_renewable_gen(secondary_bus, power_mw * ratio, source)

    # Implementation note.

    def update_compute_load(self, bus_id: int, load_mw: float):
        """Implementation documentation."""
        self.extra_loads[bus_id] = load_mw

    def replace_compute_loads(self, loads_by_bus: Dict[int, float]) -> None:
        """Atomically replace dynamic compute loads used by the next AC solve."""
        self.extra_loads = {
            int(bus_id): max(0.0, float(load_mw))
            for bus_id, load_mw in loads_by_bus.items()
            if float(load_mw) > 1e-12
        }
        self._prepare_compute_loads()

    def _map_bus_id(self, bus_id: int) -> int:
        """Map a topology's external bus ID to Pandapower's integer index."""
        if bus_id in self.bus_map:
            return self.bus_map[bus_id]
        if bus_id in self.network.bus.index:
            return int(bus_id)
        raise ValueError(f"unknown topology bus: {bus_id}")

    def update_storage_power(self, bus_id: int, net_power_mw: float):
        """Set storage net injection; positive discharges and negative charges."""
        self.storage_powers[bus_id] = net_power_mw

    def set_storage_models(self, storages) -> None:
        """Register runtime storage objects without assuming a fixed count."""
        self.storage_models = {storage.id: storage for storage in storages}

    def _forecast_value(
        self, loader, second: int, default: float = 0.0, *, cyclic: bool = False
    ) -> float:
        if loader is None:
            return float(default)
        if cyclic and getattr(loader, "length", 0) > 0 and getattr(loader, "data", None) is not None:
            compression = float(getattr(loader, "time_compression", 1.0))
            source_second = int(getattr(loader, "start_offset_seconds", 0)) + int(
                second / compression
            )
            value = float(loader.data[source_second % int(loader.length)])
            return value if np.isfinite(value) else float(default)
        value = float(loader.get_value_at_second(second))
        return value if np.isfinite(value) else float(default)

    @staticmethod
    def _forecast_needs_cyclic_fill(loader, seconds) -> bool:
        if loader is None or getattr(loader, "length", 0) <= 0:
            return False
        compression = float(getattr(loader, "time_compression", 1.0))
        offset = int(getattr(loader, "start_offset_seconds", 0))
        return any(offset + int(second / compression) >= int(loader.length) for second in seconds)

    def build_power_dispatch_problem(
        self,
        second: int,
        horizon_hours: int = 24,
        *,
        objective: str = "min_cost",
        objective_weights: Optional[Dict[str, float]] = None,
        step_hours: float = 1.0,
        actual_renewable_first_period: Optional[Dict[str, float]] = None,
    ) -> PowerDispatchProblem:
        """Build a rolling problem using online renewable forecasts."""
        step_hours = max(1e-6, float(step_hours))
        period_count = max(1, int(np.ceil(float(horizon_hours) / step_hours)))
        periods = range(period_count)
        forecast_seconds = [int(second + period * step_hours * 3600) for period in periods]
        compute_load = sum(max(0.0, float(value)) for value in self.extra_loads.values())
        forecast_load = [
            self._base_static_load_mw
            + self._load_profile_to_mw(
                self._forecast_value(self.load_loader, point, cyclic=True)
            )
            + compute_load
            for point in forecast_seconds
        ]
        raw_planned_base = [
            max(0.0, self._forecast_value(
                self.base_generation_loader, point, 0.0, cyclic=True
            ) * self.base_generation_capacity_mw)
            for point in forecast_seconds
        ]
        if self.renewable_node_loaders:
            def topology_forecast(source: str) -> list[float]:
                total = np.zeros(period_count, dtype=float)
                for node in self.renewable_node_loaders:
                    if node["type"] != source:
                        continue
                    loader = node["loader"]
                    observed = getattr(loader, "data", [])
                    if not len(observed) and hasattr(loader, "get_value_at_second"):
                        observed = [loader.get_value_at_second(second)]
                    capacity = max([float(value) for value in observed] or [0.0])
                    total += np.asarray(self.renewable_forecaster.forecast(
                        observed,
                        current_second=second,
                        horizon_steps=period_count,
                        kind=source,
                        capacity_mw=capacity,
                        values_are_pu=False,
                    ))
                return total.tolist()

            wind_forecast = topology_forecast("wind")
            pv_forecast = topology_forecast("pv")
        else:
            wind_observations = (
                getattr(self.wind_loader, "data", [])
                if self.wind_loader is not None else []
            )
            if (
                not len(wind_observations)
                and self.wind_loader is not None
                and hasattr(self.wind_loader, "get_value_at_second")
            ):
                wind_observations = [self.wind_loader.get_value_at_second(second)]
            pv_observations = (
                getattr(self.pv_loader, "data", [])
                if self.pv_loader is not None else []
            )
            if (
                not len(pv_observations)
                and self.pv_loader is not None
                and hasattr(self.pv_loader, "get_value_at_second")
            ):
                pv_observations = [self.pv_loader.get_value_at_second(second)]
            wind_forecast = self.renewable_forecaster.forecast(
                wind_observations,
                current_second=second,
                horizon_steps=period_count,
                kind="wind",
                capacity_mw=self.wind_capacity_mw,
                values_are_pu=True,
            )
            pv_forecast = self.renewable_forecaster.forecast(
                pv_observations,
                current_second=second,
                horizon_steps=period_count,
                kind="pv",
                capacity_mw=self.pv_capacity_mw,
                values_are_pu=True,
            )
            wind_forecast = [max(0.0, value * self.wind_capacity_mw) for value in wind_forecast]
            pv_forecast = [max(0.0, value * self.pv_capacity_mw) for value in pv_forecast]

        # The current observation is known even when the future forecast is
        # intentionally conservative.  Anchor period zero to that measured
        # value; only periods after zero remain forecast-driven.
        def current_renewable(source: str) -> Optional[float]:
            if self.renewable_node_loaders:
                values = [
                    float(node["loader"].get_value_at_second(second))
                    for node in self.renewable_node_loaders
                    if node.get("type") == source
                    and hasattr(node.get("loader"), "get_value_at_second")
                ]
                return sum(values) if values else None
            loader = self.wind_loader if source == "wind" else self.pv_loader
            capacity = self.wind_capacity_mw if source == "wind" else self.pv_capacity_mw
            if loader is None or not hasattr(loader, "get_value_at_second"):
                return None
            return float(loader.get_value_at_second(second)) * float(capacity)

        if period_count:
            observed_wind = current_renewable("wind")
            observed_pv = current_renewable("pv")
            if observed_wind is not None:
                wind_forecast[0] = max(0.0, observed_wind)
            if observed_pv is not None:
                pv_forecast[0] = max(0.0, observed_pv)

        # The first rolling period is already observable when the engine calls
        # this method.  Use that measurement to avoid suppressing a charge
        # decision solely because the short-term forecast lagged the live
        # renewable output.  Future periods remain forecast-only.
        actual_first = actual_renewable_first_period or {}
        if period_count and actual_first:
            if "wind" in actual_first:
                wind_forecast[0] = max(0.0, float(actual_first["wind"]))
            if "pv" in actual_first:
                pv_forecast[0] = max(0.0, float(actual_first["pv"]))

        # The conventional plan must not be allowed to contain an implicit
        # copy of the *actual* renewable output.  Use only the renewable
        # forecast available at planning time to reserve headroom for wind/PV.
        # Realized renewable output is applied later by update_time_series(),
        # so forecast error naturally becomes either curtailment or a grid
        # import instead of a long period of planned over-generation.
        planned_renewable = np.asarray(wind_forecast) + np.asarray(pv_forecast)
        planned_base = [
            min(raw, max(0.0, forecast_load[index] - float(planned_renewable[index])))
            for index, raw in enumerate(raw_planned_base)
        ]
        if self.use_topology_generators:
            fixed_generation = []
            # Topology generators are the controllable fleet for the full
            # modeled demand.  Do not subtract the platform's data-driven
            # base-generation plan as well, or the initial generator outputs
            # can over-supply the reduced load and make the rolling model
            # infeasible under ramp/export limits.
            load = list(forecast_load)
        else:
            # The data-driven conventional plan remains fixed.  Wind/PV
            # dispatch is therefore bounded by the remaining load and is
            # curtailed when it exceeds that headroom.
            fixed_generation = planned_base
            load = forecast_load

        topology_generators = self.topology.get("generators", [])
        generators = []
        for position, (generator_id, index) in enumerate(self._dispatch_generator_indices.items()):
            source = topology_generators[position]
            nominal = float(self._base_generation_nominal.loc[index])
            generators.append(GeneratorDispatchInput(
                id=generator_id,
                bus_id=int(source["bus_id"]),
                min_mw=float(source["min_p_mw"]),
                max_mw=float(source["max_p_mw"]),
                initial_mw=float(self.network.gen.at[index, "p_mw"]),
                ramp_up_mw_per_hour=float(source["ramp_up_mw_per_hour"]),
                ramp_down_mw_per_hour=float(source["ramp_down_mw_per_hour"]),
                cost_linear_yuan_per_mwh=float(source["cost_linear_yuan_per_mwh"]),
                cost_quadratic_yuan_per_mw2h=float(source["cost_quadratic_yuan_per_mw2h"]),
                emission_tco2_per_mwh=float(source["emission_tco2_per_mwh"]),
                reference_mw=[],
            ))

        renewables = [
            RenewableForecast(
                id="wind", bus_id=int(self.wind_bus),
                available_mw=wind_forecast,
            ),
            RenewableForecast(
                id="pv", bus_id=int(self.pv_bus),
                available_mw=pv_forecast,
            ),
        ]
        storages = [
            StorageDispatchInput(
                id=storage.id,
                bus_id=int(storage.bus_id),
                initial_soc_mwh=float(storage.soc_mwh),
                min_soc_mwh=float(storage.min_soc_mwh),
                max_soc_mwh=float(storage.max_soc_mwh),
                max_power_mw=float(storage.max_power_mw),
                charge_efficiency=float(storage.charge_efficiency),
                discharge_efficiency=float(storage.discharge_efficiency),
                degradation_cost_per_mwh=float(storage.degradation_cost_per_mwh),
                # Anchor the rolling-horizon terminal SOC to the configured
                # initial SOC, not the live (possibly full) SOC.  Tying the
                # terminal target to the current SOC locks a full battery at
                # its ceiling and forbids every discharge, so the storage only
                # ever charges.  A fixed reference still preserves the reserve
                # while letting the battery discharge during deficits.
                terminal_soc_mwh=float(storage.initial_soc_mwh),
                initial_power_mw=float(self.storage_powers.get(int(storage.bus_id), 0.0)),
            )
            for storage in self.storage_models.values()
        ]
        return PowerDispatchProblem(
            load_mw=load,
            fixed_generation_mw=fixed_generation,
            generators=generators,
            renewables=renewables,
            storages=storages,
            objective=objective,
            objective_weights=dict(objective_weights or {}),
            horizon_hours=period_count,
            step_hours=step_hours,
            start_second=float(second),
            forecast_fill=(
                "cyclic" if any(
                    self._forecast_needs_cyclic_fill(loader, forecast_seconds)
                    for loader in (
                        self.load_loader, self.pv_loader,
                        self.wind_loader, self.base_generation_loader,
                    )
                ) else "native"
            ),
            external_grid_max_import_mw=(
                self.external_grid_max_import_mw if self.topology.get("external_grids") else 0.0
            ),
            external_grid_max_export_mw=(
                (
                    10.0 * self.base_mva
                    if self.use_topology_generators else 0.0
                ) if self.topology.get("external_grids") else 0.0
            ),
        )

    def _dispatch_snapshot(self) -> Dict:
        return {
            "dispatch_id": self.current_dispatch_id,
            "generator_mw": {
                generator_id: float(self.network.gen.at[index, "p_mw"])
                for generator_id, index in self._dispatch_generator_indices.items()
            },
            "renewable_mw": dict(self.renewable_dispatched_mw),
            "storage_powers": dict(self.storage_powers),
            "storage_soc": {
                storage_id: float(storage.soc_mwh)
                for storage_id, storage in self.storage_models.items()
            },
            "storage_current_power": {
                storage_id: float(storage.current_power_mw)
                for storage_id, storage in self.storage_models.items()
            },
            "optimized": bool(self._optimized_dispatch_active),
        }

    def _restore_dispatch_snapshot(self, snapshot: Dict) -> None:
        self._optimized_dispatch_active = bool(snapshot.get("optimized", True))
        for generator_id, power in snapshot.get("generator_mw", {}).items():
            index = self._dispatch_generator_indices.get(generator_id)
            if index is not None:
                self.network.gen.at[index, "p_mw"] = float(power)
        self.storage_powers = dict(snapshot.get("storage_powers", {}))
        for storage_id, soc in snapshot.get("storage_soc", {}).items():
            if storage_id in self.storage_models:
                self.storage_models[storage_id].soc_mwh = float(soc)
        for storage_id, power in snapshot.get("storage_current_power", {}).items():
            if storage_id in self.storage_models:
                self.storage_models[storage_id].current_power_mw = float(power)
        self.update_time_series(self.current_second)
        self.apply_renewable_dispatch(snapshot.get("renewable_mw", {}))
        self.current_dispatch_id = snapshot.get("dispatch_id")

    def apply_power_dispatch(self, result: PowerDispatchResult, period: int = 0) -> Dict:
        # Joint COPT solves may stop at the time limit with a feasible
        # incumbent (reported as ``optimal_solution``).  Feasibility, rather
        # than the solver's proof status, is what makes a dispatch safe to
        # apply; rejecting that incumbent aborts the simulation thread.
        if not result.strictly_feasible:
            raise ValueError("only a strict feasible power dispatch can be applied")
        self._pre_candidate_dispatch = self._dispatch_snapshot()
        self._dispatch_sequence += 1
        dispatch_id = f"dispatch-{self._dispatch_sequence}"
        for generator_id, values in result.generator_mw.items():
            if generator_id not in self._dispatch_generator_indices:
                raise ValueError(f"unknown generator in dispatch: {generator_id}")
            self.network.gen.at[self._dispatch_generator_indices[generator_id], "p_mw"] = float(values[period])
        self._optimized_dispatch_active = True
        storage_by_bus: Dict[int, float] = {}
        for storage_id, values in result.storage_power_mw.items():
            storage = self.storage_models.get(storage_id)
            if storage is None:
                raise ValueError(f"unknown storage in dispatch: {storage_id}")
            power = float(values[period])
            bus_id = int(storage.bus_id)
            storage_by_bus[bus_id] = storage_by_bus.get(bus_id, 0.0) + power
            # The rolling plan's next SOC is one hour ahead. Runtime SOC is
            # advanced by SimulationEngine using the actual simulation step.
            storage.current_power_mw = power
        self.storage_powers = storage_by_bus
        self.update_time_series(self.current_second)
        renewable = {
            source: float(values[period])
            for source, values in result.renewable_dispatched_mw.items()
            if len(values) > period
        }
        self.apply_renewable_dispatch(renewable)
        self.current_dispatch_id = dispatch_id
        return {
            "dispatch_id": dispatch_id,
            "generator_mw": {
                generator_id: float(self.network.gen.at[index, "p_mw"])
                for generator_id, index in self._dispatch_generator_indices.items()
            },
            "renewable_mw": dict(self.renewable_dispatched_mw),
            "storage_power_mw": {
                storage_id: float(result.storage_power_mw[storage_id][period])
                for storage_id in result.storage_power_mw
            },
        }

    def validate_dispatch_ac(self) -> Dict:
        self.run_power_flow()
        snapshot = self.get_operating_constraint_snapshot()
        feasible = bool(self._power_flow_valid and snapshot.get("feasible"))
        result = {**snapshot, "feasible": feasible, "dispatch_id": self.current_dispatch_id}
        if feasible:
            self._last_feasible_dispatch = self._dispatch_snapshot()
        return result

    def restore_last_feasible_dispatch(self) -> bool:
        # Prefer the state immediately before the rejected candidate.  A
        # historical feasible snapshot may contain an older physical SOC and
        # must not rewind energy that has already flowed in simulation time.
        snapshot = self._pre_candidate_dispatch or self._last_feasible_dispatch
        if snapshot is None:
            return False
        self._restore_dispatch_snapshot(snapshot)
        # The rejected candidate has already overwritten pandapower's result
        # tables.  Re-run AC power flow after restoring the controls so line
        # loading, voltages, carbon intensity and health metrics describe the
        # restored feasible operating point rather than the rejected one.
        self.run_power_flow()
        return True

    def get_storage_net_power(self, bus_id: int) -> float:
        return self.storage_powers.get(bus_id, 0.0)

    # =========================================================
    # Implementation note.
    # =========================================================

    def _prepare_compute_loads(self):
        """Implementation documentation."""
        if self.compute_load_indices:
            self.network.load.drop(self.compute_load_indices, inplace=True, errors='ignore')
            self.dynamic_load_indices = [
                idx for idx in self.dynamic_load_indices
                if idx not in self.compute_load_indices
            ]
            self.compute_load_indices = []
        self._apply_compute_loads()

    def run_power_flow(self):
        t0 = time.time()
        self._prepare_compute_loads()
        self._power_flow_valid = False

        try:
            pp.runpp(self.network, numba=False, enforce_q_lims=True)
            self._compute_bus_carbon_intensity()
            self.last_pp_time = time.time() - t0
            self._power_flow_valid = True

            if self.verbose:
                print(f"[runpp] {self.last_pp_time:.3f}s (second {self.current_second})")

        except Exception as e:
            print(f"Power flow did not converge (second {self.current_second}): {e}")

    # Implementation note.

    def _compute_bus_carbon_intensity(self):
        """Implementation documentation."""
        if len(self.network.res_gen) == 0:
            self.bus_carbon_intensity = {bus: 0.5 for bus in self.network.bus.index}
            return

        total_emission = 0.0   # Implementation note.
        total_gen = 0.0        # MW

        for idx in self.network.gen.index:
            if idx not in self.network.res_gen.index:
                continue
            p_mw = self.network.res_gen.loc[idx, 'p_mw']
            if pd.isna(p_mw) or p_mw <= 0:
                continue
            bus_id = self.network.gen.loc[idx, 'bus']
            gen_type = self.GEN_TYPE_MAP.get(bus_id, 'coal')
            ef = self.EMISSION_FACTORS.get(gen_type, 0.5)
            total_emission += p_mw * ef
            total_gen += p_mw

        avg_intensity = total_emission / total_gen if total_gen > 0 else 0.5
        self.bus_carbon_intensity = {bus: avg_intensity for bus in self.network.bus.index}

    # Implementation note.

    def get_bus_carbon_intensity(self, bus_id: int) -> float:
        """Implementation documentation."""
        return self.bus_carbon_intensity.get(bus_id, 0.5)

    def get_bus_marginal_price(self, bus_id: int) -> float:
        """Implementation documentation."""
        extra_load = self.extra_loads.get(bus_id, 0.0)
        system_extra_load = sum(
            max(0.0, float(load_mw))
            for load_mw in self.extra_loads.values()
        )
        return self.electricity_strategy.get_node_price(
            self.network, bus_id, extra_load, system_extra_load
        )

    def get_bus_voltage(self, bus_id: int) -> float:
        """Implementation documentation."""
        if bus_id in self.network.res_bus.index:
            return self.network.res_bus.loc[bus_id, 'vm_pu']
        return 1.0

    def get_total_load(self) -> float:
        """Implementation documentation."""
        # This cache contains the current background profile, compute loads
        # and storage charging load, and remains stable after result tables
        # are rebuilt.
        if self._last_time_series_load_mw > 0:
            return float(self._last_time_series_load_mw)
        if len(self.network.res_load) > 0:
            result = float(self.network.res_load['p_mw'].sum())
            if result > 0:
                return result
        # A failed or not-yet-run power flow leaves result tables empty, but
        # the network tables still contain the current time-series load.
        if len(self.network.load) > 0:
            return float(self.network.load['p_mw'].sum())
        return float(self._last_time_series_load_mw)

    def get_initial_load_mw(self) -> float:
        """Return the background load before compute/storage adjustments."""
        return float(self._last_background_load_mw)

    def estimate_net_load_mw(self, second: int) -> float:
        """Estimate current demand net of available wind/PV before storage dispatch."""
        background = 0.0
        if self.load_loader is not None:
            background = self._load_profile_to_mw(
                float(self.load_loader.get_value_at_second(second))
            )
        renewable = 0.0
        if self.pv_loader is not None:
            renewable += max(
                0.0,
                float(self.pv_loader.get_value_at_second(second)) * self.pv_capacity_mw,
            )
        if self.wind_loader is not None:
            renewable += max(
                0.0,
                float(self.wind_loader.get_value_at_second(second)) * self.wind_capacity_mw,
            )
        base_generation = 0.0
        if self.base_generation_loader is not None:
            base_generation = max(
                0.0,
                float(self.base_generation_loader.get_value_at_second(second)),
            ) * self.base_generation_capacity_mw
        return (
            self._base_static_load_mw
            + background
            + sum(max(0.0, float(value)) for value in self.extra_loads.values())
            - base_generation
            - renewable
        )

    def get_total_generation(self) -> float:
        """Implementation documentation."""
        total_load = self.get_total_load()
        # Build the displayed total from the same dispatch telemetry used by
        # the breakdown.  In particular, renewable values must be the actual
        # dispatched/absorbed MW, not the available forecast before curtailment.
        breakdown = self.get_generation_breakdown()
        local_generation = sum(
            max(0.0, float(breakdown.get(name, 0.0) or 0.0))
            for name in (
                "base_generation_mw", "conventional_generation_mw",
                "wind_generation_mw", "pv_generation_mw",
                "storage_discharge_mw",
            )
        )
        grid_import = breakdown.get("grid_import_mw")
        if grid_import is None:
            # A failed/stale AC result has no ext-grid telemetry.  Infer only
            # the balancing purchase needed by the current actual local
            # dispatch; do not re-introduce uncurtailed renewable availability.
            grid_import = max(0.0, total_load - local_generation)
        result = local_generation + max(0.0, float(grid_import))
        if result > 0.0:
            return float(result)
        return float(total_load)

    def get_generation_breakdown(self) -> Dict[str, float]:
        """Return non-overlapping instantaneous supply components in MW."""
        conventional = 0.0
        if self._power_flow_valid and len(self.network.res_gen):
            conventional = float(self.network.res_gen["p_mw"].sum())
        elif len(self.network.gen):
            conventional = float(self.network.gen["p_mw"].sum())
        renewable = self.get_renewable_dispatch_summary()
        wind = float(renewable["wind"]["dispatched_mw"])
        pv = float(renewable["pv"]["dispatched_mw"])
        conventional = max(0.0, conventional - wind - pv)
        if not self.use_topology_generators:
            conventional = 0.0
        external = self.get_grid_exchange_mw()
        external_import = max(0.0, float(external or 0.0))
        storage = sum(max(0.0, float(value)) for value in self.storage_powers.values())
        return {
            "base_generation_mw": float(getattr(self, "_last_base_generation_mw", 0.0)),
            "conventional_generation_mw": conventional,
            "grid_import_mw": external_import,
            "wind_generation_mw": wind,
            "pv_generation_mw": pv,
            "storage_discharge_mw": storage,
        }

    def get_total_emission(self) -> float:
        """Implementation documentation."""
        if len(self.network.res_gen) == 0:
            return 0.0
        total = 0.0
        for idx in self.network.gen.index:
            if idx not in self.network.res_gen.index:
                continue
            p_mw = self.network.res_gen.loc[idx, 'p_mw']
            if pd.isna(p_mw) or p_mw <= 0:
                continue
            bus_id = self.network.gen.loc[idx, 'bus']
            gen_type = self.GEN_TYPE_MAP.get(bus_id, 'coal')
            total += p_mw * self.EMISSION_FACTORS.get(gen_type, 0.5)
        return total

    def get_renewable_generation(self) -> Dict[str, Optional[float]]:
        """Implementation documentation."""
        return {
            "wind_mw": self._last_wind_generation_mw,
            "pv_mw": self._last_pv_generation_mw,
        }

    def apply_renewable_dispatch(self, dispatch_mw: Dict[str, float]) -> Dict[str, float]:
        """Set actual wind/PV dispatch without changing the available profiles."""
        actual = {}
        for source in ("wind", "pv"):
            available = float(self.renewable_available_mw.get(source, 0.0))
            power = min(available, max(0.0, float(dispatch_mw.get(source, available))))
            factor = power / available if available > 1e-9 else 0.0
            for index, available_at_bus in self.renewable_gen_available_mw.get(source, {}).items():
                if index in self.network.gen.index:
                    self.network.gen.at[index, "p_mw"] = available_at_bus * factor
            self.renewable_dispatched_mw[source] = power
            actual[source] = power
        if not self.use_topology_generators:
            self._apply_base_generation(self.current_second)
        return actual

    def get_renewable_dispatch_summary(self) -> Dict[str, Dict[str, float]]:
        return {
            source: {
                "available_mw": float(self.renewable_available_mw.get(source, 0.0)),
                "dispatched_mw": float(self.renewable_dispatched_mw.get(source, 0.0)),
                "curtailed_mw": max(0.0, float(self.renewable_available_mw.get(source, 0.0)) - float(self.renewable_dispatched_mw.get(source, 0.0))),
            }
            for source in ("wind", "pv")
        }

    def get_renewable_absorption_limit_mw(self) -> float:
        """Return the load headroom available for wind/PV before grid export.

        Fixed base-generation plans and discharged storage are supplied
        first.  Renewable dispatch must fit the remaining demand so that a
        full-dispatch renewable ratio cannot be mistaken for local
        consumption when the system is already over-generated.
        """
        total_load = float(self.network.load["p_mw"].sum()) if len(self.network.load) else 0.0
        renewable_indices = {
            index for indices in self.renewable_gen_indices.values() for index in indices
        }
        conventional_generation = sum(
            float(row["p_mw"])
            for index, row in self.network.gen.iterrows()
            if index not in renewable_indices
        )
        storage_discharge = sum(
            max(0.0, float(power)) for power in self.storage_powers.values()
        )
        fixed_generation = (
            max(0.0, float(getattr(self, "_last_base_generation_mw", 0.0)))
            + (conventional_generation if self.use_topology_generators else 0.0)
            + storage_discharge
        )
        return max(0.0, total_load - fixed_generation)

    def get_operating_constraint_snapshot(
        self,
        min_voltage_pu: float = 0.95,
        max_voltage_pu: float = 1.05,
        max_line_loading_pct: float = 100.0,
    ) -> Dict[str, Optional[float | bool]]:
        if not self._power_flow_valid or not len(self.network.res_bus):
            return {"feasible": False, "min_voltage_pu": None, "max_voltage_pu": None, "max_line_loading_pct": None}
        voltages = self.network.res_bus.loc[self.network.bus.in_service, "vm_pu"]
        voltages = voltages[np.isfinite(voltages)]
        lines = self.network.res_line.loc[self.network.line.in_service, "loading_percent"]
        lines = lines[np.isfinite(lines)]
        if len(voltages) != int(self.network.bus.in_service.sum()) or len(lines) != int(self.network.line.in_service.sum()):
            return {"feasible": False, "min_voltage_pu": None, "max_voltage_pu": None, "max_line_loading_pct": None}
        min_voltage = float(voltages.min())
        max_voltage = float(voltages.max())
        max_loading = float(lines.max()) if len(lines) else 0.0
        feasible = min_voltage >= min_voltage_pu and max_voltage <= max_voltage_pu and max_loading <= max_line_loading_pct
        return {"feasible": feasible, "min_voltage_pu": min_voltage, "max_voltage_pu": max_voltage, "max_line_loading_pct": max_loading}

    def optimize_renewable_grid_constraints(
        self,
        min_voltage_pu: float = 0.95,
        max_voltage_pu: float = 1.05,
        max_line_loading_pct: float = 100.0,
    ) -> Optional[list[dict]]:
        """Linearize bus-voltage and line-loading limits around the current AC solution."""
        if not self._power_flow_valid or not self.renewable_gen_indices:
            return None

        sources = [source for source in ("wind", "pv") if self.renewable_available_mw.get(source, 0.0) > 1e-6 and source in self.renewable_gen_indices]
        if not sources:
            return []

        def measurements() -> dict[str, float]:
            result = {}
            for bus, row in self.network.res_bus.iterrows():
                voltage = row.get("vm_pu")
                if pd.notna(voltage) and np.isfinite(float(voltage)):
                    result[f"v:{bus}"] = float(voltage)
            for line, row in self.network.res_line.iterrows():
                loading = row.get("loading_percent")
                if pd.notna(loading) and np.isfinite(float(loading)):
                    result[f"l:{line}"] = float(loading)
            return result

        original = {source: float(self.renewable_dispatched_mw[source]) for source in sources}
        base_measurements = measurements()
        gradients: dict[str, dict[str, float]] = {metric: {} for metric in base_measurements}
        try:
            for source in sources:
                reduction = min(1.0, original[source] * 0.1)
                if reduction <= 1e-6:
                    continue
                trial = dict(original)
                trial[source] -= reduction
                self.apply_renewable_dispatch(trial)
                try:
                    pp.runpp(self.network, numba=False, enforce_q_lims=True)
                except Exception:
                    return None
                lower_measurements = measurements()
                for metric, base in base_measurements.items():
                    if metric in lower_measurements:
                        gradients[metric][source] = (lower_measurements[metric] - base) / -reduction
        finally:
            self.apply_renewable_dispatch(original)
            try:
                pp.runpp(self.network, numba=False, enforce_q_lims=True)
                self._power_flow_valid = True
            except Exception:
                self._power_flow_valid = False

        constraints = []
        for metric, base_value in base_measurements.items():
            for direction, limit in ((1.0, max_voltage_pu), (-1.0, -min_voltage_pu)) if metric.startswith("v:") else ((1.0, max_line_loading_pct),):
                coefficients = {source: direction * gradients[metric].get(source, 0.0) for source in sources}
                current_rhs = float(limit) - direction * base_value + sum(coefficients[source] * original[source] for source in sources)
                constraints.append({"coefficients": coefficients, "rhs": current_rhs})
        return constraints

    def apply_heuristic_renewable_curtailment(
        self,
        min_voltage_pu: float = 0.95,
        max_voltage_pu: float = 1.05,
        max_line_loading_pct: float = 100.0,
        iterations: int = 10,
    ) -> Dict[str, float | str]:
        """Keep all renewable power unless AC power flow violates an operating limit."""
        available = dict(self.renewable_available_mw)
        total_available = sum(available.values())
        absorption_limit = self.get_renewable_absorption_limit_mw()
        balance_factor = (
            min(1.0, absorption_limit / total_available)
            if total_available > 1e-9 else 1.0
        )
        available = {
            source: power * balance_factor for source, power in available.items()
        }

        def evaluate(factor: float) -> Dict[str, Optional[float | bool]]:
            self.apply_renewable_dispatch({source: power * factor for source, power in available.items()})
            self.run_power_flow()
            return self.get_operating_constraint_snapshot(min_voltage_pu, max_voltage_pu, max_line_loading_pct)

        full_state = evaluate(1.0)
        if full_state["feasible"]:
            return {"status": "full", "dispatch_factor": 1.0}

        samples = [(1.0, full_state)]
        for index in range(9, -1, -1):
            factor = index / 10.0
            samples.append((factor, evaluate(factor)))
        feasible_samples = [(factor, state) for factor, state in samples if state["feasible"]]
        if not feasible_samples:
            def violation(state):
                if any(state.get(key) is None for key in ("min_voltage_pu", "max_voltage_pu", "max_line_loading_pct")):
                    return float("inf")
                return (
                    max(0.0, min_voltage_pu - float(state["min_voltage_pu"]))
                    + max(0.0, float(state["max_voltage_pu"]) - max_voltage_pu)
                    + max(0.0, float(state["max_line_loading_pct"]) - max_line_loading_pct) / 100.0
                )

            factor, _ = min(samples, key=lambda item: (violation(item[1]), -item[0]))
            self.apply_renewable_dispatch({source: power * factor for source, power in available.items()})
            self.run_power_flow()
            return {"status": "grid_infeasible", "dispatch_factor": factor}

        lower = max(factor for factor, _ in feasible_samples)
        upper = min((factor for factor, state in samples if factor > lower and not state["feasible"]), default=1.0)
        for _ in range(max(1, iterations)):
            middle = (lower + upper) / 2.0
            if evaluate(middle)["feasible"]:
                lower = middle
            else:
                upper = middle
        self.apply_renewable_dispatch({source: power * lower for source, power in available.items()})
        self.run_power_flow()
        return {"status": "curtailed", "dispatch_factor": lower}

    def get_grid_exchange_mw(self) -> Optional[float]:
        """Implementation documentation."""
        if not self._power_flow_valid or not len(self.network.res_ext_grid):
            return None
        return float(self.network.res_ext_grid["p_mw"].sum())

    def get_network_loss_mw(self) -> Optional[float]:
        """Return current active-power losses after a converged AC solve."""
        if not self._power_flow_valid:
            return None
        line_loss = (
            float(self.network.res_line["pl_mw"].sum())
            if len(self.network.res_line) and "pl_mw" in self.network.res_line else 0.0
        )
        trafo_loss = (
            float(self.network.res_trafo["pl_mw"].sum())
            if len(self.network.res_trafo) and "pl_mw" in self.network.res_trafo else 0.0
        )
        return line_loss + trafo_loss

    def get_reserve_capacity_mw(self) -> Optional[float]:
        """Implementation documentation."""
        if not self._power_flow_valid or not len(self.network.gen):
            return None
        headroom = 0.0
        for index, generator in self.network.gen.iterrows():
            if index in self.dynamic_gen_indices or not bool(generator.get("in_service", True)):
                continue
            if index not in self.network.res_gen.index:
                continue
            max_power = generator.get("max_p_mw")
            actual_power = self.network.res_gen.at[index, "p_mw"]
            if pd.notna(max_power) and pd.notna(actual_power):
                headroom += max(0.0, float(max_power) - float(actual_power))
        return headroom

    def get_voltage_health(
        self,
        min_voltage_pu: float = 0.95,
        max_voltage_pu: float = 1.05,
    ) -> Dict[str, Optional[float | int | str]]:
        """Report the current operating-point voltage range, not CPF margin."""
        if not self._power_flow_valid or not len(self.network.res_bus):
            return {
                "status": "unknown",
                "min_voltage_pu": None,
                "max_voltage_pu": None,
                "violating_buses": None,
            }
        in_service = self.network.bus.index[self.network.bus.in_service]
        voltage = self.network.res_bus.loc[in_service, "vm_pu"]
        voltage = voltage[np.isfinite(voltage)]
        if voltage.empty:
            return {
                "status": "unknown",
                "min_voltage_pu": None,
                "max_voltage_pu": None,
                "violating_buses": None,
            }
        violating = int(((voltage < min_voltage_pu) | (voltage > max_voltage_pu)).sum())
        return {
            "status": "out_of_range" if violating else "normal",
            "min_voltage_pu": float(voltage.min()),
            "max_voltage_pu": float(voltage.max()),
            "violating_buses": violating,
        }

    def get_line_health(self) -> Dict[str, Optional[float | int | str]]:
        if not self._power_flow_valid or not len(self.network.res_line):
            return {"max_loading_pct": None, "overloaded_lines": None, "status": "unknown"}
        loading = self.network.res_line.loc[self.network.line.in_service, "loading_percent"]
        loading = loading[np.isfinite(loading)]
        if loading.empty:
            return {"max_loading_pct": None, "overloaded_lines": None, "status": "unknown"}
        max_loading = float(loading.max()) if len(loading) else 0.0
        overloaded = int((loading > 100.0).sum())
        status = "overloaded" if overloaded else "watch" if max_loading >= 90.0 else "normal"
        return {"max_loading_pct": max_loading, "overloaded_lines": overloaded, "status": status}

    def get_generation_by_type(self) -> Dict[str, float]:
        """Implementation documentation."""
        result = {k: 0.0 for k in self.EMISSION_FACTORS.keys()}
        if len(self.network.res_gen) == 0:
            return result
        for idx in self.network.gen.index:
            if idx not in self.network.res_gen.index:
                continue
            p_mw = self.network.res_gen.loc[idx, 'p_mw']
            if pd.isna(p_mw) or p_mw <= 0:
                continue
            bus_id = self.network.gen.loc[idx, 'bus']
            gen_type = self.GEN_TYPE_MAP.get(bus_id, 'coal')
            result[gen_type] = result.get(gen_type, 0.0) + p_mw
        return result

    def get_network(self):
        """Implementation documentation."""
        return self.network

    # Implementation note.

    def analyze_voltage_stability(
        self,
        step: float = 0.05,
        max_steps: int = 50
    ) -> Dict:
        t0 = time.time()
        result = run_continuation_power_flow(
            self.network,
            step=step,
            max_steps=max_steps
        )
        self.last_cpf_time = time.time() - t0

        if self.verbose:
            print(f"[CPF] {self.last_cpf_time:.3f}s, λ={result['lambda_critical']:.2f}")

        return result
