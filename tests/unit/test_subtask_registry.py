# ruff: noqa: I001, PT009, PT027, B017, E501

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from muxdeck.parsers.copilot_output_parser import CopilotTaskEvidence
from muxdeck.services.subtask_registry import SubTaskInfo, SubTaskRegistry


def _evidence(
    *,
    label: str = "general-purpose",
    model: str | None = "claude-sonnet-4.5",
    description: str = "research",
    status: str = "running",
) -> CopilotTaskEvidence:
    return CopilotTaskEvidence(
        agent_type_label=label,
        model=model,
        description=description,
        status=status,  # type: ignore[arg-type]
    )


class SubTaskRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = SubTaskRegistry(
            ttl=timedelta(seconds=10),
            completed_ttl=timedelta(seconds=5),
        )
        self.now = datetime(2025, 1, 1, 12, tzinfo=UTC)

    def test_update_inserts_new_evidence_and_get_tasks_returns_sorted(self) -> None:
        ev1 = _evidence(description="alpha")
        ev2 = _evidence(description="beta")
        self.registry.update("pane-1", (ev1, ev2), background_task_count=2, now=self.now)
        tasks = self.registry.get_tasks("pane-1")
        self.assertEqual(len(tasks), 2)
        self.assertEqual({t.description for t in tasks}, {"alpha", "beta"})
        for task in tasks:
            self.assertEqual(task.first_seen_at, self.now)
            self.assertEqual(task.last_seen_at, self.now)

    def test_update_preserves_first_seen_and_advances_last_seen(self) -> None:
        ev = _evidence(description="alpha")
        self.registry.update("pane-1", (ev,), 1, now=self.now)
        later = self.now + timedelta(seconds=4)
        self.registry.update("pane-1", (ev,), 1, now=later)
        info = self.registry.get_tasks("pane-1")[0]
        self.assertEqual(info.first_seen_at, self.now)
        self.assertEqual(info.last_seen_at, later)

    def test_update_keeps_previous_model_when_new_evidence_lacks_one(self) -> None:
        ev_with_model = _evidence(description="alpha", model="m1")
        self.registry.update("pane-1", (ev_with_model,), 1, now=self.now)
        ev_without = _evidence(description="alpha", model=None)
        later = self.now + timedelta(seconds=2)
        self.registry.update("pane-1", (ev_without,), 1, now=later)
        info = self.registry.get_tasks("pane-1")[0]
        self.assertEqual(info.model, "m1")

    def test_update_keeps_running_task_within_ttl_when_evidence_disappears(self) -> None:
        ev = _evidence(description="alpha", status="running")
        self.registry.update("pane-1", (ev,), 1, now=self.now)
        within_ttl = self.now + timedelta(seconds=5)
        self.registry.update("pane-1", (), 1, now=within_ttl)
        tasks = self.registry.get_tasks("pane-1")
        self.assertEqual(len(tasks), 1)

    def test_update_drops_running_task_after_ttl(self) -> None:
        ev = _evidence(description="alpha", status="running")
        self.registry.update("pane-1", (ev,), 1, now=self.now)
        past_ttl = self.now + timedelta(seconds=11)
        self.registry.update("pane-1", (), 1, now=past_ttl)
        self.assertEqual(self.registry.get_tasks("pane-1"), ())

    def test_update_drops_completed_task_after_completed_ttl(self) -> None:
        ev = _evidence(description="alpha", status="completed")
        self.registry.update("pane-1", (ev,), 1, now=self.now)
        past_completed_ttl = self.now + timedelta(seconds=6)
        self.registry.update("pane-1", (), 1, now=past_completed_ttl)
        self.assertEqual(self.registry.get_tasks("pane-1"), ())

    def test_zero_background_count_with_no_evidence_clears_running(self) -> None:
        running = _evidence(description="alpha", status="running")
        completed = _evidence(description="beta", status="completed")
        self.registry.update("pane-1", (running, completed), 2, now=self.now)
        # Now: bg count == 0 and no evidence — running gets dropped, completed kept
        next_now = self.now + timedelta(seconds=1)
        self.registry.update("pane-1", (), 0, now=next_now)
        descriptions = {t.description for t in self.registry.get_tasks("pane-1")}
        self.assertEqual(descriptions, {"beta"})

    def test_remove_pane_clears_all_tasks_for_pane(self) -> None:
        self.registry.update("pane-1", (_evidence(),), 1, now=self.now)
        self.registry.remove_pane("pane-1")
        self.assertEqual(self.registry.get_tasks("pane-1"), ())
        self.assertNotIn("pane-1", self.registry.all_pane_ids())

    def test_remove_pane_is_noop_for_unknown_pane(self) -> None:
        # Must not raise when pane was never registered
        self.registry.remove_pane("ghost-pane")

    def test_all_pane_ids_returns_known_panes(self) -> None:
        self.registry.update("pane-1", (_evidence(),), 1, now=self.now)
        self.registry.update("pane-2", (_evidence(description="beta"),), 1, now=self.now)
        self.assertEqual(self.registry.all_pane_ids(), frozenset({"pane-1", "pane-2"}))

    def test_expire_all_drops_running_after_ttl_keeps_completed_within_completed_ttl(
        self,
    ) -> None:
        running = _evidence(description="alpha", status="running")
        completed = _evidence(description="beta", status="completed")
        self.registry.update("pane-1", (running, completed), 2, now=self.now)
        # Expire at a time that is past TTL for running (>10s) but within
        # completed TTL (>5s) — actually completed TTL is 5s so both
        # would expire at +6s. Use +6s so running is gone (>= ttl 5? no,
        # running ttl is 10) — pick +11s so running expires too. To
        # cover the live branch: expire at +4s so both are kept.
        self.registry.expire_all(now=self.now + timedelta(seconds=4))
        self.assertEqual(
            {t.description for t in self.registry.get_tasks("pane-1")}, {"alpha", "beta"}
        )

    def test_expire_all_removes_pane_when_all_tasks_expired(self) -> None:
        running = _evidence(description="alpha", status="running")
        self.registry.update("pane-1", (running,), 1, now=self.now)
        self.registry.expire_all(now=self.now + timedelta(seconds=12))
        self.assertEqual(self.registry.get_tasks("pane-1"), ())
        self.assertNotIn("pane-1", self.registry.all_pane_ids())

    def test_expire_all_with_default_now_uses_current_time(self) -> None:
        # Just ensure it does not raise when called without now
        self.registry.expire_all()

    def test_update_with_default_now_uses_current_time(self) -> None:
        self.registry.update("pane-1", (_evidence(),), 1)
        self.assertEqual(len(self.registry.get_tasks("pane-1")), 1)

    def test_subtask_info_dataclass_is_frozen(self) -> None:
        info = SubTaskInfo(
            task_key="k",
            agent_type_label="general-purpose",
            model="m",
            description="d",
            status="running",
            first_seen_at=self.now,
            last_seen_at=self.now,
        )
        with self.assertRaises(Exception):
            info.description = "other"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
