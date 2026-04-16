# ruff: noqa: E402,ANN001,ANN201
"""Tests for dashboard send-message and view-logs action methods."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from copilot_commander.bindings import DASHBOARD_BINDINGS, WORKTREE_BINDINGS


class TestDashboardBindings(unittest.TestCase):
    """Verify that new bindings exist in the binding lists."""

    def _binding_actions(self, bindings):
        return [b.action for b in bindings]

    def test_send_message_binding_exists(self) -> None:
        actions = self._binding_actions(DASHBOARD_BINDINGS)
        self.assertIn("send_message", actions)

    def test_view_logs_binding_exists(self) -> None:
        actions = self._binding_actions(DASHBOARD_BINDINGS)
        self.assertIn("view_logs", actions)

    def test_worktree_delete_binding_exists(self) -> None:
        actions = self._binding_actions(WORKTREE_BINDINGS)
        self.assertIn("delete_worktree", actions)

    def test_worktree_prune_binding_exists(self) -> None:
        actions = self._binding_actions(WORKTREE_BINDINGS)
        self.assertIn("prune_worktrees", actions)


class TestDashboardBindingKeys(unittest.TestCase):
    """Verify key assignments for critical bindings."""

    def _key_map(self, bindings):
        return {b.action: b.key for b in bindings}

    def test_send_message_key_is_m(self) -> None:
        km = self._key_map(DASHBOARD_BINDINGS)
        self.assertEqual(km["send_message"], "m")

    def test_view_logs_key_is_l(self) -> None:
        km = self._key_map(DASHBOARD_BINDINGS)
        self.assertEqual(km["view_logs"], "l")

    def test_worktree_delete_key_is_d(self) -> None:
        km = self._key_map(WORKTREE_BINDINGS)
        self.assertEqual(km["delete_worktree"], "d")

    def test_worktree_prune_key_is_P(self) -> None:
        km = self._key_map(WORKTREE_BINDINGS)
        self.assertEqual(km["prune_worktrees"], "P")


if __name__ == "__main__":
    unittest.main()
