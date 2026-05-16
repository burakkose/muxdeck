# ruff: noqa: PT027

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from muxdeck.adapters.git_adapter import (
    GitAdapter,
    GitWorktreeCreateRequest,
    _is_windows_stamped_worktree,
    _resolve_windows_git_binary,
    _translate_windows_drive_path,
    _wsl_path_to_windows,
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


class GitAdapterInspectRepoContextTests(unittest.TestCase):
    def test_inspect_repo_context_uses_single_rev_parse_invocation(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    (
                        "git",
                        "rev-parse",
                        "--path-format=absolute",
                        "--show-toplevel",
                        "--git-common-dir",
                        "--abbrev-ref",
                        "HEAD",
                    ),
                    stdout="/repo/worktrees/task-one\n/repo/.git\nfeat/perf\n",
                ),
            )
        )
        adapter = GitAdapter(runner)

        context = adapter.inspect_repo_context("/repo/worktrees/task-one")

        assert context.repo_root == Path("/repo")
        assert context.branch == "feat/perf"
        assert len(runner.calls) == 1
        assert runner.calls[0][0] == (
            "git",
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-common-dir",
            "--abbrev-ref",
            "HEAD",
        )

    def test_inspect_repo_context_falls_back_to_worktree_root_for_separate_git_dir(
        self,
    ) -> None:
        runner = FakeRunner(
            (
                _result(
                    (
                        "git",
                        "rev-parse",
                        "--path-format=absolute",
                        "--show-toplevel",
                        "--git-common-dir",
                        "--abbrev-ref",
                        "HEAD",
                    ),
                    stdout="/repo/worktrees/task-one\n/git-store\nmain\n",
                ),
            )
        )
        adapter = GitAdapter(runner)

        context = adapter.inspect_repo_context("/repo/worktrees/task-one")

        assert context.repo_root == Path("/repo/worktrees/task-one")
        assert context.branch == "main"

    def test_inspect_repo_context_returns_none_branch_for_detached_head(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    (
                        "git",
                        "rev-parse",
                        "--path-format=absolute",
                        "--show-toplevel",
                        "--git-common-dir",
                        "--abbrev-ref",
                        "HEAD",
                    ),
                    stdout="/repo\n/repo/.git\nHEAD\n",
                ),
            )
        )
        adapter = GitAdapter(runner)

        context = adapter.inspect_repo_context("/repo")

        assert context.repo_root == Path("/repo")
        assert context.branch is None

    def test_inspect_repo_context_raises_when_output_is_truncated(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    (
                        "git",
                        "rev-parse",
                        "--path-format=absolute",
                        "--show-toplevel",
                        "--git-common-dir",
                        "--abbrev-ref",
                        "HEAD",
                    ),
                    stdout="/repo\n/repo/.git\n",
                ),
            )
        )
        adapter = GitAdapter(runner)

        with self.assertRaises(GitCommandError):
            adapter.inspect_repo_context("/repo")

    def test_inspect_repo_context_propagates_git_errors(self) -> None:
        runner = FakeRunner(
            (
                CommandError(
                    "git rev-parse",
                    stderr="fatal: not a git repository",
                ),
            )
        )
        adapter = GitAdapter(runner)

        with self.assertRaises(GitCommandError):
            adapter.inspect_repo_context("/outside")


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


class WslPathToWindowsTests(unittest.TestCase):
    """Cover the inverse of ``_translate_windows_drive_path``.

    The remove-worktree routing logic depends on this helper to decide
    when to swap WSL ``git`` for ``git.exe`` and to format the argv
    path the way the Windows-stamped worktree records expect. Each
    edge case below would otherwise leak through as either a
    silently-skipped reroute (``None``) or a malformed Windows arg.
    """

    def test_drive_root_translates_to_backslash(self) -> None:
        assert _wsl_path_to_windows("/mnt/c") == "C:\\"
        assert _wsl_path_to_windows("/mnt/c/") == "C:\\"

    def test_nested_path_uses_backslashes(self) -> None:
        assert _wsl_path_to_windows("/mnt/c/src/Foo") == "C:\\src\\Foo"

    def test_drive_letter_is_uppercased(self) -> None:
        assert _wsl_path_to_windows("/mnt/q/pm2") == "Q:\\pm2"
        assert _wsl_path_to_windows("/mnt/Q/pm2") == "Q:\\pm2"

    def test_path_with_spaces_preserved(self) -> None:
        assert _wsl_path_to_windows("/mnt/c/src/Foo Bar") == "C:\\src\\Foo Bar"

    def test_pathlike_input_accepted(self) -> None:
        assert _wsl_path_to_windows(Path("/mnt/d/repo")) == "D:\\repo"

    def test_non_mount_paths_return_none(self) -> None:
        assert _wsl_path_to_windows("/home/burakkose/muxdeck") is None
        assert _wsl_path_to_windows("/repo/worktrees/task") is None
        assert _wsl_path_to_windows("/") is None

    def test_multichar_mount_segment_rejected(self) -> None:
        # ``/mnt/wsl`` and ``/mnt/cc`` are not Windows drive mounts —
        # routing them through git.exe would emit a garbage ``WSL:\``
        # arg that breaks the remove command outright.
        assert _wsl_path_to_windows("/mnt/wsl/foo") is None
        assert _wsl_path_to_windows("/mnt/cc/foo") is None

    def test_lookalike_paths_rejected(self) -> None:
        # ``/mnt/cfoo`` shares the prefix but isn't a drive mount.
        assert _wsl_path_to_windows("/mnt/cfoo") is None
        assert _wsl_path_to_windows("mnt/c/foo") is None


