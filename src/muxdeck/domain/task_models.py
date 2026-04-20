from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from muxdeck.domain.enums import TaskPriority, TaskStatus
from muxdeck.domain.task_value_objects import TaskId, ensure_task_id
from muxdeck.domain.value_objects import (
    ensure_agent_id,
    ensure_aware_datetime,
    ensure_non_empty_text,
    ensure_worktree_id,
    utc_now,
)
from muxdeck.exceptions import DomainValidationError


def _generate_task_id() -> str:
    return str(TaskId.generate())


def _normalize_optional_text(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    return ensure_non_empty_text(value, field_name=field_name)


def _normalize_optional_agent_id(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    return str(ensure_agent_id(value, field_name=field_name))


def _normalize_optional_worktree_id(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    return str(ensure_worktree_id(value, field_name=field_name))


@dataclass(frozen=True, slots=True)
class Task:
    id: str = field(default_factory=_generate_task_id)
    title: str = ""
    summary: str | None = None
    description: str | None = None
    repo_root: str | None = None
    priority: TaskPriority = TaskPriority.NORMAL
    status: TaskStatus = TaskStatus.PENDING
    assigned_agent_id: str | None = None
    assigned_worktree_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", str(ensure_task_id(self.id, field_name="id")))
        object.__setattr__(self, "title", ensure_non_empty_text(self.title, field_name="title"))
        object.__setattr__(
            self,
            "summary",
            _normalize_optional_text(self.summary, field_name="summary"),
        )
        object.__setattr__(
            self,
            "description",
            _normalize_optional_text(self.description, field_name="description"),
        )
        object.__setattr__(
            self,
            "repo_root",
            _normalize_optional_text(self.repo_root, field_name="repo_root"),
        )
        if not isinstance(self.priority, TaskPriority):
            msg = "priority must be a TaskPriority"
            raise DomainValidationError(msg)
        if not isinstance(self.status, TaskStatus):
            msg = "status must be a TaskStatus"
            raise DomainValidationError(msg)
        object.__setattr__(
            self,
            "assigned_agent_id",
            _normalize_optional_agent_id(self.assigned_agent_id, field_name="assigned_agent_id"),
        )
        object.__setattr__(
            self,
            "assigned_worktree_id",
            _normalize_optional_worktree_id(
                self.assigned_worktree_id,
                field_name="assigned_worktree_id",
            ),
        )
        object.__setattr__(
            self,
            "created_at",
            ensure_aware_datetime(self.created_at, field_name="created_at"),
        )
        if self.started_at is not None:
            object.__setattr__(
                self,
                "started_at",
                ensure_aware_datetime(self.started_at, field_name="started_at"),
            )
            if self.started_at < self.created_at:
                msg = "started_at cannot precede created_at"
                raise DomainValidationError(msg)
        if self.completed_at is not None:
            object.__setattr__(
                self,
                "completed_at",
                ensure_aware_datetime(self.completed_at, field_name="completed_at"),
            )
            if self.completed_at < self.created_at:
                msg = "completed_at cannot precede created_at"
                raise DomainValidationError(msg)
            if self.started_at is not None and self.completed_at < self.started_at:
                msg = "completed_at cannot precede started_at"
                raise DomainValidationError(msg)
        object.__setattr__(self, "notes", _normalize_optional_text(self.notes, field_name="notes"))
        self._validate_status_invariants()

    def _validate_status_invariants(self) -> None:
        has_assignment = self.assigned_agent_id is not None or self.assigned_worktree_id is not None

        if self.status is TaskStatus.PENDING:
            if self.started_at is not None:
                msg = "pending tasks cannot have started_at"
                raise DomainValidationError(msg)
            if self.completed_at is not None:
                msg = "pending tasks cannot have completed_at"
                raise DomainValidationError(msg)
            return

        if self.status is TaskStatus.ASSIGNED:
            if not has_assignment:
                msg = "assigned tasks require an agent or worktree assignment"
                raise DomainValidationError(msg)
            if self.started_at is not None:
                msg = "assigned tasks cannot have started_at"
                raise DomainValidationError(msg)
            if self.completed_at is not None:
                msg = "assigned tasks cannot have completed_at"
                raise DomainValidationError(msg)
            return

        if self.status in (TaskStatus.RUNNING, TaskStatus.PAUSED):
            if not has_assignment:
                msg = f"{self.status.value} tasks require an agent or worktree assignment"
                raise DomainValidationError(msg)
            if self.started_at is None:
                msg = f"{self.status.value} tasks require started_at"
                raise DomainValidationError(msg)
            if self.completed_at is not None:
                msg = f"{self.status.value} tasks cannot have completed_at"
                raise DomainValidationError(msg)
            return

        if self.status is TaskStatus.COMPLETED:
            if self.started_at is None:
                msg = "completed tasks require started_at"
                raise DomainValidationError(msg)
            if self.completed_at is None:
                msg = "completed tasks require completed_at"
                raise DomainValidationError(msg)
            return

        if self.status in (TaskStatus.FAILED, TaskStatus.CANCELLED) and self.completed_at is None:
            msg = f"{self.status.value} tasks require completed_at"
            raise DomainValidationError(msg)
