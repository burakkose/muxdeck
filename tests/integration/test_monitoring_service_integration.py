# ruff: noqa: E402,E501,ANN001,ANN201

from __future__ import annotations

import shutil
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from copilot_commander.adapters.copilot_adapter import CopilotAdapter
from copilot_commander.adapters.sqlite_store import SQLiteStore
from copilot_commander.config import AppConfig, PathsConfig
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.services.agent_service import AgentService
from copilot_commander.services.discovery_service import DiscoveryPaneSnapshot, PaneDiscovery
from copilot_commander.services.monitoring_service import MonitoringService, MonitoringThresholds


class DummyRunner:
    def run(self, command, /, *, cwd=None, env=None, timeout_sec=None):
        raise AssertionError(f"unexpected runner use: {command!r}")


class MonitoringServiceIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_dir = Path(__file__).resolve().parent / "_runtime_monitoring_service"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._cleanup_runtime_dir)
        self.now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        config = AppConfig(
            paths=PathsConfig(
                state_dir=self.runtime_dir / "state",
                workspace_root=self.runtime_dir / "worktrees",
                database_path=self.runtime_dir / "state" / "commander.db",
                fallback_database_path=self.runtime_dir / "legacy" / "commander.db",
            )
        )
        self.store = SQLiteStore.from_config(config)
        self.addCleanup(self.store.close)

    def _cleanup_runtime_dir(self) -> None:
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)

    def test_monitoring_persists_agent_session_events_and_logs(self) -> None:
        agent_service_instance = AgentService(
            self.store,
            self.store,
            self.store,
            self.store,
            self.store,
            clock=lambda: self.now,
        )
        monitoring = MonitoringService(
            agent_service_instance,
            thresholds=MonitoringThresholds(waiting_input_after_seconds=0),
            clock=lambda: self.now,
        )
        copilot = CopilotAdapter(DummyRunner())
        evidence = copilot.interpret_output(
            "Copilot session id: copilot-123\nwaiting for confirmation\ninput_tokens: 9\noutput_tokens: 4"
        )
        discovery = PaneDiscovery(
            snapshot=DiscoveryPaneSnapshot(
                pane_id="%17",
                tmux_session_name="muxdeck",
                tmux_window_id="@7",
                tmux_window_name="agents",
                pane_tty="/dev/pts/17",
                pane_current_path="/repo/worktrees/task",
                pane_current_command="copilot chat",
                pane_pid=4242,
            ),
            discovered_at=self.now,
            classification="unmanaged_probable_agent",
            reasons=("command:copilot_binary",),
            command_detection=copilot.detect_command("copilot chat"),
            captured_output="Copilot session id: copilot-123\nwaiting for confirmation",
            session_evidence=evidence,
        )

        report = monitoring.monitor_discoveries((discovery,))

        assert len(report.results) == 1
        agents = self.store.list_agents()
        assert len(agents) == 1
        agent = agents[0]
        assert agent.status is AgentStatus.WAITING_INPUT
        assert agent.copilot_session_id == "copilot-123"
        assert agent.token_input == 9
        session = self.store.get_session_by_copilot_session_id("copilot-123")
        assert session is not None
        context = self.store.get_session_context(session.id)
        assert context is not None
        assert context.tmux_pane_id == "%17"
        events = self.store.list_events_for_session(session.id)
        assert [event.kind for event in events] == [
            "agent.discovered",
            "agent.session.observed",
            "agent.blocking_issue",
        ]
        chunks = self.store.list_log_chunks(session.id)
        assert len(chunks) == 1
        assert chunks[0].content.startswith("Copilot session id")


if __name__ == "__main__":
    unittest.main()