class BuildRemoveWorktreeCommandWslRoutingTests(unittest.TestCase):
    """Pin the WSL-mount detour for ``build_remove_worktree_command``.

    The user-visible bug: invoking ``delete`` on a worktree that lives
    on a Windows drive (``/mnt/c/...``) under WSL fails because WSL
    ``git`` cannot match its POSIX argument against the Windows-stamped
    ``.git/worktrees/<name>/gitdir`` records. Routing through
    ``git.exe`` with a native ``C:\\...`` argument is what the fix
    actually does — these tests stop a future refactor from
    silently regressing back to WSL ``git`` for Windows-drive targets.
    """

    def test_uses_windows_git_and_translated_path_on_wsl_mount(self) -> None:
        adapter = GitAdapter(FakeRunner(()), is_wsl_runtime=True)
        assert adapter.build_remove_worktree_command(
            "/mnt/c/src/CosmosDB",
            force=True,
        ) == (
            "git.exe",
            "worktree",
            "remove",
            "--force",
            "C:\\src\\CosmosDB",
        )

    def test_uses_windows_git_without_force_flag(self) -> None:
        adapter = GitAdapter(FakeRunner(()), is_wsl_runtime=True)
        assert adapter.build_remove_worktree_command("/mnt/d/repo") == (
            "git.exe",
            "worktree",
            "remove",
            "D:\\repo",
        )

    def test_keeps_default_binary_for_posix_repo_on_wsl(self) -> None:
        # Local Linux worktrees (``/home/...``, ``/repo/...``) must
        # continue to use WSL git even when the runtime is WSL —
        # ``git.exe`` would refuse to open a Linux-only path.
        adapter = GitAdapter(FakeRunner(()), is_wsl_runtime=True)
        assert adapter.build_remove_worktree_command("/repo/worktrees/task-one") == (
            "git",
            "worktree",
            "remove",
            "/repo/worktrees/task-one",
        )

    def test_keeps_default_binary_off_wsl(self) -> None:
        # Outside WSL the ``/mnt/c/...`` path is just a regular POSIX
        # directory (or nothing); using ``git.exe`` would either fail
        # to launch or address the wrong filesystem.
        adapter = GitAdapter(FakeRunner(()), is_wsl_runtime=False)
        assert adapter.build_remove_worktree_command("/mnt/c/src/CosmosDB", force=True) == (
            "git",
            "worktree",
            "remove",
            "--force",
            "/mnt/c/src/CosmosDB",
        )

    def test_custom_windows_binary_is_honoured(self) -> None:
        # Operators with ``git.exe`` shimmed under a different name
        # (e.g. a chocolatey/scoop alias) should still be able to
        # route through the configured Windows binary.
        adapter = GitAdapter(
            FakeRunner(()),
            is_wsl_runtime=True,
            windows_binary="/mnt/c/Program Files/Git/cmd/git.exe",
        )
        command = adapter.build_remove_worktree_command("/mnt/c/src/Foo", force=True)
        assert command[0] == "/mnt/c/Program Files/Git/cmd/git.exe"
        assert command[-1] == "C:\\src\\Foo"


