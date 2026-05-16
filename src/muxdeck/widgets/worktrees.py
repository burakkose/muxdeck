from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from rich.text import Text
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from muxdeck.controllers import (
    WorktreeConflictView,
    WorktreeDetailView,
    WorktreeProvenanceKind,
    WorktreeProvenanceView,
    WorktreeStartAgentIntent,
    WorktreeSummaryView,
)
from muxdeck.theme import (
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
    """Render a clean section heading without trailing rules."""
    text.append(title.upper(), style=f"bold {FG3}")
    text.append("\n")


def _field_row(
    text: Text,
    label: str,
    value: str | None,
    style: str,
) -> None:
    """Render a single labeled field row. Skip if value is empty."""
    if not value or value == "-":
        return
    text.append(f"  {label:<9}", style=FG3)
    text.append(f"{value}\n", style=style)


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return value[:limit]
    return value[: limit - 1] + "…"


def _worktree_dir_name(path: str) -> str:
    """Extract the last directory component for disambiguation."""
    return Path(path).name if path else ""


def _change_style(kind: str) -> str:
    return {
        "conflict": f"bold {SEVERITY_ERROR}",
        "untracked": f"bold {AQUA}",
        "mixed": f"bold {ORANGE}",
        "staged": f"bold {GREEN}",
        "unstaged": f"bold {YELLOW}",
    }.get(kind, FG2)


def _provenance_icon(provenance: WorktreeProvenanceView) -> str:
    return "⚡" if provenance.kind != WorktreeProvenanceKind.SESSION else "◌"


def _provenance_field_label(provenance: WorktreeProvenanceView) -> str:
    return {
        WorktreeProvenanceKind.ASSIGNED: "owner",
        WorktreeProvenanceKind.LIVE_AGENT: "agent",
        WorktreeProvenanceKind.SESSION: "recent",
    }[provenance.kind]


def _provenance_value(provenance: WorktreeProvenanceView) -> str:
    if provenance.agent_name and provenance.agent_name != provenance.agent_id:
        return f"{provenance.agent_name} ({provenance.agent_id})"
    return provenance.label


class WorktreeListPanel(Static, can_focus=True):
    """Worktree list with clear selection indicator and readable names."""

    class WorktreeSelected(Message):
        def __init__(self, worktree_id: str) -> None:
            super().__init__()
            self.worktree_id = worktree_id

    def __init__(
        self,
        *,
        widget_id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=widget_id, classes=classes)
        self._worktrees: tuple[WorktreeSummaryView, ...] = ()
        self._selected_index = 0
        # Pre-rendered per-row Text variants (unselected, selected)
        # keyed by item index. Rebuilt only when ``set_worktrees`` runs;
        # cursor moves just pick the right variant per row, so the
        # rich.text.Text constructor + styled appends below don't run
        # on every j/k keystroke.
        self._row_cache: tuple[tuple[Text, Text], ...] = ()

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
        self._rebuild_row_cache()
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
            (i for i, wt in enumerate(self._worktrees) if wt.worktree_id == selected_worktree_id),
            min(self._selected_index, len(self._worktrees) - 1),
        )
        self._selected_index = selected_index
        self._refresh_list()
        if notify:
            self._post_selection(self._selected_index)

    def move_cursor(self, delta: int) -> str | None:
        if not self._worktrees:
            return None
        new_idx = self._selected_index + delta
        self._selected_index = max(
            0,
            min(len(self._worktrees) - 1, new_idx),
        )
        self.focus()
        self._refresh_list()
        self._post_selection(self._selected_index)
        return self.selected_worktree_id

    def focus_list(self) -> None:
        self.focus()

    @property
    def selected_worktree_id(self) -> str | None:
        if not self._worktrees:
            return None
        return self._worktrees[self._selected_index].worktree_id

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
        viewport_widget = parent if isinstance(parent, Widget) else self
        viewport = viewport_widget.size.height - 2
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
        win_start, win_end = self._visible_window()

        # Scroll position indicator at top
        if win_start > 0:
            result.append(f"  ↑ {win_start} more\n", style=FG4)

        for index in range(win_start, win_end):
            variants = self._row_cache[index]
            row_text = variants[1] if index == self._selected_index else variants[0]
            result.append_text(row_text)

        # Scroll position indicator at bottom
        remaining = len(self._worktrees) - win_end
        if remaining > 0:
            result.append(f"  ↓ {remaining} more\n", style=FG4)

        return result

    def _rebuild_row_cache(self) -> None:
        """Build per-row Text in both selection variants once per data refresh.

        Cursor moves only swap which variant the visible window picks
        — they never re-execute the styled-append loop below. Branch
        count disambiguation is also computed once and reused.
        """
        if not self._worktrees:
            self._row_cache = ()
            return
        branch_counts: dict[str, int] = {}
        for wt in self._worktrees:
            branch_counts[wt.branch] = branch_counts.get(wt.branch, 0) + 1
        cache: list[tuple[Text, Text]] = []
        for wt in self._worktrees:
            cache.append(
                (
                    self._build_row_text(wt, is_selected=False, branch_counts=branch_counts),
                    self._build_row_text(wt, is_selected=True, branch_counts=branch_counts),
                )
            )
        self._row_cache = tuple(cache)

    def _build_row_text(
        self,
        wt: WorktreeSummaryView,
        *,
        is_selected: bool,
        branch_counts: dict[str, int],
    ) -> Text:
        row = Text()
        row_bg = f" on {SELECTED_ROW_BG}" if is_selected else ""

        # Selection indicator
        if is_selected:
            row.append(" ▎ ", style=f"bold {BLUE}{row_bg}")
        else:
            row.append("   ", style=row_bg)

        # Main/star indicator
        if wt.is_main_worktree:
            row.append("★ ", style=f"bold {FG}{row_bg}")
        else:
            row.append("  ", style=row_bg)

        # Branch name — full, not truncated
        branch = wt.branch
        branch_style = f"bold {FG}{row_bg}" if is_selected else f"{FG2}{row_bg}"
        row.append(branch, style=branch_style)

        # Disambiguate duplicate branch names with dir name
        if branch_counts.get(branch, 0) > 1:
            dir_name = _worktree_dir_name(wt.path)
            if dir_name:
                row.append(f" ({dir_name})", style=f"{FG4}{row_bg}")

        # Status indicators on same line. Dirty is the only state
        # indicator that earns colour (orange = needs review). The
        # provenance/agent label is metadata about *who* owns the
        # worktree, so it sits in the gray family.
        indicators: list[tuple[str, str]] = []
        if wt.is_dirty:
            indicators.append(("D", f"bold {ORANGE}"))
        if wt.provenance is not None:
            indicators.append(
                (
                    f"{_provenance_icon(wt.provenance)}{wt.provenance.label}",
                    FG2,
                ),
            )

        if indicators:
            row.append("  ", style=row_bg)
            for label, ind_style in indicators:
                row.append(label, style=f"{ind_style}{row_bg}")
                row.append(" ", style=row_bg)

        row.append("\n")
        return row


