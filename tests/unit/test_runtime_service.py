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


from muxdeck.adapters.copilot_adapter import CopilotCommandDetection
from muxdeck.adapters.copilot_session_resolver import CopilotSessionResolution
from muxdeck.adapters.git_adapter import GitRepoContext
from muxdeck.adapters.sqlite_store import SessionContextRecord
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.models import Agent
from muxdeck.domain.subagents import SubAgentSnapshot, SubAgentTree
from muxdeck.exceptions import TmuxCommandError
from muxdeck.services.discovery_service import (
    DiscoveryPaneSnapshot,
    PaneDiscovery,
    PaneDiscoveryReport,
)
from muxdeck.services.monitoring_service import MonitoringDiscovery, MonitoringReport
from muxdeck.services.runtime_service import RuntimeSynchronizer, RuntimeSyncReport
from muxdeck.services.subtask_registry import SubTaskRegistry


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


class InspectingGit:
    """``CountingGit`` analogue that also exposes ``inspect_repo_context``.

    Mirrors the production :class:`GitAdapter` surface so the
    runtime synchronizer takes the A2 single-call fast path
    instead of the legacy two-call fallback.
    """

    def __init__(self, repo_root: str = "/repo", branch: str | None = "task/task-one") -> None:
        self.repo_root = Path(repo_root)
        self.branch = branch
        self.repo_root_calls: list[str] = []
        self.branch_calls: list[str] = []
        self.inspect_calls: list[str] = []

    def discover_repo_root(self, cwd: str | Path, /) -> Path:
        self.repo_root_calls.append(str(cwd))
        return self.repo_root

    def current_branch(self, cwd: str | Path, /) -> str | None:
        self.branch_calls.append(str(cwd))
        return self.branch

    def inspect_repo_context(self, cwd: str | Path, /) -> GitRepoContext:
        self.inspect_calls.append(str(cwd))
        return GitRepoContext(repo_root=self.repo_root, branch=self.branch)


class FakeSessionResolver:
    def __init__(
        self,
        resolved: dict[int, str] | None = None,
        *,
        ambiguous: set[int] | None = None,
    ) -> None:
        self._resolved = resolved or {}
        self._ambiguous = ambiguous or set()
        self.calls: list[int | None] = []

    def resolve(self, pane_pid: int | None, /) -> CopilotSessionResolution:
        self.calls.append(pane_pid)
        if pane_pid is None:
            return CopilotSessionResolution()
        if pane_pid in self._ambiguous:
            return CopilotSessionResolution(state="ambiguous")
        session_id = self._resolved.get(pane_pid)
        if session_id is None:
            return CopilotSessionResolution()
        return CopilotSessionResolution(session_id=session_id, state="resolved")

    def resolve_for_pid(self, pane_pid: int | None, /) -> str | None:
        return self.resolve(pane_pid).session_id


