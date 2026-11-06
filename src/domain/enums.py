# src/domain/enums.py
from enum import Enum

class TaskType(str, Enum):
    # Implementation note.
    AI_TRAINING = "ai_training"       # Implementation note.
    SCIENTIFIC = "scientific"         # Implementation note.
    RENDERING = "rendering"           # Implementation note.
    WEB_SERVICE = "web_service"       # Implementation note.
    GENERAL = "general"               # Implementation note.


class TaskStatus(str, Enum):
    SUBMITTED = "submitted"
    PENDING = "pending"
    READY = "ready"
    SCHEDULING = "scheduling"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
