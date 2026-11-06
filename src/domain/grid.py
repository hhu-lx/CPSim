# src/domain/grid.py
from typing import Optional
from pydantic import BaseModel, Field

class GridNode(BaseModel):
    bus_id: int                              # Implementation note.
    region: str                               # Implementation note.
    voltage_kv: float = 110.0                 # Implementation note.
    load_mw: float = 0.0                      # Implementation note.
    generation_mw: float = 0.0                # Implementation note.
    price_per_kwh: Optional[float] = None     # Implementation note.

    @property
    def net_load(self) -> float:
        return self.load_mw - self.generation_mw
