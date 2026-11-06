"""Global-only policy wrapper for compute/power coordination."""

from __future__ import annotations

from typing import Mapping

from src.optimization.joint_coordination import JointCoordinationOptimizer


class GlobalCoordinationOptimizer(JointCoordinationOptimizer):
    """Apply global-only storage and peak-shaving policy adjustments."""

    @staticmethod
    def prepare_objective(
        objective: str, weights: Mapping[str, float] | None
    ) -> tuple[str, dict[str, float]]:
        configured = {key: float(value) for key, value in (weights or {}).items()}
        if objective == "min_cost":
            configured.setdefault("cost", 1.0)
            configured.setdefault("renewable_curtailment", 0.35)
            configured.setdefault("peak_shaving", 0.25)
            configured.setdefault("storage_degradation", 0.05)
            configured.setdefault("delay", 0.2)
            return "weighted", configured
        if objective == "min_curtailment":
            configured.setdefault("renewable_curtailment", 4.0)
            configured.setdefault("peak_shaving", 1.5)
            configured.setdefault("storage_degradation", 0.05)
            configured.setdefault("cost", 0.2)
            configured.setdefault("delay", 0.2)
            return "weighted", configured
        if objective == "weighted":
            configured.setdefault("peak_shaving", 1.0)
            configured.setdefault("storage_degradation", 0.05)
            return "weighted", configured
        return objective, configured

    def solve(self, tasks, data_centers, power_problem, *, objective, weights,
              latency_ms, carbon_intensity, **kwargs):
        effective_objective, effective_weights = self.prepare_objective(
            objective, weights
        )
        return super().solve(
            tasks,
            data_centers,
            power_problem,
            objective=effective_objective,
            weights=effective_weights,
            latency_ms=latency_ms,
            carbon_intensity=carbon_intensity,
            maintain_terminal_soc=False,
            **kwargs,
        )
