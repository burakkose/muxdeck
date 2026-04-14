# ruff: noqa: E402,I001,PT009

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import shutil
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

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
from copilot_commander.controllers.worktree_controller import WorktreeController
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.models import Agent
from copilot_commander.domain.value_objects import CommandResult
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
        self._worktrees = [
            worktree for worktree in self._worktrees if worktree.path != normalized_path
        ]
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
        return GitWorktreePruneOutcome(
            dry_run=dry_run,
            command_result=_result(("git", "worktree", "prune"), cwd=self.repo_root),
            worktrees=tuple(self._worktrees),
        )


class WorktreeControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_dir = (
            Path(__file__).resolve().parent
            / "_runtime_worktree_controller"
            / self._testMethodName
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
                fallback_database_path=(
                    self.runtime_dir / "legacy-state" / DEFAULT_DATABASE_FILE_NAME
                ),
            ),
            config_file=self.runtime_dir / "config.toml",
        )
        self.store = SQLiteStore.from_config(self.config)
        self.addCleanup(self.store.close)
        main_worktree = GitWorktreeInfo(
            repo_root=self.repo_root,
            path=self.repo_root,
            branch="main",
            is_main_worktree=True,
        )
        self.git = FakeGit(
            repo_root=self.repo_root,
            worktrees=(main_worktree,),
            snapshots={
                self.repo_root: GitRepositorySnapshot(
                    repo_root=self.repo_root,
                    branch="main",
                    is_dirty=False,
                    ahead_behind=AheadBehindCounts(recognized=True),
                    status_summary=GitStatusSummary(entries=()),
                    current_worktree=main_worktree,
                    safety_issues=(),
                )
            },
        )
        self.store.upsert_agent(
            Agent(
                id="agent-1",
                name="Planner",
                tmux_session_name="muxdeck",
                tmux_window_id="@1",
                tmux_pane_id="%1",
                cwd=str(self.repo_root),
                repo_root=str(self.repo_root),
                branch="task/replay",
                status=AgentStatus.RUNNING,
                started_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
                last_seen_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
            )
        )
        self.service = WorktreeService(
            config=self.config,
            git=self.git,
            worktrees=self.store,
            agents=self.store,
            session_contexts=self.store,
        )
        self.controller = WorktreeController(self.service, self.store)

    def _cleanup_runtime_dir(self) -> None:
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)

    def test_create_list_and_start_agent_intent(self) -> None:
        created = self.controller.create_worktree(
            self.repo_root,
            task_title="Replay state",
            attach_agent_id="agent-1",
        )
        worktrees = self.controller.list_worktrees(repo_root=str(self.repo_root))
        assert created.worktree is not None
        detail = self.controller.get_worktree_detail(created.worktree.summary.worktree_id)
        start_intent = self.controller.start_agent_intent(
            created.worktree.summary.worktree_id,
            model="gpt-5.4",
        )

        self.assertEqual(len(worktrees), 1)
        self.assertEqual(worktrees[0].assigned_agent_name, "Planner")
        self.assertFalse(detail.summary.has_conflicts)
        self.assertEqual(start_intent.model, "gpt-5.4")
        self.assertIn("task/replay-state", start_intent.prompt)
        self.assertEqual(start_intent.suggested_window_name, "replay-state")


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
