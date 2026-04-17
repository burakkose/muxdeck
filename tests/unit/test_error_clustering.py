# ruff: noqa: E402,I001,PT009

from __future__ import annotations

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from copilot_commander.domain.error_clustering import (
    ErrorCluster,
    cluster_errors,
    normalize_error_message,
)


class NormalizeErrorMessageTests(unittest.TestCase):
    def test_masks_posix_paths(self) -> None:
        self.assertEqual(
            normalize_error_message("File /home/user/foo.py raised"),
            "File <PATH> raised",
        )

    def test_masks_windows_paths(self) -> None:
        self.assertEqual(
            normalize_error_message(r"open C:\Users\jane\bar.txt"),
            "open <PATH>",
        )

    def test_masks_uuids(self) -> None:
        self.assertEqual(
            normalize_error_message("agent 0fd0c0d0-1234-5678-9abc-def012345678 died"),
            "agent <UUID> died",
        )

    def test_masks_long_hex(self) -> None:
        self.assertEqual(
            normalize_error_message("commit abc1234deadbeef missing"),
            "commit <HEX> missing",
        )

    def test_masks_line_col(self) -> None:
        self.assertEqual(
            normalize_error_message("err at module.py:42:7 boom"),
            "err at module.py:<POS> boom",
        )

    def test_masks_numbers(self) -> None:
        self.assertEqual(
            normalize_error_message("retry 17 of 30 failed in 1.50s"),
            "retry <N> of <N> failed in <N>s",
        )

    def test_masks_tempdirs(self) -> None:
        self.assertEqual(
            normalize_error_message("wrote /tmp/abc/scratch.log ok"),
            "wrote <PATH> ok",
        )

    def test_collapses_whitespace(self) -> None:
        self.assertEqual(
            normalize_error_message("hello    world"),
            "hello world",
        )

    def test_two_messages_with_only_path_diff_normalize_equal(self) -> None:
        a = normalize_error_message("ImportError in /a/b/c.py at line 12")
        b = normalize_error_message("ImportError in /x/y/z.py at line 99")
        self.assertEqual(a, b)


class ClusterErrorsTests(unittest.TestCase):
    def test_empty_input_returns_empty_tuple(self) -> None:
        self.assertEqual(cluster_errors(()), ())

    def test_clusters_by_canonical_form_and_counts(self) -> None:
        msgs = [
            "ImportError in /a/foo.py line 1",
            "ImportError in /b/foo.py line 99",
            "Connection refused on port 5432",
            "ImportError in /c/bar.py line 7",
            "Connection refused on port 5433",
        ]
        clusters = cluster_errors(msgs)
        self.assertEqual(len(clusters), 2)
        self.assertEqual(clusters[0].count, 3)
        self.assertEqual(clusters[1].count, 2)
        # Examples preserve original strings, deduped, capped at 3.
        self.assertEqual(len(clusters[0].examples), 3)

    def test_top_n_truncates(self) -> None:
        # Use distinct canonical groups by varying the *non-numeric*
        # parts so masking does not collapse everything into one bucket.
        msgs = [f"kind_{i // 2} failure" for i in range(10)]
        clusters = cluster_errors(msgs, top_n=2)
        self.assertEqual(len(clusters), 2)

    def test_top_n_zero_returns_empty(self) -> None:
        self.assertEqual(cluster_errors(["x"], top_n=0), ())

    def test_ordering_is_count_desc_then_alpha(self) -> None:
        clusters = cluster_errors(["b err", "a err", "b err 2"])
        # "b err" appears twice (with masked number → "b err <N>"
        # vs "b err"). Use a deterministic example:
        clusters = cluster_errors(["alpha", "beta", "alpha"])
        self.assertEqual(clusters[0], ErrorCluster("alpha", 2, ("alpha",)))
        self.assertEqual(clusters[1].canonical, "beta")

    def test_blank_messages_skipped(self) -> None:
        self.assertEqual(cluster_errors(["", "   "]), ())


if __name__ == "__main__":
    unittest.main()
