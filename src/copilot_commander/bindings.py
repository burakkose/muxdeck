from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from textual.binding import Binding

type BindingSpec = Binding | tuple[str, str] | tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class KeyHint:
    key: str
    label: str


def _binding(
    key: str,
    action: str,
    description: str,
    /,
    *,
    show: bool = True,
    priority: bool = False,
    system: bool = False,
) -> Binding:
    del system
    return Binding(
        key,
        action,
        description,
        show=show,
        priority=priority,
    )


GLOBAL_BINDINGS: Final[list[BindingSpec]] = [
    _binding("1", "show_dashboard", "Dashboard", show=False, priority=True, system=True),
    _binding("2", "show_worktrees", "Worktrees", show=False, priority=True, system=True),
    _binding("3", "show_replay", "Replay", show=False, priority=True, system=True),
    _binding("4", "show_sessions", "Sessions", show=False, priority=True, system=True),
    _binding("5", "show_setup", "Setup", show=False, priority=True, system=True),
    _binding("question_mark", "show_help", "Help", show=False, priority=True, system=True),
    _binding("r", "refresh_screen", "Refresh", show=False, priority=True, system=True),
    _binding("tab", "focus_next", "Next focus", show=False, priority=True, system=True),
    _binding("shift+tab", "focus_previous", "Prev focus", show=False, priority=True, system=True),
    _binding("q", "quit", "Quit", show=False, priority=True, system=True),
]

GLOBAL_HINTS = (
    KeyHint("1", "dashboard"),
    KeyHint("2", "worktrees"),
    KeyHint("3", "replay"),
    KeyHint("4", "sessions"),
    KeyHint("5", "setup"),
    KeyHint("?", "help"),
    KeyHint("r", "refresh"),
    KeyHint("q", "quit"),
)

DASHBOARD_BINDINGS: Final[list[BindingSpec]] = [
    Binding("j", "cursor_down", "Next agent", show=False),
    Binding("k", "cursor_up", "Prev agent", show=False),
    Binding("slash", "focus_filter", "Filter", show=False),
    Binding("a", "toggle_attention", "Attention only", show=False),
    Binding("x", "toggle_completed", "Hide completed", show=False),
    Binding("s", "cycle_sort", "Sort", show=False),
    Binding("c", "mark_complete", "Mark complete", show=False),
    Binding("i", "interrupt_agent", "Interrupt", show=False),
    Binding("p", "open_pane", "Pane target", show=False),
    Binding("w", "open_worktree", "Worktree", show=False),
    Binding("m", "send_message", "Send message", show=False),
    Binding("S", "stop_all", "Stop all", show=False),
]

DASHBOARD_HINTS = (
    KeyHint("j/k", "move"),
    KeyHint("/", "filter"),
    KeyHint("a", "attention"),
    KeyHint("x", "completed"),
    KeyHint("s", "sort"),
    KeyHint("c", "complete"),
    KeyHint("i", "interrupt"),
    KeyHint("p", "pane"),
    KeyHint("w", "worktree"),
    KeyHint("m", "message"),
    KeyHint("S", "stop all"),
)

WORKTREE_BINDINGS: Final[list[BindingSpec]] = [
    Binding("j", "cursor_down", "Next worktree", show=False),
    Binding("k", "cursor_up", "Prev worktree", show=False),
    Binding("enter", "preview_start_agent", "Start intent", show=False),
    Binding("s", "preview_start_agent", "Start intent", show=False),
    Binding("x", "execute_start", "Execute start", show=False),
]

WORKTREE_HINTS = (
    KeyHint("j/k", "move"),
    KeyHint("s", "start intent"),
    KeyHint("enter", "preview"),
    KeyHint("x", "execute"),
)

REPLAY_BINDINGS: Final[list[BindingSpec]] = [
    Binding("j", "cursor_down", "Next entry", show=False),
    Binding("k", "cursor_up", "Prev entry", show=False),
    Binding("slash", "focus_filter", "Filter", show=False),
    Binding("m", "focus_markers", "Markers", show=False),
    Binding("t", "focus_transcript", "Transcript", show=False),
    Binding("v", "toggle_presentation", "Raw/parsed", show=False),
    Binding("f", "toggle_follow_latest", "Follow latest", show=False),
    Binding("n", "jump_next_marker", "Next marker", show=False),
    Binding("N", "jump_previous_marker", "Prev marker", show=False),
    Binding("a", "jump_next_activity", "Next activity", show=False),
    Binding("x", "jump_next_problem", "Next problem", show=False),
    Binding("e", "cycle_export_format", "Export", show=False),
    Binding("g", "load_latest", "Latest session", show=False),
]

REPLAY_HINTS = (
    KeyHint("j/k", "move"),
    KeyHint("/", "filter"),
    KeyHint("m", "markers"),
    KeyHint("t", "transcript"),
    KeyHint("v", "raw/parsed"),
    KeyHint("f", "follow"),
    KeyHint("n/N", "markers"),
    KeyHint("a", "activity"),
    KeyHint("x", "problem"),
    KeyHint("e", "export"),
    KeyHint("g", "latest"),
)

SESSIONS_BINDINGS: Final[list[BindingSpec]] = [
    Binding("j", "cursor_down", "Next session", show=False),
    Binding("k", "cursor_up", "Prev session", show=False),
    Binding("slash", "focus_filter", "Filter", show=False),
    Binding("r", "resume_session", "Resume", show=False),
    Binding("x", "toggle_completed", "Toggle completed", show=False),
    Binding("y", "copy_session_id", "Copy ID", show=False),
    Binding("p", "focus_pane", "Focus pane", show=False),
]

SESSIONS_HINTS = (
    KeyHint("j/k", "move"),
    KeyHint("/", "filter"),
    KeyHint("r", "resume"),
    KeyHint("x", "completed"),
    KeyHint("y", "copy ID"),
    KeyHint("p", "focus pane"),
)

SETUP_BINDINGS: Final[list[BindingSpec]] = [
    Binding("j", "cursor_down", "Next socket", show=False),
    Binding("k", "cursor_up", "Prev socket", show=False),
    Binding("enter", "apply_socket", "Use socket", show=False),
    Binding("c", "clear_socket", "Auto socket", show=False),
]

SETUP_HINTS = (
    KeyHint("j/k", "move"),
    KeyHint("enter", "apply"),
    KeyHint("c", "auto"),
)

HELP_BINDINGS: Final[list[BindingSpec]] = [
    Binding("escape", "show_dashboard", "Dashboard", show=False)
]

HELP_HINTS = (KeyHint("esc", "dashboard"),)

ALL_HINT_GROUPS = {
    "dashboard": DASHBOARD_HINTS,
    "worktrees": WORKTREE_HINTS,
    "replay": REPLAY_HINTS,
    "sessions": SESSIONS_HINTS,
    "setup": SETUP_HINTS,
    "help": HELP_HINTS,
}

__all__ = [
    "ALL_HINT_GROUPS",
    "DASHBOARD_BINDINGS",
    "DASHBOARD_HINTS",
    "GLOBAL_BINDINGS",
    "GLOBAL_HINTS",
    "HELP_BINDINGS",
    "HELP_HINTS",
    "REPLAY_BINDINGS",
    "REPLAY_HINTS",
    "SESSIONS_BINDINGS",
    "SESSIONS_HINTS",
    "SETUP_BINDINGS",
    "SETUP_HINTS",
    "WORKTREE_BINDINGS",
    "WORKTREE_HINTS",
    "KeyHint",
]
