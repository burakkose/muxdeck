from __future__ import annotations

from collections.abc import Sequence

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static

from muxdeck.controllers.fleet_controller import (
    FleetAgentSummaryView,
    FleetHistoryMetricView,
    FleetInboxItemView,
    FleetLocalSessionView,
    FleetRecentActivityView,
    FleetRepoGroupView,
    FleetSearchHelperView,
    FleetSearchHitView,
    FleetState,
    FleetStoryLaneView,
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
    RED,
    SELECTED_ROW_BG,
    YELLOW,
)
from muxdeck.ui_preferences import UiDensity, UiPreferences, resolve_ui_preferences
from muxdeck.widgets.common import (
    format_short_timestamp,
    item_separator,
    pipe_separator,
    status_glyph_char,
    ui_symbol,
)

_SEVERITY_STYLES = {
    "info": FG,
    "warning": f"bold {ORANGE}",
    "error": f"bold {RED}",
}

_TONE_STYLES = {
    "healthy": GREEN,
    "warning": ORANGE,
    "critical": RED,
}


def _append_title(text: Text, title: str) -> None:
    text.append(f" {title}\n", style=f"bold {BLUE}")


def _count_phrase(count: int, singular: str, plural: str | None = None) -> str:
    noun = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {noun}"


def _lane_marker(lane: FleetStoryLaneView, *, preferences: UiPreferences) -> Text:
    if lane.inbox_count or lane.orphan_local_session_count or lane.blocked_count:
        return Text("!", style=f"bold {ORANGE}")
    return Text(ui_symbol("item-separator", preferences=preferences), style=FG4)


def _lane_scope(lane: FleetStoryLaneView) -> str:
    if not lane.repo_labels:
        return "unassigned"
    if len(lane.repo_labels) == 1:
        return lane.repo_labels[0]
    head = ", ".join(lane.repo_labels[:2])
    if len(lane.repo_labels) > 2:
        return f"{head} +{len(lane.repo_labels) - 2}"
    return head


def _lane_flow(lane: FleetStoryLaneView, *, preferences: UiPreferences) -> str:
    parts = [
        f"{lane.live_agent_count} live",
        f"{lane.open_session_count} open",
    ]
    if lane.orphan_local_session_count:
        parts.append(f"{lane.orphan_local_session_count} orphan")
    return item_separator(preferences).join(parts)


def _lane_queue(lane: FleetStoryLaneView, *, preferences: UiPreferences) -> str:
    parts = [f"{lane.inbox_count} act"]
    if lane.waiting_agent_count:
        parts.append(f"{lane.waiting_agent_count} reply")
    if lane.blocked_count:
        parts.append(f"{lane.blocked_count} blocked")
    return item_separator(preferences).join(parts)


def _local_status_style(session: FleetLocalSessionView) -> str:
    if session.is_orphan:
        return f"bold {ORANGE}"
    if not session.is_cleanly_closed:
        return f"bold {YELLOW}"
    return FG1


def _local_session_detail(session: FleetLocalSessionView, *, preferences: UiPreferences) -> str:
    parts = [
        session.repo_label,
        session.branch,
        session.worktree_name,
        session.origin,
        f"{session.checkpoint_count} ckpt",
    ]
    if session.token_total is not None:
        parts.append(f"{session.token_total} tok")
    if session.last_event_type:
        parts.append(session.last_event_type)
    if session.linked_agent_name:
        parts.append(f"agent {session.linked_agent_name}")
    return item_separator(preferences).join(parts)


def _inbox_summary(items: Sequence[FleetInboxItemView], *, preferences: UiPreferences) -> str:
    reply_count = sum(1 for item in items if item.suggested_action == "reply")
    recover_count = sum(1 for item in items if item.suggested_action == "recover")
    blocked_count = sum(1 for item in items if item.severity == "error")
    parts = [f"{len(items)} pending"]
    if reply_count:
        parts.append(f"{reply_count} reply")
    if blocked_count:
        parts.append(f"{blocked_count} critical")
    if recover_count:
        parts.append(f"{recover_count} recover")
    return item_separator(preferences).join(parts)


