from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Final

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

from copilot_commander.bindings import KeyHint
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.theme import (
    AQUA,
    BLUE,
    BLUE_DIM,
    FG,
    FG1,
    FG3,
    FG4,
    GREEN,
    ORANGE,
    RED,
    YELLOW,
)

# Keys that perform actions on agents / the fleet. Rendered with
# higher visual weight in the hint footer to signal that the TUI is
# a command surface, not a passive log viewer.
ACTION_HINT_KEYS: frozenset[str] = frozenset(
    {
        "i",
        "c",
        "m",
        "v",
        "p",
        "l",
        "S",
        "A",
        "x",
        "d",
        "R",
        "enter",
        "↵",
        "y",
    }
)

# ── status glyphs ───────────────────────────────────────────────────

_STATUS_GLYPHS: Final[dict[AgentStatus, tuple[str, str]]] = {
    AgentStatus.RUNNING: ("●", GREEN),
    AgentStatus.IDLE: ("◐", YELLOW),
    AgentStatus.WAITING_INPUT: ("▲", ORANGE),
    AgentStatus.BLOCKED: ("■", ORANGE),
    AgentStatus.ERROR: ("✗", RED),
    AgentStatus.DEAD: ("✗", YELLOW),
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


def status_glyph_parts(status: AgentStatus) -> tuple[str, str]:
    """Return (char, color) for a status — for inline text building."""
    return _STATUS_GLYPHS.get(status, ("?", FG3))


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
    ("1", "dashboard"),
    ("2", "worktrees"),
    ("3", "replay"),
    ("4", "sessions"),
    ("5", "setup"),
    ("6", "attention"),
    ("?", "help"),
)

_BADGE_GLYPH: Final[str] = "⬤"


class TabBar(Static):
    """Single-line tab bar — modern minimal branding."""

    active_tab = reactive("dashboard")
    badges: reactive[Mapping[str, int]] = reactive[Mapping[str, int]]({})

    def __init__(
        self,
        *,
        active: str = "dashboard",
        badges: Mapping[str, int] | None = None,
        widget_id: str | None = None,
    ) -> None:
        super().__init__(id=widget_id)
        self.active_tab = active
        self.badges = dict(badges) if badges else {}

    def set_badges(self, badges: Mapping[str, int]) -> None:
        self.badges = dict(badges)

    def render(self) -> Text:
        bar = Text()
        bar.append(" ◆ ", style=f"bold {BLUE}")
        bar.append("commander ", style=f"bold {FG}")
        bar.append("│ ", style=FG4)
        for key, label in _TAB_ITEMS:
            is_active = label.lower() == self.active_tab.lower()
            badge_count = int(self.badges.get(label, 0))
            if is_active:
                bar.append(f" {label} ", style=f"bold {FG} on {BLUE_DIM}")
            else:
                bar.append(f" {key}·{label} ", style=FG4)
            if badge_count > 0:
                bar.append(f"{_BADGE_GLYPH}{badge_count} ", style=f"bold {RED}")
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
        footer.append(f" {self.status}", style=FG3)
        footer.append("  │", style=FG4)
        for hint in self.hints:
            if hint.key in ACTION_HINT_KEYS:
                footer.append(f"  {hint.key}", style=f"bold {ORANGE}")
                footer.append(f" {hint.label}", style=f"bold {FG1}")
            else:
                footer.append(f"  {hint.key}", style=f"bold {BLUE}")
                footer.append(f" {hint.label}", style=FG4)
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
    "status_glyph_parts",
]
