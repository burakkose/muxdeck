# ruff: noqa: PT027

from __future__ import annotations

import unittest
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from copilot_commander.adapters.git_adapter import GitAdapter, GitWorktreeCreateRequest
from copilot_commander.domain.value_objects import CommandResult
from copilot_commander.exceptions import CommandError, GitCommandError


class FakeRunner:
    def __init__(self, responses: Sequence[CommandResult | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[tuple[str, ...], Path | None, float | None]] = []

    def run(
        self,
        command: Sequence[str],
        /,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> CommandResult:
        del env
        self.calls.append((tuple(command), cwd, timeout_sec))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class GitAdapterTests(unittest.TestCase):
    def test_build_create_remove_and_prune_commands(self) -> None:
        adapter = GitAdapter(FakeRunner(()))
        create_request = GitWorktreeCreateRequest(
            path="/repo/worktrees/task-one",
            branch="task-one",
            start_point="origin/main",
            create_branch=True,
            force=True,
        )

        assert adapter.build_create_worktree_command(create_request) == (
            "git",
            "worktree",
            "add",
            "--force",
            "-b",
            "task-one",
            "/repo/worktrees/task-one",
            "origin/main",
        )
        assert adapter.build_create_worktree_command(
            GitWorktreeCreateRequest(
                path="/repo/worktrees/task-three",
                branch="task-three",
                create_branch=True,
            )
        ) == ("git", "worktree", "add", "-b", "task-three", "/repo/worktrees/task-three")
        assert adapter.build_create_worktree_command(
            GitWorktreeCreateRequest(path="/repo/worktrees/task-four")
        ) == ("git", "worktree", "add", "/repo/worktrees/task-four")
        assert adapter.build_remove_worktree_command("/repo/worktrees/task-one", force=True) == (
            "git",
            "worktree",
            "remove",
            "--force",
            "/repo/worktrees/task-one",
        )
        assert adapter.build_prune_worktrees_command(dry_run=True, expire="now") == (
            "git",
            "worktree",
            "prune",
            "--dry-run",
            "--expire",
            "now",
        )

    def test_inspect_repository_uses_parser_driven_git_state(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    ("git", "rev-parse", "--path-format=absolute", "--show-toplevel"),
                    stdout="/repo/worktrees/task-one\n",
                ),
                _result(
                    ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"),
                    stdout="/repo/.git\n",
                ),
                _result(
                    ("git", "status", "--short", "--branch", "--untracked-files=all"),
                    stdout=(
                        "## task-one...origin/task-one [ahead 2, behind 1]\n"
                        "UU conflicted.txt\n"
                        " M src/app.py\n"
                    ),
                    cwd=Path("/repo/worktrees/task-one"),
                ),
                _result(
                    ("git", "worktree", "list", "--porcelain"),
                    stdout="\n".join(
                        (
                            "worktree /repo",
                            "HEAD 0123456789abcdef",
                            "branch refs/heads/main",
                            "",
                            "worktree /repo/worktrees/task-one",
                            "HEAD fedcba9876543210",
                            "branch refs/heads/task-one",
                            "locked in-use",
                            "prunable gitdir file points to non-existent location",
                            "",
                        )
                    ),
                    cwd=Path("/repo"),
                ),
                _result(
                    ("git", "branch", "--show-current"),
                    stdout="task-one\n",
                    cwd=Path("/repo/worktrees/task-one"),
                ),
            )
        )
        adapter = GitAdapter(runner)

        snapshot = adapter.inspect_repository("/repo/worktrees/task-one")
        current_worktree = snapshot.current_worktree

        assert snapshot.repo_root == Path("/repo")
        assert snapshot.branch == "task-one"
        assert snapshot.is_dirty is True
        assert (snapshot.ahead_behind.ahead, snapshot.ahead_behind.behind) == (2, 1)
        assert current_worktree is not None
        assert current_worktree.path == Path("/repo/worktrees/task-one")
        assert current_worktree.is_locked is True
        assert current_worktree.is_prunable is True
        assert [issue.code for issue in snapshot.safety_issues] == [
            "dirty_worktree",
            "merge_conflicts",
            "locked_worktree",
            "prunable_worktree",
            "ahead_of_upstream",
            "behind_upstream",
        ]

    def test_discover_repo_root_prefers_common_dir_parent(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    ("git", "rev-parse", "--path-format=absolute", "--show-toplevel"),
                    stdout="/repo/worktrees/task-one\n",
                ),
                _result(
                    ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"),
                    stdout="/repo/.git\n",
                ),
            )
        )
        adapter = GitAdapter(runner)

        repo_root = adapter.discover_repo_root("/repo/worktrees/task-one")

        assert repo_root == Path("/repo")

    def test_discover_repo_root_falls_back_to_worktree_root_for_separate_git_dir(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    ("git", "rev-parse", "--path-format=absolute", "--show-toplevel"),
                    stdout="/repo/worktrees/task-one\n",
                ),
                _result(
                    ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"),
                    stdout="/git-store\n",
                ),
            )
        )
        adapter = GitAdapter(runner)

        repo_root = adapter.discover_repo_root("/repo/worktrees/task-one")

        assert repo_root == Path("/repo/worktrees/task-one")

    def test_inspect_repository_treats_detached_head_as_zero_upstream_counts(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    ("git", "rev-parse", "--path-format=absolute", "--show-toplevel"),
                    stdout="/repo/worktrees/detached\n",
                ),
                _result(
                    ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"),
                    stdout="/repo/.git\n",
                ),
                _result(
                    ("git", "status", "--short", "--branch", "--untracked-files=all"),
                    stdout="## HEAD (no branch)\n",
                    cwd=Path("/repo/worktrees/detached"),
                ),
                _result(
                    ("git", "worktree", "list", "--porcelain"),
                    stdout="\n".join(
                        (
                            "worktree /repo",
                            "HEAD 0123456789abcdef",
                            "branch refs/heads/main",
                            "",
                            "worktree /repo/worktrees/detached",
                            "HEAD fedcba9876543210",
                            "detached",
                            "",
                        )
                    ),
                    cwd=Path("/repo"),
                ),
                _result(
                    ("git", "branch", "--show-current"),
                    stdout="",
                    cwd=Path("/repo/worktrees/detached"),
                ),
            )
        )
        adapter = GitAdapter(runner)

        snapshot = adapter.inspect_repository("/repo/worktrees/detached")

        assert snapshot.branch is None
        assert snapshot.ahead_behind.recognized is False
        assert [issue.code for issue in snapshot.safety_issues] == ["detached_head"]

    def test_create_worktree_rejects_conflicting_branch_without_force(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    ("git", "rev-parse", "--path-format=absolute", "--show-toplevel"),
                    stdout="/repo\n",
                ),
                _result(
                    ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"),
                    stdout="/repo/.git\n",
                ),
                _result(
                    ("git", "worktree", "list", "--porcelain"),
                    stdout="\n".join(
                        (
                            "worktree /repo",
                            "HEAD 0123456789abcdef",
                            "branch refs/heads/main",
                            "",
                            "worktree /repo/worktrees/task-one",
                            "HEAD fedcba9876543210",
                            "branch refs/heads/task-one",
                            "",
                        )
                    ),
                    cwd=Path("/repo"),
                ),
            )
        )
        adapter = GitAdapter(runner)
        request = GitWorktreeCreateRequest(
            path="/repo/worktrees/task-one-copy",
            branch="task-one",
            start_point="task-one",
        )

        with self.assertRaises(GitCommandError) as context:
            adapter.create_worktree("/repo", request)

        assert "branch is already checked out" in (context.exception.stderr or "")
        assert len(runner.calls) == 3

    def test_create_worktree_runs_git_and_returns_discovered_worktree(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    ("git", "rev-parse", "--path-format=absolute", "--show-toplevel"),
                    stdout="/repo\n",
                ),
                _result(
                    ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"),
                    stdout="/repo/.git\n",
                ),
                _result(
                    ("git", "worktree", "list", "--porcelain"),
                    stdout="\n".join(
                        (
                            "worktree /repo",
                            "HEAD 0123456789abcdef",
                            "branch refs/heads/main",
                            "",
                        )
                    ),
                    cwd=Path("/repo"),
                ),
                _result(
                    (
                        "git",
                        "worktree",
                        "add",
                        "-b",
                        "task-two",
                        "/repo/worktrees/task-two",
                        "origin/main",
                    ),
                    cwd=Path("/repo"),
                ),
                _result(
                    ("git", "worktree", "list", "--porcelain"),
                    stdout="\n".join(
                        (
                            "worktree /repo",
                            "HEAD 0123456789abcdef",
                            "branch refs/heads/main",
                            "",
                            "worktree /repo/worktrees/task-two",
                            "HEAD abcdef0123456789",
                            "branch refs/heads/task-two",
                            "",
                        )
                    ),
                    cwd=Path("/repo"),
                ),
            )
        )
        adapter = GitAdapter(runner)
        request = GitWorktreeCreateRequest(
            path="/repo/worktrees/task-two",
            branch="task-two",
            start_point="origin/main",
            create_branch=True,
        )

        outcome = adapter.create_worktree("/repo", request)

        assert outcome.worktree.path == Path("/repo/worktrees/task-two")
        assert outcome.worktree.branch == "task-two"
        assert runner.calls[3][0][0:4] == ("git", "worktree", "add", "-b")

    def test_remove_worktree_rejects_dirty_and_conflicted_worktree_without_force(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    ("git", "rev-parse", "--path-format=absolute", "--show-toplevel"),
                    stdout="/repo/worktrees/task-one\n",
                ),
                _result(
                    ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"),
                    stdout="/repo/.git\n",
                ),
                _result(
                    ("git", "worktree", "list", "--porcelain"),
                    stdout="\n".join(
                        (
                            "worktree /repo",
                            "HEAD 0123456789abcdef",
                            "branch refs/heads/main",
                            "",
                            "worktree /repo/worktrees/task-one",
                            "HEAD fedcba9876543210",
                            "branch refs/heads/task-one",
                            "",
                        )
                    ),
                    cwd=Path("/repo"),
                ),
                _result(
                    ("git", "status", "--short", "--branch", "--untracked-files=all"),
                    stdout="## task-one\nUU conflicted.txt\n M src/app.py\n",
                    cwd=Path("/repo/worktrees/task-one"),
                ),
            )
        )
        adapter = GitAdapter(runner)

        with self.assertRaises(GitCommandError) as context:
            adapter.remove_worktree("/repo/worktrees/task-one")

        assert "merge conflicts" in (context.exception.stderr or "")
        assert len(runner.calls) == 4

    def test_remove_worktree_rejects_main_worktree(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    ("git", "rev-parse", "--path-format=absolute", "--show-toplevel"),
                    stdout="/repo\n",
                ),
                _result(
                    ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"),
                    stdout="/repo/.git\n",
                ),
                _result(
                    ("git", "worktree", "list", "--porcelain"),
                    stdout="\n".join(
                        (
                            "worktree /repo",
                            "HEAD 0123456789abcdef",
                            "branch refs/heads/main",
                            "",
                        )
                    ),
                    cwd=Path("/repo"),
                ),
            )
        )
        adapter = GitAdapter(runner)

        with self.assertRaises(GitCommandError) as context:
            adapter.remove_worktree("/repo")

        assert "main worktree" in (context.exception.stderr or "")

    def test_git_adapter_wraps_runner_errors(self) -> None:
        runner = FakeRunner(
            (
                CommandError(
                    "git rev-parse --path-format=absolute --show-toplevel",
                    stderr="fatal: not a git repository",
                ),
            )
        )
        adapter = GitAdapter(runner)

        with self.assertRaises(GitCommandError):
            adapter.discover_repo_root("/outside")


def _result(
    command: tuple[str, ...],
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    cwd: Path | None = None,
) -> CommandResult:
    started_at = datetime(2024, 1, 1, tzinfo=UTC)
    return CommandResult(
        command=command,
        exit_code=exit_code,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
        stdout=stdout,
        stderr=stderr,
        cwd=cwd,
    )


if __name__ == "__main__":
    unittest.main()
