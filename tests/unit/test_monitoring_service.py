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


if __name__ == "__main__":
    unittest.main()
