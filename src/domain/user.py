from pydantic import BaseModel, Field
from uuid import uuid4

class User(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    region: str
    name: str | None = None
    email: str | None = None
