from __future__ import annotations

from collections.abc import Sequence

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
from copilot_commander.widgets.common import format_bool, join_lines


class WorktreeListPanel(Vertical):
    class WorktreeSelected(Message):
        def __init__(self, worktree_id: str) -> None:
            super().__init__()
            self.worktree_id = worktree_id

    def __init__(self, *, widget_id: str | None = None) -> None:
        super().__init__(id=widget_id)
        self._worktree_ids: list[str] = []

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
        selected_index = 0
        for index, worktree in enumerate(worktrees):
            prefix = "*" if worktree.is_main_worktree else " "
            dirty = "DIRTY" if worktree.is_dirty else "clean"
            assigned = worktree.assigned_agent_name or "unassigned"
            row = (
                f"{prefix} {worktree.branch:<20.20} {dirty:<5} "
                f"ctx {worktree.context_count:<2} agent {assigned}"
            )
            list_view.append(ListItem(Static(row, markup=False)))
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
    def set_detail(self, detail: WorktreeDetailView | None) -> None:
        if detail is None:
            self.update("No worktree selected.")
            return
        summary = detail.summary
        lines = (
            f"BRANCH      {summary.branch}",
            f"PATH        {summary.path}",
            f"REPO        {summary.repo_root}",
            f"BASE        {summary.base_branch or '-'}",
            f"MAIN        {format_bool(summary.is_main_worktree)}",
            f"DIRTY       {format_bool(summary.is_dirty)}",
            f"AHEAD       {summary.ahead_count if summary.ahead_count is not None else '-'}",
            f"BEHIND      {summary.behind_count if summary.behind_count is not None else '-'}",
            f"LOCKED      {format_bool(summary.locked)}",
            f"ASSIGNED    {summary.assigned_agent_name or summary.assigned_agent_id or '-'}",
            f"SESSIONS    {summary.active_session_count}",
            f"CONTEXTS    {summary.context_count}",
            f"PANES       {', '.join(detail.pane_targets) if detail.pane_targets else '-'}",
        )
        self.update(join_lines(lines))


class ConflictPanel(Static):
    def set_conflicts(self, conflicts: Sequence[WorktreeConflictView]) -> None:
        if not conflicts:
            self.update("No worktree conflicts.")
            return
        lines = [
            f"{conflict.code:<14.14} {conflict.path} :: {conflict.message}"
            for conflict in conflicts
        ]
        self.update(join_lines(lines))


class StartIntentPanel(Static):
    def set_intent(self, intent: WorktreeStartAgentIntent | None) -> None:
        if intent is None:
            self.update("Press s to preview a start-agent intent.")
            return
        lines = (
            f"WORKTREE    {intent.worktree_path}",
            f"BRANCH      {intent.branch}",
            f"SESSION     {intent.suggested_session_name}",
            f"WINDOW      {intent.suggested_window_name}",
            f"MODEL       {intent.model or '-'}",
            f"PROMPT      {intent.prompt}",
        )
        self.update(join_lines(lines))


__all__ = [
    "ConflictPanel",
    "StartIntentPanel",
    "WorktreeDetailPanel",
    "WorktreeListPanel",
]
