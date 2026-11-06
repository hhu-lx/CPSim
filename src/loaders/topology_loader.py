"""Load a neutral JSON topology and build a Pandapower network."""

import json
import math
from pathlib import Path
from typing import Any

import pandapower as pp

from src.config.models import GeneratorDefaults


_GENERATOR_PARAMETERS = (
    "ramp_up_mw_per_hour",
    "ramp_down_mw_per_hour",
    "cost_linear_yuan_per_mwh",
    "cost_quadratic_yuan_per_mw2h",
    "emission_tco2_per_mwh",
)


def _finite_number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"generator.{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"generator.{field} must be a finite number")
    return number


def normalise_generator_parameters(
    generator: dict, defaults: GeneratorDefaults
) -> dict:
    """Return a generator with finite optimization coefficients and sources."""
    if not isinstance(generator, dict):
        raise ValueError("generator must be an object")
    normalized = dict(generator)
    previous_sources = generator.get("parameter_sources", {})
    if not isinstance(previous_sources, dict):
        raise ValueError("generator.parameter_sources must be an object")

    minimum = _finite_number(generator.get("min_p_mw", 0.0), "min_p_mw")
    maximum = _finite_number(
        generator.get("max_p_mw", generator.get("p_mw", 0.0)), "max_p_mw"
    )
    if minimum > maximum:
        raise ValueError("generator.min_p_mw must not exceed max_p_mw")
    normalized["min_p_mw"] = minimum
    normalized["max_p_mw"] = maximum
    if "p_mw" in generator:
        normalized["p_mw"] = _finite_number(generator["p_mw"], "p_mw")

    requested_type = str(generator.get("generator_type", "generic_thermal")).strip().lower()
    profile_name = requested_type if requested_type in defaults.profiles else "generic_thermal"
    profile = defaults.profiles[profile_name]
    sources: dict[str, str] = {}
    ramp_default = defaults.ramp_fraction_per_hour * maximum
    fallback_values = {
        "ramp_up_mw_per_hour": ramp_default,
        "ramp_down_mw_per_hour": ramp_default,
        **profile,
    }
    for field in _GENERATOR_PARAMETERS:
        if field in generator:
            value = _finite_number(generator[field], field)
            source = str(previous_sources.get(field, "user"))
        else:
            value = float(fallback_values[field])
            source = (
                "default:0.125*max_p_mw"
                if field.startswith("ramp_")
                else f"default:{profile_name}"
            )
        if value < 0:
            raise ValueError(f"generator.{field} must be non-negative")
        normalized[field] = value
        sources[field] = source
    normalized["generator_type"] = requested_type
    normalized["parameter_sources"] = sources
    return normalized


def load_topology(path: str | Path) -> dict:
    topology_path = Path(path)
    if not topology_path.exists():
        raise FileNotFoundError(f"topology file does not exist: {topology_path}")
    with topology_path.open("r", encoding="utf-8") as stream:
        topology = json.load(stream)
    validate_topology(topology)
    return topology


def _require_list(topology: dict, key: str) -> list:
    value = topology.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{key} must be an array")
    return value


def _check_bus_ref(item: dict, key: str, bus_ids: set, index: int, field: str) -> None:
    if item.get(key) not in bus_ids:
        raise ValueError(f"{field}[{index}].{key} references unknown bus {item.get(key)}")


def validate_topology(topology: dict) -> None:
    if not isinstance(topology, dict):
        raise ValueError("topology must be an object")
    buses = _require_list(topology, "buses")
    if not buses:
        raise ValueError("buses must not be empty")
    bus_ids = set()
    for index, bus in enumerate(buses):
        if not isinstance(bus, dict):
            raise ValueError(f"buses[{index}] must be an object")
        bus_id = bus.get("id")
        if bus_id in bus_ids:
            raise ValueError(f"buses[{index}].id must be unique")
        if not isinstance(bus_id, int):
            raise ValueError(f"buses[{index}].id must be an integer")
        if float(bus.get("vn_kv", 0)) <= 0:
            raise ValueError(f"buses[{index}].vn_kv must be positive")
        bus_ids.add(bus_id)

    for index, line in enumerate(_require_list(topology, "lines")):
        if not isinstance(line, dict):
            raise ValueError(f"lines[{index}] must be an object")
        _check_bus_ref(line, "from_bus", bus_ids, index, "lines")
        _check_bus_ref(line, "to_bus", bus_ids, index, "lines")
    for index, transformer in enumerate(_require_list(topology, "transformers")):
        if not isinstance(transformer, dict):
            raise ValueError(f"transformers[{index}] must be an object")
        _check_bus_ref(transformer, "hv_bus", bus_ids, index, "transformers")
        _check_bus_ref(transformer, "lv_bus", bus_ids, index, "transformers")
    for collection in ("loads", "generators", "renewables", "storages"):
        for index, item in enumerate(_require_list(topology, collection)):
            if not isinstance(item, dict):
                raise ValueError(f"{collection}[{index}] must be an object")
            _check_bus_ref(item, "bus_id", bus_ids, index, collection)
            if collection == "generators":
                topology[collection][index] = normalise_generator_parameters(
                    item, GeneratorDefaults()
                )
    for index, item in enumerate(_require_list(topology, "external_grids")):
        if not isinstance(item, dict):
            raise ValueError(f"external_grids[{index}] must be an object")
        _check_bus_ref(item, "bus_id", bus_ids, index, "external_grids")

    mapping = topology.get("resource_mapping", {})
    if not isinstance(mapping, dict):
        raise ValueError("resource_mapping must be an object")
    for field in ("pv_bus", "wind_bus"):
        if field in mapping and mapping[field] not in bus_ids:
            raise ValueError(f"resource_mapping.{field} references unknown bus {mapping[field]}")
    if "load_buses" in mapping:
        load_buses = mapping["load_buses"]
        if not isinstance(load_buses, list):
            raise ValueError("resource_mapping.load_buses must be an array")
        for index, bus_id in enumerate(load_buses):
            if bus_id not in bus_ids:
                raise ValueError(
                    f"resource_mapping.load_buses[{index}] references unknown bus {bus_id}"
                )


