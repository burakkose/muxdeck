# ruff: noqa: I001,PT009,PT027

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import shutil
import unittest

from copilot_commander.adapters import DEFAULT_DATABASE_FILE_NAME, SQLiteStore
from copilot_commander.adapters.git_adapter import (
    GitRepositorySnapshot,
    GitWorktreeCreateOutcome,
    GitWorktreeCreateRequest,
    GitWorktreeInfo,
    GitWorktreePruneOutcome,
    GitWorktreeRemoveOutcome,
)
from copilot_commander.config import AppConfig, PathsConfig
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.models import Agent, Worktree
from copilot_commander.domain.value_objects import CommandResult
from copilot_commander.exceptions import DomainValidationError
from copilot_commander.parsers.git_parser import AheadBehindCounts, GitStatusSummary
from copilot_commander.services.worktree_service import WorktreeService


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
        self._worktrees = [worktree for worktree in self._worktrees if worktree.path != normalized_path]
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
            self._worktrees = [worktree for worktree in self._worktrees if worktree.path not in prunable_paths]
            for path in prunable_paths:
                self._snapshots.pop(path, None)
        return GitWorktreePruneOutcome(
            dry_run=dry_run,
            command_result=_result(("git", "worktree", "prune"), cwd=self.repo_root),
            worktrees=tuple(self._worktrees),
        )


class WorktreeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_dir = Path(__file__).resolve().parent / "_runtime_worktree_service" / self._testMethodName
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
                fallback_database_path=self.runtime_dir / "legacy-state" / DEFAULT_DATABASE_FILE_NAME,
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

    def _make_agent(self, *, agent_id: str = "agent-123", worktree_path: str | None = None) -> Agent:
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
        self.assertEqual(plan.worktree_path, self.workspace_root / f"{self.repo_root.name}--fix-api-state")

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
            self._make_agent(agent_id="agent-1", worktree_path=str(self.workspace_root / "repo--task-one"))
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
