from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Final

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

from muxdeck.bindings import KeyHint
from muxdeck.domain.enums import AgentStatus
from muxdeck.theme import (
    AQUA,
    BLUE,
    BLUE_DIM,
    FG,
    FG1,
    FG2,
    FG3,
    FG4,
    GREEN,
    ORANGE,
    RED,
    YELLOW,
)
from muxdeck.ui_preferences import (
    UiDecorations,
    UiGlyphs,
    UiPreferences,
    resolve_ui_preferences,
)

# Keys that perform actions on agents. Rendered with
# higher visual weight in the hint footer to signal that the TUI is
# a command surface, not a passive log viewer.
ACTION_HINT_KEYS: frozenset[str] = frozenset(
    {
        "i",
        "c",
        "ctrl+p",
        "m",
        "v",
        "w",
        "f",
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

_RICH_STATUS_GLYPHS: Final[dict[AgentStatus, tuple[str, str]]] = {
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

_ASCII_STATUS_GLYPHS: Final[dict[AgentStatus, tuple[str, str]]] = {
    AgentStatus.RUNNING: ("o", GREEN),
    AgentStatus.IDLE: ("~", YELLOW),
    AgentStatus.WAITING_INPUT: ("!", ORANGE),
    AgentStatus.BLOCKED: ("#", ORANGE),
    AgentStatus.ERROR: ("x", RED),
    AgentStatus.DEAD: ("x", YELLOW),
    AgentStatus.COMPLETED: ("v", FG4),
    AgentStatus.DISCOVERED: ("o", BLUE),
    AgentStatus.STARTING: (".", AQUA),
    AgentStatus.UNKNOWN: ("?", FG3),
}

_RICH_UI_GLYPHS: Final[dict[str, str]] = {
    "brand": "◆",
    "badge": "⬤",
    "separator": "│",
    "tab-key-separator": "·",
    "item-separator": "·",
    "selected": "▎",
    "expanded": "▾",
    "collapsed": "▸",
    "subagent": "↳",
    "bullet": "•",
    "section-lead": " ── ",
    "section-fill": " ──────────────────────────────────────",
    "connector-mid": "├─",
    "connector-last": "└─",
    "detail-arrow": "»",
    "progress-play": "▶",
    "progress-pause": "⏸",
    "progress-position": "●",
    "progress-marker": "│",
    "progress-fill": "─",
    "background-task": "⚡",
    "annotation": "✎",
}

_ASCII_UI_GLYPHS: Final[dict[str, str]] = {
    "brand": "*",
    "badge": "*",
    "separator": "|",
    "tab-key-separator": ":",
    "item-separator": "/",
    "selected": ">",
    "expanded": "v",
    "collapsed": ">",
    "subagent": "->",
    "bullet": "-",
    "section-lead": " -- ",
    "section-fill": " --------------------------------------",
    "connector-mid": "|-",
    "connector-last": "`-",
    "detail-arrow": ">",
    "progress-play": ">",
    "progress-pause": "||",
    "progress-position": "*",
    "progress-marker": "|",
    "progress-fill": "-",
    "background-task": "!",
    "annotation": "*",
}

_REDUCED_UI_OVERRIDES: Final[dict[str, str]] = {
    "brand": "",
    "badge": "*",
    "separator": "|",
    "tab-key-separator": ":",
    "item-separator": "/",
    "bullet": "-",
    "section-lead": " ",
    "section-fill": "",
}


def ui_symbol(name: str, *, preferences: UiPreferences | None = None) -> str:
    prefs = UiPreferences() if preferences is None else preferences
    glyphs = _ASCII_UI_GLYPHS if prefs.glyphs is UiGlyphs.ASCII else _RICH_UI_GLYPHS
    symbol = glyphs[name]
    if prefs.decorations is UiDecorations.REDUCED:
        return _REDUCED_UI_OVERRIDES.get(name, symbol)
    return symbol


def pipe_separator(preferences: UiPreferences | None = None) -> str:
    return f" {ui_symbol('separator', preferences=preferences)} "


def item_separator(preferences: UiPreferences | None = None) -> str:
    return f" {ui_symbol('item-separator', preferences=preferences)} "


def _status_glyph_lookup(preferences: UiPreferences | None) -> dict[AgentStatus, tuple[str, str]]:
    prefs = UiPreferences() if preferences is None else preferences
    if prefs.glyphs is UiGlyphs.ASCII:
        return _ASCII_STATUS_GLYPHS
    return _RICH_STATUS_GLYPHS


def status_glyph(
    status: AgentStatus,
    *,
    selected: bool = False,
    preferences: UiPreferences | None = None,
) -> Text:
    """Return a single-char Rich Text glyph for the given agent status."""
    char, color = _status_glyph_lookup(preferences).get(status, ("?", FG3))
    style = f"bold {color}"
    if selected:
        style = f"bold {BLUE}"
    return Text(char, style=style)


def status_glyph_char(status: AgentStatus, *, preferences: UiPreferences | None = None) -> str:
    """Return the raw character for a status (for plain-text contexts)."""
    char, _ = _status_glyph_lookup(preferences).get(status, ("?", FG3))
    return char


def status_glyph_parts(
    status: AgentStatus, *, preferences: UiPreferences | None = None
) -> tuple[str, str]:
    """Return (char, color) for a status — for inline text building."""
    return _status_glyph_lookup(preferences).get(status, ("?", FG3))


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
        preferences = resolve_ui_preferences(self)
        separator = pipe_separator(preferences)
        key_separator = ui_symbol("tab-key-separator", preferences=preferences)
        badge_glyph = ui_symbol("badge", preferences=preferences)
        bar = Text()
        brand = ui_symbol("brand", preferences=preferences)
        if brand:
            bar.append(f" {brand} ", style=f"bold {BLUE}")
        bar.append("muxdeck ", style=f"bold {FG}")
        bar.append(separator, style=FG4)
        for key, label in _TAB_ITEMS:
            is_active = label.lower() == self.active_tab.lower()
            badge_count = int(self.badges.get(label, 0))
            if is_active:
                bar.append(f" {label} ", style=f"bold {FG} on {BLUE_DIM}")
            else:
                bar.append(f" {key}{key_separator}{label} ", style=FG4)
            if badge_count > 0:
                bar.append(f"{badge_glyph}{badge_count} ", style=f"bold {RED}")
        mode_badges = preferences.mode_badges()
        if mode_badges:
            bar.append(separator, style=FG4)
            bar.append("modes ", style=FG4)
            bar.append(item_separator(preferences).join(mode_badges), style=FG3)
        return bar


# ── KeyHintFooter ───────────────────────────────────────────────────


class KeyHintFooter(Static):
    status = reactive("ready")
    hints: reactive[tuple[KeyHint, ...]] = reactive(())
    focus_label = reactive("")
    busy = reactive(False)

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
        preferences = resolve_ui_preferences(self)
        separator = pipe_separator(preferences)
        footer = Text()
        if self.busy:
            # "working" indicator carries a real state — the runtime
            # is mid-flight. Keep it amber so it reads as "in progress,
            # don't act yet".
            footer.append(" ● working", style=f"bold {ORANGE}")
            footer.append(separator, style=FG4)
            footer.append(self.status, style=FG3)
        else:
            footer.append(f" {self.status}", style=FG3)
        if self.focus_label:
            footer.append(separator, style=FG4)
            footer.append("focus ", style=FG4)
            footer.append(self.focus_label, style=f"bold {FG1}")
        mode_badges = preferences.mode_badges()
        if mode_badges:
            footer.append(separator, style=FG4)
            footer.append("modes ", style=FG4)
            footer.append(item_separator(preferences).join(mode_badges), style=FG2)
        footer.append(separator.rstrip(), style=FG4)
        # Hint key colour rules (graphite redesign):
        # - ACTION_HINT_KEYS get the primary-action accent (BLUE) so
        #   the operator's eye lands on the next action they should
        #   take. The previous ORANGE-for-shortcuts conflicted with
        #   the warning meaning of ORANGE elsewhere in the UI.
        # - Regular nav hints get a quieter FG3 key + FG4 label so the
        #   footer reads as muted infrastructure, with the primary
        #   actions standing out in blue.
        for hint in self.hints:
            if hint.key in ACTION_HINT_KEYS:
                footer.append(f"  {hint.key}", style=f"bold {BLUE}")
                footer.append(f" {hint.label}", style=f"bold {FG1}")
            else:
                footer.append(f"  {hint.key}", style=f"bold {FG3}")
                footer.append(f" {hint.label}", style=FG4)
        return footer


__all__ = [
    "KeyHintFooter",
    "TabBar",
    "format_bool",
    "format_short_timestamp",
    "format_timestamp",
    "item_separator",
    "join_lines",
    "pipe_separator",
    "status_glyph",
    "status_glyph_char",
    "status_glyph_parts",
    "ui_symbol",
]