class FleetSummaryBar(Static):
    def set_state(self, state: FleetState) -> None:
        preferences = resolve_ui_preferences(self)
        separator = pipe_separator(preferences)
        health = state.health
        line = Text()
        line.append(" fleet command center ", style=f"bold {BLUE}")
        line.append(separator.strip(), style=FG4)
        line.append(f" {_count_phrase(len(state.story_lanes), 'story', 'stories')}", style=FG)
        line.append(f" {separator} ", style=FG4)
        line.append(
            f"{_count_phrase(len(state.response_inbox), 'waiting item', 'waiting items')}",
            style=f"bold {ORANGE}",
        )
        line.append(f" {separator} ", style=FG4)
        line.append(
            _count_phrase(state.total_visible_agents, "live agent", "live agents"), style=FG
        )
        line.append(f" {separator} ", style=FG4)
        line.append(_count_phrase(state.total_groups, "repo", "repos"), style=FG)
        if state.filters.attention_only:
            line.append(f" {separator} ", style=FG4)
            line.append("attention only", style=f"bold {ORANGE}")
        query = state.filters.normalized_query()
        if query is not None:
            line.append(f" {separator} ", style=FG4)
            line.append(f"filter {query}", style=f"bold {AQUA}")
        line.append(f" {separator} ", style=FG4)
        line.append(health.message, style=f"bold {_TONE_STYLES[health.tone]}")
        if health.orphan_local_sessions:
            line.append(f" {separator} ", style=FG4)
            line.append(f"orphans {health.orphan_local_sessions}", style=f"bold {ORANGE}")
        self.update(line)


class FleetStoryLanesPanel(Static, can_focus=True):
    def __init__(self, *, widget_id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=widget_id, classes=classes)
        self._lanes: tuple[FleetStoryLaneView, ...] = ()
        self._cursor_index = 0

    @property
    def current_story_key(self) -> str | None:
        if not self._lanes:
            return None
        return self._lanes[self._cursor_index].story_key

    def compose(self) -> ComposeResult:
        yield Static()

    def set_lanes(
        self,
        lanes: Sequence[FleetStoryLaneView],
        *,
        selected_story_key: str | None,
    ) -> None:
        self._lanes = tuple(lanes)
        if not self._lanes:
            self._cursor_index = 0
            self.update(Text(" No story lanes match the current filter", style=FG4))
            return
        requested_index = next(
            (
                index
                for index, lane in enumerate(self._lanes)
                if lane.story_key == selected_story_key
            ),
            self._cursor_index,
        )
        self._cursor_index = min(requested_index, len(self._lanes) - 1)
        self.update(self._build_table())

    def move_cursor(self, delta: int) -> None:
        if not self._lanes:
            return
        self._cursor_index = max(0, min(len(self._lanes) - 1, self._cursor_index + delta))
        self.update(self._build_table())
        self.focus()

    def focus_list(self) -> None:
        self.focus()

    def _build_table(self) -> Table:
        preferences = resolve_ui_preferences(self)
        comfortable = preferences.density is UiDensity.COMFORTABLE
        table = Table(
            expand=True,
            box=None,
            header_style=f"bold {FG4}",
            border_style=FG4,
            pad_edge=False,
            show_edge=False,
            show_header=True,
            padding=(0, 1, 0, 0),
        )
        table.add_column("", width=1, no_wrap=True)
        table.add_column("Story", ratio=3, overflow="ellipsis")
        table.add_column("Scope", ratio=2, overflow="ellipsis")
        table.add_column("Flow", ratio=2, overflow="ellipsis")
        table.add_column("Queue", ratio=2, overflow="ellipsis")
        table.add_column("Next", ratio=2, overflow="ellipsis")
        for index, lane in enumerate(self._lanes):
            row_style = f"on {SELECTED_ROW_BG}" if index == self._cursor_index else ""
            story_text = Text(
                lane.story_label,
                style=f"bold {FG}" if index == self._cursor_index else FG1,
            )
            if comfortable:
                story_text.append("\n  ", style=FG4)
                story_text.append(_lane_scope(lane), style=FG4)
            next_text = Text(lane.next_action, style=f"bold {AQUA}")
            if comfortable:
                next_text.append("\n", style=FG4)
                next_text.append(_count_phrase(lane.attention_count, "attention"), style=FG4)
            table.add_row(
                _lane_marker(lane, preferences=preferences),
                story_text,
                Text(_lane_scope(lane), style=FG2),
                Text(_lane_flow(lane, preferences=preferences), style=FG2),
                Text(
                    _lane_queue(lane, preferences=preferences),
                    style=f"bold {ORANGE}" if lane.inbox_count else FG4,
                ),
                next_text,
                style=row_style,
            )
        return table