class WorktreeDetailPanel(Static):
    """Worktree detail with section headers and grouped fields."""

    def set_pending(self, summary: WorktreeSummaryView) -> None:
        """Render a partial view from the summary while detail loads.

        Selection changes on WSL Windows-stamped worktrees can take
        several seconds to enrich because ``get_worktree_detail`` shells
        out to git multiple times. Showing the new selection's branch
        and path immediately — rather than leaving the previous
        worktree's full detail on screen — confirms to the operator
        that their navigation registered. The expensive fields
        (status, ahead/behind, recent commits, change list) appear as
        ``loading…`` placeholders so the layout stays stable.
        """
        result = Text()
        _section_header(result, "worktree detail")

        glyph = "★ " if summary.is_main_worktree else "  "
        result.append("  ")
        result.append("│ ", style=f"bold {FG4}")
        result.append(glyph, style=f"bold {FG}" if summary.is_main_worktree else FG4)
        result.append(summary.branch, style=f"bold {FG}")
        result.append("   ")
        result.append("LOADING…", style=f"bold {FG4}")
        result.append("\n  ")
        result.append("│ ", style=f"bold {FG4}")
        result.append(summary.path, style=FG2)
        result.append("\n\n")

        _field_row(result, "repo", str(summary.repo_root), FG2)
        _field_row(result, "branch", summary.branch, FG2)
        result.append("\n")
        result.append("  loading detail…\n", style=FG4)
        self.update(result)

    def set_detail(self, detail: WorktreeDetailView | None) -> None:
        result = Text()
        _section_header(result, "worktree detail")
        if detail is None:
            result.append("  no worktree selected\n", style=FG4)
            self.update(result)
            return

        summary = detail.summary

        # ── dominant status block ──
        # Match the dashboard detail banner so the operator can see at a
        # glance which branch is selected, what state it is in, and where
        # it lives. Severity-coloured left bar mirrors the alert panel
        # convention so worktree health reads as ops state, not metadata.
        # Precedence is conflicts > dirty > locked > clean (conflicts
        # block work, dirty is the operator's most common next decision,
        # locked is usually transient). Secondary flags ride along as
        # ``+FLAG`` chips so a dirty-AND-locked worktree still surfaces
        # both states.
        bar_style: str
        primary_label: str
        primary_color: str
        if detail.conflicts:
            bar_style = f"bold {SEVERITY_ERROR}"
            primary_label = "CONFLICTS"
            primary_color = SEVERITY_ERROR
        elif summary.is_dirty:
            bar_style = f"bold {ORANGE}"
            primary_label = "DIRTY"
            primary_color = ORANGE
        elif summary.locked:
            bar_style = f"bold {YELLOW}"
            primary_label = "LOCKED"
            primary_color = YELLOW
        else:
            bar_style = f"bold {GREEN}"
            primary_label = "CLEAN"
            primary_color = GREEN

        glyph = "★ " if summary.is_main_worktree else "  "
        result.append("  ")
        result.append("│ ", style=bar_style)
        # The star marks the canonical worktree, not a state. Keep it
        # in primary text so GREEN remains reserved for "healthy".
        result.append(glyph, style=f"bold {FG}" if summary.is_main_worktree else FG4)
        result.append(summary.branch, style=f"bold {FG}")
        result.append("   ")
        result.append(primary_label, style=f"bold {primary_color}")
        # Secondary flags. Skip the one we already used as the primary
        # label so we don't render ``DIRTY +DIRTY``.
        secondaries: list[tuple[str, str]] = []
        if detail.conflicts and primary_label != "CONFLICTS":
            secondaries.append(("CONFLICTS", SEVERITY_ERROR))
        if summary.is_dirty and primary_label != "DIRTY":
            secondaries.append(("DIRTY", ORANGE))
        if summary.locked and primary_label != "LOCKED":
            secondaries.append(("LOCKED", YELLOW))
        for label, color in secondaries:
            result.append("  +", style=FG4)
            result.append(label, style=f"bold {color}")
        result.append("\n")

        # subtitle: tracking · ahead/behind. Ahead/behind are metadata
        # counters, not state — keep them in the gray family so colour
        # stays reserved for the primary status label above.
        result.append("  ")
        result.append("│  ", style=bar_style)
        if detail.branch_status:
            result.append(detail.branch_status, style=FG2)
        else:
            result.append("no upstream tracking", style=FG4)
        ahead = summary.ahead_count or 0
        behind = summary.behind_count or 0
        if ahead:
            result.append("  ·  ", style=FG4)
            result.append(f"ahead {ahead}", style=FG2)
        if behind:
            result.append("  ·  ", style=FG4)
            result.append(f"behind {behind}", style=FG2)
        result.append("\n")

        # path
        result.append("  ")
        result.append("│  ", style=bar_style)
        result.append(summary.path, style=FG2)
        result.append("\n")
        result.append("\n")

        # ── git info ──
        # Banner already carries branch / tracking / ahead / behind, so
        # this section now only renders fields that aren't in the banner.
        _field_row(result, "repo", summary.repo_root, FG2)
        _field_row(result, "base", summary.base_branch, FG2)
        _field_row(result, "changes", detail.change_summary, FG2)

        # ── agent assignment ──
        # Provenance is metadata ("who owns this worktree?"), not a
        # state. Keep it in the gray family so colour stays meaningful.
        if summary.provenance is not None:
            result.append("\n")
            _field_row(
                result,
                _provenance_field_label(summary.provenance),
                _provenance_value(summary.provenance),
                FG,
            )
        _field_row(
            result,
            "sessions",
            str(summary.active_session_count) if summary.active_session_count else None,
            FG2,
        )
        _field_row(
            result,
            "panes",
            ", ".join(detail.pane_targets) if detail.pane_targets else None,
            FG2,
        )

        if detail.status_entries:
            result.append("\n")
            result.append("  changes\n", style=f"bold {FG3}")
            visible_changes = detail.status_entries[:6]
            for change in visible_changes:
                result.append("  ")
                result.append(f"{change.code:<2}", style=_change_style(change.kind))
                result.append("  ", style=FG4)
                result.append(f"{_truncate(change.path, 56)}\n", style=FG2)
            remaining_changes = len(detail.status_entries) - len(visible_changes)
            if remaining_changes > 0:
                result.append(f"  +{remaining_changes} more\n", style=FG4)

        if detail.recent_commits:
            result.append("\n")
            result.append("  recent commits\n", style=f"bold {FG3}")
            for commit in detail.recent_commits[:5]:
                result.append("  ")
                # Commit hashes are identifiers, not actions — keep them
                # in a quiet bold so the recent-commits block does not
                # compete with the primary launch chip.
                result.append(commit.short_sha, style=f"bold {FG2}")
                result.append("  ")
                result.append(_truncate(commit.subject, 42), style=FG1)
                result.append("  ")
                result.append(commit.relative_date, style=FG4)
                result.append("\n")

        result.append("\n  press ", style=FG4)
        result.append("g", style=f"bold {BLUE}")
        result.append(" to open a git terminal here\n", style=FG4)

        self.update(result)


