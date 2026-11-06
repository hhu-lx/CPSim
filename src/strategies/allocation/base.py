from typing import List
from src.domain.task import Task
from src.domain.datacenter import DataCenter

class AllocationStrategy:
    """Implementation documentation."""
    def allocate(self, task: Task, data_centers: List[DataCenter], snapshot: dict | None = None) -> DataCenter:
        raise NotImplementedError
