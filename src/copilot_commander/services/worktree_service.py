from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Protocol

from copilot_commander.adapters.git_adapter import (
    GitRepositorySnapshot,
    GitWorktreeCreateOutcome,
    GitWorktreeCreateRequest,
    GitWorktreeInfo,
    GitWorktreePruneOutcome,
    GitWorktreeRemoveOutcome,
)
from copilot_commander.adapters.sqlite_store import SessionContextRecord
from copilot_commander.config import AppConfig
from copilot_commander.domain.models import Agent, Worktree
from copilot_commander.domain.value_objects import WorktreeId, utc_now
from copilot_commander.exceptions import DomainValidationError, PersistenceError

_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


class WorktreeGitPort(Protocol):
    def discover_repo_root(self, cwd: str | Path, /) -> Path: ...

    def list_worktrees(self, cwd: str | Path, /) -> tuple[GitWorktreeInfo, ...]: ...

    def inspect_repository(self, cwd: str | Path, /) -> GitRepositorySnapshot: ...

    def create_worktree(
        self,
        cwd: str | Path,
        request: GitWorktreeCreateRequest,
        /,
    ) -> GitWorktreeCreateOutcome: ...

    def remove_worktree(
        self,
        path: str | Path,
        /,
        *,
        force: bool = False,
    ) -> GitWorktreeRemoveOutcome: ...

    def prune_worktrees(
        self,
        cwd: str | Path,
        /,
        *,
        dry_run: bool = False,
        expire: str | None = None,
    ) -> GitWorktreePruneOutcome: ...


class WorktreeStorePort(Protocol):
    def upsert_worktree(self, worktree: Worktree, /) -> None: ...

    def get_worktree(self, worktree_id: str, /) -> Worktree | None: ...

    def get_worktree_by_path(self, path: str, /) -> Worktree | None: ...

    def list_worktrees(
        self,
        /,
        *,
        repo_root: str | None = None,
        assigned_agent_id: str | None = None,
    ) -> Sequence[Worktree]: ...

    def list_worktrees_by_repo(self, repo_root: str, /) -> Sequence[Worktree]: ...


class AgentStorePort(Protocol):
    def list_agents(self) -> Sequence[Agent]: ...


class SessionContextStorePort(Protocol):
    def list_session_contexts_for_worktree(self, worktree_id: str, /) -> Sequence[SessionContextRecord]: ...


@dataclass(frozen=True, slots=True)
class WorktreeNamingPlan:
    repo_root: Path
    repo_name: str
    slug: str
    branch_name: str
    base_branch: str
    worktree_name: str
    worktree_path: Path


@dataclass(frozen=True, slots=True)
class WorktreeOrphanConflict:
    code: str
    message: str
    repo_root: Path
    path: Path
    worktree_id: str | None = None
    agent_id: str | None = None
    branch: str | None = None


@dataclass(frozen=True, slots=True)
class WorktreeCreateResult:
    plan: WorktreeNamingPlan
    git_outcome: GitWorktreeCreateOutcome
    worktree: Worktree
    conflicts: tuple[WorktreeOrphanConflict, ...]


@dataclass(frozen=True, slots=True)
class WorktreeAttachResult:
    worktree: Worktree
    previous_assignment: str | None
    conflicts: tuple[WorktreeOrphanConflict, ...]


@dataclass(frozen=True, slots=True)
class WorktreeRemoveResult:
    path: Path
    git_outcome: GitWorktreeRemoveOutcome
    conflicts: tuple[WorktreeOrphanConflict, ...]


@dataclass(frozen=True, slots=True)
class WorktreePruneReport:
    repo_root: Path
    dry_run: bool
    pruned_paths: tuple[Path, ...]
    remaining_paths: tuple[Path, ...]
    conflicts: tuple[WorktreeOrphanConflict, ...]
    git_outcome: GitWorktreePruneOutcome