class ConflictPanel(Static):
    """Worktree conflicts — shows warnings when present."""

    def set_pending(self) -> None:
        """Render a transient placeholder while detail loads.

        Selection changes on WSL Windows-stamped worktrees trigger
        several git.exe calls that can take seconds. Without this
        placeholder the previous worktree's conflicts stay on screen
        and the operator can't tell whether their navigation
        registered.
        """
        result = Text()
        _section_header(result, "conflicts")
        result.append("  checking…\n", style=FG4)
        self.update(result)

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
    """Launch defaults for the selected worktree."""

    def set_pending(self) -> None:
        """Render a transient placeholder while launch defaults load.

        Same rationale as ``ConflictPanel.set_pending``: keeps the
        operator from staring at the previous worktree's defaults
        while the per-selection worker is in flight.
        """
        result = Text()
        _section_header(result, "launch agent")
        result.append("  preparing launch defaults…\n", style=FG4)
        self.update(result)

    def set_intent(self, intent: WorktreeStartAgentIntent | None) -> None:
        result = Text()
        _section_header(result, "launch agent")
        if intent is None:
            result.append("  select a worktree to launch an agent\n", style=FG4)
            self.update(result)
            return

        # Primary action chip — launching the agent IS the operational
        # purpose of this panel. Round-7 promotes it to the top so the
        # operator does not have to scan the field rows to find it.
        # Mirrors the dashboard's ``▸ key label   primary`` convention.
        result.append("  ")
        result.append("▸ ", style=f"bold {AQUA}")
        result.append("s", style=f"bold {AQUA}")
        result.append(" launch agent", style=f"bold {FG}")
        result.append("   primary", style=FG4)
        result.append("\n\n")

        for label, value in (
            ("worktree", intent.worktree_path),
            ("branch", intent.branch),
            ("session", intent.suggested_session_name),
            ("window", intent.suggested_window_name),
            ("model", intent.model or "-"),
            ("prompt", intent.prompt),
        ):
            _field_row(result, label, str(value), FG2)
        result.append("\n  also: ", style=FG4)
        result.append("x", style=f"bold {BLUE}")
        result.append(" / ", style=FG4)
        result.append("↵", style=f"bold {BLUE}")
        result.append(" open settings to attach an existing worktree\n", style=FG4)
        self.update(result)


__all__ = [
    "ConflictPanel",
    "StartIntentPanel",
    "WorktreeDetailPanel",
    "WorktreeListPanel",
]
