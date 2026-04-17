# ruff: noqa: E402,E501,ANN001,ANN201

from __future__ import annotations

import sys
import unittest
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from copilot_commander.adapters.copilot_adapter import CopilotAdapter
from copilot_commander.adapters.sqlite_store import SessionContextRecord
from copilot_commander.adapters.tmux_adapter import TmuxPaneMetadata
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.models import Agent, Session
from copilot_commander.domain.value_objects import CommandResult
from copilot_commander.services.discovery_service import DiscoveryService


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


class FakeTmuxGateway:
    def __init__(
        self,
        panes: tuple[TmuxPaneMetadata, ...],
        captures: dict[str, str],
    ) -> None:
        self._panes = panes
        self._captures = captures

    def list_panes(self) -> tuple[TmuxPaneMetadata, ...]:
        return self._panes

    def capture_pane(
        self,
        target_pane: str,
        /,
        *,
        start_line: str | int | None = None,
        end_line: str | int | None = None,
        join_wrapped_lines: bool = False,
    ) -> str:
        del start_line, end_line, join_wrapped_lines
        return self._captures[target_pane]


class InMemoryDiscoveryStore:
    def __init__(
        self,
        *,
        agent: Agent | None = None,
        session: Session | None = None,
        context: SessionContextRecord | None = None,
    ) -> None:
        self.agent = agent
        self.session = session
        self.context = context

    def get_agent_by_pane_id(self, pane_id: str, /) -> Agent | None:
        if self.agent is not None and self.agent.tmux_pane_id == pane_id:
            return self.agent
        return None

    def get_agent_by_copilot_session_id(self, copilot_session_id: str, /) -> Agent | None:
        if self.agent is not None and self.agent.copilot_session_id == copilot_session_id:
            return self.agent
        return None

    def get_session_by_copilot_session_id(self, copilot_session_id: str, /) -> Session | None:
        if self.session is not None and self.session.copilot_session_id == copilot_session_id:
            return self.session
        return None

    def get_session_context_by_tmux_pane_id(
        self, tmux_pane_id: str, /
    ) -> SessionContextRecord | None:
        if self.context is not None and self.context.tmux_pane_id == tmux_pane_id:
            return self.context
        return None


class DiscoveryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        self.copilot = CopilotAdapter(DummyRunner())

    def test_discovers_managed_agent_from_copilot_session_match(self) -> None:
        existing_agent = Agent(
            id="agent-1",
            name="planner",
            tmux_session_name="muxdeck",
            tmux_window_id="@9",
            tmux_pane_id="%42",
            cwd="/repo/worktrees/task-one",
            copilot_session_id="copilot-123",
            status=AgentStatus.RUNNING,
            started_at=self.now,
            last_seen_at=self.now,
        )
        existing_session = Session(
            id="session-1",
            agent_id="agent-1",
            copilot_session_id="copilot-123",
            created_at=self.now,
        )
        panes = (
            TmuxPaneMetadata(
                pane_id="%7",
                session_name="muxdeck",
                window_id="@1",
                window_name="agents",
                pane_current_command="bash",
                pane_current_path="/repo/worktrees/task-one",
            ),
        )
        tmux = FakeTmuxGateway(panes, {"%7": "Copilot session id: copilot-123\nPrompt: status"})
        service = DiscoveryService(
            tmux,
            self.copilot,
            InMemoryDiscoveryStore(agent=existing_agent, session=existing_session),
            clock=lambda: self.now,
        )

        report = service.discover_panes()

        assert len(report.managed_agents) == 1
        discovery = report.managed_agents[0]
        assert discovery.classification == "managed_agent"
        assert discovery.managed_agent is existing_agent
        assert discovery.matched_session is existing_session
        assert "matched stored agent" in discovery.reasons

    def test_discovers_unmanaged_probable_agent_from_command_and_output(self) -> None:
        panes = (
            TmuxPaneMetadata(
                pane_id="%8",
                session_name="muxdeck",
                window_id="@2",
                window_name="agents",
                pane_current_command="copilot chat",
                pane_current_path="/repo/worktrees/task-two",
            ),
        )
        tmux = FakeTmuxGateway(panes, {"%8": "input_tokens: 12\noutput_tokens: 5"})
        service = DiscoveryService(
            tmux,
            self.copilot,
            InMemoryDiscoveryStore(),
            clock=lambda: self.now,
        )

        report = service.discover_panes()

        assert len(report.unmanaged_probable_agents) == 1
        discovery = report.unmanaged_probable_agents[0]
        assert discovery.command_detection.is_likely_copilot is True
        assert discovery.session_evidence is not None
        assert discovery.session_evidence.latest_usage is not None

    def test_discovers_non_agent_pane_without_copilot_signals(self) -> None:
        panes = (
            TmuxPaneMetadata(
                pane_id="%9",
                session_name="muxdeck",
                window_id="@3",
                window_name="logs",
                pane_current_command="tail",
                pane_current_path="/repo",
            ),
        )
        tmux = FakeTmuxGateway(panes, {"%9": "plain log output"})
        service = DiscoveryService(
            tmux,
            self.copilot,
            InMemoryDiscoveryStore(
                context=SessionContextRecord(
                    session_id="session-x", tmux_pane_id="%100", updated_at=self.now
                )
            ),
            clock=lambda: self.now,
        )

        report = service.discover_panes()

        assert len(report.non_agent_panes) == 1
        assert report.non_agent_panes[0].classification == "non_agent_pane"
        assert report.non_agent_panes[0].managed_agent is None


if __name__ == "__main__":
    unittest.main()
