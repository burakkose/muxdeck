"""Tests for TmuxActionService."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from copilot_commander.controllers.agent_controller import (
    AgentIntentView,
    AgentTargetView,
)
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.services.action_service import (
    ActionResult,
    TmuxActionService,
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


@dataclass
class FakeTmux:
    """Minimal fake satisfying TmuxOperations protocol."""

    existing_panes: set[str] = field(default_factory=set)
    captured_text: str = ""
    select_pane_calls: list[str] = field(default_factory=list)
    send_keys_calls: list[SendKeysCall] = field(default_factory=list)
    capture_calls: list[tuple[str, int | None]] = field(default_factory=list)

    def pane_exists(self, target_pane: str, /) -> bool:
        return target_pane in self.existing_panes

    def select_pane(self, target_pane: str, /) -> None:
        self.select_pane_calls.append(target_pane)

    def send_keys(
        self,
        target_pane: str,
        keys: Sequence[str],
        /,
        *,
        literal: bool = False,
        append_enter: bool = False,
    ) -> None:
        self.send_keys_calls.append(SendKeysCall(target_pane, keys, literal, append_enter))

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TARGET = AgentTargetView(
    agent_id="agent-1",
    name="test-agent",
    status=AgentStatus.RUNNING,
    pane_target="%5",
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
        assert tmux.select_pane_calls == ["%5"]

    def test_pane_missing(self) -> None:
        tmux = FakeTmux(existing_panes=set())
        svc = TmuxActionService(tmux)

        result = svc.focus_pane("%99")

        assert result.success is False
        assert "%99" in result.message
        assert tmux.select_pane_calls == []


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
