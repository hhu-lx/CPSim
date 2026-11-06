# src/simulation/network_sim.py

import simpy
from typing import Dict, List, Optional
from src.domain.task import Task
from src.domain.datacenter import DataCenter


class BandwidthManager:
    def __init__(self, env: simpy.Environment):

        self.env = env
        self.bandwidth_resources: Dict[str, simpy.Resource] = {}
        self.data_centers: Dict[str, DataCenter] = {}

    def register_data_center(self, datacenter: DataCenter):

        self.data_centers[datacenter.id] = datacenter
        self.bandwidth_resources[datacenter.id] = simpy.Resource(
            self.env,
            capacity=datacenter.total_bandwidth
        )
        print(f"Registered datacenter {datacenter.name} (ID: {datacenter.id})，bandwidth capacity: {datacenter.total_bandwidth} Mbps")

    def request_bandwidth(self, datacenter_id: str, task: Task):

        if datacenter_id not in self.bandwidth_resources:
            raise ValueError(f"Datacenter {datacenter_id} bandwidth resource is not registered")

        # Implementation note.
        required_bandwidth = self.calculate_bandwidth_requirement(task)
        task.required_bandwidth_mbps = required_bandwidth

        # Implementation note.
        bandwidth_request = self.bandwidth_resources[datacenter_id].request()
        yield bandwidth_request

        # Implementation note.
        dc = self.data_centers[datacenter_id]
        dc.used_bandwidth += required_bandwidth

        return bandwidth_request

    def release_bandwidth(self, datacenter_id: str, task: Task, bandwidth_request):
        if datacenter_id not in self.bandwidth_resources:
            return

        # Implementation note.
        self.bandwidth_resources[datacenter_id].release(bandwidth_request)

        # Implementation note.
        dc = self.data_centers.get(datacenter_id)
        if dc:
            dc.used_bandwidth = max(0.0, dc.used_bandwidth - task.required_bandwidth_mbps)

    def calculate_bandwidth_requirement(self, task: Task) -> float:
        if task.duration_hours <= 0:
            return 0.0

        total_seconds = task.duration_hours * 3600
        if total_seconds <= 0:
            return 0.0

        return (task.data_size_gb * 8192) / total_seconds

    def get_available_bandwidth(self, datacenter_id: str) -> float:
        """Implementation documentation."""
        if datacenter_id not in self.bandwidth_resources:
            return 0.0
        resource = self.bandwidth_resources[datacenter_id]
        return resource.capacity - resource.count

    def get_bandwidth_utilization(self, datacenter_id: str) -> float:
        """Implementation documentation."""
        if datacenter_id not in self.bandwidth_resources:
            return 0.0
        resource = self.bandwidth_resources[datacenter_id]
        if resource.capacity == 0:
            return 0.0
        return resource.count / resource.capacity


class NetworkLatencySimulator:
    def __init__(self):
        # Implementation note.
        self.region_latency = {
            ("region_a", "region_a"): 5.0,
            ("region_a", "region_b"): 15.0,
            ("region_a", "region_c"): 20.0,
            ("region_b", "region_b"): 5.0,
            ("region_b", "region_c"): 12.0,
            ("region_c", "region_c"): 5.0,
        }

    def get_latency(self, from_region: str, to_region: str) -> float:
        """Implementation documentation."""
        key = (from_region, to_region)
        if key in self.region_latency:
            return self.region_latency[key]
        # Implementation note.
        return 20.0

    def get_transmission_time(self, data_size_gb: float, bandwidth_mbps: float) -> float:
        if bandwidth_mbps <= 0:
            return float('inf')
        # 1 GB = 8192 Mb
        return (data_size_gb * 8192) / bandwidth_mbps


def get_data_size_by_task_type(task_type: str, plan_gpu: float = 0.0) -> float:

    type_mapping = {
        "ai_training": 50.0,
        "scientific": 30.0,
        "rendering": 80.0,
        "web_service": 2.0,
        "general": 10.0,
    }

    base_size = type_mapping.get(task_type, 10.0)

    # Implementation note.
    if task_type == "ai_training" and plan_gpu > 0:
        gpu_factor = plan_gpu / 100.0
        return base_size + gpu_factor * 100.0

    return base_size
