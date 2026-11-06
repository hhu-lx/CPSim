# src/loaders/config_loader.py

import yaml
from pathlib import Path
from typing import Dict, Optional, List


def get_project_root() -> Path:
    """Implementation documentation."""
    return Path(__file__).resolve().parent.parent.parent


def resolve_path(path_str: Optional[str]) -> Optional[str]:
    if not path_str or path_str == '':
        return None

    p = Path(path_str)
    if p.is_absolute():
        return str(p)

    project_root = get_project_root()
    abs_path = project_root / p
    return str(abs_path)


def load_platform_config(config_file: str = None) -> Dict:
    """Implementation documentation."""
    if config_file is None:
        config_file = get_project_root() / "config" / "platform.yaml"
    else:
        config_file = Path(config_file)
        if not config_file.is_absolute():
            config_file = get_project_root() / config_file

    if not config_file.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_file}")

    with open(config_file, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    print(f"Loading configuration file: {config_file}")
    return config


def get_grid_config(config: Dict) -> Dict:
    """Implementation documentation."""
    grid = config.get('grid', {}).copy()
    for key in ['load_file', 'pv_file', 'wind_file', 'base_generation_file', 'topology_file']:
        if key in grid and grid[key]:
            grid[key] = resolve_path(grid[key])
    return grid


def get_simulation_config(config: Dict) -> Dict:
    """Implementation documentation."""
    return config.get('simulation', {})


def build_data_centers(config: Dict) -> List:
    """Implementation documentation."""
    from src.domain.datacenter import DataCenter
    from src.domain.device import DeviceSpec
    from src.data.reference_tables import load_equipment_power_curves, load_equipment_power_traces

    data_centers = []
    dc_configs = config.get('data_centers', [])
    reference_config = config.get('reference_tables', {})
    curve_file = reference_config.get('equipment_power_curves')
    if curve_file:
        curve_file = Path(resolve_path(curve_file))
    else:
        curve_file = get_project_root() / 'config' / 'reference' / 'equipment_power_curves.csv'
    curves = load_equipment_power_curves(curve_file) if curve_file.exists() else {}
    trace_file_value = reference_config.get('equipment_power_timeseries')
    trace_file = Path(resolve_path(trace_file_value)) if trace_file_value else get_project_root() / 'config' / 'reference' / 'equipment_power_timeseries.csv'
    traces = load_equipment_power_traces(trace_file) if trace_file.exists() else {}

    for dc_conf in dc_configs:
        devices = []
        for device_conf in dc_conf.get('device_pool', []):
            device_data = dict(device_conf)
            device_data.setdefault('power_curve', curves.get(device_data.get('model'), []))
            if device_data.get('model') in traces:
                period, power_trace = traces[device_data['model']]
                device_data.setdefault('power_trace', power_trace)
                device_data.setdefault('period_seconds', period)
            devices.append(DeviceSpec(**device_data))
        dc = DataCenter(
            id=dc_conf['id'],
            name=dc_conf['name'],
            region=dc_conf['region'],
            grid_bus_id=dc_conf['grid_bus_id'],
            total_compute=dc_conf['total_compute'],
            total_bandwidth=dc_conf.get('total_bandwidth', 1000.0),
            power_per_tflops=dc_conf.get('power_per_tflops', 0.5),
            power_scale=dc_conf.get('power_scale', 1.0),
            profit_margin=dc_conf.get('profit_margin', 1.3),
            pue=dc_conf.get('pue', 1.0),
            device_pool=devices,
        )
        data_centers.append(dc)
        print(f"  {dc.name} ({dc.id}) : bus {dc.grid_bus_id}, "
              f"compute {dc.total_compute} TFLOPS, bandwidth {dc.total_bandwidth} Mbps")

    return data_centers


def build_storages(config: Dict) -> List:
    from src.domain.storage import EnergyStorage

    return [EnergyStorage(**storage_config) for storage_config in config.get("storages", [])]
