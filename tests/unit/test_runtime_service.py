# ruff: noqa: E402,ANN001,ANN201

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

from copilot_commander.adapters.copilot_adapter import CopilotCommandDetection
from copilot_commander.exceptions import TmuxCommandError
from copilot_commander.services.discovery_service import (
    DiscoveryPaneSnapshot,
    PaneDiscovery,
    PaneDiscoveryReport,
)
from copilot_commander.services.monitoring_service import MonitoringDiscovery, MonitoringReport
from copilot_commander.services.runtime_service import RuntimeSynchronizer


class FakeDiscovery:
    def __init__(
        self,
        report: PaneDiscoveryReport | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._report = report
        self._error = error

    def discover_panes(self) -> PaneDiscoveryReport:
        if self._error is not None:
            raise self._error
        assert self._report is not None
        return self._report


class FakeMonitoring:
    def __init__(self, monitored_at: datetime) -> None:
        self.monitored_at = monitored_at
        self.seen: tuple[MonitoringDiscovery, ...] = ()

    def monitor_discoveries(
        self,
        discoveries: Sequence[MonitoringDiscovery],
        /,
    ) -> MonitoringReport:
        self.seen = tuple(discoveries)
        return MonitoringReport(monitored_at=self.monitored_at, results=())


class FakeGit:
    def discover_repo_root(self, cwd: str | Path, /) -> Path:
        assert str(cwd) == "/repo/worktrees/task-one"
        return Path("/repo")

    def current_branch(self, cwd: str | Path, /) -> str | None:
        assert str(cwd) == "/repo/worktrees/task-one"
        return "task/task-one"


class RuntimeSynchronizerTests(unittest.TestCase):
    def test_refresh_enriches_discoveries_with_git_context(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        pane = PaneDiscovery(
            snapshot=DiscoveryPaneSnapshot(
                pane_id="%7",
                tmux_session_name="muxdeck",
                tmux_window_id="@2",
                tmux_window_name="agents",
                pane_current_path="/repo/worktrees/task-one",
                pane_current_command="copilot chat",
            ),
            discovered_at=now,
            classification="unmanaged_probable_agent",
            reasons=("command:copilot_binary",),
            command_detection=CopilotCommandDetection(
                candidate=("copilot", "chat"),
                is_likely_copilot=True,
                reason="copilot_binary",
            ),
        )
        discovery = FakeDiscovery(
            PaneDiscoveryReport(
                discovered_at=now,
                panes=(pane,),
                managed_agents=(),
                unmanaged_probable_agents=(pane,),
                non_agent_panes=(),
            )
        )
        monitoring = FakeMonitoring(now)

        report = RuntimeSynchronizer(discovery, monitoring, FakeGit()).refresh()

        assert report.error is None
        assert report.observed_panes == 1
        assert report.discovered_agents == 1
        assert len(monitoring.seen) == 1
        assert monitoring.seen[0].snapshot.repo_root == "/repo"
        assert monitoring.seen[0].snapshot.branch == "task/task-one"

    def test_refresh_returns_typed_tmux_error(self) -> None:
        synchronizer = RuntimeSynchronizer(
            FakeDiscovery(error=TmuxCommandError("tmux list-panes -a", stderr="no server running")),
            FakeMonitoring(datetime(2025, 1, 1, 12, tzinfo=UTC)),
            FakeGit(),
        )

        report = synchronizer.refresh()

        assert report.error == "tmux discovery failed: no server running"
        assert report.observed_panes == 0


if __name__ == "__main__":
    unittest.main()
