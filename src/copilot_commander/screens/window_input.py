from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from copilot_commander.bindings import BindingSpec
from copilot_commander.services.action_service import WindowChoice


@dataclass(frozen=True, slots=True)
class RenameWindowResult:
    name: str


@dataclass(frozen=True, slots=True)
class MoveWindowResult:
    target_window: str | None
    new_window_name: str | None


_DIALOG_CSS = """
#window-input-dialog {
    width: 88;
    height: auto;
    max-height: 24;
    background: #282828;
    border: thick #504945;
    border-title-color: #83a598;
    padding: 1 2;
}

#window-input-header,
#window-input-help,
#window-input-status {
    height: auto;
    margin-bottom: 1;
    color: #a89984;
}

#window-input-buttons {
    height: auto;
    align: right middle;
}

#window-input-buttons Button {
    margin-left: 1;
    min-width: 12;
}

#btn-window-confirm {
    background: #504945;
    color: #b8bb26;
    border: none;
}

#btn-window-confirm:hover {
    background: #665c54;
}

#btn-window-cancel {
    background: #3c3836;
    color: #928374;
    border: none;
}

#btn-window-cancel:hover {
    background: #504945;
}
"""


class RenameWindowScreen(ModalScreen[RenameWindowResult | None]):
    DEFAULT_CSS = f"""
    RenameWindowScreen {{
        align: center middle;
    }}
{_DIALOG_CSS}
    """

    BINDINGS: ClassVar[list[BindingSpec]] = [("escape", "cancel", "Cancel")]

    def __init__(self, display_name: str, *, current_name: str | None = None) -> None:
        super().__init__()
        self._display_name = display_name
        self._current_name = current_name or ""

    def compose(self) -> ComposeResult:
        with Vertical(id="window-input-dialog") as dialog:
            dialog.border_title = "Rename Window"
            yield Label(
                f"Rename the tmux window for {self._display_name}.",
                id="window-input-header",
            )
            yield Input(
                value=self._current_name,
                placeholder="New window name…",
                id="window-input-value",
            )
            yield Static(id="window-input-status")
            with Horizontal(id="window-input-buttons"):
                yield Button("Cancel", id="btn-window-cancel", variant="default")
                yield Button("Rename", id="btn-window-confirm", variant="success")

    def on_mount(self) -> None:
        self.query_one("#window-input-value", Input).focus()

    @on(Button.Pressed, "#btn-window-confirm")
    def _on_confirm(self) -> None:
        self._submit()

    @on(Button.Pressed, "#btn-window-cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#window-input-value")
    def _on_submitted(self) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        value = self.query_one("#window-input-value", Input).value.strip()
        if not value:
            self.query_one("#window-input-status", Static).update("window name is required")
            self.query_one("#window-input-value", Input).focus()
            return
        self.dismiss(RenameWindowResult(name=value))


class MoveWindowScreen(ModalScreen[MoveWindowResult | None]):
    DEFAULT_CSS = f"""
    MoveWindowScreen {{
        align: center middle;
    }}
{_DIALOG_CSS}
    """

    BINDINGS: ClassVar[list[BindingSpec]] = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        display_name: str,
        *,
        current_window_name: str | None = None,
        choices: Sequence[WindowChoice] = (),
    ) -> None:
        super().__init__()
        self._display_name = display_name
        self._current_window_name = current_window_name
        self._choices = tuple(choices)

    def compose(self) -> ComposeResult:
        with Vertical(id="window-input-dialog") as dialog:
            dialog.border_title = "Move To Window"
            current = self._current_window_name or "current window"
            yield Label(
                (
                    f"Move {self._display_name} out of {current}. "
                    "Enter an existing window id/name or a new name."
                ),
                id="window-input-header",
            )
            yield Static(self._render_choices(), id="window-input-help")
            yield Input(
                placeholder="@2, dashboard, or a new window name…",
                id="window-input-value",
            )
            yield Static(id="window-input-status")
            with Horizontal(id="window-input-buttons"):
                yield Button("Cancel", id="btn-window-cancel", variant="default")
                yield Button("Move", id="btn-window-confirm", variant="success")

    def on_mount(self) -> None:
        self.query_one("#window-input-value", Input).focus()

    @on(Button.Pressed, "#btn-window-confirm")
    def _on_confirm(self) -> None:
        self._submit()

    @on(Button.Pressed, "#btn-window-cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#window-input-value")
    def _on_submitted(self) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        value = self.query_one("#window-input-value", Input).value.strip()
        if not value:
            self.query_one("#window-input-status", Static).update("window target is required")
            self.query_one("#window-input-value", Input).focus()
            return
        matched = self._match_choice(value)
        if matched is not None:
            self.dismiss(MoveWindowResult(target_window=matched.window_id, new_window_name=None))
            return
        self.dismiss(MoveWindowResult(target_window=None, new_window_name=value))

    def _render_choices(self) -> str:
        if not self._choices:
            return "No other windows discovered. Enter a new name to create one."
        preview = "; ".join(choice.label for choice in self._choices[:4])
        suffix = " …" if len(self._choices) > 4 else ""
        return f"Known windows: {preview}{suffix}"

    def _match_choice(self, value: str) -> WindowChoice | None:
        lowered = value.casefold()
        for choice in self._choices:
            if lowered in {
                choice.window_id.casefold(),
                (choice.window_name or "").casefold(),
                choice.label.casefold(),
                f"{choice.session_name}:{choice.window_id}".casefold(),
                f"{choice.session_name}:{choice.window_name or ''}".casefold(),
            }:
                return choice
        return None


__all__ = [
    "MoveWindowResult",
    "MoveWindowScreen",
    "RenameWindowResult",
    "RenameWindowScreen",
]
