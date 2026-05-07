"""Tests for TmuxActionService."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from muxdeck.adapters.copilot_adapter import CopilotLaunchParameters
from muxdeck.adapters.tmux_adapter import TmuxPaneMetadata, TmuxWindowInfo
from muxdeck.controllers.agent_controller import (
    AgentIntentView,
    AgentTargetView,
)
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.value_objects import CommandResult
from muxdeck.services.action_service import (
    ActionModelHint,
    ActionResult,
    TmuxActionService,
    WindowChoice,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class SendKeysCall:
    target_pane: str
    keys: Sequence[str]
    literal: bool
    append_enter: bool


_TIMESTAMP = datetime(2025, 1, 1, 12, tzinfo=UTC)


def _command_result(command: tuple[str, ...] = ("tmux",)) -> CommandResult:
    return CommandResult(
        command=command,
        exit_code=0,
        started_at=_TIMESTAMP,
        finished_at=_TIMESTAMP,
    )


@dataclass
class FakeCopilot:
    launch_calls: list[CopilotLaunchParameters] = field(default_factory=list)
    current_model: str | None = "gpt-5.4"

    def launch_in_pane(self, parameters: CopilotLaunchParameters, /) -> object:
        self.launch_calls.append(parameters)
        return object()

    def configured_model(self) -> str | None:
        return self.current_model


@dataclass
class FakeTmux:
    """Minimal fake satisfying TmuxOperations protocol."""

    existing_panes: set[str] = field(default_factory=set)
    captured_text: str = ""
    operation_calls: list[tuple[str, str]] = field(default_factory=list)
    select_window_calls: list[str] = field(default_factory=list)
    select_pane_calls: list[str] = field(default_factory=list)
    switch_client_calls: list[str] = field(default_factory=list)
    has_client: bool = True
    send_keys_calls: list[SendKeysCall] = field(default_factory=list)
    capture_calls: list[tuple[str, str | int | None]] = field(default_factory=list)
    new_window_pane_id: str = "%10"
    new_window_calls: list[tuple[str | None, str | None, object | None, bool]] = field(
        default_factory=list
    )
    rename_window_calls: list[tuple[str, str]] = field(default_factory=list)
    kill_pane_calls: list[str] = field(default_factory=list)
    break_pane_calls: list[tuple[str, str | None, str | None, bool]] = field(default_factory=list)
    join_pane_calls: list[tuple[str, str, bool, bool]] = field(default_factory=list)
    windows: tuple[TmuxWindowInfo, ...] = (
        TmuxWindowInfo("muxdeck", "@2", window_name="editor", pane_ids=("%5",)),
        TmuxWindowInfo("muxdeck", "@3", window_name="review", pane_ids=("%7", "%8")),
    )

    def list_windows(self) -> tuple[TmuxWindowInfo, ...]:
        return self.windows

    def pane_exists(self, target_pane: str, /) -> bool:
        return target_pane in self.existing_panes

    def select_pane(self, target_pane: str, /) -> CommandResult:
        self.operation_calls.append(("select_pane", target_pane))
        self.select_pane_calls.append(target_pane)
        return _command_result(("tmux", "select-pane"))

    def select_window(self, target_window: str, /) -> CommandResult:
        self.operation_calls.append(("select_window", target_window))
        self.select_window_calls.append(target_window)
        return _command_result(("tmux", "select-window"))

    def switch_client(self, target: str, /) -> CommandResult:
        self.operation_calls.append(("switch_client", target))
        self.switch_client_calls.append(target)
        return _command_result(("tmux", "switch-client"))

    def has_attached_client(self) -> bool:
        return self.has_client

    def send_keys(
        self,
        target_pane: str,
        keys: Sequence[str],
        /,
        *,
        literal: bool = False,
        append_enter: bool = False,
    ) -> CommandResult:
        self.send_keys_calls.append(SendKeysCall(target_pane, keys, literal, append_enter))
        return _command_result(("tmux", "send-keys"))

    def capture_pane(
        self,
        target_pane: str,
        /,
        *,
        start_line: str | int | None = None,
        end_line: str | int | None = None,
        join_wrapped_lines: bool = False,
    ) -> str:
        self.capture_calls.append((target_pane, start_line))
        return self.captured_text

    def new_window(
        self,
        target_session: str | None = None,
        /,
        *,
        window_name: str | None = None,
        start_directory: object | None = None,
        shell_command: Sequence[str] | None = None,
        detached: bool = False,
    ) -> TmuxPaneMetadata:
        self.new_window_calls.append((target_session, window_name, start_directory, detached))
        return TmuxPaneMetadata(
            pane_id=self.new_window_pane_id,
            session_name=target_session,
            window_id="@10",
            window_name=window_name,
        )

    def break_pane(
        self,
        source_pane: str,
        /,
        *,
        window_name: str | None = None,
        target_window: str | None = None,
        detached: bool = True,
    ) -> TmuxPaneMetadata:
        self.break_pane_calls.append((source_pane, window_name, target_window, detached))
        return TmuxPaneMetadata(
            pane_id=source_pane,
            session_name="muxdeck",
            window_id="@20",
            window_name=window_name or "new-window",
        )

    def join_pane(
        self,
        source_pane: str,
        target_pane: str,
        /,
        *,
        detached: bool = True,
        vertical: bool = True,
    ) -> TmuxPaneMetadata:
        self.join_pane_calls.append((source_pane, target_pane, detached, vertical))
        return TmuxPaneMetadata(
            pane_id=source_pane,
            session_name="muxdeck",
            window_id=target_pane,
            window_name="joined-window",
        )

    def rename_window(self, target_window: str, new_name: str, /) -> CommandResult:
        self.rename_window_calls.append((target_window, new_name))
        return _command_result(("tmux", "rename-window"))

    def kill_pane(self, target_pane: str, /) -> CommandResult:
        self.kill_pane_calls.append(target_pane)
        return _command_result(("tmux", "kill-pane"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TARGET = AgentTargetView(
    agent_id="agent-1",
    name="test-agent",
    status=AgentStatus.RUNNING,
    pane_target="%5",
    tmux_session_name="muxdeck",
    tmux_window_id="@2",
    worktree_path="/home/user/repo",
    repo_root="/home/user/repo",
    branch="main",
    latest_session_id="sess-1",
)


def _intent(
    kind: str,
    *,
    metadata: tuple[tuple[str, str], ...] = (),
    prompt: str | None = None,
) -> AgentIntentView:
    return AgentIntentView(
        kind=kind,  # type: ignore[arg-type]
        agent=_TARGET,
        label=f"Test {kind}",
        metadata=metadata,
        prompt=prompt,
    )


# ---------------------------------------------------------------------------
# focus_pane
# ---------------------------------------------------------------------------


class TestFocusPane:
    def test_success(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)

        result = svc.focus_pane("%5")

        assert result.success is True
        assert result.pane_id == "%5"
        assert tmux.select_window_calls == []
        assert tmux.select_pane_calls == ["%5"]
        assert tmux.switch_client_calls == ["%5"]

    def test_cross_window_focus_switches_session_then_window_then_pane(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)

        result = svc.focus_pane("%5", window_id="@42", session_name="muxdeck")

        assert result.success is True
        assert tmux.select_pane_calls == ["%5"]
        assert tmux.select_window_calls == ["@42"]
        assert tmux.switch_client_calls == ["muxdeck"]
        assert tmux.operation_calls == [
            ("switch_client", "muxdeck"),
            ("select_window", "@42"),
            ("select_pane", "%5"),
        ]

    def test_no_attached_client_reports_advisory(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"}, has_client=False)
        svc = TmuxActionService(tmux)

        result = svc.focus_pane("%5", window_id="@42")

        assert result.success is True
        assert tmux.select_pane_calls == ["%5"]
        assert tmux.select_window_calls == ["@42"]
        assert tmux.switch_client_calls == []
        assert "no attached tmux client" in result.message

    def test_pane_missing(self) -> None:
        tmux = FakeTmux(existing_panes=set())
        svc = TmuxActionService(tmux)

        result = svc.focus_pane("%99")

        assert result.success is False
        assert "%99" in result.message
        assert tmux.select_window_calls == []
        assert tmux.select_pane_calls == []

    def test_cross_window_focus_selects_window_before_pane(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)

        result = svc.focus_pane(
            "%5",
            window_id="@2",
            session_name="muxdeck",
        )

        assert result.success is True
        assert tmux.select_window_calls == ["@2"]
        assert tmux.select_pane_calls == ["%5"]
        assert tmux.operation_calls[:2] == [
            ("switch_client", "muxdeck"),
            ("select_window", "@2"),
        ]

    def test_execute_intent_open_pane_uses_agent_window_context(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)

        result = svc.execute_intent(_intent("open_pane"))

        assert result.success is True
        assert tmux.switch_client_calls == ["muxdeck"]
        assert tmux.select_window_calls == ["@2"]
        assert tmux.select_pane_calls == ["%5"]


# ---------------------------------------------------------------------------
# interrupt_pane
# ---------------------------------------------------------------------------


class TestInterruptPane:
    def test_success(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)

        result = svc.interrupt_pane("%5")

        assert result.success is True
        assert len(tmux.send_keys_calls) == 1
        call = tmux.send_keys_calls[0]
        assert list(call.keys) == ["C-c"]
        assert call.literal is False
        assert call.append_enter is False

    def test_pane_missing(self) -> None:
        tmux = FakeTmux(existing_panes=set())
        svc = TmuxActionService(tmux)

        result = svc.interrupt_pane("%5")

        assert result.success is False
        assert tmux.send_keys_calls == []


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


class TestSendMessage:
    def test_success(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)

        result = svc.send_message("%5", "hello world")

        assert result.success is True
        assert len(tmux.send_keys_calls) == 1
        call = tmux.send_keys_calls[0]
        assert list(call.keys) == ["hello world"]
        assert call.literal is True
        assert call.append_enter is True

    def test_empty_text(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)

        result = svc.send_message("%5", "")

        assert result.success is False
        assert "empty" in result.message
        assert tmux.send_keys_calls == []

    def test_whitespace_only_text(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)

        result = svc.send_message("%5", "   \t  ")

        assert result.success is False
        assert tmux.send_keys_calls == []

    def test_pane_missing(self) -> None:
        tmux = FakeTmux(existing_panes=set())
        svc = TmuxActionService(tmux)

        result = svc.send_message("%5", "hello")

        assert result.success is False
        assert "%5" in result.message
        assert tmux.send_keys_calls == []

    def test_special_characters(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)

        result = svc.send_message("%5", 'echo "foo" && bar | baz')

        assert result.success is True
        call = tmux.send_keys_calls[0]
        assert list(call.keys) == ['echo "foo" && bar | baz']
        assert call.literal is True


# ---------------------------------------------------------------------------
# capture_output
# ---------------------------------------------------------------------------


class TestCaptureOutput:
    def test_default_lines(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"}, captured_text="line1\nline2")
        svc = TmuxActionService(tmux)

        output = svc.capture_output("%5")

        assert output == "line1\nline2"
        assert tmux.capture_calls == [("%5", -50)]

    def test_custom_lines(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"}, captured_text="data")
        svc = TmuxActionService(tmux)

        output = svc.capture_output("%5", lines=10)

        assert output == "data"
        assert tmux.capture_calls == [("%5", -10)]


# ---------------------------------------------------------------------------
# execute_intent
# ---------------------------------------------------------------------------


class TestExecuteIntent:
    def test_open_pane(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)
        intent = _intent("open_pane", metadata=(("pane_target", "%5"),))

        result = svc.execute_intent(intent)

        assert result.success is True
        assert tmux.select_pane_calls == ["%5"]

    def test_interrupt(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)
        intent = _intent("interrupt", metadata=(("pane_target", "%5"), ("key", "C-c")))

        result = svc.execute_intent(intent)

        assert result.success is True
        assert list(tmux.send_keys_calls[0].keys) == ["C-c"]

    def test_send_input(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)
        intent = _intent(
            "send_input",
            metadata=(("pane_target", "%5"), ("append_enter", "true")),
            prompt="run tests",
        )

        result = svc.execute_intent(intent)

        assert result.success is True
        call = tmux.send_keys_calls[0]
        assert list(call.keys) == ["run tests"]
        assert call.literal is True
        assert call.append_enter is True

    def test_send_input_empty_prompt(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)
        intent = _intent(
            "send_input",
            metadata=(("pane_target", "%5"),),
            prompt="",
        )

        result = svc.execute_intent(intent)

        assert result.success is False

    def test_send_input_no_prompt(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)
        intent = _intent(
            "send_input",
            metadata=(("pane_target", "%5"),),
            prompt=None,
        )

        result = svc.execute_intent(intent)

        assert result.success is False

    def test_open_worktree(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)
        intent = _intent("open_worktree", metadata=(("path", "/home/user/repo"),))

        result = svc.execute_intent(intent)

        assert result.success is True
        assert "/home/user/repo" in result.message

    def test_restart_succeeds(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)
        intent = _intent(
            "restart",
            metadata=(("pane_target", "%5"), ("task_title", "fix bug")),
        )

        result = svc.execute_intent(intent)

        assert result.success is True
        assert "restart" in result.message.lower()

    def test_open_pane_falls_back_to_agent_pane_target(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)
        intent = _intent("open_pane", metadata=())

        result = svc.execute_intent(intent)

        assert result.success is True
        assert tmux.select_pane_calls == ["%5"]

    def test_interrupt_falls_back_to_agent_pane_target(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)
        intent = _intent("interrupt", metadata=())

        result = svc.execute_intent(intent)

        assert result.success is True

    def test_open_worktree_empty_path(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)
        intent = _intent("open_worktree", metadata=())

        result = svc.execute_intent(intent)

        assert result.success is True
        assert "worktree path:" in result.message

    def test_rename_window(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)
        intent = _intent(
            "rename_window",
            metadata=(("window_target", "@2"), ("window_name", "agent-ui")),
        )

        result = svc.execute_intent(intent)

        assert result.success is True
        assert tmux.rename_window_calls == [("@2", "agent-ui")]

    def test_move_to_existing_window(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)
        intent = _intent(
            "move_to_window",
            metadata=(("pane_target", "%5"), ("window_target", "@3")),
        )

        result = svc.execute_intent(intent)

        assert result.success is True
        assert tmux.join_pane_calls == [("%5", "@3", True, True)]

    def test_move_to_new_window(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)
        intent = _intent(
            "move_to_window",
            metadata=(
                ("pane_target", "%5"),
                ("session_target", "muxdeck"),
                ("new_window_name", "agent-ui"),
            ),
        )

        result = svc.execute_intent(intent)

        assert result.success is True
        assert tmux.break_pane_calls == [("%5", "agent-ui", "muxdeck:", True)]

    def test_kill_pane(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)
        intent = _intent("kill_pane", metadata=(("pane_target", "%5"),))

        result = svc.execute_intent(intent)

        assert result.success is True
        assert tmux.kill_pane_calls == ["%5"]


# ---------------------------------------------------------------------------
# ActionResult dataclass
# ---------------------------------------------------------------------------


class TestActionResult:
    def test_default_pane_id(self) -> None:
        result = ActionResult(success=True, message="ok")
        assert result.pane_id == ""

    def test_frozen(self) -> None:
        result = ActionResult(success=True, message="ok", pane_id="%1")
        with pytest.raises(AttributeError):
            result.success = False  # type: ignore[misc]


class TestWindowChoices:
    def test_window_choices_use_tmux_metadata(self) -> None:
        svc = TmuxActionService(FakeTmux())

        choices = svc.window_choices()

        assert choices == (
            WindowChoice(
                session_name="muxdeck",
                window_id="@2",
                window_name="editor",
                pane_count=1,
            ),
            WindowChoice(
                session_name="muxdeck",
                window_id="@3",
                window_name="review",
                pane_count=2,
            ),
        )

    def test_window_choices_can_exclude_current_window(self) -> None:
        svc = TmuxActionService(FakeTmux())

        choices = svc.window_choices(exclude_window_id="@2")

        assert choices == (
            WindowChoice(
                session_name="muxdeck",
                window_id="@3",
                window_name="review",
                pane_count=2,
            ),
        )


class TestLaunchModelHint:
    def test_uses_configured_copilot_model(self) -> None:
        svc = TmuxActionService(FakeTmux(), copilot=FakeCopilot())

        hint = svc.launch_model_hint()

        assert hint == ActionModelHint(
            configured_model="gpt-5.4",
            message=(
                "Configured model: gpt-5.4. "
                "Model availability depends on your Copilot account/provider. "
                "Enter a model manually or leave it blank to use Copilot's default."
            ),
        )

    def test_without_copilot_falls_back_to_manual_entry(self) -> None:
        svc = TmuxActionService(FakeTmux())

        hint = svc.launch_model_hint()

        assert hint.configured_model is None
        assert "Enter a model manually" in hint.message

    def test_without_configured_model_reports_honest_fallback(self) -> None:
        svc = TmuxActionService(FakeTmux(), copilot=FakeCopilot(current_model=None))

        hint = svc.launch_model_hint()

        assert hint.configured_model is None
        assert "No configured Copilot model detected." in hint.message


# ---------------------------------------------------------------------------
# start_agent
# ---------------------------------------------------------------------------


class TestOpenTerminal:
    def test_opens_attached_terminal_in_selected_worktree(self) -> None:
        tmux = FakeTmux()
        svc = TmuxActionService(tmux)
        from pathlib import Path

        result = svc.open_terminal(cwd=Path("/repo/worktree"), window_name="git-ui")

        assert result.success is True
        assert result.pane_id == "%10"
        assert tmux.new_window_calls == [(None, "git-ui", Path("/repo/worktree"), False)]
        assert "git-ui" in result.message


class TestStartAgent:
    def test_starts_copilot_in_new_window(self) -> None:
        tmux = FakeTmux()
        svc = TmuxActionService(tmux)
        from pathlib import Path

        result = svc.start_agent(cwd=Path("/repo/worktree"))

        assert result.success is True
        assert result.pane_id == "%10"
        assert len(tmux.send_keys_calls) == 1
        call = tmux.send_keys_calls[0]
        assert list(call.keys) == ["copilot"]
        assert call.literal is True
        assert call.append_enter is True

    def test_start_with_model(self) -> None:
        tmux = FakeTmux()
        svc = TmuxActionService(tmux)
        from pathlib import Path

        result = svc.start_agent(cwd=Path("/repo"), model="gpt-5.4")

        assert result.success is True
        call = tmux.send_keys_calls[0]
        assert "gpt-5.4" in next(iter(call.keys))

    def test_start_with_window_name(self) -> None:
        tmux = FakeTmux()
        svc = TmuxActionService(tmux)
        from pathlib import Path

        result = svc.start_agent(cwd=Path("/repo"), window_name="my-agent")

        assert result.success is True
        assert "my-agent" in result.message

    def test_start_with_copilot_uses_launch_parameters(self) -> None:
        tmux = FakeTmux()
        copilot = FakeCopilot()
        svc = TmuxActionService(tmux, copilot=copilot)
        from pathlib import Path

        result = svc.start_agent(
            cwd=Path("/repo"),
            model="gpt-5.4",
            window_name="my-agent",
            target_session="muxdeck",
            prompt="Continue work for task/ui",
        )

        assert result.success is True
        assert tmux.new_window_calls == [("muxdeck", "my-agent", Path("/repo"), True)]
        assert len(copilot.launch_calls) == 1
        launch = copilot.launch_calls[0]
        assert launch.pane_target == "%10"
        assert launch.model == "gpt-5.4"
        assert launch.extra_args == ("-i", "Continue work for task/ui")


class TestResumeWindowsSession:
    def test_local_session_uses_plain_copilot(self) -> None:
        tmux = FakeTmux()
        svc = TmuxActionService(tmux)

        result = svc.resume_session("abc123", origin="local")

        assert result.success is True
        assert len(tmux.send_keys_calls) == 1
        (sent,) = tmux.send_keys_calls[0].keys
        assert sent == "copilot --resume=abc123"

    def test_windows_session_wraps_in_pwsh_with_setlocation(self) -> None:
        tmux = FakeTmux()
        svc = TmuxActionService(tmux)

        result = svc.resume_session(
            "abc123",
            origin="windows",
            windows_cwd="C:\\Users\\alice\\proj",
        )

        assert result.success is True
        (sent,) = tmux.send_keys_calls[0].keys
        assert sent.startswith('pwsh.exe -NoExit -Command "')
        assert "Set-Location -LiteralPath 'C:\\Users\\alice\\proj'" in sent
        assert "copilot --resume=abc123" in sent

    def test_windows_session_without_cwd_skips_setlocation(self) -> None:
        tmux = FakeTmux()
        svc = TmuxActionService(tmux)

        result = svc.resume_session("abc123", origin="windows")

        assert result.success is True
        (sent,) = tmux.send_keys_calls[0].keys
        assert "Set-Location" not in sent
        assert "copilot --resume=abc123" in sent

    def test_windows_session_escapes_single_quote(self) -> None:
        tmux = FakeTmux()
        svc = TmuxActionService(tmux)

        svc.resume_session(
            "abc123",
            origin="windows",
            windows_cwd="C:\\Users\\o'brien\\proj",
        )

        (sent,) = tmux.send_keys_calls[0].keys
        # PowerShell single-quote escape doubles the quote.
        assert "o''brien" in sent


# Additional tests for better coverage


class TestKillPaneEdgeCases:
    def test_kill_pane_not_exists(self) -> None:
        tmux = FakeTmux(existing_panes=set())
        svc = TmuxActionService(tmux)

        result = svc.kill_pane("%99")

        assert result.success is False
        assert "does not exist" in result.message

    def test_kill_pane_command_error(self) -> None:
        from muxdeck.exceptions import TmuxCommandError

        class FakeTmuxWithError:
            def pane_exists(self, pane_id: str, /) -> bool:
                return True

            def kill_pane(self, pane_id: str, /) -> CommandResult:
                raise TmuxCommandError("tmux kill-pane", exit_code=1)

        svc = TmuxActionService(FakeTmuxWithError())  # type: ignore

        result = svc.kill_pane("%5")

        assert result.success is False
        assert "failed to kill pane" in result.message

    def test_kill_pane_value_error(self) -> None:
        class FakeTmuxWithError:
            def pane_exists(self, pane_id: str, /) -> bool:
                return True

            def kill_pane(self, pane_id: str, /) -> CommandResult:
                raise ValueError("bad pane")

        svc = TmuxActionService(FakeTmuxWithError())  # type: ignore

        result = svc.kill_pane("%5")

        assert result.success is False
        assert "failed to kill pane" in result.message


class TestRenameWindowEdgeCases:
    def test_rename_window_command_error(self) -> None:
        from muxdeck.exceptions import TmuxCommandError

        class FakeTmuxWithError:
            def rename_window(self, window_id: str, new_name: str) -> CommandResult:
                raise TmuxCommandError("tmux rename-window", exit_code=1)

        svc = TmuxActionService(FakeTmuxWithError())  # type: ignore

        result = svc.rename_window("@2", "new-name")

        assert result.success is False
        assert "failed to rename window" in result.message

    def test_rename_window_value_error(self) -> None:
        class FakeTmuxWithError:
            def rename_window(self, window_id: str, new_name: str) -> CommandResult:
                raise ValueError("bad window")

        svc = TmuxActionService(FakeTmuxWithError())  # type: ignore

        result = svc.rename_window("@2", "new-name")

        assert result.success is False
        assert "failed to rename window" in result.message


class TestMovePaneEdgeCases:
    def test_move_pane_not_exists(self) -> None:
        tmux = FakeTmux(existing_panes=set())
        svc = TmuxActionService(tmux)

        result = svc.move_pane_to_window("%99", new_window_name="new")

        assert result.success is False
        assert "does not exist" in result.message

    def test_move_pane_no_target_or_new_name(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)

        result = svc.move_pane_to_window("%5")

        assert result.success is False
        assert "must specify" in result.message

    def test_move_pane_to_new_window_with_session(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)

        result = svc.move_pane_to_window(
            "%5",
            new_window_name="new-window",
            target_session="muxdeck",
        )

        assert result.success is True

    def test_move_pane_break_pane_error(self) -> None:
        class FakeTmuxWithError:
            def pane_exists(self, pane_id: str, /) -> bool:
                return True

            def break_pane(
                self,
                source_pane: str,
                /,
                *,
                window_name: str | None = None,
                target_window: str | None = None,
                detached: bool = True,
            ) -> TmuxPaneMetadata:
                raise ValueError("break failed")

        svc = TmuxActionService(FakeTmuxWithError())  # type: ignore

        result = svc.move_pane_to_window("%5", new_window_name="new")

        assert result.success is False
        assert "failed to move pane" in result.message


class TestWindowChoicesError:
    def test_window_choices_handles_command_error(self) -> None:
        from muxdeck.exceptions import TmuxCommandError

        class FakeTmuxError:
            def list_windows(self) -> tuple[TmuxWindowInfo, ...]:
                raise TmuxCommandError("tmux list-windows", exit_code=1)

        svc = TmuxActionService(FakeTmuxError())  # type: ignore

        result = svc.window_choices()

        assert result == ()


class TestExecuteIntentEdgeCases:
    def test_execute_intent_unknown_kind(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)

        result = svc.execute_intent(_intent("unknown_kind"))

        assert result.success is False
        assert "unknown intent kind" in result.message

    def test_execute_intent_rename_window_no_window_id(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)

        target = AgentTargetView(
            agent_id="agent-1",
            name="test-agent",
            status=AgentStatus.RUNNING,
            pane_target="%5",
            tmux_session_name="muxdeck",
            tmux_window_id=None,
            worktree_path="/repo",
            repo_root="/repo",
            branch="main",
            latest_session_id="sess-1",
        )
        intent = AgentIntentView(
            kind="rename_window",  # type: ignore[arg-type]
            agent=target,
            label="Rename",
            metadata=(("window_name", "new-name"),),
        )

        result = svc.execute_intent(intent)

        assert result.success is False
        assert "window metadata unavailable" in result.message

    def test_execute_intent_restart_pane_not_found(self) -> None:
        tmux = FakeTmux(existing_panes=set())
        svc = TmuxActionService(tmux)

        result = svc.execute_intent(_intent("restart"))

        assert result.success is False
        assert "not found" in result.message

    def test_execute_intent_restart_with_task_no_model(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)

        result = svc.execute_intent(_intent("restart", metadata=(("task_title", "my-task"),)))

        assert result.success is True
        call = tmux.send_keys_calls[1]
        assert "copilot --resume" in next(iter(call.keys))

    def test_execute_intent_restart_with_model_no_task(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)

        result = svc.execute_intent(_intent("restart", metadata=(("model", "gpt-5.4"),)))

        assert result.success is True
        call = tmux.send_keys_calls[1]
        assert "gpt-5.4" in next(iter(call.keys))
