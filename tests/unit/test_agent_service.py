# ruff: noqa: E402,E501,ANN001,ANN201

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from copilot_commander.adapters.sqlite_store import SessionContextRecord
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.events import Event, LogChunk
from copilot_commander.domain.models import Agent, Session
from copilot_commander.services.agent_service import AgentFactInput, AgentService


class InMemoryAgentStore:
    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}

    def upsert_agent(self, agent: Agent, /) -> None:
        self.agents[agent.id] = agent

    def get_agent(self, agent_id: str, /) -> Agent | None:
        return self.agents.get(agent_id)

    def get_agent_by_pane_id(self, pane_id: str, /) -> Agent | None:
        matches = [agent for agent in self.agents.values() if agent.tmux_pane_id == pane_id]
        return sorted(matches, key=lambda agent: (agent.last_seen_at, agent.id), reverse=True)[0] if matches else None

    def get_agent_by_copilot_session_id(self, copilot_session_id: str, /) -> Agent | None:
        matches = [
            agent for agent in self.agents.values() if agent.copilot_session_id == copilot_session_id
        ]
        return sorted(matches, key=lambda agent: (agent.last_seen_at, agent.id), reverse=True)[0] if matches else None


class InMemorySessionStore:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}

    def upsert_session(self, session: Session, /) -> None:
        self.sessions[session.id] = session

    def list_sessions(self, agent_id: str | None = None, /):
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

    def append_log_chunks(self, chunks, /) -> None:
        self.chunks.extend(chunks)

    def list_log_chunks(self, session_id: str, /):
        return tuple(chunk for chunk in self.chunks if chunk.session_id == session_id)

    def get_log_chunk(self, log_chunk_id: str, /) -> LogChunk | None:
        for chunk in self.chunks:
            if chunk.id == log_chunk_id:
                return chunk
        return None


class InMemoryContextStore:
    def __init__(self) -> None:
        self.contexts: dict[str, SessionContextRecord] = {}

    def upsert_session_context(self, context: SessionContextRecord, /) -> None:
        self.contexts[context.session_id] = context

    def get_session_context(self, session_id: str, /) -> SessionContextRecord | None:
        return self.contexts.get(session_id)

    def get_session_context_by_tmux_pane_id(self, tmux_pane_id: str, /) -> SessionContextRecord | None:
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


if __name__ == "__main__":
    unittest.main()
