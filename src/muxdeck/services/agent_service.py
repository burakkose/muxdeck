from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol, cast, runtime_checkable

from muxdeck.adapters.sqlite_store import SessionContextRecord
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.events import Event, LogChunk
from muxdeck.domain.models import Agent, Session
from muxdeck.domain.value_objects import (
    AgentId,
    EventId,
    LogChunkId,
    SessionId,
    ensure_aware_datetime,
    utc_now,
)
from muxdeck.types import Clock, EventStore, LogChunkStore, SessionStore

PaneClassification = Literal["managed_agent", "unmanaged_probable_agent"]
EventSeverity = Literal["debug", "info", "warning", "error"]
LogSource = Literal["tmux_capture", "stdout", "stderr", "system"]


@runtime_checkable
class AgentStateStore(Protocol):
    def upsert_agent(self, agent: Agent, /) -> None:
        """Persist an agent snapshot."""

    def get_agent(self, agent_id: str, /) -> Agent | None:
        """Return an agent by identifier."""

    def get_agent_by_pane_id(self, pane_id: str, /) -> Agent | None:
        """Return the latest agent associated with a tmux pane."""

    def get_agent_by_copilot_session_id(self, copilot_session_id: str, /) -> Agent | None:
        """Return the latest agent associated with a Copilot session."""


@runtime_checkable
class AgentSessionStore(SessionStore, Protocol):
    def get_session_by_copilot_session_id(self, copilot_session_id: str, /) -> Session | None:
        """Return a session by Copilot session identifier."""

    # NOTE: ``get_open_session_for_agent`` is an optional fast-path that
    # ``AgentService`` discovers via ``getattr`` (see
    # ``_find_current_session``). It is intentionally *not* declared on
    # this Protocol so legacy in-memory fakes and partial stores remain
    # structural matches without having to implement it.


@runtime_checkable
class AgentContextStore(Protocol):
    def upsert_session_context(self, context: SessionContextRecord, /) -> None:
        """Persist session context metadata."""

    def get_session_context(self, session_id: str, /) -> SessionContextRecord | None:
        """Return session context by session identifier."""

    def get_session_context_by_tmux_pane_id(
        self,
        tmux_pane_id: str,
        /,
    ) -> SessionContextRecord | None:
        """Return session context by tmux pane identifier."""


@runtime_checkable
class AgentEventStore(EventStore, Protocol):
    def list_events_for_session(self, session_id: str, /) -> Sequence[Event]:
        """Return persisted session events in chronological order."""


@runtime_checkable
class AgentLogStore(LogChunkStore, Protocol):
    # NOTE: ``upsert_log_capture_if_changed`` is an optional fast-path
    # discovered via ``getattr`` in ``persist_agent_facts`` (see
    # ``AgentService._append_log_chunk_if_changed``). It is intentionally
    # *not* declared on this Protocol so partial stores (test fakes,
    # in-memory stubs) keep matching structurally without needing to
    # implement the broader signature exposed by ``SQLiteStore``.
    pass


@dataclass(frozen=True, slots=True)
class AgentFactInput:
    classification: PaneClassification
    tmux_session_name: str
    tmux_window_id: str
    tmux_pane_id: str
    observed_at: datetime
    cwd: str
    status: AgentStatus
    tmux_window_name: str | None = None
    pane_tty: str | None = None
    repo_root: str | None = None
    worktree_path: str | None = None
    branch: str | None = None
    name: str | None = None
    task_title: str | None = None
    task_summary: str | None = None
    copilot_session_id: str | None = None
    pid: int | None = None
    last_activity_at: datetime | None = None
    idle_seconds: int = 0
    needs_attention: bool = False
    attention_reason: str | None = None
    token_input: int | None = None
    token_output: int | None = None
    token_total: int | None = None
    estimated_cost_usd: Decimal | None = None
    capture_text: str | None = None
    blocking_issue_kinds: tuple[str, ...] = ()
    error_messages: tuple[str, ...] = ()
    source: LogSource = "tmux_capture"
    agent_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            ensure_aware_datetime(self.observed_at, field_name="observed_at"),
        )
        if self.last_activity_at is not None:
            object.__setattr__(
                self,
                "last_activity_at",
                ensure_aware_datetime(self.last_activity_at, field_name="last_activity_at"),
            )
        if self.capture_text is not None:
            normalized_capture = self.capture_text.rstrip()
            object.__setattr__(self, "capture_text", normalized_capture or None)


