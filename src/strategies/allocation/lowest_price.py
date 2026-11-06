# src/strategies/allocation/lowest_price.py
from typing import List
from src.domain.task import Task
from src.domain.datacenter import DataCenter
from .base import AllocationStrategy


class LowestPriceStrategy(AllocationStrategy):
    """Implementation documentation."""

    def __init__(self, allow_cross_region: bool = True):
        self.allow_cross_region = allow_cross_region

    def allocate(self, task: Task, data_centers: List[DataCenter], snapshot: dict | None = None) -> DataCenter:
        # Implementation note.
        # Use the same complete feasibility check as COPT: compute, network
        # bandwidth and requested GPU inventory must all be available.
        eligible = [dc for dc in data_centers if dc.can_allocate(task)]

        if not eligible:
            required_bandwidth = task.required_bandwidth_mbps or task.compute_bandwidth_requirement()
            details = "; ".join(
                f"{dc.id}: compute={dc.available_compute:g}/{dc.total_compute:g}, "
                f"bandwidth={dc.available_bandwidth:g}/{dc.total_bandwidth:g}, "
                f"gpu={self._gpu_snapshot(task, dc)}"
                for dc in data_centers
            ) or "no configured data centers"
            raise Exception(
                f"No available data center for task {task.id}: "
                f"required_compute={task.required_compute:g}, required_bandwidth={required_bandwidth:g}, "
                f"required_gpu={task.required_gpu_count or 0} ({task.required_gpu_type or task.gpu_type or 'any'}); {details}"
            )

        # Implementation note.
        if self.allow_cross_region:
            eligible.sort(key=lambda dc: dc.current_price)
            return eligible[0]

        # Implementation note.
        same_region = [dc for dc in eligible if dc.region == task.region]
        if not same_region:
            raise Exception(f"No available data center in region {task.region} for task {task.id}")

        same_region.sort(key=lambda dc: dc.current_price)
        return same_region[0]

    @staticmethod
    def _gpu_snapshot(task: Task, dc: DataCenter) -> str:
        required = task.required_gpu_count or 0
        if not required:
            return "not-required"
        model = task.required_gpu_type or task.gpu_type or "any"
        devices = ",".join(
            f"{d.model}:{max(0, d.quantity - dc.used_device_count(d.model))}"
            for d in dc.device_pool if dc._is_compute_device(d)
        ) or "none"
        if devices == "none" and dc.available_compute >= 80:
            devices = f"virtual-compute-gpu:{int(dc.available_compute // 80)}"
        return f"required={required}:{model}, available={devices}"
