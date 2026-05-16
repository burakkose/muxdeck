from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, Protocol

from muxdeck.adapters.sqlite_store import AgentActionTarget, SessionContextRecord
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.events import Event, LogChunk
from muxdeck.domain.models import Agent, Session, Worktree
from muxdeck.domain.value_objects import utc_now
from muxdeck.exceptions import PersistenceError
from muxdeck.services.session_service import SessionBundle, SessionContextPatch
from muxdeck.types import Clock

AgentIntentKind = Literal[
    "send_input",
    "interrupt",
    "kill_pane",
    "restart",
    "open_pane",
    "open_worktree",
    "rename_window",
    "move_to_window",
]


class AgentQueryPort(Protocol):
    def get_agent(self, agent_id: str, /) -> Agent | None: ...

    def upsert_agent(self, agent: Agent, /) -> None: ...

    def list_sessions(self, agent_id: str | None = None, /) -> Sequence[Session]: ...

    def get_session_context(self, session_id: str, /) -> SessionContextRecord | None: ...

    def get_worktree(self, worktree_id: str, /) -> Worktree | None: ...

    def get_agent_by_pane_id(self, pane_id: str, /) -> Agent | None: ...

    def get_agent_by_copilot_session_id(self, copilot_session_id: str, /) -> Agent | None: ...

    # Optional fast-path: stores that implement
    # ``get_agent_action_target`` collapse the four-call action-keystroke
    # fetch into one bundled call. The controller probes for it via
    # ``getattr`` so legacy in-memory test doubles keep working.


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
        bundle = self._resolve_action_target(agent.id)
        latest_open_session = bundle.open_session
        target = self._target_from_agent(agent, target=bundle)
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
            agent=self._target_from_agent(
                agent,
                latest_session_id=created.session.id,
                target=bundle,
            ),
            session_id=created.session.id,
            session_created=True,
        )

    def send_input_intent(self, agent_id: str, prompt: str) -> AgentIntentView:
        agent = self._require_agent(agent_id)
        target = self._target_from_agent(agent, target=self._resolve_action_target(agent.id))
        return AgentIntentView(
            kind="send_input",
            agent=target,
            label="Send input",
            prompt=prompt,
            metadata=(("append_enter", "true"), ("pane_target", target.pane_target)),
        )

    def interrupt_intent(self, agent_id: str) -> AgentIntentView:
        agent = self._require_agent(agent_id)
        target = self._target_from_agent(agent, target=self._resolve_action_target(agent.id))
        return AgentIntentView(
            kind="interrupt",
            agent=target,
            label="Interrupt agent",
            metadata=(("pane_target", target.pane_target), ("key", "C-c")),
        )

    def kill_pane_intent(self, agent_id: str) -> AgentIntentView:
        agent = self._require_agent(agent_id)
        target = self._target_from_agent(agent, target=self._resolve_action_target(agent.id))
        return AgentIntentView(
            kind="kill_pane",
            agent=target,
            label="Kill pane",
            metadata=(("pane_target", target.pane_target),),
        )

    def restart_intent(self, agent_id: str, *, model: str | None = None) -> AgentIntentView:
        agent = self._require_agent(agent_id)
        target = self._target_from_agent(agent, target=self._resolve_action_target(agent.id))
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
        bundle = self._resolve_action_target(agent.id)
        latest_open_session = bundle.open_session

        # Update agent status to COMPLETED so the monitoring heuristic
        # preserves it instead of flagging the dead pane as needs_attention.
        updated_agent = replace(
            agent,
            status=AgentStatus.COMPLETED,
            needs_attention=False,
            attention_reason=None,
        )
        self._store.upsert_agent(updated_agent)

        target = self._target_from_agent(updated_agent, target=bundle)
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
            agent=self._target_from_agent(
                updated_agent,
                latest_session_id=ended.session.id,
                target=bundle,
            ),
            session_id=ended.session.id,
            session_ended=True,
        )

    def seed_resumed_session(
        self,
        *,
        copilot_session_id: str,
        tmux_pane_id: str,
        tmux_session_name: str,
        tmux_window_id: str,
        tmux_window_name: str | None,
        pane_tty: str | None,
        pane_pid: int | None,
        cwd: str,
        repo_root: str | None,
        worktree_path: str | None,
        branch: str | None,
        name: str,
        task_title: str | None = None,
    ) -> Agent:
        """Seed an agent record for a Copilot session just resumed in tmux.

        Background. ``action_resume_session`` (sessions screen) spawns a
        new tmux window that runs ``copilot --resume=<id>`` either
        directly (local origin) or wrapped in ``pwsh.exe`` (windows
        origin). In the local case the next monitoring sync would
        discover the pane and assemble the agent record normally — but
        it would still lag a refresh tick, and for ``windows`` origin
        the WSL pane stays in muxdeck's own cwd while copilot.exe runs
        inside pwsh on the Windows side, so the resolver can't link
        pane to ``copilot_session_id`` (pwsh.exe is invisible to
        ``ps``) and pane-derived ``cwd``/``repo_root``/``branch`` are
        all wrong.

        Seeding upfront breaks both: the agent row carries the right
        identity (cwd/repo_root/branch from session metadata) plus the
        explicit ``copilot_session_id`` linkage, so the dashboard
        shows the original repo and the SESSIONS row flips to "active"
        on the very next refresh tick.

        Re-seeding into an existing pane id (rare — operator manually
        resumed twice through muxdeck) preserves the original
        ``started_at`` so the session age remains coherent.
        """
        now = self._clock()
        existing = self._store.get_agent_by_pane_id(tmux_pane_id)
        started_at = existing.started_at if existing is not None else now
        if existing is not None:
            agent = Agent(
                id=existing.id,
                name=name,
                tmux_session_name=tmux_session_name,
                tmux_window_id=tmux_window_id,
                tmux_window_name=tmux_window_name,
                tmux_pane_id=tmux_pane_id,
                pane_tty=pane_tty,
                cwd=cwd,
                repo_root=repo_root,
                worktree_path=worktree_path,
                branch=branch,
                task_title=task_title,
                copilot_session_id=copilot_session_id,
                pid=pane_pid,
                status=AgentStatus.STARTING,
                started_at=started_at,
                last_seen_at=now,
            )
        else:
            agent = Agent(
                name=name,
                tmux_session_name=tmux_session_name,
                tmux_window_id=tmux_window_id,
                tmux_window_name=tmux_window_name,
                tmux_pane_id=tmux_pane_id,
                pane_tty=pane_tty,
                cwd=cwd,
                repo_root=repo_root,
                worktree_path=worktree_path,
                branch=branch,
                task_title=task_title,
                copilot_session_id=copilot_session_id,
                pid=pane_pid,
                status=AgentStatus.STARTING,
                started_at=started_at,
                last_seen_at=now,
            )
        self._store.upsert_agent(agent)
        return agent

    def open_pane_intent(self, agent_id: str) -> AgentIntentView:
        agent = self._require_agent(agent_id)
        target = self._target_from_agent(agent, target=self._resolve_action_target(agent.id))
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
        target = self._target_from_agent(agent, target=self._resolve_action_target(agent.id))
        if target.worktree_path is None:
            msg = f"agent {agent_id} has no worktree path"
            raise PersistenceError(msg)
        return AgentIntentView(
            kind="open_worktree",
            agent=target,
            label="Open worktree",
            metadata=(("path", target.worktree_path),),
        )

    def rename_window_intent(self, agent_id: str, *, new_name: str) -> AgentIntentView:
        agent = self._require_agent(agent_id)
        target = self._target_from_agent(agent, target=self._resolve_action_target(agent.id))
        if target.tmux_window_id is None:
            msg = f"agent {agent_id} has no tmux window"
            raise PersistenceError(msg)
        normalized_name = new_name.strip()
        if not normalized_name:
            msg = "new_name must not be empty"
            raise PersistenceError(msg)
        return AgentIntentView(
            kind="rename_window",
            agent=target,
            label="Rename window",
            metadata=(
                ("window_target", target.tmux_window_id),
                ("window_name", normalized_name),
            ),
        )

    def move_to_window_intent(
        self,
        agent_id: str,
        *,
        target_window: str | None = None,
        new_window_name: str | None = None,
    ) -> AgentIntentView:
        agent = self._require_agent(agent_id)
        target = self._target_from_agent(agent, target=self._resolve_action_target(agent.id))
        normalized_target = target_window.strip() if target_window is not None else None
        normalized_new_name = new_window_name.strip() if new_window_name is not None else None
        if not normalized_target and not normalized_new_name:
            msg = "target_window or new_window_name must be provided"
            raise PersistenceError(msg)
        metadata: list[tuple[str, str]] = [("pane_target", target.pane_target)]
        if target.tmux_session_name is not None:
            metadata.append(("session_target", target.tmux_session_name))
        if normalized_target:
            metadata.append(("window_target", normalized_target))
        if normalized_new_name:
            metadata.append(("new_window_name", normalized_new_name))
        return AgentIntentView(
            kind="move_to_window",
            agent=target,
            label="Move to window",
            metadata=tuple(metadata),
        )

    def _target_from_agent(
        self,
        agent: Agent,
        *,
        latest_session_id: str | None = None,
        target: AgentActionTarget | None = None,
    ) -> AgentTargetView:
        """Build the per-action target view for ``agent``.

        When ``target`` is provided (the action bundle pre-fetched
        once per intent), no further store calls are made: the
        latest-session / context / worktree fields are read directly
        off the bundle. When ``target`` is ``None``, falls back to
        the legacy per-method path so older tests + alt store backends
        keep working.

        ``latest_session_id`` lets callers override the surfaced
        session id (e.g., a freshly-created or just-ended session)
        without needing to refetch the bundle.
        """
        if target is None:
            latest_session_obj = next(iter(self._store.list_sessions(agent.id)), None)
            resolved_session_id = (
                latest_session_id
                if latest_session_id is not None
                else (latest_session_obj.id if latest_session_obj is not None else None)
            )
            worktree_path = agent.worktree_path
            repo_root = agent.repo_root
            context: SessionContextRecord | None = None
            if resolved_session_id is not None:
                context = self._store.get_session_context(resolved_session_id)
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
                latest_session_id=resolved_session_id,
                tmux_window_id=agent.tmux_window_id or None,
                tmux_session_name=agent.tmux_session_name or None,
            )

        if latest_session_id is not None:
            resolved_session_id = latest_session_id
        else:
            resolved_session_id = (
                target.latest_session.id if target.latest_session is not None else None
            )
        worktree_path = agent.worktree_path
        repo_root = agent.repo_root
        if target.context is not None:
            worktree_path = target.context.worktree_path or worktree_path
            repo_root = target.context.repo_root or repo_root
        if target.worktree is not None:
            worktree_path = target.worktree.path
            repo_root = target.worktree.repo_root
        return AgentTargetView(
            agent_id=agent.id,
            name=agent.name,
            status=agent.status,
            pane_target=agent.tmux_pane_id,
            worktree_path=worktree_path,
            repo_root=repo_root,
            branch=agent.branch,
            latest_session_id=resolved_session_id,
            tmux_window_id=agent.tmux_window_id or None,
            tmux_session_name=agent.tmux_session_name or None,
        )

    def _resolve_action_target(self, agent_id: str) -> AgentActionTarget:
        """Fetch the action-target bundle for ``agent_id``.

        Prefers the bulk ``get_agent_action_target`` store helper when
        available (one store call covers latest session, open session,
        context, and worktree). Falls back to the legacy path
        (list_sessions twice + get_session_context + get_worktree) for
        backends that don't expose the helper, keeping older test
        doubles working.
        """
        bulk = getattr(self._store, "get_agent_action_target", None)
        if callable(bulk):
            result = bulk(agent_id)
            if isinstance(result, AgentActionTarget):
                return result
            # Defensive: a non-conforming store should fall through to
            # the legacy path rather than ship a typed lie downstream.
        sessions = tuple(self._store.list_sessions(agent_id))
        latest_session = sessions[0] if sessions else None
        open_session = next((s for s in sessions if s.ended_at is None), None)
        context: SessionContextRecord | None = None
        worktree: Worktree | None = None
        if latest_session is not None:
            context = self._store.get_session_context(latest_session.id)
            if context is not None and context.worktree_id is not None:
                worktree = self._store.get_worktree(context.worktree_id)
        return AgentActionTarget(
            latest_session=latest_session,
            open_session=open_session,
            context=context,
            worktree=worktree,
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
