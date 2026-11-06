# src/simulation/engine.py
import simpy
import math
import threading
import time
import copy
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional
from collections import defaultdict

from src.domain.task import Task
from src.domain.enums import TaskStatus
from src.domain.datacenter import DataCenter
from src.domain.storage import EnergyStorage
from src.optimization.copt_optimizer import CoptOptimizer
from src.optimization.power_dispatch import PowerDispatchOptimizer
from src.optimization.local_coordination import LocalCoordinationOptimizer
from src.optimization.global_coordination import GlobalCoordinationOptimizer
from src.optimization.objectives import load_objective
from src.infrastructure.pandapower_adapter import PandapowerAdapter
from src.simulation.datacenter_sim import DataCenterSim
from src.simulation.scheduler import Scheduler
from src.simulation.network_sim import BandwidthManager
from src.simulation.grid_health import calculate_grid_health
from src.strategies.registry import StrategyRegistry
from src.strategies.mapping.linear import LinearMapping
from src.strategies.electricity.marginal import MarginalElectricityPricing
from src.strategies.electricity.fixed_tou import FixedTimeOfUsePricing
from src.strategies.electricity.custom_parameterized import GridResponsivePricing
from src.loaders.ts_loader import TimeSeriesDataLoader
from src.loaders.config_loader import (
    load_platform_config,
    build_data_centers,
    build_storages,
    get_grid_config,
    get_simulation_config,
    resolve_path,
)
from src.loaders.topology_loader import (
    load_topology,
    topology_grid_overrides,
    validate_grid_bus_references,
    validate_topology,
)
from src.loaders.user_config_loader import (
    load_user_config,
    merge_frontend_config,
    validate_frontend_user_config,
    validate_user_resources,
)


def format_emission_log(total_emission: float) -> str:
    """Use GBK-safe console text; rich Unicode remains available in the API."""
    return f"  Total grid emissions: {float(total_emission):.2f} tCO2/h"


def format_idle_log() -> str:
    """Return a console-safe idle message without emoji variation selectors."""
    return "  No running tasks"


def task_power_snapshot(tasks, data_center_sims, second: float) -> Dict[str, float]:
    """Return planned/actual task power and compute at one simulation second."""
    planned_power = actual_power = planned_compute = actual_compute = 0.0
    for task in tasks or []:
        submitted = float(getattr(task, "submitted_at", 0.0) or 0.0)
        duration = max(0.0, float(getattr(task, "duration_hours", 0.0) or 0.0) * 3600.0)
        planned_end = submitted + duration
        compute = max(0.0, float(getattr(task, "required_compute", 0.0) or 0.0))
        power = getattr(task, "actual_power_mw", None)
        center_id = getattr(task, "allocated_data_center_id", None)
        center_sim = (data_center_sims or {}).get(center_id)
        if center_sim is None and data_center_sims:
            center_sim = next(iter(data_center_sims.values()))
        if center_sim is not None:
            try:
                power = center_sim.dc.power_for_task_mw(task)
            except (AttributeError, TypeError, ValueError):
                pass
        power = max(0.0, float(power or 0.0))
        if submitted <= float(second) < planned_end:
            planned_power += power
            planned_compute += compute
        actual_start = getattr(task, "actual_start_second", None)
        actual_end = getattr(task, "actual_end_second", None)
        if actual_start is not None and float(actual_start) <= float(second):
            if actual_end is None or float(second) < float(actual_end):
                actual_power += power
                actual_compute += compute
    return {
        "planned_task_power_mw": planned_power,
        "actual_task_power_mw": actual_power,
        "planned_task_compute_tflops": planned_compute,
        "actual_task_compute_tflops": actual_compute,
    }


def calculate_net_load_mw(initial_load_mw: float, planned_generation_mw: float, actual_compute_power_mw: float) -> float:
    """Calculate the requested net-load view without folding in renewables."""
    return float(initial_load_mw or 0.0) - float(planned_generation_mw or 0.0) + float(actual_compute_power_mw or 0.0)


def display_load_with_network_loss(total_load_mw: float, network_loss_mw: Optional[float]) -> float:
    """Return the load shown in balance charts, including AC network losses."""
    load = max(0.0, float(total_load_mw or 0.0))
    loss = max(0.0, float(network_loss_mw or 0.0))
    return load + loss


