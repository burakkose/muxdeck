"""Confirmation modal for destructive actions."""

from __future__ import annotations

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label


class ConfirmScreen(ModalScreen[bool]):
    """Modal confirmation dialog. Dismisses with True (confirm) or False (cancel)."""

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }
    #confirm-dialog {
        width: 50;
        height: auto;
        max-height: 10;
        background: #282828;
        border: thick #fb4934;
        border-title-color: #fb4934;
        padding: 1 2;
    }
    #confirm-message {
        height: auto;
        margin-bottom: 1;
        color: #ebdbb2;
    }
    #confirm-buttons {
        height: auto;
        align: right middle;
    }
    #confirm-buttons Button {
        margin-left: 1;
        min-width: 10;
    }
    #btn-yes {
        background: #3c2020;
        color: #fb4934;
        border: none;
    }
    #btn-yes:hover {
        background: #fb4934;
        color: #282828;
    }
    #btn-no {
        background: #3c3836;
        color: #928374;
        border: none;
    }
    #btn-no:hover {
        background: #504945;
    }
    """

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("escape", "cancel", "Cancel"),
        ("y", "confirm", "Yes"),
        ("n", "cancel", "No"),
    ]

    def __init__(self, message: str, title: str = "Confirm", **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._message = message
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog") as dialog:
            dialog.border_title = self._title
            yield Label(self._message, id="confirm-message")
            with Horizontal(id="confirm-buttons"):
                yield Button("No", id="btn-no", variant="default")
                yield Button("Yes", id="btn-yes", variant="error")

    @on(Button.Pressed, "#btn-yes")
    def _on_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#btn-no")
    def _on_no(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
