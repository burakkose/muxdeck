# ruff: noqa: PT009,PT027

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from copilot_commander.domain.enums import TaskPriority, TaskStatus
from copilot_commander.domain.task_models import Task
from copilot_commander.exceptions import DomainValidationError, PersistenceError
from copilot_commander.services.task_service import TaskService


class _FakeTaskStore:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}

    def upsert_task(self, task: Task, /) -> None:
        self.tasks[task.id] = task

    def get_task(self, task_id: str, /) -> Task | None:
        return self.tasks.get(task_id)

    def list_tasks(
        self,
        /,
        *,
        status: TaskStatus | None = None,
        assigned_agent_id: str | None = None,
        assigned_worktree_id: str | None = None,
        repo_root: str | None = None,
    ) -> tuple[Task, ...]:
        tasks = list(self.tasks.values())
        if status is not None:
            tasks = [task for task in tasks if task.status is status]
        if assigned_agent_id is not None:
            tasks = [task for task in tasks if task.assigned_agent_id == assigned_agent_id]
        if assigned_worktree_id is not None:
            tasks = [task for task in tasks if task.assigned_worktree_id == assigned_worktree_id]
        if repo_root is not None:
            tasks = [task for task in tasks if task.repo_root == repo_root]
        tasks.sort(key=lambda task: (task.created_at, task.id), reverse=True)
        return tuple(tasks)

    def delete_task(self, task_id: str, /) -> bool:
        return self.tasks.pop(task_id, None) is not None


class TaskServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        self.store = _FakeTaskStore()
        self.service = TaskService(store=self.store, clock=lambda: self.now)

    def test_create_assign_start_pause_resume_and_complete_task(self) -> None:
        created = self.service.create_task(
            title="Launch review flow",
            repo_root="/repo",
            priority=TaskPriority.HIGH,
        )
        assigned = self.service.assign_task(
            created.id,
            assigned_worktree_id="worktree-123",
        )
        running = self.service.start_task(
            created.id,
            assigned_agent_id="agent-123",
            started_at=self.now + timedelta(minutes=2),
        )
        paused = self.service.pause_task(created.id, notes="waiting on operator feedback")
        resumed = self.service.resume_task(created.id)
        completed = self.service.complete_task(
            created.id,
            completed_at=self.now + timedelta(minutes=10),
        )

        self.assertEqual(created.status, TaskStatus.PENDING)
        self.assertEqual(assigned.status, TaskStatus.ASSIGNED)
        self.assertEqual(assigned.assigned_worktree_id, "worktree-123")
        self.assertEqual(running.status, TaskStatus.RUNNING)
        self.assertEqual(running.assigned_agent_id, "agent-123")
        self.assertEqual(paused.status, TaskStatus.PAUSED)
        self.assertEqual(paused.notes, "waiting on operator feedback")
        self.assertEqual(resumed.status, TaskStatus.RUNNING)
        self.assertEqual(completed.status, TaskStatus.COMPLETED)
        self.assertEqual(completed.started_at, running.started_at)
        self.assertEqual(completed.completed_at, self.now + timedelta(minutes=10))
        self.assertEqual(self.service.get_task(created.id), completed)

    def test_list_helpers_and_terminal_transitions(self) -> None:
        pending = self.service.create_task(title="Pending inbox task")
        assigned = self.service.create_task(title="Assigned inbox task")
        assigned = self.service.assign_task(assigned.id, assigned_agent_id="agent-456")

        self.assertEqual(
            tuple(task.id for task in self.service.list_pending_tasks()),
            (pending.id,),
        )
        self.assertEqual(
            tuple(task.id for task in self.service.list_assigned_tasks()),
            (assigned.id,),
        )

        failed = self.service.fail_task(
            assigned.id,
            completed_at=self.now + timedelta(minutes=3),
            notes="bootstrap failed",
        )
        cancelled = self.service.cancel_task(
            pending.id,
            completed_at=self.now + timedelta(minutes=4),
        )

        self.assertEqual(failed.status, TaskStatus.FAILED)
        self.assertEqual(failed.notes, "bootstrap failed")
        self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
        self.assertEqual(self.service.list_assigned_tasks(), ())

    def test_rejects_invalid_transitions_and_unknown_tasks(self) -> None:
        created = self.service.create_task(title="Needs assignment")

        with self.assertRaises(DomainValidationError):
            self.service.start_task(created.id)
        with self.assertRaises(DomainValidationError):
            self.service.complete_task(created.id)
        with self.assertRaises(PersistenceError):
            self.service.get_task("task-missing")


if __name__ == "__main__":
    unittest.main()
