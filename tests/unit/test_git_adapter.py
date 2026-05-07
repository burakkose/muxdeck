# ruff: noqa: PT027

from __future__ import annotations

import unittest
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from muxdeck.adapters.git_adapter import (
    GitAdapter,
    GitWorktreeCreateRequest,
    _translate_windows_drive_path,
)
from muxdeck.domain.value_objects import CommandResult
from muxdeck.exceptions import CommandError, GitCommandError


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

    def test_list_recent_commits_parses_git_log_output(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    (
                        "git",
                        "log",
                        "-2",
                        "--date=relative",
                        "--pretty=format:%h%x1f%cr%x1f%s",
                    ),
                    stdout=(
                        "abc1234\x1f2 hours ago\x1fFix worktree board\n"
                        "fedcba9\x1f1 day ago\x1fAdd git panel"
                    ),
                    cwd=Path("/repo/worktrees/task-one"),
                ),
            )
        )
        adapter = GitAdapter(runner)

        commits = adapter.list_recent_commits("/repo/worktrees/task-one", limit=2)

        assert [(commit.short_sha, commit.relative_date, commit.subject) for commit in commits] == [
            ("abc1234", "2 hours ago", "Fix worktree board"),
            ("fedcba9", "1 day ago", "Add git panel"),
        ]

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


class GitWorktreeCreateRequestValidationTests(unittest.TestCase):
    def test_create_branch_requires_branch(self) -> None:
        with self.assertRaises(ValueError):
            GitWorktreeCreateRequest(path="/p", branch=None, create_branch=True)

    def test_detach_with_create_branch_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            GitWorktreeCreateRequest(path="/p", branch="b", create_branch=True, detach=True)

    def test_detach_with_branch_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            GitWorktreeCreateRequest(path="/p", branch="b", detach=True)


class GitAdapterConstructorTests(unittest.TestCase):
    def test_zero_or_negative_timeout_rejected(self) -> None:
        with self.assertRaises(ValueError):
            GitAdapter(FakeRunner(()), timeout_sec=0)
        with self.assertRaises(ValueError):
            GitAdapter(FakeRunner(()), timeout_sec=-0.1)


class BuildCreateCommandDetachTests(unittest.TestCase):
    def test_detach_appends_detach_and_skips_branch(self) -> None:
        adapter = GitAdapter(FakeRunner(()))
        request = GitWorktreeCreateRequest(path="/repo/wt-detached", detach=True)
        cmd = adapter.build_create_worktree_command(request)
        assert "--detach" in cmd
        # Last arg is the path; no branch token follows.
        assert cmd[-1] == "/repo/wt-detached"


class ListRecentCommitsEdgeTests(unittest.TestCase):
    def test_zero_or_negative_limit_returns_empty(self) -> None:
        adapter = GitAdapter(FakeRunner(()))
        assert adapter.list_recent_commits("/repo", limit=0) == ()
        assert adapter.list_recent_commits("/repo", limit=-1) == ()

    def test_no_history_swallows_known_snippets(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    ("git", "log"),
                    exit_code=128,
                    stderr="fatal: your current branch 'main' does not have any commits yet",
                ),
            )
        )
        adapter = GitAdapter(runner)
        assert adapter.list_recent_commits("/repo", limit=5) == ()

    def test_re_raises_on_unexpected_failure(self) -> None:
        runner = FakeRunner(
            (_result(("git", "log"), exit_code=128, stderr="fatal: not a git repository"),)
        )
        adapter = GitAdapter(runner)
        with self.assertRaises(GitCommandError):
            adapter.list_recent_commits("/repo", limit=5)

    def test_malformed_log_line_raises_with_unexpected_output(self) -> None:
        # log returns a line with no separator → raise.
        runner = FakeRunner((_result(("git", "log"), stdout="single-token-no-separator\n"),))
        adapter = GitAdapter(runner)
        with self.assertRaises(GitCommandError) as ctx:
            adapter.list_recent_commits("/repo", limit=5)
        assert "unexpected git log output" in (ctx.exception.stderr or "")

    def test_blank_log_line_is_skipped(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    ("git", "log"),
                    stdout="\n\nabc1234\x1f1 hour ago\x1ffix it\n",
                ),
            )
        )
        adapter = GitAdapter(runner)
        commits = adapter.list_recent_commits("/repo", limit=5)
        assert len(commits) == 1
        assert commits[0].short_sha == "abc1234"


