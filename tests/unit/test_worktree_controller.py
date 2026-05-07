# ruff: noqa: E402,I001,PT009,PT027

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

from muxdeck.adapters import DEFAULT_DATABASE_FILE_NAME, SQLiteStore
from muxdeck.adapters.sqlite_store import SessionContextRecord
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
from muxdeck.controllers.worktree_controller import (
    WorktreeController,
    WorktreeProvenanceKind,
)
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.models import Agent, Session
from muxdeck.domain.value_objects import CommandResult
from muxdeck.parsers.git_parser import AheadBehindCounts, GitStatusEntry, GitStatusSummary
from muxdeck.services.worktree_service import WorktreeOrphanConflict, WorktreeService


class FakeGit:
    def __init__(
        self,
        *,
        repo_root: Path,
        worktrees: tuple[GitWorktreeInfo, ...],
        snapshots: dict[Path, GitRepositorySnapshot],
        recent_commits: dict[Path, tuple[GitCommitSummary, ...]] | None = None,
    ) -> None:
        self.repo_root = repo_root
        self._worktrees = list(worktrees)
        self._snapshots = dict(snapshots)
        self._recent_commits = {} if recent_commits is None else dict(recent_commits)

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
        commits = self._recent_commits.get(Path(cwd).resolve(strict=False), ())
        return commits[:limit]

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
            Path(__file__).resolve().parent / "_runtime_worktree_controller" / self._testMethodName
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

    def test_get_worktree_detail_degrades_when_git_inspection_fails(self) -> None:
        """If a worktree's git internals are unreadable from this host
        (e.g. a Windows-side ``.git`` pointer file referencing a drive
        path that does not resolve under WSL), the detail view must
        still render with a degraded ``branch_status`` instead of
        crashing the screen."""
        from muxdeck.exceptions import GitCommandError

        created = self.controller.create_worktree(
            self.repo_root,
            task_title="Replay state",
            attach_agent_id="agent-1",
        )
        assert created.worktree is not None

        def _raise(_cwd: str | Path, /) -> GitRepositorySnapshot:
            raise GitCommandError(
                command="git rev-parse --show-toplevel",
                stderr="fatal: not a git repository",
                exit_code=128,
            )

        self.git.inspect_repository = _raise  # type: ignore[method-assign]

        detail = self.controller.get_worktree_detail(created.worktree.summary.worktree_id)

        assert detail.branch_status is not None
        self.assertIn("unreadable", detail.branch_status)
        self.assertEqual(detail.status_entries, ())
        self.assertEqual(detail.recent_commits, ())

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
        assert worktrees[0].provenance is not None
        self.assertEqual(worktrees[0].provenance.kind, WorktreeProvenanceKind.ASSIGNED)
        self.assertFalse(detail.summary.has_conflicts)
        self.assertEqual(start_intent.model, "gpt-5.4")
        self.assertIn("task/replay-state", start_intent.prompt)
        self.assertEqual(start_intent.suggested_window_name, "replay-state")

    def test_list_worktrees_skips_conflict_detection(self) -> None:
        """List view must not spawn per-repo git work — that's what made
        the worktree screen sluggish. Conflict detection is reserved for
        the detail view where only one worktree is inspected at a time.
        """
        self.controller.create_worktree(
            self.repo_root,
            task_title="Replay state",
            attach_agent_id="agent-1",
        )

        detect_calls: list[str | Path] = []
        original_detect = self.service.detect_orphan_conflicts

        def spy(repo_root: str | Path) -> tuple[WorktreeOrphanConflict, ...]:
            detect_calls.append(repo_root)
            return original_detect(repo_root)

        self.service.detect_orphan_conflicts = spy  # type: ignore[assignment,method-assign]
        try:
            rows = self.controller.list_worktrees(repo_root=str(self.repo_root))
        finally:
            self.service.detect_orphan_conflicts = original_detect  # type: ignore[method-assign]

        self.assertEqual(detect_calls, [])
        self.assertTrue(rows)
        self.assertFalse(rows[0].has_conflicts)

    def test_detail_includes_git_status_and_recent_commits(self) -> None:
        created = self.controller.create_worktree(
            self.repo_root,
            task_title="Replay state",
            attach_agent_id="agent-1",
        )
        assert created.worktree is not None
        worktree_path = Path(created.worktree.summary.path)
        git_worktree = next(
            worktree for worktree in self.git._worktrees if worktree.path == worktree_path
        )
        self.git._snapshots[worktree_path] = GitRepositorySnapshot(
            repo_root=self.repo_root,
            branch="task/replay-state",
            is_dirty=True,
            ahead_behind=AheadBehindCounts(ahead=1, behind=0, recognized=True),
            status_summary=GitStatusSummary(
                entries=(
                    GitStatusEntry(index_status=" ", worktree_status="M", path="src/app.py"),
                    GitStatusEntry(
                        index_status="?",
                        worktree_status="?",
                        path="notes.txt",
                        is_untracked=True,
                    ),
                ),
                branch_line="## task/replay-state...origin/task/replay-state [ahead 1]",
            ),
            current_worktree=git_worktree,
            safety_issues=(),
        )
        self.git._recent_commits[worktree_path] = (
            GitCommitSummary(
                short_sha="abc1234",
                relative_date="2 hours ago",
                subject="Fix worktree detail panel",
            ),
            GitCommitSummary(
                short_sha="def5678",
                relative_date="1 day ago",
                subject="Add worktree board actions",
            ),
        )

        detail = self.controller.get_worktree_detail(created.worktree.summary.worktree_id)

        self.assertEqual(detail.branch_status, "tracks origin/task/replay-state · ahead 1")
        assert detail.change_summary is not None
        self.assertIn("unstaged", detail.change_summary)
        self.assertIn("untracked", detail.change_summary)
        self.assertEqual(detail.status_entries[0].path, "src/app.py")
        self.assertEqual(detail.recent_commits[0].subject, "Fix worktree detail panel")

    def test_list_worktrees_shows_live_agent_provenance_from_nested_path(self) -> None:
        created = self.controller.create_worktree(self.repo_root, task_title="Replay state")
        assert created.worktree is not None
        worktree_path = Path(created.worktree.summary.path)
        self.store.upsert_agent(
            Agent(
                id="agent-2",
                name="Implementer",
                tmux_session_name="muxdeck",
                tmux_window_id="@2",
                tmux_pane_id="%2",
                cwd=str(worktree_path / "src"),
                repo_root=str(self.repo_root),
                worktree_path=str(worktree_path / "src"),
                branch="task/replay-state",
                status=AgentStatus.RUNNING,
                started_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
                last_seen_at=datetime(2025, 1, 1, 12, 2, tzinfo=UTC),
            )
        )

        summary = self.controller.list_worktrees(repo_root=str(self.repo_root))[0]

        self.assertIsNone(summary.assigned_agent_id)
        self.assertIsNone(summary.assigned_agent_name)
        assert summary.provenance is not None
        self.assertEqual(summary.provenance.kind, WorktreeProvenanceKind.LIVE_AGENT)
        self.assertEqual(summary.provenance.agent_name, "Implementer")

    def test_list_worktrees_uses_recent_session_provenance_when_unassigned(self) -> None:
        created = self.controller.create_worktree(self.repo_root, task_title="Replay state")
        assert created.worktree is not None
        worktree_id = created.worktree.summary.worktree_id
        worktree_path = created.worktree.summary.path
        self.store.upsert_agent(
            Agent(
                id="agent-3",
                name="Reviewer",
                tmux_session_name="muxdeck",
                tmux_window_id="@3",
                tmux_pane_id="%3",
                cwd=str(self.repo_root),
                repo_root=str(self.repo_root),
                branch="task/replay-state",
                status=AgentStatus.RUNNING,
                started_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
                last_seen_at=datetime(2025, 1, 1, 12, 3, tzinfo=UTC),
            )
        )
        self.store.upsert_session(
            Session(
                id="session-3",
                agent_id="agent-3",
                created_at=datetime(2025, 1, 1, 12, 4, tzinfo=UTC),
            )
        )
        self.store.upsert_session_context(
            SessionContextRecord(
                session_id="session-3",
                agent_id="agent-3",
                worktree_id=worktree_id,
                worktree_path=worktree_path,
                updated_at=datetime(2025, 1, 1, 12, 5, tzinfo=UTC),
            )
        )

        summary = self.controller.list_worktrees(repo_root=str(self.repo_root))[0]

        assert summary.provenance is not None
        self.assertEqual(summary.provenance.kind, WorktreeProvenanceKind.SESSION)
        self.assertEqual(summary.provenance.agent_name, "Reviewer")

    def test_list_worktrees_empty(self) -> None:
        """Test list_worktrees returns empty tuple when no worktrees exist."""
        result = self.controller.list_worktrees(repo_root=str(self.repo_root))

        self.assertEqual(result, ())

    def test_get_worktree_detail_with_full_branch_status(self) -> None:
        """Test branch status rendering with tracking information."""
        created = self.controller.create_worktree(
            self.repo_root, task_title="Track Test", attach_agent_id="agent-1"
        )
        assert created.worktree is not None
        worktree_path = Path(created.worktree.summary.path)
        git_wt = next(wt for wt in self.git._worktrees if wt.path == worktree_path)
        self.git._snapshots[worktree_path] = GitRepositorySnapshot(
            repo_root=self.repo_root,
            branch="feature/track",
            is_dirty=False,
            ahead_behind=AheadBehindCounts(recognized=True),
            status_summary=GitStatusSummary(
                entries=(),
                branch_line="## feature/track...origin/feature/track [ahead 1, behind 2]",
            ),
            current_worktree=git_wt,
            safety_issues=(),
        )
        self.git._recent_commits[worktree_path] = ()

        detail = self.controller.get_worktree_detail(created.worktree.summary.worktree_id)

        assert detail.branch_status is not None
        self.assertIn("ahead 1", detail.branch_status)
        self.assertIn("behind 2", detail.branch_status)
        self.assertIn("origin/feature/track", detail.branch_status)

    def test_get_worktree_detail_with_status_entries(self) -> None:
        """Test status entries rendering with various file statuses."""
        created = self.controller.create_worktree(
            self.repo_root, task_title="Status Test", attach_agent_id="agent-1"
        )
        assert created.worktree is not None
        worktree_path = Path(created.worktree.summary.path)
        git_wt = next(wt for wt in self.git._worktrees if wt.path == worktree_path)
        self.git._snapshots[worktree_path] = GitRepositorySnapshot(
            repo_root=self.repo_root,
            branch="status/test",
            is_dirty=True,
            ahead_behind=AheadBehindCounts(recognized=True),
            status_summary=GitStatusSummary(
                entries=(
                    GitStatusEntry(
                        index_status="M",
                        worktree_status=" ",
                        path="modified_staged.py",
                    ),
                    GitStatusEntry(
                        index_status="?",
                        worktree_status="?",
                        path="untracked.txt",
                        is_untracked=True,
                    ),
                ),
            ),
            current_worktree=git_wt,
            safety_issues=(),
        )
        self.git._recent_commits[worktree_path] = ()

        detail = self.controller.get_worktree_detail(created.worktree.summary.worktree_id)

        self.assertEqual(len(detail.status_entries), 2)
        codes = {entry.code for entry in detail.status_entries}
        self.assertIn("M ", codes)
        self.assertIn("??", codes)

    def test_resolve_worktree_by_path_raises_unknown(self) -> None:
        """Test that get_worktree_detail raises for unknown worktree path."""
        from muxdeck.exceptions import PersistenceError

        with self.assertRaises(PersistenceError) as cm:
            self.controller.get_worktree_detail("/nonexistent/path")

        self.assertIn("unknown worktree", str(cm.exception))

    def test_attach_worktree_action(self) -> None:
        """Test that attach_worktree returns proper action view."""
        created = self.controller.create_worktree(self.repo_root, task_title="Attach Test")
        assert created.worktree is not None
        worktree_path = Path(created.worktree.summary.path)

        # Make sure the git snapshot is set up for this path
        if worktree_path not in self.git._snapshots:
            git_wt = next(wt for wt in self.git._worktrees if wt.path == worktree_path)
            self.git._snapshots[worktree_path] = GitRepositorySnapshot(
                repo_root=self.repo_root,
                branch="task/attach-test",
                is_dirty=False,
                ahead_behind=AheadBehindCounts(recognized=True),
                status_summary=GitStatusSummary(entries=()),
                current_worktree=git_wt,
                safety_issues=(),
            )

        result = self.controller.attach_worktree(worktree_path, agent_id="agent-1")

        self.assertEqual(result.action, "attach")
        self.assertIn("attached", result.message)
        assert result.worktree is not None
        self.assertEqual(result.worktree.summary.assigned_agent_id, "agent-1")


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
