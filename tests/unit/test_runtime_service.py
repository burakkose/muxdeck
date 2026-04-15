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
from copilot_commander.adapters.sqlite_store import SessionContextRecord
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.models import Agent
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


class CountingGit:
    def __init__(self, repo_root: str = "/repo", branch: str | None = "task/task-one") -> None:
        self.repo_root = Path(repo_root)
        self.branch = branch
        self.repo_root_calls: list[str] = []
        self.branch_calls: list[str] = []

    def discover_repo_root(self, cwd: str | Path, /) -> Path:
        self.repo_root_calls.append(str(cwd))
        return self.repo_root

    def current_branch(self, cwd: str | Path, /) -> str | None:
        self.branch_calls.append(str(cwd))
        return self.branch


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

    def test_refresh_reuses_managed_agent_git_context_when_path_matches(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        managed_agent = Agent(
            id="agent-1",
            name="planner",
            tmux_session_name="muxdeck",
            tmux_window_id="@2",
            tmux_pane_id="%7",
            cwd="/repo/worktrees/task-one",
            repo_root="/repo",
            worktree_path="/repo/worktrees/task-one",
            branch="task/task-one",
            status=AgentStatus.RUNNING,
            started_at=now,
            last_seen_at=now,
        )
        pane = PaneDiscovery(
            snapshot=DiscoveryPaneSnapshot(
                pane_id="%7",
                tmux_session_name="muxdeck",
                tmux_window_id="@2",
                pane_current_path="/repo/worktrees/task-one",
                pane_current_command="copilot chat",
            ),
            discovered_at=now,
            classification="managed_agent",
            reasons=("matched stored agent",),
            command_detection=CopilotCommandDetection(
                candidate=("copilot", "chat"),
                is_likely_copilot=True,
                reason="copilot_binary",
            ),
            managed_agent=managed_agent,
        )
        discovery = FakeDiscovery(
            PaneDiscoveryReport(
                discovered_at=now,
                panes=(pane,),
                managed_agents=(pane,),
                unmanaged_probable_agents=(),
                non_agent_panes=(),
            )
        )
        monitoring = FakeMonitoring(now)
        git = CountingGit()

        report = RuntimeSynchronizer(discovery, monitoring, git).refresh()

        assert report.error is None
        assert monitoring.seen[0].snapshot.repo_root == "/repo"
        assert monitoring.seen[0].snapshot.branch == "task/task-one"
        assert git.repo_root_calls == []
        assert git.branch_calls == []

    def test_refresh_memoizes_git_lookups_for_duplicate_paths(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        pane_one = PaneDiscovery(
            snapshot=DiscoveryPaneSnapshot(
                pane_id="%7",
                tmux_session_name="muxdeck",
                tmux_window_id="@2",
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
        pane_two = PaneDiscovery(
            snapshot=DiscoveryPaneSnapshot(
                pane_id="%8",
                tmux_session_name="muxdeck",
                tmux_window_id="@3",
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
            matched_context=SessionContextRecord(
                session_id="session-1",
                tmux_pane_id="%999",
                worktree_path="/repo/worktrees/other",
                updated_at=now,
            ),
        )
        discovery = FakeDiscovery(
            PaneDiscoveryReport(
                discovered_at=now,
                panes=(pane_one, pane_two),
                managed_agents=(),
                unmanaged_probable_agents=(pane_one, pane_two),
                non_agent_panes=(),
            )
        )
        monitoring = FakeMonitoring(now)
        git = CountingGit()

        report = RuntimeSynchronizer(discovery, monitoring, git).refresh()

        assert report.error is None
        assert git.repo_root_calls == ["/repo/worktrees/task-one"]
        assert git.branch_calls == ["/repo/worktrees/task-one"]
        assert [seen.snapshot.repo_root for seen in monitoring.seen] == ["/repo", "/repo"]
        assert [seen.snapshot.branch for seen in monitoring.seen] == [
            "task/task-one",
            "task/task-one",
        ]


if __name__ == "__main__":
    unittest.main()
