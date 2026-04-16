from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from copilot_commander.domain.enums import TaskPriority, TaskStatus
from copilot_commander.domain.task_models import Task
from copilot_commander.domain.value_objects import utc_now
from copilot_commander.exceptions import DomainValidationError, PersistenceError
from copilot_commander.types import Clock


class TaskStorePort(Protocol):
    def upsert_task(self, task: Task, /) -> None: ...

    def get_task(self, task_id: str, /) -> Task | None: ...

    def list_tasks(
        self,
        /,
        *,
        status: TaskStatus | None = None,
        assigned_agent_id: str | None = None,
        assigned_worktree_id: str | None = None,
        repo_root: str | None = None,
    ) -> Sequence[Task]: ...

    def delete_task(self, task_id: str, /) -> bool: ...


class TaskService:
    def __init__(
        self,
        *,
        store: TaskStorePort,
        clock: Clock = utc_now,
    ) -> None:
        self._store = store
        self._clock = clock

    def create_task(
        self,
        *,
        title: str,
        summary: str | None = None,
        description: str | None = None,
        repo_root: str | None = None,
        priority: TaskPriority = TaskPriority.NORMAL,
        notes: str | None = None,
        created_at: datetime | None = None,
    ) -> Task:
        task = Task(
            title=title,
            summary=summary,
            description=description,
            repo_root=repo_root,
            priority=priority,
            status=TaskStatus.PENDING,
            created_at=created_at or self._clock(),
            notes=notes,
        )
        self._store.upsert_task(task)
        return task

    def get_task(self, task_id: str, /) -> Task:
        task = self._store.get_task(task_id)
        if task is None:
            msg = f"unknown task: {task_id}"
            raise PersistenceError(msg)
        return task

    def list_tasks(
        self,
        /,
        *,
        status: TaskStatus | None = None,
        assigned_agent_id: str | None = None,
        assigned_worktree_id: str | None = None,
        repo_root: str | None = None,
    ) -> tuple[Task, ...]:
        return tuple(
            self._store.list_tasks(
                status=status,
                assigned_agent_id=assigned_agent_id,
                assigned_worktree_id=assigned_worktree_id,
                repo_root=repo_root,
            )
        )

    def list_pending_tasks(self) -> tuple[Task, ...]:
        return self.list_tasks(status=TaskStatus.PENDING)

    def list_assigned_tasks(self) -> tuple[Task, ...]:
        return self.list_tasks(status=TaskStatus.ASSIGNED)

    def assign_task(
        self,
        task_id: str,
        /,
        *,
        assigned_agent_id: str | None = None,
        assigned_worktree_id: str | None = None,
        repo_root: str | None = None,
        notes: str | None = None,
    ) -> Task:
        task = self.get_task(task_id)
        self._ensure_status(
            task,
            allowed=(TaskStatus.PENDING, TaskStatus.ASSIGNED, TaskStatus.PAUSED),
            operation="assign task",
        )
        next_agent_id, next_worktree_id = self._resolve_assignment(
            task,
            assigned_agent_id=assigned_agent_id,
            assigned_worktree_id=assigned_worktree_id,
        )
        updated = replace(
            task,
            status=TaskStatus.ASSIGNED,
            assigned_agent_id=next_agent_id,
            assigned_worktree_id=next_worktree_id,
            repo_root=repo_root if repo_root is not None else task.repo_root,
            notes=notes if notes is not None else task.notes,
        )
        self._store.upsert_task(updated)
        return updated

    def start_task(
        self,
        task_id: str,
        /,
        *,
        started_at: datetime | None = None,
        assigned_agent_id: str | None = None,
        assigned_worktree_id: str | None = None,
        repo_root: str | None = None,
        notes: str | None = None,
    ) -> Task:
        task = self.get_task(task_id)
        self._ensure_status(
            task,
            allowed=(TaskStatus.PENDING, TaskStatus.ASSIGNED),
            operation="start task",
        )
        next_agent_id, next_worktree_id = self._resolve_assignment(
            task,
            assigned_agent_id=assigned_agent_id,
            assigned_worktree_id=assigned_worktree_id,
        )
        updated = replace(
            task,
            status=TaskStatus.RUNNING,
            assigned_agent_id=next_agent_id,
            assigned_worktree_id=next_worktree_id,
            repo_root=repo_root if repo_root is not None else task.repo_root,
            started_at=started_at or self._clock(),
            completed_at=None,
            notes=notes if notes is not None else task.notes,
        )
        self._store.upsert_task(updated)
        return updated

    def pause_task(
        self,
        task_id: str,
        /,
        *,
        notes: str | None = None,
    ) -> Task:
        task = self.get_task(task_id)
        self._ensure_status(task, allowed=(TaskStatus.RUNNING,), operation="pause task")
        updated = replace(
            task,
            status=TaskStatus.PAUSED,
            notes=notes if notes is not None else task.notes,
        )
        self._store.upsert_task(updated)
        return updated

    def resume_task(
        self,
        task_id: str,
        /,
        *,
        assigned_agent_id: str | None = None,
        assigned_worktree_id: str | None = None,
        notes: str | None = None,
    ) -> Task:
        task = self.get_task(task_id)
        self._ensure_status(task, allowed=(TaskStatus.PAUSED,), operation="resume task")
        next_agent_id, next_worktree_id = self._resolve_assignment(
            task,
            assigned_agent_id=assigned_agent_id,
            assigned_worktree_id=assigned_worktree_id,
        )
        updated = replace(
            task,
            status=TaskStatus.RUNNING,
            assigned_agent_id=next_agent_id,
            assigned_worktree_id=next_worktree_id,
            started_at=task.started_at or self._clock(),
            notes=notes if notes is not None else task.notes,
        )
        self._store.upsert_task(updated)
        return updated

    def complete_task(
        self,
        task_id: str,
        /,
        *,
        completed_at: datetime | None = None,
        notes: str | None = None,
    ) -> Task:
        task = self.get_task(task_id)
        self._ensure_status(
            task,
            allowed=(TaskStatus.RUNNING, TaskStatus.PAUSED),
            operation="complete task",
        )
        updated = replace(
            task,
            status=TaskStatus.COMPLETED,
            completed_at=completed_at or self._clock(),
            notes=notes if notes is not None else task.notes,
        )
        self._store.upsert_task(updated)
        return updated

    def fail_task(
        self,
        task_id: str,
        /,
        *,
        completed_at: datetime | None = None,
        notes: str | None = None,
    ) -> Task:
        task = self.get_task(task_id)
        self._ensure_status(
            task,
            allowed=(
                TaskStatus.PENDING,
                TaskStatus.ASSIGNED,
                TaskStatus.RUNNING,
                TaskStatus.PAUSED,
            ),
            operation="fail task",
        )
        updated = replace(
            task,
            status=TaskStatus.FAILED,
            completed_at=completed_at or self._clock(),
            notes=notes if notes is not None else task.notes,
        )
        self._store.upsert_task(updated)
        return updated

    def cancel_task(
        self,
        task_id: str,
        /,
        *,
        completed_at: datetime | None = None,
        notes: str | None = None,
    ) -> Task:
        task = self.get_task(task_id)
        self._ensure_status(
            task,
            allowed=(
                TaskStatus.PENDING,
                TaskStatus.ASSIGNED,
                TaskStatus.RUNNING,
                TaskStatus.PAUSED,
            ),
            operation="cancel task",
        )
        updated = replace(
            task,
            status=TaskStatus.CANCELLED,
            completed_at=completed_at or self._clock(),
            notes=notes if notes is not None else task.notes,
        )
        self._store.upsert_task(updated)
        return updated

    def _ensure_status(
        self,
        task: Task,
        *,
        allowed: tuple[TaskStatus, ...],
        operation: str,
    ) -> None:
        if task.status in allowed:
            return
        allowed_names = ", ".join(status.value for status in allowed)
        msg = (
            f"cannot {operation}: task {task.id} is {task.status.value} "
            f"(expected one of: {allowed_names})"
        )
        raise DomainValidationError(msg)

    def _resolve_assignment(
        self,
        task: Task,
        *,
        assigned_agent_id: str | None,
        assigned_worktree_id: str | None,
    ) -> tuple[str | None, str | None]:
        next_agent_id = (
            assigned_agent_id if assigned_agent_id is not None else task.assigned_agent_id
        )
        next_worktree_id = (
            assigned_worktree_id if assigned_worktree_id is not None else task.assigned_worktree_id
        )
        if next_agent_id is None and next_worktree_id is None:
            msg = "task execution requires assigned_agent_id or assigned_worktree_id"
            raise DomainValidationError(msg)
        return next_agent_id, next_worktree_id
