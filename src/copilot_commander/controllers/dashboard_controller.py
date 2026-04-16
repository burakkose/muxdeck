from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

from copilot_commander.adapters.sqlite_store import SessionContextRecord
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.events import Event, LogChunk
from copilot_commander.domain.models import Agent, Session, Worktree
from copilot_commander.domain.subagents import SubAgentSnapshot, SubAgentTree
from copilot_commander.domain.value_objects import utc_now
from copilot_commander.parsers.copilot_output_parser import parse_copilot_output
from copilot_commander.perf import timed
from copilot_commander.services.operator_status_service import (
    OperatorStatus,
    describe_operator_status,
)
from copilot_commander.types import Clock

DashboardSortField = Literal["last_seen", "name", "status", "cost", "idle_seconds", "started_at"]
HealthTone = Literal["healthy", "warning", "critical"]
AlertSeverity = Literal["info", "warning", "error"]

_SPARK_CHARS: str = " ▁▂▃▄▅▆▇"
_ACTIVITY_HISTORY_CAP: int = 100
_STALE_THRESHOLD_SECONDS: int = 120

# Module-level state for sparkline activity history and heartbeat staleness.
_activity_history: dict[str, list[datetime]] = {}
_output_hashes: dict[str, tuple[str, datetime]] = {}


class DashboardStorePort(Protocol):
    def list_agents(self) -> Sequence[Agent]: ...

    def list_sessions(self, agent_id: str | None = None, /) -> Sequence[Session]: ...

    def get_latest_session_for_agent(self, agent_id: str, /) -> Session | None: ...

    def count_sessions_for_agent(self, agent_id: str, /) -> int: ...

    def get_open_session_for_agent(self, agent_id: str, /) -> Session | None: ...

    def get_session_context(self, session_id: str, /) -> SessionContextRecord | None: ...

    def list_events_for_session(self, session_id: str, /) -> Sequence[Event]: ...

    def get_latest_event_for_session(self, session_id: str, /) -> Event | None: ...

    def list_log_chunks(self, session_id: str, /) -> Sequence[LogChunk]: ...

    def get_latest_log_chunk(self, session_id: str, /) -> LogChunk | None: ...

    def list_recent_log_chunks(
        self, session_id: str, /, *, limit: int = 20
    ) -> Sequence[LogChunk]: ...

    def get_worktree(self, worktree_id: str, /) -> Worktree | None: ...


@dataclass(frozen=True, slots=True)
class DashboardFilterState:
    statuses: tuple[AgentStatus, ...] = ()
    attention_only: bool = False
    text_query: str | None = None
    include_completed: bool = True

    def normalized_query(self) -> str | None:
        if self.text_query is None:
            return None
        query = self.text_query.strip().lower()
        return query or None


@dataclass(frozen=True, slots=True)
class DashboardSort:
    field: DashboardSortField = "last_seen"
    descending: bool = True


@dataclass(frozen=True, slots=True)
class DashboardMetricView:
    key: str
    label: str
    value: int


@dataclass(frozen=True, slots=True)
class DashboardAlertView:
    agent_id: str
    agent_name: str
    severity: AlertSeverity
    title: str
    message: str
    occurred_at: datetime
    alert_id: str = ""
    operator_status: OperatorStatus | None = None
    is_critical: bool = False


@dataclass(frozen=True, slots=True)
class DashboardHealthSummary:
    tone: HealthTone
    message: str
    total_agents: int
    active_agents: int
    attention_agents: int
    waiting_input_agents: int
    blocked_agents: int
    error_agents: int


@dataclass(frozen=True, slots=True)
class DashboardLogLineView:
    captured_at: datetime
    source: str
    sequence_no: int
    content: str


@dataclass(frozen=True, slots=True)
class DashboardAgentListItemView:
    agent_id: str
    name: str
    status: AgentStatus
    repo_name: str | None
    branch: str | None
    worktree_name: str | None
    pane_id: str
    task_title: str | None
    worktree_path: str | None
    latest_session_id: str | None
    last_event_kind: str | None
    last_log_at: datetime | None
    last_seen_at: datetime
    started_at: datetime
    idle_seconds: int
    needs_attention: bool
    attention_reason: str | None
    token_total: int | None
    estimated_cost_usd: str | None
    window_name: str | None = None
    current_activity: str | None = None
    sparkline: str = "        "
    is_potentially_stuck: bool = False
    operator_status: OperatorStatus | None = None