class RemoveWorktreeWslRoutingFlowTests(unittest.TestCase):
    """End-to-end coverage that ``remove_worktree`` actually dispatches
    the Windows-routed command (and not just the builder).

    The builder tests pin the command shape; this one pins the
    runtime invocation against the ``FakeRunner`` so any future
    short-circuit, caching, or refactor that bypasses
    ``build_remove_worktree_command`` still gets caught.
    """

    def test_remove_worktree_invokes_windows_git_for_wsl_mount(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    (
                        "git.exe",
                        "-C",
                        "C:\\src\\CosmosDB-feature",
                        "rev-parse",
                        "--path-format=absolute",
                        "--show-toplevel",
                    ),
                    stdout="/mnt/c/src/CosmosDB\n",
                ),
                _result(
                    (
                        "git.exe",
                        "-C",
                        "C:\\src\\CosmosDB-feature",
                        "rev-parse",
                        "--path-format=absolute",
                        "--git-common-dir",
                    ),
                    stdout="/mnt/c/src/CosmosDB/.git\n",
                ),
                _result(
                    (
                        "git.exe",
                        "-C",
                        "C:\\src\\CosmosDB",
                        "worktree",
                        "list",
                        "--porcelain",
                    ),
                    stdout="\n".join(
                        (
                            "worktree /mnt/c/src/CosmosDB",
                            "HEAD 1111",
                            "branch refs/heads/main",
                            "",
                            "worktree /mnt/c/src/CosmosDB-feature",
                            "HEAD 2222",
                            "branch refs/heads/feature",
                            "",
                        )
                    ),
                ),
                # force=True skips the status pre-check, so the next
                # command must be the actual remove — routed through
                # git.exe with explicit -C and a backslash Windows path.
                _result(
                    (
                        "git.exe",
                        "-C",
                        "C:\\src\\CosmosDB",
                        "worktree",
                        "remove",
                        "--force",
                        "C:\\src\\CosmosDB-feature",
                    ),
                    stdout="",
                ),
            )
        )
        adapter = GitAdapter(
            runner,
            is_wsl_runtime=True,
            windows_binary_resolver=lambda binary: binary,
        )
        # Force routing on /mnt/<letter> paths without seeding the
        # _is_windows_stamped_worktree check (no real .git stamps in
        # the test FS).
        adapter._should_route_to_windows = (  # type: ignore[assignment,method-assign]
            lambda cwd: str(cwd).startswith("/mnt/c/")
        )

        outcome = adapter.remove_worktree("/mnt/c/src/CosmosDB-feature", force=True)

        assert outcome.path == Path("/mnt/c/src/CosmosDB-feature")
        # The final invocation must be the Windows-routed remove —
        # the destructive call MUST be git.exe with explicit -C so
        # the worktree record on disk matches and we never rely on
        # WSL interop's implicit cwd translation.
        final_command, _final_cwd, _timeout = runner.calls[-1]
        assert final_command == (
            "git.exe",
            "-C",
            "C:\\src\\CosmosDB",
            "worktree",
            "remove",
            "--force",
            "C:\\src\\CosmosDB-feature",
        )


class IsWindowsStampedWorktreeTests(unittest.TestCase):
    """Detect linked worktrees whose ``.git`` file references Windows paths.

    Routing in ``_run_command`` only kicks in when this returns True,
    so misclassification here would either resurrect the original
    user-visible bug (false negative) or regress WSL-native repos on
    ``/mnt`` (false positive).
    """

    def test_returns_true_for_backslash_windows_gitdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").write_text(
                "gitdir: C:\\src\\foo\\.git\\worktrees\\bar\n",
                encoding="utf-8",
            )
            assert _is_windows_stamped_worktree(root) is True

    def test_returns_true_for_forward_slash_drive_letter_gitdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").write_text(
                "gitdir: D:/src/foo/.git/worktrees/bar\n",
                encoding="utf-8",
            )
            assert _is_windows_stamped_worktree(root) is True

    def test_returns_false_for_posix_gitdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").write_text(
                "gitdir: /home/me/src/foo/.git/worktrees/bar\n",
                encoding="utf-8",
            )
            assert _is_windows_stamped_worktree(root) is False

    def test_returns_false_when_git_is_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            assert _is_windows_stamped_worktree(root) is False

    def test_returns_false_when_git_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assert _is_windows_stamped_worktree(Path(tmp)) is False

    def test_returns_false_for_unrelated_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").write_text("not a real gitdir file\n", encoding="utf-8")
            assert _is_windows_stamped_worktree(root) is False

    def test_ignores_empty_gitdir_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").write_text("gitdir:   \n", encoding="utf-8")
            assert _is_windows_stamped_worktree(root) is False

    def test_returns_true_when_gitdir_appears_after_other_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").write_text(
                "# header comment\nGITDIR: C:\\src\\foo\\.git\\worktrees\\bar\n",
                encoding="utf-8",
            )
            assert _is_windows_stamped_worktree(root) is True


