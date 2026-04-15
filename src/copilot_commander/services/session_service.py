from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from copilot_commander.adapters.sqlite_store import SessionContextRecord
from copilot_commander.domain.events import Event, LogChunk
from copilot_commander.domain.models import Agent, Session, Worktree
from copilot_commander.domain.value_objects import utc_now
from copilot_commander.exceptions import DomainValidationError, PersistenceError
from copilot_commander.types import Clock, JsonValue


class SessionStorePort(Protocol):
    def get_agent(self, agent_id: str, /) -> Agent | None: ...

    def get_worktree(self, worktree_id: str, /) -> Worktree | None: ...

    def get_worktree_by_path(self, path: str, /) -> Worktree | None: ...

    def upsert_session(self, session: Session, /) -> None: ...

    def get_session(self, session_id: str, /) -> Session | None: ...

    def get_session_by_copilot_session_id(self, copilot_session_id: str, /) -> Session | None: ...

    def get_session_by_tmux_pane_id(self, tmux_pane_id: str, /) -> Session | None: ...

    def append_events(self, events: Sequence[Event], /) -> None: ...

    def append_log_chunks(self, chunks: Sequence[LogChunk], /) -> None: ...

    def list_log_chunks(self, session_id: str, /) -> Sequence[LogChunk]: ...

    def get_session_context(self, session_id: str, /) -> SessionContextRecord | None: ...

    def upsert_session_context(self, context: SessionContextRecord, /) -> None: ...


@dataclass(frozen=True, slots=True)
class SessionContextPatch:
    agent_id: str | None = None
    worktree_id: str | None = None
    tmux_pane_id: str | None = None
    pane_tty: str | None = None
    worktree_path: str | None = None
    copilot_session_id: str | None = None
    repo_root: str | None = None
    branch: str | None = None


@dataclass(frozen=True, slots=True)
class SessionContextView:
    session: Session
    context: SessionContextRecord
    agent: Agent | None = None
    worktree: Worktree | None = None


@dataclass(frozen=True, slots=True)
class SessionBundle(SessionContextView):
    pass


@dataclass(frozen=True, slots=True)
class SessionReplayLookup(SessionContextView):
    pass


