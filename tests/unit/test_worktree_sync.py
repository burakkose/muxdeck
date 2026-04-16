# ruff: noqa: E402,ANN001,ANN201
"""Tests for worktree auto-discovery sync and runtime integration."""

from __future__ import annotations

import sys
import unittest
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

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
from copilot_commander.services.worktree_service import (
    WorktreeService,
)


class FakeGit:
    """Minimal fake implementing WorktreeGitPort."""

    def __init__(
        self,
        worktrees_by_root: dict[str, tuple[GitWorktreeInfo, ...]] | None = None,
    ) -> None:
        self._worktrees = worktrees_by_root or {}

    def discover_repo_root(self, cwd: str | Path, /) -> Path:
        return Path(cwd)

    def list_worktrees(
        self, cwd: str | Path, /
    ) -> tuple[GitWorktreeInfo, ...]:
        return self._worktrees.get(str(Path(cwd).resolve()), ())

    def inspect_repository(
        self, cwd: str | Path, /
    ) -> GitRepositorySnapshot:
        raise NotImplementedError

    def create_worktree(
        self, cwd: str | Path, request: GitWorktreeCreateRequest, /
    ) -> GitWorktreeCreateOutcome:
        raise NotImplementedError

    def remove_worktree(
        self, path: str | Path, /, *, force: bool = False
    ) -> GitWorktreeRemoveOutcome:
        raise NotImplementedError

    def prune_worktrees(
        self,
        cwd: str | Path,
        /,
        *,
        dry_run: bool = False,
        expire: str | None = None,
    ) -> GitWorktreePruneOutcome:
        raise NotImplementedError


class FakeWorktreeStore:
    """In-memory worktree store."""

    def __init__(self) -> None:
        self._worktrees: dict[str, Worktree] = {}
        self._by_path: dict[str, str] = {}

    def upsert_worktree(self, worktree: Worktree, /) -> None:
        self._worktrees[worktree.id] = worktree
        self._by_path[worktree.path] = worktree.id

    def get_worktree(self, worktree_id: str, /) -> Worktree | None:
        return self._worktrees.get(worktree_id)

    def get_worktree_by_path(self, path: str, /) -> Worktree | None:
        wt_id = self._by_path.get(path)
        return self._worktrees.get(wt_id) if wt_id else None

    def list_worktrees(
        self,
        /,
        *,
        repo_root: str | None = None,
        assigned_agent_id: str | None = None,
    ) -> Sequence[Worktree]:
        result = list(self._worktrees.values())
        if repo_root:
            result = [w for w in result if w.repo_root == repo_root]
        if assigned_agent_id:
            result = [
                w for w in result if w.assigned_agent_id == assigned_agent_id
            ]
        return result

    def list_worktrees_by_repo(
        self, repo_root: str, /
    ) -> Sequence[Worktree]:
        return [
            w for w in self._worktrees.values() if w.repo_root == repo_root
        ]

    def delete_worktree(self, worktree_id: str, /) -> bool:
        wt = self._worktrees.pop(worktree_id, None)
        if wt is not None:
            self._by_path.pop(wt.path, None)
            return True
        return False


class FakeAgentStore:
    """In-memory agent store."""

    def __init__(self, agents: Sequence[Agent] = ()) -> None:
        self._agents = list(agents)

    def list_agents(self) -> Sequence[Agent]:
        return self._agents


class FakeSessionContextStore:
    """Empty session context store."""

    def list_session_contexts_for_worktree(
        self, worktree_id: str, /
    ) -> Sequence[SessionContextRecord]:
        return ()


def _make_git_worktree(
    repo_root: str,
    path: str,
    branch: str | None = "main",
    *,
    is_main: bool = False,
    is_bare: bool = False,
    is_locked: bool = False,
) -> GitWorktreeInfo:
    return GitWorktreeInfo(
        repo_root=Path(repo_root),
        path=Path(path),
        branch=branch,
        is_main_worktree=is_main,
        is_bare=is_bare,
        is_locked=is_locked,
    )


def _make_service(
    worktrees_by_root: dict[str, tuple[GitWorktreeInfo, ...]] | None = None,
    agents: Sequence[Agent] | None = None,
    store: FakeWorktreeStore | None = None,
) -> tuple[WorktreeService, FakeWorktreeStore]:
    wt_store = store or FakeWorktreeStore()
    return (
        WorktreeService(
            config=AppConfig.default(),
            git=FakeGit(worktrees_by_root),
            worktrees=wt_store,
            agents=FakeAgentStore(agents or ()),
            session_contexts=FakeSessionContextStore(),
        ),
        wt_store,
    )