class WorktreeService:
    def __init__(
        self,
        *,
        config: AppConfig,
        git: WorktreeGitPort,
        worktrees: WorktreeStorePort,
        agents: AgentStorePort,
        session_contexts: SessionContextStorePort,
        clock: callable = utc_now,
    ) -> None:
        self._config = config
        self._git = git
        self._worktrees = worktrees
        self._agents = agents
        self._session_contexts = session_contexts
        self._clock = clock

    def slugify_task(self, value: str, *, fallback: str = "task") -> str:
        normalized = _SLUG_PATTERN.sub("-", value.strip().lower()).strip("-")
        collapsed = re.sub(r"-+", "-", normalized)
        return collapsed or fallback

    def plan_worktree(
        self,
        cwd: str | Path,
        *,
        slug: str | None = None,
        task_title: str | None = None,
        branch: str | None = None,
        base_branch: str | None = None,
    ) -> WorktreeNamingPlan:
        repo_root = self._git.discover_repo_root(cwd)
        repo_name = repo_root.name
        effective_slug = self._derive_slug(slug=slug, task_title=task_title, branch=branch)
        branch_name = branch.strip() if branch and branch.strip() else self._default_branch_name(effective_slug)
        effective_base_branch = (base_branch or self._config.general.default_base_branch).strip()
        worktree_name = self._config.naming.worktree_name(repo=repo_name, slug=effective_slug)
        workspace_root = self._config.paths.workspace_root.expanduser().resolve(strict=False)
        worktree_path = (workspace_root / worktree_name).resolve(strict=False)
        self._ensure_under_workspace_root(worktree_path)
        return WorktreeNamingPlan(
            repo_root=repo_root,
            repo_name=repo_name,
            slug=effective_slug,
            branch_name=branch_name,
            base_branch=effective_base_branch,
            worktree_name=worktree_name,
            worktree_path=worktree_path,
        )

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
    ) -> WorktreeCreateResult:
        plan = self.plan_worktree(
            cwd,
            slug=slug,
            task_title=task_title,
            branch=branch,
            base_branch=base_branch,
        )
        known_worktrees = self._git.list_worktrees(plan.repo_root)
        self._ensure_create_safe(plan=plan, worktrees=known_worktrees, force=force)
        request = self._build_create_request(plan=plan, explicit_branch=branch is not None, force=force)
        outcome = self._git.create_worktree(plan.repo_root, request)
        existing = self._worktrees.get_worktree_by_path(str(plan.worktree_path))
        persisted = self._refresh_worktree_record(
            outcome.worktree.path,
            base_branch=plan.base_branch,
            assigned_agent_id=attach_agent_id,
            existing=existing,
        )
        conflicts = self.detect_orphan_conflicts(plan.repo_root)
        return WorktreeCreateResult(
            plan=plan,
            git_outcome=outcome,
            worktree=persisted,
            conflicts=conflicts,
        )

    def attach_worktree(
        self,
        cwd_or_path: str | Path,
        *,
        agent_id: str | None = None,
        allow_reassign: bool = False,
    ) -> WorktreeAttachResult:
        snapshot = self._git.inspect_repository(cwd_or_path)
        git_worktree = snapshot.current_worktree
        if git_worktree is None:
            msg = f"no git worktree found for {cwd_or_path!s}"
            raise PersistenceError(msg)
        existing = self._worktrees.get_worktree_by_path(str(git_worktree.path))
        previous_assignment = None if existing is None else existing.assigned_agent_id
        if agent_id is not None:
            self._ensure_attach_safe(existing=existing, agent_id=agent_id, allow_reassign=allow_reassign)
        persisted = self._worktree_from_snapshot(
            snapshot=snapshot,
            git_worktree=git_worktree,
            base_branch=None if existing is None else existing.base_branch,
            assigned_agent_id=agent_id if agent_id is not None else previous_assignment,
            existing=existing,
        )
        self._worktrees.upsert_worktree(persisted)
        conflicts = self.detect_orphan_conflicts(snapshot.repo_root)
        return WorktreeAttachResult(
            worktree=persisted,
            previous_assignment=previous_assignment,
            conflicts=conflicts,
        )

    def remove_worktree(
        self,
        path_or_id: str | Path,
        *,
        force: bool = False,
    ) -> WorktreeRemoveResult:
        existing = self._resolve_worktree(path_or_id)
        if existing is None:
            normalized_path = Path(path_or_id).expanduser().resolve(strict=False)
            target_path = normalized_path
            repo_root = self._git.discover_repo_root(normalized_path)
        else:
            target_path = Path(existing.path)
            repo_root = Path(existing.repo_root)
            if existing.assigned_agent_id is not None and not force:
                msg = f"worktree {existing.path} is assigned to agent {existing.assigned_agent_id}"
                raise DomainValidationError(msg)
            contexts = tuple(self._session_contexts.list_session_contexts_for_worktree(existing.id))
            if contexts and not force:
                msg = f"worktree {existing.path} still has cached session context"
                raise DomainValidationError(msg)
        outcome = self._git.remove_worktree(target_path, force=force)
        conflicts = self.detect_orphan_conflicts(repo_root)
        return WorktreeRemoveResult(path=target_path, git_outcome=outcome, conflicts=conflicts)

    def prune_worktrees(
        self,
        cwd: str | Path,
        *,
        dry_run: bool = False,
        expire: str | None = None,
    ) -> WorktreePruneReport:
        repo_root = self._git.discover_repo_root(cwd)
        before = self._git.list_worktrees(repo_root)
        outcome = self._git.prune_worktrees(repo_root, dry_run=dry_run, expire=expire)
        before_paths = {worktree.path for worktree in before}
        after_paths = {worktree.path for worktree in outcome.worktrees}
        if dry_run:
            pruned_paths = tuple(sorted((worktree.path for worktree in before if worktree.is_prunable), key=str))
        else:
            pruned_paths = tuple(sorted(before_paths - after_paths, key=str))
        conflicts = self.detect_orphan_conflicts(repo_root)
        return WorktreePruneReport(
            repo_root=repo_root,
            dry_run=dry_run,
            pruned_paths=pruned_paths,
            remaining_paths=tuple(sorted(after_paths, key=str)),
            conflicts=conflicts,
            git_outcome=outcome,
        )

    def detect_orphan_conflicts(self, cwd: str | Path) -> tuple[WorktreeOrphanConflict, ...]:
        repo_root = self._git.discover_repo_root(cwd)
        git_worktrees = self._git.list_worktrees(repo_root)
        git_by_path = {worktree.path: worktree for worktree in git_worktrees}
        stored_worktrees = tuple(self._worktrees.list_worktrees_by_repo(str(repo_root)))
        conflicts: list[WorktreeOrphanConflict] = []
        for stored in stored_worktrees:
            stored_path = Path(stored.path)
            git_worktree = git_by_path.get(stored_path)
            if git_worktree is None:
                conflicts.append(
                    WorktreeOrphanConflict(
                        code="store_only_worktree",
                        message=f"stored worktree {stored.path} is missing from git worktree list",
                        repo_root=repo_root,
                        path=stored_path,
                        worktree_id=stored.id,
                        agent_id=stored.assigned_agent_id,
                        branch=stored.branch,
                    )
                )
                continue
            if git_worktree.branch is not None and git_worktree.branch != stored.branch:
                conflicts.append(
                    WorktreeOrphanConflict(
                        code="branch_conflict",
                        message=(
                            f"stored branch {stored.branch} does not match git branch {git_worktree.branch} "
                            f"for {stored.path}"
                        ),
                        repo_root=repo_root,
                        path=stored_path,
                        worktree_id=stored.id,
                        agent_id=stored.assigned_agent_id,
                        branch=stored.branch,
                    )
                )
            for context in self._session_contexts.list_session_contexts_for_worktree(stored.id):
                if context.worktree_path and context.worktree_path != stored.path:
                    conflicts.append(
                        WorktreeOrphanConflict(
                            code="session_context_path_conflict",
                            message=(
                                f"session context for worktree {stored.id} points at {context.worktree_path} "
                                f"instead of {stored.path}"
                            ),
                            repo_root=repo_root,
                            path=stored_path,
                            worktree_id=stored.id,
                            agent_id=context.agent_id,
                            branch=context.branch,
                        )
                    )
        for path, git_worktree in git_by_path.items():
            if self._worktrees.get_worktree_by_path(str(path)) is None:
                conflicts.append(
                    WorktreeOrphanConflict(
                        code="unmanaged_git_worktree",
                        message=f"git worktree {path} is not tracked in the store",
                        repo_root=repo_root,
                        path=path,
                        branch=git_worktree.branch,
                    )
                )
        agent_paths = {
            Path(agent.worktree_path): agent.id
            for agent in self._agents.list_agents()
            if agent.worktree_path is not None and Path(agent.repo_root or repo_root) == repo_root
        }
        for path, agent_id in agent_paths.items():
            tracked = self._worktrees.get_worktree_by_path(str(path))
            if tracked is None:
                conflicts.append(
                    WorktreeOrphanConflict(
                        code="agent_path_orphan",
                        message=f"agent {agent_id} points at untracked worktree {path}",
                        repo_root=repo_root,
                        path=path,
                        agent_id=agent_id,
                    )
                )
        return tuple(sorted(conflicts, key=lambda item: (item.code, str(item.path), item.agent_id or "")))

    def _build_create_request(
        self,
        *,
        plan: WorktreeNamingPlan,
        explicit_branch: bool,
        force: bool,
    ) -> GitWorktreeCreateRequest:
        if explicit_branch:
            return GitWorktreeCreateRequest(path=plan.worktree_path, branch=plan.branch_name, force=force)
        return GitWorktreeCreateRequest(
            path=plan.worktree_path,
            branch=plan.branch_name,
            start_point=plan.base_branch,
            create_branch=True,
            force=force,
        )

    def _derive_slug(
        self,
        *,
        slug: str | None,
        task_title: str | None,
        branch: str | None,
    ) -> str:
        if slug is not None and slug.strip():
            return self.slugify_task(slug)
        if task_title is not None and task_title.strip():
            return self.slugify_task(task_title)
        if branch is not None and branch.strip():
            branch_tail = branch.strip().rsplit("/", 1)[-1]
            return self.slugify_task(branch_tail)
        return "task"

    def _default_branch_name(self, slug: str) -> str:
        prefix = self._config.naming.branch_prefix
        return f"{prefix}{slug}" if not slug.startswith(prefix) else slug

    def _ensure_under_workspace_root(self, worktree_path: Path) -> None:
        workspace_root = self._config.paths.workspace_root.expanduser().resolve(strict=False)
        if worktree_path == workspace_root or worktree_path.is_relative_to(workspace_root):
            return
        msg = f"worktree path must stay under workspace root {workspace_root}"
        raise DomainValidationError(msg)

    def _ensure_create_safe(
        self,
        *,
        plan: WorktreeNamingPlan,
        worktrees: Sequence[GitWorktreeInfo],
        force: bool,
    ) -> None:
        normalized_path = plan.worktree_path
        if normalized_path.exists() and not force and all(item.path != normalized_path for item in worktrees):
            msg = f"target path already exists outside managed git worktrees: {normalized_path}"
            raise DomainValidationError(msg)
        if any(item.path == normalized_path for item in worktrees):
            msg = f"git worktree already exists at {normalized_path}"
            raise DomainValidationError(msg)
        if any(item.branch == plan.branch_name for item in worktrees if item.branch is not None):
            msg = f"branch {plan.branch_name} is already attached to another worktree"
            raise DomainValidationError(msg)

    def _ensure_attach_safe(
        self,
        *,
        existing: Worktree | None,
        agent_id: str,
        allow_reassign: bool,
    ) -> None:
        if existing is not None and existing.assigned_agent_id not in (None, agent_id) and not allow_reassign:
            msg = (
                f"worktree {existing.path} is already assigned to agent {existing.assigned_agent_id}; "
                "set allow_reassign=True to override"
            )
            raise DomainValidationError(msg)
        assigned_elsewhere = [
            worktree
            for worktree in self._worktrees.list_worktrees(assigned_agent_id=agent_id)
            if existing is None or worktree.path != existing.path
        ]
        if assigned_elsewhere and not allow_reassign:
            msg = f"agent {agent_id} is already attached to worktree {assigned_elsewhere[0].path}"
            raise DomainValidationError(msg)

    def _refresh_worktree_record(
        self,
        path: Path,
        *,
        base_branch: str | None,
        assigned_agent_id: str | None,
        existing: Worktree | None,
    ) -> Worktree:
        snapshot = self._git.inspect_repository(path)
        git_worktree = snapshot.current_worktree
        if git_worktree is None:
            git_worktree = GitWorktreeInfo(
                repo_root=snapshot.repo_root,
                path=path,
                branch=snapshot.branch,
                is_main_worktree=False,
            )
        worktree = self._worktree_from_snapshot(
            snapshot=snapshot,
            git_worktree=git_worktree,
            base_branch=base_branch,
            assigned_agent_id=assigned_agent_id,
            existing=existing,
        )
        self._worktrees.upsert_worktree(worktree)
        return worktree

    def _worktree_from_snapshot(
        self,
        *,
        snapshot: GitRepositorySnapshot,
        git_worktree: GitWorktreeInfo,
        base_branch: str | None,
        assigned_agent_id: str | None,
        existing: Worktree | None,
    ) -> Worktree:
        now = self._clock()
        return Worktree(
            id=existing.id if existing is not None else str(WorktreeId.generate()),
            repo_root=str(snapshot.repo_root),
            path=str(git_worktree.path),
            branch=(
                git_worktree.branch
                or snapshot.branch
                or (existing.branch if existing is not None else None)
                or "detached"
            ),
            base_branch=base_branch if base_branch is not None else (existing.base_branch if existing is not None else None),
            is_main_worktree=git_worktree.is_main_worktree,
            is_dirty=snapshot.is_dirty,
            ahead_count=snapshot.ahead_behind.ahead,
            behind_count=snapshot.ahead_behind.behind,
            locked=git_worktree.is_locked,
            assigned_agent_id=(
                assigned_agent_id if assigned_agent_id is not None else (existing.assigned_agent_id if existing is not None else None)
            ),
            created_at=existing.created_at if existing is not None else now,
            last_seen_at=now,
        )

    def _resolve_worktree(self, path_or_id: str | Path) -> Worktree | None:
        if isinstance(path_or_id, Path):
            return self._worktrees.get_worktree_by_path(str(path_or_id.resolve(strict=False)))
        direct = self._worktrees.get_worktree(path_or_id)
        if direct is not None:
            return direct
        return self._worktrees.get_worktree_by_path(str(Path(path_or_id).expanduser().resolve(strict=False)))


__all__ = [
    "WorktreeAttachResult",
    "WorktreeCreateResult",
    "WorktreeNamingPlan",
    "WorktreeOrphanConflict",
    "WorktreePruneReport",
    "WorktreeRemoveResult",
    "WorktreeService",
]