class FleetCommandDeckPanel(Static):
    def set_story(
        self,
        story: FleetStoryLaneView | None,
        *,
        repo_groups: Sequence[FleetRepoGroupView],
        agents: Sequence[FleetAgentSummaryView],
        inbox_items: Sequence[FleetInboxItemView],
    ) -> None:
        preferences = resolve_ui_preferences(self)
        content = Text()
        _append_title(content, "command deck")
        if story is None:
            content.append(
                " select a story lane to inspect live work, queues, and the next recommended move",
                style=FG4,
            )
            self.update(content)
            return
        content.append(f" {story.story_label}\n", style=f"bold {FG}")
        content.append(f" {_lane_scope(story)}\n", style=FG4)
        for label, value, detail, style in (
            (
                "live",
                _count_phrase(story.live_agent_count, "agent", "agents"),
                (
                    f"{story.waiting_agent_count} waiting{item_separator(preferences)}"
                    f"{story.attention_count} attention"
                    f"{item_separator(preferences)}{story.blocked_count} blocked"
                ),
                FG,
            ),
            (
                "scope",
                _count_phrase(len(story.repo_labels), "repo", "repos"),
                (
                    f"{story.open_session_count} open sessions{item_separator(preferences)}"
                    f"{story.local_session_count} local sessions"
                ),
                FG1,
            ),
            (
                "queue",
                _count_phrase(story.inbox_count, "action", "actions"),
                f"next {story.next_action}",
                ORANGE if story.inbox_count else FG1,
            ),
        ):
            content.append(f" {label:<9}", style=FG4)
            content.append(value, style=f"bold {style}")
            content.append(f"  {detail}\n", style=FG3)

        content.append("\n next moves\n", style=f"bold {BLUE}")
        if inbox_items:
            for item in inbox_items[:4]:
                content.append(
                    f" ! {item.suggested_action:<8} ",
                    style=_SEVERITY_STYLES[item.severity],
                )
                content.append(item.source_label, style=f"bold {FG}")
                content.append(f"\n   {item.reason}\n", style=FG3)
        else:
            content.append(" · monitor active work and wait for the next response\n", style=FG4)

        content.append("\n repos in scope\n", style=f"bold {BLUE}")
        if repo_groups:
            for group in repo_groups[:4]:
                content.append(f" {group.repo_label}", style=f"bold {FG1}")
                content.append(
                    (
                        f"{item_separator(preferences)}{group.agent_count} ag"
                        f"{item_separator(preferences)}{group.open_session_count} open"
                        f"{item_separator(preferences)}"
                        f"{group.orphan_local_session_count} orphan\n"
                    ),
                    style=FG3,
                )
        else:
            content.append(" no repo evidence attached to this story lane\n", style=FG4)

        content.append("\n live agents\n", style=f"bold {BLUE}")
        if agents:
            for agent in agents[:4]:
                style = f"bold {ORANGE}" if agent.needs_attention else f"bold {FG1}"
                content.append(
                    f" {status_glyph_char(agent.status, preferences=preferences)} {agent.name}",
                    style=style,
                )
                content.append(
                    (
                        f"{item_separator(preferences)}{agent.status.value.replace('_', ' ')}"
                        f"{item_separator(preferences)}{agent.branch}\n"
                    ),
                    style=FG3,
                )
                content.append(f"   {agent.task_title}\n", style=FG2)
        else:
            content.append(" no visible live agents in this story lane\n", style=FG4)
        self.update(content)


class FleetInboxPanel(Static):
    def set_inbox(
        self,
        *,
        items: Sequence[FleetInboxItemView],
        selected_story_key: str | None,
        selected_story_label: str | None,
    ) -> None:
        preferences = resolve_ui_preferences(self)
        content = Text()
        _append_title(content, "response inbox")
        if not items:
            content.append(" no sessions are waiting on you right now", style=FG4)
            self.update(content)
            return
        content.append(f" {_inbox_summary(items, preferences=preferences)}\n\n", style=FG3)
        ordered_items = sorted(
            items,
            key=lambda item: (
                item.story_key != selected_story_key,
                0 if item.severity == "error" else 1 if item.severity == "warning" else 2,
                -(item.occurred_at.timestamp()),
            ),
        )
        for item in ordered_items[:8]:
            marker = (
                ">"
                if item.story_key == selected_story_key
                else ui_symbol(
                    "item-separator",
                    preferences=preferences,
                )
            )
            content.append(
                f" {marker} {format_short_timestamp(item.occurred_at):<8} ",
                style=FG4,
            )
            content.append(
                f"{item.suggested_action:<8} ",
                style=_SEVERITY_STYLES[item.severity],
            )
            content.append(item.source_label, style=f"bold {FG}")
            if item.story_key != selected_story_key:
                content.append(f"  [{item.story_label}]", style=FG2)
            elif selected_story_label is not None:
                content.append(f"  [{selected_story_label}]", style=f"bold {AQUA}")
            content.append(f"\n   {item.reason}\n", style=FG3)
        self.update(content)


