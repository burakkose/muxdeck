from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from copilot_commander.adapters.copilot_session_store import CopilotLocalSession
from copilot_commander.adapters.sqlite_store import SessionContextRecord
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.events import Event
from copilot_commander.domain.models import Agent, Session, Worktree
from copilot_commander.domain.value_objects import utc_now
from copilot_commander.types import Clock

FleetTone = Literal["healthy", "warning", "critical"]
FleetSeverity = Literal["info", "warning", "error"]

_ACTIVE_AGENT_STATUSES = {
    AgentStatus.RUNNING,
    AgentStatus.IDLE,
    AgentStatus.WAITING_INPUT,
    AgentStatus.BLOCKED,
    AgentStatus.STARTING,
    AgentStatus.DISCOVERED,
    AgentStatus.UNKNOWN,
}


class FleetStorePort(Protocol):
    def list_agents(self) -> Sequence[Agent]: ...

    def list_worktrees(
        self,
        /,
        *,
        repo_root: str | None = None,
        assigned_agent_id: str | None = None,
    ) -> Sequence[Worktree]: ...

    def list_sessions(self, agent_id: str | None = None, /) -> Sequence[Session]: ...

    def list_events(
        self,
        /,
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> Sequence[Event]: ...

    def get_session_context(self, session_id: str, /) -> SessionContextRecord | None: ...


class FleetLocalSessionStorePort(Protocol):
    def discover(self, *, force: bool = False) -> Sequence[CopilotLocalSession]: ...


@dataclass(frozen=True, slots=True)
class FleetFilterState:
    text_query: str | None = None
    attention_only: bool = False
    include_completed: bool = True

    def normalized_query(self) -> str | None:
        if self.text_query is None:
            return None
        query = self.text_query.strip().lower()
        return query or None


@dataclass(frozen=True, slots=True)
class FleetAgentSummaryView:
    agent_id: str
    name: str
    status: AgentStatus
    repo_key: str
    repo_label: str
    repo_root: str | None
    worktree_name: str
    branch: str
    task_title: str
    session_label: str
    session_count: int
    needs_attention: bool
    attention_summary: str | None
    last_update_at: datetime
    idle_seconds: int
    worktree_dirty: bool
    worktree_locked: bool
    token_total: int | None
    estimated_cost_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class FleetRepoGroupView:
    repo_key: str
    repo_label: str
    repo_root: str | None
    agent_count: int
    active_count: int
    attention_count: int
    worktree_count: int
    dirty_worktree_count: int
    locked_worktree_count: int
    session_count: int
    orphan_local_session_count: int
    token_total: int
    estimated_cost_usd: Decimal | None
    agents: tuple[FleetAgentSummaryView, ...]


@dataclass(frozen=True, slots=True)
class FleetHealthSummary:
    tone: FleetTone
    message: str
    total_agents: int
    active_agents: int
    attention_agents: int
    waiting_agents: int
    blocked_agents: int
    error_agents: int
    total_worktrees: int
    dirty_worktrees: int
    orphan_local_sessions: int


@dataclass(frozen=True, slots=True)
class FleetHistoryMetricView:
    label: str
    value: str
    detail: str


@dataclass(frozen=True, slots=True)
class FleetRecentActivityView:
    occurred_at: datetime
    title: str
    detail: str
    severity: FleetSeverity


@dataclass(frozen=True, slots=True)
class FleetSearchHitView:
    kind: str
    title: str
    detail: str


@dataclass(frozen=True, slots=True)
class FleetSearchHelperView:
    label: str
    query: str
    detail: str
    match_count: int


@dataclass(frozen=True, slots=True)
class FleetResourceView:
    label: str
    value: str
    detail: str
    tone: FleetTone


@dataclass(frozen=True, slots=True)
class FleetState:
    generated_at: datetime
    filters: FleetFilterState
    total_visible_agents: int
    total_groups: int
    health: FleetHealthSummary
    groups: tuple[FleetRepoGroupView, ...]
    history_metrics: tuple[FleetHistoryMetricView, ...]
    recent_activity: tuple[FleetRecentActivityView, ...]
    search_hits: tuple[FleetSearchHitView, ...]
    search_helpers: tuple[FleetSearchHelperView, ...]
    resources: tuple[FleetResourceView, ...]


@dataclass(slots=True)
class _AgentContext:
    agent: Agent
    sessions: tuple[Session, ...]
    latest_session: Session | None
    context: SessionContextRecord | None
    linked_local_session: CopilotLocalSession | None
    repo_key: str
    repo_label: str
    repo_root: str | None
    worktree: Worktree | None


class FleetController:
    def __init__(
        self,
        store: FleetStorePort,
        *,
        local_sessions: FleetLocalSessionStorePort | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._store = store
        self._local_sessions = local_sessions
        self._clock = clock

    def build_state(
        self,
        *,
        filters: FleetFilterState | None = None,
        activity_limit: int = 8,
        search_limit: int = 10,
    ) -> FleetState:
        applied_filters = FleetFilterState() if filters is None else filters
        now = self._clock()
        agents = tuple(self._store.list_agents())
        worktrees = tuple(self._store.list_worktrees())
        sessions = tuple(self._store.list_sessions())
        events = tuple(self._store.list_events())
        local_sessions = tuple(
            self._local_sessions.discover() if self._local_sessions is not None else ()
        )

        contexts = {session.id: self._store.get_session_context(session.id) for session in sessions}
        sessions_by_agent = _sessions_by_agent(sessions)
        worktrees_by_id = {worktree.id: worktree for worktree in worktrees}
        worktrees_by_path = {worktree.path: worktree for worktree in worktrees}
        local_by_id = {session.session_id: session for session in local_sessions}

        agent_contexts = tuple(
            self._build_agent_context(
                agent=agent,
                sessions=sessions_by_agent.get(agent.id, ()),
                local_by_id=local_by_id,
                contexts=contexts,
                worktrees_by_id=worktrees_by_id,
                worktrees_by_path=worktrees_by_path,
            )
            for agent in agents
        )
        agent_views = tuple(self._build_agent_view(item) for item in agent_contexts)
        visible_agents = self._filter_agents(agent_views, applied_filters)
        groups = self._build_groups(
            visible_agents=visible_agents,
            all_worktrees=worktrees,
            sessions=sessions,
            contexts=contexts,
            local_sessions=local_sessions,
            linked_local_session_ids=_linked_local_session_ids(sessions, agents),
            repo_lookup={context.agent.id: context for context in agent_contexts},
        )
        orphan_local_sessions = _orphan_local_sessions(local_sessions, sessions, agents)
        return FleetState(
            generated_at=now,
            filters=applied_filters,
            total_visible_agents=len(visible_agents),
            total_groups=len(groups),
            health=self._build_health(
                agents=agent_views,
                worktrees=worktrees,
                orphan_local_sessions=orphan_local_sessions,
            ),
            groups=groups,
            history_metrics=self._build_history_metrics(
                now=now,
                agents=agent_views,
                worktrees=worktrees,
                sessions=sessions,
                events=events,
                orphan_local_sessions=orphan_local_sessions,
                groups=groups,
            ),
            recent_activity=self._build_recent_activity(
                events=events,
                sessions=sessions,
                agents=agents,
                local_sessions=local_sessions,
                limit=activity_limit,
            ),
            search_hits=self._build_search_hits(
                query=applied_filters.normalized_query(),
                agents=agent_views,
                worktrees=worktrees,
                sessions=sessions,
                contexts=contexts,
                local_sessions=local_sessions,
                limit=search_limit,
            ),
            search_helpers=self._build_search_helpers(
                agents=agent_views,
                worktrees=worktrees,
                orphan_local_sessions=orphan_local_sessions,
                groups=groups,
            ),
            resources=self._build_resources(
                agents=agent_views,
                worktrees=worktrees,
                local_sessions=local_sessions,
                orphan_local_sessions=orphan_local_sessions,
                groups=groups,
            ),
        )

    def _build_agent_context(
        self,
        *,
        agent: Agent,
        sessions: tuple[Session, ...],
        local_by_id: dict[str, CopilotLocalSession],
        contexts: dict[str, SessionContextRecord | None],
        worktrees_by_id: dict[str, Worktree],
        worktrees_by_path: dict[str, Worktree],
    ) -> _AgentContext:
        latest_session = sessions[0] if sessions else None
        context = contexts.get(latest_session.id) if latest_session is not None else None
        local_session_id = agent.copilot_session_id
        if local_session_id is None and latest_session is not None:
            local_session_id = latest_session.copilot_session_id
        linked_local_session = local_by_id.get(local_session_id) if local_session_id else None
        worktree = None
        if context is not None and context.worktree_id is not None:
            worktree = worktrees_by_id.get(context.worktree_id)
        if worktree is None:
            worktree_path = context.worktree_path if context is not None else agent.worktree_path
            if worktree_path is not None:
                worktree = worktrees_by_path.get(worktree_path)
        repo_root = (
            agent.repo_root
            if agent.repo_root is not None
            else (context.repo_root if context is not None else None)
        )
        repo_key, repo_label = _repo_identity(
            repo_root=repo_root,
            repository=linked_local_session.repository
            if linked_local_session is not None
            else None,
            git_root=(
                str(linked_local_session.git_root)
                if linked_local_session is not None and linked_local_session.git_root is not None
                else None
            ),
        )
        resolved_repo_root = repo_root or (
            str(linked_local_session.git_root)
            if linked_local_session is not None and linked_local_session.git_root is not None
            else None
        )
        return _AgentContext(
            agent=agent,
            sessions=sessions,
            latest_session=latest_session,
            context=context,
            linked_local_session=linked_local_session,
            repo_key=repo_key,
            repo_label=repo_label,
            repo_root=resolved_repo_root,
            worktree=worktree,
        )

    def _build_agent_view(self, item: _AgentContext) -> FleetAgentSummaryView:
        task_title = (
            item.agent.task_title
            or (item.latest_session.task_title if item.latest_session is not None else None)
            or (
                item.linked_local_session.summary if item.linked_local_session is not None else None
            )
            or "(no task title)"
        )
        branch = (
            item.agent.branch
            or (item.context.branch if item.context is not None else None)
            or (item.linked_local_session.branch if item.linked_local_session is not None else None)
            or "-"
        )
        worktree_name = _path_name(
            item.agent.worktree_path
            or (item.context.worktree_path if item.context is not None else None)
            or (
                str(item.linked_local_session.cwd)
                if item.linked_local_session is not None
                else None
            )
        )
        attention_summary = item.agent.attention_reason
        if (
            attention_summary is None
            and item.linked_local_session is not None
            and not item.linked_local_session.is_cleanly_closed
            and item.agent.status in {AgentStatus.COMPLETED, AgentStatus.DEAD}
        ):
            attention_summary = "local session remains unclosed"
        last_update_at = item.agent.last_seen_at
        if (
            item.linked_local_session is not None
            and item.linked_local_session.updated_at is not None
        ):
            last_update_at = max(last_update_at, item.linked_local_session.updated_at)
        session_label = (
            item.agent.copilot_session_id
            or (item.latest_session.copilot_session_id if item.latest_session is not None else None)
            or (item.latest_session.id if item.latest_session is not None else None)
            or "-"
        )
        return FleetAgentSummaryView(
            agent_id=item.agent.id,
            name=item.agent.name,
            status=item.agent.status,
            repo_key=item.repo_key,
            repo_label=item.repo_label,
            repo_root=item.repo_root,
            worktree_name=worktree_name,
            branch=branch,
            task_title=task_title,
            session_label=session_label,
            session_count=len(item.sessions),
            needs_attention=item.agent.needs_attention or attention_summary is not None,
            attention_summary=attention_summary,
            last_update_at=last_update_at,
            idle_seconds=item.agent.idle_seconds,
            worktree_dirty=item.worktree.is_dirty if item.worktree is not None else False,
            worktree_locked=item.worktree.locked if item.worktree is not None else False,
            token_total=item.agent.token_total,
            estimated_cost_usd=item.agent.estimated_cost_usd,
        )

    def _filter_agents(
        self,
        agents: Sequence[FleetAgentSummaryView],
        filters: FleetFilterState,
    ) -> tuple[FleetAgentSummaryView, ...]:
        query = filters.normalized_query()
        filtered: list[FleetAgentSummaryView] = []
        for agent in agents:
            if not filters.include_completed and agent.status not in _ACTIVE_AGENT_STATUSES:
                continue
            if filters.attention_only and not agent.needs_attention:
                continue
            if query is not None and query not in _search_blob_for_agent(agent):
                continue
            filtered.append(agent)
        return tuple(
            sorted(
                filtered,
                key=lambda item: (item.repo_label.lower(), item.last_update_at, item.agent_id),
            )
        )

    def _build_groups(
        self,
        *,
        visible_agents: Sequence[FleetAgentSummaryView],
        all_worktrees: Sequence[Worktree],
        sessions: Sequence[Session],
        contexts: dict[str, SessionContextRecord | None],
        local_sessions: Sequence[CopilotLocalSession],
        linked_local_session_ids: frozenset[str],
        repo_lookup: dict[str, _AgentContext],
    ) -> tuple[FleetRepoGroupView, ...]:
        visible_repo_keys = {agent.repo_key for agent in visible_agents}
        grouped_agents: dict[str, list[FleetAgentSummaryView]] = defaultdict(list)
        for agent in visible_agents:
            grouped_agents[agent.repo_key].append(agent)

        worktree_counts: dict[str, list[Worktree]] = defaultdict(list)
        for worktree in all_worktrees:
            worktree_repo_key, _ = _repo_identity(repo_root=worktree.repo_root)
            if visible_repo_keys and worktree_repo_key not in visible_repo_keys:
                continue
            worktree_counts[worktree_repo_key].append(worktree)

        session_counts: dict[str, set[str]] = defaultdict(set)
        for session in sessions:
            context = contexts.get(session.id)
            repo_key: str | None = None
            if context is not None and context.repo_root is not None:
                repo_key, _ = _repo_identity(repo_root=context.repo_root)
            elif session.agent_id in repo_lookup:
                repo_key = repo_lookup[session.agent_id].repo_key
            if repo_key is None:
                continue
            if visible_repo_keys and repo_key not in visible_repo_keys:
                continue
            session_counts[repo_key].add(session.id)

        orphan_local_counts: dict[str, int] = defaultdict(int)
        for local_session in local_sessions:
            if local_session.session_id in linked_local_session_ids:
                continue
            repo_key, _ = _repo_identity(
                repo_root=(
                    str(local_session.git_root) if local_session.git_root is not None else None
                ),
                repository=local_session.repository,
            )
            if visible_repo_keys and repo_key not in visible_repo_keys:
                continue
            orphan_local_counts[repo_key] += 1

        group_views: list[FleetRepoGroupView] = []
        for repo_key, agents in grouped_agents.items():
            repo_worktrees = tuple(worktree_counts.get(repo_key, ()))
            costs = [
                agent.estimated_cost_usd for agent in agents if agent.estimated_cost_usd is not None
            ]
            group_views.append(
                FleetRepoGroupView(
                    repo_key=repo_key,
                    repo_label=agents[0].repo_label,
                    repo_root=next(
                        (agent.repo_root for agent in agents if agent.repo_root is not None), None
                    ),
                    agent_count=len(agents),
                    active_count=sum(
                        1 for agent in agents if agent.status in _ACTIVE_AGENT_STATUSES
                    ),
                    attention_count=sum(1 for agent in agents if agent.needs_attention),
                    worktree_count=len(repo_worktrees),
                    dirty_worktree_count=sum(1 for worktree in repo_worktrees if worktree.is_dirty),
                    locked_worktree_count=sum(1 for worktree in repo_worktrees if worktree.locked),
                    session_count=len(session_counts.get(repo_key, set())),
                    orphan_local_session_count=orphan_local_counts.get(repo_key, 0),
                    token_total=sum(agent.token_total or 0 for agent in agents),
                    estimated_cost_usd=(sum(costs, start=Decimal("0")) if costs else None),
                    agents=tuple(
                        sorted(
                            agents,
                            key=lambda item: (
                                not item.needs_attention,
                                item.status.value,
                                item.last_update_at,
                                item.name.lower(),
                            ),
                        )
                    ),
                )
            )
        return tuple(
            sorted(
                group_views,
                key=lambda item: (
                    -item.attention_count,
                    -item.agent_count,
                    item.repo_label.lower(),
                ),
            )
        )

    def _build_health(
        self,
        *,
        agents: Sequence[FleetAgentSummaryView],
        worktrees: Sequence[Worktree],
        orphan_local_sessions: Sequence[CopilotLocalSession],
    ) -> FleetHealthSummary:
        active_agents = sum(1 for agent in agents if agent.status in _ACTIVE_AGENT_STATUSES)
        attention_agents = sum(1 for agent in agents if agent.needs_attention)
        waiting_agents = sum(1 for agent in agents if agent.status is AgentStatus.WAITING_INPUT)
        blocked_agents = sum(1 for agent in agents if agent.status is AgentStatus.BLOCKED)
        error_agents = sum(
            1 for agent in agents if agent.status in {AgentStatus.ERROR, AgentStatus.DEAD}
        )
        dirty_worktrees = sum(1 for worktree in worktrees if worktree.is_dirty)
        tone: FleetTone = "healthy"
        if blocked_agents or error_agents:
            tone = "critical"
        elif attention_agents or dirty_worktrees or orphan_local_sessions:
            tone = "warning"
        message = "fleet is healthy"
        if tone == "critical":
            message = "intervention required"
        elif tone == "warning":
            message = "review pending issues"
        return FleetHealthSummary(
            tone=tone,
            message=message,
            total_agents=len(agents),
            active_agents=active_agents,
            attention_agents=attention_agents,
            waiting_agents=waiting_agents,
            blocked_agents=blocked_agents,
            error_agents=error_agents,
            total_worktrees=len(worktrees),
            dirty_worktrees=dirty_worktrees,
            orphan_local_sessions=len(orphan_local_sessions),
        )

    def _build_history_metrics(
        self,
        *,
        now: datetime,
        agents: Sequence[FleetAgentSummaryView],
        worktrees: Sequence[Worktree],
        sessions: Sequence[Session],
        events: Sequence[Event],
        orphan_local_sessions: Sequence[CopilotLocalSession],
        groups: Sequence[FleetRepoGroupView],
    ) -> tuple[FleetHistoryMetricView, ...]:
        cutoff = now - timedelta(hours=24)
        dirty_group_count = sum(1 for group in groups if group.dirty_worktree_count)
        open_session_count = sum(1 for session in sessions if session.ended_at is None)
        return (
            FleetHistoryMetricView(
                label="repos",
                value=str(len(groups)),
                detail=f"{dirty_group_count} dirty · {len(worktrees)} worktrees",
            ),
            FleetHistoryMetricView(
                label="24h sessions",
                value=str(sum(1 for session in sessions if session.created_at >= cutoff)),
                detail=f"{open_session_count} currently open",
            ),
            FleetHistoryMetricView(
                label="24h events",
                value=str(sum(1 for event in events if event.occurred_at >= cutoff)),
                detail=f"{len(orphan_local_sessions)} orphan local sessions",
            ),
            FleetHistoryMetricView(
                label="tokens",
                value=_format_count(sum(agent.token_total or 0 for agent in agents)),
                detail="observed runtime usage",
            ),
        )

    def _build_recent_activity(
        self,
        *,
        events: Sequence[Event],
        sessions: Sequence[Session],
        agents: Sequence[Agent],
        local_sessions: Sequence[CopilotLocalSession],
        limit: int,
    ) -> tuple[FleetRecentActivityView, ...]:
        agent_names = {agent.id: agent.name for agent in agents}
        items: list[FleetRecentActivityView] = []
        for event in events:
            items.append(
                FleetRecentActivityView(
                    occurred_at=event.occurred_at,
                    title=event.kind.replace("_", " "),
                    detail=agent_names.get(
                        event.agent_id or "", event.agent_id or event.session_id or "-"
                    ),
                    severity=_event_severity(event.severity),
                )
            )
        for session in sessions:
            items.append(
                FleetRecentActivityView(
                    occurred_at=session.created_at,
                    title="session started",
                    detail=session.task_title or session.copilot_session_id or session.id,
                    severity="info",
                )
            )
            if session.ended_at is not None:
                items.append(
                    FleetRecentActivityView(
                        occurred_at=session.ended_at,
                        title="session ended",
                        detail=session.exit_reason or session.id,
                        severity="warning",
                    )
                )
        for local_session in local_sessions:
            if local_session.updated_at is None:
                continue
            items.append(
                FleetRecentActivityView(
                    occurred_at=local_session.updated_at,
                    title="local session updated",
                    detail=local_session.summary or local_session.session_id,
                    severity="info" if local_session.is_cleanly_closed else "warning",
                )
            )
        return tuple(sorted(items, key=lambda item: item.occurred_at, reverse=True)[:limit])

    def _build_search_hits(
        self,
        *,
        query: str | None,
        agents: Sequence[FleetAgentSummaryView],
        worktrees: Sequence[Worktree],
        sessions: Sequence[Session],
        contexts: dict[str, SessionContextRecord | None],
        local_sessions: Sequence[CopilotLocalSession],
        limit: int,
    ) -> tuple[FleetSearchHitView, ...]:
        if query is None:
            return ()
        hits: list[FleetSearchHitView] = []
        for agent in agents:
            if query in _search_blob_for_agent(agent):
                hits.append(
                    FleetSearchHitView(
                        kind="agent",
                        title=f"{agent.name} · {agent.task_title}",
                        detail=f"{agent.repo_label} · {agent.worktree_name} · {agent.branch}",
                    )
                )
        for worktree in worktrees:
            if query in _search_blob_for_worktree(worktree):
                repo_label = _repo_identity(repo_root=worktree.repo_root)[1]
                hits.append(
                    FleetSearchHitView(
                        kind="worktree",
                        title=f"{_path_name(worktree.path)} · {worktree.branch}",
                        detail=(
                            f"{repo_label} · dirty={worktree.is_dirty} · locked={worktree.locked}"
                        ),
                    )
                )
        for session in sessions:
            context = contexts.get(session.id)
            if query in _search_blob_for_session(session, context):
                branch = (
                    context.branch if context is not None and context.branch is not None else "-"
                )
                worktree_path = (
                    context.worktree_path
                    if context is not None and context.worktree_path is not None
                    else "-"
                )
                hits.append(
                    FleetSearchHitView(
                        kind="session",
                        title=session.task_title or session.copilot_session_id or session.id,
                        detail=f"{branch} · {worktree_path}",
                    )
                )
        for local_session in local_sessions:
            if query in _search_blob_for_local_session(local_session):
                hits.append(
                    FleetSearchHitView(
                        kind="local",
                        title=local_session.summary or local_session.session_id,
                        detail=f"{local_session.repository or '-'} · {local_session.branch or '-'}",
                    )
                )
        return tuple(hits[:limit])

    def _build_search_helpers(
        self,
        *,
        agents: Sequence[FleetAgentSummaryView],
        worktrees: Sequence[Worktree],
        orphan_local_sessions: Sequence[CopilotLocalSession],
        groups: Sequence[FleetRepoGroupView],
    ) -> tuple[FleetSearchHelperView, ...]:
        helpers: list[FleetSearchHelperView] = []
        attention_agents = [agent for agent in agents if agent.needs_attention]
        if attention_agents:
            helpers.append(
                FleetSearchHelperView(
                    label="attention sweep",
                    query="attention",
                    detail="focus agents that need operator review",
                    match_count=len(attention_agents),
                )
            )
        waiting_agents = [agent for agent in agents if agent.status is AgentStatus.WAITING_INPUT]
        if waiting_agents:
            helpers.append(
                FleetSearchHelperView(
                    label="waiting input",
                    query="waiting",
                    detail="find agents paused for input",
                    match_count=len(waiting_agents),
                )
            )
        dirty_worktrees = [worktree for worktree in worktrees if worktree.is_dirty]
        if dirty_worktrees:
            helpers.append(
                FleetSearchHelperView(
                    label="dirty worktrees",
                    query="dirty",
                    detail="surface repos with uncommitted changes",
                    match_count=len(dirty_worktrees),
                )
            )
        if orphan_local_sessions:
            helpers.append(
                FleetSearchHelperView(
                    label="orphan sessions",
                    query="unclosed",
                    detail="inspect local sessions not linked to tracked agents",
                    match_count=len(orphan_local_sessions),
                )
            )
        hottest_group = next((group for group in groups if group.attention_count > 0), None)
        if hottest_group is not None:
            helpers.append(
                FleetSearchHelperView(
                    label="repo hotspot",
                    query=hottest_group.repo_label,
                    detail="jump to the repo with the most pending attention",
                    match_count=hottest_group.attention_count,
                )
            )
        return tuple(helpers)

    def _build_resources(
        self,
        *,
        agents: Sequence[FleetAgentSummaryView],
        worktrees: Sequence[Worktree],
        local_sessions: Sequence[CopilotLocalSession],
        orphan_local_sessions: Sequence[CopilotLocalSession],
        groups: Sequence[FleetRepoGroupView],
    ) -> tuple[FleetResourceView, ...]:
        dirty_worktree_count = sum(1 for worktree in worktrees if worktree.is_dirty)
        locked_worktree_count = sum(1 for worktree in worktrees if worktree.locked)
        session_link_count = sum(1 for agent in agents if agent.session_label != "-")
        active_status_count = sum(1 for agent in agents if agent.status in _ACTIVE_AGENT_STATUSES)
        linked_local_session_count = len(local_sessions) - len(orphan_local_sessions)
        return (
            FleetResourceView(
                label="repos",
                value=str(len(groups)),
                detail="active repo groupings in this view",
                tone="healthy",
            ),
            FleetResourceView(
                label="worktrees",
                value=str(len(worktrees)),
                detail=(f"dirty {dirty_worktree_count} · locked {locked_worktree_count}"),
                tone="warning"
                if any(worktree.is_dirty or worktree.locked for worktree in worktrees)
                else "healthy",
            ),
            FleetResourceView(
                label="runtime",
                value=str(len(agents)),
                detail=(
                    f"{session_link_count} session links · {active_status_count} active statuses"
                ),
                tone="healthy",
            ),
            FleetResourceView(
                label="local sessions",
                value=str(len(local_sessions)),
                detail=(
                    f"linked {linked_local_session_count} · orphan {len(orphan_local_sessions)}"
                ),
                tone="warning" if orphan_local_sessions else "healthy",
            ),
        )


def _sessions_by_agent(sessions: Sequence[Session]) -> dict[str, tuple[Session, ...]]:
    grouped: dict[str, list[Session]] = defaultdict(list)
    for session in sessions:
        grouped[session.agent_id].append(session)
    return {
        agent_id: tuple(sorted(group, key=lambda item: (item.created_at, item.id), reverse=True))
        for agent_id, group in grouped.items()
    }


def _linked_local_session_ids(
    sessions: Sequence[Session], agents: Sequence[Agent]
) -> frozenset[str]:
    linked = {
        session_id
        for session_id in (
            *(agent.copilot_session_id for agent in agents),
            *(session.copilot_session_id for session in sessions),
        )
        if session_id is not None
    }
    return frozenset(linked)


def _orphan_local_sessions(
    local_sessions: Sequence[CopilotLocalSession],
    sessions: Sequence[Session],
    agents: Sequence[Agent],
) -> tuple[CopilotLocalSession, ...]:
    linked_ids = _linked_local_session_ids(sessions, agents)
    return tuple(session for session in local_sessions if session.session_id not in linked_ids)


def _repo_identity(
    *,
    repo_root: str | None = None,
    repository: str | None = None,
    git_root: str | None = None,
) -> tuple[str, str]:
    if repo_root:
        return (f"root:{repo_root}", _path_name(repo_root))
    if git_root:
        return (f"root:{git_root}", _path_name(git_root))
    if repository:
        return (f"repo:{repository}", repository)
    return ("repo:unknown", "unknown")


def _path_name(path: str | None) -> str:
    if not path:
        return "-"
    name = Path(path).name
    return name or path


def _format_count(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _search_blob_for_agent(agent: FleetAgentSummaryView) -> str:
    return " ".join(
        part.lower()
        for part in (
            agent.agent_id,
            agent.name,
            agent.status.value,
            agent.status.value.replace("_", " "),
            agent.repo_label,
            agent.repo_root or "",
            agent.worktree_name,
            agent.branch,
            agent.task_title,
            agent.session_label,
            agent.attention_summary or "",
            "attention" if agent.needs_attention else "",
            "dirty" if agent.worktree_dirty else "",
            "locked" if agent.worktree_locked else "",
        )
        if part
    )


def _search_blob_for_worktree(worktree: Worktree) -> str:
    return " ".join(
        part.lower()
        for part in (
            worktree.repo_root,
            worktree.path,
            worktree.branch,
            worktree.base_branch or "",
            "dirty" if worktree.is_dirty else "clean",
            "locked" if worktree.locked else "unlocked",
            "main" if worktree.is_main_worktree else "feature",
        )
        if part
    )


def _search_blob_for_session(session: Session, context: SessionContextRecord | None) -> str:
    return " ".join(
        part.lower()
        for part in (
            session.id,
            session.copilot_session_id or "",
            session.task_title or "",
            session.exit_reason or "",
            context.repo_root if context is not None and context.repo_root is not None else "",
            context.branch if context is not None and context.branch is not None else "",
            context.worktree_path
            if context is not None and context.worktree_path is not None
            else "",
            "open" if session.ended_at is None else "closed",
        )
        if part
    )


def _search_blob_for_local_session(session: CopilotLocalSession) -> str:
    return " ".join(
        part.lower()
        for part in (
            session.session_id,
            session.summary or "",
            session.repository or "",
            session.branch or "",
            str(session.cwd) if session.cwd is not None else "",
            str(session.git_root) if session.git_root is not None else "",
            session.last_event_type or "",
            "completed" if session.is_cleanly_closed else "unclosed",
        )
        if part
    )


def _event_severity(value: str) -> FleetSeverity:
    if value == "error":
        return "error"
    if value == "warning":
        return "warning"
    return "info"


__all__ = [
    "FleetAgentSummaryView",
    "FleetController",
    "FleetFilterState",
    "FleetHealthSummary",
    "FleetHistoryMetricView",
    "FleetRecentActivityView",
    "FleetRepoGroupView",
    "FleetResourceView",
    "FleetSearchHelperView",
    "FleetSearchHitView",
    "FleetState",
]
