from typing import Protocol, TYPE_CHECKING
if TYPE_CHECKING:
    from src.domain.datacenter import DataCenter
class PricingStrategy(Protocol):
    """Implementation documentation."""
    def calculate_price(self, node_price_per_kwh: float, datacenter: 'DataCenter') -> float:
        ...