class SessionService:
    def __init__(
        self,
        *,
        store: SessionStorePort,
        clock: Clock = utc_now,
    ) -> None:
        self._store = store
        self._clock = clock

    def create_session(
        self,
        agent_id: str,
        *,
        task_title: str | None = None,
        copilot_session_id: str | None = None,
        context: SessionContextPatch | None = None,
        occurred_at: datetime | None = None,
    ) -> SessionBundle:
        agent = self._require_agent(agent_id)
        timestamp = occurred_at or self._clock()
        session = Session(
            agent_id=agent.id,
            copilot_session_id=copilot_session_id or agent.copilot_session_id,
            task_title=task_title or agent.task_title,
            created_at=timestamp,
        )
        self._store.upsert_session(session)
        session_context = self._build_context(session=session, agent=agent, patch=context)
        self._store.upsert_session_context(session_context)
        self._store.append_events(
            (
                Event(
                    occurred_at=timestamp,
                    agent_id=session.agent_id,
                    session_id=session.id,
                    kind="session.created",
                    payload_json=self._payload_json(
                        raw_evidence={
                            "task_title": task_title,
                            "copilot_session_id": copilot_session_id,
                            "context_patch": self._patch_dict(context),
                        },
                        derived_fact={
                            "session_id": session.id,
                            "agent_id": session.agent_id,
                            "worktree_id": session_context.worktree_id,
                            "tmux_pane_id": session_context.tmux_pane_id,
                        },
                    ),
                ),
            )
        )
        return self.assemble_session_context(session.id)

    def update_session(
        self,
        session_id: str,
        *,
        copilot_session_id: str | None = None,
        task_title: str | None = None,
        context_patch: SessionContextPatch | None = None,
        events: Sequence[Event] = (),
        log_chunks: Sequence[LogChunk] = (),
    ) -> SessionBundle:
        existing = self._require_session(session_id)
        updated = Session(
            id=existing.id,
            agent_id=existing.agent_id,
            copilot_session_id=(
                copilot_session_id
                if copilot_session_id is not None
                else existing.copilot_session_id
            ),
            task_title=task_title if task_title is not None else existing.task_title,
            created_at=existing.created_at,
            ended_at=existing.ended_at,
            exit_reason=existing.exit_reason,
        )
        self._store.upsert_session(updated)
        agent = self._store.get_agent(updated.agent_id)
        current_context = self._store.get_session_context(session_id)
        next_context = self._build_context(
            session=updated,
            agent=agent,
            patch=context_patch,
            existing=current_context,
        )
        context_changed = current_context != next_context
        if context_changed:
            self._store.upsert_session_context(next_context)
        normalized_logs = self._normalize_log_chunks(updated, log_chunks)
        if normalized_logs:
            self._store.append_log_chunks(normalized_logs)
        normalized_events = list(self._normalize_events(updated, events))
        if context_changed:
            normalized_events.insert(
                0,
                Event(
                    occurred_at=self._clock(),
                    agent_id=updated.agent_id,
                    session_id=updated.id,
                    kind="session.context.updated",
                    payload_json=self._payload_json(
                        raw_evidence={"context_patch": self._patch_dict(context_patch)},
                        derived_fact={
                            "worktree_id": next_context.worktree_id,
                            "tmux_pane_id": next_context.tmux_pane_id,
                            "copilot_session_id": next_context.copilot_session_id,
                        },
                    ),
                ),
            )
        if normalized_events:
            self._store.append_events(tuple(normalized_events))
        return self.assemble_session_context(updated.id)

    def end_session(
        self,
        session_id: str,
        *,
        exit_reason: str,
        ended_at: datetime | None = None,
        final_events: Sequence[Event] = (),
        final_log_chunks: Sequence[LogChunk] = (),
    ) -> SessionBundle:
        existing = self._require_session(session_id)
        finished_at = ended_at or self._clock()
        updated = Session(
            id=existing.id,
            agent_id=existing.agent_id,
            copilot_session_id=existing.copilot_session_id,
            task_title=existing.task_title,
            created_at=existing.created_at,
            ended_at=finished_at,
            exit_reason=exit_reason,
        )
        self._store.upsert_session(updated)
        normalized_logs = self._normalize_log_chunks(updated, final_log_chunks)
        if normalized_logs:
            self._store.append_log_chunks(normalized_logs)
        normalized_events = list(self._normalize_events(updated, final_events))
        normalized_events.append(
            Event(
                occurred_at=finished_at,
                agent_id=updated.agent_id,
                session_id=updated.id,
                kind="session.ended",
                payload_json=self._payload_json(
                    raw_evidence={"exit_reason": exit_reason},
                    derived_fact={"ended_at": finished_at.isoformat()},
                ),
            )
        )
        self._store.append_events(tuple(normalized_events))
        return self.assemble_session_context(updated.id)

    def append_events(self, session_id: str, events: Sequence[Event]) -> tuple[Event, ...]:
        session = self._require_session(session_id)
        normalized = self._normalize_events(session, events)
        if normalized:
            self._store.append_events(normalized)
        return normalized

    def append_log_capture(
        self,
        session_id: str,
        *,
        source: Literal["tmux_capture", "stdout", "stderr", "system"],
        content_blocks: Sequence[str],
        captured_at: datetime | None = None,
        agent_id: str | None = None,
    ) -> tuple[LogChunk, ...]:
        session = self._require_session(session_id)
        effective_agent_id = agent_id or session.agent_id
        if effective_agent_id != session.agent_id:
            msg = (
                f"session {session_id} belongs to agent {session.agent_id}, "
                f"not {effective_agent_id}"
            )
            raise DomainValidationError(msg)
        next_sequence = self._next_sequence_no(session_id)
        timestamp = captured_at or self._clock()
        chunks: list[LogChunk] = []
        for index, content in enumerate(content_blocks):
            stripped = content.strip("\n")
            if not stripped:
                continue
            chunks.append(
                LogChunk(
                    agent_id=session.agent_id,
                    session_id=session.id,
                    source=source,
                    sequence_no=next_sequence + index,
                    captured_at=timestamp,
                    content=stripped,
                )
            )
        if chunks:
            self._store.append_log_chunks(tuple(chunks))
        return tuple(chunks)

    def assemble_session_context(self, session_id: str) -> SessionBundle:
        session = self._require_session(session_id)
        context = self._store.get_session_context(session_id)
        agent = self._store.get_agent(session.agent_id)
        if context is None:
            context = self._build_context(session=session, agent=agent, patch=None)
            self._store.upsert_session_context(context)
        worktree = self._resolve_worktree(context)
        return SessionBundle(session=session, context=context, agent=agent, worktree=worktree)

    def lookup_for_replay(
        self,
        *,
        session_id: str | None = None,
        copilot_session_id: str | None = None,
        tmux_pane_id: str | None = None,
    ) -> SessionReplayLookup | None:
        locators = [
            value for value in (session_id, copilot_session_id, tmux_pane_id) if value is not None
        ]
        if len(locators) != 1:
            msg = "exactly one session locator must be provided"
            raise DomainValidationError(msg)
        if session_id is not None:
            session = self._store.get_session(session_id)
        elif copilot_session_id is not None:
            session = self._store.get_session_by_copilot_session_id(copilot_session_id)
        else:
            session = self._store.get_session_by_tmux_pane_id(tmux_pane_id or "")
        if session is None:
            return None
        bundle = self.assemble_session_context(session.id)
        return SessionReplayLookup(
            session=bundle.session,
            context=bundle.context,
            agent=bundle.agent,
            worktree=bundle.worktree,
        )

    def _build_context(
        self,
        *,
        session: Session,
        agent: Agent | None,
        patch: SessionContextPatch | None,
        existing: SessionContextRecord | None = None,
    ) -> SessionContextRecord:
        worktree = self._resolve_worktree_from_inputs(agent=agent, patch=patch, existing=existing)
        return SessionContextRecord(
            session_id=session.id,
            agent_id=session.agent_id,
            worktree_id=self._first_non_empty(
                patch.worktree_id if patch is not None else None,
                worktree.id if worktree is not None else None,
                existing.worktree_id if existing is not None else None,
            ),
            tmux_pane_id=self._first_non_empty(
                patch.tmux_pane_id if patch is not None else None,
                agent.tmux_pane_id if agent is not None else None,
                existing.tmux_pane_id if existing is not None else None,
            ),
            pane_tty=self._first_non_empty(
                patch.pane_tty if patch is not None else None,
                agent.pane_tty if agent is not None else None,
                existing.pane_tty if existing is not None else None,
            ),
            worktree_path=self._first_non_empty(
                patch.worktree_path if patch is not None else None,
                worktree.path if worktree is not None else None,
                agent.worktree_path if agent is not None else None,
                existing.worktree_path if existing is not None else None,
            ),
            copilot_session_id=self._first_non_empty(
                patch.copilot_session_id if patch is not None else None,
                session.copilot_session_id,
                existing.copilot_session_id if existing is not None else None,
            ),
            repo_root=self._first_non_empty(
                patch.repo_root if patch is not None else None,
                worktree.repo_root if worktree is not None else None,
                agent.repo_root if agent is not None else None,
                existing.repo_root if existing is not None else None,
            ),
            branch=self._first_non_empty(
                patch.branch if patch is not None else None,
                worktree.branch if worktree is not None else None,
                agent.branch if agent is not None else None,
                existing.branch if existing is not None else None,
            ),
            updated_at=self._clock(),
        )

    def _normalize_events(self, session: Session, events: Sequence[Event]) -> tuple[Event, ...]:
        normalized: list[Event] = []
        for event in events:
            if event.session_id is not None and event.session_id != session.id:
                msg = f"event {event.id} belongs to session {event.session_id}, not {session.id}"
                raise DomainValidationError(msg)
            if event.agent_id is not None and event.agent_id != session.agent_id:
                msg = f"event {event.id} belongs to agent {event.agent_id}, not {session.agent_id}"
                raise DomainValidationError(msg)
            normalized.append(
                Event(
                    id=event.id,
                    occurred_at=event.occurred_at,
                    agent_id=session.agent_id,
                    session_id=session.id,
                    kind=event.kind,
                    severity=event.severity,
                    payload_json=event.payload_json,
                )
            )
        return tuple(normalized)

    def _normalize_log_chunks(
        self,
        session: Session,
        log_chunks: Sequence[LogChunk],
    ) -> tuple[LogChunk, ...]:
        normalized: list[LogChunk] = []
        next_sequence = self._next_sequence_no(session.id)
        for index, chunk in enumerate(log_chunks):
            if chunk.session_id is not None and chunk.session_id != session.id:
                msg = (
                    f"log chunk {chunk.id} belongs to session {chunk.session_id}, not {session.id}"
                )
                raise DomainValidationError(msg)
            if chunk.agent_id != session.agent_id:
                msg = (
                    f"log chunk {chunk.id} belongs to agent "
                    f"{chunk.agent_id}, not {session.agent_id}"
                )
                raise DomainValidationError(msg)
            normalized.append(
                LogChunk(
                    id=chunk.id,
                    agent_id=session.agent_id,
                    session_id=session.id,
                    source=chunk.source,
                    sequence_no=next_sequence + index,
                    captured_at=chunk.captured_at,
                    content=chunk.content,
                )
            )
        return tuple(normalized)

    def _next_sequence_no(self, session_id: str) -> int:
        chunks = tuple(self._store.list_log_chunks(session_id))
        if not chunks:
            return 0
        return max(chunk.sequence_no for chunk in chunks) + 1

    def _resolve_worktree(self, context: SessionContextRecord) -> Worktree | None:
        if context.worktree_id is not None:
            worktree = self._store.get_worktree(context.worktree_id)
            if worktree is not None:
                return worktree
        if context.worktree_path is not None:
            return self._store.get_worktree_by_path(context.worktree_path)
        return None

    def _resolve_worktree_from_inputs(
        self,
        *,
        agent: Agent | None,
        patch: SessionContextPatch | None,
        existing: SessionContextRecord | None,
    ) -> Worktree | None:
        worktree_id = self._first_non_empty(
            patch.worktree_id if patch is not None else None,
            existing.worktree_id if existing is not None else None,
        )
        if worktree_id is not None:
            worktree = self._store.get_worktree(worktree_id)
            if worktree is not None:
                return worktree
        worktree_path = self._first_non_empty(
            patch.worktree_path if patch is not None else None,
            agent.worktree_path if agent is not None else None,
            existing.worktree_path if existing is not None else None,
        )
        if worktree_path is None:
            return None
        return self._store.get_worktree_by_path(worktree_path)

    def _require_agent(self, agent_id: str) -> Agent:
        agent = self._store.get_agent(agent_id)
        if agent is None:
            msg = f"unknown agent: {agent_id}"
            raise PersistenceError(msg)
        return agent

    def _require_session(self, session_id: str) -> Session:
        session = self._store.get_session(session_id)
        if session is None:
            msg = f"unknown session: {session_id}"
            raise PersistenceError(msg)
        return session

    def _payload_json(self, *, raw_evidence: JsonValue, derived_fact: JsonValue) -> str:
        return json.dumps(
            {"raw_evidence": raw_evidence, "derived_fact": derived_fact},
            sort_keys=True,
            separators=(",", ":"),
        )

    def _patch_dict(self, patch: SessionContextPatch | None) -> JsonValue:
        if patch is None:
            return {}
        return {
            "agent_id": patch.agent_id,
            "worktree_id": patch.worktree_id,
            "tmux_pane_id": patch.tmux_pane_id,
            "pane_tty": patch.pane_tty,
            "worktree_path": patch.worktree_path,
            "copilot_session_id": patch.copilot_session_id,
            "repo_root": patch.repo_root,
            "branch": patch.branch,
        }

    def _first_non_empty(self, *values: str | None) -> str | None:
        for value in values:
            if value is not None and value.strip():
                return value
        return None


__all__ = [
    "SessionBundle",
    "SessionContextPatch",
    "SessionContextView",
    "SessionReplayLookup",
    "SessionService",
]
