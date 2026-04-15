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

from copilot_commander.adapters.copilot_adapter import CopilotAdapter
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.value_objects import CommandResult
from copilot_commander.services.agent_service import AgentFactInput
from copilot_commander.services.discovery_service import DiscoveryPaneSnapshot, PaneDiscovery
from copilot_commander.services.monitoring_service import (
    MonitoringDiscovery,
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


class MonitoringServiceTests(unittest.TestCase):
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

    def test_compute_status_heuristics_blocked_dead_idle_and_running(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)

        blocked = compute_status_heuristics(
            StatusHeuristicInput(
                started_at=now - timedelta(minutes=10),
                observed_at=now,
                previous_last_activity_at=now - timedelta(minutes=1),
                blocking_issue_kinds=("authentication_issue",),
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

        assert blocked.status is AgentStatus.BLOCKED
        assert blocked.attention_reason == "authentication issue requires attention"
        assert dead.status is AgentStatus.DEAD
        assert idle.status is AgentStatus.IDLE
        assert idle.needs_attention is True
        assert running.status is AgentStatus.RUNNING
        assert running.last_activity_at == now

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
        assert facts.status is AgentStatus.WAITING_INPUT
        assert facts.token_input == 8
        assert facts.capture_text == "Copilot session id: copilot-123"


if __name__ == "__main__":
    unittest.main()