class FleetLocalSessionsPanel(Static):
    def set_sessions(
        self,
        *,
        scope_label: str | None,
        sessions: Sequence[FleetLocalSessionView],
    ) -> None:
        preferences = resolve_ui_preferences(self)
        content = Text()
        _append_title(content, "session state")
        if scope_label is None:
            content.append(
                " select a story lane to inspect linked, orphan, and open local session state",
                style=FG4,
            )
            self.update(content)
            return
        if not sessions:
            content.append(f" no local session drift for {scope_label}", style=FG4)
            self.update(content)
            return
        orphan_count = sum(1 for session in sessions if session.is_orphan)
        open_count = sum(1 for session in sessions if not session.is_cleanly_closed)
        linked_count = len(sessions) - orphan_count
        content.append(
            (
                f" {scope_label} · {orphan_count} orphan · {open_count} open · "
                f"{linked_count} linked\n\n"
            ),
            style=FG3,
        )
        prioritized_sessions = tuple(
            sorted(
                sessions,
                key=lambda session: (
                    not session.is_orphan,
                    session.is_cleanly_closed,
                    session.summary.lower(),
                ),
            )
        )
        for session in prioritized_sessions[:8]:
            timestamp = format_short_timestamp(session.updated_at or session.last_event_at)
            relation = "orphan" if session.is_orphan else "linked"
            state = "closed" if session.is_cleanly_closed else "open"
            marker = (
                "!"
                if session.is_orphan or not session.is_cleanly_closed
                else ui_symbol("item-separator", preferences=preferences)
            )
            content.append(
                f" {marker} {timestamp:<8} ",
                style=_local_status_style(session),
            )
            content.append(
                f"{relation:<7}",
                style=f"bold {ORANGE}" if session.is_orphan else AQUA,
            )
            content.append(" ")
            content.append(state, style=_local_status_style(session))
            content.append(f"  {session.summary}\n", style=f"bold {FG}")
            content.append(
                f"           {_local_session_detail(session, preferences=preferences)}\n",
                style=FG3,
            )
        self.update(content)


class FleetHistoryPanel(Static):
    def set_history(
        self,
        metrics: Sequence[FleetHistoryMetricView],
        recent_activity: Sequence[FleetRecentActivityView],
        *,
        scope_label: str | None = None,
    ) -> None:
        content = Text()
        _append_title(content, "activity")
        content.append(" view posture\n", style=f"bold {BLUE}")
        for metric in metrics:
            content.append(f" {metric.label:<13}", style=FG4)
            content.append(metric.value, style=f"bold {FG}")
            content.append(f"  {metric.detail}\n", style=FG3)
        title = (
            "recent fleet activity"
            if scope_label is None
            else f"recent story activity · {scope_label}"
        )
        content.append(f"\n {title}\n", style=f"bold {BLUE}")
        if not recent_activity:
            content.append(
                " no recent fleet activity"
                if scope_label is None
                else " no recent activity in this story lane",
                style=FG4,
            )
        for item in recent_activity:
            content.append(
                f" {format_short_timestamp(item.occurred_at):<8} ",
                style=FG4,
            )
            if scope_label is None and item.story_label:
                content.append(f"{item.story_label} ", style=FG2)
            content.append(item.title, style=_SEVERITY_STYLES[item.severity])
            content.append(f"  {item.detail}\n", style=FG3)
        self.update(content)


class FleetSearchPanel(Static):
    def set_search(
        self,
        *,
        query: str | None,
        helpers: Sequence[FleetSearchHelperView],
        hits: Sequence[FleetSearchHitView],
    ) -> None:
        preferences = resolve_ui_preferences(self)
        content = Text()
        _append_title(content, "filters / search")
        if helpers:
            for helper in helpers:
                content.append(f" /{helper.query:<12}", style=f"bold {AQUA}")
                content.append(helper.label, style=f"bold {FG}")
                content.append(
                    (
                        f"{item_separator(preferences)}{helper.match_count}"
                        f"{item_separator(preferences)}{helper.detail}\n"
                    ),
                    style=FG3,
                )
        else:
            content.append(" /waiting      find sessions waiting for your reply\n", style=FG3)
            content.append(" /dirty        surface worktree hygiene issues\n", style=FG3)
            content.append(" /unclosed     find orphan or open local sessions\n", style=FG3)
        content.append("\n search\n", style=f"bold {BLUE}")
        if not query:
            content.append(
                " story · agent · repo · branch · session id · local summary",
                style=FG4,
            )
        elif not hits:
            content.append(f" no matches for {query!r}", style=FG4)
        else:
            for hit in hits:
                content.append(f" {hit.kind:<8}", style=FG4)
                content.append(hit.title, style=FG)
                content.append(f"\n          {hit.detail}\n", style=FG3)
        self.update(content)


__all__ = [
    "FleetCommandDeckPanel",
    "FleetHistoryPanel",
    "FleetInboxPanel",
    "FleetLocalSessionsPanel",
    "FleetSearchPanel",
    "FleetStoryLanesPanel",
    "FleetSummaryBar",
]
