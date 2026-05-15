"""Application-layer service for executing tmux agent actions."""

from __future__ import annotations

import contextlib
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from muxdeck.adapters.copilot_adapter import CopilotLaunchParameters
from muxdeck.exceptions import CommandError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from muxdeck.adapters.tmux_adapter import TmuxPaneMetadata, TmuxWindowInfo
    from muxdeck.controllers.agent_controller import AgentIntentView
    from muxdeck.domain.value_objects import CommandResult


class TmuxOperations(Protocol):
    """Minimal protocol for tmux operations needed by the action service."""

    def list_windows(self) -> Sequence[TmuxWindowInfo]: ...

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

    def break_pane(
        self,
        source_pane: str,
        /,
        *,
        window_name: str | None = None,
        target_window: str | None = None,
        detached: bool = True,
    ) -> TmuxPaneMetadata: ...

    def join_pane(
        self,
        source_pane: str,
        target_pane: str,
        /,
        *,
        detached: bool = True,
        vertical: bool = True,
    ) -> TmuxPaneMetadata: ...

    def rename_window(self, target_window: str, new_name: str, /) -> CommandResult: ...

    def kill_pane(self, target_pane: str, /) -> CommandResult: ...

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


class CopilotOperations(Protocol):
    def launch_in_pane(self, parameters: CopilotLaunchParameters, /) -> object: ...

    def configured_model(self) -> str | None: ...


