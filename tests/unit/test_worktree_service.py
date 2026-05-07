# ruff: noqa: I001,PT009,PT027

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import shutil
import unittest

from muxdeck.adapters import DEFAULT_DATABASE_FILE_NAME, SQLiteStore
from muxdeck.adapters.git_adapter import (
    GitCommitSummary,
    GitRepositorySnapshot,
    GitWorktreeCreateOutcome,
    GitWorktreeCreateRequest,
    GitWorktreeInfo,
    GitWorktreePruneOutcome,
    GitWorktreeRemoveOutcome,
)
from muxdeck.config import AppConfig, PathsConfig
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.models import Agent, Worktree
from muxdeck.domain.value_objects import CommandResult
from muxdeck.exceptions import DomainValidationError, GitCommandError
from muxdeck.parsers.git_parser import AheadBehindCounts, GitStatusSummary
from muxdeck.services.worktree_service import WorktreeService


class FakeGit:
    def __init__(
        self,
        *,
        repo_root: Path,
        worktrees: tuple[GitWorktreeInfo, ...],
        snapshots: dict[Path, GitRepositorySnapshot],
    ) -> None:
        self.repo_root = repo_root
        self._worktrees = list(worktrees)
        self._snapshots = dict(snapshots)

    def discover_repo_root(self, cwd: str | Path, /) -> Path:
        del cwd
        return self.repo_root

    def list_worktrees(self, cwd: str | Path, /) -> tuple[GitWorktreeInfo, ...]:
        del cwd
        return tuple(self._worktrees)

    def inspect_repository(self, cwd: str | Path, /) -> GitRepositorySnapshot:
        return self._snapshots[Path(cwd).resolve(strict=False)]

    def list_recent_commits(
        self,
        cwd: str | Path,
        /,
        *,
        limit: int = 5,
    ) -> tuple[GitCommitSummary, ...]:
        del cwd, limit
        return ()

    def create_worktree(
        self,
        cwd: str | Path,
        request: GitWorktreeCreateRequest,
        /,
    ) -> GitWorktreeCreateOutcome:
        del cwd
        worktree = GitWorktreeInfo(
            repo_root=self.repo_root,
            path=Path(request.path),
            branch=request.branch,
            is_main_worktree=False,
        )
        self._worktrees.append(worktree)
        snapshot = GitRepositorySnapshot(
            repo_root=self.repo_root,
            branch=request.branch,
            is_dirty=False,
            ahead_behind=AheadBehindCounts(recognized=True),
            status_summary=GitStatusSummary(entries=()),
            current_worktree=worktree,
            safety_issues=(),
        )
        self._snapshots[worktree.path] = snapshot
        return GitWorktreeCreateOutcome(
            request=request,
            worktree=worktree,
            command_result=_result(("git", "worktree", "add"), cwd=self.repo_root),
        )

    def remove_worktree(
        self,
        path: str | Path,
        /,
        *,
        force: bool = False,
    ) -> GitWorktreeRemoveOutcome:
        normalized_path = Path(path).resolve(strict=False)
        self._worktrees = [
            worktree for worktree in self._worktrees if worktree.path != normalized_path
        ]
        self._snapshots.pop(normalized_path, None)
        return GitWorktreeRemoveOutcome(
            path=normalized_path,
            force=force,
            command_result=_result(("git", "worktree", "remove"), cwd=self.repo_root),
        )

    def prune_worktrees(
        self,
        cwd: str | Path,
        /,
        *,
        dry_run: bool = False,
        expire: str | None = None,
    ) -> GitWorktreePruneOutcome:
        del cwd, expire
        if not dry_run:
            prunable_paths = {worktree.path for worktree in self._worktrees if worktree.is_prunable}
            self._worktrees = [
                worktree for worktree in self._worktrees if worktree.path not in prunable_paths
            ]
            for path in prunable_paths:
                self._snapshots.pop(path, None)
        return GitWorktreePruneOutcome(
            dry_run=dry_run,
            command_result=_result(("git", "worktree", "prune"), cwd=self.repo_root),
            worktrees=tuple(self._worktrees),
        )


class WorktreeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_dir = (
            Path(__file__).resolve().parent / "_runtime_worktree_service" / self._testMethodName
        )
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._cleanup_runtime_dir)
        self.repo_root = self.runtime_dir / "repo"
        self.repo_root.mkdir()
        self.workspace_root = self.runtime_dir / "workspaces"
        self.workspace_root.mkdir()
        self.config = AppConfig(
            paths=PathsConfig(
                state_dir=self.runtime_dir / "state",
                workspace_root=self.workspace_root,
                database_path=self.runtime_dir / "state" / DEFAULT_DATABASE_FILE_NAME,
                fallback_database_path=self.runtime_dir
                / "legacy-state"
                / DEFAULT_DATABASE_FILE_NAME,
            ),
            config_file=self.runtime_dir / "config.toml",
        )
        self.store = SQLiteStore.from_config(self.config)
        self.addCleanup(self.store.close)
        self.main_worktree = GitWorktreeInfo(
            repo_root=self.repo_root,
            path=self.repo_root,
            branch="main",
            is_main_worktree=True,
        )
        self.git = FakeGit(
            repo_root=self.repo_root,
            worktrees=(self.main_worktree,),
            snapshots={
                self.repo_root: GitRepositorySnapshot(
                    repo_root=self.repo_root,
                    branch="main",
                    is_dirty=False,
                    ahead_behind=AheadBehindCounts(recognized=True),
                    status_summary=GitStatusSummary(entries=()),
                    current_worktree=self.main_worktree,
                    safety_issues=(),
                )
            },
        )
        self.service = WorktreeService(
            config=self.config,
            git=self.git,
            worktrees=self.store,
            agents=self.store,
            session_contexts=self.store,
        )

    def _cleanup_runtime_dir(self) -> None:
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)

    def _make_agent(
        self, *, agent_id: str = "agent-123", worktree_path: str | None = None
    ) -> Agent:
        started_at = datetime(2025, 1, 1, 12, tzinfo=UTC)
        return Agent(
            id=agent_id,
            name="planner",
            tmux_session_name="muxdeck",
            tmux_window_id="@1",
            tmux_window_name="main",
            tmux_pane_id=f"%{agent_id[-1]}",
            pane_tty="/dev/pts/1",
            cwd=str(self.repo_root),
            repo_root=str(self.repo_root),
            worktree_path=worktree_path,
            branch="task/example",
            task_title="Example",
            task_summary="Summary",
            copilot_session_id=f"copilot-{agent_id}",
            pid=4321,
            status=AgentStatus.RUNNING,
            started_at=started_at,
            last_activity_at=started_at + timedelta(seconds=5),
            last_seen_at=started_at + timedelta(seconds=10),
            idle_seconds=1,
            token_input=2,
            token_output=3,
            token_total=5,
            estimated_cost_usd=Decimal("0.010000"),
        )

    def test_plan_worktree_uses_slug_and_branch_convention(self) -> None:
        plan = self.service.plan_worktree(self.repo_root, task_title="Fix API / State!")

        self.assertEqual(plan.slug, "fix-api-state")
        self.assertEqual(plan.branch_name, "task/fix-api-state")
        self.assertEqual(plan.worktree_name, f"{self.repo_root.name}--fix-api-state")
        self.assertEqual(
            plan.worktree_path, self.workspace_root / f"{self.repo_root.name}--fix-api-state"
        )

    def test_create_worktree_persists_snapshot(self) -> None:
        self.store.upsert_agent(self._make_agent(agent_id="agent-123"))

        result = self.service.create_worktree(
            self.repo_root,
            task_title="Replay assembly",
            attach_agent_id="agent-123",
        )

        persisted = self.store.get_worktree_by_path(str(result.plan.worktree_path))
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.branch, "task/replay-assembly")
        self.assertEqual(persisted.assigned_agent_id, "agent-123")
        self.assertEqual(result.worktree.path, str(result.plan.worktree_path))

    def test_attach_rejects_reassignment_without_override(self) -> None:
        self.store.upsert_agent(
            self._make_agent(
                agent_id="agent-1", worktree_path=str(self.workspace_root / "repo--task-one")
            )
        )
        tracked = Worktree(
            id="worktree-123",
            repo_root=str(self.repo_root),
            path=str(self.workspace_root / "repo--task-one"),
            branch="task/task-one",
            assigned_agent_id="agent-1",
            created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
        )
        self.store.upsert_worktree(tracked)
        attached_worktree = GitWorktreeInfo(
            repo_root=self.repo_root,
            path=Path(tracked.path),
            branch=tracked.branch,
            is_main_worktree=False,
        )
        self.git._worktrees.append(attached_worktree)
        self.git._snapshots[attached_worktree.path] = GitRepositorySnapshot(
            repo_root=self.repo_root,
            branch=tracked.branch,
            is_dirty=False,
            ahead_behind=AheadBehindCounts(recognized=True),
            status_summary=GitStatusSummary(entries=()),
            current_worktree=attached_worktree,
            safety_issues=(),
        )

        with self.assertRaises(DomainValidationError):
            self.service.attach_worktree(attached_worktree.path, agent_id="agent-2")

    def test_detect_orphan_conflicts_reports_store_only_worktree(self) -> None:
        self.store.upsert_worktree(
            Worktree(
                id="worktree-123",
                repo_root=str(self.repo_root),
                path=str(self.workspace_root / "repo--missing"),
                branch="task/missing",
                created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
                last_seen_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
            )
        )

        conflicts = self.service.detect_orphan_conflicts(self.repo_root)

        self.assertEqual(conflicts[0].code, "store_only_worktree")

    def test_prune_worktrees_reports_prunable_paths(self) -> None:
        prunable = GitWorktreeInfo(
            repo_root=self.repo_root,
            path=self.workspace_root / "repo--old",
            branch="task/old",
            is_main_worktree=False,
            is_prunable=True,
        )
        self.git._worktrees.append(prunable)
        self.git._snapshots[prunable.path] = GitRepositorySnapshot(
            repo_root=self.repo_root,
            branch="task/old",
            is_dirty=False,
            ahead_behind=AheadBehindCounts(recognized=True),
            status_summary=GitStatusSummary(entries=()),
            current_worktree=prunable,
            safety_issues=(),
        )

        report = self.service.prune_worktrees(self.repo_root, dry_run=True)

        self.assertEqual(report.pruned_paths, (prunable.path,))

    def test_remove_worktree_reconciles_store_against_remaining_git_worktrees(self) -> None:
        tracked = Worktree(
            id="worktree-live",
            repo_root=str(self.repo_root),
            path=str(self.workspace_root / "repo--task"),
            branch="task/live",
            created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
        )
        ghost = Worktree(
            id="worktree-ghost",
            repo_root=str(self.repo_root),
            path=str(self.workspace_root / "repo--ghost"),
            branch="task/ghost",
            created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
        )
        self.store.upsert_worktree(tracked)
        self.store.upsert_worktree(ghost)
        self.git._worktrees.append(
            GitWorktreeInfo(
                repo_root=self.repo_root,
                path=Path(tracked.path),
                branch=tracked.branch,
                is_main_worktree=False,
            )
        )

        self.service.remove_worktree(tracked.id)

        self.assertIsNone(self.store.get_worktree(tracked.id))
        self.assertIsNone(self.store.get_worktree(ghost.id))

    def test_prune_worktrees_reconciles_store_against_git_state(self) -> None:
        prunable = GitWorktreeInfo(
            repo_root=self.repo_root,
            path=self.workspace_root / "repo--old",
            branch="task/old",
            is_main_worktree=False,
            is_prunable=True,
        )
        stale = Worktree(
            id="worktree-stale",
            repo_root=str(self.repo_root),
            path=str(self.workspace_root / "repo--stale"),
            branch="task/stale",
            created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
        )
        self.store.upsert_worktree(
            Worktree(
                id="worktree-old",
                repo_root=str(self.repo_root),
                path=str(prunable.path),
                branch="task/old",
                created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
                last_seen_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
            )
        )
        self.store.upsert_worktree(stale)
        self.git._worktrees.append(prunable)
        self.git._snapshots[prunable.path] = GitRepositorySnapshot(
            repo_root=self.repo_root,
            branch="task/old",
            is_dirty=False,
            ahead_behind=AheadBehindCounts(recognized=True),
            status_summary=GitStatusSummary(entries=()),
            current_worktree=prunable,
            safety_issues=(),
        )

        report = self.service.prune_worktrees(self.repo_root, dry_run=False)

        self.assertEqual(report.pruned_paths, (prunable.path,))
        self.assertIsNone(self.store.get_worktree_by_path(str(prunable.path)))
        self.assertIsNone(self.store.get_worktree(stale.id))

    def test_attach_worktree_raises_with_no_current_worktree(self) -> None:
        """Test attach_worktree raises PersistenceError when snapshot has no current_worktree."""
        from muxdeck.exceptions import PersistenceError

        original_inspect = self.git.inspect_repository

        def _raise(_cwd: str | Path, /) -> GitRepositorySnapshot:
            return GitRepositorySnapshot(
                repo_root=self.repo_root,
                branch="main",
                is_dirty=False,
                ahead_behind=AheadBehindCounts(recognized=True),
                status_summary=GitStatusSummary(entries=()),
                current_worktree=None,
                safety_issues=(),
            )

        self.git.inspect_repository = _raise  # type: ignore[method-assign]
        try:
            with self.assertRaises(PersistenceError):
                self.service.attach_worktree(self.repo_root)
        finally:
            self.git.inspect_repository = original_inspect  # type: ignore[method-assign]

    def test_remove_worktree_with_path_only_raises_assigned_check(self) -> None:
        """Test remove_worktree checks for assigned_agent_id when force=False."""
        from muxdeck.exceptions import DomainValidationError

        self.store.upsert_agent(self._make_agent(agent_id="agent-1"))
        tracked = Worktree(
            id="worktree-assign",
            repo_root=str(self.repo_root),
            path=str(self.workspace_root / "repo--assigned-check"),
            branch="task/assigned",
            assigned_agent_id="agent-1",
            created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
        )
        self.store.upsert_worktree(tracked)

        with self.assertRaises(DomainValidationError):
            self.service.remove_worktree(tracked.id, force=False)

    def test_detect_orphan_conflicts_with_branch_mismatch_simple(self) -> None:
        """Test that branch mismatch is detected in conflicts."""
        stored = Worktree(
            id="worktree-branch",
            repo_root=str(self.repo_root),
            path=str(self.workspace_root / "repo--branch-check"),
            branch="task/old",
            created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
        )
        self.store.upsert_worktree(stored)
        git_wt = GitWorktreeInfo(
            repo_root=self.repo_root,
            path=Path(stored.path),
            branch="task/new",
            is_main_worktree=False,
        )
        self.git._worktrees.append(git_wt)
        self.git._snapshots[git_wt.path] = GitRepositorySnapshot(
            repo_root=self.repo_root,
            branch="task/new",
            is_dirty=False,
            ahead_behind=AheadBehindCounts(recognized=True),
            status_summary=GitStatusSummary(entries=()),
            current_worktree=git_wt,
            safety_issues=(),
        )

        conflicts = self.service.detect_orphan_conflicts(self.repo_root)

        self.assertTrue(any(c.code == "branch_conflict" for c in conflicts))

    def test_sync_worktrees_deduplicates_repo_roots(self) -> None:
        """Test that sync_worktrees doesn't process the same root twice."""
        report = self.service.sync_worktrees_from_git([self.repo_root, self.repo_root])

        self.assertEqual(report.repo_roots_scanned, 1)

    def test_inspect_git_details_raises_with_unknown_worktree_id(self) -> None:
        """Test inspect_git_details raises PersistenceError for unknown worktree."""
        from muxdeck.exceptions import PersistenceError

        with self.assertRaises(PersistenceError) as cm:
            self.service.inspect_git_details("unknown-id")

        self.assertIn("unknown worktree", str(cm.exception))

    def test_remove_worktree_swallows_not_registered_when_path_absent(self) -> None:
        """Git 'not registered' is swallowed when the on-disk path is missing too."""
        tracked = Worktree(
            id="worktree-ghost",
            repo_root=str(self.repo_root),
            path=str(self.workspace_root / "repo--never-existed"),
            branch="task/ghost",
            created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
        )
        self.store.upsert_worktree(tracked)

        original_remove = self.git.remove_worktree

        def _raise(_path: str | Path, /, *, force: bool = False) -> GitWorktreeRemoveOutcome:
            del force
            raise GitCommandError(
                command="git worktree remove",
                exit_code=128,
                stderr="fatal: '/x' is not registered with this repository",
            )

        self.git.remove_worktree = _raise  # type: ignore[method-assign]
        try:
            result = self.service.remove_worktree(tracked.id)
        finally:
            self.git.remove_worktree = original_remove  # type: ignore[method-assign]

        self.assertTrue(result.already_gone)
        self.assertIsNone(self.store.get_worktree(tracked.id))

    def test_remove_worktree_reraises_other_git_errors(self) -> None:
        """Errors other than 'not registered' propagate."""
        tracked = Worktree(
            id="worktree-locked",
            repo_root=str(self.repo_root),
            path=str(self.workspace_root / "repo--locked"),
            branch="task/locked",
            created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
        )
        self.store.upsert_worktree(tracked)

        original_remove = self.git.remove_worktree

        def _raise(_path: str | Path, /, *, force: bool = False) -> GitWorktreeRemoveOutcome:
            del force
            raise GitCommandError(
                command="git worktree remove",
                exit_code=128,
                stderr="fatal: cannot remove locked worktree",
            )

        self.git.remove_worktree = _raise  # type: ignore[method-assign]
        try:
            with self.assertRaises(GitCommandError):
                self.service.remove_worktree(tracked.id)
        finally:
            self.git.remove_worktree = original_remove  # type: ignore[method-assign]

        # Row remains since git failed
        self.assertIsNotNone(self.store.get_worktree(tracked.id))

    def test_remove_worktree_reraises_not_registered_when_path_still_exists(self) -> None:
        """Even 'not registered' is reraised when the worktree directory is present."""
        wt_path = self.workspace_root / "repo--present"
        wt_path.mkdir()
        (wt_path / "extra").write_text("not empty")
        tracked = Worktree(
            id="worktree-present",
            repo_root=str(self.repo_root),
            path=str(wt_path),
            branch="task/present",
            created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
        )
        self.store.upsert_worktree(tracked)

        original_remove = self.git.remove_worktree

        def _raise(_path: str | Path, /, *, force: bool = False) -> GitWorktreeRemoveOutcome:
            del force
            raise GitCommandError(
                command="git worktree remove",
                exit_code=128,
                stderr="not registered",
            )

        self.git.remove_worktree = _raise  # type: ignore[method-assign]
        try:
            with self.assertRaises(GitCommandError):
                self.service.remove_worktree(tracked.id)
        finally:
            self.git.remove_worktree = original_remove  # type: ignore[method-assign]

        self.assertIsNotNone(self.store.get_worktree(tracked.id))

    def test_remove_worktree_swallows_not_registered_when_dir_empty(self) -> None:
        """Empty directory counts as 'absent' for the swallow path."""
        wt_path = self.workspace_root / "repo--empty-dir"
        wt_path.mkdir()
        tracked = Worktree(
            id="worktree-emptydir",
            repo_root=str(self.repo_root),
            path=str(wt_path),
            branch="task/empty",
            created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
        )
        self.store.upsert_worktree(tracked)

        original_remove = self.git.remove_worktree

        def _raise(_path: str | Path, /, *, force: bool = False) -> GitWorktreeRemoveOutcome:
            del force
            raise GitCommandError(
                command="git worktree remove",
                exit_code=128,
                stderr="not registered",
            )

        self.git.remove_worktree = _raise  # type: ignore[method-assign]
        try:
            result = self.service.remove_worktree(tracked.id)
        finally:
            self.git.remove_worktree = original_remove  # type: ignore[method-assign]

        self.assertTrue(result.already_gone)
        self.assertIsNone(self.store.get_worktree(tracked.id))

    def test_path_is_absent_returns_false_on_oserror(self) -> None:
        """OSError during path probing returns False — the swallow does not engage."""
        from unittest.mock import patch

        with patch("pathlib.Path.exists", side_effect=OSError("io")):
            self.assertFalse(WorktreeService._path_is_absent(Path("/tmp/whatever")))

    def test_remove_worktree_handles_git_list_failure_after_removal(self) -> None:
        """If git list_worktrees fails after delete, removal still completes."""
        tracked = Worktree(
            id="worktree-listfail",
            repo_root=str(self.repo_root),
            path=str(self.workspace_root / "repo--listfail"),
            branch="task/listfail",
            created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
        )
        self.store.upsert_worktree(tracked)
        self.git._worktrees.append(
            GitWorktreeInfo(
                repo_root=self.repo_root,
                path=Path(tracked.path),
                branch=tracked.branch,
                is_main_worktree=False,
            )
        )

        original_list = self.git.list_worktrees
        call_counter = {"n": 0}

        def _maybe_raise(_cwd: str | Path, /) -> tuple[GitWorktreeInfo, ...]:
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                raise RuntimeError("transient git fault")
            return original_list(_cwd)

        self.git.list_worktrees = _maybe_raise  # type: ignore[method-assign]
        try:
            result = self.service.remove_worktree(tracked.id)
        finally:
            self.git.list_worktrees = original_list  # type: ignore[method-assign]

        self.assertFalse(result.already_gone)
        self.assertIsNone(self.store.get_worktree(tracked.id))

    def test_remove_worktree_with_unknown_path_succeeds(self) -> None:
        """Removing an unknown path falls through and resolves the repo root via git."""
        unknown_path = self.workspace_root / "no-such-worktree"
        result = self.service.remove_worktree(unknown_path)
        self.assertEqual(result.path, unknown_path.resolve(strict=False))

    def test_remove_worktree_blocked_by_session_context(self) -> None:
        """Cached session context blocks removal when force=False."""
        from muxdeck.adapters.sqlite_store import SessionContextRecord
        from muxdeck.domain.models import Session

        # Need an agent + session for the FK chain
        agent = self._make_agent(agent_id="agent-x")
        self.store.upsert_agent(agent)
        self.store.upsert_session(
            Session(id="sess-x", agent_id=agent.id, created_at=datetime(2025, 1, 1, 12, tzinfo=UTC))
        )
        tracked = Worktree(
            id="worktree-with-context",
            repo_root=str(self.repo_root),
            path=str(self.workspace_root / "repo--context"),
            branch="task/context",
            created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
        )
        self.store.upsert_worktree(tracked)
        self.store.upsert_session_context(
            SessionContextRecord(
                session_id="sess-x",
                agent_id=agent.id,
                worktree_id=tracked.id,
                worktree_path=tracked.path,
                repo_root=tracked.repo_root,
                branch=tracked.branch,
                tmux_pane_id=agent.tmux_pane_id,
            )
        )

        with self.assertRaises(DomainValidationError):
            self.service.remove_worktree(tracked.id)

    def test_attach_rejects_when_agent_already_attached_elsewhere(self) -> None:
        """An agent already pointed at another worktree cannot be reassigned without override."""
        # Need an agent row to satisfy the FK on assigned_agent_id.
        self.store.upsert_agent(self._make_agent(agent_id="agent-99"))
        first_path = self.workspace_root / "repo--first"
        owned = Worktree(
            id="worktree-first",
            repo_root=str(self.repo_root),
            path=str(first_path),
            branch="task/first",
            assigned_agent_id="agent-99",
            created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
        )
        self.store.upsert_worktree(owned)

        # Try to attach the same agent to a second git worktree
        second_path = self.workspace_root / "repo--second"
        second_wt = GitWorktreeInfo(
            repo_root=self.repo_root,
            path=second_path,
            branch="task/second",
            is_main_worktree=False,
        )
        self.git._worktrees.append(second_wt)
        self.git._snapshots[second_wt.path] = GitRepositorySnapshot(
            repo_root=self.repo_root,
            branch="task/second",
            is_dirty=False,
            ahead_behind=AheadBehindCounts(recognized=True),
            status_summary=GitStatusSummary(entries=()),
            current_worktree=second_wt,
            safety_issues=(),
        )
        with self.assertRaises(DomainValidationError):
            self.service.attach_worktree(second_path, agent_id="agent-99")

    def test_derive_slug_falls_back_to_branch_tail(self) -> None:
        """When no slug or task title, the branch tail is slugified."""
        plan = self.service.plan_worktree(
            self.repo_root, branch="feature/cool-thing", slug=None, task_title=None
        )
        self.assertEqual(plan.slug, "cool-thing")

    def test_derive_slug_falls_back_to_default_when_nothing(self) -> None:
        """When no inputs, returns 'task'."""
        plan = self.service.plan_worktree(self.repo_root)
        self.assertEqual(plan.slug, "task")

    def test_ensure_under_workspace_root_rejects_outside_path(self) -> None:
        """The private guard refuses paths outside the workspace root."""
        outside = self.runtime_dir / "outside-of-workspace"
        with self.assertRaises(DomainValidationError):
            self.service._ensure_under_workspace_root(outside / "thing")

    def test_create_worktree_blocked_when_path_exists_outside_git(self) -> None:
        """If the target path exists on disk and isn't a git worktree, creation is blocked."""
        # Place an existing path matching the planned worktree path.
        repo_name = self.repo_root.name
        target = self.workspace_root / f"{repo_name}--collision"
        target.mkdir()
        with self.assertRaises(DomainValidationError):
            self.service.create_worktree(self.repo_root, slug="collision")

    def test_create_worktree_blocked_when_branch_already_attached(self) -> None:
        """If a branch is already attached to a different worktree, creation is blocked."""
        existing_path = self.workspace_root / "repo--has-branch"
        existing = GitWorktreeInfo(
            repo_root=self.repo_root,
            path=existing_path,
            branch="task/dup",
            is_main_worktree=False,
        )
        self.git._worktrees.append(existing)
        self.git._snapshots[existing.path] = GitRepositorySnapshot(
            repo_root=self.repo_root,
            branch="task/dup",
            is_dirty=False,
            ahead_behind=AheadBehindCounts(recognized=True),
            status_summary=GitStatusSummary(entries=()),
            current_worktree=existing,
            safety_issues=(),
        )
        with self.assertRaises(DomainValidationError):
            self.service.create_worktree(self.repo_root, slug="dup", branch="task/dup")


def _result(command: tuple[str, ...], *, cwd: Path) -> CommandResult:
    started_at = datetime(2025, 1, 1, tzinfo=UTC)
    return CommandResult(
        command=command,
        exit_code=0,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
        cwd=cwd,
    )


if __name__ == "__main__":
    unittest.main()
