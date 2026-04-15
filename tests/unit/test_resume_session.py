"""Tests for resume_session in TmuxActionService."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from copilot_commander.services.action_service import TmuxActionService

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass
class FakePaneMeta:
    pane_id: str = "%99"
    session_name: str | None = None
    session_id: str | None = None
    window_id: str | None = None
    window_index: int | None = None
    window_name: str | None = None
    window_active: bool | None = None
    pane_index: int | None = None


@dataclass
class FakeCommandResult:
    return_code: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeTmux:
    """Minimal fake implementing TmuxOperations for testing resume."""

    def __init__(self, *, fail_new_window: bool = False) -> None:
        self.sent_keys: list[tuple[str, list[str]]] = []
        self.new_window_calls: list[dict[str, object]] = []
        self._fail_new_window = fail_new_window
        self._pane_exists: set[str] = set()

    def select_pane(self, target_pane: str, /) -> FakeCommandResult:
        return FakeCommandResult()

    def send_keys(
        self,
        target_pane: str,
        keys: Sequence[str],
        /,
        *,
        literal: bool = False,
        append_enter: bool = False,
    ) -> FakeCommandResult:
        self.sent_keys.append((target_pane, list(keys)))
        return FakeCommandResult()

    def capture_pane(
        self,
        target_pane: str,
        /,
        *,
        start_line: str | int | None = None,
        end_line: str | int | None = None,
        join_wrapped_lines: bool = False,
    ) -> str:
        return ""

    def pane_exists(self, target_pane: str, /) -> bool:
        return target_pane in self._pane_exists

    def new_window(
        self,
        target_session: str | None = None,
        /,
        *,
        window_name: str | None = None,
        start_directory: Path | None = None,
        shell_command: Sequence[str] | None = None,
        detached: bool = False,
    ) -> FakePaneMeta:
        self.new_window_calls.append(
            {
                "target_session": target_session,
                "window_name": window_name,
                "start_directory": start_directory,
                "detached": detached,
            }
        )
        if self._fail_new_window:
            raise RuntimeError("tmux not available")
        return FakePaneMeta(pane_id="%99")


def test_resume_session_success() -> None:
    tmux = FakeTmux()
    svc = TmuxActionService(tmux=tmux)  # type: ignore[arg-type]
    result = svc.resume_session(
        "abc-12345678-def",
        cwd=Path("/home/user/project"),
        window_name="copilot-fix-bug",
    )
    assert result.success is True
    assert "abc-1234" in result.message
    assert result.pane_id == "%99"
    # Verify tmux calls
    assert len(tmux.new_window_calls) == 1
    call = tmux.new_window_calls[0]
    assert call["window_name"] == "copilot-fix-bug"
    assert call["start_directory"] == Path("/home/user/project")
    assert call["detached"] is True
    # Verify command sent
    assert len(tmux.sent_keys) == 1
    pane, keys = tmux.sent_keys[0]
    assert pane == "%99"
    assert "copilot --resume=abc-12345678-def" in keys[0]


def test_resume_session_default_window_name() -> None:
    tmux = FakeTmux()
    svc = TmuxActionService(tmux=tmux)  # type: ignore[arg-type]
    result = svc.resume_session("abcdefgh-1234-5678-90ab-cdef12345678")
    assert result.success is True
    call = tmux.new_window_calls[0]
    assert call["window_name"] == "copilot-abcdefgh"


def test_resume_session_tmux_failure() -> None:
    tmux = FakeTmux(fail_new_window=True)
    svc = TmuxActionService(tmux=tmux)  # type: ignore[arg-type]
    result = svc.resume_session("fail-id")
    assert result.success is False
    assert "failed to resume" in result.message


def test_resume_session_no_cwd() -> None:
    tmux = FakeTmux()
    svc = TmuxActionService(tmux=tmux)  # type: ignore[arg-type]
    result = svc.resume_session("no-cwd-session")
    assert result.success is True
    call = tmux.new_window_calls[0]
    assert call["start_directory"] is None