_MODEL_HINT_MESSAGE = (
    "Model availability depends on your Copilot account/provider. "
    "Enter a model manually or leave it blank to use Copilot's default."
)


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Result of executing an agent action."""

    success: bool
    message: str
    pane_id: str = ""
    # Full tmux metadata for actions that spawn a new pane. Callers
    # that need to seed downstream state (e.g. SessionsScreen pinning
    # attribution for a resumed Copilot session) read window/session
    # identifiers from here without re-querying tmux.
    pane_meta: TmuxPaneMetadata | None = None


@dataclass(frozen=True, slots=True)
class ActionModelHint:
    configured_model: str | None = None
    message: str = _MODEL_HINT_MESSAGE


@dataclass(frozen=True, slots=True)
class WindowChoice:
    session_name: str
    window_id: str
    window_name: str | None
    pane_count: int

    @property
    def label(self) -> str:
        title = self.window_name or self.window_id
        pane_label = "pane" if self.pane_count == 1 else "panes"
        return f"{self.session_name}:{title} ({self.pane_count} {pane_label})"


class TmuxActionService:
    """Executes agent actions via tmux."""

    def __init__(
        self,
        tmux: TmuxOperations,
        *,
        copilot: CopilotOperations | None = None,
    ) -> None:
        self._tmux = tmux
        self._copilot = copilot

    def launch_model_hint(self) -> ActionModelHint:
        if self._copilot is None:
            return ActionModelHint()
        configured_model = self._copilot.configured_model()
        if configured_model is None:
            return ActionModelHint(
                message=f"No configured Copilot model detected. {_MODEL_HINT_MESSAGE}"
            )
        return ActionModelHint(
            configured_model=configured_model,
            message=f"Configured model: {configured_model}. {_MODEL_HINT_MESSAGE}",
        )

    def window_choices(self, *, exclude_window_id: str | None = None) -> tuple[WindowChoice, ...]:
        try:
            windows = self._tmux.list_windows()
        except CommandError:
            return ()
        return tuple(
            WindowChoice(
                session_name=window.session_name,
                window_id=window.window_id,
                window_name=window.window_name,
                pane_count=window.pane_count,
            )
            for window in windows
            if exclude_window_id is None or window.window_id != exclude_window_id
        )

    def focus_pane(
        self,
        pane_id: str,
        *,
        window_id: str | None = None,
        session_name: str | None = None,
    ) -> ActionResult:
        """Switch tmux focus to the agent's pane.

        ``select-pane`` alone only flips the active pane **within the pane's
        window**. If the agent lives in a different session/window than the
        currently attached tmux client, we first switch the client to the
        session (when known), then select the window, and only then activate
        the pane. Falling back to ``switch-client`` with a pane target can
        leave tmux on the wrong window even though the pane id resolves.
        """
        if not self._tmux.pane_exists(pane_id):
            return ActionResult(
                success=False,
                message=f"pane {pane_id} does not exist",
                pane_id=pane_id,
            )

        has_client = self._tmux.has_attached_client()
        if has_client and session_name:
            with contextlib.suppress(CommandError, ValueError):
                self._tmux.switch_client(session_name)

        if window_id:
            with contextlib.suppress(CommandError, ValueError):
                self._tmux.select_window(window_id)

        # Always flip the active pane within its window after the client is on
        # the right session/window. This is also enough for same-window focus.
        self._tmux.select_pane(pane_id)

        if has_client and window_id is None and session_name is None:
            with contextlib.suppress(CommandError, ValueError):
                self._tmux.switch_client(pane_id)

        if has_client:
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

    def kill_pane(self, pane_id: str) -> ActionResult:
        if not self._tmux.pane_exists(pane_id):
            return ActionResult(
                success=False,
                message=f"pane {pane_id} does not exist",
                pane_id=pane_id,
            )
        try:
            self._tmux.kill_pane(pane_id)
        except (CommandError, ValueError) as exc:
            return ActionResult(
                success=False,
                message=f"failed to kill pane {pane_id}: {exc}",
                pane_id=pane_id,
            )
        return ActionResult(
            success=True,
            message=f"killed pane {pane_id}",
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

    def rename_window(self, window_id: str, new_name: str) -> ActionResult:
        try:
            self._tmux.rename_window(window_id, new_name)
        except (CommandError, ValueError) as exc:
            return ActionResult(success=False, message=f"failed to rename window: {exc}")
        return ActionResult(success=True, message=f"renamed window {window_id} to {new_name}")

    def move_pane_to_window(
        self,
        pane_id: str,
        *,
        target_window: str | None = None,
        new_window_name: str | None = None,
        target_session: str | None = None,
    ) -> ActionResult:
        if not self._tmux.pane_exists(pane_id):
            return ActionResult(
                success=False,
                message=f"pane {pane_id} does not exist",
                pane_id=pane_id,
            )
        if target_window is None and new_window_name is None:
            return ActionResult(
                success=False,
                message="move target must specify an existing window or a new window name",
                pane_id=pane_id,
            )
        try:
            if target_window is not None:
                metadata = self._tmux.join_pane(pane_id, target_window, detached=True)
                destination = metadata.window_name or metadata.window_id or target_window
                return ActionResult(
                    success=True,
                    message=f"moved pane {pane_id} to {destination}",
                    pane_id=metadata.pane_id,
                )
            session_target = None
            if target_session is not None:
                session_target = (
                    target_session if target_session.endswith(":") else f"{target_session}:"
                )
            metadata = self._tmux.break_pane(
                pane_id,
                window_name=new_window_name,
                target_window=session_target,
                detached=True,
            )
        except (CommandError, ValueError) as exc:
            return ActionResult(
                success=False,
                message=f"failed to move pane {pane_id}: {exc}",
                pane_id=pane_id,
            )
        destination = metadata.window_name or new_window_name or metadata.window_id or "new window"
        return ActionResult(
            success=True,
            message=f"moved pane {pane_id} to {destination}",
            pane_id=metadata.pane_id,
        )

    def open_terminal(
        self,
        *,
        cwd: Path,
        window_name: str | None = None,
        target_session: str | None = None,
    ) -> ActionResult:
        normalized_cwd = cwd.expanduser().resolve(strict=False)
        has_client = self._tmux.has_attached_client()
        try:
            metadata = self._tmux.new_window(
                target_session,
                window_name=window_name,
                start_directory=normalized_cwd,
                detached=not has_client,
            )
        except (CommandError, RuntimeError, ValueError) as exc:
            return ActionResult(success=False, message=f"failed to open terminal: {exc}")
        destination = metadata.window_name or window_name or metadata.window_id or "terminal"
        if has_client:
            message = f"opened {destination} at {normalized_cwd}"
        else:
            message = f"opened detached {destination} at {normalized_cwd}"
        return ActionResult(success=True, message=message, pane_id=metadata.pane_id)

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

        if kind == "kill_pane":
            pane_target = meta.get("pane_target", intent.agent.pane_target)
            return self.kill_pane(pane_target)

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

        if kind == "rename_window":
            window_target = meta.get("window_target") or intent.agent.tmux_window_id
            new_name = meta.get("window_name", "")
            if window_target is None:
                return ActionResult(
                    success=False,
                    message=f"window metadata unavailable for {intent.agent.name}",
                    pane_id=intent.agent.pane_target,
                )
            return self.rename_window(window_target, new_name)

        if kind == "move_to_window":
            pane_target = meta.get("pane_target", intent.agent.pane_target)
            return self.move_pane_to_window(
                pane_target,
                target_window=meta.get("window_target"),
                new_window_name=meta.get("new_window_name"),
                target_session=meta.get("session_target") or intent.agent.tmux_session_name,
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
            model = meta.get("model")
            if model and not task:
                cmd = f"{cmd} --model {model}"
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
                pane_meta=meta,
            )
        except (CommandError, RuntimeError, ValueError) as exc:
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
        target_session: str | None = None,
        prompt: str | None = None,
    ) -> ActionResult:
        """Start a new Copilot CLI agent in a tmux window.

        Creates a detached window and runs ``copilot`` (optionally with
        ``--model``) so the agent appears alongside existing panes.
        """
        normalized_model = model.strip() if model and model.strip() else None
        normalized_prompt = prompt.strip() if prompt and prompt.strip() else None
        name = (window_name or "copilot").strip() or "copilot"
        try:
            meta = self._tmux.new_window(
                target_session,
                window_name=name,
                start_directory=cwd,
                detached=True,
            )
            if self._copilot is not None:
                extra_args = ("-i", normalized_prompt) if normalized_prompt is not None else ()
                self._copilot.launch_in_pane(
                    CopilotLaunchParameters(
                        pane_target=meta.pane_id,
                        cwd=cwd,
                        model=normalized_model,
                        command_prefix=("copilot",),
                        extra_args=extra_args,
                    )
                )
            else:
                command = ["copilot"]
                if normalized_model is not None:
                    command.extend(("--model", normalized_model))
                if normalized_prompt is not None:
                    command.extend(("-i", normalized_prompt))
                self._tmux.send_keys(
                    meta.pane_id,
                    [shlex.join(command)],
                    literal=True,
                    append_enter=True,
                )
            return ActionResult(
                success=True,
                message=f"started agent in {meta.pane_id} ({name})",
                pane_id=meta.pane_id,
            )
        except (CommandError, ValueError) as exc:
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
    "ActionModelHint",
    "ActionResult",
    "TmuxActionService",
    "TmuxOperations",
    "WindowChoice",
]
