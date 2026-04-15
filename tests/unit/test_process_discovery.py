"""Tests for process-tree-based agent discovery and UI marker detection."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from copilot_commander.adapters.copilot_adapter import (
    CopilotAdapter,
    CopilotCommandDetection,
    CopilotSessionEvidence,
)
from copilot_commander.adapters.process_adapter import ProcessAdapter
from copilot_commander.adapters.sqlite_store import SessionContextRecord
from copilot_commander.adapters.tmux_adapter import TmuxPaneMetadata
from copilot_commander.domain.models import Agent, Session
from copilot_commander.domain.value_objects import CommandResult
from copilot_commander.parsers.copilot_output_parser import parse_copilot_output
from copilot_commander.services.discovery_service import (
    DiscoveryService,
    _has_session_signal,
)

_NOW = datetime(2026, 4, 15, 4, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeProcessInspector:
    """Simulate reading child process cmdlines from /proc."""

    def __init__(self, cmdlines: tuple[str, ...] = ()) -> None:
        self.cmdlines = cmdlines
        self.inspected_pids: list[int] = []

    def get_child_cmdlines(self, pid: int, /) -> tuple[str, ...]:
        self.inspected_pids.append(pid)
        return self.cmdlines


class FakeProcessInspectorRaises:
    """Simulate /proc read failure."""

    def get_child_cmdlines(self, pid: int, /) -> tuple[str, ...]:
        raise OSError("permission denied")


class FakeTmux:
    """Minimal tmux gateway for discovery tests."""

    def __init__(
        self,
        panes: list[TmuxPaneMetadata],
        captured_output: str = "",
    ) -> None:
        self._panes = panes
        self._captured_output = captured_output

    def list_panes(self) -> list[TmuxPaneMetadata]:
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
        return self._captured_output


class FakeCopilot:
    """Minimal copilot gateway that delegates to real detect_command."""

    def __init__(self) -> None:
        self._adapter = CopilotAdapter(command_runner=_NullRunner())

    def detect_command(self, candidate: str | tuple[str, ...], /) -> CopilotCommandDetection:
        return self._adapter.detect_command(candidate)

    def interpret_output(self, output: str, /) -> CopilotSessionEvidence:
        return self._adapter.interpret_output(output)


class _NullRunner:
    def run(self, command: Sequence[str], **kwargs: object) -> CommandResult:
        raise NotImplementedError


class FakeStore:
    """Empty store — no stored agents/sessions."""

    def get_agent_by_pane_id(self, pane_id: str, /) -> Agent | None:
        return None

    def get_agent_by_copilot_session_id(self, copilot_session_id: str, /) -> Agent | None:
        return None

    def get_session_by_copilot_session_id(self, copilot_session_id: str, /) -> Session | None:
        return None

    def get_session_context_by_tmux_pane_id(
        self,
        tmux_pane_id: str,
        /,
    ) -> SessionContextRecord | None:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pane(
    pane_id: str = "%0",
    pane_current_command: str = "node",
    pane_pid: int = 1234,
    session_name: str = "main",
    window_id: str = "@0",
) -> TmuxPaneMetadata:
    return TmuxPaneMetadata(
        pane_id=pane_id,
        session_name=session_name,
        window_id=window_id,
        pane_current_command=pane_current_command,
        pane_pid=pane_pid,
    )


# ---------------------------------------------------------------------------
# Process tree inspection tests
# ---------------------------------------------------------------------------


class TestProcessTreeDetection:
    """Discovery detects copilot via child process cmdlines."""

    def test_node_running_copilot_detected(self) -> None:
        """When 'node' is the pane command but child runs copilot binary."""
        inspector = FakeProcessInspector(cmdlines=("node /usr/local/bin/copilot --resume=abc-123",))
        pane = _make_pane(pane_current_command="node", pane_pid=2026)
        service = DiscoveryService(
            tmux=FakeTmux([pane]),
            copilot=FakeCopilot(),
            store=FakeStore(),
            process_inspector=inspector,
            clock=lambda: _NOW,
        )
        report = service.discover_panes()
        assert len(report.panes) == 1
        discovery = report.panes[0]
        assert discovery.command_detection.is_likely_copilot is True
        assert discovery.classification in ("managed_agent", "unmanaged_probable_agent")
        assert 2026 in inspector.inspected_pids

    def test_node_running_copilot_chat_detected(self) -> None:
        """Child process running 'copilot chat' via gh extension."""
        inspector = FakeProcessInspector(cmdlines=("gh copilot chat --model opus",))
        pane = _make_pane(pane_current_command="node", pane_pid=3000)
        service = DiscoveryService(
            tmux=FakeTmux([pane]),
            copilot=FakeCopilot(),
            store=FakeStore(),
            process_inspector=inspector,
            clock=lambda: _NOW,
        )
        report = service.discover_panes()
        assert report.panes[0].command_detection.is_likely_copilot is True

    def test_non_copilot_node_process(self) -> None:
        """Child process is a regular node app, not copilot."""
        inspector = FakeProcessInspector(cmdlines=("node /app/server.js",))
        pane = _make_pane(pane_current_command="node", pane_pid=4000)
        service = DiscoveryService(
            tmux=FakeTmux([pane]),
            copilot=FakeCopilot(),
            store=FakeStore(),
            process_inspector=inspector,
            clock=lambda: _NOW,
        )
        report = service.discover_panes()
        assert report.panes[0].command_detection.is_likely_copilot is False
        assert report.panes[0].classification == "non_agent_pane"

    def test_process_inspector_error_is_graceful(self) -> None:
        """If /proc reading fails, fallback to command-only detection."""
        pane = _make_pane(pane_current_command="node", pane_pid=5000)
        service = DiscoveryService(
            tmux=FakeTmux([pane]),
            copilot=FakeCopilot(),
            store=FakeStore(),
            process_inspector=FakeProcessInspectorRaises(),
            clock=lambda: _NOW,
        )
        report = service.discover_panes()
        assert report.panes[0].command_detection.is_likely_copilot is False

    def test_no_process_inspector_skips_tree_check(self) -> None:
        """Without process inspector, detection uses command name only."""
        pane = _make_pane(pane_current_command="node", pane_pid=6000)
        service = DiscoveryService(
            tmux=FakeTmux([pane]),
            copilot=FakeCopilot(),
            store=FakeStore(),
            process_inspector=None,
            clock=lambda: _NOW,
        )
        report = service.discover_panes()
        assert report.panes[0].command_detection.is_likely_copilot is False

    def test_no_pane_pid_skips_tree_check(self) -> None:
        """Without pane_pid, process tree inspection is skipped."""
        inspector = FakeProcessInspector(cmdlines=("node /usr/local/bin/copilot",))
        pane = _make_pane(pane_current_command="node", pane_pid=None)  # type: ignore[arg-type]
        # Force pane_pid to None for this test
        pane = TmuxPaneMetadata(
            pane_id="%0",
            session_name="main",
            window_id="@0",
            pane_current_command="node",
            pane_pid=None,
        )
        service = DiscoveryService(
            tmux=FakeTmux([pane]),
            copilot=FakeCopilot(),
            store=FakeStore(),
            process_inspector=inspector,
            clock=lambda: _NOW,
        )
        report = service.discover_panes()
        _ = report  # exercise the code path
        assert inspector.inspected_pids == []

    def test_direct_copilot_command_skips_tree_check(self) -> None:
        """When pane_current_command is already 'copilot', no tree check needed."""
        inspector = FakeProcessInspector()
        pane = _make_pane(pane_current_command="copilot", pane_pid=7000)
        service = DiscoveryService(
            tmux=FakeTmux([pane]),
            copilot=FakeCopilot(),
            store=FakeStore(),
            process_inspector=inspector,
            clock=lambda: _NOW,
        )
        report = service.discover_panes()
        assert report.panes[0].command_detection.is_likely_copilot is True
        assert inspector.inspected_pids == []


# ---------------------------------------------------------------------------
# UI marker detection tests (output parser)
# ---------------------------------------------------------------------------


class TestCopilotUIMarkerDetection:
    """Parser recognizes Copilot CLI visual markers in pane output."""

    def test_slash_commands_detected(self) -> None:
        output = " / commands · ? help · ctrl+q enqueue"
        result = parse_copilot_output(output)
        assert len(result.ui_markers) >= 1
        kinds = {m.kind for m in result.ui_markers}
        assert "slash_commands" in kinds

    def test_autopilot_prompt_detected(self) -> None:
        output = " autopilot · / commands"
        result = parse_copilot_output(output)
        kinds = {m.kind for m in result.ui_markers}
        assert "autopilot_prompt" in kinds

    def test_ctrl_q_enqueue_detected(self) -> None:
        output = " / commands · ? help · ctrl+q enqueue"
        result = parse_copilot_output(output)
        kinds = {m.kind for m in result.ui_markers}
        assert "enqueue_binding" in kinds

    def test_esc_to_cancel_detected(self) -> None:
        output = "● Debugging agent discovery (Esc to cancel · 4.3 KiB)"
        result = parse_copilot_output(output)
        kinds = {m.kind for m in result.ui_markers}
        assert "esc_to_cancel" in kinds

    def test_claude_model_detected(self) -> None:
        output = " ~/projects/muxdeck [main]  Claude Opus 4.6 (3x) (high)"
        result = parse_copilot_output(output)
        kinds = {m.kind for m in result.ui_markers}
        assert "copilot_model" in kinds

    def test_gpt_model_detected(self) -> None:
        output = " ~/projects/foo [main]  GPT-5.1 (2x) (high)"
        result = parse_copilot_output(output)
        kinds = {m.kind for m in result.ui_markers}
        assert "copilot_model" in kinds

    def test_no_markers_in_regular_output(self) -> None:
        output = "ls\nREADME.md  src  tests\n$ "
        result = parse_copilot_output(output)
        assert len(result.ui_markers) == 0

    def test_real_copilot_pane_output(self) -> None:
        """Test with realistic Copilot CLI pane output."""
        output = (
            "\u25cf Debugging agent discovery (Esc to cancel \u00b7 4.3 KiB)\n"
            "\n"
            " ~/projects/muxdeck [main]                 Claude Opus 4.6 (3x) (high)\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            ">\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            " / commands \u00b7 ? help \u00b7 ctrl+q enqueue\n"
        )
        result = parse_copilot_output(output)
        assert len(result.ui_markers) >= 3
        kinds = {m.kind for m in result.ui_markers}
        assert "esc_to_cancel" in kinds
        assert "copilot_model" in kinds
        assert "slash_commands" in kinds


class TestUIMarkersInDiscoverySignal:
    """UI markers should trigger _has_session_signal."""

    def test_ui_markers_count_as_signal(self) -> None:
        """Pane output with Copilot UI markers produces session signal."""
        copilot = FakeCopilot()
        output = (
            "● Running tests (Esc to cancel · 2.1 KiB)\n"
            " ~/projects/foo [main]  Claude Sonnet 4.5 (1x) (high)\n"
            " / commands · ? help · ctrl+q enqueue\n"
        )
        evidence = copilot.interpret_output(output)
        assert _has_session_signal(evidence) is True

    def test_no_markers_no_signal(self) -> None:
        copilot = FakeCopilot()
        evidence = copilot.interpret_output("just some regular shell output")
        assert _has_session_signal(evidence) is False

    def test_discovery_classifies_via_output_markers(self) -> None:
        """Even without process tree, UI markers in output classify pane as agent."""
        copilot_output = (
            "● Working on task (Esc to cancel · 1.5 KiB)\n"
            " ~/projects/test [main]  Claude Opus 4.6 (2x) (high)\n"
            " / commands · ? help · ctrl+q enqueue\n"
        )
        pane = _make_pane(pane_current_command="node", pane_pid=8000)
        service = DiscoveryService(
            tmux=FakeTmux([pane], captured_output=copilot_output),
            copilot=FakeCopilot(),
            store=FakeStore(),
            process_inspector=None,
            clock=lambda: _NOW,
        )
        report = service.discover_panes()
        assert report.panes[0].classification == "unmanaged_probable_agent"


# ---------------------------------------------------------------------------
# ProcessAdapter.get_child_cmdlines unit tests
# ---------------------------------------------------------------------------


class TestProcessAdapterChildCmdlines:
    """ProcessAdapter reads /proc for child cmdlines."""

    def test_returns_empty_for_nonexistent_pid(self) -> None:
        adapter = ProcessAdapter()
        result = adapter.get_child_cmdlines(999999999)
        assert result == ()

    def test_returns_tuple(self) -> None:
        adapter = ProcessAdapter()
        result = adapter.get_child_cmdlines(1)
        assert isinstance(result, tuple)
