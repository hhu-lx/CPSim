# src/infrastructure/continuation_power_flow.py
import pandapower as pp
import numpy as np
from typing import Dict, List, Tuple


def run_continuation_power_flow(
    network,
    step: float = 0.05,
    max_steps: int = 100,
    min_v_limit: float = 0.7
) -> Dict:
    """Implementation documentation."""
    original_load_p = network.load['p_mw'].copy()
    original_load_q = network.load['q_mvar'].copy()
    pv_curve: List[Tuple[float, float]] = []
    lambda_critical = 1.0
    min_v_at_critical = 0.0
    power_flow_converged = False
    critical_point_found = False
    stop_reason = "initial_power_flow_failure"
    search_limit_reached = False
    failure_lambda = None

    # Implementation note.
    # Implementation note.
    slack_buses = set(network.ext_grid.loc[network.ext_grid.in_service, "bus"].tolist()) if len(network.ext_grid) else set()
    monitored_buses = [bus for bus in network.bus.index if bus not in slack_buses]

    try:
        try:
            pp.runpp(network, numba=False, enforce_q_lims=True)
            power_flow_converged = True
        except Exception as exc:
            print(f"CPF initial power flow failed: {exc}")
            return {
                "lambda_critical": 1.0,
                "voltage_margin_pct": 0.0,
                "min_voltage_at_critical": 0.0,
                "pv_curve": [],
                "converged": False,
                "critical_point_found": False,
                "search_limit_reached": False,
                "stop_reason": "initial_power_flow_failure",
            }

        initial_vm = network.res_bus.loc[monitored_buses, "vm_pu"] if monitored_buses else network.res_bus["vm_pu"]
        min_v_at_critical = float(initial_vm.min())
        pv_curve.append((1.0, min_v_at_critical))
        stop_reason = "search_limit_reached"

        for i in range(max_steps):
            lam = 1.0 + step * (i + 1)
            network.load["p_mw"] = original_load_p * lam
            network.load["q_mvar"] = original_load_q * lam
            try:
                pp.runpp(network, numba=False, max_iteration=30, enforce_q_lims=True)
                min_vm = network.res_bus.loc[monitored_buses, "vm_pu"] if monitored_buses else network.res_bus["vm_pu"]
                min_v = float(min_vm.min())
                pv_curve.append((lam, min_v))
                lambda_critical = lam
                min_v_at_critical = min_v
                if min_v < min_v_limit:
                    critical_point_found = True
                    stop_reason = "voltage_limit_reached"
                    break
            except Exception as exc:
                # Implementation note.
                critical_point_found = True
                stop_reason = "power_flow_failure"
                failure_lambda = lam
                print(f"CPF load multiplier {lam:.2f} power-flow failed: {exc}")
                break
        else:
            search_limit_reached = True
    finally:
        network.load["p_mw"] = original_load_p
        network.load["q_mvar"] = original_load_q
        try:
            pp.runpp(network, numba=False, enforce_q_lims=True)
        except Exception:
            pass

    return {
        "lambda_critical": lambda_critical,
        "voltage_margin_pct": (lambda_critical - 1.0) * 100,
        "min_voltage_at_critical": min_v_at_critical,
        "pv_curve": pv_curve,
        "converged": power_flow_converged,
        "critical_point_found": critical_point_found,
        "search_limit_reached": search_limit_reached,
        "stop_reason": stop_reason,
        "failure_lambda": failure_lambda,
    }
