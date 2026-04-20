from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual import events, on
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

#window-choice-list {
    height: auto;
    max-height: 9;
    margin-bottom: 1;
    padding: 0 1;
    background: #1d2021;
    border: solid #504945;
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
        self._selected_index: int | None = 0 if self._choices else None

    def compose(self) -> ComposeResult:
        with Vertical(id="window-input-dialog") as dialog:
            dialog.border_title = "Move To Window"
            current = self._current_window_name or "current window"
            yield Label(
                (
                    f"Move {self._display_name} out of {current}. "
                    "Use arrows to pick an existing window or type a new one."
                ),
                id="window-input-header",
            )
            yield Static("↑/↓ choose · Enter move · type to create", id="window-input-help")
            yield Static(id="window-choice-list")
            yield Input(
                placeholder="@2, dashboard, or a new window name…",
                id="window-input-value",
            )
            yield Static(id="window-input-status")
            with Horizontal(id="window-input-buttons"):
                yield Button("Cancel", id="btn-window-cancel", variant="default")
                yield Button("Move", id="btn-window-confirm", variant="success")

    def on_mount(self) -> None:
        self._refresh_choices()
        self._refresh_status()
        self.query_one("#window-input-value", Input).focus()

    def on_key(self, event: events.Key) -> None:
        if event.key == "up":
            self._move_selection(-1)
            event.stop()
            event.prevent_default()
            return
        if event.key == "down":
            self._move_selection(1)
            event.stop()
            event.prevent_default()
            return

    @on(Button.Pressed, "#btn-window-confirm")
    def _on_confirm(self) -> None:
        self._submit()

    @on(Button.Pressed, "#btn-window-cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#window-input-value")
    def _on_submitted(self) -> None:
        self._submit()

    @on(Input.Changed, "#window-input-value")
    def _on_input_changed(self, event: Input.Changed) -> None:
        self._sync_selection(event.value)
        self._refresh_choices()
        self._refresh_status()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        value = self.query_one("#window-input-value", Input).value.strip()
        if not value:
            selected = self._selected_choice()
            if selected is not None:
                self.dismiss(
                    MoveWindowResult(target_window=selected.window_id, new_window_name=None)
                )
                return
            self.query_one("#window-input-status", Static).update("window target is required")
            self.query_one("#window-input-value", Input).focus()
            return
        matched = self._match_choice(value)
        if matched is not None:
            self.dismiss(MoveWindowResult(target_window=matched.window_id, new_window_name=None))
            return
        self.dismiss(MoveWindowResult(target_window=None, new_window_name=value))

    def _move_selection(self, delta: int) -> None:
        if not self._choices:
            return
        if self._selected_index is None:
            self._selected_index = 0
        else:
            self._selected_index = (self._selected_index + delta) % len(self._choices)
        choice = self._selected_choice()
        if choice is not None:
            self.query_one("#window-input-value", Input).value = self._choice_input_value(choice)
        self._refresh_choices()
        self._refresh_status()

    def _refresh_choices(self) -> None:
        panel = self.query_one("#window-choice-list", Static)
        panel.update(self._render_choices())

    def _refresh_status(self) -> None:
        status = self.query_one("#window-input-status", Static)
        value = self.query_one("#window-input-value", Input).value.strip()
        selected = self._selected_choice()
        if value:
            matched = self._match_choice(value)
            if matched is not None:
                status.update(f"move to {matched.label}")
                return
            status.update(f"create new window {value!r}")
            return
        if selected is not None:
            status.update(f"selected {selected.label} · Enter moves there")
            return
        status.update("type a new window name to create one")

    def _render_choices(self) -> Text:
        text = Text()
        if not self._choices:
            text.append("No other windows discovered. Type a name below to create one.")
            return text
        start, end = self._visible_choice_range()
        if start > 0:
            text.append("  …\n", style="#665c54")
        for index in range(start, end):
            choice = self._choices[index]
            is_selected = index == self._selected_index
            prefix = "▸ " if is_selected else "  "
            prefix_style = "bold #83a598" if is_selected else "#665c54"
            title_style = "bold #ebdbb2" if is_selected else "#d5c4a1"
            meta_style = "#a89984" if is_selected else "#928374"
            text.append(prefix, style=prefix_style)
            text.append(choice.window_name or choice.window_id, style=title_style)
            text.append(f"  {choice.session_name}:{choice.window_id}", style=meta_style)
            if choice.window_name == self._current_window_name:
                text.append("  current", style="#fabd2f")
            pane_label = "pane" if choice.pane_count == 1 else "panes"
            text.append(f"  ·  {choice.pane_count} {pane_label}", style=meta_style)
            text.append("\n")
        if end < len(self._choices):
            text.append("  …\n", style="#665c54")
        return text

    def _visible_choice_range(self) -> tuple[int, int]:
        max_visible = 6
        if len(self._choices) <= max_visible:
            return (0, len(self._choices))
        selected_index = self._selected_index or 0
        start = max(0, selected_index - (max_visible // 2))
        end = min(len(self._choices), start + max_visible)
        start = max(0, end - max_visible)
        return (start, end)

    def _sync_selection(self, value: str) -> None:
        normalized = value.strip()
        if not normalized:
            return
        match_index = self._find_choice_index(normalized)
        if match_index is not None:
            self._selected_index = match_index

    def _selected_choice(self) -> WindowChoice | None:
        if self._selected_index is None or not self._choices:
            return None
        return self._choices[self._selected_index]

    def _choice_input_value(self, choice: WindowChoice) -> str:
        if choice.window_name:
            return f"{choice.session_name}:{choice.window_name}"
        return f"{choice.session_name}:{choice.window_id}"

    def _find_choice_index(self, value: str, *, exact_only: bool = False) -> int | None:
        lowered = value.casefold()
        for index, choice in enumerate(self._choices):
            if lowered in self._choice_aliases(choice):
                return index
        if exact_only:
            return None
        for index, choice in enumerate(self._choices):
            aliases = self._choice_aliases(choice)
            if any(alias.startswith(lowered) for alias in aliases):
                return index
        for index, choice in enumerate(self._choices):
            if lowered in choice.label.casefold():
                return index
        return None

    def _choice_aliases(self, choice: WindowChoice) -> tuple[str, ...]:
        aliases = [
            choice.window_id.casefold(),
            choice.label.casefold(),
            f"{choice.session_name}:{choice.window_id}".casefold(),
        ]
        if choice.window_name:
            aliases.append(choice.window_name.casefold())
            aliases.append(f"{choice.session_name}:{choice.window_name}".casefold())
        return tuple(aliases)

    def _match_choice(self, value: str) -> WindowChoice | None:
        match_index = self._find_choice_index(value.strip(), exact_only=True)
        if match_index is None:
            return None
        return self._choices[match_index]


__all__ = [
    "MoveWindowResult",
    "MoveWindowScreen",
    "RenameWindowResult",
    "RenameWindowScreen",
]
