from typing import Protocol
import pandapower as pp

class ElectricityPricingStrategy(Protocol):
    """Implementation documentation."""
    def get_node_price(
        self,
        network: pp.pandapowerNet,
        bus_id: int,
        extra_load_mw: float = 0.0,
        system_extra_load_mw: float = 0.0,
    ) -> float:
        ...
