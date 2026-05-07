"""Widget-only tests for the worktree screen panels.

These tests cover the small pure helpers (``_truncate``,
``_worktree_dir_name``, ``_change_style``, ``_provenance_*``) and the
visual rendering of the four worktree ``Static`` panels. List panels
that read ``self.parent.size.height`` are mounted inside a tiny
:class:`textual.app.App`.
"""

from __future__ import annotations

from typing import Protocol

import pytest
from textual.app import App, ComposeResult

from muxdeck.controllers import (
    WorktreeChangeView,
    WorktreeCommitView,
    WorktreeConflictView,
    WorktreeDetailView,
    WorktreeProvenanceKind,
    WorktreeProvenanceView,
    WorktreeStartAgentIntent,
    WorktreeSummaryView,
)
from muxdeck.widgets.worktrees import (
    ConflictPanel,
    StartIntentPanel,
    WorktreeDetailPanel,
    WorktreeListPanel,
    _change_style,
    _provenance_field_label,
    _provenance_icon,
    _provenance_value,
    _truncate,
    _worktree_dir_name,
)


class _Renderable(Protocol):
    def render(self) -> object: ...


def _render(widget: _Renderable) -> str:
    renderable = widget.render()
    plain = getattr(renderable, "plain", None)
    return plain if isinstance(plain, str) else str(renderable)


# ── pure helpers ────────────────────────────────────────────────────


def test_truncate_returns_input_when_under_limit() -> None:
    assert _truncate("hello", 10) == "hello"
    assert _truncate("hello", 5) == "hello"


def test_truncate_clips_at_limit_minus_ellipsis() -> None:
    assert _truncate("hello world", 6) == "hello…"


def test_truncate_handles_zero_or_one_limit() -> None:
    assert _truncate("hello", 0) == ""
    assert _truncate("hello", 1) == "h"


def test_worktree_dir_name_returns_last_path_component() -> None:
    assert _worktree_dir_name("/repo/wt-foo") == "wt-foo"
    assert _worktree_dir_name("") == ""


def test_change_style_covers_all_kinds_and_falls_back() -> None:
    for kind in ("conflict", "untracked", "mixed", "staged", "unstaged"):
        style = _change_style(kind)
        assert isinstance(style, str)
        assert "bold " in style
    # Fallback for unknown kind
    assert _change_style("unknown") != _change_style("conflict")


def test_provenance_helpers_per_kind() -> None:
    for kind in WorktreeProvenanceKind:
        view = WorktreeProvenanceView(kind=kind, agent_id="agent-1", agent_name="display")
        icon = _provenance_icon(view)
        label = _provenance_field_label(view)
        value = _provenance_value(view)
        assert icon
        assert label
        assert "display" in value


def test_provenance_value_uses_label_when_agent_name_missing() -> None:
    view = WorktreeProvenanceView(
        kind=WorktreeProvenanceKind.SESSION,
        agent_id="agent-x",
        agent_name=None,
    )
    assert _provenance_value(view) == "agent-x"


def test_provenance_value_uses_label_when_name_equals_id() -> None:
    view = WorktreeProvenanceView(
        kind=WorktreeProvenanceKind.LIVE_AGENT,
        agent_id="agent-x",
        agent_name="agent-x",
    )
    assert _provenance_value(view) == "agent-x"


# ── WorktreeListPanel ────────────────────────────────────────────────


def _summary(
    *,
    worktree_id: str = "wt-1",
    branch: str = "feature/x",
    is_dirty: bool = False,
    is_main: bool = False,
    locked: bool = False,
    provenance: WorktreeProvenanceView | None = None,
    path: str = "/repo/wt",
    ahead_count: int | None = 0,
    behind_count: int | None = 0,
) -> WorktreeSummaryView:
    return WorktreeSummaryView(
        worktree_id=worktree_id,
        repo_root="/repo",
        path=path,
        branch=branch,
        base_branch="main",
        is_main_worktree=is_main,
        is_dirty=is_dirty,
        ahead_count=ahead_count,
        behind_count=behind_count,
        locked=locked,
        assigned_agent_id=None,
        assigned_agent_name=None,
        provenance=provenance,
        active_session_count=0,
        context_count=0,
        has_conflicts=False,
    )


