from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

from copilot_commander.bindings import KeyHint


def format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def format_short_timestamp(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone(UTC).strftime("%H:%M:%SZ")


def format_bool(value: bool) -> str:
    return "yes" if value else "no"


def join_lines(lines: Iterable[str]) -> str:
    collected = tuple(lines)
    return "\n".join(collected) if collected else "-"


class KeyHintFooter(Static):
    title = reactive("DASHBOARD")
    status = reactive("ready")
    hints: reactive[tuple[KeyHint, ...]] = reactive(())

    def __init__(
        self,
        *,
        title: str,
        hints: tuple[KeyHint, ...],
        status: str = "ready",
        widget_id: str | None = None,
    ) -> None:
        super().__init__(id=widget_id)
        self.title = title
        self.hints = hints
        self.status = status

    def render(self) -> Text:
        footer = Text()
        footer.append(f" {self.title.upper()} ", style="bold black on cyan")
        footer.append(f" {self.status} ", style="white on rgb(20,28,38)")
        for hint in self.hints:
            footer.append(" ")
            footer.append(f" {hint.key} ", style="bold black on rgb(148,163,184)")
            footer.append(f" {hint.label}", style="rgb(191,219,254)")
        return footer


__all__ = [
    "KeyHintFooter",
    "format_bool",
    "format_short_timestamp",
    "format_timestamp",
    "join_lines",
]
