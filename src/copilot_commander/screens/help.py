from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Input, Static

from copilot_commander.bindings import (
    DASHBOARD_HINTS,
    GLOBAL_HINTS,
    HELP_BINDINGS,
    HELP_HINTS,
    REPLAY_HINTS,
    SETUP_HINTS,
    WORKTREE_HINTS,
    KeyHint,
)
from copilot_commander.screens.base import ShellScreen
from copilot_commander.theme import AQUA, BLUE, FG, FG1, FG4, YELLOW
from copilot_commander.ui_preferences import resolve_ui_preferences
from copilot_commander.widgets.common import item_separator, pipe_separator, ui_symbol

if TYPE_CHECKING:
    from copilot_commander.app import CommanderRuntime

_HINT_SECTIONS: tuple[tuple[str, tuple[KeyHint, ...]], ...] = (
    ("Global", GLOBAL_HINTS),
    ("Dashboard", DASHBOARD_HINTS),
    ("Worktrees", WORKTREE_HINTS),
    ("Replay", REPLAY_HINTS),
    ("Setup", SETUP_HINTS),
)

_HELP_NOTES: tuple[str, ...] = (
    "ctrl+p opens the command palette from any screen",
    (
        "Use the command palette to toggle comfortable density, simple glyphs, "
        "high contrast, reduced decoration, and log wrapping"
    ),
    "The footer shows the current focus target and any active UI modes",
    "Discovery scans tmux with list-panes -a across the current server",
    "Copilot panes in other windows appear on the dashboard",
    "Use the Setup screen to inspect tmux socket health and switch servers",
    "Press y on dashboard, worktrees, sessions, or fleet to copy the selected details",
    "Use r to rescan, j/k to move, / to filter, and 1-8 to switch screens",
)


def _matches(query: str, *parts: str) -> bool:
    if not query:
        return True
    lowered = query.casefold()
    return any(lowered in part.casefold() for part in parts if part)


def _render_hints(title: str, hints: Sequence[KeyHint], *, query: str) -> tuple[Text, int]:
    matching = tuple(hint for hint in hints if _matches(query, title, hint.key, hint.label))
    if not matching:
        return Text(), 0
    section = Text()
    section.append(f"\n {title}\n", style=f"bold {BLUE}")
    for hint in matching:
        section.append(f"  {hint.key:<10}", style=f"bold {YELLOW}")
        section.append(f"{hint.label}\n", style=FG1)
    return section, len(matching)


class HelpScreen(ShellScreen):
    SCREEN_TITLE = "HELP"
    BINDINGS = HELP_BINDINGS
    FOOTER_HINTS = HELP_HINTS

    def __init__(self, runtime: CommanderRuntime) -> None:
        super().__init__(runtime)
        self._filter_text = ""

    def compose_body(self) -> ComposeResult:
        with Vertical(id="help-root"):
            yield Input(
                placeholder="/ search bindings, commands, and accessibility modes",
                id="help-filter-input",
            )
            with VerticalScroll(id="help-scroll"):
                yield Static(id="help-content")

    def on_mount(self) -> None:
        self.set_status("operator reference")
        self.call_after_refresh(self.refresh_data)

    def on_show(self) -> None:
        self.refresh_data()

    def apply_ui_preferences(self) -> bool:
        if self.is_mounted:
            self.call_after_refresh(self.refresh_data)
        return True

    def action_focus_filter(self) -> None:
        self.query_one("#help-filter-input", Input).focus()
        self.set_status("search help")

    def action_escape_filter(self) -> None:
        filter_input = self.query_one("#help-filter-input", Input)
        if filter_input.has_focus:
            self.set_focus(None)
            self.set_status("operator reference")
            return
        if self._filter_text:
            self._filter_text = ""
            filter_input.value = ""
            self.refresh_data()
            self.set_status("operator reference")
            return
        self.app.switch_mode("dashboard")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "help-filter-input":
            return
        self._filter_text = event.value
        self.refresh_data()

    def refresh_data(self) -> None:
        preferences = resolve_ui_preferences(self)
        mode_badges = preferences.mode_badges()
        query = self._filter_text.strip()
        content = Text()
        content.append(" Copilot Commander\n", style=f"bold {FG}")
        content.append(
            " Search bindings, screen actions, and accessibility controls.",
            style=FG4,
        )
        content.append("\n")
        content.append(f" {ui_symbol('bullet', preferences=preferences)} ", style=FG4)
        content.append("ctrl+p", style=f"bold {AQUA}")
        content.append(" command palette", style=FG1)
        content.append(pipe_separator(preferences), style=FG4)
        content.append("/", style=f"bold {AQUA}")
        content.append(" search help", style=FG1)
        if mode_badges:
            content.append(pipe_separator(preferences), style=FG4)
            content.append("active modes ", style=FG4)
            content.append(item_separator(preferences).join(mode_badges), style=AQUA)

        hint_matches = 0
        for title, hints in _HINT_SECTIONS:
            section, matches = _render_hints(title, hints, query=query)
            if matches:
                content.append_text(section)
                hint_matches += matches

        note_matches = tuple(note for note in _HELP_NOTES if _matches(query, "notes", note))
        if note_matches:
            content.append("\n Notes\n", style=f"bold {BLUE}")
            bullet = ui_symbol("bullet", preferences=preferences)
            for note in note_matches:
                content.append(f"  {bullet} {note}\n", style=FG4)

        if not hint_matches and not note_matches:
            content.append("\n No help matches\n", style=f"bold {AQUA}")
            content.append(
                "  Try words like filter, replay, copy, contrast, or commands.\n", style=FG4
            )

        try:
            self.query_one("#help-content", Static).update(content)
        except NoMatches:
            self.call_after_refresh(self.refresh_data)
            return
        if query:
            total = hint_matches + len(note_matches)
            self.set_status(f"help search · {total} match{'es' if total != 1 else ''}")
        else:
            self.set_status("operator reference")
