# src/strategies/mapping/linear.py
from typing import Dict
from .base import MappingStrategy
from src.domain.task import Task


class LinearMapping(MappingStrategy):
    # Implementation note.
    TYPE_POWER_FACTORS: Dict[str, float] = {
        "ai_training": 1.8,  # Implementation note.
        "scientific": 1.2,  # Implementation note.
        "rendering": 1.5,  # Implementation note.
        "web_service": 0.4,  # Implementation note.
        "general": 0.7,  # Implementation note.
        "default": 0.5
    }

    def __init__(self, base_coefficient: float = 0.5,
                 gpu_scale_factor: float = 0.9,
                 custom_factors: Dict[str, float] = None):

        self.base_coefficient = base_coefficient
        self.gpu_scale_factor = gpu_scale_factor

        # Implementation note.
        if custom_factors:
            self.TYPE_POWER_FACTORS.update(custom_factors)

    def map_to_power(self, task: Task) -> float:

        # Implementation note.
        task_type = getattr(task, 'task_type', 'default')
        if not task_type or task_type not in self.TYPE_POWER_FACTORS:
            task_type = 'default'
        type_factor = self.TYPE_POWER_FACTORS[task_type]

        # Implementation note.
        plan_gpu = getattr(task, 'plan_gpu', 0) or 0
        if plan_gpu > 0:
            # Implementation note.
            gpu_ratio = plan_gpu / 100.0
            gpu_count = gpu_ratio * 2  # Implementation note.

            # Implementation note.
            if gpu_count <= 1:
                gpu_factor = 1.0
            else:
                # Implementation note.
                gpu_factor = 1.0 + (gpu_count - 1) * self.gpu_scale_factor
        else:
            # Implementation note.
            gpu_factor = 0.6

        # Implementation note.
        power_mw = self.base_coefficient * task.required_compute * type_factor * gpu_factor

        # Implementation note.
        return max(0.1, power_mw)

    def get_type_factor(self, task_type: str) -> float:
        """Implementation documentation."""
        return self.TYPE_POWER_FACTORS.get(task_type, self.TYPE_POWER_FACTORS['default'])

    def update_type_factor(self, task_type: str, factor: float):
        """Implementation documentation."""
        self.TYPE_POWER_FACTORS[task_type] = factor