class RouteCommandForCwdTests(unittest.TestCase):
    """Pin the routing decision: WSL git → git.exe only when Windows-stamped."""

    def _adapter(self) -> GitAdapter:
        # Inject an identity resolver so tests never depend on whether
        # ``git.exe`` is actually installed on the host filesystem.
        return GitAdapter(
            FakeRunner(()),
            is_wsl_runtime=True,
            windows_binary_resolver=lambda binary: binary,
        )

    def test_routes_wsl_git_to_windows_for_stamped_worktree(self) -> None:
        cwd = Path("/mnt/c/worktree")
        adapter = self._adapter()
        adapter._should_route_to_windows = lambda _cwd: True  # type: ignore[assignment,method-assign]

        routed = adapter._route_command_for_cwd(
            ("git", "rev-parse", "--show-toplevel"),
            cwd,
        )
        # git.exe sees a Windows-native cwd via explicit ``-C`` rather
        # than relying on WSL interop's implicit POSIX cwd translation.
        assert routed == (
            "git.exe",
            "-C",
            "C:\\worktree",
            "rev-parse",
            "--show-toplevel",
        )

    def test_passes_through_when_not_routed(self) -> None:
        cwd = Path("/home/me/repo")
        adapter = self._adapter()
        adapter._should_route_to_windows = lambda _cwd: False  # type: ignore[assignment,method-assign]

        routed = adapter._route_command_for_cwd(
            ("git", "rev-parse", "--show-toplevel"),
            cwd,
        )
        assert routed == ("git", "rev-parse", "--show-toplevel")

    def test_normalizes_already_windows_binary_and_injects_dash_c(self) -> None:
        cwd = Path("/mnt/c/src/repo")
        adapter = self._adapter()
        adapter._should_route_to_windows = lambda _cwd: False  # type: ignore[assignment,method-assign]
        # build_remove_worktree_command pre-built a ``git.exe`` tuple
        # because the target path needed translation. _route must
        # still resolve the binary and inject ``-C`` with the cwd in
        # Windows form so git.exe never inherits a POSIX-shaped cwd
        # via WSL interop's implicit translation.
        command = ("git.exe", "worktree", "remove", "C:\\src\\foo")
        routed = adapter._route_command_for_cwd(command, cwd)
        assert routed == (
            "git.exe",
            "-C",
            "C:\\src\\repo",
            "worktree",
            "remove",
            "C:\\src\\foo",
        )

    def test_does_not_double_inject_dash_c(self) -> None:
        cwd = Path("/mnt/c/src/repo")
        adapter = self._adapter()
        adapter._should_route_to_windows = lambda _cwd: True  # type: ignore[assignment,method-assign]
        command = ("git", "-C", "D:\\already\\set", "rev-parse", "--show-toplevel")
        routed = adapter._route_command_for_cwd(command, cwd)
        # When the caller already passed an explicit ``-C``, respect
        # it verbatim and only normalize the binary.
        assert routed == ("git.exe", "-C", "D:\\already\\set", "rev-parse", "--show-toplevel")

    def test_skips_dash_c_for_non_mount_cwd(self) -> None:
        cwd = Path("/home/me/src/repo")
        adapter = self._adapter()
        adapter._should_route_to_windows = lambda _cwd: False  # type: ignore[assignment,method-assign]
        # An explicit absolute git.exe with a non-/mnt cwd: we can't
        # produce a Windows path so ``-C`` is omitted. The binary is
        # still normalized.
        command = ("git.exe", "status", "--short")
        routed = adapter._route_command_for_cwd(command, cwd)
        assert routed == ("git.exe", "status", "--short")

    def test_passes_through_outside_wsl(self) -> None:
        cwd = Path("/mnt/c/worktree")
        adapter = GitAdapter(
            FakeRunner(()),
            is_wsl_runtime=False,
            windows_binary_resolver=lambda binary: binary,
        )
        # Even with a (hypothetical) stamp, non-WSL runtime never routes.
        assert adapter._should_route_to_windows(cwd) is False
        routed = adapter._route_command_for_cwd(
            ("git", "rev-parse", "--show-toplevel"),
            cwd,
        )
        assert routed == ("git", "rev-parse", "--show-toplevel")

    def test_should_route_skips_non_mount_paths(self) -> None:
        adapter = self._adapter()
        # Non-mount path short-circuits regardless of any .git file
        # contents so we never touch the filesystem for /home/... etc.
        assert adapter._should_route_to_windows(Path("/home/me/repo")) is False

    def test_should_route_caches_result_per_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            adapter = self._adapter()
            adapter._windows_routing_cache[cwd] = True
            # Bypass the mount guard: with the cache primed, the
            # predicate would still return False because the mount
            # check runs first. The cache only short-circuits the
            # filesystem read for /mnt cwds — assert the cache is
            # actually consulted by checking the dict survives.
            assert adapter._windows_routing_cache[cwd] is True


