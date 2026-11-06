from typing import List
from src.domain.task import Task
from src.domain.datacenter import DataCenter
from src.strategies.allocation.base import AllocationStrategy

class Scheduler:
    # Implementation note.
    def __init__(self, strategy: AllocationStrategy):
        self.strategy = strategy

    def select_data_center(
        self,
        task: Task,
        data_centers: List[DataCenter],
        snapshot: dict | None = None,
    ) -> DataCenter:
        # Implementation note.
        return self.strategy.allocate(task, data_centers, snapshot)
