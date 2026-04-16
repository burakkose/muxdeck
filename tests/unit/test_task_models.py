# ruff: noqa: PT009,PT027

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from copilot_commander.domain.enums import TaskStatus
from copilot_commander.domain.task_models import Task
from copilot_commander.exceptions import DomainValidationError


class TaskModelTests(unittest.TestCase):
    def test_defaults_generate_pending_task_with_normalized_text(self) -> None:
        task = Task(
            title="  Review merge lane  ",
            summary="  Summarize checks  ",
            description="  Capture CI state before merge  ",
            repo_root="  /repo/main  ",
            notes="  waiting for approval  ",
        )

        self.assertTrue(task.id.startswith("task-"))
        self.assertEqual(task.title, "Review merge lane")
        self.assertEqual(task.summary, "Summarize checks")
        self.assertEqual(task.description, "Capture CI state before merge")
        self.assertEqual(task.repo_root, "/repo/main")
        self.assertEqual(task.notes, "waiting for approval")
        self.assertEqual(task.status, TaskStatus.PENDING)
        self.assertIsNone(task.started_at)
        self.assertIsNone(task.completed_at)
        self.assertEqual(task.created_at.tzinfo, UTC)

    def test_running_tasks_require_assignment_and_started_at(self) -> None:
        created_at = datetime(2025, 1, 1, 11, 55, tzinfo=UTC)
        started_at = datetime(2025, 1, 1, 12, tzinfo=UTC)
        task = Task(
            title="Launch harness",
            status=TaskStatus.RUNNING,
            assigned_agent_id="agent-123",
            created_at=created_at,
            started_at=started_at,
        )

        self.assertEqual(task.status, TaskStatus.RUNNING)
        self.assertEqual(task.assigned_agent_id, "agent-123")
        self.assertEqual(task.started_at, started_at)

        with self.assertRaises(DomainValidationError):
            Task(
                title="Broken running task",
                status=TaskStatus.RUNNING,
                assigned_agent_id="agent-123",
            )
        with self.assertRaises(DomainValidationError):
            Task(
                title="Missing assignment",
                status=TaskStatus.RUNNING,
                started_at=started_at,
            )

    def test_terminal_states_validate_completion_timestamps(self) -> None:
        created_at = datetime(2025, 1, 1, 12, tzinfo=UTC)
        started_at = created_at + timedelta(minutes=5)
        completed_at = started_at + timedelta(minutes=20)

        completed = Task(
            title="Complete review",
            status=TaskStatus.COMPLETED,
            assigned_worktree_id="worktree-123",
            created_at=created_at,
            started_at=started_at,
            completed_at=completed_at,
        )
        failed = Task(
            title="Setup failure",
            status=TaskStatus.FAILED,
            created_at=created_at,
            completed_at=created_at + timedelta(minutes=1),
        )

        self.assertEqual(completed.status, TaskStatus.COMPLETED)
        self.assertEqual(failed.status, TaskStatus.FAILED)

        with self.assertRaises(DomainValidationError):
            Task(
                title="Missing started_at",
                status=TaskStatus.COMPLETED,
                completed_at=completed_at,
            )
        with self.assertRaises(DomainValidationError):
            Task(
                title="Bad ordering",
                status=TaskStatus.FAILED,
                created_at=created_at,
                completed_at=created_at - timedelta(minutes=1),
            )


if __name__ == "__main__":
    unittest.main()
