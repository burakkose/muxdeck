"""Modal screen for jumping the replay clock to a specific time."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import ClassVar, Final

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

_DELTA_RE: Final = re.compile(r"^([+-])(\d+)([smh])$")
_HMS_RE: Final = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_DELTA_UNITS: Final = {"s": 1, "m": 60, "h": 3600}


def parse_time_input(
    raw: str,
    *,
    clock: datetime,
    start: datetime,
    end: datetime,
) -> datetime | None:
    """Parse ``HH:MM[:SS]`` absolute or ``±N(s|m|h)`` delta time inputs.

    Returns the resulting :class:`datetime` clamped to ``[start, end]``,
    or ``None`` when the input is malformed. Absolute times are anchored
    to ``start``'s date; if the resulting moment falls outside the
    timeline the function still returns it (the caller clamps).
    """

    text = raw.strip()
    if not text:
        return None
    delta_match = _DELTA_RE.match(text)
    if delta_match is not None:
        sign, amount, unit = delta_match.groups()
        seconds = int(amount) * _DELTA_UNITS[unit]
        delta = timedelta(seconds=seconds)
        return clock - delta if sign == "-" else clock + delta
    hms_match = _HMS_RE.match(text)
    if hms_match is not None:
        hours = int(hms_match.group(1))
        minutes = int(hms_match.group(2))
        seconds = int(hms_match.group(3) or 0)
        if hours > 23 or minutes > 59 or seconds > 59:
            return None
        anchor = start
        return anchor.replace(
            hour=hours,
            minute=minutes,
            second=seconds,
            microsecond=0,
        )
    del end  # reserved for future contextual parsing
    return None


class JumpToTimeScreen(ModalScreen[datetime | None]):
    """Prompt the operator for a target playback time."""

    DEFAULT_CSS = """
    JumpToTimeScreen {
        align: center middle;
    }

    #jump-dialog {
        width: 70;
        height: auto;
        max-height: 14;
        background: #282828;
        border: thick #504945;
        border-title-color: #83a598;
        padding: 1 2;
    }

    #jump-header {
        height: auto;
        margin-bottom: 1;
        color: #a89984;
    }

    #jump-input {
        margin-bottom: 1;
    }

    #jump-buttons {
        height: auto;
        align: right middle;
    }

    #jump-buttons Button {
        margin-left: 1;
        min-width: 10;
    }
    """

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        *,
        clock: datetime,
        start: datetime,
        end: datetime,
    ) -> None:
        super().__init__()
        self._clock = clock
        self._start = start
        self._end = end

    def compose(self) -> ComposeResult:
        with Vertical(id="jump-dialog") as dialog:
            dialog.border_title = "Jump replay clock"
            yield Label(
                "HH:MM[:SS] absolute, or ±Ns / ±Nm / ±Nh delta from clock.",
                id="jump-header",
            )
            yield Input(placeholder="e.g. 12:05:00 or +30s", id="jump-input")
            with Horizontal(id="jump-buttons"):
                yield Button("Cancel", id="btn-cancel", variant="default")
                yield Button("Jump", id="btn-jump", variant="success")

    def on_mount(self) -> None:
        self.query_one("#jump-input", Input).focus()

    @on(Button.Pressed, "#btn-jump")
    def _on_jump(self) -> None:
        self._submit()

    @on(Button.Pressed, "#btn-cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#jump-input")
    def _on_input_submitted(self) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        raw = self.query_one("#jump-input", Input).value
        target = parse_time_input(
            raw,
            clock=self._clock,
            start=self._start,
            end=self._end,
        )
        if target is None:
            self.query_one("#jump-input", Input).focus()
            return
        self.dismiss(target)


__all__ = ["JumpToTimeScreen", "parse_time_input"]
