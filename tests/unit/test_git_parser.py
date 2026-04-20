from __future__ import annotations

import unittest

from muxdeck.parsers.git_parser import (
    parse_ahead_behind,
    parse_git_status_porcelain,
    parse_git_worktree_list_porcelain,
)


class GitParserTests(unittest.TestCase):
    def test_parse_git_worktree_list_porcelain_handles_multiple_blocks(self) -> None:
        output = "\n".join(
            (
                "worktree /repo",
                "HEAD 0123456789abcdef",
                "branch refs/heads/main",
                "",
                "worktree /repo/worktrees/task-one",
                "HEAD fedcba9876543210",
                "branch refs/heads/task-one",
                "locked in-use",
                "",
                "worktree /repo/worktrees/detached",
                "HEAD abcdefabcdefabcd",
                "detached",
                "prunable gitdir file points to non-existent location",
                "",
            )
        )

        records = parse_git_worktree_list_porcelain(output)

        assert len(records) == 3
        assert records[0].path == "/repo"
        assert records[0].branch == "main"
        assert not records[0].is_locked
        assert records[1].is_locked
        assert records[1].lock_reason == "in-use"
        assert records[2].is_detached
        assert records[2].is_prunable
        assert "non-existent location" in (records[2].prunable_reason or "")

    def test_parse_ahead_behind_supports_multiple_git_output_shapes(self) -> None:
        tab_counts = parse_ahead_behind("3\t1")
        branch_header = parse_ahead_behind("## feat...origin/feat [ahead 2, behind 4]")
        unmatched = parse_ahead_behind("up to date")

        assert (tab_counts.ahead, tab_counts.behind, tab_counts.recognized) == (3, 1, True)
        assert (branch_header.ahead, branch_header.behind) == (2, 4)
        assert not unmatched.recognized

    def test_parse_git_status_porcelain_tracks_dirty_entries_and_ignored_lines(self) -> None:
        output = "\n".join(
            (
                "## feat...origin/feat [ahead 1]",
                " M src/muxdeck/parsers/git_parser.py",
                "R  old_name.py -> new_name.py",
                "?? tests/unit/test_git_parser.py",
                "!! .venv/",
                "UU conflicted.txt",
                "bad-line",
            )
        )

        summary = parse_git_status_porcelain(output)

        assert summary.is_dirty
        assert summary.branch_line == "## feat...origin/feat [ahead 1]"
        assert len(summary.entries) == 4
        renamed = summary.entries[1]
        assert renamed.original_path == "old_name.py"
        assert renamed.path == "new_name.py"
        assert summary.entries[2].is_untracked
        assert summary.entries[3].is_unmerged
        assert summary.ignored_lines == ("!! .venv/", "bad-line")

    def test_parse_git_status_porcelain_empty_output_is_clean(self) -> None:
        summary = parse_git_status_porcelain("")

        assert not summary.is_dirty
        assert summary.entries == ()

    def test_parse_git_parser_decodes_quoted_fields_and_rename_paths(self) -> None:
        worktrees = parse_git_worktree_list_porcelain(
            "\n".join(
                (
                    "worktree /repo/worktrees/cafe",
                    'locked "reason\\nfor\\040lock"',
                    'prunable "caf\\303\\251 reason"',
                )
            )
        )
        summary = parse_git_status_porcelain('R  "old -> name.py" -> "caf\\303\\251 -> new.py"')

        assert worktrees[0].lock_reason == "reason\nfor lock"
        assert worktrees[0].prunable_reason == "café reason"
        assert summary.entries[0].original_path == "old -> name.py"
        assert summary.entries[0].path == "café -> new.py"


if __name__ == "__main__":
    unittest.main()