class AheadBehindFallbackTests(unittest.TestCase):
    def test_rev_list_fallback_for_no_upstream_returns_empty(self) -> None:
        # 1) status (porcelain --branch) returns line without ahead/behind data
        # 2) current_branch returns "main"
        # 3) rev-list fails with no-upstream snippet
        runner = FakeRunner(
            (
                _result(("git", "status"), stdout="## main\n"),
                _result(("git", "branch", "--show-current"), stdout="main\n"),
                _result(
                    ("git", "rev-list"),
                    exit_code=128,
                    stderr="fatal: no upstream configured for branch 'main'",
                ),
            )
        )
        adapter = GitAdapter(runner)
        counts = adapter.ahead_behind_counts("/repo")
        assert counts.ahead == 0
        assert counts.behind == 0

    def test_rev_list_fallback_uses_supplied_branch(self) -> None:
        runner = FakeRunner(
            (
                _result(("git", "status"), stdout="## main\n"),
                _result(("git", "rev-list"), stdout="2\t3\n"),
            )
        )
        adapter = GitAdapter(runner)
        counts = adapter.ahead_behind_counts("/repo", branch="main")
        assert counts.ahead == 2
        assert counts.behind == 3

    def test_rev_list_re_raises_unrelated_error(self) -> None:
        runner = FakeRunner(
            (
                _result(("git", "status"), stdout="## main\n"),
                _result(("git", "branch", "--show-current"), stdout="main\n"),
                _result(("git", "rev-list"), exit_code=128, stderr="fatal: bad revision"),
            )
        )
        adapter = GitAdapter(runner)
        with self.assertRaises(GitCommandError):
            adapter.ahead_behind_counts("/repo")

    def test_returns_empty_when_no_branch_and_no_summary(self) -> None:
        # No branch line and current_branch returns empty.
        runner = FakeRunner(
            (
                _result(("git", "status"), stdout=""),
                _result(("git", "branch", "--show-current"), stdout="\n"),
            )
        )
        adapter = GitAdapter(runner)
        counts = adapter.ahead_behind_counts("/repo")
        assert counts.ahead == 0
        assert counts.behind == 0


class RemoveWorktreeUnregisteredAndLockedTests(unittest.TestCase):
    def test_remove_worktree_path_not_registered_raises(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    ("git", "rev-parse", "--show-toplevel"),
                    stdout="/repo\n",
                ),
                _result(
                    ("git", "rev-parse", "--git-common-dir"),
                    stdout="/repo/.git\n",
                ),
                _result(
                    ("git", "worktree", "list", "--porcelain"),
                    stdout="\n".join(
                        (
                            "worktree /repo",
                            "HEAD 1111",
                            "branch refs/heads/main",
                            "",
                        )
                    ),
                ),
            )
        )
        adapter = GitAdapter(runner)
        with self.assertRaises(GitCommandError) as ctx:
            adapter.remove_worktree("/repo/worktrees/missing")
        assert "not registered" in (ctx.exception.stderr or "")

    def test_remove_worktree_locked_without_force_raises(self) -> None:
        # worktree list reports a locked worktree with reason → must
        # require force=True.
        runner = FakeRunner(
            (
                _result(
                    ("git", "rev-parse", "--show-toplevel"),
                    stdout="/repo\n",
                ),
                _result(
                    ("git", "rev-parse", "--git-common-dir"),
                    stdout="/repo/.git\n",
                ),
                _result(
                    ("git", "worktree", "list", "--porcelain"),
                    stdout="\n".join(
                        (
                            "worktree /repo",
                            "HEAD 0000",
                            "branch refs/heads/main",
                            "",
                            "worktree /repo/worktrees/locked",
                            "HEAD ffff",
                            "branch refs/heads/locked",
                            "locked do-not-delete",
                            "",
                        )
                    ),
                ),
            )
        )
        adapter = GitAdapter(runner)
        with self.assertRaises(GitCommandError) as ctx:
            adapter.remove_worktree("/repo/worktrees/locked")
        msg = (ctx.exception.stderr or "").lower()
        assert "do-not-delete" in msg or "locked" in msg