def aggregate_storage_power_samples(points, bucket_seconds: float = 300.0) -> list[tuple[float, float]]:
    """Average storage power samples into fixed time buckets for plotting."""
    width = max(1.0, float(bucket_seconds or 300.0))
    buckets = {}
    for point in points or []:
        try:
            second = max(0.0, float(point.get("second", 0.0) or 0.0))
            power = float(point.get("storage_power_mw", 0.0) or 0.0)
        except (AttributeError, TypeError, ValueError):
            continue
        bucket = int(second // width)
        total, count = buckets.get(bucket, (0.0, 0))
        buckets[bucket] = (total + power, count + 1)
    return [
        ((bucket + 0.5) * width, total / count)
        for bucket, (total, count) in sorted(buckets.items())
        if count
    ]


def format_total_cost_log(total_cost_yuan: float) -> str:
    return f"  Total operating cost: {float(total_cost_yuan):,.2f} CNY"


def _merge_frontend_config(base: dict, user: dict) -> dict:
    """Translate frontend JSON sections into the engine's legacy config shape."""
    import copy

    config = copy.deepcopy(base)
    for section in ("simulation", "optimization", "data_centers", "storages", "topology"):
        if section in user:
            config[section] = copy.deepcopy(user[section])
    if "pricing" in user:
        config["electricity_pricing"] = copy.deepcopy(user["pricing"])
    if "tasks" in user:
        config["tasks"] = copy.deepcopy(user["tasks"])
    sources = user.get("data_sources", {})
    if sources:
        grid = config.setdefault("grid", {})
        mapping = {
            "load_file": "load_file",
            "pv_file": "pv_file",
            "wind_file": "wind_file",
            "base_generation_file": "base_generation_file",
            "topology_file": "topology_file",
        }
        for source_key, grid_key in mapping.items():
            if sources.get(source_key) is not None:
                grid[grid_key] = sources[source_key]
        if sources.get("task_file") is not None:
            config.setdefault("tasks", {})["excel_file"] = sources["task_file"]
    return config

class SimulationEngine:

    @staticmethod
    def _reserve_task_resources(task: Task, data_center: DataCenter) -> None:
        """Reserve a placement before starting its SimPy process.

        Dispatch happens in a tight loop while SimPy processes have not run
        yet.  Reserving here keeps the scheduler's view in sync with the
        actual data-center capacity and prevents a batch from oversubscribing
        one center.
        """
        if getattr(task, "_resources_reserved", False):
            return
        data_center.allocate(task)
        task._resources_reserved = True

    def __init__(
            self,
            config_file: Optional[str] = None,
            time_scale: Optional[float] = None,
            load_file: Optional[str] = None,
            pv_file: Optional[str] = None,
            wind_file: Optional[str] = None,
            base_generation_file: Optional[str] = None,
            user_config_file: Optional[str] = None,
            user_config: Optional[dict] = None,
            ui_config: Optional[dict] = None,
            stats_interval_seconds: Optional[int] = None
    ):
        self.env = simpy.Environment()

        # Implementation note.
        try:
            config = load_platform_config(config_file)
        except FileNotFoundError:
            print("Configuration file not found; using defaults")
            config = {}

        self.user_config = {}
        if user_config_file or user_config is not None:
            self.user_config = load_user_config(
                user_config_file=user_config_file,
                user_config=user_config,
                defaults={},
            )
            if user_config is not None and ui_config is not None:
                validate_frontend_user_config(self.user_config)
                config, _ = merge_frontend_config(config, self.user_config, ui_config or {})
                topology_for_validation = config.get("topology")
            else:
                config = _merge_frontend_config(config, self.user_config)
                topology_for_validation = self.user_config.get("topology")
                topology_file_for_validation = self.user_config.get("data_sources", {}).get("topology_file")
                if topology_file_for_validation:
                    topology_for_validation = load_topology(resolve_path(topology_file_for_validation))
        elif ui_config:
            config, _ = merge_frontend_config(config, {}, ui_config)

        topology_for_validation = config.get("topology")
        topology_file_for_validation = config.get("grid", {}).get("topology_file")
        if topology_file_for_validation and topology_for_validation is None:
            topology_for_validation = load_topology(resolve_path(topology_file_for_validation))
        if topology_for_validation is not None:
            validate_topology(topology_for_validation)
            config.setdefault("grid", {}).update(topology_grid_overrides(topology_for_validation))
            validate_grid_bus_references(topology_for_validation, config["grid"])
        validate_user_resources(config, topology_for_validation)

        grid_cfg = get_grid_config(config)
        sim_cfg = get_simulation_config(config)
        report_cfg = config.get("reports", {}) if isinstance(config.get("reports", {}), dict) else {}
        self.report_dir = resolve_path(report_cfg.get("directory")) or str(Path("reports"))
        grid_time_compression = float(grid_cfg.get("time_compression", 1.0))
        time_series_start_offset_seconds = int(grid_cfg.get("time_series_start_offset_seconds", 0))
        start_datetime = sim_cfg.get("start_datetime", "2026-01-01T00:00:00")
        self.simulation_start_time = datetime.fromisoformat(start_datetime)
        self.optimization_config = config.get("optimization", {})
        if not isinstance(self.optimization_config, dict):
            self.optimization_config = {}
        self.scheduling_config = config.get("scheduling", {})
        if not isinstance(self.scheduling_config, dict):
            self.scheduling_config = {}
        self.pricing_config = config.get("electricity_pricing", {})
        if not isinstance(self.pricing_config, dict):
            raise ValueError("electricity_pricing must be a mapping with mode and optional periods")
        self.storage_control_config = config.get("storage_control", {})
        if not isinstance(self.storage_control_config, dict):
            self.storage_control_config = {}
        pricing_file = resolve_path(self.pricing_config.get("file"))
        # Implementation note.
        self.base_tou_pricing = (
            FixedTimeOfUsePricing.from_csv(pricing_file)
            if pricing_file
            else FixedTimeOfUsePricing(self.pricing_config.get("periods", []))
            if self.pricing_config.get("periods")
            else None
        )
        self.fixed_pricing = self.base_tou_pricing if self.pricing_config.get("mode") == "fixed_tou" else None

        # Implementation note.
        base_mva = grid_cfg.get('base_mva', 100.0)
        load_factor = grid_cfg.get('load_factor', 1.0)
        load_capacity_mw = grid_cfg.get('load_capacity_mw')
        pv_bus = grid_cfg.get('pv_bus', 10)
        wind_bus = grid_cfg.get('wind_bus', 20)
        pv_secondary_bus = grid_cfg.get('pv_secondary_bus')
        wind_secondary_bus = grid_cfg.get('wind_secondary_bus')
        pv_secondary_ratio = float(grid_cfg.get('pv_secondary_ratio', 0.0))
        wind_secondary_ratio = float(grid_cfg.get('wind_secondary_ratio', 0.0))
        pv_capacity_mw = float(grid_cfg.get('pv_capacity_mw', base_mva))
        wind_capacity_mw = float(grid_cfg.get('wind_capacity_mw', base_mva))
        base_generation_capacity_mw = float(grid_cfg.get('base_generation_capacity_mw', base_mva))
        external_grid_max_import_mw = grid_cfg.get('external_grid_max_import_mw')
        use_topology_generators = bool(grid_cfg.get('use_topology_generators', True))

        # Implementation note.
        load_file = resolve_path(load_file) or grid_cfg.get('load_file')
        pv_file = resolve_path(pv_file) or grid_cfg.get('pv_file')
        wind_file = resolve_path(wind_file) or grid_cfg.get('wind_file')
        base_generation_file = resolve_path(base_generation_file) or grid_cfg.get('base_generation_file')
        topology_file = resolve_path(grid_cfg.get('topology_file'))
        topology = config.get("topology")
        runtime_topology = topology
        if runtime_topology is None and topology_file:
            runtime_topology = load_topology(topology_file)

        step_seconds = sim_cfg.get('step_seconds', 5)
        stats_interval_seconds = (
            stats_interval_seconds
            if stats_interval_seconds is not None
            else sim_cfg.get('stats_interval_seconds', 1)
        )
        renewable_optimization_interval_seconds = sim_cfg.get(
            'renewable_optimization_interval_seconds', 5
        )
        report_interval_seconds = sim_cfg.get('report_interval_seconds', 5)
        cpf_interval_seconds = sim_cfg.get('cpf_interval_seconds', 30)
        power_flow_interval_seconds = sim_cfg.get('power_flow_interval_seconds', 300)
        time_scale = (
            time_scale
            if time_scale is not None
            else sim_cfg.get('time_scale', 1.0)
        )

        print("\nConfiguration loaded:")
        print(f"Base capacity: {base_mva} MVA")
        print(f"Load scale: {load_factor}")
        print(f"PV bus: {pv_bus}, Wind bus: {wind_bus}")
        print(f"Time-series start offset: {time_series_start_offset_seconds}s")
        print(f"PV/Wind capacity: {pv_capacity_mw}/{wind_capacity_mw} MW")
        print(f"Statistics interval: {stats_interval_seconds}s")
        print(f"Renewable optimization interval: {renewable_optimization_interval_seconds}s")
        print(f"Report interval: {report_interval_seconds}s")
        print(f"Continuation power-flow interval: {cpf_interval_seconds}s")
        print(f"Power-flow refresh interval: {power_flow_interval_seconds}s")
        print(f"Load file: {load_file}")
        print(f"PV file: {pv_file}")
        print(f"Wind file: {wind_file}")
        print(f"Base generation file: {base_generation_file}")

        # Implementation note.
        self.load_loader = None
        self.pv_loader = None
        self.wind_loader = None
        self.base_generation_loader = None

        if load_file and Path(load_file).exists():
            self.load_loader = TimeSeriesDataLoader(load_file, "electric load", grid_time_compression, time_series_start_offset_seconds)
        elif load_file:
            print(f"Load data file not found: {load_file}")

        if pv_file and Path(pv_file).exists():
            self.pv_loader = TimeSeriesDataLoader(pv_file, "PV generation", grid_time_compression, time_series_start_offset_seconds)
        elif pv_file:
            print(f"PV data file not found: {pv_file}")

        if wind_file and Path(wind_file).exists():
            self.wind_loader = TimeSeriesDataLoader(wind_file, "wind generation", grid_time_compression, time_series_start_offset_seconds)
        elif wind_file:
            print(f"Wind data file not found: {wind_file}")

        if base_generation_file and Path(base_generation_file).exists():
            self.base_generation_loader = TimeSeriesDataLoader(
                base_generation_file, "base generation per unit", grid_time_compression,
                time_series_start_offset_seconds,
            )
        elif base_generation_file:
            print(f"Base generation data file not found: {base_generation_file}")

        # A topology may declare direct MW profiles for each renewable node.
        # These are independent physical injections, unlike legacy aggregate
        # PV/wind profiles that require runtime capacity scaling.
        renewable_node_loaders = []
        for renewable in (runtime_topology or {}).get("renewables", []):
            power_file = renewable.get("power_file")
            if not power_file:
                continue
            source = str(renewable.get("type", "")).lower()
            if source not in {"pv", "wind"}:
                raise ValueError("topology renewable type must be pv or wind")
            node_power_path = resolve_path(str(power_file))
            if node_power_path is None or not Path(node_power_path).exists():
                raise FileNotFoundError(
                    f"Renewable bus {renewable.get('name', renewable.get('bus_id'))} output file not found: {power_file}"
                )
            renewable_node_loaders.append({
                "type": source,
                "bus_id": int(renewable["bus_id"]),
                "loader": TimeSeriesDataLoader(
                    node_power_path,
                    f"{renewable.get('name', source)} bus output (MW)",
                    grid_time_compression,
                    time_series_start_offset_seconds,
                ),
            })

        # Implementation note.
        if self.pricing_config.get("mode") == "custom":
            custom_parameters = {
                key: value for key, value in self.pricing_config.get("parameters", {}).items()
                if key != "name"
            }
            electricity_strategy = GridResponsivePricing(**custom_parameters)
        else:
            electricity_strategy = MarginalElectricityPricing(
                base_price=0.5,
                sensitivity=0.10,
                scale=1000.0,
                base_pricing=self.base_tou_pricing,
            )

        # Implementation note.
        self.pp_adapter = PandapowerAdapter(
            load_loader=self.load_loader,
            pv_loader=self.pv_loader,
            wind_loader=self.wind_loader,
            renewable_node_loaders=renewable_node_loaders,
            base_generation_loader=self.base_generation_loader,
            topology=topology,
            topology_file=topology_file,
            pv_bus=pv_bus,
            wind_bus=wind_bus,
            pv_secondary_bus=pv_secondary_bus,
            wind_secondary_bus=wind_secondary_bus,
            pv_secondary_ratio=pv_secondary_ratio,
            wind_secondary_ratio=wind_secondary_ratio,
            load_factor=load_factor,
            base_mva=base_mva,
            load_capacity_mw=load_capacity_mw,
            base_generation_capacity_mw=base_generation_capacity_mw,
            pv_capacity_mw=pv_capacity_mw,
            wind_capacity_mw=wind_capacity_mw,
            external_grid_max_import_mw=external_grid_max_import_mw,
            use_topology_generators=use_topology_generators,
            electricity_strategy=electricity_strategy,
            verbose=False
        )

        # Implementation note.
        self.bandwidth_manager = BandwidthManager(self.env)

        # Implementation note.
        self.data_centers = build_data_centers(config)
        self.storages: Dict[str, EnergyStorage] = {
            storage.id: storage for storage in build_storages(config)
        }
        self.pp_adapter.set_storage_models(self.storages.values())
        if not self.data_centers:
            print("No datacenters configured; using defaults")
            self.data_centers = [
                DataCenter(
                    id="DC1", name="Datacenter A", region="region_a",
                    total_compute=200.0, total_bandwidth=2000.0, grid_bus_id=3
                ),
                DataCenter(
                    id="DC2", name="Datacenter B", region="region_a",
                    total_compute=160.0, total_bandwidth=1600.0, grid_bus_id=16
                ),
                DataCenter(
                    id="DC3", name="Datacenter C", region="region_b",
                    total_compute=240.0, total_bandwidth=4000.0, grid_bus_id=21
                ),
            ]

        # Implementation note.
        for dc in self.data_centers:
            self.bandwidth_manager.register_data_center(dc)

        # Implementation note.
        strategy_name = self.scheduling_config.get("strategy", "lowest_price")
        strategy_settings = self.scheduling_config.get("settings", {})
        if strategy_name == "lowest_price":
            strategy_settings = {"allow_cross_region": True, **strategy_settings}
        self.scheduler = Scheduler(
            StrategyRegistry().create_allocation(strategy_name, strategy_settings)
        )

        # Implementation note.
        self.dc_sims = {}
        for dc in self.data_centers:
            mapping_strategy = LinearMapping(base_coefficient=dc.power_per_tflops)
            self.dc_sims[dc.id] = DataCenterSim(
                env=self.env,
                datacenter=dc,
                pp_adapter=self.pp_adapter,
                bandwidth_manager=self.bandwidth_manager,
                mapping_strategy=mapping_strategy
            )
            self.dc_sims[dc.id].step_seconds = float(step_seconds)

        # Implementation note.
        self.pending_tasks: List[Task] = []  # Implementation note.
        self.ready_tasks: List[Task] = []  # Implementation note.
        self.task_registry: Dict[str, Task] = {}
        self.dispatched_task_ids = set()

        # Implementation note.
        self.stats_records: Dict[int, Dict] = {}
        self.stats_count = 0
        self.total_operating_cost_yuan = 0.0
        self.current_second = 0
        self.renewable_energy_mwh = {
            source: {"available": 0.0, "consumed": 0.0, "curtailed": 0.0}
            for source in ("wind", "pv")
        }
        self.renewable_dispatch_result = {"status": "not_started", "mode": "heuristic"}
        self.storage_dispatch_result = {
            "status": "idle", "reason": "not_started",
            "requested_power_mw": 0.0, "actual_power_mw": 0.0,
            "price": None,
        }
        self.last_cpf_result = None
        self._last_renewable_optimization_second = None
        self._last_report_second = None
        self._last_cpf_second = None
        self._last_power_flow_second = None
        self._last_dispatch_second = None
        self._renewable_dispatch_factors = {"wind": 1.0, "pv": 1.0}
        self._storage_load_reference_mw = None
        self._last_storage_optimization_second = None
        self._storage_requested_power_mw = {}
        self._optimization_state_version = 0
        self._optimization_cache = OrderedDict()
        self._optimization_cache_limit = 32

        # Implementation note.
        self.running = False
        self.time_scale = time_scale
        self.step_seconds = step_seconds
        self.stats_interval_seconds = stats_interval_seconds
        self.renewable_optimization_interval_seconds = renewable_optimization_interval_seconds
        self.report_interval_seconds = report_interval_seconds
        self.cpf_interval_seconds = cpf_interval_seconds
        self.power_flow_interval_seconds = power_flow_interval_seconds

        # Construction only loads configuration.  The Web start button or
        # CLI explicitly starts the worker so validation/tests cannot leak
        # background solver threads.
        self.thread = None
        print("Simulation engine initialized")

    # Implementation note.

    def _bump_state_version(self, reason: str = "") -> None:
        """Invalidate optimization reuse after a simulation state mutation."""
        self._optimization_state_version = int(
            getattr(self, "_optimization_state_version", 0)
        ) + 1
        cache = getattr(self, "_optimization_cache", None)
        if cache is not None:
            cache.clear()

    def _optimization_cache_get(self, key: tuple):
        cache = getattr(self, "_optimization_cache", None)
        if cache is None:
            return None
        value = cache.get(key)
        if value is None:
            return None
        cache.move_to_end(key)
        return copy.deepcopy(value)

    def _optimization_cache_put(self, key: tuple, value: dict) -> None:
        cache = getattr(self, "_optimization_cache", None)
        if cache is None:
            cache = self._optimization_cache = OrderedDict()
        cache[key] = copy.deepcopy(value)
        cache.move_to_end(key)
        limit = max(1, int(getattr(self, "_optimization_cache_limit", 32)))
        while len(cache) > limit:
            cache.popitem(last=False)

    def _optimization_cache_key(self, mode: str, tasks: List[Task]) -> tuple:
        config = getattr(self, "optimization_config", {})
        task_fingerprint = tuple(
            (
                getattr(task, "id", None),
                getattr(task, "status", None),
                getattr(task, "allocated_data_center_id", None),
                getattr(task, "submitted_at", None),
                getattr(task, "deadline_second", None),
                getattr(task, "duration_hours", None),
                getattr(task, "required_compute", None),
                getattr(task, "required_bandwidth_mbps", None),
                getattr(task, "required_gpu_count", None),
            )
            for task in tasks
        )
        return (
            mode,
            float(getattr(self, "current_second", 0)),
            int(getattr(self, "_optimization_state_version", 0)),
            repr(config),
            task_fingerprint,
        )

    def submit_task(self, task: Task):
        """Implementation documentation."""
        if task.id in self.task_registry:
            print(f"[skip] duplicate task ID {task.id}，not registering again")
            return
        if task.status == TaskStatus.PENDING:
            task.transition_to(TaskStatus.SUBMITTED)
        self.pending_tasks.append(task)
        self.task_registry[task.id] = task
        self._bump_state_version("task_submitted")
        print(f"[register] Task {task.id} registered; scheduled submission time {task.submitted_at:.0f}s")
        self.pending_tasks.sort(key=lambda t: getattr(t, 'submitted_at', 0))

    def submit_tasks(self, tasks: List[Task]) -> int:
        """Implementation documentation."""
        accepted = []
        skipped = 0
        for task in tasks:
            if task.id in self.task_registry:
                skipped += 1
                continue
            if task.status == TaskStatus.PENDING:
                task.transition_to(TaskStatus.SUBMITTED)
            accepted.append(task)
            self.task_registry[task.id] = task
        self.pending_tasks.extend(accepted)
        self.pending_tasks.sort(key=lambda item: getattr(item, "submitted_at", 0))
        if accepted:
            self._bump_state_version("tasks_submitted")
        if accepted or skipped:
            print(
                f"[batch register] Loaded {len(accepted)} tasks; "
                f"skipped {skipped} duplicates, ordered by submission time"
            )
        return len(accepted)

    def submit_task_with_dc(self, task: Task, dc_id: str):
        """Implementation documentation."""
        task.allocated_data_center_id = dc_id
        self.submit_task(task)

    def register_storage(self, storage: EnergyStorage) -> None:
        self.storages[storage.id] = storage
        self._bump_state_version("storage_registered")
        if hasattr(self.pp_adapter, "set_storage_models"):
            self.pp_adapter.set_storage_models(self.storages.values())

    def apply_storage_dispatch(self, requested_power_mw: Dict[str, float], step_hours: float) -> Dict[str, float]:
        """Apply a storage schedule while enforcing the storage's physical SOC limits."""
        actual_power = {}
        by_bus = defaultdict(float)
        for storage_id, requested in requested_power_mw.items():
            storage = self.storages.get(storage_id)
            if storage is None:
                raise ValueError(f"unknown storage: {storage_id}")
            actual = storage.apply_power(requested, step_hours)
            by_bus[int(storage.bus_id)] += actual
            actual_power[storage_id] = actual
        for bus_id, power in by_bus.items():
            self.pp_adapter.update_storage_power(bus_id, power)
        if actual_power:
            self._bump_state_version("storage_dispatch_applied")
        return actual_power

    def _advance_optimized_storage(self) -> None:
        """Advance held storage controls by one real simulation step."""
        storages = getattr(self, "storages", {})
        if not storages:
            return

        requested = {
            storage_id: float(storage.current_power_mw)
            for storage_id, storage in storages.items()
        }

        summary = (
            self.pp_adapter.get_renewable_dispatch_summary()
            if hasattr(self.pp_adapter, "get_renewable_dispatch_summary") else {}
        )
        curtailed = sum(
            max(0.0, float(item.get("curtailed_mw", 0.0)))
            for item in summary.values()
        )

        # Implementation note.
        net_load = (
            float(self.pp_adapter.estimate_net_load_mw(self.current_second))
            if hasattr(self.pp_adapter, "estimate_net_load_mw")
            else 0.0
        )
        # Implementation note.
        surplus_without_storage = max(0.0, -net_load)

        total_power = sum(float(storage.max_power_mw) for storage in storages.values())

        # Implementation note.
        need_charge = (curtailed > 1e-6 or surplus_without_storage > 1e-6) and total_power > 0.0

        if need_charge:
            # Implementation note.
            target_charge = min(total_power, max(curtailed, surplus_without_storage))
            requested_total = -target_charge
            requested = {
                storage_id: requested_total * float(storage.max_power_mw) / total_power
                for storage_id, storage in storages.items()
            }

        requested = {
            storage_id: float(requested.get(storage_id, 0.0))
            for storage_id in storages
        }

        actual = self.apply_storage_dispatch(
            requested,
            step_hours=float(getattr(self, "step_seconds", 1)) / 3600.0,
        )

        if need_charge:
            actual_total = sum(actual.values())
            self.storage_dispatch_result = {
                **getattr(self, "storage_dispatch_result", {}),
                "status": "active" if abs(actual_total) > 1e-9 else "limited",
                "reason": "optimized_renewable_charging",
                "requested_power_mw": sum(requested.values()),
                "actual_power_mw": actual_total,
            }
    # def _advance_optimized_storage(self) -> None:
    #     """Advance held storage controls by one real simulation step."""
    #     storages = getattr(self, "storages", {})
    #     if not storages:
    #         return
    #     requested = {
    #         storage_id: float(storage.current_power_mw)
    #         for storage_id, storage in storages.items()
    #     }
    #     # Between rolling solves, replace a held discharge/idle command when
    #     # the last measured renewable dispatch shows real curtailed green
    #     # power.  This is the emergency absorption path; it is based only on
    #     # observed renewable surplus and still advances SOC exactly once.
    #     summary = (
    #         self.pp_adapter.get_renewable_dispatch_summary()
    #         if hasattr(self.pp_adapter, "get_renewable_dispatch_summary") else {}
    #     )
    #     curtailed = sum(
    #         max(0.0, float(item.get("curtailed_mw", 0.0)))
    #         for item in summary.values()
    #     )
    #     total_power = sum(float(storage.max_power_mw) for storage in storages.values())
    #     emergency_charge = curtailed > 1e-6 and total_power > 0.0
    #     if emergency_charge:
    #         requested_total = -min(total_power, curtailed)
    #         requested = {
    #             storage_id: requested_total * float(storage.max_power_mw) / total_power
    #             for storage_id, storage in storages.items()
    #         }
    #     requested = {
    #         storage_id: float(requested.get(storage_id, 0.0))
    #         for storage_id in storages
    #     }
    #     actual = self.apply_storage_dispatch(
    #         requested,
    #         step_hours=float(getattr(self, "step_seconds", 1)) / 3600.0,
    #     )
    #     if emergency_charge:
    #         actual_total = sum(actual.values())
    #         self.storage_dispatch_result = {
    #             **getattr(self, "storage_dispatch_result", {}),
    #             "status": "active" if abs(actual_total) > 1e-9 else "limited",
    #             "reason": "optimized_renewable_charging",
    #             "requested_power_mw": sum(requested.values()),
    #             "actual_power_mw": actual_total,
    #         }

    def _dispatch_storage(self) -> None:
        """Dispatch storage from live net demand, with grid security taking priority."""
        storages = getattr(self, "storages", {})
        if not storages:
            self.storage_dispatch_result = {
                "status": "idle", "reason": "no_storage",
                "requested_power_mw": 0.0, "actual_power_mw": 0.0,
                "price": None,
            }
            return

        adapter = self.pp_adapter
        voltage = adapter.get_voltage_health() if hasattr(adapter, "get_voltage_health") else {}
        line = adapter.get_line_health() if hasattr(adapter, "get_line_health") else {}
        min_voltage = voltage.get("min_voltage_pu")
        max_voltage = voltage.get("max_voltage_pu")
        needs_support = (
            line.get("status") == "overloaded"
            or (min_voltage is not None and float(min_voltage) < 0.95)
        )
        needs_absorption_voltage = (
            not needs_support
            and max_voltage is not None
            and float(max_voltage) > 1.05
        )

        current_price = None
        pricing = getattr(self, "fixed_pricing", None)
        if pricing is not None:
            current = getattr(self, "simulation_start_time", datetime(2026, 1, 1)) + timedelta(
                seconds=getattr(self, "current_second", 0)
            )
            hour = current.hour + current.minute / 60 + current.second / 3600
            current_price = float(pricing.price_at_hour(hour))
        else:
            prices = [
                float(adapter.get_bus_marginal_price(storage.bus_id))
                for storage in storages.values()
                if hasattr(adapter, "get_bus_marginal_price")
            ]
            current_price = sum(prices) / len(prices) if prices else 0.0

        net_load = (
            float(adapter.estimate_net_load_mw(self.current_second))
            if hasattr(adapter, "estimate_net_load_mw")
            else float(adapter.get_total_load())
        )
        reference_value = getattr(self, "_storage_load_reference_mw", None)
        reference_initialized = reference_value is not None
        reference = reference_value
        if reference is None:
            reference = net_load
        deadband = max(0.0, float(getattr(self, "storage_control_config", {}).get("deadband_mw", 12.0)))
        alpha = min(1.0, max(0.0001, float(getattr(self, "storage_control_config", {}).get("reference_alpha", 0.02))))
        deviation = net_load - float(reference)
        renewable_summary = (
            adapter.get_renewable_dispatch_summary()
            if hasattr(adapter, "get_renewable_dispatch_summary") else {}
        )
        curtailed = sum(float(item.get("curtailed_mw", 0.0)) for item in renewable_summary.values())
        renewable_available = sum(
            max(0.0, float(item.get("available_mw", 0.0)))
            for item in renewable_summary.values()
        )
        renewable_reference = getattr(self, "_storage_renewable_reference_mw", None)
        if renewable_reference is None:
            renewable_reference = renewable_available
        renewable_deficit = max(0.0, float(renewable_reference) - renewable_available)
        total_soc = sum(float(storage.soc_mwh) for storage in storages.values())
        total_capacity = sum(float(storage.max_soc_mwh or storage.capacity_mwh) for storage in storages.values())
        soc_ratio = total_soc / total_capacity if total_capacity > 0 else 0.0
        total_power = sum(float(storage.max_power_mw) for storage in storages.values())
        # Voltage absorption is only a valid storage charging signal when the
        # system also has observable renewable surplus.  A high voltage by
        # itself can be caused by network conditions and must not make the
        # battery charge during an ordinary positive net-load interval.
        needs_absorption = needs_absorption_voltage and (
            curtailed > deadband or net_load < -deadband
        )
        load_deficit = (
            deviation > deadband
            if reference_initialized
            else net_load > deadband
        )

        requested = {storage_id: 0.0 for storage_id in storages}
        reason = "realtime_deadband_idle"
        objective_value = None
        if needs_support:
            requested = {storage_id: float(storage.max_power_mw) for storage_id, storage in storages.items()}
            reason = "grid_support_discharging"
        elif needs_absorption:
            requested = {storage_id: -float(storage.max_power_mw) for storage_id, storage in storages.items()}
            reason = "overvoltage_charging"
        elif (
            renewable_deficit > deadband
            and soc_ratio >= 0.80
            and curtailed <= 1e-6
            # A drop in renewable output alone is not a discharge request.
            # Require an actual positive load deviation so the battery does
            # not discharge in a midday valley merely because wind/PV fell
            # relative to an earlier reference sample.
            and load_deficit
        ):
            requested_total = min(total_power, renewable_deficit)
            requested = {
                storage_id: requested_total * float(storage.max_power_mw) / total_power
                for storage_id, storage in storages.items()
            } if total_power > 0 else requested
            reason = "renewable_deficit_discharging"
        else:
            mode = self._normalise_optimization_mode(getattr(self, "optimization_config", {}))
            if mode == "none":
                requested_total = 0.0
                if curtailed > 1e-6:
                    requested_total = -min(total_power, max(curtailed, max(0.0, -deviation - deadband)))
                    reason = "renewable_absorption_charging"
                elif deviation > deadband:
                    requested_total = min(total_power, deviation - deadband)
                    reason = "realtime_peak_discharging"
                elif deviation < -deadband:
                    requested_total = -min(total_power, -deviation - deadband)
                    reason = "realtime_valley_charging"
                if total_power > 0:
                    requested = {
                        storage_id: requested_total * float(storage.max_power_mw) / total_power
                        for storage_id, storage in storages.items()
                    }
            elif abs(deviation) > deadband or curtailed > 1e-6:
                interval = max(1.0, float(getattr(self, "storage_control_config", {}).get("optimization_interval_seconds", 5)))
                last_solve = getattr(self, "_last_storage_optimization_second", None)
                cached = getattr(self, "_storage_requested_power_mw", {})
                solve_due = last_solve is None or float(self.current_second) - float(last_solve) >= interval
                if not solve_due and cached:
                    requested = {
                        storage_id: float(cached.get(storage_id, 0.0))
                        for storage_id in storages
                    }
                    objective_value = getattr(self, "storage_dispatch_result", {}).get("objective_value")
                else:
                    weights = getattr(self, "optimization_config", {}).get("weights", {})
                    adjusted_load = net_load
                    objective_total = 0.0
                    for storage_id, storage in storages.items():
                        optimized = CoptOptimizer().optimize_storage_realtime(
                            storage,
                            net_load_mw=adjusted_load,
                            reference_load_mw=float(reference),
                            electricity_price=float(current_price or 0.0),
                            step_hours=float(getattr(self, "step_seconds", 1)) / 3600.0,
                            weights=weights,
                            renewable_surplus_mw=curtailed,
                        )
                        power = float(optimized.get("power_mw", 0.0)) if optimized.get("status") == "optimal" else 0.0
                        requested[storage_id] = power
                        adjusted_load -= power
                        objective_total += float(optimized.get("objective_value") or 0.0)
                    objective_value = objective_total
                    self._storage_requested_power_mw = dict(requested)
                    self._last_storage_optimization_second = float(self.current_second)
                if sum(requested.values()) > 1e-9:
                    reason = "optimized_peak_discharging"
                elif sum(requested.values()) < -1e-9:
                    reason = "optimized_renewable_charging" if curtailed > 1e-6 else "optimized_valley_charging"

        actual = self.apply_storage_dispatch(
            requested,
            step_hours=float(getattr(self, "step_seconds", 1)) / 3600.0,
        )
        requested_total = sum(requested.values())
        actual_total = sum(actual.values())
        limited = abs(requested_total) > 1e-9 and abs(actual_total) < 1e-9
        self.storage_dispatch_result = {
            "status": "limited" if limited else "active" if abs(actual_total) > 1e-9 else "idle",
            "reason": f"{reason}_soc_limited" if limited else reason,
            "requested_power_mw": requested_total,
            "actual_power_mw": actual_total,
            "price": current_price,
            "net_load_mw": net_load,
            "reference_load_mw": float(reference),
            "objective_value": objective_value,
        }
        self._storage_load_reference_mw = (1.0 - alpha) * float(reference) + alpha * net_load
        renewable_alpha = min(1.0, max(0.001, alpha * 20.0))
        self._storage_renewable_reference_mw = (
            (1.0 - renewable_alpha) * float(renewable_reference)
            + renewable_alpha * renewable_available
        )

    def _enforce_surplus_storage_policy(self) -> None:
        storages = getattr(self, "storages", {})
        if not storages:
            return

        net_load = (
            float(self.pp_adapter.estimate_net_load_mw(self.current_second))
            if hasattr(self.pp_adapter, "estimate_net_load_mw")
            else 0.0
        )

        curtailed = 0.0
        if hasattr(self.pp_adapter, "get_renewable_dispatch_summary"):
            summary = self.pp_adapter.get_renewable_dispatch_summary()
            curtailed = sum(max(0.0, float(item.get("curtailed_mw", 0.0))) for item in summary.values())

        surplus = max(0.0, -net_load)
        if (curtailed > 1e-6 or surplus > 1e-6):
            current_storage_power = sum(float(s.current_power_mw) for s in storages.values())

            if current_storage_power > 1e-6:
                requested = {sid: 0.0 for sid in storages}
                self.apply_storage_dispatch(
                    requested,
                    step_hours=float(getattr(self, "step_seconds", 1)) / 3600.0,
                )

                self.storage_dispatch_result = {
                    **getattr(self, "storage_dispatch_result", {}),
                    "status": "idle",
                    "reason": "forced_surplus_zero_discharge",
                    "requested_power_mw": 0.0,
                    "actual_power_mw": 0.0,
                }

    def _storage_runtime_summary(self) -> Dict:
        storages = list(getattr(self, "storages", {}).values())
        capacity = sum(float(storage.capacity_mwh) for storage in storages)
        soc = sum(float(storage.soc_mwh) for storage in storages)
        dispatch = getattr(self, "storage_dispatch_result", {})
        return {
            "storage_power_mw": sum(float(storage.current_power_mw) for storage in storages),
            "storage_soc_mwh": soc,
            "storage_capacity_mwh": capacity,
            "storage_soc_pct": soc / capacity * 100 if capacity > 0 else None,
            "storage_dispatch_status": dispatch.get("status", "idle"),
            "storage_dispatch_reason": dispatch.get("reason", "not_started"),
            "storage_price": dispatch.get("price"),
        }

    def get_storage_summary(self) -> Dict[str, Dict]:
        return {
            storage_id: {
                "bus_id": storage.bus_id,
                "soc_mwh": storage.soc_mwh,
                "current_power_mw": storage.current_power_mw,
                "throughput_mwh": storage.throughput_mwh,
            }
            for storage_id, storage in self.storages.items()
        }

    def get_optimization_result(self) -> Dict:
        return getattr(self, "latest_optimization_result", {})

    def _with_electricity_price_forecast(self, problem):
        """Attach the active tariff to every rolling-horizon power model."""
        interval_seconds = getattr(
            self, "renewable_optimization_interval_seconds", None
        )
        if interval_seconds is not None:
            problem = replace(
                problem,
                control_interval_hours=max(0.0, float(interval_seconds)) / 3600.0,
            )
        horizon = len(problem.load_mw)
        step_hours = float(problem.step_hours)
        fixed_pricing = getattr(self, "fixed_pricing", None)
        if fixed_pricing is not None:
            start = self.current_datetime()
            prices = []
            for period in range(horizon):
                point = start + timedelta(hours=period * step_hours)
                hour = point.hour + point.minute / 60.0 + point.second / 3600.0
                prices.append(float(fixed_pricing.price_at_hour(hour)))
            return replace(problem, electricity_prices=prices)

        adapter = getattr(self, "pp_adapter", None)
        if adapter is None or not hasattr(adapter, "get_bus_marginal_price"):
            return problem
        strategy = getattr(adapter, "electricity_strategy", None)
        forecast_prices = getattr(strategy, "forecast_prices", None)
        if forecast_prices is not None:
            # Implementation note.
            # Implementation note.
            extra_loads = getattr(adapter, "extra_loads", None)
            current_task_load = sum(
                max(0.0, float(value)) for value in extra_loads.values()
            ) if isinstance(extra_loads, dict) else None
            if current_task_load is not None:
                net_load = [current_task_load] * len(problem.load_mw)
            else:
                net_load = []
                for period, load in enumerate(problem.load_mw):
                    renewable_available = sum(
                        float(item.available_mw[period % len(item.available_mw)])
                        for item in problem.renewables
                        if item.available_mw
                    )
                    net_load.append(max(0.0, float(load) - renewable_available))
            current = getattr(self, "simulation_start_time", datetime(2026, 1, 1)) + timedelta(
                seconds=getattr(self, "current_second", 0)
            )
            start_hour = current.hour + current.minute / 60.0 + current.second / 3600.0
            return replace(
                problem,
                electricity_prices=list(forecast_prices(net_load, start_hour=start_hour, step_hours=step_hours)),
            )
        buses = {generator.bus_id for generator in problem.generators}
        if not buses:
            buses = {
                center.grid_bus_id for center in getattr(self, "data_centers", [])
            }
        try:
            prices = [float(adapter.get_bus_marginal_price(bus)) for bus in buses]
        except Exception:
            return problem
        if not prices:
            return problem
        return replace(
            problem,
            electricity_prices=[sum(prices) / len(prices)] * horizon,
        )

    def _projected_compute_loads(
        self, assignments: Dict[str, str], tasks_by_id: Dict[str, Task]
    ) -> Dict[int, float]:
        """Build per-bus loads for the candidate task placement."""
        projected = defaultdict(float)
        for center in getattr(self, "data_centers", []):
            sim = getattr(self, "dc_sims", {}).get(center.id)
            current = (
                float(getattr(sim, "current_load_mw", 0.0))
                if sim is not None else 0.0
            )
            projected[int(center.grid_bus_id)] += current
        centers = {center.id: center for center in getattr(self, "data_centers", [])}
        for task_id, center_id in assignments.items():
            task = tasks_by_id.get(task_id)
            center = centers.get(center_id)
            if task is not None and center is not None:
                projected[int(center.grid_bus_id)] += float(
                    center.power_for_task_mw(task)
                )
        return dict(projected)

    def _validate_candidate_ac(
        self,
        power_dispatch,
        assignments: Dict[str, str],
        tasks_by_id: Dict[str, Task],
    ) -> Dict:
        """Validate dispatch against the candidate per-bus compute loads."""
        adapter = self.pp_adapter
        previous_loads = dict(getattr(adapter, "extra_loads", {}))
        replace_loads = getattr(adapter, "replace_compute_loads", None)
        if replace_loads is not None:
            replace_loads(self._projected_compute_loads(assignments, tasks_by_id))
        try:
            adapter.apply_power_dispatch(power_dispatch, period=0)
            return adapter.validate_dispatch_ac()
        finally:
            if replace_loads is not None:
                replace_loads(previous_loads)

    def _remember_power_controls(self, power_dispatch) -> None:
        """Keep approved renewable/storage controls active between solves."""
        if power_dispatch is None or not power_dispatch.strictly_feasible:
            return
        storage_schedule = getattr(power_dispatch, "storage_power_mw", {}) or {}
        storage_power = sum(
            float(values[0])
            for values in storage_schedule.values()
            if values
        )
        self.storage_dispatch_result = {
            "status": "active" if abs(storage_power) > 1e-9 else "idle",
            "reason": (
                "rolling_power_optimization"
                if storage_schedule else "no_storage"
            ),
            "requested_power_mw": storage_power,
            "actual_power_mw": storage_power,
            "objective_value": power_dispatch.objective_value,
            "price": None,
        }
        summary = (
            self.pp_adapter.get_renewable_dispatch_summary()
            if hasattr(self.pp_adapter, "get_renewable_dispatch_summary") else {}
        )
        if summary:
            self._renewable_dispatch_factors = {
                source: (
                    values["dispatched_mw"] / values["available_mw"]
                    if values["available_mw"] > 1e-9 else 1.0
                )
                for source, values in summary.items()
            }
            if getattr(self, "_last_renewable_energy_second", None) == getattr(
                self, "current_second", 0
            ):
                return
            self._last_renewable_energy_second = getattr(self, "current_second", 0)
            totals = getattr(self, "renewable_energy_mwh", None)
            if totals is None:
                self.renewable_energy_mwh = totals = {
                    source: {"available": 0.0, "consumed": 0.0, "curtailed": 0.0}
                    for source in ("wind", "pv")
                }
            duration_hours = float(getattr(self, "step_seconds", 1)) / 3600.0
            for source, values in summary.items():
                totals.setdefault(
                    source, {"available": 0.0, "consumed": 0.0, "curtailed": 0.0}
                )
                totals[source]["available"] += values["available_mw"] * duration_hours
                totals[source]["consumed"] += values["dispatched_mw"] * duration_hours
                totals[source]["curtailed"] += values["curtailed_mw"] * duration_hours

    def _run_heuristic_cycle(self) -> Dict:
        """Optimize the electrical side without feeding signals into task placement."""
        cache_key = self._optimization_cache_key("heuristic", [])
        cached = self._optimization_cache_get(cache_key)
        if cached is not None:
            self.latest_optimization_result = copy.deepcopy(cached)
            return cached
        config = getattr(self, "optimization_config", {})
        objective = config.get("objective", "min_cost")
        if objective == "custom":
            objective = "weighted"
        pending_assignments, pending_tasks = getattr(
            self, "_heuristic_pending_projection", ({}, {})
        )
        if hasattr(self, "_heuristic_pending_projection"):
            delattr(self, "_heuristic_pending_projection")
        adapter = self.pp_adapter
        previous_loads = dict(getattr(adapter, "extra_loads", {}))
        replace_loads = getattr(adapter, "replace_compute_loads", None)
        try:
            if pending_assignments and replace_loads is not None:
                replace_loads(
                    self._projected_compute_loads(
                        pending_assignments, pending_tasks
                    )
                )
            forecast_horizon_hours = float(config.get("forecast_horizon_hours", config.get("horizon_hours", 24)))
            forecast_step_hours = max(1.0 / 60.0, float(config.get("forecast_step_minutes", 5.0)) / 60.0)
            problem = self.pp_adapter.build_power_dispatch_problem(
                int(getattr(self, "current_second", 0)),
                horizon_hours=forecast_horizon_hours,
                objective=objective,
                objective_weights=config.get("weights", {}),
                step_hours=forecast_step_hours,
                actual_renewable_first_period=dict(
                    getattr(self.pp_adapter, "renewable_available_mw", {})
                ),
            )
            self.latest_power_dispatch_problem = problem
            problem = self._with_electricity_price_forecast(problem)
            result = PowerDispatchOptimizer().solve(problem)
        except Exception as exc:
            payload = {
                "mode": "heuristic", "status": "optimizer_error",
                "objective_value": None, "strictly_feasible": False,
                "error": str(exc),
            }
            self.latest_optimization_result = payload
            return payload
        finally:
            if pending_assignments and replace_loads is not None:
                replace_loads(previous_loads)

        self.latest_power_dispatch_result = result
        if not result.strictly_feasible:
            payload = {
                "mode": "heuristic", "status": result.status,
                "objective_value": result.objective_value,
                "strictly_feasible": False,
                "forecast_fill": result.forecast_fill,
                "ac_validation_status": "not_run",
            }
            self.latest_optimization_result = payload
            return payload

        if pending_assignments:
            ac_state = self._validate_candidate_ac(
                result, pending_assignments, pending_tasks
            )
            applied = {"dispatch_id": ac_state.get("dispatch_id")}
        else:
            applied = self.pp_adapter.apply_power_dispatch(result, period=0)
            ac_state = self.pp_adapter.validate_dispatch_ac()
        if not ac_state.get("feasible"):
            # The rolling aggregate model has no line-flow constraints.  Keep
            # the largest AC-feasible renewable injection instead of rejecting
            # the entire heuristic dispatch when a local line is congested.
            fallback_dispatch = getattr(
                self.pp_adapter, "apply_heuristic_renewable_curtailment", None
            )
            fallback = fallback_dispatch() if fallback_dispatch is not None else None
            ac_state = self.pp_adapter.validate_dispatch_ac() if fallback is not None else ac_state
            if not ac_state.get("feasible"):
                self.pp_adapter.restore_last_feasible_dispatch()
                result = replace(result, status="ac_validation_failed", strictly_feasible=False)
                self.latest_power_dispatch_result = result
                payload = {
                    "mode": "heuristic", "status": "ac_validation_failed",
                    "objective_value": result.objective_value,
                    "strictly_feasible": False, "ac_state": ac_state,
                    "forecast_fill": result.forecast_fill,
                    "ac_validation_status": "failed",
                }
                self.latest_optimization_result = payload
                return payload
            summary = self.pp_adapter.get_renewable_dispatch_summary()
            dispatched = {
                source: [values["dispatched_mw"], *result.renewable_dispatched_mw.get(source, [])[1:]]
                for source, values in summary.items()
            }
            curtailed = {
                source: [values["curtailed_mw"], *result.renewable_curtailed_mw.get(source, [])[1:]]
                for source, values in summary.items()
            }
            external = list(result.external_grid_mw)
            if external:
                external[0] = max(0.0, float(self.pp_adapter.get_grid_exchange_mw() or 0.0))
            result = replace(
                result,
                renewable_dispatched_mw=dispatched,
                renewable_curtailed_mw=curtailed,
                external_grid_mw=external,
            )
            applied = {"dispatch_id": ac_state.get("dispatch_id"), "renewable_fallback": fallback}

        self._remember_power_controls(result)
        storage_power = sum(values[0] for values in result.storage_power_mw.values())
        self.storage_dispatch_result = {
            "status": "active" if abs(storage_power) > 1e-9 else "idle",
            "reason": "rolling_power_optimization",
            "requested_power_mw": storage_power,
            "actual_power_mw": storage_power,
            "objective_value": result.objective_value,
            "price": None,
        }
        payload = {
            "mode": "heuristic", "status": "optimal",
            "objective_value": result.objective_value,
            "strictly_feasible": True,
            "dispatch_id": applied.get("dispatch_id"),
            "generator_mw": {key: values[0] for key, values in result.generator_mw.items()},
            "renewable_dispatched_mw": {
                key: values[0] for key, values in result.renewable_dispatched_mw.items()
            },
            "renewable_curtailed_mw": {
                key: values[0] for key, values in result.renewable_curtailed_mw.items()
            },
            "storage_power_mw": {
                key: values[0] for key, values in result.storage_power_mw.items()
            },
            "storage_soc_mwh": {
                key: values[1] if len(values) > 1 else values[0]
                for key, values in result.storage_soc_mwh.items()
            },
            "objective_components": result.objective_components,
            "external_grid_mw": result.external_grid_mw[0] if result.external_grid_mw else 0.0,
            "forecast_fill": result.forecast_fill,
            "ac_validation_status": "validated",
        }
        self.latest_optimization_result = payload
        if payload.get("strictly_feasible"):
            self._optimization_cache_put(cache_key, payload)
        return payload

    def _run_local_coordination(self, tasks: List[Task]) -> Dict:
        """Alternate task placement and power dispatch with strict AC checks."""
        engine = self
        config = getattr(self, "optimization_config", {})
        objective = config.get("objective", "min_cost")
        if objective == "custom":
            objective = "weighted"
        provisional = {
            task.id: task.allocated_data_center_id
            for task in tasks if task.allocated_data_center_id
        }

        class TaskSubproblem:
            def __init__(self):
                self.iteration = 0

            def solve(self, active_tasks, data_centers, context):
                self.iteration += 1
                # The dispatch loop has already performed a capacity-aware
                # reservation pass for this batch.  Keep that feasible
                # placement fixed throughout local power coordination instead
                # of rebuilding a second all-task assignment MIP after every
                # feedback round.
                if len(provisional) == len(active_tasks):
                    return {
                        "status": "optimal", "objective_value": 0.0,
                        "assignments": dict(provisional),
                    }
                feedback = context.get("power_feedback", {})
                nodal_prices = feedback.get("nodal_prices", {})
                for data_center in data_centers:
                    if data_center.grid_bus_id in nodal_prices:
                        data_center.update_price(float(nodal_prices[data_center.grid_bus_id]))
                task_objective = objective if objective in {
                    "min_cost", "min_carbon", "min_delay", "weighted"
                } else "min_cost"
                result = CoptOptimizer().assign_tasks(
                    active_tasks,
                    data_centers,
                    objective=task_objective,
                    carbon_intensity=feedback.get("carbon_intensity") or {
                        dc.grid_bus_id: engine.pp_adapter.get_bus_carbon_intensity(dc.grid_bus_id)
                        for dc in data_centers
                    },
                    latency_ms=getattr(engine, "scheduling_config", {}).get("latency_ms", {}),
                    weights=config.get("weights"),
                    energy_based=True,
                    objective_function=load_objective(config.get("objective_function")),
                    # The preceding capacity-aware pass reserves compute,
                    # bandwidth and devices for these provisional centers.
                    # Keep those assignments fixed during the local power
                    # coordination solve; allowing a second unrestricted
                    # placement can invalidate the reservation and create a
                    # repeatedly infeasible 24-task MIP.
                    respect_allocated_data_center=True,
                    load_balance_weight=float(config.get("load_balance_weight", 1.0)),
                    minimum_active_center_tasks=int(config.get("minimum_active_center_tasks", 1)),
                )
                return result

        tasks_by_id = {task.id: task for task in tasks}
        centers_by_id = {center.id: center for center in self.data_centers}
        base_power_problem = self._with_electricity_price_forecast(
            self.pp_adapter.build_power_dispatch_problem(
                int(getattr(self, "current_second", 0)),
                horizon_hours=int(config.get("horizon_hours", 24)),
                objective=objective,
                objective_weights=config.get("weights", {}),
                actual_renewable_first_period=dict(
                    getattr(self.pp_adapter, "renewable_available_mw", {})
                ),
            )
        )

        def power_problem_factory(assignments, iteration, context):
            base = base_power_problem
            projected = list(base.load_mw)
            for task_id, center_id in assignments.items():
                task = tasks_by_id[task_id]
                center = centers_by_id[center_id]
                power = float(center.power_for_task_mw(task))
                active_periods = max(1, min(len(projected), int(math.ceil(task.duration_hours))))
                for period in range(active_periods):
                    projected[period] += power
            return replace(base, load_mw=projected)

        def validate_power_dispatch(power_dispatch, assignments, iteration):
            state = self._validate_candidate_ac(
                power_dispatch, assignments, tasks_by_id
            )
            if not state.get("feasible"):
                self.pp_adapter.restore_last_feasible_dispatch()
            return state

        def build_power_feedback(power_dispatch, assignments, iteration):
            return {
                "objective_value": float(power_dispatch.objective_value or 0.0),
                "nodal_prices": {
                    dc.grid_bus_id: self.pp_adapter.get_bus_marginal_price(dc.grid_bus_id)
                    for dc in self.data_centers
                },
                "carbon_intensity": {
                    dc.grid_bus_id: self.pp_adapter.get_bus_carbon_intensity(dc.grid_bus_id)
                    for dc in self.data_centers
                },
                "grid_capacity": self.pp_adapter.get_operating_constraint_snapshot(),
            }

        result = LocalCoordinationOptimizer(TaskSubproblem()).solve(
            tasks,
            self.data_centers,
            {
                "validate_power_dispatch": validate_power_dispatch,
                "build_power_feedback": build_power_feedback,
            },
            power_problem_factory,
            max_iterations=int(config.get("max_iterations", 5)),
            tolerance=float(config.get("tolerance", 1e-3)),
        )
        power = result.power_dispatch
        if power is not None:
            self.latest_power_dispatch_result = power
            self._remember_power_controls(power)
        payload = {
            "mode": "local",
            "status": result.status,
            "coordination_status": result.status,
            "objective_value": result.objective_history[-1] if result.objective_history else None,
            "assignments": result.assignments,
            "coordination_iterations": result.iterations,
            "converged": result.converged,
            "objective_history": result.objective_history,
            "strictly_feasible": bool(power and power.strictly_feasible),
            "generator_mw": {
                key: values[0] for key, values in (power.generator_mw if power else {}).items()
            },
            "renewable_dispatched_mw": {
                key: values[0] for key, values in (power.renewable_dispatched_mw if power else {}).items()
            },
            "renewable_curtailed_mw": {
                key: values[0] for key, values in (power.renewable_curtailed_mw if power else {}).items()
            },
            "storage_power_mw": {
                key: values[0] for key, values in (power.storage_power_mw if power else {}).items()
            },
            "storage_soc_mwh": {
                key: values[1] if len(values) > 1 else values[0]
                for key, values in (power.storage_soc_mwh if power else {}).items()
            },
            "objective_components": power.objective_components if power else {},
            "external_grid_mw": (
                power.external_grid_mw[0]
                if power and power.external_grid_mw else 0.0
            ),
            "forecast_fill": power.forecast_fill if power else None,
            "ac_validation_status": "validated" if power and power.strictly_feasible else "failed",
        }
        self.latest_optimization_result = payload
        return payload

    def _run_joint_coordination(self, tasks: List[Task]) -> Dict:
        """Solve task placement and electrical dispatch in one COPT model."""
        config = getattr(self, "optimization_config", {})
        objective = config.get("objective", "min_cost")
        if objective == "custom":
            objective = "weighted"
        problem = self.pp_adapter.build_power_dispatch_problem(
            int(getattr(self, "current_second", 0)),
            horizon_hours=float(config.get("forecast_horizon_hours", 2.0)),
            objective=objective,
            objective_weights=config.get("weights", {}),
            step_hours=float(config.get("forecast_step_minutes", 5.0)) / 60.0,
            actual_renewable_first_period=dict(
                getattr(self.pp_adapter, "renewable_available_mw", {})
            ),
        )
        problem = self._with_electricity_price_forecast(problem)
        validated = {"candidate": None}
        tasks_by_id = {task.id: task for task in tasks}

        def validate_candidate(candidate, attempt):
            if candidate.power_dispatch is None:
                return False
            active_assignments = {
                task_id: center_id
                for task_id, center_id in candidate.assignments.items()
                if int(candidate.start_periods.get(task_id, 0) or 0) == 0
            }
            state = self._validate_candidate_ac(
                candidate.power_dispatch, active_assignments, tasks_by_id
            )
            if state.get("feasible"):
                validated["candidate"] = candidate
                return True
            self.pp_adapter.restore_last_feasible_dispatch()
            return False

        def apply_candidate(candidate):
            if validated["candidate"] is not candidate and candidate.power_dispatch is not None:
                self.pp_adapter.apply_power_dispatch(candidate.power_dispatch, period=0)

        result = GlobalCoordinationOptimizer(
            ac_validator=validate_candidate,
            result_applier=apply_candidate,
            feasible_restorer=self.pp_adapter.restore_last_feasible_dispatch,
        ).solve(
            tasks,
            self.data_centers,
            problem,
            objective=objective,
            weights=config.get("weights", {}),
            latency_ms=getattr(self, "scheduling_config", {}).get("latency_ms", {}),
            carbon_intensity={
                dc.grid_bus_id: self.pp_adapter.get_bus_carbon_intensity(dc.grid_bus_id)
                for dc in self.data_centers
            },
            load_balance_weight=float(config.get("load_balance_weight", 1.0)),
            minimum_active_center_tasks=int(config.get("minimum_active_center_tasks", 1)),
            time_limit_seconds=float(config.get("global_time_limit_seconds", 2.0)),
            relative_mip_gap=float(config.get("global_relative_mip_gap", 0.03)),
        )
        if result.power_dispatch is not None:
            self.latest_power_dispatch_result = result.power_dispatch
            self._remember_power_controls(result.power_dispatch)
        payload = {
            "mode": "global",
            "status": result.status,
            "objective_value": result.objective_value,
            "assignments": result.assignments,
            "start_periods": result.start_periods,
            "queued_tasks": result.queued_tasks,
            "coordination_iterations": 1,
            "converged": result.status == "optimal",
            "strictly_feasible": result.strictly_feasible,
            "generator_mw": {key: values[0] for key, values in result.generator_mw.items()},
            "renewable_dispatched_mw": {
                key: values[0] for key, values in result.renewable_dispatched_mw.items()
            },
            "renewable_curtailed_mw": {
                key: values[0] for key, values in result.renewable_curtailed_mw.items()
            },
            "storage_power_mw": {
                key: values[0] for key, values in result.storage_power_mw.items()
            },
            "storage_soc_mwh": {
                key: values[1] if len(values) > 1 else values[0]
                for key, values in result.storage_soc_mwh.items()
            },
            "objective_components": result.objective_components,
            "external_grid_mw": (
                result.external_grid_mw[0] if result.external_grid_mw else 0.0
            ),
            "forecast_fill": result.forecast_fill,
            "ac_validation_status": "validated" if result.strictly_feasible else "failed",
        }
        self.latest_optimization_result = payload
        return payload

    def optimize_ready_tasks(self, tasks: List[Task], objective: str = "min_cost") -> Dict:
        """Run the real COPT assignment model and retain its API-safe result."""
        optimizer = CoptOptimizer()
        optimization_config = getattr(self, "optimization_config", {})
        if optimization_config.get("enabled", True) is False:
            payload = {"status": "disabled", "objective_value": None, "assignments": {}}
            self.latest_optimization_result = payload
            return payload
        mode = self._normalise_optimization_mode(optimization_config)
        cache_key = self._optimization_cache_key(mode, tasks)
        cached = self._optimization_cache_get(cache_key)
        if cached is not None:
            self.latest_optimization_result = copy.deepcopy(cached)
            return cached
        if mode == "hierarchical" and hasattr(
            getattr(self, "pp_adapter", None), "build_power_dispatch_problem"
        ):
            result = self._run_local_coordination(tasks)
            if result.get("strictly_feasible"):
                self._optimization_cache_put(cache_key, result)
            return result
        if mode == "joint" and hasattr(
            getattr(self, "pp_adapter", None), "build_power_dispatch_problem"
        ):
            result = self._run_joint_coordination(tasks)
            if result.get("strictly_feasible"):
                self._optimization_cache_put(cache_key, result)
            return result
        objective = optimization_config.get("objective", objective)
        objective_function = load_objective(optimization_config.get("objective_function"))
        adapter = getattr(self, "pp_adapter", None)
        carbon_intensity = {
            dc.grid_bus_id: adapter.get_bus_carbon_intensity(dc.grid_bus_id)
            for dc in self.data_centers
        } if adapter is not None else None
        if mode == "joint":
            global_result = optimizer.global_optimize(
                tasks, self.data_centers, objective=objective, carbon_intensity=carbon_intensity,
                weights=optimization_config.get("weights"),
                objective_function=objective_function,
            )
            payload = {
                "mode": "joint",
                "status": global_result.status,
                "objective_value": global_result.objective_value,
                "assignments": global_result.assignments,
                "economic_cost": global_result.economic_cost,
                "carbon_emission": global_result.carbon_emission,
            }
            self.latest_optimization_result = payload
            return payload
        result = optimizer.assign_tasks(
            tasks, self.data_centers, objective=objective,
            carbon_intensity=carbon_intensity,
            weights=optimization_config.get("weights"),
            objective_function=objective_function,
        )
        payload = {
            "mode": mode,
            "status": result.status,
            "objective_value": result.objective_value,
            "assignments": result.assignments,
        }
        self.latest_optimization_result = payload
        if result.status in {"optimal", "optimal_solution"}:
            self._optimization_cache_put(cache_key, payload)
        return payload

    @staticmethod
    def _normalise_optimization_mode(config: Dict) -> str:
        """Resolve the single user-facing mode switch, retaining old aliases."""
        if config.get("enabled", True) is False:
            return "none"
        mode = config.get("mode", "hierarchical")
        aliases = {"local": "hierarchical", "global": "joint"}
        mode = aliases.get(mode, mode)
        if mode not in {"none", "hierarchical", "joint"}:
            raise ValueError("optimization.mode must be none, hierarchical, or joint")
        return mode

    def _dispatch_renewables(self) -> None:
        """Dispatch renewable power under the selected heuristic/optimization mode."""
        adapter = getattr(self, "pp_adapter", None)
        if adapter is None or not hasattr(adapter, "get_renewable_dispatch_summary"):
            return
        available = {source: item["available_mw"] for source, item in adapter.get_renewable_dispatch_summary().items()}
        if sum(available.values()) <= 1e-9:
            result = {"status": "no_available_renewables", "mode": "heuristic"}
        else:
            mode = self._normalise_optimization_mode(getattr(self, "optimization_config", {}))
            mode_name = "heuristic" if mode == "none" else "local" if mode == "hierarchical" else "joint"
            if mode == "none":
                result = adapter.apply_heuristic_renewable_curtailment()
                result["mode"] = mode_name
            else:
                constraints = adapter.optimize_renewable_grid_constraints()
                constraints = list(constraints or [])
                constraints.append({
                    "coefficients": {source: 1.0 for source in available},
                    "rhs": adapter.get_renewable_absorption_limit_mw(),
                })
                try:
                    optimized = (
                        CoptOptimizer().optimize_renewable_dispatch(available, linear_constraints=constraints)
                    )
                except Exception as exc:
                    optimized = {"status": "optimizer_error", "error": str(exc)}
                if optimized.get("status") == "optimal":
                    adapter.apply_renewable_dispatch(optimized["dispatched_mw"])
                    adapter.run_power_flow()
                    grid_state = adapter.get_operating_constraint_snapshot()
                    if grid_state["feasible"]:
                        result = {**optimized, "status": "optimized", "mode": mode_name, "grid_state": grid_state}
                    else:
                        fallback = adapter.apply_heuristic_renewable_curtailment()
                        result = {**fallback, "mode": mode_name, "status": "ac_validation_fallback", "grid_state": grid_state}
                else:
                    fallback = adapter.apply_heuristic_renewable_curtailment()
                    result = {**fallback, "mode": mode_name, "status": "heuristic_fallback", "optimizer_status": optimized.get("status"), "optimizer_error": optimized.get("error")}

        self.renewable_dispatch_result = result
        if getattr(self, "_last_renewable_energy_second", None) == getattr(
            self, "current_second", 0
        ):
            return
        self._last_renewable_energy_second = getattr(self, "current_second", 0)
        duration_hours = float(getattr(self, "step_seconds", 1)) / 3600.0
        totals = getattr(self, "renewable_energy_mwh", None)
        if totals is None:
            self.renewable_energy_mwh = totals = {
                source: {"available": 0.0, "consumed": 0.0, "curtailed": 0.0}
                for source in ("wind", "pv")
            }
        dispatch_summary = adapter.get_renewable_dispatch_summary()
        self._renewable_dispatch_factors = {
            source: (
                values["dispatched_mw"] / values["available_mw"]
                if values["available_mw"] > 1e-9 else 1.0
            )
            for source, values in dispatch_summary.items()
        }
        for source, values in dispatch_summary.items():
            totals[source]["available"] += values["available_mw"] * duration_hours
            totals[source]["consumed"] += values["dispatched_mw"] * duration_hours
            totals[source]["curtailed"] += values["curtailed_mw"] * duration_hours

    def _reuse_renewable_dispatch(self) -> None:
        """Apply the previous optimized renewable ratios until the next solve."""
        adapter = getattr(self, "pp_adapter", None)
        if adapter is None or not hasattr(adapter, "get_renewable_dispatch_summary"):
            return
        available = {
            source: values["available_mw"]
            for source, values in adapter.get_renewable_dispatch_summary().items()
        }
        factors = getattr(self, "_renewable_dispatch_factors", {"wind": 1.0, "pv": 1.0})
        requested = {
            source: power * min(1.0, max(0.0, float(factors.get(source, 1.0))))
            for source, power in available.items()
        }
        absorption_limit = max(0.0, float(adapter.get_renewable_absorption_limit_mw()))
        requested_total = sum(requested.values())
        clamp_factor = (
            min(1.0, absorption_limit / requested_total)
            if requested_total > 1e-9 else 1.0
        )
        adapter.apply_renewable_dispatch({
            source: power * clamp_factor for source, power in requested.items()
        })

        if getattr(self, "_last_renewable_energy_second", None) == getattr(
            self, "current_second", 0
        ):
            return
        self._last_renewable_energy_second = getattr(self, "current_second", 0)
        duration_hours = float(getattr(self, "step_seconds", 1)) / 3600.0
        totals = getattr(self, "renewable_energy_mwh", None)
        if totals is None:
            self.renewable_energy_mwh = totals = {
                source: {"available": 0.0, "consumed": 0.0, "curtailed": 0.0}
                for source in ("wind", "pv")
            }
        for source, values in adapter.get_renewable_dispatch_summary().items():
            totals[source]["available"] += values["available_mw"] * duration_hours
            totals[source]["consumed"] += values["dispatched_mw"] * duration_hours
            totals[source]["curtailed"] += values["curtailed_mw"] * duration_hours

    def _periodic_due(self, last_run_attribute: str, interval_seconds: float) -> bool:
        """Return true initially and whenever the requested simulation interval elapses."""
        last_run = getattr(self, last_run_attribute, None)
        current = float(getattr(self, "current_second", 0))
        if last_run is None or current - float(last_run) >= float(interval_seconds):
            setattr(self, last_run_attribute, current)
            return True
        return False

    def _update_data_center_prices(self):
        """Feed the latest node price back into every data centre."""
        for data_center in self.data_centers:
            if getattr(self, "fixed_pricing", None) is not None:
                current = self.current_datetime()
                hour = current.hour + current.minute / 60 + current.second / 3600
                node_price = self.fixed_pricing.price_at_hour(hour)
            else:
                strategy = getattr(self.pp_adapter, "electricity_strategy", None)
                if hasattr(strategy, "set_hour"):
                    current = self.current_datetime()
                    strategy.set_hour(current.hour + current.minute / 60 + current.second / 3600)
                node_price = self.pp_adapter.get_bus_marginal_price(data_center.grid_bus_id)
            data_center.update_price(node_price)

    def _allocation_snapshot(self) -> Dict:
        """Return live signals consumed by allocation strategies."""
        adapter = getattr(self, "pp_adapter", None)
        carbon_intensity = {
            dc.grid_bus_id: adapter.get_bus_carbon_intensity(dc.grid_bus_id)
            for dc in self.data_centers
        } if adapter is not None else {}
        return {
            "carbon_intensity": carbon_intensity,
            "latency_ms": getattr(self, "scheduling_config", {}).get("latency_ms", {}),
        }

    def _select_data_center(self, task: Task, data_centers: List[DataCenter]) -> DataCenter:
        """Select a center while retaining two-argument custom schedulers."""
        if isinstance(self.scheduler, Scheduler):
            return self.scheduler.select_data_center(
                task, data_centers, self._allocation_snapshot()
            )
        return self.scheduler.select_data_center(task, data_centers)

    def current_datetime(self) -> datetime:
        """Map elapsed simulation seconds to a user-facing calendar time."""
        start = getattr(self, "simulation_start_time", datetime(2026, 1, 1))
        return start + timedelta(seconds=self.current_second)

    def get_pricing(self) -> Dict:
        if self.fixed_pricing is not None:
            return self.fixed_pricing.as_dict()
        return {"mode": self.pricing_config.get("mode", "marginal")}

    def set_pricing(self, config: Dict) -> Dict:
        mode = config.get("mode", "fixed_tou")
        if mode == "fixed_tou":
            pricing_file = resolve_path(config.get("file"))
            pricing = (
                FixedTimeOfUsePricing.from_csv(pricing_file)
                if pricing_file
                else FixedTimeOfUsePricing(config.get("periods", []))
            )
            self.base_tou_pricing = pricing
            self.fixed_pricing = pricing
            self.pricing_config = dict(pricing.as_dict())
            if config.get("file"):
                self.pricing_config["file"] = config["file"]
        elif mode == "marginal":
            self.fixed_pricing = None
            self.pricing_config = {"mode": "marginal"}
            self.pp_adapter.electricity_strategy = MarginalElectricityPricing(
                base_price=0.5, sensitivity=0.10, scale=1000.0,
                base_pricing=getattr(self, "base_tou_pricing", None),
            )
        elif mode == "custom":
            parameters = dict(config.get("parameters", {}))
            self.fixed_pricing = None
            self.pricing_config = {"mode": "custom", "parameters": parameters}
            self.pp_adapter.electricity_strategy = GridResponsivePricing(**{
                key: value for key, value in parameters.items() if key != "name"
            })
        else:
            raise ValueError("pricing mode must be fixed_tou, marginal, or custom")
        return self.get_pricing()

    def step(self) -> bool:
        """Advance one deterministic simulation step and return whether metrics were recorded."""
        self.current_second += self.step_seconds
        explicit_mode = hasattr(self, "optimization_config")
        mode = self._normalise_optimization_mode(getattr(self, "optimization_config", {}))
        # Object.__new__ legacy fixtures omit optimization_config; real engines
        # use one complete mode-specific electrical cycle.
        if not explicit_mode:
            self._dispatch_storage()
        else:
            self._advance_optimized_storage()
        self.pp_adapter.update_time_series(self.current_second)
        # Implementation note.
        # Implementation note.
        # Implementation note.
        power_flow_interval = max(
            float(getattr(self, "power_flow_interval_seconds", 300)),
            float(getattr(self, "step_seconds", 1)),
        )
        if self._periodic_due("_last_power_flow_second", power_flow_interval):
            self.pp_adapter.run_power_flow()
        self._update_data_center_prices()
        self._release_pending_tasks()
        self._dispatch_ready_tasks()
        if self._periodic_due(
            "_last_renewable_optimization_second",
            getattr(self, "renewable_optimization_interval_seconds", 1),
        ):
            if not explicit_mode:
                self._dispatch_renewables()
            elif mode == "none":
                self._run_heuristic_cycle()
            elif mode == "hierarchical":
                self._run_local_coordination([])
            else:
                self._run_joint_coordination([])
        else:
            # A heuristic projection is only needed in the same event turn in
            # which the task processes have not yet registered their loads.
            # On later turns the live data-center loads already include them.
            if hasattr(self, "_heuristic_pending_projection"):
                delattr(self, "_heuristic_pending_projection")
            self._reuse_renewable_dispatch()

            # Implementation note.
            self._enforce_surplus_storage_policy()

            if self.current_second % self.stats_interval_seconds == 0:
                self.stats_count += 1
                self._record_stats()
                return True
            return False
        if self.current_second % self.stats_interval_seconds == 0:
            self.stats_count += 1
            self._record_stats()
            return True
        return False

    # Implementation note.

    def _run_simulation(self):
        """Implementation documentation."""

        def second_proc():
            while self.running:
                # Implementation note.
                # Implementation note.
                yield self.env.timeout(max(0.001, float(self.step_seconds)))
                self.step()
                if self.time_scale > 0:
                    time.sleep(self.time_scale)

        self.env.process(second_proc())
        self.env.run(until=None)

    def start(self) -> None:
        """Start the simulation worker once configuration and tasks are ready."""
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run_simulation, daemon=True)
        self.thread.start()
        print("Simulation engine started")

    # Implementation note.

    def _release_pending_tasks(self):
        """Implementation documentation."""
        if not self.pending_tasks:
            return

        released = []
        # submit_task(s) keeps this list sorted by submitted_at, so only the
        # arrived prefix needs to be inspected on every simulation step.
        split_at = 0
        for task in self.pending_tasks:
            if getattr(task, 'submitted_at', 0) > self.current_second:
                break
            released.append(task)
            split_at += 1
        remaining = self.pending_tasks[split_at:]

        self.pending_tasks = remaining
        if released:
            for task in released:
                task.transition_to(TaskStatus.SCHEDULING)
                print(f"[submit] Task {task.id} at simulation time {self.current_second:.0f}s arrived")
            self.ready_tasks.extend(released)
            if len(released) <= 5:
                for t in released:
                    print(f"[schedule] Task {t.id} at {self.current_second}s entered the scheduling queue")
            else:
                print(f"[schedule] {len(released)} tasks at {self.current_second}s entered the scheduling queue")

    def _dispatch_ready_tasks(self):
        """Scan the ready queue fairly and dispatch up to the batch limit."""
        if not self.ready_tasks:
            return

        batch_interval = max(1.0, float(getattr(self, "scheduling_config", {}).get("batch_interval_seconds", 60)))
        mode = (
            self._normalise_optimization_mode(self.optimization_config)
            if hasattr(self, "optimization_config") else "none"
        )
        # Local and joint coordination build and validate a complete rolling
        # power model.  Coalesce ready tasks before invoking that expensive
        # path, while keeping the queue intact between windows so no task is
        # skipped.  Heuristic scheduling remains responsive at its normal
        # batch interval.
        if mode in {"hierarchical", "joint"}:
            batch_interval = max(
                batch_interval,
                float(getattr(self, "optimization_config", {}).get(
                    "task_batch_interval_seconds", 300,
                )),
            )
        current_second = float(getattr(self, "current_second", 0))
        last_dispatch = getattr(self, "_last_dispatch_second", None)
        next_coordinated_dispatch = (
            current_second
            if last_dispatch is None
            else max(
                current_second,
                float(last_dispatch) + batch_interval,
            )
        )
        deadline_within_window = mode in {"hierarchical", "joint"} and any(
            float(task.deadline_second) <= next_coordinated_dispatch
            for task in self.ready_tasks
            if task.status == TaskStatus.SCHEDULING
        )
        if (
            last_dispatch is not None
            and current_second - float(last_dispatch) < batch_interval
            and not deadline_within_window
        ):
            return
        self._last_dispatch_second = current_second
        MAX_DISPATCH = int(getattr(self, "scheduling_config", {}).get("max_tasks_per_batch", 20))
        if mode == "hierarchical":
            MAX_DISPATCH = min(
                MAX_DISPATCH,
                int(getattr(self, "scheduling_config", {}).get("local_max_tasks_per_batch", 4)),
            )
        # Inspect the whole queue so a blocked GPU task at the front cannot
        # prevent a later feasible task from using idle resources.
        batch = list(self.ready_tasks)
        self.ready_tasks = []
        tasks_to_dispatch = []
        batch_task_ids = set()
        for task in batch:
            if task.id in batch_task_ids or task.id in self.dispatched_task_ids:
                print(f"[skip] Task {task.id} already dispatched in this simulation; skipping")
                continue
            if task.status != TaskStatus.SCHEDULING:
                print(f"[skip] Task {task.id} current status is {task.status.value}，not scheduling")
                continue
            batch_task_ids.add(task.id)
            tasks_to_dispatch.append(task)
        deferred_tasks = []

        # A task may wait after submission, but once its configured waiting
        # window expires it must not be silently dispatched.
        valid_tasks = []
        current_second = getattr(self, "current_second", 0)
        for task in tasks_to_dispatch:
            if task.is_expired(current_second):
                try:
                    task.cancel("exceeded max_wait_hours")
                except ValueError:
                    pass
                print(f"[timeout] Task {task.id} exceeded the maximum wait time; cancelling")
            else:
                valid_tasks.append(task)
        tasks_to_dispatch = valid_tasks
        if not tasks_to_dispatch:
            return

        # Freeze the actual current-period batch before any optimizer or AC
        # validation sees it.  Overflow remains queued for the next batch, so
        # the approved electrical operating point matches the launched tasks.
        current_batch = []
        for task in tasks_to_dispatch:
            not_before = getattr(task, "_joint_not_before_second", None)
            if (
                mode == "joint" and not_before is not None
                and float(not_before) > float(current_second)
            ):
                deferred_tasks.append(task)
                continue
            preferred = getattr(task, "user_specified_data_center", None)
            individually_eligible = bool(
                preferred and preferred in getattr(self, "dc_sims", {})
            ) or any(
                (not preferred or center.id == preferred)
                and center.can_allocate(task)
                for center in self.data_centers
            )
            if individually_eligible and len(current_batch) < MAX_DISPATCH:
                current_batch.append(task)
            else:
                task.allocated_data_center_id = None
                deferred_tasks.append(task)
        tasks_to_dispatch = current_batch
        if not tasks_to_dispatch:
            self.ready_tasks.extend(deferred_tasks)
            return

        optimization_tasks = list(tasks_to_dispatch)
        if mode == "joint":
            fresh_tasks = []
            active_tasks = []
            for task in tasks_to_dispatch:
                not_before = getattr(task, "_joint_not_before_second", None)
                if not_before is None:
                    fresh_tasks.append(task)
                    active_tasks.append(task)
                elif float(not_before) > float(current_second):
                    deferred_tasks.append(task)
                else:
                    task._joint_force_start_now = True
                    fresh_tasks.append(task)
                    active_tasks.append(task)
                    delattr(task, "_joint_not_before_second")
            tasks_to_dispatch = active_tasks
            optimization_tasks = fresh_tasks
            result = self.optimize_ready_tasks(optimization_tasks) if optimization_tasks else {"status": "optimal", "assignments": {}, "start_periods": {}}
            if optimization_tasks:
                self._last_renewable_optimization_second = float(current_second)
            if result.get("status") == "ac_validation_failed":
                # Removing one task at a time cannot repair a failed network
                # validation reliably, but it can launch N additional global
                # MIP solves for an N-task batch.  Keep the complete batch in
                # the ready queue and wait for the next grid state instead.
                print("[optimize] Joint optimization failed the power-flow check; retaining the batch for a later cycle")
                deferred_tasks.extend(tasks_to_dispatch)
                tasks_to_dispatch = []
                result = {"status": "ac_validation_failed", "assignments": {}, "start_periods": {}}
            elif result.get("status") not in {"optimal", "optimal_solution", "fallback_feasible"}:
                print(f"[optimize] Joint optimization batch infeasible ({result.get('status')}); trying a feasible subset")
                candidates = list(optimization_tasks)
                result = {"status": "infeasible", "assignments": {}}
                while candidates:
                    result = self.optimize_ready_tasks(candidates)
                    if result.get("status") in {"optimal", "optimal_solution"}:
                        break
                    deferred = candidates.pop()
                    deferred.allocated_data_center_id = None
                    deferred_tasks.append(deferred)
                    print(f"[wait] Task {deferred.id} deferred from this joint optimization; waiting for later resources")

                accepted_ids = {task.id for task in candidates}
                tasks_to_dispatch = [
                    task for task in tasks_to_dispatch
                    if task.id in accepted_ids
                ]
                if not candidates:
                    print("[optimize] No tasks were approved; retaining all tasks for the next solve")
            assignments = result.get("assignments", {})
            start_periods = result.get("start_periods", {})
            # Joint coordination uses the same rolling step as the power
            # problem (5 minutes by default). Convert optimizer periods to
            # simulation seconds instead of treating each period as one hour.
            joint_step_seconds = max(
                1.0,
                float(getattr(self, "optimization_config", {}).get(
                    "forecast_step_minutes", 5.0
                )) * 60.0,
            )
            future_ids = set()
            for task in tasks_to_dispatch:
                if assignments.get(task.id):
                    task.allocated_data_center_id = assignments[task.id]
                    start_period = int(start_periods.get(task.id, 0) or 0)
                    if start_period > 0:
                        task._joint_not_before_second = float(
                            current_second + start_period * joint_step_seconds
                        )
                        task._joint_planned_center = assignments[task.id]
                        future_ids.add(task.id)
                        deferred_tasks.append(task)
            assigned_ids = set(assignments)
            still_dispatchable = []
            for task in tasks_to_dispatch:
                if task.id in future_ids:
                    continue
                if task.id in assigned_ids:
                    still_dispatchable.append(task)
                else:
                    task.allocated_data_center_id = None
                    deferred_tasks.append(task)
            tasks_to_dispatch = still_dispatchable

        # Hierarchical mode first chooses a dispatch center for each task,
        # then lets the optimizer evaluate those fixed placements. Resource
        # reservation is delayed until the common dispatch loop below; doing
        # it here would make COPT count the same batch against capacity twice.
        if mode == "hierarchical":
            reserved_compute = defaultdict(float)
            reserved_bandwidth = defaultdict(float)
            reserved_gpu = defaultdict(lambda: defaultdict(int))
            planned_tasks = []
            for task in tasks_to_dispatch:
                if task.user_specified_data_center:
                    task.allocated_data_center_id = task.user_specified_data_center
                    planned_tasks.append(task)
                else:
                    # Automatic choices are provisional for this batch. A
                    # prior infeasible solve must not pin retries to stale
                    # centers whose capacity may since have changed.
                    task.allocated_data_center_id = None
                    try:
                        required_bandwidth = task.required_bandwidth_mbps or task.compute_bandwidth_requirement()
                        required_gpu = task.required_gpu_count or 0
                        gpu_model = task.required_gpu_type or task.gpu_type
                        eligible = []
                        for dc in self.data_centers:
                            if not dc.can_allocate(task):
                                continue
                            if dc.available_compute - reserved_compute[dc.id] < task.required_compute:
                                continue
                            if dc.available_bandwidth - reserved_bandwidth[dc.id] < required_bandwidth:
                                continue
                            if required_gpu:
                                compute_devices = [d for d in dc.device_pool if dc._is_compute_device(d)]
                                exact_model = gpu_model and any(d.model == gpu_model for d in compute_devices)
                                if exact_model:
                                    free_gpu = sum(
                                        d.quantity - dc.used_device_count(d.model) - reserved_gpu[dc.id][d.model]
                                        for d in compute_devices if d.model == gpu_model
                                    )
                                else:
                                    # Unknown model labels (V100, T4, MISC,
                                    # numeric legacy values) use any available
                                    # compute device in the simulated fleet.
                                    free_gpu = sum(
                                        d.quantity - dc.used_device_count(d.model)
                                        for d in compute_devices
                                    ) - sum(reserved_gpu[dc.id].values())
                                    if not compute_devices:
                                        virtual_capacity = max(1, int(dc.total_compute // dc._VIRTUAL_GPU_TFLOPS))
                                        free_gpu = virtual_capacity - reserved_gpu[dc.id]["__any__"]
                                if exact_model:
                                    free_gpu -= reserved_gpu[dc.id]["__any__"]
                                if free_gpu < required_gpu:
                                    continue
                            eligible.append(dc)
                        if not eligible:
                            raise ValueError("batch resource capacity is insufficient")
                        chosen = self._select_data_center(task, eligible)
                        task.allocated_data_center_id = chosen.id
                        reserved_compute[chosen.id] += task.required_compute
                        reserved_bandwidth[chosen.id] += required_bandwidth
                        planned_tasks.append(task)
                        if required_gpu:
                            compute_devices = [d for d in chosen.device_pool if chosen._is_compute_device(d)]
                            exact_model = gpu_model and any(d.model == gpu_model for d in compute_devices)
                            reservation_key = gpu_model if exact_model else "__any__"
                            reserved_gpu[chosen.id][reservation_key] += required_gpu
                    except Exception as e:
                        print(f"[wait] Task {task.id} no datacenter is available; retaining in the scheduling queue: {e}")
                        deferred_tasks.append(task)
            # Tasks that failed this batch's capacity-aware preselection must
            # remain queued; including them in the same COPT model makes the
            # entire otherwise-feasible batch infeasible.
            tasks_to_dispatch = list(planned_tasks)
            planned_optimizable_tasks = list(planned_tasks)
            result = self.optimize_ready_tasks(planned_optimizable_tasks) if planned_optimizable_tasks else {"status": "optimal", "assignments": {}}
            if planned_optimizable_tasks:
                self._last_renewable_optimization_second = float(current_second)
            if result.get("status") not in {"optimal", "optimal_solution", "fallback_feasible"}:
                print(f"[optimize] Fixed placement is infeasible ({result.get('status')}); redistributing across datacenters")
                for task in planned_optimizable_tasks:
                    task.allocated_data_center_id = None

                candidates = list(planned_optimizable_tasks)
                fallback_result = {"status": "infeasible", "assignments": {}}
                while candidates:
                    # Retry one task at a time after a failed coordinated
                    # batch.  Re-submitting the entire batch recreates the
                    # same infeasible MIP indefinitely.
                    trial = candidates[:1]
                    fallback_result = self.optimize_ready_tasks(trial)
                    if fallback_result.get("status") in {"optimal", "optimal_solution", "fallback_feasible"}:
                        candidates = trial
                        break
                    # Keep making progress when only part of the batch can fit
                    # the resources currently available. Tasks removed here
                    # stay queued and can be reconsidered after completions.
                    deferred = candidates.pop()
                    deferred.allocated_data_center_id = None
                    deferred_tasks.append(deferred)
                print(f"[wait] Task {deferred.id} deferred from this optimization; waiting for later resources")

                if not candidates:
                    self.ready_tasks.extend(deferred_tasks)
                    return

                result = fallback_result
                for task in candidates:
                    assigned_dc = result.get("assignments", {}).get(task.id)
                    if assigned_dc:
                        task.allocated_data_center_id = assigned_dc
                accepted_ids = {task.id for task in candidates}
                tasks_to_dispatch = [
                    task for task in planned_tasks
                    if task.id in accepted_ids
                ]
            if result.get("status") in {"optimal", "optimal_solution", "fallback_feasible"}:
                for task in planned_tasks:
                    assigned_dc = result.get("assignments", {}).get(task.id)
                    if assigned_dc:
                        task.allocated_data_center_id = assigned_dc

        # A local coordination assignment can still fail the AC check after
        # all tasks are projected onto the grid.  Retry smaller batches so a
        # single high-power task does not block the whole ready queue.
        if mode == "hierarchical" and result.get("status") not in {
            "optimal", "optimal_solution", "fallback_feasible"
        }:
            candidates = list(tasks_to_dispatch)
            tasks_to_dispatch = []
            while candidates:
                candidate = candidates[:1]
                retry = self.optimize_ready_tasks(candidate)
                if retry.get("status") in {"optimal", "optimal_solution", "fallback_feasible"}:
                    task = candidate[0]
                    assigned_dc = retry.get("assignments", {}).get(task.id)
                    if assigned_dc:
                        task.allocated_data_center_id = assigned_dc
                        tasks_to_dispatch.append(task)
                else:
                    candidates[0].allocated_data_center_id = None
                    deferred_tasks.append(candidates[0])
                print(f"[wait] Single-task power-flow check for {candidates[0].id} is still infeasible; waiting for later resources")
                candidates = candidates[1:]

        if mode == "hierarchical" and result.get("status") not in {
            "optimal", "optimal_solution", "fallback_feasible"
        }:
            # Never launch a task batch after the coordination model reports
            # infeasible.  Older fallback paths could leave provisional
            # assignments in tasks_to_dispatch and dispatch them anyway.
            for task in tasks_to_dispatch:
                task.allocated_data_center_id = None
                deferred_tasks.append(task)
            tasks_to_dispatch = []

        dispatched_count = 0
        heuristic_assignments = {}
        heuristic_tasks = {}
        for task in tasks_to_dispatch:
            if dispatched_count >= MAX_DISPATCH:
                task.allocated_data_center_id = None
                deferred_tasks.append(task)
                continue
            # A user-specified placement is authoritative.  Only tasks with
            # an empty preference enter the scheduler/COPT allocation path.
            preferred_dc = getattr(task, "user_specified_data_center", None)
            if preferred_dc:
                dc_id = preferred_dc
                task.allocated_data_center_id = dc_id
            elif task.allocated_data_center_id:
                dc_id = task.allocated_data_center_id
            else:
                try:
                    chosen_dc = self._select_data_center(task, self.data_centers)
                    dc_id = chosen_dc.id
                    self._reserve_task_resources(task, chosen_dc)
                    task.allocated_data_center_id = dc_id
                except Exception as e:
                    print(f"[wait] Task {task.id} no datacenter is available; retaining in the scheduling queue: {e}")
                    deferred_tasks.append(task)
                    continue
            if dc_id in self.dc_sims:
                target_dc = next((dc for dc in self.data_centers if dc.id == dc_id), None)
                try:
                    if target_dc is not None:
                        self._reserve_task_resources(task, target_dc)
                except ValueError as e:
                    print(f"[wait] Task {task.id} is waiting for datacenter {dc_id} to release resources: {e}")
                    deferred_tasks.append(task)
                    continue
                task.actual_dispatch_second = float(current_second)
                for attribute in ("_joint_force_start_now", "_joint_planned_center"):
                    if hasattr(task, attribute):
                        delattr(task, attribute)
                print(f"[schedule] Task {task.id} assigned to {dc_id}")
                self.dispatched_task_ids.add(task.id)
                if mode == "none":
                    heuristic_assignments[task.id] = dc_id
                    heuristic_tasks[task.id] = task
                self.env.process(self.dc_sims[dc_id].run_task(task))
                dispatched_count += 1
            else:
                print(f"Datacenter {dc_id} does not exist; Task {task.id} discarded")

        # Rotate blocked work to the tail instead of retrying the same queue
        # prefix forever.
        self.ready_tasks.extend(deferred_tasks)
        if mode == "none" and heuristic_assignments:
            self._heuristic_pending_projection = (
                heuristic_assignments, heuristic_tasks
            )

    # Implementation note.

    def _running_compute_by_datacenter(self) -> Dict[str, Dict[str, float | str]]:
        """Return the instantaneous running compute load for each data center."""
        result = {}
        for dc_id, sim in self.dc_sims.items():
            result[dc_id] = {
                "name": sim.dc.name,
                "compute_tflops": float(sum(
                    getattr(task, "required_compute", 0.0)
                    for task in sim.running_tasks
                )),
            }
        return result

    def _datacenter_runtime_summary(self) -> Dict[str, Dict[str, float | int | str]]:
        """Return live resource and power values for dashboard monitoring."""
        summary = {}
        for dc_id, sim in self.dc_sims.items():
            dc = sim.dc
            total_compute = float(getattr(dc, "total_compute", 0.0))
            total_bandwidth = float(getattr(dc, "total_bandwidth", 0.0))
            used_compute = float(getattr(dc, "used_compute", 0.0))
            used_bandwidth = float(getattr(dc, "used_bandwidth", 0.0))
            summary[dc_id] = {
                "name": dc.name,
                "total_compute": total_compute,
                "used_compute": used_compute,
                "compute_utilization": used_compute / total_compute if total_compute else 0.0,
                "total_bandwidth": total_bandwidth,
                "used_bandwidth": used_bandwidth,
                "bandwidth_utilization": used_bandwidth / total_bandwidth if total_bandwidth else 0.0,
                "running_tasks": len(sim.running_tasks),
                "power_mw": float(getattr(sim, "current_load_mw", 0.0)),
            }
        return summary

    def _record_stats(self):
        """Implementation documentation."""
        # Implementation note.
        all_running = []
        for sim in self.dc_sims.values():
            all_running.extend(sim.running_tasks)

        stats = defaultdict(int)
        for task in all_running:
            stats[getattr(task, 'task_type', 'unknown')] += 1

        # Implementation note.
        s = self.current_second
        hours = s // 3600
        minutes = (s % 3600) // 60
        seconds = s % 60

        # Implementation note.
        total_load = self.pp_adapter.get_total_load()
        total_gen = self.pp_adapter.get_total_generation()
        generation_breakdown = (
            self.pp_adapter.get_generation_breakdown()
            if hasattr(self.pp_adapter, "get_generation_breakdown") else {}
        )
        total_emission = self.pp_adapter.get_total_emission()
        renewable_dispatch = self.pp_adapter.get_renewable_dispatch_summary()
        renewable_energy = getattr(self, "renewable_energy_mwh", {})
        wind = renewable_dispatch.get("wind", {})
        pv = renewable_dispatch.get("pv", {})
        renewable_available_mw = float(wind.get("available_mw", 0.0) + pv.get("available_mw", 0.0))
        renewable_consumed_mw = float(wind.get("dispatched_mw", 0.0) + pv.get("dispatched_mw", 0.0))
        renewable_curtailment_mw = float(wind.get("curtailed_mw", 0.0) + pv.get("curtailed_mw", 0.0))
        # Keep the displayed supply and purchase values on one snapshot.  The
        # AC result table may be older than the current load/dispatch because
        # full power flow runs at a coarser interval.  Reconcile the grid
        # purchase from the actual dispatched local components, never from
        # renewable availability before curtailment.
        local_generation = sum(
            max(0.0, float(generation_breakdown.get(name, 0.0) or 0.0))
            for name in (
                "base_generation_mw", "conventional_generation_mw",
                "wind_generation_mw", "pv_generation_mw",
                "storage_discharge_mw",
            )
        )
        network_loss = 0.0
        if hasattr(self.pp_adapter, "get_network_loss_mw"):
            try:
                network_loss = max(0.0, float(self.pp_adapter.get_network_loss_mw() or 0.0))
            except (TypeError, ValueError):
                network_loss = 0.0
        displayed_total_load = display_load_with_network_loss(total_load, network_loss)
        synchronized_grid_import = max(0.0, displayed_total_load - local_generation)
        generation_breakdown["grid_import_mw"] = synchronized_grid_import
        total_gen = local_generation + synchronized_grid_import
        storage_runtime = self._storage_runtime_summary()
        self._accumulate_operating_cost(
            grid_import_mw=float(generation_breakdown.get("grid_import_mw", 0.0) or 0.0),
            storage_power_mw=float(storage_runtime.get("storage_power_mw", 0.0) or 0.0),
        )
        grid_line_health = self.pp_adapter.get_line_health()
        grid_voltage_health = self.pp_adapter.get_voltage_health()
        reserve_capacity_mw = self.pp_adapter.get_reserve_capacity_mw()
        compute_power_mw = sum(getattr(sim, "current_load_mw", 0.0) for sim in self.dc_sims.values())
        initial_load_mw = (
            float(self.pp_adapter.get_initial_load_mw())
            if hasattr(self.pp_adapter, "get_initial_load_mw")
            else max(0.0, float(total_load) - compute_power_mw)
        )
        planned_generation_mw = float(generation_breakdown.get("base_generation_mw", 0.0) or 0.0)
        net_load_mw = calculate_net_load_mw(
            initial_load_mw, planned_generation_mw, compute_power_mw
        )
        task_power = task_power_snapshot(
            getattr(self, "task_registry", {}).values(),
            getattr(self, "dc_sims", {}),
            self.current_second,
        )
        running_compute_by_datacenter = self._running_compute_by_datacenter()
        datacenter_runtime = self._datacenter_runtime_summary()
        optimization_snapshot = dict(getattr(self, "latest_optimization_result", {}) or {})
        normalized_mode = self._normalise_optimization_mode(getattr(self, "optimization_config", {}))
        coordination_mode = {
            "none": "heuristic", "hierarchical": "local", "joint": "global",
        }[normalized_mode]

        # Implementation note.
        dc_emissions = {}
        for dc_id, sim in self.dc_sims.items():
            dc_emissions[dc_id] = {
                'name': sim.dc.name,
                'energy_mwh': sim.total_energy_mwh,
                'emission_kg': sim.total_emission_kg,
            }

        # Implementation note.
        cpf_result = None
        if self._periodic_due(
            "_last_cpf_second", getattr(self, "cpf_interval_seconds", 30)
        ):
            try:
                cpf_result = self.pp_adapter.analyze_voltage_stability(
                    step=0.05, max_steps=30
                )
                self.last_cpf_result = cpf_result
            except Exception as e:
                print(f"CPF analysis failed: {e}")
        effective_cpf = cpf_result or getattr(self, "last_cpf_result", None) or {}
        grid_health = calculate_grid_health(
            converged=effective_cpf.get("converged", getattr(self.pp_adapter, "_power_flow_valid", None)),
            voltage=grid_voltage_health,
            line=grid_line_health,
            voltage_margin_pct=effective_cpf.get("voltage_margin_pct"),
            reserve_capacity_mw=reserve_capacity_mw,
            total_load_mw=displayed_total_load,
        )
        debug_surplus = (
                self.pp_adapter.renewable_available_mw.get("wind", 0.0)
                + self.pp_adapter.renewable_available_mw.get("pv", 0.0)
                + generation_breakdown.get("base_generation_mw", 0.0)
                - displayed_total_load
        )
        current_storage_power = sum(
            float(storage.current_power_mw) for storage in self.storages.values()
        )

        # Implementation note.
        record = {
            'second': self.current_second,
            'time_str': f"{hours:02d}:{minutes:02d}:{seconds:02d}",
            'clock_time': self.current_datetime().strftime("%Y-%m-%d %H:%M:%S"),
            'running_tasks': len(all_running),
            'task_types': dict(stats),
            'total_load': displayed_total_load,
            'initial_load_mw': initial_load_mw,
            'planned_generation_mw': planned_generation_mw,
            'net_load_mw': net_load_mw,
            'total_generation': total_gen,
            'base_generation_mw': generation_breakdown.get('base_generation_mw', 0.0),
            'conventional_generation_mw': generation_breakdown.get('conventional_generation_mw', 0.0),
            'grid_import_mw': generation_breakdown.get('grid_import_mw', 0.0),
            'storage_discharge_mw': generation_breakdown.get('storage_discharge_mw', 0.0),
            'wind_available_mw': wind.get('available_mw', 0.0),
            'wind_generation_mw': wind.get('dispatched_mw', 0.0),
            'wind_consumed_mw': wind.get('dispatched_mw', 0.0),
            'wind_curtailed_mw': wind.get('curtailed_mw', 0.0),
            'wind_consumption_rate': wind.get('dispatched_mw', 0.0) / wind['available_mw'] if wind.get('available_mw', 0.0) > 0 else None,
            'pv_available_mw': pv.get('available_mw', 0.0),
            'pv_generation_mw': pv.get('dispatched_mw', 0.0),
            'pv_consumed_mw': pv.get('dispatched_mw', 0.0),
            'pv_curtailed_mw': pv.get('curtailed_mw', 0.0),
            'pv_consumption_rate': pv.get('dispatched_mw', 0.0) / pv['available_mw'] if pv.get('available_mw', 0.0) > 0 else None,
            'renewable_available_mw': renewable_available_mw,
            'renewable_consumed_mw': renewable_consumed_mw,
            'renewable_curtailed_mw': renewable_curtailment_mw,
            'renewable_consumption_rate': renewable_consumed_mw / renewable_available_mw if renewable_available_mw > 0 else None,
            'wind_available_energy_mwh': renewable_energy.get('wind', {}).get('available', 0.0),
            'wind_consumed_energy_mwh': renewable_energy.get('wind', {}).get('consumed', 0.0),
            'wind_curtailed_energy_mwh': renewable_energy.get('wind', {}).get('curtailed', 0.0),
            'pv_available_energy_mwh': renewable_energy.get('pv', {}).get('available', 0.0),
            'pv_consumed_energy_mwh': renewable_energy.get('pv', {}).get('consumed', 0.0),
            'pv_curtailed_energy_mwh': renewable_energy.get('pv', {}).get('curtailed', 0.0),
            'renewable_available_energy_mwh': sum(source.get('available', 0.0) for source in renewable_energy.values()),
            'renewable_consumed_energy_mwh': sum(source.get('consumed', 0.0) for source in renewable_energy.values()),
            'renewable_curtailed_energy_mwh': sum(source.get('curtailed', 0.0) for source in renewable_energy.values()),
            'renewable_energy_consumption_rate': (
                sum(source.get('consumed', 0.0) for source in renewable_energy.values())
                / sum(source.get('available', 0.0) for source in renewable_energy.values())
                if sum(source.get('available', 0.0) for source in renewable_energy.values()) > 0 else None
            ),
            'renewable_dispatch_mode': getattr(self, 'renewable_dispatch_result', {}).get('mode'),
            'renewable_dispatch_status': getattr(self, 'renewable_dispatch_result', {}).get('status'),
            'grid_exchange_mw': self.pp_adapter.get_grid_exchange_mw(),
            'network_loss_mw': (
                self.pp_adapter.get_network_loss_mw()
                if hasattr(self.pp_adapter, 'get_network_loss_mw') else None
            ),
            **storage_runtime,
            'reserve_capacity_mw': reserve_capacity_mw,
            'grid_line_health': grid_line_health,
            'grid_voltage_health': grid_voltage_health,
            'grid_health': grid_health,
            'total_emission_rate': total_emission,
            'compute_power_mw': compute_power_mw,
            **task_power,
            'running_compute_by_datacenter': running_compute_by_datacenter,
            'datacenter_runtime': datacenter_runtime,
            'dc_emissions': dc_emissions,
            'cpf_result': cpf_result,
            'task_summary': self._task_summary(),
            'coordination_mode': optimization_snapshot.get('mode', coordination_mode),
            'coordination_iterations': optimization_snapshot.get(
                'coordination_iterations', 1 if optimization_snapshot else 0
            ),
            'generator_dispatch': optimization_snapshot.get('generator_mw', {}),
            'renewable_dispatch': optimization_snapshot.get(
                'renewable_dispatched_mw', {}
            ),
            'renewable_curtailment_dispatch': optimization_snapshot.get(
                'renewable_curtailed_mw', {}
            ),
            'storage_dispatch': optimization_snapshot.get('storage_power_mw', {}),
            'storage_soc_dispatch_mwh': optimization_snapshot.get(
                'storage_soc_mwh', {}
            ),
            'objective_components': optimization_snapshot.get('objective_components', {}),
            'strictly_feasible': optimization_snapshot.get('strictly_feasible'),
            'forecast_fill': optimization_snapshot.get('forecast_fill'),
            'ac_validation_status': optimization_snapshot.get(
                'ac_validation_status'
            ),
            'feasibility_slack_used': False,
            'generator_parameter_sources': {
                str(generator.get('id') or generator.get('name') or f'gen_{index}'):
                    dict(generator.get('parameter_sources', {}))
                for index, generator in enumerate(
                    getattr(self.pp_adapter, 'topology', {}).get('generators', [])
                )
            },
        }
        self.stats_records[self.current_second] = record
        if self._periodic_due(
            "_last_report_second", getattr(self, "report_interval_seconds", 5)
        ):
            self._write_visual_report(record)

        # Implementation note.
        print(f"\n{'=' * 60}")
        print(f"Statistics #{self.stats_count}  |  Simulation time: {record['time_str']}  |  Interval: {self.stats_interval_seconds}s")
        print(f"{'=' * 60}")
        print(
            f"  Running tasks: {len(all_running)}  |  Pending release: {len(self.pending_tasks)}  |  Ready to schedule: {len(self.ready_tasks)}")
        print(f"  Total grid load: {total_load:.2f} MW  |  Total generation: {total_gen:.2f} MW")
        print(format_emission_log(total_emission))

        if len(all_running) > 0:
            for t, cnt in stats.items():
                pct = cnt / len(all_running) * 100
                # bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                # print(f"    {t:20s}: {cnt:2d} tasks ({pct:5.1f}%) {bar}")
        else:
            print(format_idle_log())

        # Implementation note.
        has_emission = any(info['energy_mwh'] > 0 for info in dc_emissions.values())
        if has_emission:
            print(f"\nCumulative datacenter emissions:")
            for dc_id, info in dc_emissions.items():
                if info['energy_mwh'] > 0:
                    print(f"    {info['name']} ({dc_id}): "
                          f"Energy {info['energy_mwh']:.2f} MWh, "
                          f"Emissions {info['emission_kg']:.1f} kgCO₂")

        # Implementation note.
        if cpf_result is not None:
            print(f"\nVoltage stability analysis (continuation power flow):")
            print(f"Critical load multiplier: {cpf_result['lambda_critical']:.2f} pu")
            print(f"Voltage stability margin: {cpf_result['voltage_margin_pct']:.1f}%")
            print(f"Minimum voltage at critical point: {cpf_result['min_voltage_at_critical']:.3f} pu")

    # Implementation note.

    def _task_summary(self) -> Dict:
        tasks = list(getattr(self, "task_registry", {}).values())
        arrived = [t for t in tasks if getattr(t, "submitted_at", 0) <= self.current_second]
        status_counts = {}
        for task in arrived:
            raw_status = getattr(task, "status", "unknown")
            status = getattr(raw_status, "value", raw_status)
            status_counts[str(status)] = status_counts.get(str(status), 0) + 1
        total = len(arrived)
        running = status_counts.get(TaskStatus.RUNNING.value, 0)
        completed = status_counts.get(TaskStatus.COMPLETED.value, 0)
        cancelled = status_counts.get(TaskStatus.CANCELLED.value, 0)
        rejected = status_counts.get(TaskStatus.REJECTED.value, 0)
        scheduling = status_counts.get(TaskStatus.SCHEDULING.value, 0)
        queued = sum(status_counts.get(status, 0) for status in (
            TaskStatus.SUBMITTED.value, TaskStatus.PENDING.value, TaskStatus.READY.value,
        ))
        return {
            "submitted": total,
            "total": total,
            "dispatched": sum(1 for t in arrived if getattr(t, "actual_start_second", None) is not None),
            "running": running,
            "queued": queued,
            "scheduling": scheduling,
            "completed": completed,
            "cancelled": cancelled,
            "rejected": rejected,
            "unclassified": max(0, total - running - queued - scheduling - completed - cancelled - rejected),
            "avg_planned_duration_seconds": (sum(t.duration_hours * 3600 for t in arrived) / len(arrived)) if arrived else 0.0,
            "avg_actual_execution_seconds": (sum(t.actual_execution_seconds for t in arrived if t.actual_execution_seconds is not None) / max(1, sum(1 for t in arrived if t.actual_execution_seconds is not None))),
        }

    def _accumulate_operating_cost(self, *, grid_import_mw: float, storage_power_mw: float) -> None:
        """Accumulate sampled operating cost in yuan for the console summary."""
        hours = max(1.0, float(getattr(self, "stats_interval_seconds", 1.0) or 1.0)) / 3600.0
        problem = getattr(self, "latest_power_dispatch_problem", None)
        result = getattr(self, "latest_power_dispatch_result", None)
        price = 0.0
        if problem is not None and getattr(problem, "electricity_prices", None):
            price = float(problem.electricity_prices[0] or 0.0)
        elif getattr(self, "fixed_pricing", None) is not None:
            current = self.current_datetime()
            price = float(self.fixed_pricing.price_at_hour(current.hour + current.minute / 60.0))
        else:
            price = 0.5
        grid_cost = max(0.0, float(grid_import_mw)) * price * 1000.0 * hours
        generator_cost = 0.0
        if problem is not None and result is not None:
            for generator in getattr(problem, "generators", []):
                values = getattr(result, "generator_mw", {}).get(generator.id, [])
                power = float(values[0]) if values else 0.0
                generator_cost += (
                    float(generator.cost_linear_yuan_per_mwh) * power
                    + float(generator.cost_quadratic_yuan_per_mw2h) * power * power
                ) * hours
        storage_cost = 0.0
        for storage in getattr(self, "storages", {}).values():
            storage_cost += abs(float(storage_power_mw)) * float(storage.degradation_cost_per_mwh) * hours
        self.total_operating_cost_yuan = float(getattr(self, "total_operating_cost_yuan", 0.0)) + grid_cost + generator_cost + storage_cost

    def _write_visual_report(self, record: Dict) -> None:
        """Write dependency-free CSV and SVG visual reports at each stats tick."""
        report_dir = Path(getattr(self, "report_dir", Path("reports")))
        report_dir.mkdir(parents=True, exist_ok=True)
        csv_path = report_dir / "simulation_stats.csv"
        import csv
        row = {k: v for k, v in record.items() if not isinstance(v, (dict, list))}
        row.update({f"task_{k}": v for k, v in record["task_summary"].items()})
        write_header = not csv_path.exists()
        with csv_path.open("a", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(row))
            if write_header:
                writer.writeheader()
            writer.writerow(row)
        detail_path = report_dir / "task_execution.csv"
        detail_fields = ["task_id", "status", "submitted_at", "planned_duration_seconds", "actual_start_second", "actual_end_second", "actual_execution_seconds", "required_compute", "actual_power_mw", "allocated_data_center_id"]
        # This is a live snapshot, not an event log: overwrite it each tick
        # so two runs or two statistics windows cannot be mixed accidentally.
        with detail_path.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=detail_fields)
            writer.writeheader()
            for task in getattr(self, "task_registry", {}).values():
                writer.writerow({
                    "task_id": task.id,
                    "status": task.status.value,
                    "submitted_at": task.submitted_at,
                    "planned_duration_seconds": task.duration_hours * 3600.0,
                    "actual_start_second": getattr(task, "actual_start_second", None),
                    "actual_end_second": getattr(task, "actual_end_second", None),
                    "actual_execution_seconds": getattr(task, "actual_execution_seconds", None),
                    "required_compute": task.required_compute,
                    "actual_power_mw": getattr(task, "actual_power_mw", None),
                    "allocated_data_center_id": task.allocated_data_center_id,
                })
        self._write_svg_report(report_dir / "simulation_dashboard.svg")

    def _write_svg_report(self, path: Path) -> None:
        points = list(self.stats_records.values())
        if not points:
            return
        def legend_name(label: str) -> str:
            """Keep units on axes, but omit them from legend captions."""
            return str(label).split(" (", 1)[0]
        def clean(value) -> float:
            try:
                return float(value or 0.0)
            except (TypeError, ValueError):
                return 0.0

        def make_chart(target: Path, series: list[tuple[str, str, list[float]]],
                       ylabel: str, xlabel: str = "Time (h)"):
            width, height = 1000, 560
            left, top, right, bottom = 90, 55, 950, 465
            all_values = [clean(v) for _, _, values in series for v in values]
            vmax = max(abs(v) for v in all_values) if all_values else 1.0
            vmax = vmax or 1.0
            # Keep zero visible when a series contains negative values.
            ymin = min(0.0, min(all_values or [0.0]))
            ymax = max(vmax, max(all_values or [0.0]))
            span = max(1.0, ymax - ymin)
            horizon_seconds = max(24 * 3600, max((clean(p.get("second", 0)) for p in points), default=0.0))
            def y(value):
                return bottom - (clean(value) - ymin) / span * (bottom - top)
            svg = [
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
                '<rect width="100%" height="100%" fill="white"/>',
                f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="#555"/>',
                f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="#555"/>',
                f'<text x="18" y="{(top + bottom) / 2:.0f}" font-size="15" text-anchor="middle" transform="rotate(-90 18 {(top + bottom) / 2:.0f})">{ylabel}</text>',
                f'<text x="{(left + right) / 2:.0f}" y="535" font-size="15" text-anchor="middle">{xlabel}</text>',
            ]
            for hour in (0, 6, 12, 18, 24):
                x_tick = left + hour / 24 * (right - left)
                svg.append(f'<text x="{x_tick:.1f}" y="487" font-size="12" text-anchor="middle">{hour}</text>')
            for series_index, (label, color, values) in enumerate(series):
                coords = []
                for i, value in enumerate(values):
                    second = clean(points[i].get("second", i)) if i < len(points) else i
                    x = left + min(horizon_seconds, max(0.0, second)) / horizon_seconds * (right - left)
                    coords.append((x, y(value)))
                svg.append(f'<polyline fill="none" stroke="{color}" stroke-width="3" points="{" ".join(f"{x:.1f},{yy:.1f}" for x,yy in coords)}"/>')
                legend_row = series_index // 3
                legend_col = series_index % 3
                legend_x = left + legend_col * 270
                legend_y = 30 + legend_row * 18
                svg.append(f'<rect x="{legend_x}" y="{legend_y - 10}" width="12" height="12" fill="{color}"/>')
                svg.append(f'<text x="{legend_x + 18}" y="{legend_y}" fill="{color}" font-size="14">{legend_name(label)}</text>')
            target.write_text("".join(svg) + '</svg>', encoding="utf-8")

        def make_stacked_generation_chart(target: Path, series: list[tuple[str, str, list[float]]]):
            """Write a positive generation stack with a readable top-left legend."""
            width, height = 1000, 560
            left, top, right, bottom = 90, 100, 950, 465
            horizon_seconds = max(
                24 * 3600,
                max((clean(p.get("second", 0)) for p in points), default=0.0),
            )
            values = [
                [max(0.0, clean(value)) for value in item_values]
                for _, _, item_values in series
            ]
            totals = [
                sum(item[index] for item in values)
                for index in range(len(points))
            ]
            line_series = [
                ("Total Load (MW)", "#274c5e", [clean(p.get("total_load")) for p in points]),
                ("Total Generation (MW)", "#b35f3f", [clean(p.get("total_generation")) for p in points]),
            ]
            ymax = max([*(totals or [1.0]), *(value for _, _, values in line_series for value in values)]) or 1.0

            def x_at(index: int) -> float:
                second = clean(points[index].get("second", index))
                return left + min(horizon_seconds, max(0.0, second)) / horizon_seconds * (right - left)

            def y_at(value: float) -> float:
                return bottom - value / ymax * (bottom - top)

            svg = [
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
                '<rect width="100%" height="100%" fill="white"/>',
                f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="#9eb0c2"/>',
                f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="#9eb0c2"/>',
                f'<text x="18" y="{(top + bottom) / 2:.0f}" font-size="15" text-anchor="middle" transform="rotate(-90 18 {(top + bottom) / 2:.0f})">Power (MW)</text>',
                f'<text x="{(left + right) / 2:.0f}" y="535" font-size="15" text-anchor="middle">Time (h)</text>',
            ]
            for hour in (0, 6, 12, 18, 24):
                x_tick = left + hour / 24 * (right - left)
                svg.append(f'<text x="{x_tick:.1f}" y="487" font-size="12" text-anchor="middle">{hour}</text>')

            lower = [0.0] * len(points)
            for series_index, (label, color, _) in enumerate(series):
                upper = [lower[i] + values[series_index][i] for i in range(len(points))]
                polygon_points = [
                    (x_at(i), y_at(upper[i])) for i in range(len(points))
                ] + [
                    (x_at(i), y_at(lower[i])) for i in reversed(range(len(points)))
                ]
                points_text = " ".join(f"{x:.1f},{y:.1f}" for x, y in polygon_points)
                svg.append(
                    f'<polygon fill="{color}" fill-opacity="0.78" stroke="{color}" '
                    f'stroke-width="0.8" points="{points_text}"/>'
                )
                lower = upper
            for label, color, line_values in line_series:
                coords = " ".join(f"{x_at(i):.1f},{y_at(value):.1f}" for i, value in enumerate(line_values))
                svg.append(f'<polyline fill="none" stroke="{color}" stroke-width="2.8" points="{coords}"/>')
            legend_items = [*[(label, color) for label, color, _ in series], *[(label, color) for label, color, _ in line_series]]
            for legend_index, (label, color) in enumerate(legend_items):
                legend_row = legend_index // 3
                legend_col = legend_index % 3
                legend_x = 110 + legend_col * 270
                legend_y = 58 + legend_row * 20
                svg.append(f'<rect x="{legend_x}" y="{legend_y - 9}" width="12" height="12" fill="{color}"/>')
                svg.append(f'<text x="{legend_x + 18}" y="{legend_y}" fill="{color}" font-size="13">{legend_name(label)}</text>')
            target.write_text("".join(svg) + '</svg>', encoding="utf-8")

        def make_supply_storage_chart(target: Path):
            width, height = 1000, 560
            left, top, right, bottom = 90, 90, 950, 465
            horizon_seconds = max(24 * 3600, max((clean(p.get("second", 0)) for p in points), default=0.0))
            load_values = [clean(p.get("net_load_mw", p.get("total_load"))) for p in points]
            wind_values = [max(0.0, clean(p.get("wind_available_mw"))) for p in points]
            pv_values = [max(0.0, clean(p.get("pv_available_mw"))) for p in points]
            net_load_values = load_values
            storage_values = [clean(p.get("storage_power_mw", p.get("storage_discharge_mw"))) for p in points]
            renewable_stack = [wind_values[i] + pv_values[i] for i in range(len(points))]
            left_abs_max = max([1.0, *(abs(value) for value in net_load_values), *renewable_stack])
            storage_abs_max = max([1.0, *(abs(value) for value in storage_values)])

            def x_at(index):
                second = clean(points[index].get("second", index))
                return left + min(horizon_seconds, max(0.0, second)) / horizon_seconds * (right - left)

            def y_left(value):
                zero = (top + bottom) / 2
                return zero - clean(value) / left_abs_max * (bottom - top) / 2

            def y_storage(value):
                zero = (top + bottom) / 2
                return zero - clean(value) / storage_abs_max * (bottom - top) / 2

            svg = [
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
                '<rect width="100%" height="100%" fill="white"/>',
                f'<line x1="{left}" y1="{(top + bottom) / 2:.1f}" x2="{right}" y2="{(top + bottom) / 2:.1f}" stroke="#9eb0c2"/>',
                f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="#9eb0c2"/>',
                f'<text x="18" y="{(top + bottom) / 2:.0f}" font-size="15" text-anchor="middle" transform="rotate(-90 18 {(top + bottom) / 2:.0f})">Net Load (MW)</text>',
                f'<text x="982" y="{(top + bottom) / 2:.0f}" font-size="15" text-anchor="middle" transform="rotate(90 982 {(top + bottom) / 2:.0f})">Storage Power (MW)</text>',
                f'<text x="{(left + right) / 2:.0f}" y="535" font-size="15" text-anchor="middle">Time (h)</text>',
            ]
            for hour in (0, 6, 12, 18, 24):
                x_tick = left + hour / 24 * (right - left)
                svg.append(f'<text x="{x_tick:.1f}" y="487" font-size="12" text-anchor="middle">{hour}</text>')
            lower = [0.0] * len(points)
            for label, color, values in [
                ("Wind Input (MW)", "#6f9aa8", wind_values),
                ("PV Input (MW)", "#c5a56a", pv_values),
            ]:
                upper = [lower[i] + values[i] for i in range(len(points))]
                polygon = [(x_at(i), y_left(upper[i])) for i in range(len(points))]
                polygon += [(x_at(i), y_left(lower[i])) for i in reversed(range(len(points)))]
                svg.append(f'<polygon fill="{color}" fill-opacity="0.60" stroke="{color}" stroke-width="0.8" points="{" ".join(f"{x:.1f},{y:.1f}" for x,y in polygon)}"/>')
                lower = upper
            for label, color, values in [
                ("Net Load (MW)", "#385b6a", net_load_values),
            ]:
                coords = " ".join(f"{x_at(i):.1f},{y_left(value):.1f}" for i, value in enumerate(values))
                svg.append(f'<polyline fill="none" stroke="{color}" stroke-width="3" points="{coords}"/>')
            zero_y = (top + bottom) / 2
            svg.append(f'<line x1="{left}" y1="{zero_y:.1f}" x2="{right}" y2="{zero_y:.1f}" stroke="#8f8792" stroke-dasharray="4 3"/>')
            bar_width = max(1.0, (right - left) / max(1, len(points)) * 0.72)
            for i, value in enumerate(storage_values):
                y_value = y_storage(value)
                y_rect = min(zero_y, y_value)
                h_rect = max(1.0, abs(y_value - zero_y))
                color = "#806c88" if value >= 0 else "#b07a7a"
                svg.append(f'<rect x="{x_at(i) - bar_width / 2:.1f}" y="{y_rect:.1f}" width="{bar_width:.1f}" height="{h_rect:.1f}" fill="{color}" fill-opacity="0.42"/>')
            legend = [
                ("Net Load (MW)", "#385b6a"), ("Wind Input (MW)", "#6f9aa8"),
                ("PV Input (MW)", "#c5a56a"), ("Storage Discharge (MW)", "#806c88"),
                ("Storage Charge (MW)", "#b07a7a"),
            ]
            for index, (label, color) in enumerate(legend):
                x = left + (index % 3) * 270
                y = 58 + (index // 3) * 20
                svg.append(f'<rect x="{x}" y="{y - 9}" width="12" height="12" fill="{color}"/>')
                svg.append(f'<text x="{x + 18}" y="{y}" fill="{color}" font-size="13">{legend_name(label)}</text>')
            target.write_text("".join(svg) + '</svg>', encoding="utf-8")

        generation_series = [
            ("Base Generation (MW)", "#b7d9b1", [p.get("base_generation_mw", 0) for p in points]),
            ("Conventional Generation (MW)", "#f5c99d", [p.get("conventional_generation_mw", 0) for p in points]),
            ("Wind Generation (MW)", "#8eb9c8", [p.get("wind_generation_mw", 0) for p in points]),
            ("PV Generation (MW)", "#e0bd72", [p.get("pv_generation_mw", 0) for p in points]),
            ("Storage Discharge (MW)", "#a895b8", [p.get("storage_discharge_mw", 0) for p in points]),
            ("Grid Import (MW)", "#b58f86", [p.get("grid_import_mw", 0) for p in points]),
        ]
        make_stacked_generation_chart(path.parent / "generation_24h.svg", generation_series)
        make_supply_storage_chart(path.parent / "supply_storage_24h.svg")
        # Keep the dashboard filename as a compatibility alias.
        (path.parent / "grid_power.svg").write_text(
            (path.parent / "generation_24h.svg").read_text(encoding="utf-8"), encoding="utf-8"
        )

        task_summary = lambda key: [p.get("task_summary", {}).get(key, 0) for p in points]
        make_chart(path.parent / "task_statistics.svg", [
            ("Submitted", "#1f77b4", task_summary("submitted")),
            ("Scheduling", "#ff7f0e", task_summary("scheduling")),
            ("Completed", "#2ca02c", task_summary("completed")),
            ("Running", "#d62728", task_summary("running")),
        ], "Task Count")
        make_chart(path.parent / "task_duration.svg", [
            ("Planned Average (s)", "#9467bd", task_summary("avg_planned_duration_seconds")),
            ("Actual Average (s)", "#8c564b", task_summary("avg_actual_execution_seconds")),
        ], "Duration (s)")
        make_chart(path.parent / "compute_power.svg", [
            ("Compute Mapping Power (MW)", "#17becf", [p.get("compute_power_mw", 0) for p in points])
        ], "Power (MW)")
        make_chart(path.parent / "task_power_24h.svg", [
            ("Planned Task Power (MW)", "#6f9aa8", [p.get("planned_task_power_mw", 0) for p in points]),
            ("Actual Task Power (MW)", "#a7836d", [p.get("actual_task_power_mw", 0) for p in points]),
        ], "Power (MW)")

        # Storage power uses the dispatch convention: positive is discharge and
        # negative is charge.  The SOC trace shares the same time axis below it.
        storage_power = [clean(p.get("storage_power_mw", 0.0)) for p in points]
        storage_power_samples = aggregate_storage_power_samples(points, bucket_seconds=300.0)
        storage_soc = [clean(p.get("storage_soc_pct", 0.0)) for p in points]
        width, height = 1000, 760
        left, right = 90, 950
        panels = ((55, 340), (430, 680))
        max_power = max([abs(v) for v in storage_power] or [1.0]) or 1.0
        max_soc = max([100.0] + storage_soc)
        svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">', '<rect width="100%" height="100%" fill="white"/>']
        power_top, power_bottom = panels[0]
        zero_y = power_bottom - max_power / (2 * max_power) * (power_bottom - power_top)
        horizon_seconds = max(24 * 3600, max((clean(p.get("second", 0)) for p in points), default=0.0))
        svg.extend([
            f'<line x1="{left}" y1="{power_top}" x2="{left}" y2="{power_bottom}" stroke="#555"/>',
            f'<line x1="{left}" y1="{zero_y:.1f}" x2="{right}" y2="{zero_y:.1f}" stroke="#555"/>',
            f'<text x="18" y="{(power_top + power_bottom) / 2:.0f}" font-size="15" text-anchor="middle" transform="rotate(-90 18 {(power_top + power_bottom) / 2:.0f})">Storage Power (MW)</text>',
        ])
        bar_width = max(1.0, (right - left) / 24.0 * 0.18)
        for second, value in storage_power_samples:
            x = left + min(horizon_seconds, max(0.0, second)) / horizon_seconds * (right - left)
            y_value = zero_y - value / (2 * max_power) * (power_bottom - power_top)
            y_rect = min(zero_y, y_value)
            h_rect = max(1.0, abs(y_value - zero_y))
            color = "#2ca02c" if value >= 0 else "#1f77b4"
            svg.append(f'<rect x="{x - bar_width / 2:.1f}" y="{y_rect:.1f}" width="{bar_width:.1f}" height="{h_rect:.1f}" fill="{color}" fill-opacity="0.45"/>')
        svg.extend([
            f'<text x="{left + 15}" y="{power_top + 20}" fill="#2ca02c" font-size="14">Discharge Power</text>',
            f'<text x="{left + 190}" y="{power_top + 20}" fill="#1f77b4" font-size="14">Charge Power</text>',
        ])
        soc_top, soc_bottom = panels[1]
        svg.extend([
            f'<line x1="{left}" y1="{soc_top}" x2="{left}" y2="{soc_bottom}" stroke="#555"/>',
            f'<line x1="{left}" y1="{soc_bottom}" x2="{right}" y2="{soc_bottom}" stroke="#555"/>',
            f'<text x="18" y="{(soc_top + soc_bottom) / 2:.0f}" font-size="15" text-anchor="middle" transform="rotate(-90 18 {(soc_top + soc_bottom) / 2:.0f})">State of Charge (%)</text>',
            f'<text x="{(left + right) / 2:.0f}" y="735" font-size="15" text-anchor="middle">Time (h)</text>',
        ])
        for hour in (0, 6, 12, 18, 24):
            x_tick = left + hour / 24 * (right - left)
            svg.append(f'<text x="{x_tick:.1f}" y="700" font-size="12" text-anchor="middle">{hour}</text>')
        soc_points = []
        for i, value in enumerate(storage_soc):
            second = clean(points[i].get("second", i)) if i < len(points) else i
            x = left + min(horizon_seconds, max(0.0, second)) / horizon_seconds * (right - left)
            y_value = soc_bottom - value / max_soc * (soc_bottom - soc_top)
            soc_points.append((x, y_value))
        svg.append(f'<polyline fill="none" stroke="#9467bd" stroke-width="3" points="{" ".join(f"{x:.1f},{yy:.1f}" for x,yy in soc_points)}"/>')
        svg.append(f'<text x="{left + 15}" y="{soc_top + 20}" fill="#9467bd" font-size="14">SOC</text>')
        (path.parent / "storage_dispatch.svg").write_text("".join(svg) + '</svg>', encoding="utf-8")
        path.write_text((path.parent / "grid_power.svg").read_text(encoding="utf-8"), encoding="utf-8")

    def get_stats_at(self, second: int = None) -> Dict:
        """Implementation documentation."""
        if second is None:
            second = self.current_second
        return self.stats_records.get(second, {})

    def get_latest_stats(self) -> Dict:
        """Implementation documentation."""
        latest = self.get_stats_at()
        if latest:
            return latest
        # The simulation clock often ends between two statistics samples.
        # Keep the final sampled result visible instead of returning an empty
        # object that makes the dashboard appear to reset to zero.
        if self.stats_records:
            return self.stats_records[max(self.stats_records)]
        return {}

    def get_stats_history(self) -> List[Dict]:
        """Return every sampled time point in chronological order for exports."""
        return [self.stats_records[second] for second in sorted(self.stats_records)]

    def get_hourly_stats(self, hour: int = None) -> Dict:
        """Implementation documentation."""
        if hour is None:
            hour = self.current_second // 3600
        # Implementation note.
        target_second = hour * 3600
        candidates = [s for s in self.stats_records.keys() if s <= target_second]
        if candidates:
            return self.stats_records[max(candidates)]
        return {}

    def get_total_carbon_summary(self) -> Dict:
        """Implementation documentation."""
        total_energy = sum(sim.total_energy_mwh for sim in self.dc_sims.values())
        total_emission = sum(sim.total_emission_kg for sim in self.dc_sims.values())
        return {
            'total_energy_mwh': total_energy,
            'total_emission_kg': total_emission,
            'total_emission_ton': total_emission / 1000.0,
            'per_dc': {
                dc_id: {
                    'name': sim.dc.name,
                    'energy_mwh': sim.total_energy_mwh,
                    'emission_kg': sim.total_emission_kg,
                }
                for dc_id, sim in self.dc_sims.items()
            }
        }

    def stop(self):
        """Implementation documentation."""
        self.running = False
        # Rebuild the visual reports from the complete sampled history when a
        # run ends, so the saved 24-hour charts include the final sample even
        # when it did not coincide with the regular report interval.
        if self.stats_records:
            report_dir = Path(getattr(self, "report_dir", Path("reports")))
            report_dir.mkdir(parents=True, exist_ok=True)
            self._write_svg_report(report_dir / "simulation_dashboard.svg")
        print(format_total_cost_log(getattr(self, "total_operating_cost_yuan", 0.0)))
        print("Simulation engine stopped")
