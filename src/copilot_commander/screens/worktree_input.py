"""Modal screens for worktree creation and existing-worktree selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label


@dataclass(frozen=True, slots=True)
class CreateWorktreeResult:
    """Submitted create-worktree parameters."""

    repo_root: str
    task_title: str


@dataclass(frozen=True, slots=True)
class AttachWorktreeResult:
    """Submitted existing-worktree selection parameters."""

    path: str


class CreateWorktreeScreen(ModalScreen[CreateWorktreeResult | None]):
    """Modal for collecting the minimum inputs needed to create a worktree."""

    DEFAULT_CSS = """
    CreateWorktreeScreen {
        align: center middle;
    }

    #create-worktree-dialog {
        width: 76;
        height: auto;
        max-height: 16;
        background: #282828;
        border: thick #504945;
        border-title-color: #83a598;
        padding: 1 2;
    }

    #create-worktree-header {
        height: auto;
        margin-bottom: 1;
        color: #a89984;
    }

    #create-worktree-title {
        margin-bottom: 1;
    }

    #create-worktree-buttons {
        height: auto;
        align: right middle;
    }

    #create-worktree-buttons Button {
        margin-left: 1;
        min-width: 12;
    }

    #btn-create-worktree {
        background: #504945;
        color: #b8bb26;
        border: none;
    }

    #btn-create-worktree:hover {
        background: #665c54;
    }

    #btn-cancel-create-worktree {
        background: #3c3836;
        color: #928374;
        border: none;
    }

    #btn-cancel-create-worktree:hover {
        background: #504945;
    }
    """

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, repo_root: str, **kwargs: object) -> None:
        super().__init__(**kwargs)
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

    DEFAULT_CSS = """
    AttachWorktreeScreen {
        align: center middle;
    }

    #attach-worktree-dialog {
        width: 76;
        height: auto;
        max-height: 16;
        background: #282828;
        border: thick #504945;
        border-title-color: #83a598;
        padding: 1 2;
    }

    #attach-worktree-header {
        height: auto;
        margin-bottom: 1;
        color: #a89984;
    }

    #attach-worktree-path {
        margin-bottom: 1;
    }

    #attach-worktree-buttons {
        height: auto;
        align: right middle;
    }

    #attach-worktree-buttons Button {
        margin-left: 1;
        min-width: 12;
    }

    #btn-attach-worktree {
        background: #504945;
        color: #b8bb26;
        border: none;
    }

    #btn-attach-worktree:hover {
        background: #665c54;
    }

    #btn-cancel-attach-worktree {
        background: #3c3836;
        color: #928374;
        border: none;
    }

    #btn-cancel-attach-worktree:hover {
        background: #504945;
    }
    """

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
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


__all__ = [
    "AttachWorktreeResult",
    "AttachWorktreeScreen",
    "CreateWorktreeResult",
    "CreateWorktreeScreen",
]