class RunCommandFailureTests(unittest.TestCase):
    def test_non_zero_exit_raises_git_command_error_via_run_command(self) -> None:
        # discover_repo_root failure path returns succeeded=False which
        # routes through _raise_git_error in _run_command.
        runner = FakeRunner(
            (
                _result(
                    ("git", "rev-parse", "--show-toplevel"),
                    exit_code=1,
                    stderr="fatal: nope",
                ),
            )
        )
        adapter = GitAdapter(runner)
        with self.assertRaises(GitCommandError):
            adapter.discover_repo_root("/repo")


class RemoveWorktreeUnmergedAndDirtyTests(unittest.TestCase):
    def test_remove_worktree_dirty_without_force_raises(self) -> None:
        # Path-find via discover + list returns the registered worktree;
        # status returns dirty entries → must require force=True.
        runner = FakeRunner(
            (
                _result(
                    ("git", "rev-parse", "--show-toplevel"),
                    stdout="/repo\n",
                ),
                _result(
                    ("git", "rev-parse", "--git-common-dir"),
                    stdout="/repo/.git\n",
                ),
                _result(
                    ("git", "worktree", "list", "--porcelain"),
                    stdout="\n".join(
                        (
                            "worktree /repo",
                            "HEAD 0000",
                            "branch refs/heads/main",
                            "",
                            "worktree /repo/wt/dirty",
                            "HEAD ffff",
                            "branch refs/heads/dirty",
                            "",
                        )
                    ),
                ),
                _result(
                    ("git", "status", "--short", "--branch", "--untracked-files=all"),
                    # ` M file.py` — modified in worktree (porcelain v1, 2 chars + space + path)
                    stdout=" M file.py\n",
                ),
            )
        )
        adapter = GitAdapter(runner)
        with self.assertRaises(GitCommandError) as ctx:
            adapter.remove_worktree("/repo/wt/dirty")
        assert "uncommitted changes" in (ctx.exception.stderr or "")

    def test_remove_worktree_unmerged_without_force_raises(self) -> None:
        # Use a status entry containing a conflict marker (DD/AA/UU) → unmerged.
        runner = FakeRunner(
            (
                _result(
                    ("git", "rev-parse", "--show-toplevel"),
                    stdout="/repo\n",
                ),
                _result(
                    ("git", "rev-parse", "--git-common-dir"),
                    stdout="/repo/.git\n",
                ),
                _result(
                    ("git", "worktree", "list", "--porcelain"),
                    stdout="\n".join(
                        (
                            "worktree /repo",
                            "HEAD 0000",
                            "branch refs/heads/main",
                            "",
                            "worktree /repo/wt/conflict",
                            "HEAD ffff",
                            "branch refs/heads/conflict",
                            "",
                        )
                    ),
                ),
                _result(
                    ("git", "status", "--short", "--branch", "--untracked-files=all"),
                    # `UU file.py` — unmerged
                    stdout="UU file.py\n",
                ),
            )
        )
        adapter = GitAdapter(runner)
        with self.assertRaises(GitCommandError) as ctx:
            adapter.remove_worktree("/repo/wt/conflict")
        assert "merge conflict" in (ctx.exception.stderr or "").lower()


class PruneWorktreesTests(unittest.TestCase):
    def test_prune_worktrees_returns_outcome_with_remaining(self) -> None:
        runner = FakeRunner(
            (
                _result(("git", "rev-parse", "--show-toplevel"), stdout="/repo\n"),
                _result(("git", "rev-parse", "--git-common-dir"), stdout="/repo/.git\n"),
                _result(("git", "worktree", "prune"), stdout=""),
                _result(
                    ("git", "worktree", "list", "--porcelain"),
                    stdout="\n".join(
                        (
                            "worktree /repo",
                            "HEAD 0000",
                            "branch refs/heads/main",
                            "",
                        )
                    ),
                ),
            )
        )
        adapter = GitAdapter(runner)
        outcome = adapter.prune_worktrees("/repo", dry_run=False)
        assert outcome.dry_run is False
        assert len(outcome.worktrees) == 1


