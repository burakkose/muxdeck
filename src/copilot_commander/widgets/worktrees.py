from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import ListItem, ListView, Static

from copilot_commander.controllers import (
    WorktreeConflictView,
    WorktreeDetailView,
    WorktreeStartAgentIntent,
    WorktreeSummaryView,
)
from copilot_commander.theme import BLUE, FG, FG1, FG4, GREEN, ORANGE, YELLOW
from copilot_commander.widgets.common import format_bool, join_lines


class WorktreeListPanel(Vertical):
    class WorktreeSelected(Message):
        def __init__(self, worktree_id: str) -> None:
            super().__init__()
            self.worktree_id = worktree_id

    def __init__(self, *, widget_id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=widget_id, classes=classes)
        self._worktree_ids: list[str] = []

    def on_mount(self) -> None:
        self.border_title = "Worktrees"

    def compose(self) -> ComposeResult:
        yield ListView(id="worktree-list")

    def set_worktrees(
        self,
        worktrees: Sequence[WorktreeSummaryView],
        *,
        selected_worktree_id: str | None,
    ) -> None:
        list_view = self.query_one(ListView)
        list_view.clear()
        self._worktree_ids = []
        self.border_title = f"Worktrees ({len(worktrees)})"
        selected_index = 0
        for index, worktree in enumerate(worktrees):
            line = Text()
            if worktree.is_main_worktree:
                line.append("★ ", style=f"bold {GREEN}")
            else:
                line.append("  ")
            line.append(f"{worktree.branch:<18.18}", style=f"bold {FG}")
            dirty_style = f"bold {ORANGE}" if worktree.is_dirty else FG4
            line.append(f" {'DIRTY' if worktree.is_dirty else 'clean':<5}", style=dirty_style)
            line.append(f" ctx:{worktree.context_count}", style=FG4)
            agent = worktree.assigned_agent_name or "—"
            agent_style = f"bold {BLUE}" if worktree.assigned_agent_name else FG4
            line.append(f" {agent}", style=agent_style)
            list_view.append(ListItem(Static(line)))
            self._worktree_ids.append(worktree.worktree_id)
            if worktree.worktree_id == selected_worktree_id:
                selected_index = index
        if self._worktree_ids:
            list_view.index = selected_index
        self._post_selection(list_view.index)

    def move_cursor(self, delta: int) -> None:
        if not self._worktree_ids:
            return
        list_view = self.query_one(ListView)
        current = list_view.index if list_view.index is not None else 0
        list_view.index = max(0, min(len(self._worktree_ids) - 1, current + delta))
        list_view.focus()
        self._post_selection(list_view.index)

    def focus_list(self) -> None:
        self.query_one(ListView).focus()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        del event
        self._post_selection(self.query_one(ListView).index)

    def _post_selection(self, index: int | None) -> None:
        if index is None or index >= len(self._worktree_ids):
            return
        self.post_message(self.WorktreeSelected(self._worktree_ids[index]))


class WorktreeDetailPanel(Static):
    def on_mount(self) -> None:
        self.border_title = "Detail"

    def set_detail(self, detail: WorktreeDetailView | None) -> None:
        if detail is None:
            self.update(Text("No worktree selected", style=FG4))
            return
        summary = detail.summary
        lines: list[Text] = []
        for label, value, style in (
            ("branch", summary.branch, f"bold {FG}"),
            ("path", summary.path, FG1),
            ("repo", summary.repo_root, FG1),
            ("base", summary.base_branch or "-", FG1),
            ("main", format_bool(summary.is_main_worktree), FG1),
            ("dirty", format_bool(summary.is_dirty), f"bold {ORANGE}" if summary.is_dirty else FG1),
            ("ahead", str(summary.ahead_count) if summary.ahead_count is not None else "-", FG1),
            ("behind", str(summary.behind_count) if summary.behind_count is not None else "-", FG1),
            ("locked", format_bool(summary.locked), f"bold {YELLOW}" if summary.locked else FG1),
            ("agent",
             summary.assigned_agent_name or summary.assigned_agent_id or "-",
             f"bold {BLUE}" if summary.assigned_agent_name else FG1),
            ("sessions", str(summary.active_session_count), FG1),
            ("contexts", str(summary.context_count), FG1),
            ("panes", ", ".join(detail.pane_targets) if detail.pane_targets else "-", FG1),
        ):
            line = Text()
            line.append(f"{label:<9}", style=FG4)
            line.append(str(value), style=style)
            lines.append(line)
        result = Text()
        for i, line in enumerate(lines):
            if i:
                result.append("\n")
            result.append_text(line)
        self.update(result)


class ConflictPanel(Static):
    def on_mount(self) -> None:
        self.border_title = "Conflicts"

    def set_conflicts(self, conflicts: Sequence[WorktreeConflictView]) -> None:
        if not conflicts:
            self.update(Text("No conflicts", style=FG4))
            return
        lines = [
            f"{conflict.code:<12.12} {conflict.path} :: {conflict.message}"
            for conflict in conflicts
        ]
        self.update(join_lines(lines))


class StartIntentPanel(Static):
    def on_mount(self) -> None:
        self.border_title = "Start Agent"

    def set_intent(self, intent: WorktreeStartAgentIntent | None) -> None:
        if intent is None:
            self.update(Text("Press s to preview start-agent intent", style=FG4))
            return
        lines: list[Text] = []
        for label, value in (
            ("worktree", intent.worktree_path),
            ("branch", intent.branch),
            ("session", intent.suggested_session_name),
            ("window", intent.suggested_window_name),
            ("model", intent.model or "-"),
            ("prompt", intent.prompt),
        ):
            line = Text()
            line.append(f"{label:<9}", style=FG4)
            line.append(str(value), style=FG)
            lines.append(line)
        result = Text()
        for i, line in enumerate(lines):
            if i:
                result.append("\n")
            result.append_text(line)
        self.update(result)


__all__ = [
    "ConflictPanel",
    "StartIntentPanel",
    "WorktreeDetailPanel",
    "WorktreeListPanel",
]
