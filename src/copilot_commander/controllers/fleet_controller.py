from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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

_ERROR_AGENT_STATUSES = {
    AgentStatus.ERROR,
    AgentStatus.DEAD,
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
    open_session_count: int = 0
    local_session_count: int = 0
    unclosed_local_session_count: int = 0


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
    repo_key: str | None = None
    repo_label: str | None = None
    story_key: str | None = None
    story_label: str | None = None


@dataclass(frozen=True, slots=True)
class FleetLocalSessionView:
    session_id: str
    repo_key: str
    repo_label: str
    repo_root: str | None
    summary: str
    branch: str
    worktree_name: str
    origin: str
    updated_at: datetime | None
    last_event_at: datetime | None
    last_event_type: str | None
    checkpoint_count: int
    is_cleanly_closed: bool
    is_orphan: bool
    linked_agent_id: str | None
    linked_agent_name: str | None
    token_total: int | None = None


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
class FleetStoryLaneView:
    story_key: str
    story_label: str
    repo_keys: tuple[str, ...]
    repo_labels: tuple[str, ...]
    agent_ids: tuple[str, ...]
    session_ids: tuple[str, ...]
    local_session_ids: tuple[str, ...]
    live_agent_count: int
    waiting_agent_count: int
    attention_count: int
    blocked_count: int
    open_session_count: int
    local_session_count: int
    orphan_local_session_count: int
    inbox_count: int
    latest_update_at: datetime
    next_action: str


@dataclass(frozen=True, slots=True)
class FleetInboxItemView:
    story_key: str
    story_label: str
    repo_label: str
    source_kind: str
    source_label: str
    reason: str
    occurred_at: datetime
    severity: FleetSeverity
    suggested_action: str
    agent_id: str | None = None
    session_id: str | None = None
    local_session_id: str | None = None


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
    local_sessions: tuple[FleetLocalSessionView, ...] = ()
    story_lanes: tuple[FleetStoryLaneView, ...] = ()
    response_inbox: tuple[FleetInboxItemView, ...] = ()


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


@dataclass(frozen=True, slots=True)
class _StoryIdentity:
    key: str
    label: str


@dataclass(slots=True)
class _StoryAggregate:
    label: str
    repo_keys: set[str] = field(default_factory=set)
    repo_labels: set[str] = field(default_factory=set)
    agent_ids: set[str] = field(default_factory=set)
    session_ids: set[str] = field(default_factory=set)
    local_session_ids: set[str] = field(default_factory=set)
    live_agent_count: int = 0
    waiting_agent_count: int = 0
    attention_count: int = 0
    blocked_count: int = 0
    open_session_count: int = 0
    local_session_count: int = 0
    orphan_local_session_count: int = 0
    latest_update_at: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))


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
        repo_lookup = {context.agent.id: context for context in agent_contexts}
        session_repos = {
            session.id: _repo_identity_for_session(
                session,
                contexts.get(session.id),
                repo_lookup,
            )
            for session in sessions
        }
        session_search_blobs = {
            session.id: _search_blob_for_session(session, contexts.get(session.id))
            for session in sessions
        }
        worktree_repo_keys = {
            worktree.id: _repo_identity(repo_root=worktree.repo_root)[0] for worktree in worktrees
        }
        event_repo_keys = {
            event.id: _repo_key_for_event(event, session_repos, repo_lookup) for event in events
        }
        agent_views = tuple(self._build_agent_view(item) for item in agent_contexts)
        local_session_views = self._build_local_session_views(
            local_sessions=local_sessions,
            agents=agents,
            sessions=sessions,
            repo_lookup=repo_lookup,
        )
        visible_local_sessions = self._filter_local_sessions(local_session_views, applied_filters)
        visible_agents = self._filter_agents(agent_views, applied_filters)
        visible_repo_keys = self._visible_repo_keys(
            filters=applied_filters,
            all_agents=agent_views,
            visible_agents=visible_agents,
            worktrees=worktrees,
            sessions=sessions,
            session_repos=session_repos,
            session_search_blobs=session_search_blobs,
            worktree_repo_keys=worktree_repo_keys,
            local_sessions=visible_local_sessions,
        )
        scoped_agents = tuple(
            agent for agent in visible_agents if agent.repo_key in visible_repo_keys
        )
        scoped_worktrees = tuple(
            worktree
            for worktree in worktrees
            if worktree_repo_keys[worktree.id] in visible_repo_keys
        )
        scoped_sessions = tuple(
            session for session in sessions if session_repos[session.id][0] in visible_repo_keys
        )
        scoped_events = tuple(
            event for event in events if event_repo_keys[event.id] in visible_repo_keys
        )
        scoped_local_sessions = tuple(
            session for session in visible_local_sessions if session.repo_key in visible_repo_keys
        )
        groups = self._build_groups(
            visible_agents=scoped_agents,
            all_worktrees=scoped_worktrees,
            sessions=scoped_sessions,
            session_repos=session_repos,
            local_sessions=scoped_local_sessions,
            visible_repo_keys=visible_repo_keys,
        )
        open_scoped_sessions = tuple(
            session for session in scoped_sessions if session.ended_at is None
        )
        agent_story_map, session_story_map, local_story_map = self._build_story_maps(
            agents=scoped_agents,
            sessions=scoped_sessions,
            session_repos=session_repos,
            local_sessions=scoped_local_sessions,
        )
        response_inbox = self._build_response_inbox(
            agents=scoped_agents,
            sessions=open_scoped_sessions,
            local_sessions=scoped_local_sessions,
            session_repos=session_repos,
            agent_story_map=agent_story_map,
            session_story_map=session_story_map,
            local_story_map=local_story_map,
        )
        story_lanes = self._build_story_lanes(
            agents=scoped_agents,
            sessions=open_scoped_sessions,
            local_sessions=scoped_local_sessions,
            session_repos=session_repos,
            agent_story_map=agent_story_map,
            session_story_map=session_story_map,
            local_story_map=local_story_map,
            response_inbox=response_inbox,
        )
        orphan_local_sessions = tuple(
            session for session in scoped_local_sessions if session.is_orphan
        )
        return FleetState(
            generated_at=now,
            filters=applied_filters,
            total_visible_agents=len(visible_agents),
            total_groups=len(groups),
            health=self._build_health(
                agents=scoped_agents,
                worktrees=scoped_worktrees,
                orphan_local_sessions=orphan_local_sessions,
            ),
            groups=groups,
            history_metrics=self._build_history_metrics(
                now=now,
                agents=scoped_agents,
                worktrees=scoped_worktrees,
                sessions=scoped_sessions,
                events=scoped_events,
                orphan_local_sessions=orphan_local_sessions,
                groups=groups,
            ),
            recent_activity=self._build_recent_activity(
                events=scoped_events,
                sessions=scoped_sessions,
                local_sessions=scoped_local_sessions,
                session_repos=session_repos,
                repo_lookup=repo_lookup,
                agent_story_map=agent_story_map,
                session_story_map=session_story_map,
                local_story_map=local_story_map,
                limit=activity_limit,
            ),
            search_hits=self._build_search_hits(
                query=applied_filters.normalized_query(),
                agents=agent_views,
                worktrees=worktrees,
                sessions=sessions,
                contexts=contexts,
                local_sessions=visible_local_sessions,
                limit=search_limit,
            ),
            search_helpers=self._build_search_helpers(
                agents=scoped_agents,
                worktrees=scoped_worktrees,
                orphan_local_sessions=orphan_local_sessions,
                groups=groups,
            ),
            resources=self._build_resources(
                agents=scoped_agents,
                worktrees=scoped_worktrees,
                local_sessions=scoped_local_sessions,
                orphan_local_sessions=orphan_local_sessions,
                groups=groups,
            ),
            local_sessions=scoped_local_sessions,
            story_lanes=story_lanes,
            response_inbox=response_inbox,
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

    def _build_local_session_views(
        self,
        *,
        local_sessions: Sequence[CopilotLocalSession],
        agents: Sequence[Agent],
        sessions: Sequence[Session],
        repo_lookup: dict[str, _AgentContext],
    ) -> tuple[FleetLocalSessionView, ...]:
        linked_ids = _linked_local_session_ids(sessions, agents)
        agent_by_id = {agent.id: agent for agent in agents}
        linked_agent_by_local_session_id: dict[str, Agent] = {}
        for agent in agents:
            if agent.copilot_session_id is not None:
                linked_agent_by_local_session_id.setdefault(agent.copilot_session_id, agent)
        for session in sessions:
            if (
                session.copilot_session_id is None
                or session.copilot_session_id in linked_agent_by_local_session_id
            ):
                continue
            linked_agent = agent_by_id.get(session.agent_id)
            if linked_agent is not None:
                linked_agent_by_local_session_id[session.copilot_session_id] = linked_agent

        views = [
            _local_session_view(
                session,
                linked_agent=linked_agent_by_local_session_id.get(session.session_id),
                repo_lookup=repo_lookup,
                is_orphan=session.session_id not in linked_ids,
            )
            for session in local_sessions
        ]
        return tuple(sorted(views, key=_fleet_local_session_sort_key, reverse=True))

    def _visible_repo_keys(
        self,
        *,
        filters: FleetFilterState,
        all_agents: Sequence[FleetAgentSummaryView],
        visible_agents: Sequence[FleetAgentSummaryView],
        worktrees: Sequence[Worktree],
        sessions: Sequence[Session],
        session_repos: dict[str, tuple[str | None, str | None, str | None]],
        session_search_blobs: dict[str, str],
        worktree_repo_keys: dict[str, str],
        local_sessions: Sequence[FleetLocalSessionView],
    ) -> frozenset[str]:
        query = filters.normalized_query()
        repo_keys = {agent.repo_key for agent in visible_agents}
        if query is not None:
            for worktree in worktrees:
                if query in _search_blob_for_worktree(worktree):
                    repo_keys.add(worktree_repo_keys[worktree.id])
            for session in sessions:
                repo_key, _, _ = session_repos[session.id]
                if repo_key is None or query not in session_search_blobs[session.id]:
                    continue
                repo_keys.add(repo_key)
            for local_session in local_sessions:
                if query in _search_blob_for_local_session(local_session):
                    repo_keys.add(local_session.repo_key)
        else:
            for worktree in worktrees:
                if worktree.is_dirty or worktree.locked:
                    repo_keys.add(worktree_repo_keys[worktree.id])
            if not filters.attention_only:
                for session in sessions:
                    if session.ended_at is not None:
                        continue
                    repo_key, _, _ = session_repos[session.id]
                    if repo_key is not None:
                        repo_keys.add(repo_key)
            for local_session in local_sessions:
                if local_session.is_orphan or not local_session.is_cleanly_closed:
                    repo_keys.add(local_session.repo_key)

        if not filters.attention_only:
            return frozenset(repo_keys)
        attention_repo_keys = {agent.repo_key for agent in all_agents if agent.needs_attention}
        worktree_attention_repo_keys = {
            worktree_repo_keys[worktree.id]
            for worktree in worktrees
            if worktree.is_dirty or worktree.locked
        }
        local_attention_repo_keys = {
            session.repo_key
            for session in local_sessions
            if session.is_orphan or not session.is_cleanly_closed
        }
        return frozenset(
            repo_key
            for repo_key in repo_keys
            if repo_key in attention_repo_keys
            or repo_key in worktree_attention_repo_keys
            or repo_key in local_attention_repo_keys
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

    def _filter_local_sessions(
        self,
        local_sessions: Sequence[FleetLocalSessionView],
        filters: FleetFilterState,
    ) -> tuple[FleetLocalSessionView, ...]:
        if filters.include_completed:
            return tuple(local_sessions)
        return tuple(session for session in local_sessions if not session.is_cleanly_closed)

    def _build_story_maps(
        self,
        *,
        agents: Sequence[FleetAgentSummaryView],
        sessions: Sequence[Session],
        session_repos: dict[str, tuple[str | None, str | None, str | None]],
        local_sessions: Sequence[FleetLocalSessionView],
    ) -> tuple[
        dict[str, _StoryIdentity],
        dict[str, _StoryIdentity],
        dict[str, _StoryIdentity],
    ]:
        agent_story_map = {
            agent.agent_id: _story_identity(
                title=agent.task_title,
                repo_key=agent.repo_key,
                repo_label=agent.repo_label,
            )
            for agent in agents
        }
        session_story_map: dict[str, _StoryIdentity] = {}
        for session in sessions:
            story = agent_story_map.get(session.agent_id)
            if story is None:
                repo_key, repo_label, _ = session_repos[session.id]
                story = _story_identity(
                    title=session.task_title,
                    repo_key=repo_key or "repo:unknown",
                    repo_label=repo_label or "unassigned",
                )
            session_story_map[session.id] = story
        local_story_map: dict[str, _StoryIdentity] = {}
        for local_session in local_sessions:
            story = (
                agent_story_map.get(local_session.linked_agent_id)
                if local_session.linked_agent_id is not None
                else None
            )
            if story is None:
                story = _story_identity(
                    title=local_session.summary,
                    repo_key=local_session.repo_key,
                    repo_label=local_session.repo_label,
                )
            local_story_map[local_session.session_id] = story
        return (agent_story_map, session_story_map, local_story_map)

    def _build_response_inbox(
        self,
        *,
        agents: Sequence[FleetAgentSummaryView],
        sessions: Sequence[Session],
        local_sessions: Sequence[FleetLocalSessionView],
        session_repos: dict[str, tuple[str | None, str | None, str | None]],
        agent_story_map: dict[str, _StoryIdentity],
        session_story_map: dict[str, _StoryIdentity],
        local_story_map: dict[str, _StoryIdentity],
    ) -> tuple[FleetInboxItemView, ...]:
        active_agent_ids = {
            agent.agent_id for agent in agents if agent.status in _ACTIVE_AGENT_STATUSES
        }
        items: list[FleetInboxItemView] = []
        for agent in agents:
            story = agent_story_map[agent.agent_id]
            if agent.status is AgentStatus.WAITING_INPUT:
                items.append(
                    FleetInboxItemView(
                        story_key=story.key,
                        story_label=story.label,
                        repo_label=agent.repo_label,
                        source_kind="agent",
                        source_label=agent.name,
                        reason=agent.attention_summary or "waiting for your reply",
                        occurred_at=agent.last_update_at,
                        severity="warning",
                        suggested_action="reply",
                        agent_id=agent.agent_id,
                    )
                )
                continue
            if agent.status in _ERROR_AGENT_STATUSES | {AgentStatus.BLOCKED}:
                items.append(
                    FleetInboxItemView(
                        story_key=story.key,
                        story_label=story.label,
                        repo_label=agent.repo_label,
                        source_kind="agent",
                        source_label=agent.name,
                        reason=agent.attention_summary or agent.status.value.replace("_", " "),
                        occurred_at=agent.last_update_at,
                        severity="error",
                        suggested_action="unblock",
                        agent_id=agent.agent_id,
                    )
                )
                continue
            if agent.needs_attention and agent.attention_summary is not None:
                items.append(
                    FleetInboxItemView(
                        story_key=story.key,
                        story_label=story.label,
                        repo_label=agent.repo_label,
                        source_kind="agent",
                        source_label=agent.name,
                        reason=agent.attention_summary,
                        occurred_at=agent.last_update_at,
                        severity="warning",
                        suggested_action="review",
                        agent_id=agent.agent_id,
                    )
                )
        for session in sessions:
            if session.agent_id in active_agent_ids:
                continue
            story = session_story_map[session.id]
            repo_key, repo_label, _ = session_repos[session.id]
            del repo_key
            items.append(
                FleetInboxItemView(
                    story_key=story.key,
                    story_label=story.label,
                    repo_label=repo_label or "unassigned",
                    source_kind="session",
                    source_label=session.task_title or session.copilot_session_id or session.id,
                    reason="tracked session is open without a visible live agent",
                    occurred_at=session.created_at,
                    severity="warning",
                    suggested_action="resume",
                    session_id=session.id,
                )
            )
        for local_session in local_sessions:
            story = local_story_map[local_session.session_id]
            occurred_at = local_session.updated_at or local_session.last_event_at
            if occurred_at is None:
                continue
            if local_session.is_orphan:
                items.append(
                    FleetInboxItemView(
                        story_key=story.key,
                        story_label=story.label,
                        repo_label=local_session.repo_label,
                        source_kind="local",
                        source_label=local_session.summary,
                        reason="orphan local session can be resumed or archived",
                        occurred_at=occurred_at,
                        severity="warning",
                        suggested_action="recover",
                        local_session_id=local_session.session_id,
                    )
                )
                continue
            if not local_session.is_cleanly_closed and local_session.linked_agent_id is not None:
                items.append(
                    FleetInboxItemView(
                        story_key=story.key,
                        story_label=story.label,
                        repo_label=local_session.repo_label,
                        source_kind="local",
                        source_label=local_session.summary,
                        reason="linked local session is still open on disk",
                        occurred_at=occurred_at,
                        severity="info",
                        suggested_action="continue",
                        local_session_id=local_session.session_id,
                    )
                )
        return tuple(sorted(items, key=_fleet_inbox_sort_key))

    def _build_story_lanes(
        self,
        *,
        agents: Sequence[FleetAgentSummaryView],
        sessions: Sequence[Session],
        local_sessions: Sequence[FleetLocalSessionView],
        session_repos: dict[str, tuple[str | None, str | None, str | None]],
        agent_story_map: dict[str, _StoryIdentity],
        session_story_map: dict[str, _StoryIdentity],
        local_story_map: dict[str, _StoryIdentity],
        response_inbox: Sequence[FleetInboxItemView],
    ) -> tuple[FleetStoryLaneView, ...]:
        aggregates: dict[str, _StoryAggregate] = {}

        def ensure_story(story: _StoryIdentity) -> _StoryAggregate:
            return aggregates.setdefault(story.key, _StoryAggregate(label=story.label))

        for agent in agents:
            story = agent_story_map[agent.agent_id]
            aggregate = ensure_story(story)
            aggregate.repo_keys.add(agent.repo_key)
            aggregate.repo_labels.add(agent.repo_label)
            aggregate.agent_ids.add(agent.agent_id)
            aggregate.live_agent_count += 1
            aggregate.attention_count += int(agent.needs_attention)
            aggregate.waiting_agent_count += int(agent.status is AgentStatus.WAITING_INPUT)
            aggregate.blocked_count += int(
                agent.status in _ERROR_AGENT_STATUSES | {AgentStatus.BLOCKED}
            )
            aggregate.latest_update_at = max(aggregate.latest_update_at, agent.last_update_at)

        for session in sessions:
            story = session_story_map[session.id]
            aggregate = ensure_story(story)
            repo_key, repo_label, _ = session_repos[session.id]
            if repo_key is not None:
                aggregate.repo_keys.add(repo_key)
            if repo_label is not None:
                aggregate.repo_labels.add(repo_label)
            aggregate.session_ids.add(session.id)
            aggregate.open_session_count += 1
            aggregate.latest_update_at = max(aggregate.latest_update_at, session.created_at)

        for local_session in local_sessions:
            story = local_story_map[local_session.session_id]
            aggregate = ensure_story(story)
            aggregate.repo_keys.add(local_session.repo_key)
            aggregate.repo_labels.add(local_session.repo_label)
            aggregate.local_session_ids.add(local_session.session_id)
            aggregate.local_session_count += 1
            if local_session.is_orphan:
                aggregate.orphan_local_session_count += 1
            aggregate.latest_update_at = max(
                aggregate.latest_update_at,
                _coalesce_datetime(local_session.updated_at, local_session.last_event_at),
            )

        inbox_counts: dict[str, int] = defaultdict(int)
        for item in response_inbox:
            inbox_counts[item.story_key] += 1

        story_lanes = [
            FleetStoryLaneView(
                story_key=story_key,
                story_label=aggregate.label,
                repo_keys=tuple(sorted(aggregate.repo_keys)),
                repo_labels=tuple(sorted(aggregate.repo_labels)),
                agent_ids=tuple(sorted(aggregate.agent_ids)),
                session_ids=tuple(sorted(aggregate.session_ids)),
                local_session_ids=tuple(sorted(aggregate.local_session_ids)),
                live_agent_count=aggregate.live_agent_count,
                waiting_agent_count=aggregate.waiting_agent_count,
                attention_count=aggregate.attention_count,
                blocked_count=aggregate.blocked_count,
                open_session_count=aggregate.open_session_count,
                local_session_count=aggregate.local_session_count,
                orphan_local_session_count=aggregate.orphan_local_session_count,
                inbox_count=inbox_counts.get(story_key, 0),
                latest_update_at=aggregate.latest_update_at,
                next_action=_story_next_action(aggregate, inbox_counts.get(story_key, 0)),
            )
            for story_key, aggregate in aggregates.items()
        ]
        return tuple(sorted(story_lanes, key=_fleet_story_sort_key))

    def _build_groups(
        self,
        *,
        visible_agents: Sequence[FleetAgentSummaryView],
        all_worktrees: Sequence[Worktree],
        sessions: Sequence[Session],
        session_repos: dict[str, tuple[str | None, str | None, str | None]],
        local_sessions: Sequence[FleetLocalSessionView],
        visible_repo_keys: frozenset[str],
    ) -> tuple[FleetRepoGroupView, ...]:
        grouped_agents: dict[str, list[FleetAgentSummaryView]] = defaultdict(list)
        repo_labels: dict[str, str] = {}
        repo_roots: dict[str, str | None] = {}

        def remember_repo(repo_key: str, repo_label: str, repo_root: str | None) -> None:
            repo_labels.setdefault(repo_key, repo_label)
            repo_roots.setdefault(repo_key, repo_root)

        for agent in visible_agents:
            if visible_repo_keys and agent.repo_key not in visible_repo_keys:
                continue
            grouped_agents[agent.repo_key].append(agent)
            remember_repo(agent.repo_key, agent.repo_label, agent.repo_root)

        worktree_counts: dict[str, list[Worktree]] = defaultdict(list)
        for worktree in all_worktrees:
            worktree_repo_key, worktree_repo_label = _repo_identity(repo_root=worktree.repo_root)
            if visible_repo_keys and worktree_repo_key not in visible_repo_keys:
                continue
            worktree_counts[worktree_repo_key].append(worktree)
            remember_repo(worktree_repo_key, worktree_repo_label, worktree.repo_root)

        session_counts: dict[str, set[str]] = defaultdict(set)
        open_session_counts: dict[str, int] = defaultdict(int)
        for session in sessions:
            repo_key, repo_label, repo_root = session_repos[session.id]
            if repo_key is None:
                continue
            if visible_repo_keys and repo_key not in visible_repo_keys:
                continue
            session_counts[repo_key].add(session.id)
            if session.ended_at is None:
                open_session_counts[repo_key] += 1
            if repo_label is not None:
                remember_repo(repo_key, repo_label, repo_root)

        local_session_counts: dict[str, int] = defaultdict(int)
        unclosed_local_counts: dict[str, int] = defaultdict(int)
        orphan_local_counts: dict[str, int] = defaultdict(int)
        for local_session in local_sessions:
            repo_key = local_session.repo_key
            if visible_repo_keys and repo_key not in visible_repo_keys:
                continue
            local_session_counts[repo_key] += 1
            if not local_session.is_cleanly_closed:
                unclosed_local_counts[repo_key] += 1
            if local_session.is_orphan:
                orphan_local_counts[repo_key] += 1
            remember_repo(repo_key, local_session.repo_label, local_session.repo_root)

        group_views: list[FleetRepoGroupView] = []
        candidate_repo_keys = (
            set(visible_repo_keys)
            if visible_repo_keys
            else (
                set(grouped_agents)
                | set(worktree_counts)
                | set(session_counts)
                | set(local_session_counts)
            )
        )
        for repo_key in candidate_repo_keys:
            agents = grouped_agents.get(repo_key, [])
            repo_worktrees = tuple(worktree_counts.get(repo_key, ()))
            costs = [
                agent.estimated_cost_usd for agent in agents if agent.estimated_cost_usd is not None
            ]
            group_views.append(
                FleetRepoGroupView(
                    repo_key=repo_key,
                    repo_label=repo_labels.get(repo_key, "unknown"),
                    repo_root=repo_roots.get(repo_key),
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
                    open_session_count=open_session_counts.get(repo_key, 0),
                    local_session_count=local_session_counts.get(repo_key, 0),
                    unclosed_local_session_count=unclosed_local_counts.get(repo_key, 0),
                )
            )
        return tuple(
            sorted(
                group_views,
                key=lambda item: (
                    -_group_attention_score(item),
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
        orphan_local_sessions: Sequence[FleetLocalSessionView],
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
        orphan_local_sessions: Sequence[FleetLocalSessionView],
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
        local_sessions: Sequence[FleetLocalSessionView],
        session_repos: dict[str, tuple[str | None, str | None, str | None]],
        repo_lookup: dict[str, _AgentContext],
        agent_story_map: dict[str, _StoryIdentity],
        session_story_map: dict[str, _StoryIdentity],
        local_story_map: dict[str, _StoryIdentity],
        limit: int,
    ) -> tuple[FleetRecentActivityView, ...]:
        agent_names = {context.agent.id: context.agent.name for context in repo_lookup.values()}
        session_lookup = {session.id: session for session in sessions}
        items: list[FleetRecentActivityView] = []
        for event in events:
            repo_key: str | None = None
            repo_label: str | None = None
            story_key: str | None = None
            story_label: str | None = None
            if event.agent_id is not None and event.agent_id in repo_lookup:
                repo_context = repo_lookup[event.agent_id]
                repo_key = repo_context.repo_key
                repo_label = repo_context.repo_label
                story = agent_story_map.get(event.agent_id)
                if story is not None:
                    story_key = story.key
                    story_label = story.label
            elif event.session_id is not None and event.session_id in session_lookup:
                repo_key, repo_label, _ = session_repos[event.session_id]
                story = session_story_map.get(event.session_id)
                if story is not None:
                    story_key = story.key
                    story_label = story.label
            items.append(
                FleetRecentActivityView(
                    occurred_at=event.occurred_at,
                    title=event.kind.replace("_", " "),
                    detail=agent_names.get(
                        event.agent_id or "", event.agent_id or event.session_id or "-"
                    ),
                    severity=_event_severity(event.severity),
                    repo_key=repo_key,
                    repo_label=repo_label,
                    story_key=story_key,
                    story_label=story_label,
                )
            )
        for session in sessions:
            repo_key, repo_label, _ = session_repos[session.id]
            story = session_story_map.get(session.id)
            items.append(
                FleetRecentActivityView(
                    occurred_at=session.created_at,
                    title="session started",
                    detail=session.task_title or session.copilot_session_id or session.id,
                    severity="info",
                    repo_key=repo_key,
                    repo_label=repo_label,
                    story_key=story.key if story is not None else None,
                    story_label=story.label if story is not None else None,
                )
            )
            if session.ended_at is not None:
                items.append(
                    FleetRecentActivityView(
                        occurred_at=session.ended_at,
                        title="session ended",
                        detail=session.exit_reason or session.id,
                        severity="warning",
                        repo_key=repo_key,
                        repo_label=repo_label,
                        story_key=story.key if story is not None else None,
                        story_label=story.label if story is not None else None,
                    )
                )
        for local_session in local_sessions:
            occurred_at = local_session.updated_at or local_session.last_event_at
            if occurred_at is None:
                continue
            story = local_story_map.get(local_session.session_id)
            items.append(
                FleetRecentActivityView(
                    occurred_at=occurred_at,
                    title="local session updated",
                    detail=local_session.summary,
                    severity="info" if local_session.is_cleanly_closed else "warning",
                    repo_key=local_session.repo_key,
                    repo_label=local_session.repo_label,
                    story_key=story.key if story is not None else None,
                    story_label=story.label if story is not None else None,
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
        local_sessions: Sequence[FleetLocalSessionView],
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
                        title=local_session.summary,
                        detail=(
                            f"{local_session.repo_label} · {local_session.branch} · "
                            f"{'orphan' if local_session.is_orphan else local_session.origin}"
                        ),
                    )
                )
        return tuple(hits[:limit])

    def _build_search_helpers(
        self,
        *,
        agents: Sequence[FleetAgentSummaryView],
        worktrees: Sequence[Worktree],
        orphan_local_sessions: Sequence[FleetLocalSessionView],
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
        local_sessions: Sequence[FleetLocalSessionView],
        orphan_local_sessions: Sequence[FleetLocalSessionView],
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
                detail="visible repo buckets",
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
                detail=f"{session_link_count} linked · {active_status_count} active",
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


def _repo_identity_for_session(
    session: Session,
    context: SessionContextRecord | None,
    repo_lookup: dict[str, _AgentContext],
) -> tuple[str | None, str | None, str | None]:
    if context is not None and context.repo_root is not None:
        repo_key, repo_label = _repo_identity(repo_root=context.repo_root)
        return (repo_key, repo_label, context.repo_root)
    repo_context = repo_lookup.get(session.agent_id)
    if repo_context is not None:
        return (repo_context.repo_key, repo_context.repo_label, repo_context.repo_root)
    return (None, None, None)


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
    return ("repo:unknown", "unassigned")


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


def _search_blob_for_local_session(session: FleetLocalSessionView) -> str:
    return " ".join(
        part.lower()
        for part in (
            session.session_id,
            session.summary,
            session.repo_label,
            session.repo_root or "",
            session.branch,
            session.worktree_name,
            session.linked_agent_name or "",
            session.last_event_type or "",
            session.origin,
            "orphan" if session.is_orphan else "linked",
            "completed" if session.is_cleanly_closed else "unclosed",
        )
        if part
    )


def _repo_key_for_event(
    event: Event,
    session_repos: dict[str, tuple[str | None, str | None, str | None]],
    repo_lookup: dict[str, _AgentContext],
) -> str | None:
    if event.agent_id is not None and event.agent_id in repo_lookup:
        return repo_lookup[event.agent_id].repo_key
    if event.session_id is not None and event.session_id in session_repos:
        repo_key, _, _ = session_repos[event.session_id]
        return repo_key
    return None


def _event_severity(value: str) -> FleetSeverity:
    if value == "error":
        return "error"
    if value == "warning":
        return "warning"
    return "info"


def _coalesce_datetime(*values: datetime | None) -> datetime:
    for value in values:
        if value is not None:
            return value
    return datetime.min.replace(tzinfo=UTC)


def _fleet_local_session_sort_key(session: FleetLocalSessionView) -> tuple[datetime, str]:
    return (
        _coalesce_datetime(session.updated_at, session.last_event_at),
        session.session_id,
    )


def _local_session_view(
    session: CopilotLocalSession,
    *,
    linked_agent: Agent | None,
    repo_lookup: dict[str, _AgentContext],
    is_orphan: bool,
) -> FleetLocalSessionView:
    repo_root = str(session.git_root) if session.git_root is not None else None
    repo_key, repo_label = _repo_identity(
        repo_root=repo_root,
        repository=session.repository,
    )
    if repo_key == "repo:unknown" and linked_agent is not None:
        linked_repo = repo_lookup.get(linked_agent.id)
        if linked_repo is not None:
            repo_key = linked_repo.repo_key
            repo_label = linked_repo.repo_label
            repo_root = linked_repo.repo_root
    token_total = session.usage.total_tokens if session.usage is not None else None
    return FleetLocalSessionView(
        session_id=session.session_id,
        repo_key=repo_key,
        repo_label=repo_label,
        repo_root=repo_root,
        summary=session.summary or session.session_id,
        branch=session.branch or "-",
        worktree_name=_path_name(str(session.cwd) if session.cwd is not None else None),
        origin=session.origin,
        updated_at=session.updated_at,
        last_event_at=session.last_event_at,
        last_event_type=session.last_event_type,
        checkpoint_count=session.checkpoint_count,
        is_cleanly_closed=session.is_cleanly_closed,
        is_orphan=is_orphan,
        linked_agent_id=linked_agent.id if linked_agent is not None else None,
        linked_agent_name=linked_agent.name if linked_agent is not None else None,
        token_total=token_total,
    )


def _group_attention_score(group: FleetRepoGroupView) -> int:
    return (
        group.attention_count
        + group.dirty_worktree_count
        + group.locked_worktree_count
        + group.orphan_local_session_count
        + group.unclosed_local_session_count
    )


def _normalized_story_key(label: str) -> str:
    collapsed = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return collapsed or "story"


def _meaningful_story_title(value: str | None) -> str | None:
    if value is None:
        return None
    label = value.strip()
    if not label or label == "(no task title)":
        return None
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", label):
        return None
    return label


def _story_identity(*, title: str | None, repo_key: str, repo_label: str) -> _StoryIdentity:
    story_title = _meaningful_story_title(title)
    if story_title is not None:
        return _StoryIdentity(
            key=f"story:{_normalized_story_key(story_title)}",
            label=story_title,
        )
    return _StoryIdentity(
        key=f"repo-story:{repo_key}",
        label=f"{repo_label} focus",
    )


def _story_next_action(aggregate: _StoryAggregate, inbox_count: int) -> str:
    if aggregate.waiting_agent_count:
        return "reply"
    if aggregate.blocked_count:
        return "unblock"
    if aggregate.orphan_local_session_count:
        return "recover"
    if aggregate.open_session_count and aggregate.live_agent_count == 0:
        return "resume"
    if inbox_count:
        return "review"
    return "monitor"


def _fleet_story_sort_key(story: FleetStoryLaneView) -> tuple[int, float, str]:
    return (
        -(
            story.waiting_agent_count * 5
            + story.blocked_count * 4
            + story.attention_count * 3
            + story.orphan_local_session_count * 3
            + min(story.open_session_count, 3) * 2
            + story.inbox_count
        ),
        -story.latest_update_at.timestamp(),
        story.story_label.lower(),
    )


def _fleet_inbox_sort_key(item: FleetInboxItemView) -> tuple[int, float, str]:
    severity_rank = {
        "error": 0,
        "warning": 1,
        "info": 2,
    }
    return (
        severity_rank[item.severity],
        -item.occurred_at.timestamp(),
        item.story_label.lower(),
    )


__all__ = [
    "FleetAgentSummaryView",
    "FleetController",
    "FleetFilterState",
    "FleetHealthSummary",
    "FleetHistoryMetricView",
    "FleetInboxItemView",
    "FleetLocalSessionView",
    "FleetRecentActivityView",
    "FleetRepoGroupView",
    "FleetResourceView",
    "FleetSearchHelperView",
    "FleetSearchHitView",
    "FleetState",
    "FleetStoryLaneView",
]
