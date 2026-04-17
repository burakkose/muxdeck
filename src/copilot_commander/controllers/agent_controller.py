from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, Protocol

from copilot_commander.adapters.sqlite_store import SessionContextRecord
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.events import Event, LogChunk
from copilot_commander.domain.models import Agent, Session, Worktree
from copilot_commander.domain.value_objects import utc_now
from copilot_commander.exceptions import PersistenceError
from copilot_commander.services.session_service import SessionBundle, SessionContextPatch
from copilot_commander.types import Clock

AgentIntentKind = Literal[
    "send_input",
    "interrupt",
    "restart",
    "open_pane",
    "open_worktree",
]


class AgentQueryPort(Protocol):
    def get_agent(self, agent_id: str, /) -> Agent | None: ...

    def upsert_agent(self, agent: Agent, /) -> None: ...

    def list_sessions(self, agent_id: str | None = None, /) -> Sequence[Session]: ...

    def get_session_context(self, session_id: str, /) -> SessionContextRecord | None: ...

    def get_worktree(self, worktree_id: str, /) -> Worktree | None: ...


class AgentSessionPort(Protocol):
    def create_session(
        self,
        agent_id: str,
        *,
        task_title: str | None = None,
        copilot_session_id: str | None = None,
        context: SessionContextPatch | None = None,
        occurred_at: datetime | None = None,
    ) -> SessionBundle: ...

    def end_session(
        self,
        session_id: str,
        *,
        exit_reason: str,
        ended_at: datetime | None = None,
        final_events: Sequence[Event] = (),
        final_log_chunks: Sequence[LogChunk] = (),
    ) -> SessionBundle: ...


@dataclass(frozen=True, slots=True)
class AgentTargetView:
    agent_id: str
    name: str
    status: AgentStatus
    pane_target: str
    worktree_path: str | None
    repo_root: str | None
    branch: str | None
    latest_session_id: str | None
    tmux_session_name: str | None = None
    tmux_window_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentIntentView:
    kind: AgentIntentKind
    agent: AgentTargetView
    label: str
    metadata: tuple[tuple[str, str], ...] = ()
    prompt: str | None = None


@dataclass(frozen=True, slots=True)
class AgentActionResult:
    action: str
    agent: AgentTargetView
    session_id: str | None
    session_created: bool = False
    session_ended: bool = False
    intent: AgentIntentView | None = None