@dataclass(frozen=True, slots=True)
class DashboardSelectedAgentView:
    item: DashboardAgentListItemView
    repo_root: str | None
    worktree_id: str | None
    session_count: int
    open_session_id: str | None
    copilot_session_id: str | None
    latest_event_kind: str | None
    latest_event_severity: str | None
    latest_event_at: datetime | None
    log_preview: tuple[DashboardLogLineView, ...]
    recent_events: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DashboardSubAgentView:
    """Presentation shape for one sub-agent row under a parent agent.

    ``tool_call_id`` is the stable identity the widget uses to diff
    rows across refreshes. Duration is pre-formatted so the widget
    doesn't have to know about seconds/minutes math.
    """

    tool_call_id: str
    agent_name: str
    display_name: str
    description: str | None
    started_at: datetime
    completed_at: datetime | None
    is_running: bool
    task_name: str | None = None
    agent_type: str | None = None
    prompt: str | None = None
    result_content: str | None = None
    success: bool | None = None


@dataclass(frozen=True, slots=True)
class DashboardSubAgentTreeView:
    """The full expanded view for one agent's sub-agent tree."""

    agent_id: str
    session_id: str | None
    running: tuple[DashboardSubAgentView, ...]
    recent: tuple[DashboardSubAgentView, ...]

    @property
    def is_empty(self) -> bool:
        return not self.running and not self.recent


class SubAgentReaderPort(Protocol):
    """The subset of ``SubAgentReader`` the controller needs."""

    def read(self, session_id: str) -> SubAgentTree | None: ...


class CopilotSessionResolverPort(Protocol):
    """Resolve a tmux pane's pid to a live Copilot session id."""

    def resolve_for_pid(self, pane_pid: int | None) -> str | None: ...


@dataclass(frozen=True, slots=True)
class DashboardState:
    generated_at: datetime
    metrics: tuple[DashboardMetricView, ...]
    filters: DashboardFilterState
    sort: DashboardSort
    health: DashboardHealthSummary
    alerts: tuple[DashboardAlertView, ...]
    agents: tuple[DashboardAgentListItemView, ...]
    selected_agent_id: str | None
    selected_agent: DashboardSelectedAgentView | None


