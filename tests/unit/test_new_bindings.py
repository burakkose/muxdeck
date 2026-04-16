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

from textual.binding import Binding

from copilot_commander.bindings import DASHBOARD_BINDINGS, WORKTREE_BINDINGS


class TestDashboardBindings(unittest.TestCase):
    """Verify that new bindings exist in the binding lists."""

    def _binding_actions(self, bindings: list[Binding]) -> list[str]:
        return [b.action for b in bindings]

    def test_send_message_binding_exists(self) -> None:
        actions = self._binding_actions(DASHBOARD_BINDINGS)
        assert "send_message" in actions

    def test_view_logs_binding_exists(self) -> None:
        actions = self._binding_actions(DASHBOARD_BINDINGS)
        assert "view_logs" in actions

    def test_worktree_delete_binding_exists(self) -> None:
        actions = self._binding_actions(WORKTREE_BINDINGS)
        assert "delete_worktree" in actions

    def test_worktree_prune_binding_exists(self) -> None:
        actions = self._binding_actions(WORKTREE_BINDINGS)
        assert "prune_worktrees" in actions


class TestDashboardBindingKeys(unittest.TestCase):
    """Verify key assignments for critical bindings."""

    def _key_map(self, bindings: list[Binding]) -> dict[str, str]:
        return {b.action: b.key for b in bindings}

    def test_send_message_key_is_m(self) -> None:
        km = self._key_map(DASHBOARD_BINDINGS)
        assert km["send_message"] == "m"

    def test_view_logs_key_is_l(self) -> None:
        km = self._key_map(DASHBOARD_BINDINGS)
        assert km["view_logs"] == "l"

    def test_worktree_delete_key_is_d(self) -> None:
        km = self._key_map(WORKTREE_BINDINGS)
        assert km["delete_worktree"] == "d"

    def test_worktree_prune_key_is_shift_p(self) -> None:
        km = self._key_map(WORKTREE_BINDINGS)
        assert km["prune_worktrees"] == "P"


if __name__ == "__main__":
    unittest.main()