class AgentController:
    def __init__(
        self,
        store: AgentQueryPort,
        sessions: AgentSessionPort,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._store = store
        self._sessions = sessions
        self._clock = clock

    def adopt(self, agent_id: str, *, task_title: str | None = None) -> AgentActionResult:
        agent = self._require_agent(agent_id)
        latest_open_session = self._latest_open_session(agent.id)
        target = self._target_from_agent(agent)
        if latest_open_session is not None:
            return AgentActionResult(
                action="adopt",
                agent=target,
                session_id=latest_open_session.id,
                session_created=False,
            )
        created = self._sessions.create_session(
            agent.id,
            task_title=task_title or agent.task_title,
            occurred_at=self._clock(),
        )
        return AgentActionResult(
            action="adopt",
            agent=self._target_from_agent(agent, latest_session_id=created.session.id),
            session_id=created.session.id,
            session_created=True,
        )

    def send_input_intent(self, agent_id: str, prompt: str) -> AgentIntentView:
        agent = self._require_agent(agent_id)
        target = self._target_from_agent(agent)
        return AgentIntentView(
            kind="send_input",
            agent=target,
            label="Send input",
            prompt=prompt,
            metadata=(("append_enter", "true"), ("pane_target", target.pane_target)),
        )

    def interrupt_intent(self, agent_id: str) -> AgentIntentView:
        agent = self._require_agent(agent_id)
        target = self._target_from_agent(agent)
        return AgentIntentView(
            kind="interrupt",
            agent=target,
            label="Interrupt agent",
            metadata=(("pane_target", target.pane_target), ("key", "C-c")),
        )

    def restart_intent(self, agent_id: str, *, model: str | None = None) -> AgentIntentView:
        agent = self._require_agent(agent_id)
        target = self._target_from_agent(agent)
        metadata = [
            ("pane_target", target.pane_target),
            ("cwd", agent.worktree_path or agent.cwd),
            ("task_title", agent.task_title or agent.name),
        ]
        if model is not None:
            metadata.append(("model", model))
        return AgentIntentView(
            kind="restart",
            agent=target,
            label="Restart agent",
            metadata=tuple(metadata),
        )

    def mark_complete(
        self,
        agent_id: str,
        *,
        exit_reason: str = "marked_complete",
    ) -> AgentActionResult:
        agent = self._require_agent(agent_id)
        latest_open_session = self._latest_open_session(agent.id)

        # Update agent status to COMPLETED so the monitoring heuristic
        # preserves it instead of flagging the dead pane as needs_attention.
        updated_agent = replace(
            agent,
            status=AgentStatus.COMPLETED,
            needs_attention=False,
            attention_reason=None,
        )
        self._store.upsert_agent(updated_agent)

        target = self._target_from_agent(updated_agent)
        if latest_open_session is None:
            return AgentActionResult(
                action="mark_complete",
                agent=target,
                session_id=None,
                session_ended=False,
            )
        ended = self._sessions.end_session(
            latest_open_session.id,
            exit_reason=exit_reason,
            ended_at=self._clock(),
        )
        return AgentActionResult(
            action="mark_complete",
            agent=self._target_from_agent(updated_agent, latest_session_id=ended.session.id),
            session_id=ended.session.id,
            session_ended=True,
        )

    def open_pane_intent(self, agent_id: str) -> AgentIntentView:
        agent = self._require_agent(agent_id)
        target = self._target_from_agent(agent)
        metadata: list[tuple[str, str]] = [("pane_target", target.pane_target)]
        if target.tmux_window_id:
            metadata.append(("window_target", target.tmux_window_id))
        if target.tmux_session_name:
            metadata.append(("session_target", target.tmux_session_name))
        return AgentIntentView(
            kind="open_pane",
            agent=target,
            label="Open pane",
            metadata=tuple(metadata),
        )

    def open_worktree_intent(self, agent_id: str) -> AgentIntentView:
        agent = self._require_agent(agent_id)
        target = self._target_from_agent(agent)
        if target.worktree_path is None:
            msg = f"agent {agent_id} has no worktree path"
            raise PersistenceError(msg)
        return AgentIntentView(
            kind="open_worktree",
            agent=target,
            label="Open worktree",
            metadata=(("path", target.worktree_path),),
        )

    def _target_from_agent(
        self,
        agent: Agent,
        *,
        latest_session_id: str | None = None,
    ) -> AgentTargetView:
        if latest_session_id is None:
            latest_session = next(iter(self._store.list_sessions(agent.id)), None)
            latest_session_id = latest_session.id if latest_session is not None else None
        worktree_path = agent.worktree_path
        repo_root = agent.repo_root
        context = None
        if latest_session_id is not None:
            context = self._store.get_session_context(latest_session_id)
        if context is not None:
            worktree_path = context.worktree_path or worktree_path
            repo_root = context.repo_root or repo_root
            if context.worktree_id is not None:
                worktree = self._store.get_worktree(context.worktree_id)
                if worktree is not None:
                    worktree_path = worktree.path
                    repo_root = worktree.repo_root
        return AgentTargetView(
            agent_id=agent.id,
            name=agent.name,
            status=agent.status,
            pane_target=agent.tmux_pane_id,
            worktree_path=worktree_path,
            repo_root=repo_root,
            branch=agent.branch,
            latest_session_id=latest_session_id,
            tmux_window_id=agent.tmux_window_id or None,
            tmux_session_name=agent.tmux_session_name or None,
        )

    def _latest_open_session(self, agent_id: str) -> Session | None:
        sessions = tuple(self._store.list_sessions(agent_id))
        return next((session for session in sessions if session.ended_at is None), None)

    def _require_agent(self, agent_id: str) -> Agent:
        agent = self._store.get_agent(agent_id)
        if agent is None:
            msg = f"unknown agent: {agent_id}"
            raise PersistenceError(msg)
        return agent


__all__ = [
    "AgentActionResult",
    "AgentController",
    "AgentIntentView",
    "AgentTargetView",
]