class FakeSubagentReader:
    def __init__(self, trees: dict[str, SubAgentTree]) -> None:
        self._trees = trees
        self.calls: list[str] = []

    def read(self, session_id: str) -> SubAgentTree | None:
        self.calls.append(session_id)
        return self._trees.get(session_id)


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

    def test_refresh_uses_inspect_repo_context_when_port_supplies_it(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        pane = PaneDiscovery(
            snapshot=DiscoveryPaneSnapshot(
                pane_id="%21",
                tmux_session_name="muxdeck",
                tmux_window_id="@5",
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
        git = InspectingGit()

        report = RuntimeSynchronizer(discovery, monitoring, git).refresh()

        assert report.error is None
        assert git.inspect_calls == ["/repo/worktrees/task-one"]
        assert git.repo_root_calls == []
        assert git.branch_calls == []
        assert monitoring.seen[0].snapshot.repo_root == "/repo"
        assert monitoring.seen[0].snapshot.branch == "task/task-one"

    def test_refresh_prefers_branch_from_powershell_prompt_capture(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        pane = PaneDiscovery(
            snapshot=DiscoveryPaneSnapshot(
                pane_id="%17",
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
            captured_output=(
                "PS  [16:33] Q:\\src\\cosmosdb-wt\\coroutine-agents "
                "[users/burakkose/coroutine-agents]> copilot\n"
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
        git = CountingGit(branch="task/task-one")

        report = RuntimeSynchronizer(discovery, monitoring, git).refresh()

        assert report.error is None
        assert monitoring.seen[0].snapshot.repo_root == "/repo"
        assert monitoring.seen[0].snapshot.branch == "users/burakkose/coroutine-agents"

    def test_refresh_uses_branch_from_copilot_banner_when_git_branch_is_stale(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        pane = PaneDiscovery(
            snapshot=DiscoveryPaneSnapshot(
                pane_id="%19",
                tmux_session_name="muxdeck",
                tmux_window_id="@4",
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
            captured_output=(
                " Q:\\src\\CosmosDB [⎇ users/burakkose/rcm-opencontext*%] GPT-5.4 (xhigh)\n"
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
        git = CountingGit(branch="users/burakkose/fm/xp-imp")

        report = RuntimeSynchronizer(discovery, monitoring, git).refresh()

        assert report.error is None
        assert monitoring.seen[0].snapshot.branch == "users/burakkose/rcm-opencontext"

    def test_refresh_populates_subtasks_from_resolved_subagent_tree(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        pane = PaneDiscovery(
            snapshot=DiscoveryPaneSnapshot(
                pane_id="%27",
                tmux_session_name="muxdeck",
                tmux_window_id="@7",
                tmux_window_name="nested",
                pane_current_path="/repo/worktrees/task-one",
                pane_current_command="tmux",
                pane_pid=7272,
            ),
            discovered_at=now,
            classification="unmanaged_probable_agent",
            reasons=("captured Copilot evidence",),
            command_detection=CopilotCommandDetection(
                candidate=("tmux",),
                is_likely_copilot=False,
                reason="no_copilot_signature",
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
        subtask_registry = SubTaskRegistry()
        resolver = FakeSessionResolver({7272: "copilot-live"})
        reader = FakeSubagentReader(
            {
                "copilot-live": SubAgentTree(
                    session_id="copilot-live",
                    running=(
                        SubAgentSnapshot(
                            tool_call_id="tc-1",
                            agent_name="general-purpose",
                            display_name="General Purpose Agent",
                            description="review logs",
                            started_at=now,
                            completed_at=None,
                        ),
                    ),
                    recent=(),
                    scanned_at=now,
                )
            }
        )

        report = RuntimeSynchronizer(
            discovery,
            monitoring,
            CountingGit(),
            subtask_registry=subtask_registry,
            subagent_reader=reader,
            session_resolver=resolver,
        ).refresh()

        assert report.error is None
        assert resolver.calls == [7272]
        assert reader.calls == ["copilot-live"]
        tasks = subtask_registry.get_tasks("%27")
        assert len(tasks) == 1
        assert tasks[0].status == "running"
        assert tasks[0].agent_type_label == "General Purpose Agent"

    def test_refresh_does_not_use_stale_managed_session_when_live_resolution_is_ambiguous(
        self,
    ) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        managed_agent = Agent(
            id="agent-1",
            name="planner",
            tmux_session_name="muxdeck",
            tmux_window_id="@7",
            tmux_pane_id="%27",
            cwd="/repo/worktrees/task-one",
            repo_root="/repo",
            worktree_path="/repo/worktrees/task-one",
            branch="task/task-one",
            task_title="Nested agent",
            copilot_session_id="stale-session",
            pid=7272,
            status=AgentStatus.RUNNING,
            started_at=now,
            last_seen_at=now,
        )
        pane = PaneDiscovery(
            snapshot=DiscoveryPaneSnapshot(
                pane_id="%27",
                tmux_session_name="muxdeck",
                tmux_window_id="@7",
                tmux_window_name="nested",
                pane_current_path="/repo/worktrees/task-one",
                pane_current_command="tmux",
                pane_pid=7272,
            ),
            discovered_at=now,
            classification="managed_agent",
            reasons=("matched stored agent",),
            command_detection=CopilotCommandDetection(
                candidate=("tmux",),
                is_likely_copilot=False,
                reason="no_copilot_signature",
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
        subtask_registry = SubTaskRegistry()
        resolver = FakeSessionResolver(ambiguous={7272})
        reader = FakeSubagentReader(
            {
                "stale-session": SubAgentTree(
                    session_id="stale-session",
                    running=(
                        SubAgentSnapshot(
                            tool_call_id="tc-stale",
                            agent_name="general-purpose",
                            display_name="General Purpose Agent",
                            description="wrong task",
                            started_at=now,
                            completed_at=None,
                        ),
                    ),
                    recent=(),
                    scanned_at=now,
                )
            }
        )

        report = RuntimeSynchronizer(
            discovery,
            monitoring,
            CountingGit(),
            subtask_registry=subtask_registry,
            subagent_reader=reader,
            session_resolver=resolver,
        ).refresh()

        assert report.error is None
        assert resolver.calls == [7272]
        assert reader.calls == []
        assert subtask_registry.get_tasks("%27") == ()

    def test_runtime_sync_report_properties_when_none(self) -> None:
        """Test RuntimeSyncReport properties handle None reports gracefully."""
        report = RuntimeSyncReport()
        assert report.observed_panes == 0
        assert report.discovered_agents == 0
        assert report.persisted_agents == 0

    def test_format_git_error_uses_stderr(self) -> None:
        """_format_git_error should use stderr when available."""
        from muxdeck.exceptions import GitCommandError

        exc = GitCommandError("git", stderr="  error: bad branch  ")
        synchronizer = RuntimeSynchronizer(
            FakeDiscovery(None),
            FakeMonitoring(datetime(2025, 1, 1, 12, tzinfo=UTC)),
            FakeGit(),
        )

        msg = synchronizer._format_git_error(exc)
        assert msg == "git context unavailable: error: bad branch"

    def test_is_non_repository_error(self) -> None:
        """_is_non_repository_error should detect various non-repo patterns."""
        from muxdeck.exceptions import GitCommandError

        synchronizer = RuntimeSynchronizer(
            FakeDiscovery(None),
            FakeMonitoring(datetime(2025, 1, 1, 12, tzinfo=UTC)),
            FakeGit(),
        )

        assert synchronizer._is_non_repository_error(
            GitCommandError("git", stderr="not a git repository")
        )
        assert synchronizer._is_non_repository_error(
            GitCommandError("git", stderr="outside repository")
        )
        assert synchronizer._is_non_repository_error(GitCommandError("git", stderr="cannot chdir"))
        assert synchronizer._is_non_repository_error(
            GitCommandError("git", stderr="no such file or directory")
        )
        assert not synchronizer._is_non_repository_error(
            GitCommandError("git", stderr="permission denied")
        )

    def test_infer_capture_branch_empty_output(self) -> None:
        """_infer_capture_branch with None should return None."""
        from muxdeck.services.runtime_service import _infer_capture_branch

        assert _infer_capture_branch(None) is None

    def test_infer_capture_branch_no_matches(self) -> None:
        """_infer_capture_branch with no matching patterns should return None."""
        from muxdeck.services.runtime_service import _infer_capture_branch

        output = "some random text\nno patterns here\n"
        assert _infer_capture_branch(output) is None

    def test_infer_capture_branch_whitespace_lines_ignored(self) -> None:
        """_infer_capture_branch should skip empty/whitespace lines."""
        from muxdeck.services.runtime_service import _infer_capture_branch

        output = "\n   \n\n[⎇ main]\n"
        branch = _infer_capture_branch(output)
        assert branch == "main"

    def test_task_evidence_from_tree_failed_status(self) -> None:
        """_task_evidence_from_tree should set 'failed' when success=False."""
        from muxdeck.services.runtime_service import _task_evidence_from_tree

        datetime(2025, 1, 1, 12, tzinfo=UTC)

        class FakeSnapshot:
            task_name = "task"
            prompt = None
            description = None
            display_name = "Agent"
            agent_name = "test-agent"
            model = "gpt-5"
            is_running = False
            success = False
            error_message = None

        class FakeTree:
            running = ()
            recent = (FakeSnapshot(),)

        tasks = _task_evidence_from_tree(FakeTree())
        assert len(tasks) == 1
        assert tasks[0].status == "failed"

    def test_task_evidence_from_tree_completed_status(self) -> None:
        """_task_evidence_from_tree should set 'completed' when is_running=False."""
        from muxdeck.services.runtime_service import _task_evidence_from_tree

        datetime(2025, 1, 1, 12, tzinfo=UTC)

        class FakeSnapshot:
            task_name = "task"
            prompt = None
            description = None
            display_name = "Agent"
            agent_name = "test-agent"
            model = "gpt-5"
            is_running = False
            success = True
            error_message = None

        class FakeTree:
            running = ()
            recent = (FakeSnapshot(),)

        tasks = _task_evidence_from_tree(FakeTree())
        assert len(tasks) == 1
        assert tasks[0].status == "completed"

    def test_unique_session_ids_deduplicates(self) -> None:
        """_unique_session_ids should remove duplicates while preserving order."""
        from muxdeck.services.runtime_service import _unique_session_ids

        ids = _unique_session_ids(("session-1", "session-2", "session-1", "session-3", "session-2"))
        assert ids == ("session-1", "session-2", "session-3")

    def test_normalize_capture_branch_empty_value(self) -> None:
        """_normalize_capture_branch with empty/whitespace should return None."""
        from muxdeck.services.runtime_service import _normalize_capture_branch

        assert _normalize_capture_branch("") is None
        assert _normalize_capture_branch("   ") is None

    def test_normalize_capture_branch_strips_decoration(self) -> None:
        """_normalize_capture_branch should strip trailing decoration chars."""
        from muxdeck.services.runtime_service import _normalize_capture_branch

        assert _normalize_capture_branch("branch*") == "branch"
        assert _normalize_capture_branch("branch%") == "branch"
        assert _normalize_capture_branch("branch+!") == "branch"


if __name__ == "__main__":
    unittest.main()
