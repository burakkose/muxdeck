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


class EvidenceKind(StrEnum):
    RAW = "raw"
    DERIVED = "derived"
