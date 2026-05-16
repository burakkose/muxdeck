# mypy: disable-error-code=no-untyped-def
# ruff: noqa: E402,E501,ANN001,ANN201,ANN202

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from muxdeck.adapters.sqlite_store import SessionContextRecord
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.events import Event, LogChunk
from muxdeck.domain.models import Agent, Session
from muxdeck.services.agent_service import AgentFactInput, AgentService


class InMemoryAgentStore:
    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}

    def upsert_agent(self, agent: Agent, /) -> None:
        self.agents[agent.id] = agent

    def get_agent(self, agent_id: str, /) -> Agent | None:
        return self.agents.get(agent_id)

    def get_agent_by_pane_id(self, pane_id: str, /) -> Agent | None:
        matches = [agent for agent in self.agents.values() if agent.tmux_pane_id == pane_id]
        return (
            sorted(matches, key=lambda agent: (agent.last_seen_at, agent.id), reverse=True)[0]
            if matches
            else None
        )

    def get_agent_by_copilot_session_id(self, copilot_session_id: str, /) -> Agent | None:
        matches = [
            agent
            for agent in self.agents.values()
            if agent.copilot_session_id == copilot_session_id
        ]
        return (
            sorted(matches, key=lambda agent: (agent.last_seen_at, agent.id), reverse=True)[0]
            if matches
            else None
        )


class InMemorySessionStore:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.open_session_calls: list[str] = []
        self.list_sessions_calls: list[str | None] = []

    def upsert_session(self, session: Session, /) -> None:
        self.sessions[session.id] = session

    def list_sessions(self, agent_id: str | None = None, /):
        self.list_sessions_calls.append(agent_id)
        sessions = tuple(self.sessions.values())
        if agent_id is None:
            return sessions
        return tuple(session for session in sessions if session.agent_id == agent_id)

    def get_session(self, session_id: str, /) -> Session | None:
        return self.sessions.get(session_id)

    def get_session_by_copilot_session_id(self, copilot_session_id: str, /) -> Session | None:
        for session in self.sessions.values():
            if session.copilot_session_id == copilot_session_id:
                return session
        return None

    def get_open_session_for_agent(self, agent_id: str, /) -> Session | None:
        self.open_session_calls.append(agent_id)
        candidates = [
            session
            for session in self.sessions.values()
            if session.agent_id == agent_id and session.ended_at is None
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda session: (session.created_at, session.id), reverse=True)
        return candidates[0]


class InMemoryEventStore:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def append_events(self, events, /) -> None:
        self.events.extend(events)

    def list_events_for_session(self, session_id: str, /):
        return tuple(event for event in self.events if event.session_id == session_id)


class InMemoryLogStore:
    def __init__(self) -> None:
        self.chunks: list[LogChunk] = []
        self.list_log_chunks_calls: int = 0
        self.get_latest_log_chunk_calls: int = 0

    def append_log_chunks(self, chunks, /) -> None:
        self.chunks.extend(chunks)

    def list_log_chunks(self, session_id: str, /):
        self.list_log_chunks_calls += 1
        return tuple(chunk for chunk in self.chunks if chunk.session_id == session_id)

    def get_latest_log_chunk(self, session_id: str, /) -> LogChunk | None:
        self.get_latest_log_chunk_calls += 1
        latest: LogChunk | None = None
        for chunk in self.chunks:
            if chunk.session_id != session_id:
                continue
            if latest is None or chunk.sequence_no > latest.sequence_no:
                latest = chunk
        return latest

    def get_log_chunk(self, log_chunk_id: str, /) -> LogChunk | None:
        for chunk in self.chunks:
            if chunk.id == log_chunk_id:
                return chunk
        return None


