from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from rich.text import Text
from textual.message import Message
from textual.widgets import Static

from copilot_commander.controllers import (
    WorktreeConflictView,
    WorktreeDetailView,
    WorktreeStartAgentIntent,
    WorktreeSummaryView,
)
from copilot_commander.theme import (
    AQUA,
    BLUE,
    FG,
    FG1,
    FG2,
    FG3,
    FG4,
    GREEN,
    ORANGE,
    SELECTED_ROW_BG,
    SEVERITY_ERROR,
    YELLOW,
)


def _section_header(text: Text, title: str) -> None:
    """Render a clean section header with box-drawing decoration."""
    text.append(" ── ", style=FG4)
    text.append(title.upper(), style=f"bold {FG3}")
    text.append(" ──────────────────────────────────────\n", style=FG4)


def _field_row(
    text: Text, label: str, value: str | None, style: str,
) -> None:
    """Render a single labeled field row. Skip if value is empty."""
    if not value or value == "-":
        return
    text.append(f"  {label:<9}", style=FG4)
    text.append(f"{value}\n", style=style)


def _worktree_dir_name(path: str) -> str:
    """Extract the last directory component for disambiguation."""
    return Path(path).name if path else ""


class WorktreeListPanel(Static, can_focus=True):
    """Worktree list with clear selection indicator and readable names."""

    class WorktreeSelected(Message):
        def __init__(self, worktree_id: str) -> None:
            super().__init__()
            self.worktree_id = worktree_id

    def __init__(
        self, *, widget_id: str | None = None, classes: str | None = None,
    ) -> None:
        super().__init__(id=widget_id, classes=classes)
        self._worktrees: tuple[WorktreeSummaryView, ...] = ()
        self._selected_index = 0

    def on_mount(self) -> None:
        pass

    def set_worktrees(
        self,
        worktrees: Sequence[WorktreeSummaryView],
        *,
        selected_worktree_id: str | None,
        notify: bool = True,
    ) -> None:
        self._worktrees = tuple(worktrees)
        if not self._worktrees:
            self._selected_index = 0
            empty = Text()
            empty.append("\n  no worktrees found\n", style=f"bold {FG3}")
            empty.append("  press ", style=FG4)
            empty.append("r", style=f"bold {BLUE}")
            empty.append(" to refresh", style=FG4)
            self.update(empty)
            return
        selected_index = next(
            (
                i
                for i, wt in enumerate(self._worktrees)
                if wt.worktree_id == selected_worktree_id
            ),
            min(self._selected_index, len(self._worktrees) - 1),
        )
        self._selected_index = selected_index
        self._refresh_list()
        if notify:
            self._post_selection(self._selected_index)

    def move_cursor(self, delta: int) -> None:
        if not self._worktrees:
            return
        new_idx = self._selected_index + delta
        self._selected_index = max(
            0, min(len(self._worktrees) - 1, new_idx),
        )
        self.focus()
        self._refresh_list()
        self._post_selection(self._selected_index)

    def focus_list(self) -> None:
        self.focus()

    def _post_selection(self, index: int | None) -> None:
        if index is None or index >= len(self._worktrees):
            return
        self.post_message(
            self.WorktreeSelected(self._worktrees[index].worktree_id),
        )

    def _refresh_list(self) -> None:
        self.update(self._build_list())

    def _visible_window(self) -> tuple[int, int]:
        """Compute the slice of items to render, keeping selected in view."""
        # Use the parent container's height as the viewport, since
        # Static widget's own height equals content height (auto).
        parent = self.parent
        viewport = (parent.size.height if parent else self.size.height) - 2
        visible = max(viewport, 5)
        total = len(self._worktrees)
        if total <= visible:
            return 0, total
        # Keep selected item roughly centered, clamped to edges
        half = visible // 2
        start = self._selected_index - half
        start = max(0, min(start, total - visible))
        return start, start + visible

    def _build_list(self) -> Text:
        result = Text()
        # Count branch name occurrences for disambiguation
        branch_counts: dict[str, int] = {}
        for wt in self._worktrees:
            branch_counts[wt.branch] = branch_counts.get(wt.branch, 0) + 1

        win_start, win_end = self._visible_window()

        # Scroll position indicator at top
        if win_start > 0:
            result.append(f"  ↑ {win_start} more\n", style=FG4)

        for index in range(win_start, win_end):
            wt = self._worktrees[index]
            is_selected = index == self._selected_index
            row_bg = f" on {SELECTED_ROW_BG}" if is_selected else ""

            # Selection indicator
            if is_selected:
                result.append(" ▎ ", style=f"bold {BLUE}{row_bg}")
            else:
                result.append("   ", style=row_bg)

            # Main/star indicator
            if wt.is_main_worktree:
                result.append("★ ", style=f"bold {GREEN}{row_bg}")
            else:
                result.append("  ", style=row_bg)

            # Branch name — full, not truncated
            branch = wt.branch
            branch_style = (
                f"bold {FG}{row_bg}" if is_selected else f"{FG2}{row_bg}"
            )
            result.append(branch, style=branch_style)

            # Disambiguate duplicate branch names with dir name
            if branch_counts.get(branch, 0) > 1:
                dir_name = _worktree_dir_name(wt.path)
                if dir_name:
                    result.append(f" ({dir_name})", style=f"{FG4}{row_bg}")

            # Status indicators on same line
            indicators: list[tuple[str, str]] = []
            if wt.is_dirty:
                indicators.append(("D", f"bold {ORANGE}"))
            if wt.assigned_agent_name:
                indicators.append(
                    (f"⚡{wt.assigned_agent_name}", f"bold {AQUA}"),
                )

            if indicators:
                result.append("  ", style=row_bg)
                for label, ind_style in indicators:
                    result.append(label, style=f"{ind_style}{row_bg}")
                    result.append(" ", style=row_bg)

            result.append("\n")

        # Scroll position indicator at bottom
        remaining = len(self._worktrees) - win_end
        if remaining > 0:
            result.append(f"  ↓ {remaining} more\n", style=FG4)

        return result


