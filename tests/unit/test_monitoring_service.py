# ruff: noqa: E402,E501,ANN001,ANN201

from __future__ import annotations

import sys
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from muxdeck.adapters.copilot_adapter import CopilotAdapter
from muxdeck.adapters.copilot_session_resolver import CopilotSessionResolution
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.models import Agent
from muxdeck.domain.value_objects import CommandResult
from muxdeck.services.agent_service import AgentFactInput
from muxdeck.services.discovery_service import DiscoveryPaneSnapshot, PaneDiscovery
from muxdeck.services.monitoring_service import (
    MonitoringDiscovery,
    MonitoringLocalSession,
    MonitoringLocalSessionStore,
    MonitoringService,
    MonitoringThresholds,
    StatusHeuristicInput,
    compute_status_heuristics,
)


class DummyRunner:
    def run(
        self,
        command: Sequence[str],
        /,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> CommandResult:
        del cwd, env, timeout_sec
        raise AssertionError(f"unexpected runner use: {tuple(command)!r}")


@dataclass(slots=True)
class FakeRecorder:
    recorded: list[AgentFactInput]

    def persist_agent_facts(self, facts: AgentFactInput, /) -> AgentFactInput:
        self.recorded.append(facts)
        return facts


@dataclass(slots=True)
class FakeSessionResolver:
    resolution: CopilotSessionResolution
    seen_pids: list[int | None]

    def resolve(self, pane_pid: int | None, /) -> CopilotSessionResolution:
        self.seen_pids.append(pane_pid)
        return self.resolution

    def resolve_for_pid(self, pane_pid: int | None, /) -> str | None:
        return self.resolve(pane_pid).session_id


@dataclass(slots=True)
class FakeLocalSessionUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(slots=True)
class FakeLocalSession:
    session_id: str
    usage: FakeLocalSessionUsage | None = None


@dataclass(slots=True)
class FakeLocalSessionStore:
    sessions: tuple[FakeLocalSession, ...]
    discover_calls: int = 0

    def discover(self, *, force: bool = False) -> tuple[FakeLocalSession, ...]:
        del force
        self.discover_calls += 1
        return self.sessions


class MonitoringServiceTests(unittest.TestCase):
    def test_compute_status_heuristics_stale_errors_do_not_flip_running(self) -> None:
        """Scrollback `error:` lines must not flag a working agent as ERROR.

        Regression: `_ERROR_PATTERNS` match routine tool output
        (e.g. `error: could not apply <sha>` from a `git rebase`).
        With fresh activity observed, the agent is actively working
        and must stay RUNNING regardless of any ambient error text
        in the scrollback.
        """
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        result = compute_status_heuristics(
            StatusHeuristicInput(
                started_at=now - timedelta(minutes=5),
                observed_at=now,
                previous_last_activity_at=now - timedelta(seconds=5),
                activity_observed=True,
                error_messages=("error: could not apply 15c1344",),
            )
        )
        assert result.status is AgentStatus.RUNNING
        assert result.needs_attention is False

    def test_compute_status_heuristics_errors_require_idle_gate(self) -> None:
        """Errors alone must not flip to ERROR until the idle gate elapses.

        Without fresh activity but well before the error idle gate,
        the agent should remain RUNNING (or IDLE for long enough
        quiet periods) rather than being prematurely labelled
        failed on stale scrollback text.
        """
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        thresholds = MonitoringThresholds(
            idle_after_seconds=600,
            attention_idle_after_seconds=1200,
            error_after_seconds=300,
        )
        early = compute_status_heuristics(
            StatusHeuristicInput(
                started_at=now - timedelta(minutes=5),
                observed_at=now,
                previous_last_activity_at=now - timedelta(seconds=30),
                activity_observed=False,
                error_messages=("error: something",),
            ),
            thresholds=thresholds,
        )
        assert early.status is AgentStatus.RUNNING

        late = compute_status_heuristics(
            StatusHeuristicInput(
                started_at=now - timedelta(minutes=20),
                observed_at=now,
                previous_last_activity_at=now - timedelta(minutes=10),
                activity_observed=False,
                error_messages=("error: something",),
            ),
            thresholds=thresholds,
        )
        assert late.status is AgentStatus.ERROR
        assert late.needs_attention is True

    def test_compute_status_heuristics_waiting_for_input(self) -> None:
        now = datetime(2025, 1, 1, 12, 5, tzinfo=UTC)
        result = compute_status_heuristics(
            StatusHeuristicInput(
                started_at=now - timedelta(minutes=5),
                observed_at=now,
                previous_last_activity_at=now - timedelta(seconds=90),
                blocking_issue_kinds=("waiting_for_confirmation",),
            ),
            thresholds=MonitoringThresholds(waiting_input_after_seconds=30),
        )

        assert result.status is AgentStatus.WAITING_INPUT
        assert result.needs_attention is True
        assert result.attention_reason == "waiting for confirmation input"

    def test_compute_status_heuristics_dead_idle_running_and_active_blocking_kind_ignored(
        self,
    ) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)

        # A non-confirmation "blocking kind" (authentication_issue /
        # rate_limit / merge_conflict / tool_failure) by itself no
        # longer flips status to BLOCKED. The agent is RUNNING when
        # fresh activity was observed this cycle, regardless of what
        # noisy patterns appeared in the scrollback. The signal is
        # still preserved for downstream uses (attention reasons when
        # the agent later goes quiet).
        running_with_stale_blocking_pattern = compute_status_heuristics(
            StatusHeuristicInput(
                started_at=now - timedelta(minutes=10),
                observed_at=now,
                previous_last_activity_at=now - timedelta(seconds=5),
                blocking_issue_kinds=("authentication_issue",),
                activity_observed=True,
            )
        )
        dead = compute_status_heuristics(
            StatusHeuristicInput(
                started_at=now - timedelta(minutes=1), observed_at=now, pane_dead=True
            )
        )
        idle = compute_status_heuristics(
            StatusHeuristicInput(
                started_at=now - timedelta(hours=1),
                observed_at=now,
                previous_last_activity_at=now - timedelta(minutes=20),
            ),
            thresholds=MonitoringThresholds(
                idle_after_seconds=60, attention_idle_after_seconds=300
            ),
        )
        running = compute_status_heuristics(
            StatusHeuristicInput(
                started_at=now - timedelta(minutes=10),
                observed_at=now,
                previous_last_activity_at=now - timedelta(seconds=10),
                activity_observed=True,
            )
        )

        assert running_with_stale_blocking_pattern.status is AgentStatus.RUNNING
        assert dead.status is AgentStatus.DEAD
        assert idle.status is AgentStatus.IDLE
        assert idle.needs_attention is True
        assert running.status is AgentStatus.RUNNING
        assert running.last_activity_at == now

    def test_compute_status_heuristics_idle_surfaces_blocking_kind_reason(self) -> None:
        """An agent that went quiet on an auth/rate-limit pattern should

        still get the descriptive attention reason even though status
        itself is IDLE, not BLOCKED.
        """
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        result = compute_status_heuristics(
            StatusHeuristicInput(
                started_at=now - timedelta(hours=1),
                observed_at=now,
                previous_last_activity_at=now - timedelta(minutes=20),
                blocking_issue_kinds=("rate_limit",),
            ),
            thresholds=MonitoringThresholds(
                idle_after_seconds=60, attention_idle_after_seconds=300
            ),
        )
        assert result.status is AgentStatus.IDLE
        assert result.needs_attention is True
        assert result.attention_reason == "rate limit is blocking progress"

    def test_compute_status_heuristics_completed_agent_dead_pane(self) -> None:
        """Dead pane + marked_complete exit reason → COMPLETED, not DEAD."""
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        completed = compute_status_heuristics(
            StatusHeuristicInput(
                started_at=now - timedelta(minutes=5),
                observed_at=now,
                pane_dead=True,
                session_exit_reason="marked_complete",
            )
        )
        assert completed.status is AgentStatus.COMPLETED
        assert completed.needs_attention is False

    def test_compute_status_heuristics_dead_pane_no_exit_reason(self) -> None:
        """Dead pane without exit reason → DEAD + needs attention."""
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        dead = compute_status_heuristics(
            StatusHeuristicInput(
                started_at=now - timedelta(minutes=5),
                observed_at=now,
                pane_dead=True,
                session_exit_reason=None,
            )
        )
        assert dead.status is AgentStatus.DEAD
        assert dead.needs_attention is True

    def test_compute_status_heuristics_completed_live_pane_with_stale_errors(self) -> None:
        """Live pane + marked_complete + stale error lines → COMPLETED, not ERROR.

        Regression: error text captured before the user marked the agent as
        complete must not flip the displayed status back to ERROR once the
        pane has gone quiet.
        """
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        completed = compute_status_heuristics(
            StatusHeuristicInput(
                started_at=now - timedelta(minutes=5),
                observed_at=now,
                previous_last_activity_at=now - timedelta(minutes=2),
                pane_dead=False,
                activity_observed=False,
                error_messages=("ERROR: rate limit",),
                session_exit_reason="marked_complete",
            )
        )
        assert completed.status is AgentStatus.COMPLETED
        assert completed.needs_attention is False

    def test_compute_status_heuristics_completed_reopens_on_new_activity(self) -> None:
        """If fresh activity appears after mark-complete, the live status wins.

        This lets the user reuse the same pane without the TUI freezing on a
        terminal COMPLETED state.
        """
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        result = compute_status_heuristics(
            StatusHeuristicInput(
                started_at=now - timedelta(minutes=5),
                observed_at=now,
                pane_dead=False,
                activity_observed=True,
                session_exit_reason="marked_complete",
            )
        )
        assert result.status is AgentStatus.RUNNING

    def test_monitor_discoveries_builds_persistable_agent_facts(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        recorder = FakeRecorder(recorded=[])
        service = MonitoringService(
            recorder,
            thresholds=MonitoringThresholds(waiting_input_after_seconds=0),
            clock=lambda: now,
        )
        copilot = CopilotAdapter(DummyRunner())
        evidence = copilot.interpret_output(
            "Copilot session id: copilot-123\nwaiting for confirmation\ninput_tokens: 8"
        )
        discovery = PaneDiscovery(
            snapshot=DiscoveryPaneSnapshot(
                pane_id="%3",
                tmux_session_name="muxdeck",
                tmux_window_id="@3",
                tmux_window_name="agents",
                pane_current_path="/repo/worktrees/task",
                pane_current_command="copilot chat",
                pane_pid=321,
                repo_root="/repo",
                branch="task/demo",
            ),
            discovered_at=now,
            classification="unmanaged_probable_agent",
            reasons=("command:copilot_binary",),
            command_detection=copilot.detect_command("copilot chat"),
            captured_output="Copilot session id: copilot-123",
            session_evidence=evidence,
        )

        report = service.monitor_discoveries(cast("Sequence[MonitoringDiscovery]", (discovery,)))

        assert len(report.results) == 1
        assert len(recorder.recorded) == 1
        facts = recorder.recorded[0]
        assert facts.copilot_session_id == "copilot-123"
        assert facts.repo_root == "/repo"
        assert facts.branch == "task/demo"
        assert facts.status is AgentStatus.WAITING_INPUT
        assert facts.token_input == 8
        assert facts.capture_text == "Copilot session id: copilot-123"

    def test_monitor_discoveries_falls_back_to_cached_local_session_usage(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        recorder = FakeRecorder(recorded=[])
        local_sessions = FakeLocalSessionStore(
            sessions=(
                FakeLocalSession(
                    session_id="copilot-123",
                    usage=FakeLocalSessionUsage(input_tokens=21, output_tokens=34),
                ),
            )
        )
        service = MonitoringService(
            recorder,
            local_session_store=cast(MonitoringLocalSessionStore, local_sessions),
            clock=lambda: now,
        )
        copilot = CopilotAdapter(DummyRunner())
        evidence = copilot.interpret_output("Copilot session id: copilot-123\nworking tree clean")
        discovery = PaneDiscovery(
            snapshot=DiscoveryPaneSnapshot(
                pane_id="%3",
                tmux_session_name="muxdeck",
                tmux_window_id="@3",
                tmux_window_name="agents",
                pane_current_path="/repo/worktrees/task",
                pane_current_command="copilot chat",
                pane_pid=321,
                repo_root="/repo",
                branch="task/demo",
            ),
            discovered_at=now,
            classification="unmanaged_probable_agent",
            reasons=("command:copilot_binary",),
            command_detection=copilot.detect_command("copilot chat"),
            captured_output="Copilot session id: copilot-123",
            session_evidence=evidence,
        )

        service.monitor_discoveries(cast("Sequence[MonitoringDiscovery]", (discovery,)))

        assert local_sessions.discover_calls == 1
        facts = recorder.recorded[0]
        assert facts.copilot_session_id == "copilot-123"
        assert facts.token_input == 21
        assert facts.token_output == 34
        assert facts.token_total == 55

    def test_monitor_discoveries_prefers_live_usage_over_cached_local_session_usage(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        recorder = FakeRecorder(recorded=[])
        local_sessions = FakeLocalSessionStore(
            sessions=(
                FakeLocalSession(
                    session_id="copilot-123",
                    usage=FakeLocalSessionUsage(input_tokens=99, output_tokens=1),
                ),
            )
        )
        service = MonitoringService(
            recorder,
            local_session_store=cast(MonitoringLocalSessionStore, local_sessions),
            clock=lambda: now,
        )
        copilot = CopilotAdapter(DummyRunner())
        evidence = copilot.interpret_output(
            "Copilot session id: copilot-123\ninput_tokens: 8\noutput_tokens: 13\n"
        )
        discovery = PaneDiscovery(
            snapshot=DiscoveryPaneSnapshot(
                pane_id="%3",
                tmux_session_name="muxdeck",
                tmux_window_id="@3",
                tmux_window_name="agents",
                pane_current_path="/repo/worktrees/task",
                pane_current_command="copilot chat",
                pane_pid=321,
                repo_root="/repo",
                branch="task/demo",
            ),
            discovered_at=now,
            classification="unmanaged_probable_agent",
            reasons=("command:copilot_binary",),
            command_detection=copilot.detect_command("copilot chat"),
            captured_output="Copilot session id: copilot-123",
            session_evidence=evidence,
        )

        service.monitor_discoveries(cast("Sequence[MonitoringDiscovery]", (discovery,)))

        facts = recorder.recorded[0]
        assert facts.token_input == 8
        assert facts.token_output == 13
        assert facts.token_total == 21

    def test_monitor_discoveries_uses_resolver_when_capture_has_multiple_session_ids(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        recorder = FakeRecorder(recorded=[])
        resolver = FakeSessionResolver(
            resolution=CopilotSessionResolution(
                session_id="copilot-live",
                state="resolved",
            ),
            seen_pids=[],
        )
        service = MonitoringService(
            recorder,
            session_resolver=resolver,
            clock=lambda: now,
        )
        copilot = CopilotAdapter(DummyRunner())
        evidence = copilot.interpret_output(
            "Copilot session id: stale-pane\n"
            "split view\n"
            "Copilot session id: copilot-live\n"
            "input_tokens: 3\n"
        )
        discovery = PaneDiscovery(
            snapshot=DiscoveryPaneSnapshot(
                pane_id="%9",
                tmux_session_name="muxdeck",
                tmux_window_id="@9",
                tmux_window_name="nested",
                pane_current_path="/repo/worktrees/task",
                pane_current_command="tmux",
                pane_pid=9876,
                repo_root="/repo",
                branch="task/demo",
            ),
            discovered_at=now,
            classification="unmanaged_probable_agent",
            reasons=("captured Copilot evidence",),
            command_detection=copilot.detect_command("tmux"),
            captured_output="nested capture",
            session_evidence=evidence,
        )

        report = service.monitor_discoveries(cast("Sequence[MonitoringDiscovery]", (discovery,)))

        assert len(report.results) == 1
        assert resolver.seen_pids == [9876]
        assert recorder.recorded[0].copilot_session_id == "copilot-live"

    def test_monitor_discoveries_suppresses_stale_session_when_live_resolution_is_ambiguous(
        self,
    ) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        recorder = FakeRecorder(recorded=[])
        resolver = FakeSessionResolver(
            resolution=CopilotSessionResolution(state="ambiguous"),
            seen_pids=[],
        )
        service = MonitoringService(
            recorder,
            session_resolver=resolver,
            clock=lambda: now,
        )
        existing_agent = Agent(
            id="agent-1",
            name="agent-1",
            tmux_session_name="muxdeck",
            tmux_window_id="@9",
            tmux_pane_id="%9",
            cwd="/repo/worktrees/task",
            repo_root="/repo",
            worktree_path="/repo/worktrees/task",
            branch="task/demo",
            task_title="Nested agent",
            copilot_session_id="stale-session",
            pid=9876,
            status=AgentStatus.RUNNING,
            started_at=now,
            last_seen_at=now,
        )
        discovery = PaneDiscovery(
            snapshot=DiscoveryPaneSnapshot(
                pane_id="%9",
                tmux_session_name="muxdeck",
                tmux_window_id="@9",
                tmux_window_name="nested",
                pane_current_path="/repo/worktrees/task",
                pane_current_command="tmux",
                pane_pid=9876,
                repo_root="/repo",
                branch="task/demo",
            ),
            discovered_at=now,
            classification="managed_agent",
            reasons=("matched stored agent",),
            command_detection=CopilotAdapter(DummyRunner()).detect_command("tmux"),
            captured_output="nested capture",
            managed_agent=existing_agent,
        )

        report = service.monitor_discoveries(cast("Sequence[MonitoringDiscovery]", (discovery,)))

        assert len(report.results) == 1
        assert resolver.seen_pids == [9876]
        assert recorder.recorded[0].copilot_session_id is None

    def test_compute_status_heuristics_captured_output_changed_resets_idle(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        result = compute_status_heuristics(
            StatusHeuristicInput(
                started_at=now - timedelta(hours=1),
                observed_at=now,
                previous_last_activity_at=now - timedelta(minutes=20),
                activity_observed=False,
                captured_output_changed=True,
            ),
            thresholds=MonitoringThresholds(
                idle_after_seconds=60, attention_idle_after_seconds=300
            ),
        )

        assert result.status is AgentStatus.RUNNING
        assert result.last_activity_at == now
        assert result.idle_seconds == 0

    def test_capture_added_new_lines_helper_detects_new_tail_lines(self) -> None:
        from muxdeck.services.monitoring_service import _capture_added_new_lines

        previous = "step 1\nstep 2\nspinner ●\n"
        current = "step 1\nstep 2\nspinner ●\nstep 3 done\n"

        assert _capture_added_new_lines(current, previous) is True

    def test_capture_added_new_lines_helper_ignores_repeated_spinner(self) -> None:
        from muxdeck.services.monitoring_service import _capture_added_new_lines

        previous = "header\nspinner ●\n"
        current = "header\nspinner ●\n"

        assert _capture_added_new_lines(current, previous) is False

    def test_capture_added_new_lines_helper_treats_first_capture_as_activity(self) -> None:
        from muxdeck.services.monitoring_service import _capture_added_new_lines

        # No previous capture but current has real content → activity.
        assert _capture_added_new_lines("hello\n", None) is True
        # No previous, current is blank → no activity.
        assert _capture_added_new_lines("\n", None) is False

    def test_monitor_discoveries_uses_log_history_to_signal_activity(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        recorder = FakeRecorder(recorded=[])
        existing_agent = Agent(
            id="agent-busy",
            name="agent",
            cwd="/repo",
            repo_root="/repo",
            branch="task/demo",
            tmux_session_name="muxdeck",
            tmux_window_id="@1",
            tmux_window_name="agents",
            tmux_pane_id="%1",
            pane_tty="/dev/pts/0",
            pid=222,
            copilot_session_id="copilot-busy",
            started_at=now - timedelta(hours=1),
            last_seen_at=now,
            last_activity_at=now - timedelta(minutes=30),
            status=AgentStatus.IDLE,
        )

        @dataclass(slots=True)
        class _FakeChunk:
            content: str

        @dataclass(slots=True)
        class _FakeHistory:
            chunk: _FakeChunk | None

            def get_latest_log_chunk(self, session_id: str, /) -> _FakeChunk | None:
                del session_id
                return self.chunk

        history = _FakeHistory(chunk=_FakeChunk(content="header\nspinner ●\n"))
        service = MonitoringService(
            recorder,
            log_history=history,
            thresholds=MonitoringThresholds(idle_after_seconds=60),
            clock=lambda: now,
        )

        copilot = CopilotAdapter(DummyRunner())
        evidence = copilot.interpret_output("Copilot session id: copilot-busy")
        discovery = PaneDiscovery(
            snapshot=DiscoveryPaneSnapshot(
                pane_id="%1",
                tmux_session_name="muxdeck",
                tmux_window_id="@1",
                tmux_window_name="agents",
                pane_current_path="/repo",
                pane_current_command="copilot chat",
                pane_pid=222,
                repo_root="/repo",
                branch="task/demo",
            ),
            discovered_at=now,
            classification="managed_agent",
            reasons=("matched stored agent",),
            command_detection=copilot.detect_command("copilot chat"),
            captured_output="header\nspinner ●\nfresh new line\n",
            session_evidence=evidence,
            managed_agent=existing_agent,
        )

        report = service.monitor_discoveries(cast("Sequence[MonitoringDiscovery]", (discovery,)))

        assert len(report.results) == 1
        evaluation = report.results[0].evaluation
        assert evaluation.status is AgentStatus.RUNNING
        assert evaluation.last_activity_at == now

    def test_monitoring_thresholds_validation(self) -> None:
        """MonitoringThresholds should validate non-negative values."""
        import pytest

        with pytest.raises(ValueError, match="must be non-negative"):
            MonitoringThresholds(waiting_input_after_seconds=-1)

        with pytest.raises(ValueError, match="must be non-negative"):
            MonitoringThresholds(idle_after_seconds=-1)

        with pytest.raises(ValueError, match="must be non-negative"):
            MonitoringThresholds(attention_idle_after_seconds=-1)

        with pytest.raises(ValueError, match="must be non-negative"):
            MonitoringThresholds(error_after_seconds=-1)

    def test_build_local_usage_index_when_no_store(self) -> None:
        """_build_local_usage_index should return empty dict when no store."""
        recorder = FakeRecorder(recorded=[])
        service = MonitoringService(recorder, local_session_store=None)

        index = service._build_local_usage_index()
        assert index == {}

    def test_resolve_copilot_session_id_uses_resolver_when_no_other_source(self) -> None:
        """When neither evidence nor an existing agent supplies a session
        id, ``_resolve_copilot_session_id`` must fall through to the
        live resolver and return its session id.

        Originally named ``..._live_resolution`` and claimed to verify
        the resolver is *preferred over* other sources — but no other
        sources were configured, so "preference" wasn't actually being
        tested.
        """
        recorder = FakeRecorder(recorded=[])

        class FakeResolver:
            def resolve(self, pane_pid: int | None, /) -> CopilotSessionResolution:
                return CopilotSessionResolution(session_id="live-session")

            def resolve_for_pid(self, pane_pid: int | None, /) -> str:
                return "live-session"

        service = MonitoringService(recorder, session_resolver=FakeResolver())

        snapshot = DiscoveryPaneSnapshot(
            pane_id="%1",
            tmux_session_name="test",
            tmux_window_id="@1",
            pane_current_path="/repo",
            pane_pid=1234,
        )

        session_id = service._resolve_copilot_session_id(
            snapshot=snapshot,
            session_evidence=None,
            existing_agent=None,
        )

        assert session_id == "live-session"

    def test_resolve_copilot_session_id_prefers_resolver_over_existing_agent(
        self,
    ) -> None:
        """A live resolver result must beat the cached agent's session id.

        This is the actual "prefer" behavior the original test name
        promised. With both an existing agent and a resolver in play,
        the resolver wins.
        """
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        recorder = FakeRecorder(recorded=[])

        class FakeResolver:
            def resolve(self, pane_pid: int | None, /) -> CopilotSessionResolution:
                return CopilotSessionResolution(session_id="live-session")

            def resolve_for_pid(self, pane_pid: int | None, /) -> str:
                return "live-session"

        existing_agent = Agent(
            id="agent-1",
            name="cached",
            tmux_session_name="test",
            tmux_window_id="@1",
            tmux_pane_id="%1",
            cwd="/repo",
            repo_root="/repo",
            worktree_path="/repo",
            branch="main",
            status=AgentStatus.RUNNING,
            started_at=now,
            last_seen_at=now,
            copilot_session_id="cached-session",
        )

        service = MonitoringService(recorder, session_resolver=FakeResolver())
        snapshot = DiscoveryPaneSnapshot(
            pane_id="%1",
            tmux_session_name="test",
            tmux_window_id="@1",
            pane_current_path="/repo",
            pane_pid=1234,
        )
        session_id = service._resolve_copilot_session_id(
            snapshot=snapshot,
            session_evidence=None,
            existing_agent=existing_agent,
        )
        # Resolver short-circuits BEFORE the existing-agent fallback.
        assert session_id == "live-session"

    def test_resolve_copilot_session_id_ambiguous_returns_none(self) -> None:
        """When the live resolver reports the pane is ambiguous, the
        method must return ``None`` (and NOT fall through to the
        existing-agent fallback) — that's the contract the
        "ambiguous" branch in production enforces.
        """
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        recorder = FakeRecorder(recorded=[])

        class FakeResolver:
            def resolve(self, pane_pid: int | None, /) -> CopilotSessionResolution:
                return CopilotSessionResolution(state="ambiguous")

            def resolve_for_pid(self, pane_pid: int | None, /) -> None:
                return None

        existing_agent = Agent(
            id="agent-1",
            name="cached",
            tmux_session_name="test",
            tmux_window_id="@1",
            tmux_pane_id="%1",
            cwd="/repo",
            repo_root="/repo",
            worktree_path="/repo",
            branch="main",
            status=AgentStatus.RUNNING,
            started_at=now,
            last_seen_at=now,
            copilot_session_id="cached-session",
        )

        service = MonitoringService(recorder, session_resolver=FakeResolver())

        snapshot = DiscoveryPaneSnapshot(
            pane_id="%1",
            tmux_session_name="test",
            tmux_window_id="@1",
            pane_current_path="/repo",
            pane_pid=1234,
        )

        session_id = service._resolve_copilot_session_id(
            snapshot=snapshot,
            session_evidence=None,
            existing_agent=existing_agent,
        )

        # Ambiguous resolutions must short-circuit BEFORE the
        # existing-agent fallback. If they didn't, this would return
        # "cached-session" instead of None.
        assert session_id is None

    def test_compute_status_heuristics_running_when_just_started(self) -> None:
        """A pane that just started (no idle window elapsed) must
        evaluate to ``RUNNING`` regardless of token data.

        Originally named ``..._token_total_from_input_output``, but the
        ``StatusHeuristicInput`` API has no token fields, so that name
        was meaningless. Rename + sharpen to cover what's actually
        observable.
        """
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        result = compute_status_heuristics(
            StatusHeuristicInput(
                started_at=now,
                observed_at=now,
            )
        )
        assert result.status is AgentStatus.RUNNING
        assert result.attention_reason is None
        # last_activity_at falls back to previous_last_activity_at; we
        # supplied none so it must remain None.
        assert result.last_activity_at is None

    def test_blocking_attention_reason_authentication_issue(self) -> None:
        """_blocking_attention_reason should handle authentication_issue."""
        from muxdeck.services.monitoring_service import _blocking_attention_reason

        msg = _blocking_attention_reason("authentication_issue")
        assert msg == "authentication issue requires attention"

    def test_blocking_attention_reason_merge_conflict(self) -> None:
        """_blocking_attention_reason should handle merge_conflict."""
        from muxdeck.services.monitoring_service import _blocking_attention_reason

        msg = _blocking_attention_reason("merge_conflict")
        assert msg == "merge conflict requires intervention"

    def test_blocking_attention_reason_rate_limit(self) -> None:
        """_blocking_attention_reason should handle rate_limit."""
        from muxdeck.services.monitoring_service import _blocking_attention_reason

        msg = _blocking_attention_reason("rate_limit")
        assert msg == "rate limit is blocking progress"

    def test_blocking_attention_reason_tool_failure(self) -> None:
        """_blocking_attention_reason should handle tool_failure."""
        from muxdeck.services.monitoring_service import _blocking_attention_reason

        msg = _blocking_attention_reason("tool_failure")
        assert msg == "tool failure detected"

    def test_blocking_attention_reason_generic(self) -> None:
        """_blocking_attention_reason should replace underscores for unknown kinds."""
        from muxdeck.services.monitoring_service import _blocking_attention_reason

        msg = _blocking_attention_reason("some_unknown_kind")
        assert msg == "some unknown kind"

    def test_has_activity_signal_empty_evidence(self) -> None:
        """_has_activity_signal should return False for None evidence."""
        from muxdeck.services.monitoring_service import _has_activity_signal

        assert _has_activity_signal(None) is False

    def test_first_blocking_kind_empty_list(self) -> None:
        """_first_blocking_kind should return None for empty sequence."""
        from muxdeck.services.monitoring_service import _first_blocking_kind

        result = _first_blocking_kind(())
        assert result is None

    def test_compute_status_heuristics_error_with_blocking_kind(self) -> None:
        """Error status should use blocking_kind reason when available."""
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        thresholds = MonitoringThresholds(
            idle_after_seconds=600,
            attention_idle_after_seconds=1200,
            error_after_seconds=300,
        )

        result = compute_status_heuristics(
            StatusHeuristicInput(
                started_at=now - timedelta(minutes=20),
                observed_at=now,
                previous_last_activity_at=now - timedelta(minutes=10),
                activity_observed=False,
                error_messages=("error: something",),
                blocking_issue_kinds=("merge_conflict",),
            ),
            thresholds=thresholds,
        )

        assert result.status is AgentStatus.ERROR
        assert result.needs_attention is True
        assert result.attention_reason is not None
        assert "merge conflict" in result.attention_reason

    def test_compute_status_heuristics_idle_with_blocking_kind(self) -> None:
        """Idle status with blocking_kind should surface the reason."""
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        thresholds = MonitoringThresholds(
            idle_after_seconds=60,
            attention_idle_after_seconds=300,
        )

        result = compute_status_heuristics(
            StatusHeuristicInput(
                started_at=now - timedelta(hours=1),
                observed_at=now,
                previous_last_activity_at=now - timedelta(minutes=20),
                blocking_issue_kinds=("authentication_issue",),
            ),
            thresholds=thresholds,
        )

        assert result.status is AgentStatus.IDLE
        assert result.needs_attention is True
        assert result.attention_reason is not None
        assert "authentication" in result.attention_reason

    def test_compute_status_heuristics_idle_without_attention_gate(self) -> None:
        """Agent can be IDLE without needing attention if idle_after < attention_idle_after."""
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        thresholds = MonitoringThresholds(
            idle_after_seconds=60,
            attention_idle_after_seconds=600,
        )

        result = compute_status_heuristics(
            StatusHeuristicInput(
                started_at=now - timedelta(hours=1),
                observed_at=now,
                previous_last_activity_at=now - timedelta(minutes=2),
            ),
            thresholds=thresholds,
        )

        assert result.status is AgentStatus.IDLE
        assert result.needs_attention is False

    def test_monitor_discoveries_uses_local_store_tokens_when_no_direct_usage(
        self,
    ) -> None:
        """When there's no direct usage on the discovery, the recorded
        agent must still get token totals from the local session store
        fallback path.

        Originally named ``..._combines_token_sources`` but neither a
        local store nor any direct usage was wired up — so token-source
        combination wasn't being tested at all.
        """
        from dataclasses import dataclass, field

        now = datetime(2025, 1, 1, 12, tzinfo=UTC)

        @dataclass(frozen=True, slots=True)
        class _LocalUsage:
            input_tokens: int | None = 7
            output_tokens: int | None = 11

        @dataclass(frozen=True, slots=True)
        class _LocalSession:
            session_id: str = "copilot-session-1"
            usage: _LocalUsage = field(default_factory=_LocalUsage)

        @dataclass(frozen=True, slots=True)
        class _LocalStore:
            sessions: tuple[_LocalSession, ...]

            def discover(self, *, force: bool = False) -> Sequence[MonitoringLocalSession]:
                return cast("Sequence[MonitoringLocalSession]", self.sessions)

        recorder = FakeRecorder(recorded=[])
        service = MonitoringService(
            recorder,
            local_session_store=cast(
                MonitoringLocalSessionStore,
                _LocalStore(sessions=(_LocalSession(),)),
            ),
        )

        existing_agent = Agent(
            id="agent-1",
            name="test",
            tmux_session_name="test",
            tmux_window_id="@1",
            tmux_pane_id="%1",
            cwd="/repo",
            repo_root="/repo",
            worktree_path="/repo",
            branch="main",
            status=AgentStatus.RUNNING,
            started_at=now,
            last_seen_at=now,
            copilot_session_id="copilot-session-1",
        )

        copilot = CopilotAdapter(DummyRunner())
        discovery = PaneDiscovery(
            snapshot=DiscoveryPaneSnapshot(
                pane_id="%1",
                tmux_session_name="test",
                tmux_window_id="@1",
                pane_current_path="/repo",
                pane_current_command="copilot",
                repo_root="/repo",
                branch="main",
            ),
            discovered_at=now,
            classification="managed_agent",
            reasons=("matched",),
            command_detection=copilot.detect_command("copilot"),
            managed_agent=existing_agent,
        )

        report = service.monitor_discoveries(cast("Sequence[MonitoringDiscovery]", (discovery,)))

        assert len(report.results) == 1
        assert len(recorder.recorded) == 1
        recorded_agent = recorder.recorded[0]
        # Tokens flowed from the local store fallback into the recorded
        # agent. The previous test didn't wire up a local store at all
        # and then asserted only `len(recorder.recorded) == 1` — so a
        # regression that dropped the local-store branch entirely
        # would have passed silently.
        assert recorded_agent.token_input == 7, (
            f"local-store input_tokens not propagated: got {recorded_agent.token_input!r}"
        )
        assert recorded_agent.token_output == 11
        # token_total must combine the two when not provided directly.
        assert recorded_agent.token_total == 18, (
            f"token_total should combine input+output, got {recorded_agent.token_total!r}"
        )

    def test_candidate_session_ids_no_evidence(self) -> None:
        """_candidate_session_ids should return empty for None evidence."""
        from muxdeck.services.monitoring_service import _candidate_session_ids

        ids = _candidate_session_ids(None)
        assert ids == ()


if __name__ == "__main__":
    unittest.main()
