from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from copilot_commander.adapters.sqlite_store import SessionContextRecord
from copilot_commander.domain.models import Agent, Session, Worktree
from copilot_commander.exceptions import PersistenceError
from copilot_commander.parsers.git_parser import GitStatusEntry
from copilot_commander.services.worktree_service import (
    WorktreeCreateResult,
    WorktreeGitDetails,
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
    provenance: WorktreeProvenanceView | None
    active_session_count: int
    context_count: int
    has_conflicts: bool


@dataclass(frozen=True, slots=True)
class WorktreeDetailView:
    summary: WorktreeSummaryView
    conflicts: tuple[WorktreeConflictView, ...]
    active_session_ids: tuple[str, ...]
    pane_targets: tuple[str, ...]
    branch_status: str | None = None
    change_summary: str | None = None
    status_entries: tuple[WorktreeChangeView, ...] = ()
    recent_commits: tuple[WorktreeCommitView, ...] = ()


@dataclass(frozen=True, slots=True)
class WorktreeChangeView:
    code: str
    path: str
    kind: str


@dataclass(frozen=True, slots=True)
class WorktreeCommitView:
    short_sha: str
    relative_date: str
    subject: str


@dataclass(frozen=True, slots=True)
class WorktreeActionView:
    action: str
    message: str
    worktree: WorktreeDetailView | None
    conflicts: tuple[WorktreeConflictView, ...]
    pruned_paths: tuple[str, ...] = ()
    previous_assignment: str | None = None


class WorktreeProvenanceKind(StrEnum):
    ASSIGNED = "assigned"
    LIVE_AGENT = "live_agent"
    SESSION = "session"


@dataclass(frozen=True, slots=True)
class WorktreeProvenanceView:
    kind: WorktreeProvenanceKind
    agent_id: str
    agent_name: str | None = None

    @property
    def label(self) -> str:
        return self.agent_name or self.agent_id


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
        if not worktrees:
            return ()
        # Hoist the agent lookup so we don't re-query for every row. On a
        # busy machine this was the dominant cost of `_build_summary`.
        agents = tuple(self._store.list_agents())
        agents_by_id = {agent.id: agent for agent in agents}
        # Conflict detection runs multiple git subprocesses per repo root
        # and is what makes the worktree screen feel sluggish. The list
        # view doesn't need per-row conflict rendering — detail view
        # computes it for the selected worktree. We skip here and keep
        # `has_conflicts=False` for list rows until the user drills in.
        return tuple(
            self._build_summary(
                worktree,
                conflicts=(),
                agents=agents,
                agents_by_id=agents_by_id,
            )
            for worktree in worktrees
        )

    def get_worktree_detail(self, path_or_id: str | Path) -> WorktreeDetailView:
        worktree = self._resolve_worktree(path_or_id)
        conflicts = self._conflicts_for_worktree(worktree)
        return self._build_detail(worktree, conflicts)

    def create_worktree(
        self,
        cwd: str | Path,
        *,
        slug: str | None = None,
        task_title: str | None = None,
        branch: str | None = None,
        base_branch: str | None = None,
        attach_agent_id: str | None = None,
        force: bool = False,
    ) -> WorktreeActionView:
        result = self._service.create_worktree(
            cwd,
            slug=slug,
            task_title=task_title,
            branch=branch,
            base_branch=base_branch,
            attach_agent_id=attach_agent_id,
            force=force,
        )
        return self._action_from_create_result(result)

    def attach_worktree(
        self,
        cwd_or_path: str | Path,
        *,
        agent_id: str | None = None,
        allow_reassign: bool = False,
    ) -> WorktreeActionView:
        result = self._service.attach_worktree(
            cwd_or_path,
            agent_id=agent_id,
            allow_reassign=allow_reassign,
        )
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
        pruned_count = len(report.pruned_paths)
        if dry_run:
            message = f"dry run: {pruned_count} stale worktree(s)"
        else:
            message = f"pruned {pruned_count} stale worktree(s)"
        return WorktreeActionView(
            action="prune",
            message=message,
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
        git_details = self._service.inspect_git_details(worktree.id, commit_limit=5)
        return WorktreeDetailView(
            summary=self._build_summary(worktree, conflicts=conflicts),
            conflicts=self._render_conflicts(conflicts),
            active_session_ids=tuple(
                session.id for session in sessions if session.ended_at is None
            ),
            pane_targets=tuple(
                context.tmux_pane_id for context in contexts if context.tmux_pane_id is not None
            ),
            branch_status=self._format_branch_status(worktree.branch, git_details),
            change_summary=self._summarize_changes(git_details.snapshot.status_summary.entries),
            status_entries=self._render_status_entries(git_details.snapshot.status_summary.entries),
            recent_commits=tuple(
                WorktreeCommitView(
                    short_sha=commit.short_sha,
                    relative_date=commit.relative_date,
                    subject=commit.subject,
                )
                for commit in git_details.recent_commits
            ),
        )

    def _build_summary(
        self,
        worktree: Worktree,
        *,
        conflicts: Sequence[WorktreeOrphanConflict] = (),
        agents: Sequence[Agent] | None = None,
        agents_by_id: dict[str, Agent] | None = None,
    ) -> WorktreeSummaryView:
        if agents is None:
            agents = tuple(self._store.list_agents())
        if agents_by_id is None:
            agents_by_id = {agent.id: agent for agent in agents}
        assigned_agent = None
        if worktree.assigned_agent_id is not None:
            assigned_agent = agents_by_id.get(worktree.assigned_agent_id)
        sessions = tuple(self._store.list_sessions_for_worktree(worktree.id))
        contexts = tuple(self._store.list_session_contexts_for_worktree(worktree.id))
        provenance = self._resolve_provenance(
            worktree,
            agents=agents,
            agents_by_id=agents_by_id,
            sessions=sessions,
            contexts=contexts,
        )
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
            provenance=provenance,
            active_session_count=sum(1 for session in sessions if session.ended_at is None),
            context_count=len(contexts),
            has_conflicts=bool(conflicts),
        )

    def _resolve_provenance(
        self,
        worktree: Worktree,
        *,
        agents: Sequence[Agent],
        agents_by_id: dict[str, Agent],
        sessions: Sequence[Session],
        contexts: Sequence[SessionContextRecord],
    ) -> WorktreeProvenanceView | None:
        if worktree.assigned_agent_id is not None:
            return self._provenance_view(
                worktree.assigned_agent_id,
                kind=WorktreeProvenanceKind.ASSIGNED,
                agents_by_id=agents_by_id,
            )
        live_agent = self._find_live_agent_for_worktree(worktree, agents)
        if live_agent is not None:
            return WorktreeProvenanceView(
                kind=WorktreeProvenanceKind.LIVE_AGENT,
                agent_id=live_agent.id,
                agent_name=live_agent.name,
            )
        for context in contexts:
            if context.agent_id is not None:
                return self._provenance_view(
                    context.agent_id,
                    kind=WorktreeProvenanceKind.SESSION,
                    agents_by_id=agents_by_id,
                )
        if sessions:
            return self._provenance_view(
                sessions[0].agent_id,
                kind=WorktreeProvenanceKind.SESSION,
                agents_by_id=agents_by_id,
            )
        return None

    def _provenance_view(
        self,
        agent_id: str,
        *,
        kind: WorktreeProvenanceKind,
        agents_by_id: dict[str, Agent],
    ) -> WorktreeProvenanceView:
        agent = agents_by_id.get(agent_id)
        return WorktreeProvenanceView(
            kind=kind,
            agent_id=agent_id,
            agent_name=None if agent is None else agent.name,
        )

    def _find_live_agent_for_worktree(
        self,
        worktree: Worktree,
        agents: Sequence[Agent],
    ) -> Agent | None:
        worktree_path = Path(worktree.path).expanduser().resolve(strict=False)
        matches = [agent for agent in agents if self._agent_matches_worktree(agent, worktree_path)]
        if not matches:
            return None
        return max(matches, key=lambda agent: (agent.last_seen_at, agent.started_at, agent.id))

    def _agent_matches_worktree(self, agent: Agent, worktree_path: Path) -> bool:
        for raw_path in (agent.worktree_path, agent.cwd):
            if raw_path is None:
                continue
            candidate = Path(raw_path).expanduser().resolve(strict=False)
            if candidate == worktree_path or candidate.is_relative_to(worktree_path):
                return True
        return False

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

    def _format_branch_status(
        self,
        branch_name: str,
        git_details: WorktreeGitDetails,
    ) -> str | None:
        branch_line = git_details.snapshot.status_summary.branch_line
        if branch_line is not None:
            normalized = branch_line.removeprefix("## ").strip()
            if normalized == branch_name:
                return "local branch only"
            prefix = f"{branch_name}..."
            if normalized.startswith(prefix):
                tracking = normalized.removeprefix(prefix)
                upstream, separator, relation = tracking.partition(" [")
                parts: list[str] = []
                if upstream:
                    parts.append(f"tracks {upstream}")
                if separator:
                    parts.append(relation.removesuffix("]"))
                if parts:
                    return " · ".join(parts)
            return normalized
        fallback_parts: list[str] = []
        if git_details.snapshot.ahead_behind.ahead > 0:
            fallback_parts.append(f"ahead {git_details.snapshot.ahead_behind.ahead}")
        if git_details.snapshot.ahead_behind.behind > 0:
            fallback_parts.append(f"behind {git_details.snapshot.ahead_behind.behind}")
        return " · ".join(fallback_parts) if fallback_parts else "local branch only"

    def _summarize_changes(self, entries: Sequence[GitStatusEntry]) -> str:
        if not entries:
            return "clean working tree"
        conflict_count = sum(1 for entry in entries if entry.is_unmerged)
        untracked_count = sum(1 for entry in entries if entry.is_untracked)
        staged_count = sum(
            1
            for entry in entries
            if entry.index_status != " " and not entry.is_unmerged and not entry.is_untracked
        )
        unstaged_count = sum(
            1
            for entry in entries
            if entry.worktree_status != " " and not entry.is_unmerged and not entry.is_untracked
        )
        parts: list[str] = []
        if staged_count > 0:
            parts.append(f"{staged_count} staged")
        if unstaged_count > 0:
            parts.append(f"{unstaged_count} unstaged")
        if untracked_count > 0:
            parts.append(f"{untracked_count} untracked")
        if conflict_count > 0:
            parts.append(f"{conflict_count} conflicts")
        return " · ".join(parts) if parts else "clean working tree"

    def _render_status_entries(
        self,
        entries: Sequence[GitStatusEntry],
    ) -> tuple[WorktreeChangeView, ...]:
        views: list[WorktreeChangeView] = []
        for entry in entries:
            code = "??" if entry.is_untracked else f"{entry.index_status}{entry.worktree_status}"
            if entry.is_unmerged:
                kind = "conflict"
            elif entry.is_untracked:
                kind = "untracked"
            elif entry.index_status != " " and entry.worktree_status != " ":
                kind = "mixed"
            elif entry.index_status != " ":
                kind = "staged"
            else:
                kind = "unstaged"
            path = (
                f"{entry.original_path} -> {entry.path}"
                if entry.original_path is not None
                else entry.path
            )
            views.append(WorktreeChangeView(code=code, path=path, kind=kind))
        return tuple(views)

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
    "WorktreeChangeView",
    "WorktreeCommitView",
    "WorktreeConflictView",
    "WorktreeController",
    "WorktreeDetailView",
    "WorktreeProvenanceKind",
    "WorktreeProvenanceView",
    "WorktreeStartAgentIntent",
    "WorktreeSummaryView",
]
