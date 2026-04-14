from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from copilot_commander.domain.value_objects import (
    AgentId,
    EventId,
    LogChunkId,
    SessionId,
    ensure_agent_id,
    ensure_aware_datetime,
    ensure_event_id,
    ensure_log_chunk_id,
    ensure_non_empty_text,
    ensure_non_negative_int,
    ensure_session_id,
    utc_now,
)
from copilot_commander.exceptions import DomainValidationError

_ALLOWED_SEVERITIES = {"debug", "info", "warning", "error"}
_ALLOWED_SOURCES = {"tmux_capture", "stdout", "stderr", "system"}


def _generate_event_id() -> str:
    return str(EventId.generate())


def _generate_log_chunk_id() -> str:
    return str(LogChunkId.generate())


def _generate_agent_id() -> str:
    return str(AgentId.generate())


def _normalize_id(
    value: str | AgentId | EventId | LogChunkId | SessionId,
    *,
    field_name: str,
) -> str:
    if isinstance(value, AgentId):
        return str(ensure_agent_id(value, field_name=field_name))
    if isinstance(value, EventId):
        return str(ensure_event_id(value, field_name=field_name))
    if isinstance(value, LogChunkId):
        return str(ensure_log_chunk_id(value, field_name=field_name))
    if isinstance(value, SessionId):
        return str(ensure_session_id(value, field_name=field_name))
    return ensure_non_empty_text(value, field_name=field_name)


def _normalize_optional_text(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    return ensure_non_empty_text(value, field_name=field_name)


@dataclass(frozen=True, slots=True)
class Event:
    id: str = field(default_factory=_generate_event_id)
    occurred_at: datetime = field(default_factory=utc_now)
    agent_id: str | None = None
    session_id: str | None = None
    kind: str = ""
    severity: Literal["debug", "info", "warning", "error"] = "info"
    payload_json: str = "{}"

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _normalize_id(self.id, field_name="id"))
        object.__setattr__(
            self, "occurred_at", ensure_aware_datetime(self.occurred_at, field_name="occurred_at")
        )
        if self.agent_id is not None:
            object.__setattr__(
                self, "agent_id", _normalize_id(self.agent_id, field_name="agent_id")
            )
        if self.session_id is not None:
            object.__setattr__(
                self, "session_id", _normalize_id(self.session_id, field_name="session_id")
            )
        object.__setattr__(self, "kind", ensure_non_empty_text(self.kind, field_name="kind"))
        if self.severity not in _ALLOWED_SEVERITIES:
            msg = "severity must be one of debug, info, warning, error"
            raise DomainValidationError(msg)
        object.__setattr__(
            self,
            "payload_json",
            ensure_non_empty_text(self.payload_json, field_name="payload_json"),
        )
        try:
            json.loads(self.payload_json)
        except json.JSONDecodeError as exc:
            msg = "payload_json must contain valid JSON"
            raise DomainValidationError(msg) from exc


@dataclass(frozen=True, slots=True)
class LogChunk:
    id: str = field(default_factory=_generate_log_chunk_id)
    agent_id: str = field(default_factory=_generate_agent_id)
    session_id: str | None = None
    source: Literal["tmux_capture", "stdout", "stderr", "system"] = "tmux_capture"
    sequence_no: int = 0
    captured_at: datetime = field(default_factory=utc_now)
    content: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _normalize_id(self.id, field_name="id"))
        object.__setattr__(self, "agent_id", _normalize_id(self.agent_id, field_name="agent_id"))
        if self.session_id is not None:
            object.__setattr__(
                self, "session_id", _normalize_id(self.session_id, field_name="session_id")
            )
        if self.source not in _ALLOWED_SOURCES:
            msg = "source must be one of tmux_capture, stdout, stderr, system"
            raise DomainValidationError(msg)
        object.__setattr__(
            self, "sequence_no", ensure_non_negative_int(self.sequence_no, field_name="sequence_no")
        )
        object.__setattr__(
            self, "captured_at", ensure_aware_datetime(self.captured_at, field_name="captured_at")
        )
        object.__setattr__(
            self, "content", ensure_non_empty_text(self.content, field_name="content")
        )
