from .base import ElectricityPricingStrategy
import pandapower as pp


class MarginalElectricityPricing(ElectricityPricingStrategy):
    """Apply a marginal adjustment on top of the time-of-use base price.

    The optional base pricing object preserves compatibility with existing configurations.
    """
    def __init__(
        self,
        base_price: float = 0.5,
        sensitivity: float = 0.10,
        scale: float = 1000.0,
        base_pricing=None,
    ):
        self.base_price = base_price
        self.sensitivity = sensitivity
        self.scale = scale
        self.base_pricing = base_pricing
        self.current_hour = 0.0

    def set_hour(self, hour: float) -> None:
        self.current_hour = float(hour) % 24.0

    def _base_at_hour(self, hour: float | None = None) -> float:
        point = self.current_hour if hour is None else float(hour)
        if self.base_pricing is not None:
            return float(self.base_pricing.price_at_hour(point))
        return float(self.base_price)

    def get_node_price(
        self,
        network: pp.pandapowerNet,
        bus_id: int,
        extra_load_mw: float,
        system_extra_load_mw: float = 0.0,
    ) -> float:
        """Return the TOU base price with equal system and node load feedback.

        Both feedback terms are normalised by ``scale`` and only positive
        data-centre load is priced.  The optional system argument preserves
        compatibility for direct callers that only provide a node load.
        """
        node_load_pu = max(0.0, float(extra_load_mw)) / self.scale
        system_load_pu = max(0.0, float(system_extra_load_mw)) / self.scale
        feedback = self.sensitivity * 0.5 * (system_load_pu + node_load_pu)
        return self._base_at_hour() * (1.0 + feedback)

    def forecast_prices(self, system_load_mw, start_hour: float | None = None, step_hours: float = 1.0) -> list[float]:
        """Forecast TOU-based prices using the available system feedback term.

        A forecast has no per-node allocation yet, so it deliberately applies
        only the equal-weight system half of the runtime feedback formula.
        """
        start = self.current_hour if start_hour is None else float(start_hour)
        return [
            self._base_at_hour(start + index * float(step_hours))
            * (1.0 + self.sensitivity * 0.5 * max(0.0, float(load)) / self.scale)
            for index, load in enumerate(system_load_mw)
        ]