@dataclass(frozen=True, slots=True)
class AgentRecordResult:
    agent: Agent
    session: Session
    created_agent: bool
    created_session: bool
    events: tuple[Event, ...] = ()
    log_chunks: tuple[LogChunk, ...] = ()


@dataclass(frozen=True, slots=True)
class _EventDraft:
    kind: str
    payload_json: str
    severity: EventSeverity = "info"


class AgentService:
    def __init__(
        self,
        agent_store: AgentStateStore,
        session_store: AgentSessionStore,
        event_store: AgentEventStore,
        log_store: AgentLogStore,
        context_store: AgentContextStore,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._agent_store = agent_store
        self._session_store = session_store
        self._event_store = event_store
        self._log_store = log_store
        self._context_store = context_store
        self._clock = clock

    def persist_agent_facts(self, facts: AgentFactInput, /) -> AgentRecordResult:
        existing_agent = self._resolve_existing_agent(facts)
        agent = self._build_agent(facts, existing=existing_agent)
        self._agent_store.upsert_agent(agent)

        session, created_session = self._ensure_session(agent, facts)
        self._context_store.upsert_session_context(
            SessionContextRecord(
                session_id=session.id,
                agent_id=agent.id,
                tmux_pane_id=agent.tmux_pane_id,
                pane_tty=agent.pane_tty,
                worktree_path=agent.worktree_path,
                copilot_session_id=agent.copilot_session_id,
                repo_root=agent.repo_root,
                branch=agent.branch,
                updated_at=facts.observed_at,
            )
        )

        event_drafts = self._event_drafts(agent, session, facts, existing_agent, created_session)
        events = self._append_events(agent.id, session.id, facts.observed_at, event_drafts)
        log_chunks = self._append_log_chunk_if_changed(agent.id, session.id, facts)
        return AgentRecordResult(
            agent=agent,
            session=session,
            created_agent=existing_agent is None,
            created_session=created_session,
            events=events,
            log_chunks=log_chunks,
        )

    def _resolve_existing_agent(self, facts: AgentFactInput, /) -> Agent | None:
        if facts.agent_id is not None:
            existing = self._agent_store.get_agent(facts.agent_id)
            if existing is not None:
                return existing
        existing = self._agent_store.get_agent_by_pane_id(facts.tmux_pane_id)
        if existing is not None:
            return existing
        if facts.copilot_session_id is None:
            return None
        return self._agent_store.get_agent_by_copilot_session_id(facts.copilot_session_id)

    def _build_agent(self, facts: AgentFactInput, /, *, existing: Agent | None) -> Agent:
        observed_at = facts.observed_at
        agent_name = self._pick_required(
            facts.name,
            existing.name if existing is not None else None,
            facts.task_title,
            facts.tmux_window_name,
            f"pane-{facts.tmux_pane_id.removeprefix('%') or facts.tmux_pane_id}",
        )
        cwd = self._pick_required(
            facts.cwd,
            facts.worktree_path,
            facts.repo_root,
            existing.cwd if existing is not None else None,
            "/",
        )
        last_activity_at = facts.last_activity_at
        if last_activity_at is None and existing is not None:
            last_activity_at = existing.last_activity_at
        return Agent(
            id=existing.id if existing is not None else facts.agent_id or str(AgentId.generate()),
            name=agent_name,
            tmux_session_name=facts.tmux_session_name,
            tmux_window_id=facts.tmux_window_id,
            tmux_window_name=self._pick_optional(
                facts.tmux_window_name,
                existing.tmux_window_name if existing else None,
            ),
            tmux_pane_id=facts.tmux_pane_id,
            pane_tty=self._pick_optional(facts.pane_tty, existing.pane_tty if existing else None),
            cwd=cwd,
            repo_root=self._pick_optional(
                facts.repo_root,
                existing.repo_root if existing else None,
            ),
            worktree_path=self._pick_optional(
                facts.worktree_path,
                existing.worktree_path if existing is not None else None,
                facts.cwd,
            ),
            branch=self._pick_optional(facts.branch, existing.branch if existing else None),
            task_title=self._pick_optional(
                facts.task_title,
                existing.task_title if existing else None,
            ),
            task_summary=self._pick_optional(
                facts.task_summary,
                existing.task_summary if existing else None,
            ),
            copilot_session_id=self._pick_optional(
                facts.copilot_session_id,
                existing.copilot_session_id if existing else None,
            ),
            pid=(
                facts.pid
                if facts.pid is not None
                else existing.pid
                if existing is not None
                else None
            ),
            status=facts.status,
            started_at=existing.started_at if existing is not None else observed_at,
            last_activity_at=last_activity_at,
            last_seen_at=observed_at,
            idle_seconds=facts.idle_seconds,
            needs_attention=facts.needs_attention,
            attention_reason=facts.attention_reason,
            token_input=(
                facts.token_input
                if facts.token_input is not None
                else existing.token_input
                if existing
                else None
            ),
            token_output=(
                facts.token_output
                if facts.token_output is not None
                else existing.token_output
                if existing
                else None
            ),
            token_total=(
                facts.token_total
                if facts.token_total is not None
                else existing.token_total
                if existing
                else None
            ),
            estimated_cost_usd=(
                facts.estimated_cost_usd
                if facts.estimated_cost_usd is not None
                else existing.estimated_cost_usd
                if existing is not None
                else None
            ),
        )

    def _ensure_session(self, agent: Agent, facts: AgentFactInput, /) -> tuple[Session, bool]:
        current = self._find_current_session(agent, facts)
        if current is None:
            session = Session(
                id=str(SessionId.generate()),
                agent_id=agent.id,
                copilot_session_id=facts.copilot_session_id,
                task_title=facts.task_title,
                created_at=facts.observed_at,
            )
            self._session_store.upsert_session(session)
            return session, True
        updated = Session(
            id=current.id,
            agent_id=agent.id,
            copilot_session_id=self._pick_optional(
                facts.copilot_session_id,
                current.copilot_session_id,
            ),
            task_title=self._pick_optional(facts.task_title, current.task_title),
            created_at=current.created_at,
            ended_at=current.ended_at,
            exit_reason=current.exit_reason,
        )
        self._session_store.upsert_session(updated)
        return updated, False

    def _find_current_session(self, agent: Agent, facts: AgentFactInput, /) -> Session | None:
        if facts.copilot_session_id is not None:
            session = self._session_store.get_session_by_copilot_session_id(
                facts.copilot_session_id
            )
            if session is not None:
                return session
        context = self._context_store.get_session_context_by_tmux_pane_id(agent.tmux_pane_id)
        if context is not None:
            session = self._session_store.get_session(context.session_id)
            if session is not None:
                return session
        open_session = self._lookup_open_session_for_agent(agent.id)
        if open_session is not None:
            return open_session
        sessions = tuple(self._session_store.list_sessions(agent.id))
        if not sessions:
            return None
        open_sessions = tuple(session for session in sessions if session.ended_at is None)
        return open_sessions[0] if open_sessions else sessions[0]

    def _lookup_open_session_for_agent(self, agent_id: str, /) -> Session | None:
        """Use the indexed ``get_open_session_for_agent`` when available.

        Falls back to ``None`` (which triggers the legacy
        ``list_sessions(agent_id)`` scan in
        :meth:`_find_current_session`) when the store port does not
        expose the optimised method, keeping older fakes / partial
        protocol implementations working.
        """
        getter = getattr(self._session_store, "get_open_session_for_agent", None)
        if not callable(getter):
            return None
        result = getter(agent_id)
        return cast("Session | None", result)

    def _event_drafts(
        self,
        agent: Agent,
        session: Session,
        facts: AgentFactInput,
        existing: Agent | None,
        created_session: bool,
    ) -> tuple[_EventDraft, ...]:
        drafts: list[_EventDraft] = []
        if existing is None:
            drafts.append(
                _EventDraft(
                    kind="agent.discovered",
                    payload_json=self._json_payload(
                        classification=facts.classification,
                        pane_id=agent.tmux_pane_id,
                    ),
                )
            )
        elif existing.status != agent.status or existing.attention_reason != agent.attention_reason:
            drafts.append(
                _EventDraft(
                    kind="agent.status.changed",
                    payload_json=self._json_payload(
                        from_status=existing.status.value,
                        to_status=agent.status.value,
                        attention_reason=agent.attention_reason,
                    ),
                    severity="warning" if agent.needs_attention else "info",
                )
            )
        existing_copilot_session_id = existing.copilot_session_id if existing else None
        if created_session or (
            facts.copilot_session_id is not None
            and facts.copilot_session_id != existing_copilot_session_id
        ):
            drafts.append(
                _EventDraft(
                    kind="agent.session.observed",
                    payload_json=self._json_payload(
                        session_id=session.id,
                        copilot_session_id=session.copilot_session_id,
                    ),
                )
            )
        for issue_kind in facts.blocking_issue_kinds:
            drafts.append(
                _EventDraft(
                    kind="agent.blocking_issue",
                    payload_json=self._json_payload(kind=issue_kind),
                    severity="warning",
                )
            )
        for message in facts.error_messages:
            drafts.append(
                _EventDraft(
                    kind="agent.error_detected",
                    payload_json=self._json_payload(message=message),
                    severity="error",
                )
            )
        return tuple(drafts)

    def _append_events(
        self,
        agent_id: str,
        session_id: str,
        occurred_at: datetime,
        drafts: Sequence[_EventDraft],
    ) -> tuple[Event, ...]:
        if not drafts:
            return ()
        events = tuple(
            Event(
                id=str(EventId.generate()),
                occurred_at=occurred_at,
                agent_id=agent_id,
                session_id=session_id,
                kind=draft.kind,
                severity=draft.severity,
                payload_json=draft.payload_json,
            )
            for draft in drafts
        )
        self._event_store.append_events(events)
        return events

    def _append_log_chunk_if_changed(
        self,
        agent_id: str,
        session_id: str,
        facts: AgentFactInput,
    ) -> tuple[LogChunk, ...]:
        if facts.capture_text is None:
            return ()
        # Prefer the transactional upsert when the store exposes it
        # (A3): bundles the existing dedup read and the insert into a
        # single BEGIN/COMMIT, halving the fsync count for every
        # changed capture on the hot path.
        upsert = getattr(self._log_store, "upsert_log_capture_if_changed", None)
        if callable(upsert):
            chunk = upsert(
                agent_id=agent_id,
                session_id=session_id,
                source=facts.source,
                content=facts.capture_text,
                captured_at=facts.observed_at,
            )
            return (chunk,) if chunk is not None else ()
        # Only the tail matters: we need the previous content to detect
        # a no-op append, and the previous ``sequence_no`` to compute the
        # next one. Pulling every chunk for the session here used to cost
        # ~2 s per refresh on long-lived sessions (26k+ chunks observed
        # in the wild) just to inspect the last row.
        latest = self._log_store.get_latest_log_chunk(session_id)
        if latest is not None and latest.content == facts.capture_text:
            return ()
        next_sequence = latest.sequence_no + 1 if latest is not None else 0
        chunk = LogChunk(
            id=str(LogChunkId.generate()),
            agent_id=agent_id,
            session_id=session_id,
            source=facts.source,
            sequence_no=next_sequence,
            captured_at=facts.observed_at,
            content=facts.capture_text,
        )
        self._log_store.append_log_chunks((chunk,))
        return (chunk,)

    def _pick_optional(self, *values: str | None) -> str | None:
        for value in values:
            if value is not None and value.strip():
                return value
        return None

    def _pick_required(self, *values: str | None) -> str:
        value = self._pick_optional(*values)
        return "unknown" if value is None else value

    def _json_payload(self, /, **payload: str | None) -> str:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)