class UpsertingLogStore(InMemoryLogStore):
    """Log store fake that exposes the A3 transactional upsert."""

    def __init__(self) -> None:
        super().__init__()
        self.upsert_calls: int = 0
        self.append_log_chunks_calls: int = 0

    def append_log_chunks(self, chunks, /) -> None:
        self.append_log_chunks_calls += 1
        super().append_log_chunks(chunks)

    def upsert_log_capture_if_changed(
        self,
        *,
        agent_id: str,
        session_id: str,
        source: str,
        content: str,
        captured_at: datetime,
    ) -> LogChunk | None:
        self.upsert_calls += 1
        # Inline the equivalent dedup logic rather than calling the
        # tracked ``get_latest_log_chunk`` helper, so the test can
        # distinguish the new transactional path from the legacy
        # ``get_latest_log_chunk`` + ``append_log_chunks`` pair.
        latest: LogChunk | None = None
        for chunk in self.chunks:
            if chunk.session_id != session_id:
                continue
            if latest is None or chunk.sequence_no > latest.sequence_no:
                latest = chunk
        if latest is not None and latest.content == content:
            return None
        next_sequence = latest.sequence_no + 1 if latest is not None else 0
        new_chunk = LogChunk(
            id=f"upsert-{len(self.chunks)}",
            agent_id=agent_id,
            session_id=session_id,
            source=source,  # type: ignore[arg-type]
            sequence_no=next_sequence,
            captured_at=captured_at,
            content=content,
        )
        self.chunks.append(new_chunk)
        return new_chunk


class InMemoryContextStore:
    def __init__(self) -> None:
        self.contexts: dict[str, SessionContextRecord] = {}

    def upsert_session_context(self, context: SessionContextRecord, /) -> None:
        self.contexts[context.session_id] = context

    def get_session_context(self, session_id: str, /) -> SessionContextRecord | None:
        return self.contexts.get(session_id)

    def get_session_context_by_tmux_pane_id(
        self, tmux_pane_id: str, /
    ) -> SessionContextRecord | None:
        for context in self.contexts.values():
            if context.tmux_pane_id == tmux_pane_id:
                return context
        return None


class AgentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent_store = InMemoryAgentStore()
        self.session_store = InMemorySessionStore()
        self.event_store = InMemoryEventStore()
        self.log_store = InMemoryLogStore()
        self.context_store = InMemoryContextStore()
        self.now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        self.service = AgentService(
            self.agent_store,
            self.session_store,
            self.event_store,
            self.log_store,
            self.context_store,
            clock=lambda: self.now,
        )

    def test_persist_agent_facts_creates_agent_session_events_and_log(self) -> None:
        result = self.service.persist_agent_facts(
            AgentFactInput(
                classification="unmanaged_probable_agent",
                tmux_session_name="muxdeck",
                tmux_window_id="@1",
                tmux_window_name="agents",
                tmux_pane_id="%1",
                pane_tty="/dev/pts/1",
                cwd="/repo/worktrees/task-one",
                worktree_path="/repo/worktrees/task-one",
                observed_at=self.now,
                status=AgentStatus.RUNNING,
                copilot_session_id="copilot-123",
                idle_seconds=0,
                token_input=12,
                token_output=30,
                token_total=42,
                capture_text="Copilot session id: copilot-123",
                blocking_issue_kinds=("waiting_for_confirmation",),
            )
        )

        assert result.created_agent is True
        assert result.created_session is True
        assert result.agent.copilot_session_id == "copilot-123"
        assert result.session.agent_id == result.agent.id
        assert len(result.events) == 3
        assert result.events[0].kind == "agent.discovered"
        assert result.events[1].kind == "agent.session.observed"
        assert result.events[2].kind == "agent.blocking_issue"
        assert len(result.log_chunks) == 1
        context = self.context_store.get_session_context(result.session.id)
        assert context is not None
        assert context.tmux_pane_id == "%1"

    def test_persist_agent_facts_updates_existing_agent_and_skips_duplicate_capture(self) -> None:
        first = self.service.persist_agent_facts(
            AgentFactInput(
                classification="unmanaged_probable_agent",
                tmux_session_name="muxdeck",
                tmux_window_id="@1",
                tmux_pane_id="%1",
                cwd="/repo/worktrees/task-one",
                observed_at=self.now,
                status=AgentStatus.RUNNING,
                capture_text="still running",
            )
        )
        second = self.service.persist_agent_facts(
            AgentFactInput(
                classification="managed_agent",
                agent_id=first.agent.id,
                tmux_session_name="muxdeck",
                tmux_window_id="@1",
                tmux_pane_id="%1",
                cwd="/repo/worktrees/task-one",
                observed_at=self.now + timedelta(minutes=5),
                status=AgentStatus.IDLE,
                idle_seconds=300,
                needs_attention=True,
                attention_reason="idle for 300s",
                capture_text="still running",
            )
        )

        assert second.created_agent is False
        assert second.agent.id == first.agent.id
        assert second.agent.started_at == first.agent.started_at
        assert second.agent.status is AgentStatus.IDLE
        assert len(second.events) == 1
        assert second.events[0].kind == "agent.status.changed"
        assert second.log_chunks == ()
        assert len(self.log_store.list_log_chunks(first.session.id)) == 1

    def test_persist_agent_facts_does_not_full_scan_log_chunks(self) -> None:
        """Regression: ``_append_log_chunk_if_changed`` must not call
        ``list_log_chunks`` on the hot path. Pulling every chunk for a
        long-lived session (26k+ rows observed in production) once per
        agent per refresh dominated dashboard latency and made the UI
        feel frozen for tens of seconds.
        """
        # Seed a session via a normal first persist so subsequent calls
        # exercise the "existing chunks" branch.
        first = self.service.persist_agent_facts(
            AgentFactInput(
                classification="unmanaged_probable_agent",
                tmux_session_name="muxdeck",
                tmux_window_id="@1",
                tmux_pane_id="%1",
                cwd="/repo/worktrees/task-one",
                observed_at=self.now,
                status=AgentStatus.RUNNING,
                capture_text="initial",
            )
        )
        baseline = self.log_store.list_log_chunks_calls
        for index in range(5):
            self.service.persist_agent_facts(
                AgentFactInput(
                    classification="managed_agent",
                    agent_id=first.agent.id,
                    tmux_session_name="muxdeck",
                    tmux_window_id="@1",
                    tmux_pane_id="%1",
                    cwd="/repo/worktrees/task-one",
                    observed_at=self.now + timedelta(seconds=index + 1),
                    status=AgentStatus.RUNNING,
                    capture_text=f"capture-{index}",
                )
            )
        assert self.log_store.list_log_chunks_calls == baseline, (
            "persist_agent_facts must use get_latest_log_chunk instead of "
            "list_log_chunks on the refresh hot path"
        )

    def test_persist_agent_facts_uses_open_session_fast_path_after_first_cycle(self) -> None:
        """Regression: the second + subsequent persist for the same
        managed agent must take the indexed
        ``get_open_session_for_agent`` route instead of the legacy
        ``list_sessions(agent_id)`` full scan when neither the
        copilot_session_id nor the pane-id context resolves the
        session.
        """
        first = self.service.persist_agent_facts(
            AgentFactInput(
                classification="unmanaged_probable_agent",
                tmux_session_name="muxdeck",
                tmux_window_id="@1",
                tmux_pane_id="%1",
                cwd="/repo/worktrees/task-one",
                observed_at=self.now,
                status=AgentStatus.RUNNING,
                capture_text="initial",
            )
        )
        # Drop the cached pane→session context so the lookup is forced
        # to fall through to the agent-id branch the way it does when
        # muxdeck restarts mid-cycle on a host with a populated DB.
        self.context_store.contexts.clear()
        self.session_store.open_session_calls.clear()
        before_list_calls = len(self.session_store.list_sessions_calls)

        self.service.persist_agent_facts(
            AgentFactInput(
                classification="managed_agent",
                agent_id=first.agent.id,
                tmux_session_name="muxdeck",
                tmux_window_id="@1",
                tmux_pane_id="%1",
                cwd="/repo/worktrees/task-one",
                observed_at=self.now + timedelta(seconds=10),
                status=AgentStatus.RUNNING,
                capture_text="step-one",
            )
        )

        assert self.session_store.open_session_calls == [first.agent.id]
        assert len(self.session_store.list_sessions_calls) == before_list_calls, (
            "AgentService._find_current_session must consult the indexed "
            "get_open_session_for_agent helper before falling back to a "
            "full list_sessions(agent_id) scan"
        )

    def test_persist_agent_facts_falls_back_to_list_when_open_session_helper_absent(
        self,
    ) -> None:
        """Stores without ``get_open_session_for_agent`` (legacy fakes,
        partial Protocol implementations) must still resolve the
        session via the original ``list_sessions(agent_id)`` scan so
        nothing crashes when the optimisation surface is missing.
        """
        legacy_store = _LegacySessionStore()
        legacy_context = InMemoryContextStore()
        service = AgentService(
            self.agent_store,
            legacy_store,
            self.event_store,
            self.log_store,
            legacy_context,
            clock=lambda: self.now,
        )
        first = service.persist_agent_facts(
            AgentFactInput(
                classification="unmanaged_probable_agent",
                tmux_session_name="muxdeck",
                tmux_window_id="@1",
                tmux_pane_id="%1",
                cwd="/repo/worktrees/task-one",
                observed_at=self.now,
                status=AgentStatus.RUNNING,
                capture_text="initial",
            )
        )
        legacy_context.contexts.clear()
        before_list_calls = len(legacy_store.list_sessions_calls)

        second = service.persist_agent_facts(
            AgentFactInput(
                classification="managed_agent",
                agent_id=first.agent.id,
                tmux_session_name="muxdeck",
                tmux_window_id="@1",
                tmux_pane_id="%1",
                cwd="/repo/worktrees/task-one",
                observed_at=self.now + timedelta(seconds=10),
                status=AgentStatus.RUNNING,
                capture_text="step-one",
            )
        )

        assert second.session.id == first.session.id
        assert len(legacy_store.list_sessions_calls) == before_list_calls + 1, (
            "fallback must hit list_sessions exactly once when the helper is absent"
        )

    def test_persist_agent_facts_uses_transactional_upsert_when_log_store_supports_it(
        self,
    ) -> None:
        """Regression: when the log store exposes the A3 transactional
        ``upsert_log_capture_if_changed`` helper, the agent service
        must call it instead of the legacy ``get_latest_log_chunk``
        + ``append_log_chunks`` pair so the dashboard hot path pays
        a single BEGIN/COMMIT per changed capture.
        """
        upserting_log_store = UpsertingLogStore()
        service = AgentService(
            self.agent_store,
            self.session_store,
            self.event_store,
            upserting_log_store,
            self.context_store,
            clock=lambda: self.now,
        )

        result = service.persist_agent_facts(
            AgentFactInput(
                classification="unmanaged_probable_agent",
                tmux_session_name="muxdeck",
                tmux_window_id="@1",
                tmux_pane_id="%1",
                cwd="/repo/worktrees/task-one",
                observed_at=self.now,
                status=AgentStatus.RUNNING,
                capture_text="initial",
            )
        )
        second = service.persist_agent_facts(
            AgentFactInput(
                classification="managed_agent",
                agent_id=result.agent.id,
                tmux_session_name="muxdeck",
                tmux_window_id="@1",
                tmux_pane_id="%1",
                cwd="/repo/worktrees/task-one",
                observed_at=self.now + timedelta(seconds=1),
                status=AgentStatus.RUNNING,
                capture_text="initial\nstep-two",
            )
        )

        assert upserting_log_store.upsert_calls == 2
        # The legacy two-call read+write path should be entirely
        # bypassed when the fast helper is available.
        assert upserting_log_store.get_latest_log_chunk_calls == 0
        assert upserting_log_store.append_log_chunks_calls == 0
        assert len(upserting_log_store.chunks) == 2
        assert result.log_chunks[0].content == "initial"
        assert second.log_chunks[0].content == "initial\nstep-two"


class _LegacySessionStore:
    """Session store fake that intentionally omits the A4 helper.

    Mirrors what a partial Protocol implementation (or a test fake
    that hasn't been migrated yet) looks like, so the agent service
    can be verified to gracefully degrade to the legacy scan path.
    """

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.list_sessions_calls: list[str | None] = []

    def upsert_session(self, session: Session, /) -> None:
        self.sessions[session.id] = session

    def list_sessions(self, agent_id: str | None = None, /):
        self.list_sessions_calls.append(agent_id)
        sessions = tuple(self.sessions.values())
        if agent_id is None:
            return sessions
        return tuple(session for session in sessions if session.agent_id == agent_id)

    def get_session(self, session_id: str, /) -> Session | None:
        return self.sessions.get(session_id)

    def get_session_by_copilot_session_id(self, copilot_session_id: str, /) -> Session | None:
        for session in self.sessions.values():
            if session.copilot_session_id == copilot_session_id:
                return session
        return None


if __name__ == "__main__":
    unittest.main()
