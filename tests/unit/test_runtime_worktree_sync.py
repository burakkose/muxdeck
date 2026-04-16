# ruff: noqa: E402,ANN001,ANN201
"""Tests for RuntimeSynchronizer worktree sync integration."""

from __future__ import annotations

import sys
import unittest
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from typing import Never

from copilot_commander.adapters.copilot_adapter import CopilotCommandDetection
from copilot_commander.services.discovery_service import (
    DiscoveryPaneSnapshot,
    PaneDiscovery,
    PaneDiscoveryReport,
)
from copilot_commander.services.monitoring_service import (
    MonitoringDiscovery,
    MonitoringReport,
)
from copilot_commander.services.runtime_service import RuntimeSynchronizer


class FakeDiscovery:
    def __init__(self, report: PaneDiscoveryReport) -> None:
        self._report = report

    def discover_panes(self) -> PaneDiscoveryReport:
        return self._report


class FakeMonitoring:
    def __init__(self, monitored_at: datetime) -> None:
        self.monitored_at = monitored_at

    def monitor_discoveries(
        self,
        discoveries: Sequence[MonitoringDiscovery],
        /,
    ) -> MonitoringReport:
        return MonitoringReport(monitored_at=self.monitored_at, results=())


class FakeGit:
    def __init__(self, repo_root: str = "/repo", branch: str | None = "main") -> None:
        self.repo_root = Path(repo_root)
        self.branch = branch

    def discover_repo_root(self, cwd: str | Path, /) -> Path:
        return self.repo_root

    def current_branch(self, cwd: str | Path, /) -> str | None:
        return self.branch


class FakeWorktreeSync:
    def __init__(self) -> None:
        self.called_with: list[Sequence[Path]] = []

    def sync_worktrees_from_git(
        self,
        repo_roots: Sequence[Path],
    ) -> object:
        self.called_with.append(list(repo_roots))
        return None


def _make_pane(
    pane_id: str = "%7",
    cwd: str = "/repo/worktrees/task",
) -> PaneDiscovery:
    return PaneDiscovery(
        snapshot=DiscoveryPaneSnapshot(
            pane_id=pane_id,
            tmux_session_name="session",
            tmux_window_id="@1",
            tmux_window_name="work",
            pane_current_path=cwd,
            pane_current_command="copilot chat",
        ),
        discovered_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        classification="unmanaged_probable_agent",
        reasons=("command:copilot_binary",),
        command_detection=CopilotCommandDetection(
            candidate=("copilot", "chat"),
            is_likely_copilot=True,
            reason="copilot_binary",
        ),
    )


class TestRuntimeWorktreeSync(unittest.TestCase):
    """Verify that RuntimeSynchronizer calls worktree sync during refresh."""

    def test_worktree_sync_called_with_discovered_repo_roots(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        pane = _make_pane(cwd="/repo/worktrees/task")
        report = PaneDiscoveryReport(
            discovered_at=now,
            panes=(pane,),
            managed_agents=(),
            unmanaged_probable_agents=(pane,),
            non_agent_panes=(),
        )
        wt_sync = FakeWorktreeSync()
        sync = RuntimeSynchronizer(
            FakeDiscovery(report),
            FakeMonitoring(now),
            FakeGit("/repo"),
            worktree_sync=wt_sync,
        )

        sync.refresh()

        assert len(wt_sync.called_with) == 1
        roots = wt_sync.called_with[0]
        assert any(str(r) == "/repo" for r in roots)

    def test_worktree_sync_not_called_when_none(self) -> None:
        """When worktree_sync is None, refresh still works."""
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        pane = _make_pane()
        report = PaneDiscoveryReport(
            discovered_at=now,
            panes=(pane,),
            managed_agents=(),
            unmanaged_probable_agents=(pane,),
            non_agent_panes=(),
        )
        sync = RuntimeSynchronizer(
            FakeDiscovery(report),
            FakeMonitoring(now),
            FakeGit("/repo"),
            worktree_sync=None,
        )

        result = sync.refresh()

        assert result.discovery_report is not None
        assert result.error is None

    def test_worktree_sync_error_becomes_warning(self) -> None:
        """If worktree sync fails, it should add a warning, not crash."""
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        pane = _make_pane(cwd="/repo/worktrees/task")
        report = PaneDiscoveryReport(
            discovered_at=now,
            panes=(pane,),
            managed_agents=(),
            unmanaged_probable_agents=(pane,),
            non_agent_panes=(),
        )

        class FailingSync:
            def sync_worktrees_from_git(self, repo_roots) -> Never:
                msg = "sync failed"
                raise RuntimeError(msg)

        sync = RuntimeSynchronizer(
            FakeDiscovery(report),
            FakeMonitoring(now),
            FakeGit("/repo"),
            worktree_sync=FailingSync(),
        )

        result = sync.refresh()

        # Should still complete, not raise
        assert result.discovery_report is not None
        # Should have a warning about the failure
        warning_msgs = [w.message for w in result.warnings]
        assert any("worktree sync failed" in m for m in warning_msgs), (
            f"Expected worktree sync warning, got: {warning_msgs}"
        )


if __name__ == "__main__":
    unittest.main()
