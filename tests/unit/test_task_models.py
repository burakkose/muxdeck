# ruff: noqa: PT009,PT027

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from muxdeck.domain.enums import TaskStatus
from muxdeck.domain.task_models import Task
from muxdeck.exceptions import DomainValidationError


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


class TaskModelInvariantTests(unittest.TestCase):
    """Cover the negative branches of Task.__post_init__ explicitly."""

    def test_priority_must_be_task_priority_enum(self) -> None:
        with self.assertRaises(DomainValidationError):
            Task(title="Bad priority", priority="urgent")  # type: ignore[arg-type]

    def test_status_must_be_task_status_enum(self) -> None:
        with self.assertRaises(DomainValidationError):
            Task(title="Bad status", status="running")  # type: ignore[arg-type]

    def test_completed_at_cannot_precede_created_at(self) -> None:
        created_at = datetime(2025, 1, 1, 12, tzinfo=UTC)
        with self.assertRaises(DomainValidationError):
            Task(
                title="Out-of-order completion",
                status=TaskStatus.FAILED,
                created_at=created_at,
                completed_at=created_at - timedelta(seconds=1),
            )

    def test_completed_at_cannot_precede_started_at(self) -> None:
        created_at = datetime(2025, 1, 1, 12, tzinfo=UTC)
        started_at = created_at + timedelta(minutes=5)
        with self.assertRaises(DomainValidationError):
            Task(
                title="Bad timing",
                status=TaskStatus.COMPLETED,
                assigned_agent_id="agent-1",
                created_at=created_at,
                started_at=started_at,
                completed_at=started_at - timedelta(seconds=1),
            )

    def test_pending_tasks_cannot_have_started_at(self) -> None:
        created_at = datetime(2025, 1, 1, 12, tzinfo=UTC)
        with self.assertRaises(DomainValidationError):
            Task(
                title="Pending+started",
                status=TaskStatus.PENDING,
                created_at=created_at,
                started_at=created_at + timedelta(seconds=1),
            )

    def test_pending_tasks_cannot_have_completed_at(self) -> None:
        created_at = datetime(2025, 1, 1, 12, tzinfo=UTC)
        with self.assertRaises(DomainValidationError):
            Task(
                title="Pending+completed",
                status=TaskStatus.PENDING,
                created_at=created_at,
                completed_at=created_at + timedelta(seconds=1),
            )

    def test_assigned_tasks_require_agent_or_worktree(self) -> None:
        with self.assertRaises(DomainValidationError):
            Task(title="Assigned no-one", status=TaskStatus.ASSIGNED)

    def test_assigned_tasks_cannot_have_started_at(self) -> None:
        created_at = datetime(2025, 1, 1, 12, tzinfo=UTC)
        with self.assertRaises(DomainValidationError):
            Task(
                title="Assigned+started",
                status=TaskStatus.ASSIGNED,
                assigned_agent_id="agent-1",
                created_at=created_at,
                started_at=created_at + timedelta(seconds=1),
            )

    def test_assigned_tasks_cannot_have_completed_at(self) -> None:
        created_at = datetime(2025, 1, 1, 12, tzinfo=UTC)
        with self.assertRaises(DomainValidationError):
            Task(
                title="Assigned+completed",
                status=TaskStatus.ASSIGNED,
                assigned_agent_id="agent-1",
                created_at=created_at,
                completed_at=created_at + timedelta(seconds=1),
            )

    def test_running_tasks_cannot_have_completed_at(self) -> None:
        created_at = datetime(2025, 1, 1, 12, tzinfo=UTC)
        started_at = created_at + timedelta(minutes=1)
        with self.assertRaises(DomainValidationError):
            Task(
                title="Running+completed",
                status=TaskStatus.RUNNING,
                assigned_agent_id="agent-1",
                created_at=created_at,
                started_at=started_at,
                completed_at=started_at + timedelta(minutes=1),
            )

    def test_paused_tasks_must_be_assigned_and_started(self) -> None:
        created_at = datetime(2025, 1, 1, 12, tzinfo=UTC)
        with self.assertRaises(DomainValidationError):
            Task(
                title="Paused no assignment",
                status=TaskStatus.PAUSED,
                created_at=created_at,
                started_at=created_at + timedelta(minutes=1),
            )
        with self.assertRaises(DomainValidationError):
            Task(
                title="Paused no started_at",
                status=TaskStatus.PAUSED,
                assigned_agent_id="agent-1",
                created_at=created_at,
            )

    def test_completed_requires_completed_at(self) -> None:
        created_at = datetime(2025, 1, 1, 12, tzinfo=UTC)
        started_at = created_at + timedelta(minutes=1)
        with self.assertRaises(DomainValidationError):
            Task(
                title="Completed no completed_at",
                status=TaskStatus.COMPLETED,
                assigned_agent_id="agent-1",
                created_at=created_at,
                started_at=started_at,
            )

    def test_cancelled_requires_completed_at(self) -> None:
        with self.assertRaises(DomainValidationError):
            Task(title="Cancelled no completed_at", status=TaskStatus.CANCELLED)

    def test_started_at_cannot_precede_created_at(self) -> None:
        created_at = datetime(2025, 1, 1, 12, tzinfo=UTC)
        with self.assertRaises(DomainValidationError):
            Task(
                title="Out-of-order start",
                status=TaskStatus.RUNNING,
                assigned_agent_id="agent-1",
                created_at=created_at,
                started_at=created_at - timedelta(seconds=1),
            )


if __name__ == "__main__":
    unittest.main()
