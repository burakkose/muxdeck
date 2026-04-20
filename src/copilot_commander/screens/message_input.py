"""Modal screen for sending a message to an agent's tmux pane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

from copilot_commander import theme
from copilot_commander.bindings import BindingSpec


@dataclass(frozen=True, slots=True)
class MessageResult:
    """Result from the send-message modal."""

    text: str
    pane_id: str


class SendMessageScreen(ModalScreen[MessageResult | None]):
    """Modal for typing a message to send to an agent's pane.

    Dismissed with a ``MessageResult`` on send, or ``None`` on cancel.
    """

    DEFAULT_CSS = f"""
    SendMessageScreen {{
        align: center middle;
    }}

    #message-dialog {{
        width: 70;
        height: auto;
        max-height: 16;
        background: {theme.BG1};
        border: thick {theme.BORDER};
        border-title-color: {theme.BORDER_FOCUS};
        padding: 1 2;
    }}

    #message-header {{
        height: auto;
        margin-bottom: 1;
        color: {theme.FG2};
    }}

    #message-input {{
        margin-bottom: 1;
    }}

    #message-buttons {{
        height: auto;
        align: right middle;
    }}

    #message-buttons Button {{
        margin-left: 1;
        min-width: 12;
    }}

    #btn-send {{
        background: {theme.BADGE_BG};
        color: {theme.BADGE_FG};
        border: none;
    }}

    #btn-send:hover {{
        background: {theme.YELLOW};
    }}

    #btn-cancel {{
        background: {theme.BG3};
        color: {theme.FG3};
        border: none;
    }}

    #btn-cancel:hover {{
        background: {theme.BG4};
    }}
    """

    BINDINGS: ClassVar[list[BindingSpec]] = [
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        agent_name: str,
        pane_id: str,
        *,
        name: str | None = None,
        widget_id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=widget_id, classes=classes)
        self._agent_name = agent_name
        self._pane_id = pane_id

    def compose(self) -> ComposeResult:
        with Vertical(id="message-dialog") as dialog:
            dialog.border_title = f"Send to {self._agent_name}"
            yield Label(
                f"Pane: {self._pane_id}  •  Text will be sent with Enter",
                id="message-header",
            )
            yield Input(
                placeholder="Type your message…",
                id="message-input",
            )
            with Horizontal(id="message-buttons"):
                yield Button("Cancel", id="btn-cancel", variant="default")
                yield Button("Send", id="btn-send", variant="success")

    def on_mount(self) -> None:
        self.query_one("#message-input", Input).focus()

    @on(Button.Pressed, "#btn-send")
    def _on_send(self) -> None:
        text = self.query_one("#message-input", Input).value.strip()
        if text:
            self.dismiss(MessageResult(text=text, pane_id=self._pane_id))
        else:
            self.query_one("#message-input", Input).focus()

    @on(Button.Pressed, "#btn-cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#message-input")
    def _on_input_submitted(self) -> None:
        self._on_send()

    def action_cancel(self) -> None:
        """Handle the escape binding."""
        self.dismiss(None)


__all__ = ["MessageResult", "SendMessageScreen"]