def topology_grid_overrides(topology: dict) -> dict[str, Any]:
    """Return grid bus settings declared by an uploaded topology."""
    validate_topology(topology)
    mapping = topology.get("resource_mapping", {})
    overrides = {
        key: value
        for key, value in mapping.items()
        if key in {"pv_bus", "wind_bus", "load_buses"}
    }
    overrides["base_mva"] = float(topology.get("base_mva", 100.0))
    return overrides


def validate_grid_bus_references(topology: dict, grid: dict) -> None:
    """Validate runtime grid bus settings against an effective topology."""
    validate_topology(topology)
    if not isinstance(grid, dict):
        raise ValueError("grid must be an object")
    bus_ids = {bus["id"] for bus in topology["buses"]}
    for field in ("pv_bus", "wind_bus"):
        if field in grid and grid[field] not in bus_ids:
            raise ValueError(f"grid.{field} references unknown bus {grid[field]}")
    for index, bus_id in enumerate(grid.get("load_buses", [])):
        if bus_id not in bus_ids:
            raise ValueError(f"grid.load_buses[{index}] references unknown bus {bus_id}")


def topology_summary(topology: dict) -> dict[str, Any]:
    """Return safe, compact topology metadata for the dashboard."""
    validate_topology(topology)
    return {
        "name": str(topology.get("name", "Unnamed topology")),
        "base_mva": float(topology.get("base_mva", 100.0)),
        "bus_count": len(topology.get("buses", [])),
        "line_count": len(topology.get("lines", [])),
        "transformer_count": len(topology.get("transformers", [])),
    }


def build_pandapower_network(topology: dict):
    validate_topology(topology)
    net = pp.create_empty_network(sn_mva=float(topology.get("base_mva", 100.0)))
    bus_map: dict[int, int] = {}
    for bus in topology["buses"]:
        bus_map[bus["id"]] = pp.create_bus(
            net,
            vn_kv=float(bus["vn_kv"]),
            name=bus.get("name", f"Bus {bus['id']}"),
            type=str(bus.get("type", "b")).lower()[0],
        )

    for line in topology.get("lines", []):
        pp.create_line_from_parameters(
            net,
            bus_map[line["from_bus"]],
            bus_map[line["to_bus"]],
            length_km=float(line.get("length_km", 1.0)),
            r_ohm_per_km=float(line.get("r_ohm_per_km", 0.01)),
            x_ohm_per_km=float(line.get("x_ohm_per_km", 0.1)),
            c_nf_per_km=float(line.get("c_nf_per_km", 10.0)),
            max_i_ka=float(line.get("max_i_ka", 1.0)),
            name=line.get("name"),
        )

    for transformer in topology.get("transformers", []):
        pp.create_transformer_from_parameters(
            net,
            hv_bus=bus_map[transformer["hv_bus"]],
            lv_bus=bus_map[transformer["lv_bus"]],
            sn_mva=float(transformer.get("sn_mva", 100.0)),
            vn_hv_kv=float(transformer["vn_hv_kv"]),
            vn_lv_kv=float(transformer["vn_lv_kv"]),
            vk_percent=float(transformer.get("vk_percent", 10.0)),
            vkr_percent=float(transformer.get("vkr_percent", 0.3)),
            pfe_kw=float(transformer.get("pfe_kw", 0.0)),
            i0_percent=float(transformer.get("i0_percent", 0.0)),
            name=transformer.get("name"),
        )

    for load in topology.get("loads", []):
        pp.create_load(
            net,
            bus_map[load["bus_id"]],
            p_mw=float(load.get("p_mw", 0.0)),
            q_mvar=float(load.get("q_mvar", 0.0)),
            name=load.get("name"),
        )

    for grid in topology.get("external_grids", []):
        pp.create_ext_grid(
            net,
            bus_map[grid["bus_id"]],
            vm_pu=float(grid.get("vm_pu", 1.0)),
            va_degree=float(grid.get("va_degree", 0.0)),
            name=grid.get("name"),
        )

    for generator in topology.get("generators", []):
        pp.create_gen(
            net,
            bus_map[generator["bus_id"]],
            p_mw=float(generator.get("p_mw", 0.0)),
            vm_pu=float(generator.get("vm_pu", 1.0)),
            min_p_mw=float(generator.get("min_p_mw", 0.0)),
            max_p_mw=float(generator.get("max_p_mw", generator.get("p_mw", 0.0))),
            min_q_mvar=float(generator.get("min_q_mvar", -100.0)),
            max_q_mvar=float(generator.get("max_q_mvar", 100.0)),
            name=generator.get("name"),
        )

    for renewable in topology.get("renewables", []):
        pp.create_sgen(
            net,
            bus_map[renewable["bus_id"]],
            p_mw=float(renewable.get("p_mw", 0.0)),
            q_mvar=float(renewable.get("q_mvar", 0.0)),
            name=renewable.get("name", renewable.get("type", "renewable")),
        )
    # Storage is retained as topology metadata and is instantiated by the
    # simulation layer, where SOC and dynamic charge/discharge are managed.
    return net, bus_map
