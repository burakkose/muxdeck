from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from copilot_commander.adapters.sqlite_store import SessionContextRecord
from copilot_commander.domain.models import Agent, Session, Worktree
from copilot_commander.exceptions import PersistenceError
from copilot_commander.services.worktree_service import (
    WorktreeCreateResult,
    WorktreeOrphanConflict,
    WorktreeRemoveResult,
    WorktreeService,
)


class WorktreeQueryPort(Protocol):
    def get_worktree(self, worktree_id: str, /) -> Worktree | None: ...

    def get_worktree_by_path(self, path: str, /) -> Worktree | None: ...

    def list_worktrees(
        self,
        /,
        *,
        repo_root: str | None = None,
        assigned_agent_id: str | None = None,
    ) -> Sequence[Worktree]: ...

    def list_agents(self) -> Sequence[Agent]: ...

    def list_sessions_for_worktree(self, worktree_id: str, /) -> Sequence[Session]: ...

    def list_session_contexts_for_worktree(
        self,
        worktree_id: str,
        /,
    ) -> Sequence[SessionContextRecord]: ...


@dataclass(frozen=True, slots=True)
class WorktreeConflictView:
    code: str
    message: str
    path: str
    worktree_id: str | None
    agent_id: str | None
    branch: str | None


@dataclass(frozen=True, slots=True)
class WorktreeSummaryView:
    worktree_id: str
    repo_root: str
    path: str
    branch: str
    base_branch: str | None
    is_main_worktree: bool
    is_dirty: bool
    ahead_count: int | None
    behind_count: int | None
    locked: bool
    assigned_agent_id: str | None
    assigned_agent_name: str | None
    active_session_count: int
    context_count: int
    has_conflicts: bool


@dataclass(frozen=True, slots=True)
class WorktreeDetailView:
    summary: WorktreeSummaryView
    conflicts: tuple[WorktreeConflictView, ...]
    active_session_ids: tuple[str, ...]
    pane_targets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorktreeActionView:
    action: str
    message: str
    worktree: WorktreeDetailView | None
    conflicts: tuple[WorktreeConflictView, ...]
    pruned_paths: tuple[str, ...] = ()
    previous_assignment: str | None = None


@dataclass(frozen=True, slots=True)
class WorktreeStartAgentIntent:
    worktree_id: str
    repo_root: str
    worktree_path: str
    branch: str
    suggested_session_name: str
    suggested_window_name: str
    prompt: str
    model: str | None = None


