# src/strategies/mapping/base.py
from typing import Protocol
from src.domain.task import Task

class MappingStrategy(Protocol):
    """Implementation documentation."""
    def map_to_power(self, task: Task) -> float:
        """Implementation documentation."""
        ...
