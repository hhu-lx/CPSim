"""One-model compute/power coordination for the global optimization mode."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Callable

import coptpy as cp

from src.optimization.power_dispatch import (
    PowerDispatchProblem,
    PowerDispatchResult,
    electricity_price_factors,
)


@dataclass(frozen=True)
class JointCoordinationResult:
    status: str
    objective_value: float | None
    assignments: dict[str, str]
    start_periods: dict[str, int]
    queued_tasks: list[str]
    generator_mw: dict[str, list[float]]
    renewable_dispatched_mw: dict[str, list[float]]
    renewable_curtailed_mw: dict[str, list[float]]
    storage_power_mw: dict[str, list[float]]
    storage_soc_mwh: dict[str, list[float]]
    objective_components: dict[str, dict[str, float]]
    strictly_feasible: bool
    forecast_fill: str
    power_dispatch: PowerDispatchResult | None = None
    external_grid_mw: list[float] | None = None


def _expand(values: list[float], horizon: int) -> tuple[list[float], bool]:
    if not values:
        raise ValueError("joint power forecast must not be empty")
    converted = [float(value) for value in values]
    if any(not math.isfinite(value) or value < 0 for value in converted):
        raise ValueError("joint power forecast values must be finite and non-negative")
    if len(converted) >= horizon:
        return converted[:horizon], False
    return [converted[index % len(converted)] for index in range(horizon)], True


class JointCoordinationOptimizer:
    """Solve task placement/timing and all electrical controls in one COPT model."""

    def __init__(
        self,
        *,
        ac_validator: Callable | None = None,
        result_applier: Callable | None = None,
        feasible_restorer: Callable | None = None,
    ):
        self.ac_validator = ac_validator
        self.result_applier = result_applier
        self.feasible_restorer = feasible_restorer

    def solve(
        self,
        tasks,
        data_centers,
        power_problem: PowerDispatchProblem,
        *,
        objective,
        weights,
        latency_ms,
        carbon_intensity,
        load_balance_weight: float = 0.0,
        time_limit_seconds: float | None = None,
        relative_mip_gap: float | None = None,
        minimum_active_center_tasks: int = 0,
        maintain_terminal_soc: bool = False,
    ) -> JointCoordinationResult:
        first = self._solve_model(
            tasks, data_centers, power_problem, objective, weights,
            latency_ms, carbon_intensity, renewable_tightening=1.0,
            load_balance_weight=load_balance_weight,
            time_limit_seconds=time_limit_seconds,
            relative_mip_gap=relative_mip_gap,
            minimum_active_center_tasks=minimum_active_center_tasks,
            maintain_terminal_soc=maintain_terminal_soc,
        )
        if not first.strictly_feasible or self.ac_validator is None:
            if first.strictly_feasible and self.result_applier is not None:
                self.result_applier(first)
            return first
        if self.ac_validator(first, 1):
            if self.result_applier is not None:
                self.result_applier(first)
            return first

        second = self._solve_model(
            tasks, data_centers, power_problem, objective, weights,
            latency_ms, carbon_intensity, renewable_tightening=0.95,
            load_balance_weight=load_balance_weight,
            time_limit_seconds=time_limit_seconds,
            relative_mip_gap=relative_mip_gap,
            minimum_active_center_tasks=minimum_active_center_tasks,
            maintain_terminal_soc=maintain_terminal_soc,
        )
        if second.strictly_feasible and self.ac_validator(second, 2):
            if self.result_applier is not None:
                self.result_applier(second)
            return second
        if self.feasible_restorer is not None:
            self.feasible_restorer()
        failed = second if second.strictly_feasible else first
        return replace(failed, status="ac_validation_failed", strictly_feasible=False)

    def _solve_model(
        self, tasks, data_centers, problem, objective, weights,
        latency_ms, carbon_intensity, renewable_tightening, load_balance_weight=0.0,
        time_limit_seconds=None, relative_mip_gap=None, minimum_active_center_tasks=0,
        maintain_terminal_soc=False,
    ) -> JointCoordinationResult:
        horizon = int(problem.horizon_hours)
        if horizon <= 0:
            raise ValueError("horizon_hours must be positive")
        periods = range(horizon)
        load, cyclic = _expand(problem.load_mw, horizon)
        if problem.fixed_generation_mw:
            fixed_generation, fixed_filled = _expand(
                problem.fixed_generation_mw, horizon
            )
            cyclic = cyclic or fixed_filled
        else:
            fixed_generation = [0.0] * horizon
        cyclic = cyclic or problem.forecast_fill == "cyclic"
        price_factors, price_filled = electricity_price_factors(
            problem.electricity_prices, horizon
        )
        cyclic = cyclic or price_filled
        renewable_available = {}
        for renewable in problem.renewables:
            available, filled = _expand(renewable.available_mw, horizon)
            renewable_available[renewable.id] = [value * renewable_tightening for value in available]
            cyclic = cyclic or filled
        generator_reference = {}
        for generator in problem.generators:
            if generator.reference_mw:
                values, filled = _expand(generator.reference_mw, horizon)
                generator_reference[generator.id] = values
                cyclic = cyclic or filled

        env = cp.Envr()
        model = env.createModel("joint_compute_power_coordination")
        model.setParam(cp.COPT.Param.LogToConsole, 0)
        bounded_solve = time_limit_seconds is not None and float(time_limit_seconds) > 0
        if bounded_solve:
            model.setParam(cp.COPT.Param.TimeLimit, float(time_limit_seconds))
        if relative_mip_gap is not None:
            model.setParam(cp.COPT.Param.RelGap, float(relative_mip_gap))

        placement = {}
        queued = {}
        task_periods = {}
        for task in tasks:
            queued[task.id] = model.addVar(vtype=cp.COPT.BINARY, name=f"queued_{task.id}")
            duration = max(1, int(math.ceil(float(task.duration_hours) / problem.step_hours)))
            earliest = max(0, int(math.ceil(
                (float(task.submitted_at) - float(problem.start_second))
                / (3600.0 * problem.step_hours)
            )))
            latest_by_wait = int(math.floor(
                (float(task.deadline_second) - float(problem.start_second))
                / (3600.0 * problem.step_hours)
            ))
            starts = []
            for dc in data_centers:
                if task.user_specified_data_center and dc.id != task.user_specified_data_center:
                    continue
                if not dc.can_allocate(task):
                    continue
                forced_center = getattr(task, "_joint_planned_center", None)
                if getattr(task, "_joint_force_start_now", False) and forced_center and dc.id != forced_center:
                    continue
                candidate_starts = (
                    [0] if getattr(task, "_joint_force_start_now", False)
                    else range(earliest, min(latest_by_wait, horizon - duration) + 1)
                )
                for start in candidate_starts:
                    if start < earliest or start > min(latest_by_wait, horizon - duration):
                        continue
                    key = (task.id, dc.id, start)
                    placement[key] = model.addVar(vtype=cp.COPT.BINARY, name=f"place_{task.id}_{dc.id}_{start}")
                    starts.append(key)
            task_periods[task.id] = duration
            model.addConstr(cp.quicksum(placement[key] for key in starts) + queued[task.id] == 1)

        # Protect against an otherwise economically optimal single-center
        # placement.  Only centers with an immediate-start placement option
        # are covered, so user-fixed, resource-ineligible and future-only
        # placements cannot make the current batch infeasible.
        activity_floor = max(0, int(minimum_active_center_tasks or 0))
        immediate_task_ids = {
            task_id for task_id, _dc_id, start in placement
            if start == 0
        }
        immediate_centers = [
            dc for dc in data_centers
            if any((task_id, dc.id, 0) in placement for task_id in immediate_task_ids)
        ]
        if activity_floor and len(immediate_task_ids) >= len(immediate_centers) * activity_floor:
            for dc in immediate_centers:
                model.addConstr(cp.quicksum(
                    placement[task_id, dc.id, 0]
                    for task_id in immediate_task_ids
                    if (task_id, dc.id, 0) in placement
                ) >= activity_floor)

        def active_expression(task, dc, period):
            duration = task_periods[task.id]
            return cp.quicksum(
                variable for (task_id, dc_id, start), variable in placement.items()
                if task_id == task.id and dc_id == dc.id and start <= period < start + duration
            )

        # Hard coverage rule for the current workload.  The soft utilization
        # range below can still choose a primary center, but it may no longer
        # leave another eligible center idle whenever there are enough
        # concurrent tasks to cover all eligible centers.  Eligibility is
        # evaluated per period so fixed-center tasks, future-only tasks, and
        # resource-ineligible centers do not make the model infeasible.
        coverage_floor = max(1, int(minimum_active_center_tasks or 1))
        task_count_big_m = max(1, len(tasks) * coverage_floor)
        for period in periods:
            eligible_centers = [
                dc for dc in data_centers
                if any(
                    task_id == task.id and dc_id == dc.id
                    and start <= period < start + task_periods[task.id]
                    for (task_id, dc_id, start) in placement
                    for task in tasks
                )
            ]
            if len(eligible_centers) < 2:
                continue
            active_by_center = {
                dc.id: cp.quicksum(
                    active_expression(task, dc, period) for task in tasks
                )
                for dc in eligible_centers
            }
            active_total = cp.quicksum(active_by_center.values())
            enough_coverage = model.addVar(
                vtype=cp.COPT.BINARY, name=f"enough_center_coverage_{period}"
            )
            required_total = len(eligible_centers) * coverage_floor
            model.addConstr(active_total >= required_total * enough_coverage)
            model.addConstr(
                active_total <= (required_total - 1)
                + task_count_big_m * enough_coverage
            )
            for dc in eligible_centers:
                model.addConstr(active_by_center[dc.id] >= coverage_floor * enough_coverage)

        for dc in data_centers:
            compute_devices = [device for device in dc.device_pool if dc._is_compute_device(device)]
            gpu_capacity = (
                sum(max(0, device.quantity - dc.used_device_count(device.model)) for device in compute_devices)
                if compute_devices else max(1, int(dc.total_compute // dc._VIRTUAL_GPU_TFLOPS))
            )
            for period in periods:
                model.addConstr(cp.quicksum(
                    float(task.required_compute) * active_expression(task, dc, period)
                    for task in tasks
                ) <= float(dc.available_compute))
                model.addConstr(cp.quicksum(
                    float(task.required_bandwidth_mbps or task.compute_bandwidth_requirement())
                    * active_expression(task, dc, period)
                    for task in tasks
                ) <= float(dc.available_bandwidth))
                model.addConstr(cp.quicksum(
                    float(task.required_gpu_count or 0) * active_expression(task, dc, period)
                    for task in tasks
                ) <= float(gpu_capacity))

        generator_vars = {
            (item.id, t): model.addVar(lb=item.min_mw, ub=item.max_mw, name=f"gen_{item.id}_{t}")
            for item in problem.generators for t in periods
        }
        renewable_vars = {
            (item.id, t): model.addVar(lb=0, ub=renewable_available[item.id][t], name=f"ren_{item.id}_{t}")
            for item in problem.renewables for t in periods
        }
        charge = {
            (item.id, t): model.addVar(lb=0, ub=item.max_power_mw, name=f"charge_{item.id}_{t}")
            for item in problem.storages for t in periods
        }
        discharge = {
            (item.id, t): model.addVar(lb=0, ub=item.max_power_mw, name=f"discharge_{item.id}_{t}")
            for item in problem.storages for t in periods
        }
        charging = {
            (item.id, t): model.addVar(vtype=cp.COPT.BINARY, name=f"charging_{item.id}_{t}")
            for item in problem.storages for t in periods
        }
        soc = {
            (item.id, t): model.addVar(lb=item.min_soc_mwh, ub=item.max_soc_mwh, name=f"soc_{item.id}_{t}")
            for item in problem.storages for t in range(horizon + 1)
        }
        grid_import = {
            t: model.addVar(
                lb=0.0, ub=float(problem.external_grid_max_import_mw),
                name=f"grid_import_{t}",
            ) for t in periods
        }
        grid_export = {
            t: model.addVar(
                lb=0.0, ub=float(problem.external_grid_max_export_mw),
                name=f"grid_export_{t}",
            ) for t in periods
        }
        reference_deviation = {
            (item.id, t): model.addVar(
                lb=0.0, name=f"reference_deviation_{item.id}_{t}"
            )
            for item in problem.generators if item.id in generator_reference
            for t in periods
        }

        for item in problem.generators:
            previous = float(item.initial_mw)
            for t in periods:
                current = generator_vars[item.id, t]
                ramp_step = (
                    float(problem.control_interval_hours)
                    if t == 0 and problem.control_interval_hours is not None
                    else problem.step_hours
                )
                model.addConstr(current - previous <= item.ramp_up_mw_per_hour * ramp_step)
                model.addConstr(previous - current <= item.ramp_down_mw_per_hour * ramp_step)
                if item.id in generator_reference:
                    reference = generator_reference[item.id][t]
                    deviation = reference_deviation[item.id, t]
                    model.addConstr(deviation >= current - reference)
                    model.addConstr(deviation >= reference - current)
                previous = current
        for item in problem.storages:
            model.addConstr(soc[item.id, 0] == item.initial_soc_mwh)
            # Positive power means discharge. Limit changes to 20% of the
            # installed inverter power per optimization period.
            ramp_limit = 0.20 * float(item.max_power_mw)
            previous_power = float(item.initial_power_mw)
            for t in periods:
                model.addConstr(charge[item.id, t] <= item.max_power_mw * charging[item.id, t])
                model.addConstr(discharge[item.id, t] <= item.max_power_mw * (1 - charging[item.id, t]))
                current_power = discharge[item.id, t] - charge[item.id, t]
                effective_ramp_limit = ramp_limit
                if t == 0:
                    # A rolling solve may change the storage direction at the
                    # horizon boundary.  The old dispatch can be positive
                    # (discharging) while the new renewable-surplus signal
                    # requires charging, or vice versa.  Allow the first
                    # period to cross zero while still limiting the new
                    # command to one normal ramp step beyond zero; otherwise
                    # the direction binary (charge-only/discharge-only) and
                    # the ramp constraint can be infeasible together.
                    effective_ramp_limit = max(
                        ramp_limit,
                        abs(previous_power) + ramp_limit,
                    )
                model.addConstr(current_power - previous_power <= effective_ramp_limit)
                model.addConstr(previous_power - current_power <= effective_ramp_limit)
                model.addConstr(
                    soc[item.id, t + 1] == soc[item.id, t]
                    + charge[item.id, t] * item.charge_efficiency * problem.step_hours
                    - discharge[item.id, t] * problem.step_hours / item.discharge_efficiency
                )
                previous_power = current_power
            if maintain_terminal_soc:
                # Keep the rolling-horizon solution from emptying the battery
                # at the beginning of the day. The next solve can still use
                # the battery, but must plan a recharge within its horizon.
                # Anchor to the configured terminal/initial SOC, never to the
                # live SOC: tying the floor to the current (possibly full) SOC
                # locks a charged battery at its ceiling so it can only charge.
                terminal_target = (
                    item.terminal_soc_mwh
                    if item.terminal_soc_mwh is not None
                    else item.initial_soc_mwh
                )
                model.addConstr(
                    soc[item.id, horizon] >= min(
                        float(item.max_soc_mwh),
                        max(float(item.min_soc_mwh), float(terminal_target)),
                    )
                )
            # Minimum SOC is retained as reserve; the battery may discharge
            # before importing external power without a forced recharge.

        compute_load = {}
        for dc in data_centers:
            for t in periods:
                compute_load[dc.id, t] = cp.quicksum(
                    float(dc.power_for_task_mw(task)) * active_expression(task, dc, t)
                    for task in tasks
                )

        # Max-min fairness across the full rolling window.  The existing
        # per-period range penalty prevents extreme spikes, while this floor
        # prevents one eligible center from remaining nearly unused for the
        # whole solve when another center is cheaper or closer to green power.
        fairness_centers = [
            dc for dc in data_centers
            if any(dc.id == dc_id for (_task_id, dc_id, _start) in placement)
        ]
        fairness_floor = model.addVar(lb=0.0, ub=1.0, name="minimum_center_utilization")
        for dc in fairness_centers:
            total_compute = cp.quicksum(compute_load[dc.id, t] for t in periods)
            model.addConstr(
                fairness_floor <= total_compute / max(
                    1.0, float(dc.total_compute) * max(1, horizon)
                )
            )
        if not fairness_centers:
            model.addConstr(fairness_floor == 0.0)

        conventional = {}
        net_demand = {}
        deficit_mode = {
            t: model.addVar(vtype=cp.COPT.BINARY, name=f"storage_deficit_mode_{t}")
            for t in periods
        }
        green_surplus_mode = {
            t: model.addVar(vtype=cp.COPT.BINARY, name=f"green_surplus_mode_{t}")
            for t in periods
        }
        storage_power_limit = sum(float(item.max_power_mw) for item in problem.storages)
        balance_big_m = max(
            1.0,
            max(load or [0.0])
            + max(fixed_generation or [0.0])
            + sum(float(item.max_mw) for item in problem.generators)
            + sum(max(values or [0.0]) for values in renewable_available.values())
            + storage_power_limit,
        )


        raw_renewable_available = {}
        for renewable in problem.renewables:
            values, _ = _expand(renewable.available_mw, horizon)
            raw_renewable_available[renewable.id] = values

        renewable_surplus_mode = {
            t: model.addVar(
                vtype=cp.COPT.BINARY,
                name=f"renewable_surplus_mode_{t}",
            )
            for t in periods
        }

        for t in periods:
            # Implementation note.
            # Implementation note.
            # Implementation note.
            surplus_without_storage = (
                    sum(
                        raw_renewable_available[item.id][t]
                        for item in problem.renewables
                    )
                    + fixed_generation[t]
                    - load[t]
                    - cp.quicksum(
                compute_load[dc.id, t] for dc in data_centers
            )
            )

            # Implementation note.
            # Implementation note.
            # Implementation note.
            raw_renewable_available = {}
            for renewable in problem.renewables:
                values, _ = _expand(renewable.available_mw, horizon)
                raw_renewable_available[renewable.id] = values

            renewable_surplus_mode = {
                t: model.addVar(
                    vtype=cp.COPT.BINARY,
                    name=f"renewable_surplus_mode_{t}",
                )
                for t in periods
            }

            for t in periods:
                # Implementation note.
                # Implementation note.
                # Implementation note.
                surplus_without_storage = (
                        sum(
                            raw_renewable_available[item.id][t]
                            for item in problem.renewables
                        )
                        + fixed_generation[t]
                        - load[t]
                        - cp.quicksum(
                    compute_load[dc.id, t] for dc in data_centers
                )
                )

                # surplus_without_storage >= 0  => surplus_mode = 1
                # surplus_without_storage <  0  => surplus_mode = 0
                model.addConstr(
                    surplus_without_storage
                    >= -balance_big_m * (1 - renewable_surplus_mode[t])
                )
                model.addConstr(
                    surplus_without_storage
                    <= balance_big_m * renewable_surplus_mode[t]
                )

                # Implementation note.
                model.addConstr(
                    cp.quicksum(
                        discharge[item.id, t]
                        for item in problem.storages
                    )
                    <= storage_power_limit * (1 - renewable_surplus_mode[t])
                )
            # Implementation note.


        pre_storage_balance = {}
        for t in periods:
            conventional[t] = cp.quicksum(generator_vars[item.id, t] for item in problem.generators)
            renewable = cp.quicksum(renewable_vars[item.id, t] for item in problem.renewables)
            storage_supply = cp.quicksum(
                discharge[item.id, t] - charge[item.id, t] for item in problem.storages
            )
            model.addConstr(
                fixed_generation[t] + conventional[t] + renewable + storage_supply
                + grid_import[t] - grid_export[t]
                == load[t] + cp.quicksum(compute_load[dc.id, t] for dc in data_centers)
            )
            net_demand[t] = (
                load[t]
                + cp.quicksum(compute_load[dc.id, t] for dc in data_centers)
                - fixed_generation[t]
                - renewable
                - storage_supply
            )
            # Enforce the physical direction of storage operation. The sign
            # is decided from the balance before storage: deficit => discharge
            # only; surplus => charge only.
            available_renewable = sum(
                renewable_available[item.id][t] for item in problem.renewables
            )
            pre_storage_balance[t] = (
                load[t]
                + cp.quicksum(compute_load[dc.id, t] for dc in data_centers)
                - fixed_generation[t]
                - conventional[t]
                # Use available renewable forecast for the direction signal,
                # not the post-curtailment dispatch variable. Otherwise a
                # curtailed renewable surplus can incorrectly look like a
                # generation deficit and block battery charging.
                - available_renewable
            )
            model.addConstr(pre_storage_balance[t] <= balance_big_m * deficit_mode[t])
            model.addConstr(pre_storage_balance[t] >= -balance_big_m * (1 - deficit_mode[t]))
            model.addConstr(
                cp.quicksum(charge[item.id, t] for item in problem.storages)
                <= storage_power_limit * (1 - deficit_mode[t])
            )
            model.addConstr(
                cp.quicksum(discharge[item.id, t] for item in problem.storages)
                <= storage_power_limit * deficit_mode[t]
            )
            # Charging is allowed only when the fixed renewable/base supply
            # leaves a non-negative surplus after serving the modeled load.
            # Dispatchable generation and grid imports cannot manufacture a
            # charging opportunity by increasing conventional output.
            green_surplus = (
                available_renewable
                + fixed_generation[t]
                - load[t]
                - cp.quicksum(compute_load[dc.id, t] for dc in data_centers)
            )
            charge_total = cp.quicksum(
                charge[item.id, t] for item in problem.storages
            )
            model.addConstr(charge_total <= storage_power_limit * green_surplus_mode[t])
            model.addConstr(
                charge_total
                <= green_surplus + balance_big_m * (1 - green_surplus_mode[t])
            )

        # Track the post-storage supply-demand residual explicitly so the
        # storage schedule follows generation deficits and surpluses.
        supply_balance_abs = {
            t: model.addVar(lb=0.0, name=f"supply_balance_abs_{t}")
            for t in periods
        }
        for t in periods:
            storage_supply = cp.quicksum(
                discharge[item.id, t] - charge[item.id, t]
                for item in problem.storages
            )
            model.addConstr(
                supply_balance_abs[t]
                >= pre_storage_balance[t] - storage_supply
            )
            # Use the available-renewable direction signal here as well, so
            # curtailing renewable output cannot hide a real surplus from the
            # storage objective.
            # Keep this residual constraint inside the period loop.  Using
            # the last loop value here would leave earlier periods without a
            # storage-following balance signal.
            # model.addConstr(supply_balance_abs[t] >= (
            #     pre_storage_balance[t]
            #     - cp.quicksum(discharge[item.id, t] - charge[item.id, t]
            #                   for item in problem.storages)
            # ))
            # model.addConstr(supply_balance_abs[t] >= (
            #     -pre_storage_balance[t]
            #     + cp.quicksum(discharge[item.id, t] - charge[item.id, t]
            #                   for item in problem.storages)
            # ))

        green_match = {}
        for dc in data_centers:
            local_renewables = [item for item in problem.renewables if item.bus_id == dc.grid_bus_id]
            for t in periods:
                green_match[dc.id, t] = model.addVar(lb=0, name=f"green_match_{dc.id}_{t}")
                model.addConstr(green_match[dc.id, t] <= compute_load[dc.id, t])
                model.addConstr(green_match[dc.id, t] <= cp.quicksum(
                    renewable_vars[item.id, t] for item in local_renewables
                ))
        for bus_id in {dc.grid_bus_id for dc in data_centers}:
            bus_centers = [dc for dc in data_centers if dc.grid_bus_id == bus_id]
            bus_renewables = [item for item in problem.renewables if item.bus_id == bus_id]
            for t in periods:
                model.addConstr(cp.quicksum(green_match[dc.id, t] for dc in bus_centers) <= cp.quicksum(
                    renewable_vars[item.id, t] for item in bus_renewables
                ))

        peak = model.addVar(lb=0, name="peak")
        renewable_upper = sum(max(values) for values in renewable_available.values())
        storage_upper = sum(item.max_power_mw for item in problem.storages)
        residual_big_m = max(
            balance_big_m,
            float(renewable_upper + storage_upper + max(load or [0.0]) + 1.0),
        )
        valley = model.addVar(lb=0.0, name="valley")
        positive_net_demand = {}
        positive_net_demand_mode = {}
        for t in periods:
            # Peak shaving is defined over positive net demand only.  A
            # renewable surplus is a zero valley, not a negative valley that
            # the optimizer can improve by discharging the battery.
            positive_net_demand[t] = model.addVar(
                lb=0.0, ub=residual_big_m,
                name=f"positive_net_demand_{t}",
            )
            positive_net_demand_mode[t] = model.addVar(
                vtype=cp.COPT.BINARY,
                name=f"positive_net_demand_mode_{t}",
            )
            model.addConstr(positive_net_demand[t] >= net_demand[t])
            model.addConstr(
                positive_net_demand[t]
                <= net_demand[t] + residual_big_m * (1 - positive_net_demand_mode[t])
            )
            model.addConstr(
                positive_net_demand[t]
                <= residual_big_m * positive_net_demand_mode[t]
            )
            model.addConstr(net_demand[t] <= residual_big_m * positive_net_demand_mode[t])
            model.addConstr(
                net_demand[t]
                >= -residual_big_m * (1 - positive_net_demand_mode[t])
            )
            model.addConstr(peak >= positive_net_demand[t])
            model.addConstr(valley <= positive_net_demand[t])

        # A soft range penalty keeps compute utilization from collapsing onto
        # one center.  It is deliberately a secondary objective term: user
        # placement preferences, resource feasibility and the selected
        # energy objective remain authoritative.
        utilization_peak = {t: model.addVar(lb=0.0, ub=1.0, name=f"utilization_peak_{t}") for t in periods}
        utilization_floor = {t: model.addVar(lb=0.0, ub=1.0, name=f"utilization_floor_{t}") for t in periods}
        utilization_target = {
            t: model.addVar(lb=0.0, ub=1.0, name=f"utilization_target_{t}")
            for t in periods
        } if tasks else {}
        utilization_deviation = {
            (dc.id, t): model.addVar(lb=0.0, ub=1.0, name=f"utilization_deviation_{dc.id}_{t}")
            for dc in data_centers for t in periods
        } if tasks else {}
        total_capacity = max(1.0, sum(float(dc.total_compute) for dc in data_centers))
        for t in periods:
            if tasks:
                model.addConstr(
                    utilization_target[t] * total_capacity
                    == cp.quicksum(compute_load[dc.id, t] for dc in data_centers)
                )
            for dc in data_centers:
                utilization = compute_load[dc.id, t] / max(1.0, float(dc.total_compute))
                model.addConstr(utilization_peak[t] >= utilization)
                model.addConstr(utilization_floor[t] <= utilization)
                if tasks:
                    model.addConstr(utilization_deviation[dc.id, t] >= utilization - utilization_target[t])
                    model.addConstr(utilization_deviation[dc.id, t] >= utilization_target[t] - utilization)
        if tasks:
            load_balance_raw = cp.quicksum(
                utilization_deviation[dc.id, t]
                for dc in data_centers for t in periods
            ) + 0.25 * cp.quicksum(
                utilization_peak[t] - utilization_floor[t] for t in periods
            )
        else:
            load_balance_raw = cp.quicksum(
                utilization_peak[t] - utilization_floor[t] for t in periods
            )
        cost_raw = cp.quicksum(
            (item.cost_linear_yuan_per_mwh * generator_vars[item.id, t]
             + item.cost_quadratic_yuan_per_mw2h * generator_vars[item.id, t] * generator_vars[item.id, t])
            * problem.step_hours * price_factors[t]
            for item in problem.generators for t in periods
        ) + cp.quicksum(
            item.degradation_cost_per_mwh
            * (charge[item.id, t] + discharge[item.id, t])
            * problem.step_hours
            for item in problem.storages for t in periods
        ) + cp.quicksum(
            1000.0 * price_factors[t]
            * grid_import[t] * problem.step_hours
            for t in periods
        )
        carbon_raw = cp.quicksum(
            item.emission_tco2_per_mwh * generator_vars[item.id, t] * problem.step_hours
            for item in problem.generators for t in periods
        )
        curtailment_raw = cp.quicksum(
            (renewable_available[item.id][t] - renewable_vars[item.id, t]) * problem.step_hours
            for item in problem.renewables for t in periods
        )
        storage_raw = cp.quicksum(
            (charge[item.id, t] + discharge[item.id, t]) * problem.step_hours
            for item in problem.storages for t in periods
        )
        # Storage is a system-level sink: when aggregate generation exceeds
        # aggregate demand, charging should be preferred to curtailment,
        # regardless of the storage/renewable bus pairing.  Direction and
        # ramp/SOC constraints still cap the physically possible amount.
        renewable_absorption_raw = cp.quicksum(
            charge[item.id, t] * problem.step_hours
            for item in problem.storages for t in periods
        )
        reference_raw = cp.quicksum(reference_deviation.values()) * problem.step_hours
        delay_raw = cp.quicksum(
            (
                max(0.0, start * 3600 * problem.step_hours + problem.start_second - task.submitted_at)
                + float(latency_ms.get((task.region, dc.region), 20.0)) / 1000.0
            ) * variable
            for (task_id, dc_id, start), variable in placement.items()
            for task in tasks if task.id == task_id
            for dc in data_centers if dc.id == dc_id
        )
        queue_count = cp.quicksum(queued.values())
        green_raw = cp.quicksum(green_match.values())
        fairness_raw = cp.quicksum([fairness_floor])
        supply_balance_raw = cp.quicksum(supply_balance_abs.values()) * problem.step_hours
        references = {
            "cost": max(1.0, sum(abs(item.cost_linear_yuan_per_mwh) * item.max_mw * horizon for item in problem.generators)),
            "carbon": max(1.0, sum(item.emission_tco2_per_mwh * item.max_mw * horizon for item in problem.generators)),
            "renewable_curtailment": max(1.0, sum(sum(values) for values in renewable_available.values())),
            "peak_shaving": max(1.0, max(load) - min(load)),
            "storage_degradation": max(1.0, sum(item.max_power_mw * horizon for item in problem.storages)),
            "renewable_absorption": max(1.0, sum(item.max_power_mw * horizon for item in problem.storages)),
            "reference_deviation": max(1.0, sum(
                sum(values) * problem.step_hours
                for values in generator_reference.values()
            )),
            "delay": max(1.0, horizon * 3600.0 * max(1, len(tasks))),
            "load_balance": max(1.0, float(horizon) * max(1, len(data_centers))),
            # Local renewable matching is a secondary placement signal.  It
            # is normalized by available renewable energy so its scale stays
            # stable when the installed capacity changes.
            "green_match": max(1.0, sum(sum(values) for values in renewable_available.values()) * problem.step_hours),
            "minimum_center_utilization": 1.0,
            "supply_balance": max(1.0, sum(load) * problem.step_hours),
        }
        raw = {
            "cost": cost_raw, "carbon": carbon_raw,
            "renewable_curtailment": curtailment_raw,
            "peak_shaving": peak - valley,
            "storage_degradation": storage_raw,
            "renewable_absorption": renewable_absorption_raw,
            "reference_deviation": reference_raw,
            "delay": delay_raw,
            "load_balance": load_balance_raw,
            "green_match": green_raw,
            "minimum_center_utilization": fairness_raw,
            "supply_balance": supply_balance_raw,
        }
        normalized = {name: expression / references[name] for name, expression in raw.items()}
        queue_penalty = 1000.0 * queue_count
        # Green-first scheduling can otherwise defer or concentrate work to
        # chase renewable peaks.  Keep the same utilization-balancing policy
        # used by the comprehensive objective, with a stronger secondary
        # coefficient so a curtailment tie does not leave a center idle.
        configured_balance_weight = max(0.0, float(load_balance_weight or 0.0))
        balance_priority = 2.0 if objective == "min_curtailment" else 1.0
        balance_term = configured_balance_weight * balance_priority * normalized["load_balance"]
        supply_balance_term = max(
            0.0, float((weights or {}).get("peak_shaving", 1.0))
        ) * normalized["supply_balance"]
        # During bounded real-time solves the exact lexicographic tie-break is
        # intentionally skipped.  Keep a small local-green bonus in the
        # primary objective so a renewable tie does not systematically avoid
        # the center connected to the available renewable node (for example,
        # the PV-connected Shanghai center at Bus16).
        green_match_bonus = (
            0.25 * normalized["green_match"]
            if objective == "min_curtailment" else 0.0
        )
        fairness_bonus = 0.75 * normalized["minimum_center_utilization"]
        if objective == "min_cost":
            objective_expression = normalized["cost"] + supply_balance_term + balance_term - fairness_bonus + 1e-8 * normalized["reference_deviation"] + queue_penalty
        elif objective == "min_carbon":
            objective_expression = normalized["carbon"] + supply_balance_term + balance_term - fairness_bonus + 1e-8 * normalized["reference_deviation"] + queue_penalty
        elif objective == "min_curtailment":
            objective_expression = normalized["renewable_curtailment"] + supply_balance_term + balance_term - green_match_bonus - fairness_bonus + 1e-8 * normalized["reference_deviation"] + queue_penalty
        elif objective == "peak_shaving":
            objective_expression = normalized["peak_shaving"] + supply_balance_term + balance_term - fairness_bonus + 1e-8 * normalized["reference_deviation"] + queue_penalty
        elif objective == "min_delay":
            objective_expression = normalized["delay"] + supply_balance_term + balance_term - fairness_bonus + 1e-8 * normalized["reference_deviation"] + queue_penalty
        else:
            objective_expression = cp.quicksum(
                float(weights.get(name, 0.0)) * expression
                for name, expression in normalized.items()
                if name != "supply_balance"
            ) + supply_balance_term + balance_term - fairness_bonus + queue_penalty
        model.setObjective(objective_expression, sense=cp.COPT.MINIMIZE)
        model.solve()
        has_incumbent = bool(model.getAttr(cp.COPT.Attr.HasMipSol))
        if model.status != cp.COPT.OPTIMAL and not has_incumbent:
            return JointCoordinationResult(
                status="infeasible", objective_value=None, assignments={}, start_periods={},
                queued_tasks=[task.id for task in tasks], generator_mw={},
                renewable_dispatched_mw={}, renewable_curtailed_mw={},
                storage_power_mw={}, storage_soc_mwh={}, objective_components={},
                strictly_feasible=False, forecast_fill="cyclic" if cyclic else "native",
                external_grid_mw=[],
            )
        primary_objective_value = float(model.objval)
        solve_status = "optimal" if model.status == cp.COPT.OPTIMAL else "optimal_solution"
        # Exact runs retain the green-energy tie-break.  Bounded real-time
        # runs intentionally keep the first feasible incumbent: the weighted
        # objective already includes renewable curtailment, and a second/third
        # MIP solve would defeat the configured latency bound.
        if not bounded_solve and model.status == cp.COPT.OPTIMAL:
            tolerance = max(1e-5, abs(primary_objective_value) * 1e-7)
            primary_bound = objective_expression <= primary_objective_value + tolerance
            if "QConstrBuilder" in type(primary_bound).__name__:
                model.addQConstr(primary_bound)
            else:
                model.addConstr(primary_bound)
            model.setObjective(-green_raw, sense=cp.COPT.MINIMIZE)
            model.solve()
            if model.status != cp.COPT.OPTIMAL:
                raise RuntimeError("joint lexicographic tie-break became infeasible")
            best_green_match = float(green_raw.getValue())
            model.addConstr(green_raw >= best_green_match - 1e-6)
            model.setObjective(objective_expression, sense=cp.COPT.MINIMIZE)
            model.solve()
            if model.status != cp.COPT.OPTIMAL:
                raise RuntimeError("joint primary objective restore became infeasible")

        assignments = {}
        starts = {}
        for (task_id, dc_id, start), variable in placement.items():
            if variable.x > 0.5:
                assignments[task_id] = dc_id
                starts[task_id] = start
        queued_tasks = [task.id for task in tasks if queued[task.id].x > 0.5]
        generator_output = {
            item.id: [float(generator_vars[item.id, t].x) for t in periods]
            for item in problem.generators
        }
        renewable_output = {
            item.id: [float(renewable_vars[item.id, t].x) for t in periods]
            for item in problem.renewables
        }
        storage_power = {
            item.id: [float(discharge[item.id, t].x - charge[item.id, t].x) for t in periods]
            for item in problem.storages
        }
        storage_soc = {
            item.id: [float(soc[item.id, t].x) for t in range(horizon + 1)]
            for item in problem.storages
        }
        raw_values = {name: float(expression.getValue()) for name, expression in raw.items()}
        components = {
            name: {"raw": raw_values[name], "normalized": raw_values[name] / references[name]}
            for name in raw
        }
        power_dispatch = PowerDispatchResult(
            status=solve_status, objective_value=primary_objective_value,
            generator_mw=generator_output,
            renewable_dispatched_mw=renewable_output,
            renewable_curtailed_mw={
                item.id: [max(0.0, renewable_available[item.id][t] - renewable_output[item.id][t]) for t in periods]
                for item in problem.renewables
            },
            storage_power_mw=storage_power, storage_soc_mwh=storage_soc,
            objective_components=components, strictly_feasible=True,
            forecast_fill="cyclic" if cyclic else "native",
            external_grid_mw=[
                float(grid_import[t].x - grid_export[t].x) for t in periods
            ],
        )
        return JointCoordinationResult(
            status=solve_status, objective_value=primary_objective_value, assignments=assignments,
            start_periods=starts, queued_tasks=queued_tasks,
            generator_mw=power_dispatch.generator_mw,
            renewable_dispatched_mw=power_dispatch.renewable_dispatched_mw,
            renewable_curtailed_mw=power_dispatch.renewable_curtailed_mw,
            storage_power_mw=power_dispatch.storage_power_mw,
            storage_soc_mwh=power_dispatch.storage_soc_mwh,
            objective_components=components, strictly_feasible=True,
            forecast_fill=power_dispatch.forecast_fill, power_dispatch=power_dispatch,
            external_grid_mw=power_dispatch.external_grid_mw,
        )