class WorktreeController:
    def __init__(self, service: WorktreeService, store: WorktreeQueryPort) -> None:
        self._service = service
        self._store = store

    def list_worktrees(self, *, repo_root: str | None = None) -> tuple[WorktreeSummaryView, ...]:
        worktrees = tuple(self._store.list_worktrees(repo_root=repo_root))
        conflict_map = self._conflict_map(worktrees)
        return tuple(
            self._build_summary(worktree, conflict_map.get(worktree.path, ()))
            for worktree in worktrees
        )

    def get_worktree_detail(self, path_or_id: str | Path) -> WorktreeDetailView:
        worktree = self._resolve_worktree(path_or_id)
        conflicts = self._conflicts_for_worktree(worktree)
        return self._build_detail(worktree, conflicts)

    def create_worktree(self, cwd: str | Path, **kwargs: object) -> WorktreeActionView:
        result = self._service.create_worktree(cwd, **kwargs)
        return self._action_from_create_result(result)

    def attach_worktree(self, cwd_or_path: str | Path, **kwargs: object) -> WorktreeActionView:
        result = self._service.attach_worktree(cwd_or_path, **kwargs)
        detail = self._build_detail(result.worktree, result.conflicts)
        return WorktreeActionView(
            action="attach",
            message=f"attached {result.worktree.path}",
            worktree=detail,
            conflicts=self._render_conflicts(result.conflicts),
            previous_assignment=result.previous_assignment,
        )

    def remove_worktree(self, path_or_id: str | Path, *, force: bool = False) -> WorktreeActionView:
        result = self._service.remove_worktree(path_or_id, force=force)
        return self._action_from_remove_result(result)

    def prune_worktrees(
        self,
        cwd: str | Path,
        *,
        dry_run: bool = False,
        expire: str | None = None,
    ) -> WorktreeActionView:
        report = self._service.prune_worktrees(cwd, dry_run=dry_run, expire=expire)
        return WorktreeActionView(
            action="prune",
            message="prune completed" if not dry_run else "prune dry run completed",
            worktree=None,
            conflicts=self._render_conflicts(report.conflicts),
            pruned_paths=tuple(str(path) for path in report.pruned_paths),
        )

    def start_agent_intent(
        self,
        path_or_id: str | Path,
        *,
        prompt: str | None = None,
        model: str | None = None,
        target_session_name: str | None = None,
        window_name: str | None = None,
    ) -> WorktreeStartAgentIntent:
        worktree = self._resolve_worktree(path_or_id)
        branch_tail = worktree.branch.rsplit("/", 1)[-1]
        suggested_prompt = prompt or f"Continue work for {worktree.branch}"
        return WorktreeStartAgentIntent(
            worktree_id=worktree.id,
            repo_root=worktree.repo_root,
            worktree_path=worktree.path,
            branch=worktree.branch,
            suggested_session_name=target_session_name or "muxdeck",
            suggested_window_name=window_name or branch_tail,
            prompt=suggested_prompt,
            model=model,
        )

    def _action_from_create_result(self, result: WorktreeCreateResult) -> WorktreeActionView:
        detail = self._build_detail(result.worktree, result.conflicts)
        return WorktreeActionView(
            action="create",
            message=f"created {result.worktree.path}",
            worktree=detail,
            conflicts=self._render_conflicts(result.conflicts),
        )

    def _action_from_remove_result(self, result: WorktreeRemoveResult) -> WorktreeActionView:
        return WorktreeActionView(
            action="remove",
            message=f"removed {result.path}",
            worktree=None,
            conflicts=self._render_conflicts(result.conflicts),
        )

    def _build_detail(
        self,
        worktree: Worktree,
        conflicts: Sequence[WorktreeOrphanConflict],
    ) -> WorktreeDetailView:
        contexts = tuple(self._store.list_session_contexts_for_worktree(worktree.id))
        sessions = tuple(self._store.list_sessions_for_worktree(worktree.id))
        return WorktreeDetailView(
            summary=self._build_summary(worktree, conflicts),
            conflicts=self._render_conflicts(conflicts),
            active_session_ids=tuple(
                session.id for session in sessions if session.ended_at is None
            ),
            pane_targets=tuple(
                context.tmux_pane_id for context in contexts if context.tmux_pane_id is not None
            ),
        )

    def _build_summary(
        self,
        worktree: Worktree,
        conflicts: Sequence[WorktreeOrphanConflict],
    ) -> WorktreeSummaryView:
        agents = {agent.id: agent for agent in self._store.list_agents()}
        assigned_agent = None
        if worktree.assigned_agent_id is not None:
            assigned_agent = agents.get(worktree.assigned_agent_id)
        sessions = tuple(self._store.list_sessions_for_worktree(worktree.id))
        contexts = tuple(self._store.list_session_contexts_for_worktree(worktree.id))
        return WorktreeSummaryView(
            worktree_id=worktree.id,
            repo_root=worktree.repo_root,
            path=worktree.path,
            branch=worktree.branch,
            base_branch=worktree.base_branch,
            is_main_worktree=worktree.is_main_worktree,
            is_dirty=worktree.is_dirty,
            ahead_count=worktree.ahead_count,
            behind_count=worktree.behind_count,
            locked=worktree.locked,
            assigned_agent_id=worktree.assigned_agent_id,
            assigned_agent_name=assigned_agent.name if assigned_agent is not None else None,
            active_session_count=sum(1 for session in sessions if session.ended_at is None),
            context_count=len(contexts),
            has_conflicts=bool(conflicts),
        )

    def _conflict_map(
        self,
        worktrees: Sequence[Worktree],
    ) -> dict[str, tuple[WorktreeOrphanConflict, ...]]:
        grouped: dict[str, list[WorktreeOrphanConflict]] = {}
        repo_roots = {worktree.repo_root for worktree in worktrees}
        for repo_root in repo_roots:
            for conflict in self._service.detect_orphan_conflicts(repo_root):
                grouped.setdefault(str(conflict.path), []).append(conflict)
        return {path: tuple(conflicts) for path, conflicts in grouped.items()}

    def _conflicts_for_worktree(self, worktree: Worktree) -> tuple[WorktreeOrphanConflict, ...]:
        return tuple(
            conflict
            for conflict in self._service.detect_orphan_conflicts(worktree.repo_root)
            if str(conflict.path) == worktree.path or conflict.worktree_id == worktree.id
        )

    def _render_conflicts(
        self,
        conflicts: Sequence[WorktreeOrphanConflict],
    ) -> tuple[WorktreeConflictView, ...]:
        return tuple(
            WorktreeConflictView(
                code=conflict.code,
                message=conflict.message,
                path=str(conflict.path),
                worktree_id=conflict.worktree_id,
                agent_id=conflict.agent_id,
                branch=conflict.branch,
            )
            for conflict in conflicts
        )

    def _resolve_worktree(self, path_or_id: str | Path) -> Worktree:
        if isinstance(path_or_id, Path):
            worktree = self._store.get_worktree_by_path(str(path_or_id.resolve(strict=False)))
        else:
            worktree = self._store.get_worktree(path_or_id)
            if worktree is None:
                worktree = self._store.get_worktree_by_path(
                    str(Path(path_or_id).expanduser().resolve(strict=False))
                )
        if worktree is None:
            msg = f"unknown worktree: {path_or_id}"
            raise PersistenceError(msg)
        return worktree


__all__ = [
    "WorktreeActionView",
    "WorktreeConflictView",
    "WorktreeController",
    "WorktreeDetailView",
    "WorktreeStartAgentIntent",
    "WorktreeSummaryView",
]