class DiscoverRepoRootEdgeTests(unittest.TestCase):
    def test_blank_common_dir_raises(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    ("git", "rev-parse", "--show-toplevel"),
                    stdout="/repo\n",
                ),
                _result(
                    ("git", "rev-parse", "--git-common-dir"),
                    stdout="\n",
                ),
            )
        )
        adapter = GitAdapter(runner)
        with self.assertRaises(GitCommandError) as ctx:
            adapter.discover_repo_root("/repo")
        assert "common directory" in (ctx.exception.stderr or "")

    def test_blank_worktree_root_raises(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    ("git", "rev-parse", "--show-toplevel"),
                    stdout="\n",
                ),
            )
        )
        adapter = GitAdapter(runner)
        with self.assertRaises(GitCommandError) as ctx:
            adapter.discover_repo_root("/repo")
        assert "worktree root" in (ctx.exception.stderr or "")

    def test_common_dir_without_git_suffix_returns_worktree_root(self) -> None:
        # When git-common-dir doesn't end in ".git", the function returns
        # the worktree root from rev-parse --show-toplevel instead of
        # common_dir.parent. (Covers the else branch on line 216.)
        runner = FakeRunner(
            (
                _result(
                    ("git", "rev-parse", "--show-toplevel"),
                    stdout="/repo\n",
                ),
                _result(
                    ("git", "rev-parse", "--git-common-dir"),
                    stdout="/repo/some-other-dir\n",
                ),
            )
        )
        adapter = GitAdapter(runner)
        repo_root = adapter.discover_repo_root("/repo")
        assert repo_root == Path("/repo")


class IsDirtyHelperTests(unittest.TestCase):
    def test_is_dirty_returns_true_when_status_has_entries(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    ("git", "status"),
                    stdout=" M file.py\n",
                ),
            )
        )
        adapter = GitAdapter(runner)
        assert adapter.is_dirty("/repo") is True

    def test_is_dirty_returns_false_when_clean(self) -> None:
        runner = FakeRunner((_result(("git", "status"), stdout="## main\n"),))
        adapter = GitAdapter(runner)
        assert adapter.is_dirty("/repo") is False


class CurrentBranchTests(unittest.TestCase):
    def test_current_branch_returns_none_when_blank(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    ("git", "branch", "--show-current"),
                    stdout="\n",
                ),
            )
        )
        adapter = GitAdapter(runner)
        assert adapter.current_branch("/repo") is None

    def test_translates_forward_slash_drive_path(self) -> None:
        assert _translate_windows_drive_path("Q:/pm2") == "/mnt/q/pm2"

    def test_translates_backslash_drive_path(self) -> None:
        assert _translate_windows_drive_path("C:\\Users\\me\\repo") == "/mnt/c/Users/me/repo"

    def test_translates_bare_drive_root(self) -> None:
        assert _translate_windows_drive_path("D:/") == "/mnt/d"
        assert _translate_windows_drive_path("D:") == "/mnt/d"

    def test_lowercases_drive_letter(self) -> None:
        assert _translate_windows_drive_path("E:/Foo/Bar") == "/mnt/e/Foo/Bar"

    def test_passes_through_posix_path(self) -> None:
        assert _translate_windows_drive_path("/mnt/q/pm2") is None
        assert _translate_windows_drive_path("/home/user") is None

    def test_passes_through_relative_path(self) -> None:
        assert _translate_windows_drive_path("pm2") is None
        assert _translate_windows_drive_path("./pm2") is None

    def test_rejects_path_with_colon_but_no_separator(self) -> None:
        # ``Q:foo`` is a Windows drive-relative path (rare, ambiguous on
        # POSIX). We refuse to guess.
        assert _translate_windows_drive_path("Q:foo") is None

    def test_rejects_non_letter_drive_prefix(self) -> None:
        assert _translate_windows_drive_path("1:/foo") is None
        assert _translate_windows_drive_path(":/foo") is None


class NormalizePathTests(unittest.TestCase):
    def test_drive_letter_path_is_translated_before_resolve(self) -> None:
        from muxdeck.adapters.git_adapter import _normalize_path

        # On POSIX, ``Path("Q:/pm2").resolve()`` would join the literal
        # text onto the cwd (e.g. ``/home/x/Q:/pm2``). The translator
        # routes it through ``/mnt/<letter>/`` instead, which is what the
        # rest of the adapter assumes.
        assert _normalize_path("Q:/pm2") == Path("/mnt/q/pm2")
        assert _normalize_path("C:\\Users\\me") == Path("/mnt/c/Users/me")
