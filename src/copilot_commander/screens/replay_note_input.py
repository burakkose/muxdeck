"""Modal screen for capturing a replay annotation note body."""

from __future__ import annotations

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

from copilot_commander import theme
from copilot_commander.bindings import BindingSpec


class ReplayNoteInputScreen(ModalScreen[str | None]):
    """Modal that asks the operator for a note body for the selected entry."""

    DEFAULT_CSS = f"""
    ReplayNoteInputScreen {{
        align: center middle;
    }}

    #note-dialog {{
        width: 70;
        height: auto;
        background: {theme.BG1};
        border: thick {theme.BORDER};
        border-title-color: {theme.BORDER_FOCUS};
        padding: 1 2;
    }}

    #note-header {{
        height: auto;
        margin-bottom: 1;
        color: {theme.FG2};
    }}

    #note-input {{
        margin-bottom: 1;
    }}

    #note-buttons {{
        height: auto;
        align: right middle;
    }}

    #note-buttons Button {{
        margin-left: 1;
        min-width: 10;
    }}
    """

    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, ordinal: int, *, initial: str = "") -> None:
        super().__init__()
        self._ordinal = ordinal
        self._initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="note-dialog") as dialog:
            dialog.border_title = f"Note for entry #{self._ordinal}"
            yield Label("Enter to save · Esc to cancel", id="note-header")
            yield Input(value=self._initial, placeholder="Write a note…", id="note-input")
            with Horizontal(id="note-buttons"):
                yield Button("Cancel", id="note-cancel", variant="default")
                yield Button("Save", id="note-save", variant="success")

    def on_mount(self) -> None:
        self.query_one("#note-input", Input).focus()

    @on(Input.Submitted, "#note-input")
    def _on_input_submitted(self) -> None:
        self._save()

    @on(Button.Pressed, "#note-save")
    def _on_save_pressed(self) -> None:
        self._save()

    @on(Button.Pressed, "#note-cancel")
    def _on_cancel_pressed(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _save(self) -> None:
        text = self.query_one("#note-input", Input).value.strip()
        if not text:
            self.dismiss(None)
            return
        self.dismiss(text)


__all__ = ["ReplayNoteInputScreen"]
