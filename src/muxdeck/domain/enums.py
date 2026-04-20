from __future__ import annotations

from enum import StrEnum


class AgentStatus(StrEnum):
    DISCOVERED = "discovered"
    STARTING = "starting"
    RUNNING = "running"
    IDLE = "idle"
    WAITING_INPUT = "waiting_input"
    BLOCKED = "blocked"
    ERROR = "error"
    COMPLETED = "completed"
    DEAD = "dead"
    UNKNOWN = "unknown"


class TaskStatus(StrEnum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class EvidenceKind(StrEnum):
    RAW = "raw"
    DERIVED = "derived"