class DashboardController:
    def __init__(
        self,
        store: DashboardStorePort,
        *,
        clock: Clock = utc_now,
        max_cost_usd: Decimal | None = None,
        max_runtime_minutes: int | None = None,
        subagent_reader: SubAgentReaderPort | None = None,
        session_resolver: CopilotSessionResolverPort | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._max_cost_usd = max_cost_usd
        self._max_runtime_minutes = max_runtime_minutes
        self._subagent_reader = subagent_reader
        self._session_resolver = session_resolver

    def load_subagents(self, agent_id: str) -> DashboardSubAgentTreeView:
        """Lazy-load the sub-agent tree for one agent.

        Deliberately *not* called from :meth:`build_state` — the point
        is that rendering the dashboard list stays cheap even when the
        user has dozens of agents, and the parent-events parse only
        runs for rows the operator has explicitly expanded.

        Returns an empty tree (with ``session_id=None``) when the
        agent has no known ``copilot_session_id``, no reader was
        configured, or the session directory can't be resolved to any
        of the reader's roots. The widget uses this shape to render a
        friendly "no sub-agents" placeholder instead of hiding the
        toggle.
        """
        agent = next(
            (a for a in self._store.list_agents() if a.id == agent_id),
            None,
        )
        if agent is None or self._subagent_reader is None:
            return DashboardSubAgentTreeView(
                agent_id=agent_id, session_id=None, running=(), recent=()
            )
        session_id = self._resolve_copilot_session_id(agent)
        if session_id is None:
            return DashboardSubAgentTreeView(
                agent_id=agent_id, session_id=None, running=(), recent=()
            )
        tree = self._subagent_reader.read(session_id)
        if tree is None:
            return DashboardSubAgentTreeView(
                agent_id=agent_id, session_id=session_id, running=(), recent=()
            )
        return DashboardSubAgentTreeView(
            agent_id=agent_id,
            session_id=session_id,
            running=tuple(_to_view(snapshot) for snapshot in tree.running),
            recent=tuple(_to_view(snapshot) for snapshot in tree.recent),
        )

    def _resolve_copilot_session_id(self, agent: Agent) -> str | None:
        if agent.copilot_session_id:
            return agent.copilot_session_id
        # Fall back to the latest session linked to this agent in the
        # sqlite store — some agents only record their copilot session
        # id on the Session row, not the Agent row itself.
        latest = self._store.get_latest_session_for_agent(agent.id)
        if latest is not None and latest.copilot_session_id:
            return latest.copilot_session_id
        # Last resort: scan Copilot's ``inuse.<pid>.lock`` files and
        # match against the pane's pid chain. Agent discovery does not
        # always populate ``copilot_session_id`` (e.g. when the agent
        # was adopted from a tmux pane that was already running), so
        # the resolver fills that gap at read time.
        if self._session_resolver is not None:
            return self._session_resolver.resolve_for_pid(agent.pid)
        return None

    def build_state(
        self,
        *,
        filters: DashboardFilterState | None = None,
        sort: DashboardSort | None = None,
        selected_agent_id: str | None = None,
        preview_line_limit: int = 8,
        alert_limit: int = 5,
    ) -> DashboardState:
        with timed("dashboard.build_state"):
            applied_filters = DashboardFilterState() if filters is None else filters
            applied_sort = DashboardSort() if sort is None else sort
            generated_at = self._clock()
            with timed("dashboard.list_agents"):
                agents = self._store.list_agents()
            with timed("dashboard.build_agent_items"):
                agent_views = tuple(self._build_agent_item(agent) for agent in agents)
            filtered_agents = self._filter_agents(agent_views, applied_filters)
            sorted_agents = self._sort_agents(filtered_agents, applied_sort)
            selected_item = self._select_agent(sorted_agents, selected_agent_id)
            selected_view = None
            if selected_item is not None:
                with timed("dashboard.build_selected_agent"):
                    selected_view = self._build_selected_agent(
                        selected_item,
                        preview_line_limit=preview_line_limit,
                    )
            alerts = self._build_alerts(agent_views, limit=alert_limit)
            metrics = self._build_metrics(agent_views)
            health = self._build_health_summary(agent_views)
            return DashboardState(
                generated_at=generated_at,
                metrics=metrics,
                filters=applied_filters,
                sort=applied_sort,
                health=health,
                alerts=alerts,
                agents=sorted_agents,
                selected_agent_id=selected_item.agent_id if selected_item is not None else None,
                selected_agent=selected_view,
            )

    def _build_agent_item(self, agent: Agent) -> DashboardAgentListItemView:
        latest_session = self._store.get_latest_session_for_agent(agent.id)
        latest_event = (
            self._store.get_latest_event_for_session(latest_session.id)
            if latest_session is not None
            else None
        )
        latest_log = (
            self._store.get_latest_log_chunk(latest_session.id)
            if latest_session is not None
            else None
        )
        estimated_cost = None
        if agent.estimated_cost_usd is not None:
            estimated_cost = format(agent.estimated_cost_usd, "f")

        needs_attention = agent.needs_attention
        attention_reason = agent.attention_reason

        runaway = _check_runaway(
            agent,
            now=self._clock(),
            max_cost_usd=self._max_cost_usd,
            max_runtime_minutes=self._max_runtime_minutes,
        )
        if runaway is not None:
            needs_attention = True
            attention_reason = runaway

        current_activity = _activity_from_task_title(agent.task_title)
        now = self._clock()

        # Sparkline: record activity and build visualization
        _record_activity(agent.id, current_activity, now)
        sparkline = _build_sparkline(
            _activity_history.get(agent.id, []),
            now=now,
        )

        # Heartbeat: detect stuck agents via output hash staleness
        output_blob = (agent.task_title or "") + (
            latest_log.content if latest_log is not None else ""
        )
        is_potentially_stuck = _check_stale_output(
            agent.id,
            output_blob,
            now=now,
            agent_status=agent.status,
        )
        if is_potentially_stuck and not needs_attention:
            stale_entry = _output_hashes.get(agent.id)
            stale_secs = (
                int((now - stale_entry[1]).total_seconds())
                if stale_entry is not None
                else _STALE_THRESHOLD_SECONDS
            )
            needs_attention = True
            attention_reason = f"output unchanged for {stale_secs}s — may be stuck"

        operator_status = describe_operator_status(
            agent_status=agent.status,
            needs_attention=needs_attention,
            attention_reason=attention_reason,
            idle_seconds=agent.idle_seconds,
            is_potentially_stuck=is_potentially_stuck,
            task_title=agent.task_title,
            current_activity=current_activity,
        )
        needs_attention = operator_status.needs_attention
        if needs_attention and attention_reason is None:
            attention_reason = operator_status.reason

        return DashboardAgentListItemView(
            agent_id=agent.id,
            name=agent.name,
            status=agent.status,
            repo_name=_path_name(agent.repo_root),
            branch=agent.branch,
            worktree_name=_path_name(agent.worktree_path),
            pane_id=agent.tmux_pane_id,
            task_title=agent.task_title,
            worktree_path=agent.worktree_path,
            latest_session_id=latest_session.id if latest_session is not None else None,
            last_event_kind=latest_event.kind if latest_event is not None else None,
            last_log_at=latest_log.captured_at if latest_log is not None else None,
            last_seen_at=agent.last_seen_at,
            started_at=agent.started_at,
            idle_seconds=agent.idle_seconds,
            needs_attention=needs_attention,
            attention_reason=attention_reason,
            token_total=agent.token_total,
            estimated_cost_usd=estimated_cost,
            window_name=agent.tmux_window_name,
            current_activity=current_activity,
            sparkline=sparkline,
            is_potentially_stuck=is_potentially_stuck,
            operator_status=operator_status,
        )

    def _filter_agents(
        self,
        agents: Sequence[DashboardAgentListItemView],
        filters: DashboardFilterState,
    ) -> tuple[DashboardAgentListItemView, ...]:
        query = filters.normalized_query()
        statuses = set(filters.statuses)
        visible: list[DashboardAgentListItemView] = []
        for agent in agents:
            if not filters.include_completed and agent.status in {
                AgentStatus.COMPLETED,
                AgentStatus.DEAD,
            }:
                continue
            if statuses and agent.status not in statuses:
                continue
            if filters.attention_only and not agent.needs_attention:
                continue
            if query is not None and query not in self._search_blob(agent):
                continue
            visible.append(agent)
        return tuple(visible)

    def _sort_agents(
        self,
        agents: Sequence[DashboardAgentListItemView],
        sort: DashboardSort,
    ) -> tuple[DashboardAgentListItemView, ...]:
        key_lookup: dict[
            DashboardSortField,
            Callable[[DashboardAgentListItemView], tuple[Any, ...]],
        ] = {
            "last_seen": lambda item: (item.last_seen_at, item.started_at, item.agent_id),
            "name": lambda item: (item.name.lower(), item.last_seen_at, item.agent_id),
            "status": lambda item: (item.status.value, item.last_seen_at, item.agent_id),
            "cost": lambda item: (item.estimated_cost_usd or "", item.last_seen_at, item.agent_id),
            "idle_seconds": lambda item: (item.idle_seconds, item.last_seen_at, item.agent_id),
            "started_at": lambda item: (item.started_at, item.last_seen_at, item.agent_id),
        }
        sorter = key_lookup[sort.field]
        return tuple(sorted(agents, key=sorter, reverse=sort.descending))

    def _select_agent(
        self,
        agents: Sequence[DashboardAgentListItemView],
        selected_agent_id: str | None,
    ) -> DashboardAgentListItemView | None:
        if selected_agent_id is not None:
            for agent in agents:
                if agent.agent_id == selected_agent_id:
                    return agent
        return agents[0] if agents else None

    def build_selected_agent_view(
        self,
        item: DashboardAgentListItemView,
        *,
        preview_line_limit: int = 8,
    ) -> DashboardSelectedAgentView:
        """Public entry point: build detailed view for a single selected agent."""
        return self._build_selected_agent(item, preview_line_limit=preview_line_limit)

    def _build_selected_agent(
        self,
        item: DashboardAgentListItemView,
        *,
        preview_line_limit: int,
    ) -> DashboardSelectedAgentView:
        latest_session = self._store.get_latest_session_for_agent(item.agent_id)
        session_count = self._store.count_sessions_for_agent(item.agent_id)
        open_session = self._store.get_open_session_for_agent(item.agent_id)
        context = None
        if latest_session is not None:
            context = self._store.get_session_context(latest_session.id)
        worktree = (
            self._store.get_worktree(context.worktree_id)
            if context is not None and context.worktree_id is not None
            else None
        )
        latest_event = (
            self._store.get_latest_event_for_session(latest_session.id)
            if latest_session is not None
            else None
        )
        # Only fetch the last N log chunks for preview — not the entire history.
        log_limit = max(preview_line_limit * 2, 20)
        logs = (
            tuple(self._store.list_recent_log_chunks(latest_session.id, limit=log_limit))
            if latest_session is not None
            else ()
        )
        worktree_id = None
        if worktree is not None:
            worktree_id = worktree.id
        elif context is not None:
            worktree_id = context.worktree_id
        return DashboardSelectedAgentView(
            item=item,
            repo_root=context.repo_root if context is not None else None,
            worktree_id=worktree_id,
            session_count=session_count,
            open_session_id=open_session.id if open_session is not None else None,
            copilot_session_id=(
                latest_session.copilot_session_id if latest_session is not None else None
            ),
            latest_event_kind=latest_event.kind if latest_event is not None else None,
            latest_event_severity=latest_event.severity if latest_event is not None else None,
            latest_event_at=latest_event.occurred_at if latest_event is not None else None,
            log_preview=self._build_log_preview(logs, preview_line_limit=preview_line_limit),
            recent_events=_extract_recent_events(logs),
        )

    def _build_log_preview(
        self,
        logs: Sequence[LogChunk],
        *,
        preview_line_limit: int,
    ) -> tuple[DashboardLogLineView, ...]:
        lines: list[DashboardLogLineView] = []
        for chunk in logs:
            for line in chunk.content.splitlines():
                stripped = line.rstrip()
                if not stripped:
                    continue
                lines.append(
                    DashboardLogLineView(
                        captured_at=chunk.captured_at,
                        source=chunk.source,
                        sequence_no=chunk.sequence_no,
                        content=stripped,
                    )
                )
        if preview_line_limit <= 0:
            return ()
        return tuple(lines[-preview_line_limit:])

    def _build_alerts(
        self,
        agents: Sequence[DashboardAgentListItemView],
        *,
        limit: int,
    ) -> tuple[DashboardAlertView, ...]:
        alerts: list[DashboardAlertView] = []
        # Terminal-status agents (DEAD/COMPLETED) are historical
        # records, not actionable signals. A dead pane that was reaped
        # minutes or days ago should not keep lighting up the
        # dashboard with "tmux pane no longer exists" — the user has
        # already moved on, and the alert has no remedy. We keep the
        # row in the agent list (so the history is visible) but
        # suppress the alert itself.
        terminal_statuses = {AgentStatus.DEAD, AgentStatus.COMPLETED}
        for agent in agents:
            if not agent.needs_attention:
                continue
            if agent.status in terminal_statuses:
                continue
            operator_status = agent.operator_status
            if operator_status is None:
                operator_status = describe_operator_status(
                    agent_status=agent.status,
                    needs_attention=agent.needs_attention,
                    attention_reason=agent.attention_reason,
                    idle_seconds=agent.idle_seconds,
                    is_potentially_stuck=agent.is_potentially_stuck,
                    task_title=agent.task_title,
                    current_activity=agent.current_activity,
                )
            severity: AlertSeverity = "info"
            if operator_status.tone == "warning":
                severity = "warning"
            elif operator_status.tone == "error":
                severity = "error"
            alerts.append(
                DashboardAlertView(
                    agent_id=agent.agent_id,
                    agent_name=agent.name,
                    severity=severity,
                    title=operator_status.headline,
                    message=operator_status.reason,
                    occurred_at=agent.last_seen_at,
                    alert_id=f"{agent.agent_id}:{operator_status.kind.value}",
                    operator_status=operator_status,
                    is_critical=operator_status.is_critical,
                )
            )
        ordered = sorted(alerts, key=lambda item: (item.occurred_at, item.agent_id), reverse=True)
        return tuple(ordered[:limit])

    def _build_metrics(
        self,
        agents: Sequence[DashboardAgentListItemView],
    ) -> tuple[DashboardMetricView, ...]:
        total_agents = len(agents)
        active_agents = sum(
            1 for agent in agents if agent.status not in {AgentStatus.COMPLETED, AgentStatus.DEAD}
        )
        attention_agents = sum(1 for agent in agents if agent.needs_attention)
        sessions_total = sum(1 for agent in agents if agent.latest_session_id is not None)
        total_tokens = sum(a.token_total or 0 for a in agents)
        metrics: list[DashboardMetricView] = [
            DashboardMetricView(key="agents", label="Agents", value=total_agents),
            DashboardMetricView(key="active", label="Active", value=active_agents),
            DashboardMetricView(
                key="attention",
                label="Attention",
                value=attention_agents,
            ),
            DashboardMetricView(
                key="sessions",
                label="Sessions",
                value=sessions_total,
            ),
        ]
        if total_tokens > 0:
            metrics.append(
                DashboardMetricView(
                    key="tokens",
                    label="Tokens",
                    value=total_tokens,
                ),
            )
        return tuple(metrics)

    def _build_health_summary(
        self,
        agents: Sequence[DashboardAgentListItemView],
    ) -> DashboardHealthSummary:
        total_agents = len(agents)
        active_agents = sum(
            1 for agent in agents if agent.status not in {AgentStatus.COMPLETED, AgentStatus.DEAD}
        )
        attention_agents = sum(1 for agent in agents if agent.needs_attention)
        waiting_input_agents = sum(
            1 for agent in agents if agent.status is AgentStatus.WAITING_INPUT
        )
        blocked_agents = sum(1 for agent in agents if agent.status is AgentStatus.BLOCKED)
        error_agents = sum(
            1 for agent in agents if agent.status in {AgentStatus.ERROR, AgentStatus.DEAD}
        )
        tone: HealthTone = "healthy"
        if blocked_agents or error_agents:
            tone = "critical"
        elif attention_agents or waiting_input_agents:
            tone = "warning"
        message = "all agents healthy"
        if tone == "critical":
            message = "intervention required"
        elif tone == "warning":
            message = "some agents need review"
        return DashboardHealthSummary(
            tone=tone,
            message=message,
            total_agents=total_agents,
            active_agents=active_agents,
            attention_agents=attention_agents,
            waiting_input_agents=waiting_input_agents,
            blocked_agents=blocked_agents,
            error_agents=error_agents,
        )

    def _search_blob(self, agent: DashboardAgentListItemView) -> str:
        return " ".join(
            part.lower()
            for part in (
                agent.agent_id,
                agent.name,
                agent.repo_name or "",
                agent.branch or "",
                agent.worktree_name or "",
                agent.pane_id,
                agent.task_title or "",
                agent.worktree_path or "",
                agent.last_event_kind or "",
                agent.attention_reason or "",
                agent.operator_status.headline if agent.operator_status is not None else "",
                agent.operator_status.reason if agent.operator_status is not None else "",
            )
            if part
        )


_EVENT_EMOJI: dict[str, str] = {
    "file_read": "📖",
    "file_write": "✏️",
    "command": "⚡",
    "search": "🔍",
    "thinking": "💭",
    "tool_use": "🔧",
}

_MAX_RECENT_EVENTS = 20


def _extract_recent_events(
    logs: Sequence[LogChunk],
    *,
    limit: int = _MAX_RECENT_EVENTS,
) -> tuple[str, ...]:
    """Parse log content through activity patterns and return emoji-prefixed event strings."""
    content_parts: list[str] = []
    for chunk in logs:
        content_parts.append(chunk.content)
    if not content_parts:
        return ()
    combined = "\n".join(content_parts)
    result = parse_copilot_output(combined)
    events: list[str] = []
    # Sort activity markers by their line position to preserve order
    sorted_markers = sorted(result.activity_markers, key=lambda m: m.span.start_line)
    for marker in sorted_markers:
        emoji = _EVENT_EMOJI.get(marker.category, "●")
        activity = marker.activity
        # Capitalize first letter after emoji
        if activity:
            activity = activity[0].upper() + activity[1:]
        events.append(f"{emoji} {activity}")
    # Also include errors as events
    for error in result.errors:
        events.append(f"⚠️ {error.message[:80]}")
    # Deduplicate consecutive identical events
    deduped: list[str] = []
    for event in events:
        if not deduped or deduped[-1] != event:
            deduped.append(event)
    return tuple(deduped[-limit:])


def _activity_from_task_title(title: str | None) -> str | None:
    """Surface whatever Copilot said it's doing, not just whitelisted verbs.

    Earlier versions only passed titles that started with a known verb
    ("reading", "writing", ...), which meant the dashboard said nothing
    when the parser returned a free-form title ("Investigating test
    failures", "Refactoring cache layer"). The user's top complaint was
    "I do not easily know what an agent is doing", so show the title
    verbatim when it's present and non-empty; trim overly long lines so
    the detail panel stays compact.
    """
    if title is None:
        return None
    trimmed = title.strip()
    if not trimmed:
        return None
    if len(trimmed) > 120:
        trimmed = trimmed[:117].rstrip() + "…"
    return trimmed


def _path_name(value: str | None) -> str | None:
    if value is None:
        return None
    path_name = Path(value).name
    return path_name or value


def _check_runaway(
    agent: Agent,
    *,
    now: datetime,
    max_cost_usd: Decimal | None,
    max_runtime_minutes: int | None,
) -> str | None:
    """Return an attention reason if the agent exceeds cost or runtime limits."""
    if agent.status in {AgentStatus.COMPLETED, AgentStatus.DEAD}:
        return None

    if (
        max_cost_usd is not None
        and agent.estimated_cost_usd is not None
        and agent.estimated_cost_usd > max_cost_usd
    ):
        return f"cost ${agent.estimated_cost_usd:.2f} exceeds limit ${max_cost_usd:.2f}"

    if max_runtime_minutes is not None:
        runtime_minutes = (now - agent.started_at).total_seconds() / 60
        if runtime_minutes > max_runtime_minutes:
            return f"runtime {runtime_minutes:.0f}m exceeds limit {max_runtime_minutes}m"

    return None


def _build_sparkline(
    activity_timestamps: Sequence[datetime],
    *,
    now: datetime | None = None,
    window_minutes: int = 10,
    bars: int = 8,
) -> str:
    """Map recent activity timestamps into a sparkline string of length *bars*."""
    if now is None:
        now = datetime.now(UTC)
    window = timedelta(minutes=window_minutes)
    cutoff = now - window
    relevant = [ts for ts in activity_timestamps if ts >= cutoff]
    if not relevant:
        return " " * bars
    bucket_width = window / bars
    counts: list[int] = [0] * bars
    for ts in relevant:
        bucket_index = int((ts - cutoff) / bucket_width)
        bucket_index = min(bucket_index, bars - 1)
        counts[bucket_index] += 1
    max_count = max(counts)
    if max_count == 0:
        return " " * bars
    last_index = len(_SPARK_CHARS) - 1
    return "".join(
        _SPARK_CHARS[min(round(count / max_count * last_index), last_index)] for count in counts
    )


def _record_activity(agent_id: str, current_activity: str | None, now: datetime) -> None:
    """Append a timestamp to activity history when the agent has activity."""
    if current_activity is None:
        return
    history = _activity_history.setdefault(agent_id, [])
    history.append(now)
    if len(history) > _ACTIVITY_HISTORY_CAP:
        _activity_history[agent_id] = history[-_ACTIVITY_HISTORY_CAP:]


def _check_stale_output(
    agent_id: str,
    output_blob: str,
    *,
    now: datetime,
    agent_status: AgentStatus,
) -> bool:
    """Return True when running agent output hasn't changed beyond threshold."""
    terminal = {AgentStatus.COMPLETED, AgentStatus.DEAD, AgentStatus.ERROR}
    if agent_status in terminal:
        _output_hashes.pop(agent_id, None)
        return False
    current_hash = hashlib.md5(output_blob.encode(), usedforsecurity=False).hexdigest()
    previous = _output_hashes.get(agent_id)
    if previous is None or previous[0] != current_hash:
        _output_hashes[agent_id] = (current_hash, now)
        return False
    stale_seconds = (now - previous[1]).total_seconds()
    return stale_seconds > _STALE_THRESHOLD_SECONDS


def _to_view(snapshot: SubAgentSnapshot) -> DashboardSubAgentView:
    return DashboardSubAgentView(
        tool_call_id=snapshot.tool_call_id,
        agent_name=snapshot.agent_name,
        display_name=snapshot.display_name,
        description=snapshot.description,
        started_at=snapshot.started_at,
        completed_at=snapshot.completed_at,
        is_running=snapshot.is_running,
        task_name=snapshot.task_name,
        agent_type=snapshot.agent_type,
        prompt=snapshot.prompt,
        result_content=snapshot.result_content,
        success=snapshot.success,
    )


__all__ = [
    "DashboardAgentListItemView",
    "DashboardAlertView",
    "DashboardController",
    "DashboardFilterState",
    "DashboardHealthSummary",
    "DashboardLogLineView",
    "DashboardMetricView",
    "DashboardSelectedAgentView",
    "DashboardSort",
    "DashboardState",
    "DashboardSubAgentTreeView",
    "DashboardSubAgentView",
    "SubAgentReaderPort",
    "_build_sparkline",
    "_check_stale_output",
]
