from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Final

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

from copilot_commander.bindings import KeyHint
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.theme import (
    AQUA,
    BADGE_BG,
    BADGE_FG,
    BG1,
    BLUE,
    FG,
    FG1,
    FG3,
    FG4,
    GREEN,
    ORANGE,
    PANEL_BG,
    RED,
    YELLOW,
)

# ── status glyphs ───────────────────────────────────────────────────

_STATUS_GLYPHS: Final[dict[AgentStatus, tuple[str, str]]] = {
    AgentStatus.RUNNING: ("●", GREEN),
    AgentStatus.IDLE: ("◐", YELLOW),
    AgentStatus.WAITING_INPUT: ("▲", ORANGE),
    AgentStatus.BLOCKED: ("■", ORANGE),
    AgentStatus.ERROR: ("✗", RED),
    AgentStatus.DEAD: ("✗", RED),
    AgentStatus.COMPLETED: ("✓", FG4),
    AgentStatus.DISCOVERED: ("◇", BLUE),
    AgentStatus.STARTING: ("◌", AQUA),
    AgentStatus.UNKNOWN: ("?", FG3),
}


def status_glyph(status: AgentStatus, *, selected: bool = False) -> Text:
    """Return a single-char Rich Text glyph for the given agent status."""
    char, color = _STATUS_GLYPHS.get(status, ("?", FG3))
    style = f"bold {color}"
    if selected:
        style = f"bold {BLUE}"
    return Text(char, style=style)


def status_glyph_char(status: AgentStatus) -> str:
    """Return the raw character for a status (for plain-text contexts)."""
    char, _ = _STATUS_GLYPHS.get(status, ("?", FG3))
    return char


# ── formatting helpers ──────────────────────────────────────────────


def format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def format_short_timestamp(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone(UTC).strftime("%H:%M:%S")


def format_bool(value: bool) -> str:
    return "yes" if value else "no"


def join_lines(lines: Iterable[str]) -> str:
    collected = tuple(lines)
    return "\n".join(collected) if collected else "-"


# ── TabBar ──────────────────────────────────────────────────────────

_TAB_ITEMS: Final[tuple[tuple[str, str], ...]] = (
    ("1", "Dashboard"),
    ("2", "Worktrees"),
    ("3", "Replay"),
    ("?", "Help"),
)


class TabBar(Static):
    """Single-line tab bar replacing the Textual Header widget."""

    active_tab = reactive("dashboard")

    def __init__(self, *, active: str = "dashboard", widget_id: str | None = None) -> None:
        super().__init__(id=widget_id)
        self.active_tab = active

    def render(self) -> Text:
        bar = Text()
        bar.append(" ⌘ ", style=f"bold {BADGE_FG} on {BADGE_BG}")
        for key, label in _TAB_ITEMS:
            bar.append("  ")
            is_active = label.lower() == self.active_tab.lower()
            if is_active:
                bar.append(f" {key}", style=f"bold {BLUE}")
                bar.append(f" {label} ", style=f"bold {FG}")
            else:
                bar.append(f" {key}", style=FG4)
                bar.append(f" {label} ", style=FG4)
        return bar


# ── KeyHintFooter ───────────────────────────────────────────────────


class KeyHintFooter(Static):
    status = reactive("ready")
    hints: reactive[tuple[KeyHint, ...]] = reactive(())

    def __init__(
        self,
        *,
        hints: tuple[KeyHint, ...],
        status: str = "ready",
        widget_id: str | None = None,
    ) -> None:
        super().__init__(id=widget_id)
        self.hints = hints
        self.status = status

    def render(self) -> Text:
        footer = Text()
        footer.append(f" {self.status} ", style=f"{FG} on {BG1}")
        for hint in self.hints:
            footer.append(" ")
            footer.append(f" {hint.key} ", style=f"bold {BADGE_FG} on {PANEL_BG}")
            footer.append(f"{hint.label}", style=FG1)
        return footer


__all__ = [
    "KeyHintFooter",
    "TabBar",
    "format_bool",
    "format_short_timestamp",
    "format_timestamp",
    "join_lines",
    "status_glyph",
    "status_glyph_char",
]
