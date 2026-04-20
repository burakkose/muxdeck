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

from muxdeck.bindings import (
    DASHBOARD_BINDINGS,
    SESSIONS_BINDINGS,
    WORKTREE_BINDINGS,
    BindingSpec,
)


class TestDashboardBindings(unittest.TestCase):
    """Verify that new bindings exist in the binding lists."""

    def _binding_actions(self, bindings: list[BindingSpec]) -> list[str]:
        actions: list[str] = []
        for binding in bindings:
            if isinstance(binding, Binding):
                actions.append(binding.action)
            else:
                actions.append(binding[1])
        return actions

    def test_send_message_binding_exists(self) -> None:
        actions = self._binding_actions(DASHBOARD_BINDINGS)
        assert "send_message" in actions

    def test_view_logs_binding_exists(self) -> None:
        actions = self._binding_actions(DASHBOARD_BINDINGS)
        assert "view_logs" in actions

    def test_rename_window_binding_exists(self) -> None:
        actions = self._binding_actions(DASHBOARD_BINDINGS)
        assert "rename_window" in actions

    def test_move_to_window_binding_exists(self) -> None:
        actions = self._binding_actions(DASHBOARD_BINDINGS)
        assert "move_to_window" in actions

    def test_kill_agent_binding_exists(self) -> None:
        actions = self._binding_actions(DASHBOARD_BINDINGS)
        assert "kill_agent" in actions

    def test_dashboard_copy_details_binding_exists(self) -> None:
        actions = self._binding_actions(DASHBOARD_BINDINGS)
        assert "copy_details" in actions

    def test_worktree_delete_binding_exists(self) -> None:
        actions = self._binding_actions(WORKTREE_BINDINGS)
        assert "delete_worktree" in actions

    def test_worktree_copy_details_binding_exists(self) -> None:
        actions = self._binding_actions(WORKTREE_BINDINGS)
        assert "copy_details" in actions

    def test_sessions_copy_details_binding_exists(self) -> None:
        actions = self._binding_actions(SESSIONS_BINDINGS)
        assert "copy_details" in actions

    def test_worktree_launch_binding_exists(self) -> None:
        actions = self._binding_actions(WORKTREE_BINDINGS)
        assert "launch_agent" in actions

    def test_worktree_create_binding_exists(self) -> None:
        actions = self._binding_actions(WORKTREE_BINDINGS)
        assert "create_worktree" in actions

    def test_worktree_attach_binding_exists(self) -> None:
        actions = self._binding_actions(WORKTREE_BINDINGS)
        assert "attach_worktree" in actions

    def test_worktree_prune_binding_exists(self) -> None:
        actions = self._binding_actions(WORKTREE_BINDINGS)
        assert "prune_worktrees" in actions


class TestDashboardBindingKeys(unittest.TestCase):
    """Verify key assignments for critical bindings."""

    def _key_map(self, bindings: list[BindingSpec]) -> dict[str, str]:
        key_map: dict[str, str] = {}
        for binding in bindings:
            if isinstance(binding, Binding):
                key_map[binding.action] = binding.key
            elif len(binding) == 2:
                key_map[binding[1]] = binding[0]
            else:
                key_map[binding[1]] = binding[0]
        return key_map

    def test_send_message_key_is_m(self) -> None:
        km = self._key_map(DASHBOARD_BINDINGS)
        assert km["send_message"] == "m"

    def test_view_logs_key_is_l(self) -> None:
        km = self._key_map(DASHBOARD_BINDINGS)
        assert km["view_logs"] == "l"

    def test_rename_window_key_is_shift_r(self) -> None:
        km = self._key_map(DASHBOARD_BINDINGS)
        assert km["rename_window"] == "R"

    def test_move_to_window_key_is_shift_w(self) -> None:
        km = self._key_map(DASHBOARD_BINDINGS)
        assert km["move_to_window"] == "W"

    def test_kill_agent_key_is_shift_k(self) -> None:
        km = self._key_map(DASHBOARD_BINDINGS)
        assert km["kill_agent"] == "K"

    def test_dashboard_copy_details_key_is_y(self) -> None:
        km = self._key_map(DASHBOARD_BINDINGS)
        assert km["copy_details"] == "y"

    def test_worktree_delete_key_is_d(self) -> None:
        km = self._key_map(WORKTREE_BINDINGS)
        assert km["delete_worktree"] == "d"

    def test_worktree_copy_details_key_is_y(self) -> None:
        km = self._key_map(WORKTREE_BINDINGS)
        assert km["copy_details"] == "y"

    def test_sessions_copy_details_key_is_y(self) -> None:
        km = self._key_map(SESSIONS_BINDINGS)
        assert km["copy_details"] == "y"

    def test_worktree_create_key_is_c(self) -> None:
        km = self._key_map(WORKTREE_BINDINGS)
        assert km["create_worktree"] == "c"

    def test_worktree_attach_key_is_a(self) -> None:
        km = self._key_map(WORKTREE_BINDINGS)
        assert km["attach_worktree"] == "a"

    def test_worktree_prune_key_is_shift_p(self) -> None:
        km = self._key_map(WORKTREE_BINDINGS)
        assert km["prune_worktrees"] == "P"


if __name__ == "__main__":
    unittest.main()
