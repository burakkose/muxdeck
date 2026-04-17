"""Application-layer service for executing tmux agent actions."""

from __future__ import annotations

import contextlib
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

    def select_window(self, target_window: str, /) -> CommandResult: ...

    def switch_client(self, target: str, /) -> CommandResult: ...

    def has_attached_client(self) -> bool: ...

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

    def focus_pane(
        self,
        pane_id: str,
        *,
        window_id: str | None = None,
        session_name: str | None = None,
    ) -> ActionResult:
        """Switch tmux focus to the agent's pane.

        ``select-pane`` alone only flips the active pane **within the pane's
        window**. If the agent's pane is in a different window (or session)
        than the currently attached tmux client, the user's view won't move
        unless we also point the window at it and hand the client over with
        ``switch-client``. We do all three, best-effort, and report back
        which hop succeeded so the dashboard can say something useful.
        """
        if not self._tmux.pane_exists(pane_id):
            return ActionResult(
                success=False,
                message=f"pane {pane_id} does not exist",
                pane_id=pane_id,
            )

        # Always flip the active pane within its window — cheap and needed
        # even when the client is already on that window.
        self._tmux.select_pane(pane_id)

        # Point the window to the pane; harmless if it's already current.
        if window_id:
            with contextlib.suppress(Exception):
                self._tmux.select_window(window_id)

        # Hand the attached client over. When commander runs on a socket
        # with no attached clients (e.g. the user is on a different tmux
        # server), this is a genuine no-op and we say so rather than
        # pretending we moved focus.
        moved_client = False
        if self._tmux.has_attached_client():
            switch_target = pane_id
            try:
                self._tmux.switch_client(switch_target)
                moved_client = True
            except Exception:
                # Some tmux versions reject pane targets for switch-client
                # when the window isn't current; fall back to the window
                # or session target if we have one.
                fallback = window_id or session_name
                if fallback is not None:
                    try:
                        self._tmux.switch_client(fallback)
                        moved_client = True
                    except Exception:
                        moved_client = False

        if moved_client:
            message = f"focused pane {pane_id}"
        else:
            message = (
                f"selected pane {pane_id} — no attached tmux client on this "
                "socket, run `tmux attach` or press `a` to jump over"
            )
        return ActionResult(
            success=True,
            message=message,
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
            window_target = meta.get("window_target") or intent.agent.tmux_window_id
            session_target = meta.get("session_target") or intent.agent.tmux_session_name
            return self.focus_pane(
                pane_target,
                window_id=window_target,
                session_name=session_target,
            )

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

    def execute_intents(self, intents: Sequence[AgentIntentView]) -> tuple[ActionResult, ...]:
        """Execute multiple intents in order for bulk operations."""
        return tuple(self.execute_intent(intent) for intent in intents)

    def resume_session(
        self,
        session_id: str,
        *,
        cwd: Path | None = None,
        window_name: str | None = None,
        origin: str = "local",
        windows_cwd: str | None = None,
        pwsh_binary: str = "pwsh.exe",
    ) -> ActionResult:
        """Resume a Copilot CLI session in a new tmux window.

        Creates a detached window and runs ``copilot --resume=<session_id>``
        so the session appears alongside existing panes.

        When ``origin`` is ``"windows"`` the session was created on the
        Windows side of WSL, so we wrap the resume in ``pwsh`` and use
        the original Windows-style ``cwd`` the CLI persisted. The tmux
        window still starts in the WSL directory (or ``None``) because
        PowerShell's own ``Set-Location`` handles the Windows path.
        """
        name = window_name or f"copilot-{session_id[:8]}"
        if origin == "windows":
            keys = self._build_windows_resume_keys(
                session_id=session_id,
                windows_cwd=windows_cwd,
                pwsh_binary=pwsh_binary,
            )
            start_directory: Path | None = None
        else:
            keys = [f"copilot --resume={session_id}"]
            start_directory = cwd
        try:
            meta = self._tmux.new_window(
                window_name=name,
                start_directory=start_directory,
                detached=True,
            )
            self._tmux.send_keys(meta.pane_id, keys, literal=True, append_enter=True)
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

    @staticmethod
    def _build_windows_resume_keys(
        *,
        session_id: str,
        windows_cwd: str | None,
        pwsh_binary: str,
    ) -> list[str]:
        """Assemble the key sequence that starts pwsh + resumes copilot.

        We run a single ``pwsh -NoExit -Command "..."`` invocation so the
        pane keeps the interactive PowerShell prompt after the CLI exits,
        which matches the user's existing workflow of running ``pwsh``
        then ``copilot`` manually.
        """
        script_parts: list[str] = []
        if windows_cwd:
            # PowerShell accepts forward or back slashes; quote the path
            # to survive spaces. Single quotes keep it literal so ``$``
            # or backticks inside the path aren't expanded.
            escaped = windows_cwd.replace("'", "''")
            script_parts.append(f"Set-Location -LiteralPath '{escaped}'")
        script_parts.append(f"copilot --resume={session_id}")
        script = "; ".join(script_parts)
        return [f'{pwsh_binary} -NoExit -Command "{script}"']

    def start_agent(
        self,
        *,
        cwd: Path,
        model: str | None = None,
        window_name: str | None = None,
    ) -> ActionResult:
        """Start a new Copilot CLI agent in a tmux window.

        Creates a detached window and runs ``copilot`` (optionally with
        ``--model``) so the agent appears alongside existing panes.
        """
        cmd = "copilot"
        if model:
            cmd += f" --model {model}"
        name = window_name or "copilot"
        try:
            meta = self._tmux.new_window(
                window_name=name,
                start_directory=cwd,
                detached=True,
            )
            self._tmux.send_keys(meta.pane_id, [cmd], literal=True, append_enter=True)
            return ActionResult(
                success=True,
                message=f"started agent in {meta.pane_id} ({name})",
                pane_id=meta.pane_id,
            )
        except Exception as exc:
            return ActionResult(
                success=False,
                message=f"failed to start agent: {exc}",
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