class RemoveWorktreeWindowsStampedFlowTests(unittest.TestCase):
    """End-to-end coverage for the user-reported failure mode.

    Reproduces a Windows-stamped linked worktree under WSL: every
    pre-validation call (``rev-parse``, ``worktree list``, ``status``)
    MUST go through ``git.exe`` so the gitdir back-reference resolves
    correctly. Without the fix, ``rev-parse --show-toplevel`` runs
    under WSL git and fails before the routed remove can dispatch.
    """

    def test_full_remove_flow_routes_every_call_to_windows_git(self) -> None:
        # Use synthetic /mnt/<letter>/... paths so _select_worktree_binary_and_path
        # naturally produces a git.exe + backslash target. The routing
        # predicate is monkey-patched to deterministically distinguish
        # the Windows-stamped worktree from the POSIX-stamped main
        # repo without touching the host filesystem.
        worktree_path = Path("/mnt/c/src/CosmosDB.worktrees/feature")
        repo_root_path = Path("/mnt/c/src/CosmosDB")
        worktree_windows = "C:\\src\\CosmosDB.worktrees\\feature"
        repo_root_windows = "C:\\src\\CosmosDB"
        runner = FakeRunner(
            (
                _result(
                    (
                        "git.exe",
                        "-C",
                        worktree_windows,
                        "rev-parse",
                        "--path-format=absolute",
                        "--show-toplevel",
                    ),
                    stdout=f"{repo_root_path}\n",
                ),
                _result(
                    (
                        "git.exe",
                        "-C",
                        worktree_windows,
                        "rev-parse",
                        "--path-format=absolute",
                        "--git-common-dir",
                    ),
                    stdout=f"{repo_root_path}/.git\n",
                ),
                _result(
                    ("git", "worktree", "list", "--porcelain"),
                    stdout="\n".join(
                        (
                            f"worktree {repo_root_path}",
                            "HEAD 1111",
                            "branch refs/heads/main",
                            "",
                            f"worktree {worktree_path}",
                            "HEAD 2222",
                            "branch refs/heads/feature",
                            "",
                        )
                    ),
                ),
                _result(
                    (
                        "git.exe",
                        "-C",
                        worktree_windows,
                        "status",
                        "--short",
                        "--branch",
                        "--untracked-files=all",
                    ),
                    stdout="## feature\n",
                ),
                _result(
                    (
                        "git.exe",
                        "-C",
                        repo_root_windows,
                        "worktree",
                        "remove",
                        worktree_windows,
                    ),
                    stdout="",
                ),
            )
        )
        adapter = GitAdapter(
            runner,
            is_wsl_runtime=True,
            windows_binary_resolver=lambda binary: binary,
        )
        # The Windows-stamped worktree routes; the POSIX-stamped main
        # repo does not. Patch the predicate rather than seeding the
        # cache so the mount short-circuit cannot mask this decision.

        def fake_should_route(cwd: Path) -> bool:
            return cwd == worktree_path

        adapter._should_route_to_windows = fake_should_route  # type: ignore[assignment,method-assign]

        outcome = adapter.remove_worktree(str(worktree_path), force=False)

        assert outcome.path == worktree_path
        invoked_binaries = [call[0][0] for call in runner.calls]
        # rev-parse (x2) + status MUST be git.exe so the gitdir
        # back-reference resolves. worktree list runs at the
        # POSIX-stamped main repo cwd and stays on WSL git.
        # Final destructive remove must also be git.exe.
        assert invoked_binaries == [
            "git.exe",
            "git.exe",
            "git",
            "git.exe",
            "git.exe",
        ]
        # Every git.exe invocation carries an explicit ``-C
        # <windows_cwd>`` so it never depends on WSL interop to
        # translate a POSIX cwd at exec time.
        for call_command, _call_cwd, _call_timeout in runner.calls:
            if call_command[0] == "git.exe":
                assert call_command[1] == "-C", call_command
                assert call_command[2].startswith("C:\\"), call_command


