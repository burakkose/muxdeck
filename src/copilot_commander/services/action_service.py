"""Application-layer service for executing tmux agent actions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from copilot_commander.adapters.tmux_adapter import TmuxPaneMetadata
    from copilot_commander.controllers.agent_controller import AgentIntentView
    from copilot_commander.domain.value_objects import CommandResult


class TmuxOperations(Protocol):
    """Minimal protocol for tmux operations needed by the action service."""

    def select_pane(self, target_pane: str, /) -> CommandResult: ...

    def send_keys(
        self,
        target_pane: str,
        keys: Sequence[str],
        /,
        *,
        literal: bool = False,
        append_enter: bool = False,
    ) -> CommandResult: ...

    def capture_pane(
        self,
        target_pane: str,
        /,
        *,
        start_line: str | int | None = None,
        end_line: str | int | None = None,
        join_wrapped_lines: bool = False,
    ) -> str: ...

    def pane_exists(self, target_pane: str, /) -> bool: ...

    def new_window(
        self,
        target_session: str | None = None,
        /,
        *,
        window_name: str | None = None,
        start_directory: Path | None = None,
        shell_command: Sequence[str] | None = None,
        detached: bool = False,
    ) -> TmuxPaneMetadata: ...


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Result of executing an agent action."""

    success: bool
    message: str
    pane_id: str = ""


class TmuxActionService:
    """Executes agent actions via tmux."""

    def __init__(self, tmux: TmuxOperations) -> None:
        self._tmux = tmux

    def focus_pane(self, pane_id: str) -> ActionResult:
        """Switch tmux focus to the agent's pane."""
        if not self._tmux.pane_exists(pane_id):
            return ActionResult(
                success=False,
                message=f"pane {pane_id} does not exist",
                pane_id=pane_id,
            )
        self._tmux.select_pane(pane_id)
        return ActionResult(
            success=True,
            message=f"focused pane {pane_id}",
            pane_id=pane_id,
        )

    def interrupt_pane(self, pane_id: str) -> ActionResult:
        """Send Ctrl-C to the agent's pane."""
        if not self._tmux.pane_exists(pane_id):
            return ActionResult(
                success=False,
                message=f"pane {pane_id} does not exist",
                pane_id=pane_id,
            )
        self._tmux.send_keys(pane_id, ["C-c"])
        return ActionResult(
            success=True,
            message=f"sent interrupt to pane {pane_id}",
            pane_id=pane_id,
        )

    def send_message(self, pane_id: str, text: str) -> ActionResult:
        """Send text message to agent's pane (with Enter)."""
        if not text.strip():
            return ActionResult(
                success=False,
                message="message text must not be empty",
                pane_id=pane_id,
            )
        if not self._tmux.pane_exists(pane_id):
            return ActionResult(
                success=False,
                message=f"pane {pane_id} does not exist",
                pane_id=pane_id,
            )
        self._tmux.send_keys(pane_id, [text], literal=True, append_enter=True)
        return ActionResult(
            success=True,
            message=f"sent message to pane {pane_id}",
            pane_id=pane_id,
        )

    def capture_output(self, pane_id: str, *, lines: int = 50) -> str:
        """Capture the last N lines of pane output."""
        return self._tmux.capture_pane(pane_id, start_line=-lines, join_wrapped_lines=True)

    def execute_intent(self, intent: AgentIntentView) -> ActionResult:
        """Execute an agent intent by dispatching to the appropriate method."""
        meta = dict(intent.metadata)
        kind = intent.kind

        if kind == "open_pane":
            pane_target = meta.get("pane_target", intent.agent.pane_target)
            return self.focus_pane(pane_target)

        if kind == "interrupt":
            pane_target = meta.get("pane_target", intent.agent.pane_target)
            return self.interrupt_pane(pane_target)

        if kind == "send_input":
            pane_target = meta.get("pane_target", intent.agent.pane_target)
            prompt = intent.prompt or ""
            return self.send_message(pane_target, prompt)

        if kind == "open_worktree":
            path = meta.get("path", "")
            return ActionResult(
                success=True,
                message=f"worktree path: {path}",
                pane_id=intent.agent.pane_target,
            )

        if kind == "restart":
            pane = meta.get("pane_target", intent.agent.pane_target)
            if not self._tmux.pane_exists(pane):
                return ActionResult(
                    success=False,
                    message=f"pane {pane} not found",
                    pane_id=pane,
                )
            self._tmux.send_keys(pane, ["C-c"])
            task = meta.get("task_title", "")
            cmd = "copilot --resume" if task else "copilot"
            self._tmux.send_keys(
                pane,
                [cmd],
                literal=True,
                append_enter=True,
            )
            return ActionResult(
                success=True,
                message=f"restarted agent in {pane}",
                pane_id=pane,
            )

        return ActionResult(
            success=False,
            message=f"unknown intent kind: {kind}",
            pane_id=intent.agent.pane_target,
        )

    def resume_session(
        self,
        session_id: str,
        *,
        cwd: Path | None = None,
        window_name: str | None = None,
    ) -> ActionResult:
        """Resume a Copilot CLI session in a new tmux window.

        Creates a detached window and runs ``copilot --resume=<session_id>``
        so the session appears alongside existing panes.
        """
        cmd = f"copilot --resume={session_id}"
        name = window_name or f"copilot-{session_id[:8]}"
        try:
            meta = self._tmux.new_window(
                window_name=name,
                start_directory=cwd,
                detached=True,
            )
            self._tmux.send_keys(meta.pane_id, [cmd], literal=True, append_enter=True)
            return ActionResult(
                success=True,
                message=f"resumed session {session_id[:8]}… in {meta.pane_id}",
                pane_id=meta.pane_id,
            )
        except Exception as exc:
            return ActionResult(
                success=False,
                message=f"failed to resume: {exc}",
            )

    def stop_all_agents(
        self,
        pane_ids: Sequence[str],
    ) -> list[ActionResult]:
        """Send Ctrl-C to multiple agent panes. Used for emergency stop."""
        results: list[ActionResult] = []
        for pane_id in pane_ids:
            results.append(self.interrupt_pane(pane_id))
        return results


__all__ = [
    "ActionResult",
    "TmuxActionService",
    "TmuxOperations",
]