class TestWorktreeSync(unittest.TestCase):
    """Tests for WorktreeService.sync_worktrees_from_git."""

    def test_sync_discovers_new_worktrees(self) -> None:
        root = "/home/user/repo"
        git_wts = (
            _make_git_worktree(root, root, "main", is_main=True),
            _make_git_worktree(root, f"{root}/wt-feat", "feat/login"),
        )
        service, store = _make_service({root: git_wts})

        report = service.sync_worktrees_from_git([Path(root)])

        assert report.repo_roots_scanned == 1
        assert report.worktrees_upserted == 2
        assert report.worktrees_total == 2
        assert len(report.errors) == 0
        assert len(store.list_worktrees()) == 2

    def test_sync_updates_existing_worktree(self) -> None:
        root = "/home/user/repo"
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        existing = Worktree(
            id="wt-1",
            repo_root=root,
            path=root,
            branch="main",
            is_main_worktree=True,
            assigned_agent_id="agent-1",
            created_at=now,
            last_seen_at=now,
        )
        store = FakeWorktreeStore()
        store.upsert_worktree(existing)

        git_wts = (
            _make_git_worktree(root, root, "main", is_main=True),
        )
        service, _ = _make_service({root: git_wts}, store=store)

        report = service.sync_worktrees_from_git([Path(root)])

        assert report.worktrees_upserted == 1
        updated = store.get_worktree("wt-1")
        assert updated is not None
        # Preserves agent assignment
        assert updated.assigned_agent_id == "agent-1"
        # Updates last_seen_at
        assert updated.last_seen_at > now

    def test_sync_skips_bare_worktrees(self) -> None:
        root = "/home/user/repo"
        git_wts = (
            _make_git_worktree(root, root, is_bare=True),
            _make_git_worktree(root, f"{root}/wt-a", "feat/a"),
        )
        service, store = _make_service({root: git_wts})

        report = service.sync_worktrees_from_git([Path(root)])

        assert report.worktrees_total == 1
        assert report.worktrees_upserted == 1

    def test_sync_deduplicates_repo_roots(self) -> None:
        root = "/home/user/repo"
        git_wts = (
            _make_git_worktree(root, root, "main", is_main=True),
        )
        service, store = _make_service({root: git_wts})

        report = service.sync_worktrees_from_git(
            [Path(root), Path(root), Path(root)]
        )

        assert report.repo_roots_scanned == 1
        assert report.worktrees_upserted == 1

    def test_sync_handles_git_error_gracefully(self) -> None:
        """If git fails for one root, other roots still sync."""
        root_ok = "/home/user/repo-ok"
        root_bad = "/home/user/repo-bad"
        git_wts_ok = (
            _make_git_worktree(root_ok, root_ok, "main", is_main=True),
        )

        class ErrorGit(FakeGit):
            def list_worktrees(
                self, cwd: str | Path, /
            ) -> tuple[GitWorktreeInfo, ...]:
                if str(cwd) == root_bad:
                    msg = "not a git repository"
                    raise RuntimeError(msg)
                return super().list_worktrees(cwd)

        store = FakeWorktreeStore()
        service = WorktreeService(
            config=AppConfig.default(),
            git=ErrorGit({root_ok: git_wts_ok}),
            worktrees=store,
            agents=FakeAgentStore(),
            session_contexts=FakeSessionContextStore(),
        )

        report = service.sync_worktrees_from_git(
            [Path(root_bad), Path(root_ok)]
        )

        assert report.repo_roots_scanned == 2
        assert report.worktrees_upserted == 1
        assert len(report.errors) == 1
        assert "not a git repository" in report.errors[0]

    def test_sync_empty_repo_roots(self) -> None:
        service, store = _make_service()

        report = service.sync_worktrees_from_git([])

        assert report.repo_roots_scanned == 0
        assert report.worktrees_upserted == 0

    def test_sync_assigns_agent_from_live_agents(self) -> None:
        """Agent worktree_path should map to assigned_agent_id."""
        root = "/home/user/repo"
        wt_path = f"{root}/wt-task"
        agent = Agent(
            id="agent-99",
            name="worker-99",
            tmux_session_name="s",
            tmux_window_id="@1",
            tmux_window_name="work",
            tmux_pane_id="%1",
            cwd=wt_path,
            repo_root=root,
            worktree_path=wt_path,
            branch="feat/task",
        )
        git_wts = (
            _make_git_worktree(root, wt_path, "feat/task"),
        )
        service, store = _make_service(
            {root: git_wts}, agents=[agent]
        )

        service.sync_worktrees_from_git([Path(root)])

        wts = store.list_worktrees()
        assert len(wts) == 1
        assert wts[0].assigned_agent_id == "agent-99"


if __name__ == "__main__":
    unittest.main()
