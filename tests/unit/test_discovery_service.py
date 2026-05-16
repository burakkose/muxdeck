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

from muxdeck.adapters.copilot_adapter import CopilotAdapter
from muxdeck.adapters.sqlite_store import SessionContextRecord
from muxdeck.adapters.tmux_adapter import TmuxPaneMetadata
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.models import Agent, Session
from muxdeck.domain.value_objects import CommandResult
from muxdeck.services.discovery_service import DiscoveryService


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
                # Non-shell foreground process: copilot is still
                # actually running here. A shell-foreground pane
                # would be demoted to non_agent_pane (see
                # ``test_managed_agent_pane_demoted_when_copilot_exited``)
                # — the stored session id is not enough to keep an
                # agent alive after copilot CLI has exited.
                pane_current_command="node",
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

    def test_managed_agent_pane_demoted_when_copilot_exited(self) -> None:
        # Operator killed the copilot CLI in a managed pane. The
        # tmux pane survives as a plain shell, and the scrollback
        # still carries the just-exited session's banner / id.
        # Discovery must demote it to ``non_agent_pane`` so the
        # runtime reaper can mark the agent DEAD instead of letting
        # it linger on the dashboard as if copilot were still up.
        existing_agent = Agent(
            id="agent-1",
            name="planner",
            tmux_session_name="muxdeck",
            tmux_window_id="@9",
            tmux_pane_id="%7",
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

        assert len(report.managed_agents) == 0
        assert len(report.non_agent_panes) == 1
        discovery = report.non_agent_panes[0]
        assert discovery.classification == "non_agent_pane"
        assert "copilot CLI no longer running in pane" in discovery.reasons

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


class DiscoveryServiceCaptureCacheTests(unittest.TestCase):
    """A1: per-pane capture cache keyed on ``pane_activity``."""

    def setUp(self) -> None:
        self.now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        self.copilot = CopilotAdapter(DummyRunner())

    def _make_pane(
        self,
        *,
        pane_id: str = "%7",
        pane_pid: int | None = 1234,
        pane_tty: str | None = "/dev/pts/9",
        pane_activity: int | None = 1_700_000_000,
        pane_current_command: str = "node",
    ) -> TmuxPaneMetadata:
        return TmuxPaneMetadata(
            pane_id=pane_id,
            session_name="muxdeck",
            window_id="@1",
            window_name="agents",
            pane_pid=pane_pid,
            pane_tty=pane_tty,
            pane_current_command=pane_current_command,
            pane_current_path="/repo",
            pane_activity=pane_activity,
        )

    def _gateway(
        self,
        panes: tuple[TmuxPaneMetadata, ...],
        captures: dict[str, str],
    ) -> CountingTmuxGateway:
        return CountingTmuxGateway(panes, captures)

    def _service(self, gateway: CountingTmuxGateway) -> DiscoveryService:
        return DiscoveryService(
            gateway,
            self.copilot,
            InMemoryDiscoveryStore(),
            clock=lambda: self.now,
        )

    def test_cache_hit_skips_capture_when_activity_unchanged(self) -> None:
        pane = self._make_pane(pane_activity=1_700_000_500)
        gateway = self._gateway((pane,), {"%7": "Copilot session id: c-1\n"})
        service = self._service(gateway)
        first = service.discover_panes()
        second = service.discover_panes()
        # capture-pane forks exactly once across both cycles.
        assert gateway.capture_calls == {"%7": 1}
        # Both cycles return the same classification + session evidence.
        assert first.panes[0].session_evidence is not None
        assert second.panes[0].session_evidence is first.panes[0].session_evidence
        assert first.panes[0].captured_output == second.panes[0].captured_output
        # Each cycle still re-stamps ``discovered_at`` to the current
        # clock so downstream consumers see fresh metadata.
        assert second.discovered_at == self.now

    def test_cache_invalidated_when_pane_activity_advances(self) -> None:
        pane_v1 = self._make_pane(pane_activity=1_700_000_500)
        pane_v2 = self._make_pane(pane_activity=1_700_000_501)
        captures = {"%7": "Copilot session id: c-1\n"}
        gateway = self._gateway((pane_v1,), captures)
        service = self._service(gateway)
        service.discover_panes()
        gateway.panes = (pane_v2,)
        service.discover_panes()
        assert gateway.capture_calls == {"%7": 2}

    def test_cache_invalidated_when_pane_pid_changes(self) -> None:
        pane_v1 = self._make_pane(pane_pid=1234, pane_activity=1_700_000_500)
        pane_v2 = self._make_pane(pane_pid=4321, pane_activity=1_700_000_500)
        gateway = self._gateway((pane_v1,), {"%7": "x"})
        service = self._service(gateway)
        service.discover_panes()
        gateway.panes = (pane_v2,)
        service.discover_panes()
        assert gateway.capture_calls == {"%7": 2}

    def test_cache_invalidated_when_pane_tty_changes(self) -> None:
        pane_v1 = self._make_pane(pane_tty="/dev/pts/9", pane_activity=1_700_000_500)
        pane_v2 = self._make_pane(pane_tty="/dev/pts/12", pane_activity=1_700_000_500)
        gateway = self._gateway((pane_v1,), {"%7": "x"})
        service = self._service(gateway)
        service.discover_panes()
        gateway.panes = (pane_v2,)
        service.discover_panes()
        assert gateway.capture_calls == {"%7": 2}

    def test_missing_pane_activity_forces_recapture_every_cycle(self) -> None:
        # Legacy tmux (or panes that have never produced output) report
        # pane_activity as None — the optimization must degrade
        # gracefully and never cache, so behaviour matches pre-A1 code.
        pane = self._make_pane(pane_activity=None)
        gateway = self._gateway((pane,), {"%7": "x"})
        service = self._service(gateway)
        service.discover_panes()
        service.discover_panes()
        assert gateway.capture_calls == {"%7": 2}

    def test_evicts_cache_for_panes_that_disappear(self) -> None:
        pane = self._make_pane(pane_id="%7", pane_activity=1_700_000_500)
        gateway = self._gateway((pane,), {"%7": "x"})
        service = self._service(gateway)
        service.discover_panes()
        # Pane goes away.
        gateway.panes = ()
        service.discover_panes()
        # Pane reappears with the same id and same activity timestamp:
        # cache was evicted, so capture must run again.
        gateway.panes = (pane,)
        service.discover_panes()
        assert gateway.capture_calls == {"%7": 2}


class DiscoveryServiceCaptureSkipTests(unittest.TestCase):
    """A5: skip capture+interpret when the pane is provably non-agent."""

    def setUp(self) -> None:
        self.now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        self.copilot = CopilotAdapter(DummyRunner())

    def _pane(
        self,
        *,
        pane_id: str = "%7",
        command: str = "claude",
        pane_activity: int | None = 1_700_000_500,
    ) -> TmuxPaneMetadata:
        return TmuxPaneMetadata(
            pane_id=pane_id,
            session_name="muxdeck",
            window_id="@1",
            window_name="ai",
            pane_pid=1234,
            pane_tty="/dev/pts/9",
            pane_current_command=command,
            pane_current_path="/repo",
            pane_activity=pane_activity,
        )

    def _service(
        self,
        gateway: CountingTmuxGateway,
        *,
        store: InMemoryDiscoveryStore | None = None,
    ) -> DiscoveryService:
        return DiscoveryService(
            gateway,
            self.copilot,
            store if store is not None else InMemoryDiscoveryStore(),
            clock=lambda: self.now,
        )

    def test_known_non_copilot_command_skips_capture_entirely(self) -> None:
        pane = self._pane(command="claude")
        gateway = CountingTmuxGateway((pane,), {"%7": "claude transcript output"})
        report = self._service(gateway).discover_panes()
        # No capture-pane fork at all — A5 short-circuits before A1's
        # capture call site.
        assert gateway.capture_calls == {}
        # classify_pane still runs and tags the pane as non-agent.
        assert len(report.non_agent_panes) == 1
        non_agent = report.non_agent_panes[0]
        assert non_agent.captured_output is None
        assert non_agent.session_evidence is None
        assert "known non-copilot AI CLI" in non_agent.reasons

    def test_skip_disabled_when_managed_agent_owns_pane(self) -> None:
        existing_agent = Agent(
            id="agent-1",
            name="planner",
            tmux_session_name="muxdeck",
            tmux_window_id="@1",
            tmux_pane_id="%7",
            cwd="/repo",
            status=AgentStatus.RUNNING,
            started_at=self.now,
            last_seen_at=self.now,
        )
        pane = self._pane(command="claude")
        gateway = CountingTmuxGateway((pane,), {"%7": "scrollback"})
        store = InMemoryDiscoveryStore(agent=existing_agent)
        report = self._service(gateway, store=store).discover_panes()
        # Stored agent association forces the capture path so that
        # the demotion-to-non-agent transition stays observable.
        assert gateway.capture_calls == {"%7": 1}
        assert len(report.non_agent_panes) == 1

    def test_skip_disabled_when_session_context_owns_pane(self) -> None:
        pane = self._pane(command="claude")
        gateway = CountingTmuxGateway((pane,), {"%7": "scrollback"})
        store = InMemoryDiscoveryStore(
            context=SessionContextRecord(
                session_id="session-x", tmux_pane_id="%7", updated_at=self.now
            )
        )
        report = self._service(gateway, store=store).discover_panes()
        assert gateway.capture_calls == {"%7": 1}
        # Pane still ends up non-agent because the command is a
        # known non-copilot AI CLI, but we paid for the capture to
        # confirm — the session context owner deserves the chance
        # to observe scrollback changes.
        assert len(report.non_agent_panes) == 1

    def test_skip_disabled_for_shell_panes(self) -> None:
        # Shells stay on the capture path so previously-running
        # copilot sessions can still be inferred from scrollback.
        pane = self._pane(command="bash")
        gateway = CountingTmuxGateway((pane,), {"%7": "$ ls\nfile1\nfile2\n"})
        report = self._service(gateway).discover_panes()
        assert gateway.capture_calls == {"%7": 1}
        assert len(report.non_agent_panes) == 1


class CountingTmuxGateway:
    def __init__(
        self,
        panes: tuple[TmuxPaneMetadata, ...],
        captures: dict[str, str],
    ) -> None:
        self.panes: tuple[TmuxPaneMetadata, ...] = panes
        self._captures = captures
        self.capture_calls: dict[str, int] = {}

    def list_panes(self) -> tuple[TmuxPaneMetadata, ...]:
        return self.panes

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
        self.capture_calls[target_pane] = self.capture_calls.get(target_pane, 0) + 1
        return self._captures.get(target_pane, "")


if __name__ == "__main__":
    unittest.main()
