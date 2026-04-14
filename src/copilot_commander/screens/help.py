from __future__ import annotations

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
)
from copilot_commander.screens.base import ShellScreen


class HelpScreen(ShellScreen):
    SCREEN_TITLE = "HELP"
    BINDINGS = HELP_BINDINGS
    FOOTER_HINTS = HELP_HINTS

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(id="help-root"):
            yield Static(
                "\n".join(
                    (
                        "COPILOT COMMANDER OPERATOR CONSOLE",
                        "",
                        "GLOBAL",
                        *[f"  {hint.key:<6} {hint.label}" for hint in GLOBAL_HINTS],
                        "",
                        "DASHBOARD",
                        *[f"  {hint.key:<6} {hint.label}" for hint in DASHBOARD_HINTS],
                        "",
                        "WORKTREES",
                        *[f"  {hint.key:<6} {hint.label}" for hint in WORKTREE_HINTS],
                        "",
                        "REPLAY",
                        *[f"  {hint.key:<6} {hint.label}" for hint in REPLAY_HINTS],
                        "",
                        "NOTES",
                        "  - widgets render controller state only",
                        "  - footer status shows the latest operator intent preview",
                        "  - dashboard selection seeds replay context where possible",
                    )
                ),
                id="help-content",
                markup=False,
            )

    def on_mount(self) -> None:
        self.set_status("operator reference")
