"""Modal screens for worktree creation and existing-worktree selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Protocol

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from muxdeck import theme
from muxdeck.bindings import BindingSpec
from muxdeck.controllers import WorktreeActionView, WorktreeStartAgentIntent
from muxdeck.exceptions import DomainValidationError, PersistenceError
from muxdeck.services.action_service import ActionModelHint


class LaunchWorktreeController(Protocol):
    def create_worktree(
        self,
        cwd: str,
        /,
        *,
        task_title: str | None = None,
    ) -> WorktreeActionView: ...

    def attach_worktree(self, path: str, /) -> WorktreeActionView: ...

    def start_agent_intent(
        self,
        worktree_id: str,
        /,
        *,
        prompt: str | None = None,
        model: str | None = None,
        target_session_name: str | None = None,
        window_name: str | None = None,
    ) -> WorktreeStartAgentIntent: ...


@dataclass(frozen=True, slots=True)
class CreateWorktreeResult:
    """Submitted create-worktree parameters."""

    repo_root: str
    task_title: str


@dataclass(frozen=True, slots=True)
class AttachWorktreeResult:
    """Submitted existing-worktree selection parameters."""

    path: str


@dataclass(frozen=True, slots=True)
class LaunchAgentResult:
    """Submitted launch-agent parameters."""

    confirmed: bool
    selected_worktree_id: str
    target_session_name: str
    window_name: str
    prompt: str
    model: str | None


class CreateWorktreeScreen(ModalScreen[CreateWorktreeResult | None]):
    """Modal for collecting the minimum inputs needed to create a worktree."""

    DEFAULT_CSS = f"""
    CreateWorktreeScreen {{
        align: center middle;
    }}

    #create-worktree-dialog {{
        width: 76;
        height: auto;
        max-height: 16;
        background: {theme.BG1};
        border: thick {theme.BORDER};
        border-title-color: {theme.BORDER_FOCUS};
        padding: 1 2;
    }}

    #create-worktree-header {{
        height: auto;
        margin-bottom: 1;
        color: {theme.FG2};
    }}

    #create-worktree-title {{
        margin-bottom: 1;
    }}

    #create-worktree-buttons {{
        height: auto;
        align: right middle;
    }}

    #create-worktree-buttons Button {{
        margin-left: 1;
        min-width: 12;
    }}

    #btn-create-worktree {{
        background: {theme.BADGE_BG};
        color: {theme.BADGE_FG};
        border: none;
    }}

    #btn-create-worktree:hover {{
        background: {theme.YELLOW};
    }}

    #btn-cancel-create-worktree {{
        background: {theme.BG3};
        color: {theme.FG3};
        border: none;
    }}

    #btn-cancel-create-worktree:hover {{
        background: {theme.BG4};
    }}
    """

    BINDINGS: ClassVar[list[BindingSpec]] = [
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, repo_root: str) -> None:
        super().__init__()
        self._repo_root = repo_root

    def compose(self) -> ComposeResult:
        with Vertical(id="create-worktree-dialog") as dialog:
            dialog.border_title = "Create Worktree"
            yield Label(f"Repo: {self._repo_root}", id="create-worktree-header")
            yield Input(
                placeholder="Task title…",
                id="create-worktree-title",
            )
            with Horizontal(id="create-worktree-buttons"):
                yield Button("Cancel", id="btn-cancel-create-worktree", variant="default")
                yield Button("Create", id="btn-create-worktree", variant="success")

    def on_mount(self) -> None:
        self.query_one("#create-worktree-title", Input).focus()

    @on(Button.Pressed, "#btn-create-worktree")
    def _on_create(self) -> None:
        task_title = self.query_one("#create-worktree-title", Input).value.strip()
        if task_title:
            self.dismiss(CreateWorktreeResult(repo_root=self._repo_root, task_title=task_title))
        else:
            self.query_one("#create-worktree-title", Input).focus()

    @on(Button.Pressed, "#btn-cancel-create-worktree")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#create-worktree-title")
    def _on_input_submitted(self) -> None:
        self._on_create()

    def action_cancel(self) -> None:
        """Handle the escape binding."""
        self.dismiss(None)


class AttachWorktreeScreen(ModalScreen[AttachWorktreeResult | None]):
    """Modal for selecting an existing worktree path to attach."""

    DEFAULT_CSS = f"""
    AttachWorktreeScreen {{
        align: center middle;
    }}

    #attach-worktree-dialog {{
        width: 76;
        height: auto;
        max-height: 16;
        background: {theme.BG1};
        border: thick {theme.BORDER};
        border-title-color: {theme.BORDER_FOCUS};
        padding: 1 2;
    }}

    #attach-worktree-header {{
        height: auto;
        margin-bottom: 1;
        color: {theme.FG2};
    }}

    #attach-worktree-path {{
        margin-bottom: 1;
    }}

    #attach-worktree-buttons {{
        height: auto;
        align: right middle;
    }}

    #attach-worktree-buttons Button {{
        margin-left: 1;
        min-width: 12;
    }}

    #btn-attach-worktree {{
        background: {theme.BADGE_BG};
        color: {theme.BADGE_FG};
        border: none;
    }}

    #btn-attach-worktree:hover {{
        background: {theme.YELLOW};
    }}

    #btn-cancel-attach-worktree {{
        background: {theme.BG3};
        color: {theme.FG3};
        border: none;
    }}

    #btn-cancel-attach-worktree:hover {{
        background: {theme.BG4};
    }}
    """

    BINDINGS: ClassVar[list[BindingSpec]] = [
        ("escape", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="attach-worktree-dialog") as dialog:
            dialog.border_title = "Select Existing Worktree"
            yield Label(
                "Enter the path to an existing git worktree.",
                id="attach-worktree-header",
            )
            yield Input(
                placeholder="/path/to/worktree",
                id="attach-worktree-path",
            )
            with Horizontal(id="attach-worktree-buttons"):
                yield Button("Cancel", id="btn-cancel-attach-worktree", variant="default")
                yield Button("Select", id="btn-attach-worktree", variant="success")

    def on_mount(self) -> None:
        self.query_one("#attach-worktree-path", Input).focus()

    @on(Button.Pressed, "#btn-attach-worktree")
    def _on_attach(self) -> None:
        path = self.query_one("#attach-worktree-path", Input).value.strip()
        if path:
            self.dismiss(AttachWorktreeResult(path=path))
        else:
            self.query_one("#attach-worktree-path", Input).focus()

    @on(Button.Pressed, "#btn-cancel-attach-worktree")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#attach-worktree-path")
    def _on_input_submitted(self) -> None:
        self._on_attach()

    def action_cancel(self) -> None:
        """Handle the escape binding."""
        self.dismiss(None)


class LaunchAgentScreen(ModalScreen[LaunchAgentResult]):
    """Modal for selecting launch settings for a Copilot agent."""

    DEFAULT_CSS = f"""
    LaunchAgentScreen {{
        align: center middle;
    }}

    #launch-agent-dialog {{
        width: 96;
        height: auto;
        max-height: 28;
        background: {theme.BG1};
        border: thick {theme.BORDER};
        border-title-color: {theme.BORDER_FOCUS};
        padding: 1 2;
    }}

    #launch-agent-summary,
    #launch-agent-model-help,
    #launch-agent-status {{
        height: auto;
        margin-bottom: 1;
        color: {theme.FG2};
    }}

    #launch-agent-buttons {{
        height: auto;
        align: right middle;
    }}

    #launch-agent-buttons Button {{
        margin-left: 1;
        min-width: 12;
    }}

    #btn-launch-agent {{
        background: {theme.BADGE_BG};
        color: {theme.BADGE_FG};
        border: none;
    }}

    #btn-launch-agent:hover {{
        background: {theme.YELLOW};
    }}

    #btn-cancel-launch-agent,
    #btn-launch-create-worktree,
    #btn-launch-attach-worktree {{
        background: {theme.BG3};
        color: {theme.FG3};
        border: none;
    }}

    #btn-cancel-launch-agent:hover,
    #btn-launch-create-worktree:hover,
    #btn-launch-attach-worktree:hover {{
        background: {theme.BG4};
    }}
    """

    BINDINGS: ClassVar[list[BindingSpec]] = [
        ("escape", "cancel", "Cancel"),
        ("c", "create_worktree", "Create worktree"),
        ("a", "attach_worktree", "Select existing"),
    ]

    def __init__(
        self,
        worktrees: LaunchWorktreeController,
        *,
        intent: WorktreeStartAgentIntent,
        model_hint: ActionModelHint,
    ) -> None:
        super().__init__()
        self._worktrees = worktrees
        self._intent = intent
        self._model_hint = model_hint

    def compose(self) -> ComposeResult:
        with Vertical(id="launch-agent-dialog") as dialog:
            dialog.border_title = "Launch Agent"
            yield Static(id="launch-agent-summary")
            yield Static(id="launch-agent-model-help")
            yield Input(
                value=self._intent.suggested_window_name,
                placeholder="Agent / window name…",
                id="launch-agent-name",
            )
            yield Input(
                value=self._intent.suggested_session_name,
                placeholder="Tmux session…",
                id="launch-agent-session",
            )
            yield Input(
                value=self._intent.model or self._model_hint.configured_model or "",
                placeholder="Model override (blank = Copilot default)",
                id="launch-agent-model",
            )
            yield Input(
                value=self._intent.prompt,
                placeholder="Initial prompt…",
                id="launch-agent-prompt",
            )
            yield Static(id="launch-agent-status")
            with Horizontal(id="launch-agent-buttons"):
                yield Button(
                    "Cancel",
                    id="btn-cancel-launch-agent",
                    variant="default",
                )
                yield Button(
                    "Select Existing",
                    id="btn-launch-attach-worktree",
                    variant="default",
                )
                yield Button(
                    "Create Worktree",
                    id="btn-launch-create-worktree",
                    variant="default",
                )
                yield Button("Launch", id="btn-launch-agent", variant="success")

    def on_mount(self) -> None:
        self._refresh_summary()
        self.query_one("#launch-agent-name", Input).focus()

    def action_create_worktree(self) -> None:
        self.app.push_screen(
            CreateWorktreeScreen(repo_root=self._intent.repo_root),
            callback=self._on_create_worktree_result,
        )

    def action_attach_worktree(self) -> None:
        self.app.push_screen(
            AttachWorktreeScreen(),
            callback=self._on_attach_worktree_result,
        )

    def action_cancel(self) -> None:
        self.dismiss(self._collect_result(confirmed=False))

    @on(Button.Pressed, "#btn-launch-create-worktree")
    def _on_create_button(self) -> None:
        self.action_create_worktree()

    @on(Button.Pressed, "#btn-launch-attach-worktree")
    def _on_attach_button(self) -> None:
        self.action_attach_worktree()

    @on(Button.Pressed, "#btn-launch-agent")
    def _on_launch_button(self) -> None:
        self._submit()

    @on(Button.Pressed, "#btn-cancel-launch-agent")
    def _on_cancel_button(self) -> None:
        self.action_cancel()

    @on(Input.Submitted)
    def _on_input_submitted(self) -> None:
        self._submit()

    def _submit(self) -> None:
        name_input = self.query_one("#launch-agent-name", Input)
        if not name_input.value.strip():
            name_input.focus()
            self._set_status("agent/window name is required")
            return
        self.dismiss(self._collect_result(confirmed=True))

    def _on_create_worktree_result(self, result: CreateWorktreeResult | None) -> None:
        if result is None:
            self._set_status("create cancelled")
            return
        try:
            action_view = self._worktrees.create_worktree(
                result.repo_root,
                task_title=result.task_title,
            )
        except (DomainValidationError, PersistenceError) as exc:
            self._set_status(f"✗ create failed: {exc}")
            return
        if action_view.worktree is None:
            self._set_status("✗ create failed: no worktree returned")
            return
        self._refresh_intent(action_view.worktree.summary.worktree_id)
        self._set_status(f"✓ {action_view.message}")

    def _on_attach_worktree_result(self, result: AttachWorktreeResult | None) -> None:
        if result is None:
            self._set_status("select existing cancelled")
            return
        try:
            action_view = self._worktrees.attach_worktree(result.path)
        except (DomainValidationError, PersistenceError) as exc:
            self._set_status(f"✗ attach failed: {exc}")
            return
        if action_view.worktree is None:
            self._set_status("✗ attach failed: no worktree returned")
            return
        self._refresh_intent(action_view.worktree.summary.worktree_id)
        self._set_status(f"✓ {action_view.message}")

    def _refresh_intent(self, worktree_id: str) -> None:
        model = self._normalized_model()
        prompt = self.query_one("#launch-agent-prompt", Input).value.strip() or None
        session_name = self.query_one("#launch-agent-session", Input).value.strip() or None
        window_name = self.query_one("#launch-agent-name", Input).value.strip() or None
        self._intent = self._worktrees.start_agent_intent(
            worktree_id,
            prompt=prompt,
            model=model,
            target_session_name=session_name,
            window_name=window_name,
        )
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        self.query_one("#launch-agent-summary", Static).update(
            "\n".join(
                (
                    f"Repo: {self._intent.repo_root}",
                    f"Folder: {self._intent.worktree_path}",
                    f"Branch: {self._intent.branch}",
                    "Press c to create a worktree or a to select an existing one.",
                )
            )
        )
        self.query_one("#launch-agent-model-help", Static).update(self._model_hint.message)

    def _set_status(self, message: str) -> None:
        self.query_one("#launch-agent-status", Static).update(message)

    def _normalized_model(self) -> str | None:
        value = self.query_one("#launch-agent-model", Input).value.strip()
        return value or None

    def _collect_result(self, *, confirmed: bool) -> LaunchAgentResult:
        session_name = self.query_one("#launch-agent-session", Input).value.strip()
        window_name = self.query_one("#launch-agent-name", Input).value.strip()
        prompt = self.query_one("#launch-agent-prompt", Input).value.strip()
        return LaunchAgentResult(
            confirmed=confirmed,
            selected_worktree_id=self._intent.worktree_id,
            target_session_name=session_name or self._intent.suggested_session_name,
            window_name=window_name or self._intent.suggested_window_name,
            prompt=prompt,
            model=self._normalized_model(),
        )


__all__ = [
    "AttachWorktreeResult",
    "AttachWorktreeScreen",
    "CreateWorktreeResult",
    "CreateWorktreeScreen",
    "LaunchAgentResult",
    "LaunchAgentScreen",
]