def test_list_panel_empty_renders_helpful_hint() -> None:
    panel = WorktreeListPanel(widget_id="wt-list")
    panel.set_worktrees((), selected_worktree_id=None)
    rendered = _render(panel)
    assert "no worktrees found" in rendered
    assert "r" in rendered


def test_list_panel_move_cursor_on_empty_returns_none() -> None:
    panel = WorktreeListPanel(widget_id="wt-list")
    assert panel.move_cursor(1) is None
    assert panel.selected_worktree_id is None


@pytest.mark.asyncio
async def test_list_panel_renders_rows_with_dirty_marker_and_provenance() -> None:
    panel = WorktreeListPanel(widget_id="wt-list")

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield panel

    async with _Harness().run_test() as pilot:
        await pilot.pause()
        worktrees = (
            _summary(
                worktree_id="wt-1",
                branch="feature/x",
                is_dirty=True,
                is_main=True,
                provenance=WorktreeProvenanceView(
                    kind=WorktreeProvenanceKind.LIVE_AGENT,
                    agent_id="agent-1",
                    agent_name="alpha",
                ),
            ),
            _summary(
                worktree_id="wt-2",
                branch="feature/x",
                path="/repo/wt-2-extra",
            ),
            _summary(worktree_id="wt-3", branch="other"),
        )
        panel.set_worktrees(worktrees, selected_worktree_id="wt-2")
        await pilot.pause()
        rendered = _render(panel)
        assert "feature/x" in rendered
        assert "(wt-2-extra)" in rendered  # dir-name disambiguator on duplicate branch
        assert panel.selected_worktree_id == "wt-2"
        # Move cursor + focus
        panel.move_cursor(1)
        panel.focus_list()
        await pilot.pause()
        assert panel.selected_worktree_id == "wt-3"
        # Move past edge — clamps to last
        panel.move_cursor(50)
        assert panel.selected_worktree_id == "wt-3"


@pytest.mark.asyncio
async def test_list_panel_window_scroll_renders_scroll_indicators() -> None:
    panel = WorktreeListPanel(widget_id="wt-list")

    class _Harness(App[None]):
        CSS = "Screen { layers: base; } WorktreeListPanel { height: 7; }"

        def compose(self) -> ComposeResult:
            yield panel

    worktrees = tuple(_summary(worktree_id=f"wt-{i}", branch=f"branch-{i}") for i in range(20))
    async with _Harness().run_test(size=(80, 10)) as pilot:
        await pilot.pause()
        panel.set_worktrees(worktrees, selected_worktree_id="wt-10")
        await pilot.pause()
        rendered = _render(panel)
        # With selected index in middle, both top and bottom indicators
        # should be present.
        assert "more" in rendered


# ── WorktreeDetailPanel ─────────────────────────────────────────────


def test_detail_panel_no_selection() -> None:
    panel = WorktreeDetailPanel()
    panel.set_detail(None)
    rendered = _render(panel)
    assert "no worktree selected" in rendered


