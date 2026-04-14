from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.value_objects import (
    AgentId,
    SessionId,
    WorktreeId,
    ensure_aware_datetime,
    ensure_non_empty_text,
    ensure_non_negative_decimal,
    ensure_non_negative_int,
    utc_now,
)
from copilot_commander.exceptions import DomainValidationError

_ALLOWED_BACKEND = "copilot_cli"


def _generate_agent_id() -> str:
    return str(AgentId.generate())


def _generate_worktree_id() -> str:
    return str(WorktreeId.generate())


def _generate_session_id() -> str:
    return str(SessionId.generate())


def _normalize_id(value: str | AgentId | SessionId | WorktreeId, *, field_name: str) -> str:
    if isinstance(value, AgentId | SessionId | WorktreeId):
        return str(value)
    return ensure_non_empty_text(value, field_name=field_name)


def _normalize_optional_text(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    return ensure_non_empty_text(value, field_name=field_name)


def _normalize_optional_pid(value: int | None, *, field_name: str) -> int | None:
    if value is None:
        return None
    return ensure_non_negative_int(value, field_name=field_name)


def _normalize_optional_tokens(value: int | None, *, field_name: str) -> int | None:
    if value is None:
        return None
    return ensure_non_negative_int(value, field_name=field_name)


def _normalize_optional_decimal(
    value: Decimal | str | int | float | None, *, field_name: str
) -> Decimal | None:
    if value is None:
        return None
    return ensure_non_negative_decimal(value, field_name=field_name, quantize=None)


@dataclass(frozen=True, slots=True)
class Agent:
    id: str = field(default_factory=_generate_agent_id)
    name: str = ""
    backend: Literal["copilot_cli"] = "copilot_cli"
    tmux_session_name: str = ""
    tmux_window_id: str = ""
    tmux_window_name: str | None = None
    tmux_pane_id: str = ""
    pane_tty: str | None = None
    cwd: str = ""
    repo_root: str | None = None
    worktree_path: str | None = None
    branch: str | None = None
    task_title: str | None = None
    task_summary: str | None = None
    copilot_session_id: str | None = None
    pid: int | None = None
    status: AgentStatus = AgentStatus.UNKNOWN
    started_at: datetime = field(default_factory=utc_now)
    last_activity_at: datetime | None = None
    last_seen_at: datetime = field(default_factory=utc_now)
    idle_seconds: int = 0
    needs_attention: bool = False
    attention_reason: str | None = None
    token_input: int | None = None
    token_output: int | None = None
    token_total: int | None = None
    estimated_cost_usd: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _normalize_id(self.id, field_name="id"))
        object.__setattr__(self, "name", ensure_non_empty_text(self.name, field_name="name"))
        if self.backend != _ALLOWED_BACKEND:
            msg = f"backend must be '{_ALLOWED_BACKEND}'"
            raise DomainValidationError(msg)
        object.__setattr__(
            self,
            "tmux_session_name",
            ensure_non_empty_text(self.tmux_session_name, field_name="tmux_session_name"),
        )
        object.__setattr__(
            self,
            "tmux_window_id",
            ensure_non_empty_text(self.tmux_window_id, field_name="tmux_window_id"),
        )
        object.__setattr__(
            self,
            "tmux_window_name",
            _normalize_optional_text(self.tmux_window_name, field_name="tmux_window_name"),
        )
        object.__setattr__(
            self,
            "tmux_pane_id",
            ensure_non_empty_text(self.tmux_pane_id, field_name="tmux_pane_id"),
        )
        object.__setattr__(
            self, "pane_tty", _normalize_optional_text(self.pane_tty, field_name="pane_tty")
        )
        object.__setattr__(self, "cwd", ensure_non_empty_text(self.cwd, field_name="cwd"))
        object.__setattr__(
            self, "repo_root", _normalize_optional_text(self.repo_root, field_name="repo_root")
        )
        object.__setattr__(
            self,
            "worktree_path",
            _normalize_optional_text(self.worktree_path, field_name="worktree_path"),
        )
        object.__setattr__(
            self, "branch", _normalize_optional_text(self.branch, field_name="branch")
        )
        object.__setattr__(
            self,
            "task_title",
            _normalize_optional_text(self.task_title, field_name="task_title"),
        )
        object.__setattr__(
            self,
            "task_summary",
            _normalize_optional_text(self.task_summary, field_name="task_summary"),
        )
        object.__setattr__(
            self,
            "copilot_session_id",
            _normalize_optional_text(self.copilot_session_id, field_name="copilot_session_id"),
        )
        object.__setattr__(self, "pid", _normalize_optional_pid(self.pid, field_name="pid"))
        if not isinstance(self.status, AgentStatus):
            msg = "status must be an AgentStatus"
            raise DomainValidationError(msg)
        object.__setattr__(
            self, "started_at", ensure_aware_datetime(self.started_at, field_name="started_at")
        )
        if self.last_activity_at is not None:
            object.__setattr__(
                self,
                "last_activity_at",
                ensure_aware_datetime(self.last_activity_at, field_name="last_activity_at"),
            )
            if self.last_activity_at < self.started_at:
                msg = "last_activity_at cannot precede started_at"
                raise DomainValidationError(msg)
        object.__setattr__(
            self,
            "last_seen_at",
            ensure_aware_datetime(self.last_seen_at, field_name="last_seen_at"),
        )
        if self.last_seen_at < self.started_at:
            msg = "last_seen_at cannot precede started_at"
            raise DomainValidationError(msg)
        if self.last_activity_at is not None and self.last_seen_at < self.last_activity_at:
            msg = "last_seen_at cannot precede last_activity_at"
            raise DomainValidationError(msg)
        object.__setattr__(
            self,
            "idle_seconds",
            ensure_non_negative_int(self.idle_seconds, field_name="idle_seconds"),
        )
        if self.attention_reason is not None and not self.needs_attention:
            msg = "attention_reason requires needs_attention to be true"
            raise DomainValidationError(msg)
        object.__setattr__(
            self,
            "attention_reason",
            _normalize_optional_text(self.attention_reason, field_name="attention_reason"),
        )
        object.__setattr__(
            self,
            "token_input",
            _normalize_optional_tokens(self.token_input, field_name="token_input"),
        )
        object.__setattr__(
            self,
            "token_output",
            _normalize_optional_tokens(self.token_output, field_name="token_output"),
        )
        object.__setattr__(
            self,
            "token_total",
            _normalize_optional_tokens(self.token_total, field_name="token_total"),
        )
        if (
            self.token_total is not None
            and self.token_input is not None
            and self.token_output is not None
            and self.token_total != self.token_input + self.token_output
        ):
            msg = "token_total must equal token_input + token_output"
            raise DomainValidationError(msg)
        object.__setattr__(
            self,
            "estimated_cost_usd",
            _normalize_optional_decimal(self.estimated_cost_usd, field_name="estimated_cost_usd"),
        )


