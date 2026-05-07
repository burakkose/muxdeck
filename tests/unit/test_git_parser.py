# ruff: noqa: PT009, PT027, N802

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


class GitParserBranchTests(unittest.TestCase):
    """Cover edge branches in the git parser internals."""

    def test_parse_ahead_behind_empty_string_returns_default(self) -> None:
        result = parse_ahead_behind("")
        assert not result.recognized
        assert result.ahead == 0
        assert result.behind == 0

    def test_parse_ahead_behind_only_one_side(self) -> None:
        ahead = parse_ahead_behind("ahead 5")
        behind = parse_ahead_behind("behind 7")
        assert ahead.ahead == 5
        assert ahead.behind == 0
        assert ahead.recognized
        assert behind.ahead == 0
        assert behind.behind == 7
        assert behind.recognized

    def test_parse_git_status_porcelain_skips_short_and_misformatted_lines(self) -> None:
        summary = parse_git_status_porcelain("xx\nM   ok.py\nM_no_space.py")
        # short line "xx" → ignored. "M_no_space.py" has no space at idx 2 → ignored.
        # blank lines also skipped.
        assert "xx" in summary.ignored_lines
        assert "M_no_space.py" in summary.ignored_lines

    def test_parse_git_status_porcelain_marks_unmerged_AA_and_DD(self) -> None:
        summary = parse_git_status_porcelain("AA both_added.py\nDD both_deleted.py")
        assert all(entry.is_unmerged for entry in summary.entries)

    def test_parse_git_worktree_list_porcelain_supports_bare_marker(self) -> None:
        records = parse_git_worktree_list_porcelain("worktree /repo\nbare\n")
        assert len(records) == 1
        assert records[0].is_bare

    def test_decode_git_path_handles_dangling_backslash(self) -> None:
        from muxdeck.parsers.git_parser import _decode_git_path

        # A trailing backslash inside the quoted form preserves the literal '\'
        decoded = _decode_git_path('"end\\"')
        assert decoded.endswith("\\")

    def test_decode_git_path_handles_unknown_escape(self) -> None:
        from muxdeck.parsers.git_parser import _decode_git_path

        # Unknown escapes (not in the table, not octal) get the literal char.
        decoded = _decode_git_path('"foo\\zbar"')
        assert decoded == "foozbar"

    def test_decode_git_path_unquoted_returns_stripped(self) -> None:
        from muxdeck.parsers.git_parser import _decode_git_path

        assert _decode_git_path("plain.py") == "plain.py"

    def test_split_git_rename_payload_returns_none_when_no_separator(self) -> None:
        from muxdeck.parsers.git_parser import _split_git_rename_payload

        assert _split_git_rename_payload("only-one-name.py") is None

    def test_split_git_rename_payload_handles_escapes_in_quotes(self) -> None:
        from muxdeck.parsers.git_parser import _split_git_rename_payload

        # The escape consumes the next character so the embedded ` -> `
        # inside the escaped-quoted string is NOT a separator.
        result = _split_git_rename_payload('"a\\"b" -> "c"')
        assert result is not None
        before, after = result
        assert before == '"a\\"b"'
        assert after == '"c"'


if __name__ == "__main__":
    unittest.main()