class ResolveWindowsGitBinaryTests(unittest.TestCase):
    """Pin ``git.exe`` discovery so muxdeck works on WSL setups where
    Git for Windows is installed but absent from PATH.

    The user-visible bug was a ``FileNotFoundError: 'git.exe'`` when
    ``which git.exe`` returned nothing even though Git was at
    ``/mnt/c/Program Files/Git/cmd/git.exe``. Discovery must fall
    back to standard install paths.
    """

    def test_absolute_path_is_returned_verbatim(self) -> None:
        # Operator-provided absolute paths win even if shutil.which
        # would otherwise resolve them differently — they may be a
        # deliberately pinned shim.
        absolute = "/some/explicit/path/git.exe"
        resolved = _resolve_windows_git_binary(
            absolute,
            path_searcher=lambda _name: "/should/not/be/used.exe",
            path_is_file=lambda _candidate: False,
        )
        assert resolved == absolute

    def test_prefers_path_search_when_available(self) -> None:
        resolved = _resolve_windows_git_binary(
            "git.exe",
            fallback_paths=("/never/touched.exe",),
            path_searcher=lambda name: f"/usr/bin/{name}" if name == "git.exe" else None,
            path_is_file=lambda _candidate: True,
        )
        assert resolved == "/usr/bin/git.exe"

    def test_falls_back_to_first_existing_install_path(self) -> None:
        existing = "/mnt/c/Program Files/Git/cmd/git.exe"
        resolved = _resolve_windows_git_binary(
            "git.exe",
            fallback_paths=(
                "/mnt/c/missing/git.exe",
                existing,
                "/mnt/c/also/missing/git.exe",
            ),
            path_searcher=lambda _name: None,
            path_is_file=lambda candidate: candidate == existing,
        )
        assert resolved == existing

    def test_returns_none_when_nothing_resolves(self) -> None:
        resolved = _resolve_windows_git_binary(
            "git.exe",
            fallback_paths=("/mnt/c/missing/git.exe",),
            path_searcher=lambda _name: None,
            path_is_file=lambda _candidate: False,
        )
        assert resolved is None


class WindowsGitLazyResolverTests(unittest.TestCase):
    """``GitAdapter._windows_git`` lazily resolves git.exe and surfaces
    a single actionable error when Git for Windows is missing."""

    def test_resolves_lazily_and_caches(self) -> None:
        calls: list[str] = []

        def resolver(binary: str) -> str | None:
            calls.append(binary)
            return "/mnt/c/Program Files/Git/cmd/git.exe"

        adapter = GitAdapter(
            FakeRunner(()),
            is_wsl_runtime=True,
            windows_binary_resolver=resolver,
        )
        # Constructor must not eagerly probe — discovery is deferred
        # until the first Windows-routed command.
        assert calls == []
        first = adapter._windows_git()
        second = adapter._windows_git()
        assert first == "/mnt/c/Program Files/Git/cmd/git.exe"
        assert second == first
        # Caching guarantees we only probe the filesystem once per
        # adapter lifetime.
        assert calls == ["git.exe"]

    def test_raises_actionable_error_when_not_found(self) -> None:
        adapter = GitAdapter(
            FakeRunner(()),
            is_wsl_runtime=True,
            windows_binary_resolver=lambda _binary: None,
        )
        with self.assertRaises(GitCommandError) as ctx:
            adapter._windows_git()
        # The message must name Git for Windows AND a remediation
        # path so the operator can act without spelunking logs.
        message = str(ctx.exception).lower()
        assert "git for windows" in message
        assert "install" in message or "path" in message


class RouteCommandRaisesWhenGitExeMissingTests(unittest.TestCase):
    """End-to-end: when the resolver fails, ``remove_worktree`` raises
    the actionable Git-for-Windows error rather than an opaque
    ``FileNotFoundError`` from ``subprocess``.
    """

    def test_remove_worktree_raises_actionable_error_when_git_exe_missing(self) -> None:
        adapter = GitAdapter(
            FakeRunner(()),
            is_wsl_runtime=True,
            windows_binary_resolver=lambda _binary: None,
        )
        adapter._should_route_to_windows = lambda _cwd: True  # type: ignore[assignment,method-assign]
        with self.assertRaises(GitCommandError) as ctx:
            adapter.remove_worktree("/mnt/c/src/CosmosDB.worktrees/feature", force=True)
        message = str(ctx.exception).lower()
        assert "git for windows" in message
