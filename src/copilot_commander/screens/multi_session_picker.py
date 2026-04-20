"""Modal screen for selecting multiple replay sessions to merge."""

from __future__ import annotations

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

from copilot_commander import theme
from copilot_commander.bindings import BindingSpec


class MultiSessionPickerScreen(ModalScreen[tuple[str, ...] | None]):
    """Prompt the operator for a comma-separated list of session ids.

    Dismisses with a tuple of trimmed session ids when the operator
    submits, or ``None`` on cancel.
    """

    DEFAULT_CSS = f"""
    MultiSessionPickerScreen {{
        align: center middle;
    }}

    #multi-dialog {{
        width: 80;
        height: auto;
        max-height: 16;
        background: {theme.BG1};
        border: thick {theme.BORDER};
        border-title-color: {theme.BORDER_FOCUS};
        padding: 1 2;
    }}

    #multi-header {{
        height: auto;
        margin-bottom: 1;
        color: {theme.FG2};
    }}

    #multi-input {{
        margin-bottom: 1;
    }}

    #multi-buttons {{
        height: auto;
        align: right middle;
    }}

    #multi-buttons Button {{
        margin-left: 1;
        min-width: 12;
    }}
    """

    BINDINGS: ClassVar[list[BindingSpec]] = [
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, prefill: str = "") -> None:
        super().__init__()
        self._prefill = prefill

    def compose(self) -> ComposeResult:
        with Vertical(id="multi-dialog") as dialog:
            dialog.border_title = "Merge sessions into unified timeline"
            yield Label(
                "Comma-separated session ids — events and logs are merged chronologically.",
                id="multi-header",
            )
            yield Input(
                value=self._prefill,
                placeholder="session-id-1, session-id-2, …",
                id="multi-input",
            )
            with Horizontal(id="multi-buttons"):
                yield Button("Cancel", id="btn-cancel", variant="default")
                yield Button("Merge", id="btn-merge", variant="success")

    def on_mount(self) -> None:
        self.query_one("#multi-input", Input).focus()

    @on(Button.Pressed, "#btn-merge")
    def _on_merge(self) -> None:
        self._submit()

    @on(Button.Pressed, "#btn-cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#multi-input")
    def _on_input_submitted(self) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        raw = self.query_one("#multi-input", Input).value
        ids = tuple(part.strip() for part in raw.split(",") if part.strip())
        if not ids:
            self.query_one("#multi-input", Input).focus()
            return
        self.dismiss(ids)


__all__ = ["MultiSessionPickerScreen"]
