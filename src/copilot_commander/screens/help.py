from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from copilot_commander.bindings import (
    DASHBOARD_HINTS,
    GLOBAL_HINTS,
    HELP_BINDINGS,
    HELP_HINTS,
    REPLAY_HINTS,
    WORKTREE_HINTS,
    KeyHint,
)
from copilot_commander.screens.base import ShellScreen
from copilot_commander.theme import BLUE, FG, FG1, FG4, YELLOW


def _render_hints(title: str, hints: tuple[KeyHint, ...]) -> Text:
    section = Text()
    section.append(f"\n {title}\n", style=f"bold {BLUE}")
    for hint in hints:
        section.append(f"  {hint.key:<8}", style=f"bold {YELLOW}")
        section.append(f"{hint.label}\n", style=FG1)
    return section


class HelpScreen(ShellScreen):
    SCREEN_TITLE = "HELP"
    BINDINGS = HELP_BINDINGS
    FOOTER_HINTS = HELP_HINTS

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(id="help-root"):
            content = Text()
            content.append(" Copilot Commander\n", style=f"bold {FG}")
            content.append_text(_render_hints("Global", GLOBAL_HINTS))
            content.append_text(_render_hints("Dashboard", DASHBOARD_HINTS))
            content.append_text(_render_hints("Worktrees", WORKTREE_HINTS))
            content.append_text(_render_hints("Replay", REPLAY_HINTS))
            content.append("\n Notes\n", style=f"bold {BLUE}")
            for note in (
                "Discovery scans tmux with list-panes -a across the current server",
                "Copilot panes in other windows appear on the dashboard",
                "Run this app inside the same tmux server you want to inspect",
                "Use r to rescan, j/k to move, / to filter, 1/2/3 to switch",
            ):
                content.append(f"  • {note}\n", style=FG4)
            yield Static(content, id="help-content")

    def on_mount(self) -> None:
        self.set_status("operator reference")