@dataclass(frozen=True, slots=True)
class Worktree:
    id: str = field(default_factory=_generate_worktree_id)
    repo_root: str = ""
    path: str = ""
    branch: str = ""
    base_branch: str | None = None
    is_main_worktree: bool = False
    is_dirty: bool = False
    ahead_count: int | None = None
    behind_count: int | None = None
    locked: bool = False
    assigned_agent_id: str | None = None
    created_at: datetime | None = None
    last_seen_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _normalize_id(self.id, field_name="id"))
        object.__setattr__(
            self, "repo_root", ensure_non_empty_text(self.repo_root, field_name="repo_root")
        )
        object.__setattr__(self, "path", ensure_non_empty_text(self.path, field_name="path"))
        object.__setattr__(self, "branch", ensure_non_empty_text(self.branch, field_name="branch"))
        object.__setattr__(
            self,
            "base_branch",
            _normalize_optional_text(self.base_branch, field_name="base_branch"),
        )
        object.__setattr__(
            self,
            "ahead_count",
            _normalize_optional_pid(self.ahead_count, field_name="ahead_count"),
        )
        object.__setattr__(
            self,
            "behind_count",
            _normalize_optional_pid(self.behind_count, field_name="behind_count"),
        )
        if self.assigned_agent_id is not None:
            object.__setattr__(
                self,
                "assigned_agent_id",
                _normalize_id(self.assigned_agent_id, field_name="assigned_agent_id"),
            )
        if self.created_at is not None:
            object.__setattr__(
                self,
                "created_at",
                ensure_aware_datetime(self.created_at, field_name="created_at"),
            )
        object.__setattr__(
            self,
            "last_seen_at",
            ensure_aware_datetime(self.last_seen_at, field_name="last_seen_at"),
        )
        if self.created_at is not None and self.last_seen_at < self.created_at:
            msg = "last_seen_at cannot precede created_at"
            raise DomainValidationError(msg)


@dataclass(frozen=True, slots=True)
class Session:
    id: str = field(default_factory=_generate_session_id)
    agent_id: str = field(default_factory=_generate_agent_id)
    copilot_session_id: str | None = None
    task_title: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    ended_at: datetime | None = None
    exit_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _normalize_id(self.id, field_name="id"))
        object.__setattr__(self, "agent_id", _normalize_id(self.agent_id, field_name="agent_id"))
        object.__setattr__(
            self,
            "copilot_session_id",
            _normalize_optional_text(self.copilot_session_id, field_name="copilot_session_id"),
        )
        object.__setattr__(
            self,
            "task_title",
            _normalize_optional_text(self.task_title, field_name="task_title"),
        )
        object.__setattr__(
            self, "created_at", ensure_aware_datetime(self.created_at, field_name="created_at")
        )
        if self.ended_at is not None:
            object.__setattr__(
                self, "ended_at", ensure_aware_datetime(self.ended_at, field_name="ended_at")
            )
            if self.ended_at < self.created_at:
                msg = "ended_at cannot precede created_at"
                raise DomainValidationError(msg)
        object.__setattr__(
            self,
            "exit_reason",
            _normalize_optional_text(self.exit_reason, field_name="exit_reason"),
        )