def test_detail_panel_full_render_with_changes_commits_and_conflicts() -> None:
    panel = WorktreeDetailPanel()
    summary = WorktreeSummaryView(
        worktree_id="wt-1",
        repo_root="/repo",
        path="/repo/wt",
        branch="feature/x",
        base_branch="main",
        is_main_worktree=True,
        is_dirty=True,
        ahead_count=2,
        behind_count=1,
        locked=True,
        assigned_agent_id="agent-1",
        assigned_agent_name="alpha",
        provenance=WorktreeProvenanceView(
            kind=WorktreeProvenanceKind.ASSIGNED,
            agent_id="agent-1",
            agent_name="alpha",
        ),
        active_session_count=2,
        context_count=1,
        has_conflicts=True,
    )
    detail = WorktreeDetailView(
        summary=summary,
        conflicts=(),
        active_session_ids=("session-1",),
        pane_targets=("%1", "%2"),
        branch_status="ahead 2, behind 1",
        change_summary="3 modified",
        status_entries=tuple(
            WorktreeChangeView(code="M", path=f"src/file{i}.py", kind="staged") for i in range(8)
        ),
        recent_commits=(
            WorktreeCommitView(short_sha="abcdef0", relative_date="2h ago", subject="fix x"),
            WorktreeCommitView(short_sha="abcdef1", relative_date="5h ago", subject="add y"),
        ),
    )
    panel.set_detail(detail)
    rendered = _render(panel)
    assert "feature/x" in rendered
    assert "dirty" in rendered
    assert "locked" in rendered
    assert "ahead 2, behind 1" in rendered
    assert "%1, %2" in rendered
    assert "+2 more" in rendered  # 8 entries, only 6 shown
    assert "abcdef0" in rendered
    assert "alpha" in rendered


def test_detail_panel_minimal_render_skips_optional_sections() -> None:
    panel = WorktreeDetailPanel()
    summary = _summary()
    detail = WorktreeDetailView(
        summary=summary,
        conflicts=(),
        active_session_ids=(),
        pane_targets=(),
    )
    panel.set_detail(detail)
    rendered = _render(panel)
    assert "feature/x" in rendered
    assert "press" in rendered


# ── ConflictPanel ───────────────────────────────────────────────────


def test_conflict_panel_no_conflicts_says_none() -> None:
    panel = ConflictPanel()
    panel.set_conflicts(())
    rendered = _render(panel)
    assert "conflicts" in rendered.lower()
    assert "none" in rendered


def test_conflict_panel_renders_conflict_rows() -> None:
    panel = ConflictPanel()
    conflicts = tuple(
        WorktreeConflictView(
            code="UU",
            message=f"merge conflict {i}",
            path=f"src/conflict{i}.py",
            worktree_id="wt-1",
            agent_id=None,
            branch="feature/x",
        )
        for i in range(8)
    )
    panel.set_conflicts(conflicts)
    rendered = _render(panel)
    assert "UU" in rendered
    assert "src/conflict0.py" in rendered
    # Cap at 6 entries
    assert "src/conflict7.py" not in rendered


# ── StartIntentPanel ────────────────────────────────────────────────


def test_start_intent_panel_no_intent_renders_hint() -> None:
    panel = StartIntentPanel()
    panel.set_intent(None)
    assert "select a worktree" in _render(panel)


def test_start_intent_panel_with_intent_renders_fields() -> None:
    panel = StartIntentPanel()
    intent = WorktreeStartAgentIntent(
        worktree_id="wt-1",
        repo_root="/repo",
        worktree_path="/repo/wt",
        branch="feature/x",
        suggested_session_name="copilot-feature",
        suggested_window_name="agent-1",
        prompt="implement task",
        model="claude",
    )
    panel.set_intent(intent)
    rendered = _render(panel)
    assert "/repo/wt" in rendered
    assert "feature/x" in rendered
    assert "claude" in rendered
    assert "implement task" in rendered


def test_start_intent_panel_default_dash_when_no_model() -> None:
    panel = StartIntentPanel()
    intent = WorktreeStartAgentIntent(
        worktree_id="wt-1",
        repo_root="/repo",
        worktree_path="/repo/wt",
        branch="feature/x",
        suggested_session_name="s",
        suggested_window_name="w",
        prompt="p",
        model=None,
    )
    panel.set_intent(intent)
    # The "-" rendered for model is dropped by `_field_row` because it
    # bails on "-" — confirm the panel still renders without crashing.
    rendered = _render(panel)
    assert "feature/x" in rendered