class WorktreeDetailPanel(Static):
    """Worktree detail with section headers and grouped fields."""

    def set_detail(self, detail: WorktreeDetailView | None) -> None:
        result = Text()
        _section_header(result, "worktree detail")
        if detail is None:
            result.append("  no worktree selected\n", style=FG4)
            self.update(result)
            return

        summary = detail.summary

        # ── header: branch + status ──
        if summary.is_main_worktree:
            result.append("  ★ ", style=f"bold {GREEN}")
        else:
            result.append("    ")
        result.append(summary.branch, style=f"bold {FG}")
        if summary.is_dirty:
            result.append("  dirty", style=f"bold {ORANGE}")
        if summary.locked:
            result.append("  locked", style=f"bold {YELLOW}")
        result.append("\n")

        # ── path (shortened) ──
        result.append(f"  {summary.path}\n", style=FG4)
        result.append("\n")

        # ── git info ──
        _field_row(result, "repo", summary.repo_root, FG2)
        _field_row(result, "base", summary.base_branch, FG1)
        if summary.ahead_count is not None and summary.ahead_count > 0:
            _field_row(
                result, "ahead",
                str(summary.ahead_count), GREEN,
            )
        if summary.behind_count is not None and summary.behind_count > 0:
            _field_row(
                result, "behind",
                str(summary.behind_count), ORANGE,
            )

        # ── agent assignment ──
        agent_name = summary.assigned_agent_name or summary.assigned_agent_id
        if agent_name:
            result.append("\n")
            _field_row(result, "agent", agent_name, f"bold {AQUA}")
        _field_row(
            result, "sessions",
            str(summary.active_session_count)
            if summary.active_session_count
            else None,
            FG1,
        )
        _field_row(
            result, "panes",
            ", ".join(detail.pane_targets) if detail.pane_targets else None,
            BLUE,
        )

        self.update(result)


class ConflictPanel(Static):
    """Worktree conflicts — shows warnings when present."""

    def set_conflicts(self, conflicts: Sequence[WorktreeConflictView]) -> None:
        result = Text()
        _section_header(result, "conflicts")
        if not conflicts:
            result.append("  none\n", style=FG4)
            self.update(result)
            return
        for conflict in conflicts[:6]:
            result.append("  ")
            result.append(conflict.code, style=f"bold {SEVERITY_ERROR}")
            result.append(f"  {conflict.path}", style=FG1)
            result.append(f"  {conflict.message}\n", style=FG3)
        self.update(result)


class StartIntentPanel(Static):
    """Start agent intent preview."""

    def set_intent(self, intent: WorktreeStartAgentIntent | None) -> None:
        result = Text()
        _section_header(result, "start agent")
        if intent is None:
            result.append("  press ", style=FG4)
            result.append("s", style=f"bold {BLUE}")
            result.append(" to preview start intent\n", style=FG4)
            self.update(result)
            return
        for label, value in (
            ("worktree", intent.worktree_path),
            ("branch", intent.branch),
            ("session", intent.suggested_session_name),
            ("window", intent.suggested_window_name),
            ("model", intent.model or "-"),
            ("prompt", intent.prompt),
        ):
            _field_row(result, label, str(value), FG1)
        result.append("\n  press ", style=FG4)
        result.append("x", style=f"bold {BLUE}")
        result.append(" to execute\n", style=FG4)
        self.update(result)


__all__ = [
    "ConflictPanel",
    "StartIntentPanel",
    "WorktreeDetailPanel",
    "WorktreeListPanel",
]
