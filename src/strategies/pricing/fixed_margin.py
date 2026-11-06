from .base import PricingStrategy
from src.domain.datacenter import DataCenter

class FixedMarginPricing(PricingStrategy):
    """Implementation documentation."""
    def calculate_price(self, node_price_per_kwh: float, datacenter: DataCenter) -> float:
        # Implementation note.
        electricity_cost = node_price_per_kwh * datacenter.power_per_tflops * 1000
        # Implementation note.
        return electricity_cost * datacenter.profit_margin
