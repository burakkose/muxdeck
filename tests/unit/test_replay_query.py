# ruff: noqa: E402,I001,PT009,PT027

from __future__ import annotations

from datetime import time
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from muxdeck.domain.replay_query import (
    EMPTY_QUERY,
    ReplayQuery,
    build_chip_filter_text,
    parse_replay_query,
    query_matches,
)


class ReplayQueryParserTests(unittest.TestCase):
    def test_empty_input_returns_singleton_empty(self) -> None:
        self.assertIs(parse_replay_query(""), EMPTY_QUERY)
        self.assertIs(parse_replay_query("   "), EMPTY_QUERY)

    def test_bare_text_falls_into_text_field_casefolded(self) -> None:
        query = parse_replay_query("Merge Conflict")
        self.assertEqual(query.text, "merge conflict")
        self.assertEqual(query.kinds, frozenset())

    def test_quoted_phrase_preserved_as_single_text_term(self) -> None:
        query = parse_replay_query('text:"ImportError in foo"')
        self.assertEqual(query.text, "importerror in foo")

    def test_each_facet_token_recognized(self) -> None:
        query = parse_replay_query(
            "kind:event severity:error agent:planner marker:activity since:14:30 until:15:00:30"
        )
        self.assertEqual(query.kinds, frozenset({"event"}))
        self.assertEqual(query.severities, frozenset({"error"}))
        self.assertEqual(query.agents, frozenset({"planner"}))
        self.assertEqual(query.marker_kinds, frozenset({"activity"}))
        self.assertEqual(query.since, time(14, 30))
        self.assertEqual(query.until, time(15, 0, 30))
        self.assertIsNone(query.text)

    def test_repeated_facet_keys_or_within_facet(self) -> None:
        query = parse_replay_query("kind:event kind:log severity:error severity:warning")
        self.assertEqual(query.kinds, frozenset({"event", "log"}))
        self.assertEqual(query.severities, frozenset({"error", "warning"}))

    def test_malformed_tokens_fall_through_to_text(self) -> None:
        query = parse_replay_query("foo:bar:baz unknown:value :leading trailing:")
        # ``foo:bar:baz`` has unknown head → text. ``unknown:value`` →
        # text. ``:leading`` and ``trailing:`` are malformed → text.
        self.assertIsNotNone(query.text)
        assert query.text is not None
        for fragment in ("foo:bar:baz", "unknown:value", ":leading", "trailing:"):
            self.assertIn(fragment.casefold(), query.text)

    def test_malformed_time_falls_through_to_text(self) -> None:
        query = parse_replay_query("since:notatime hello")
        self.assertIsNone(query.since)
        assert query.text is not None
        self.assertIn("since:notatime", query.text)
        self.assertIn("hello", query.text)

    def test_unbalanced_quotes_fall_back_to_raw_text(self) -> None:
        query = parse_replay_query('text:"oops')
        # shlex raises ValueError → entire raw string becomes text.
        self.assertEqual(query.text, 'text:"oops')

    def test_aliases_map_to_canonical_facets(self) -> None:
        query = parse_replay_query("kinds:event severities:warning markers:boundary q:hello")
        self.assertEqual(query.kinds, frozenset({"event"}))
        self.assertEqual(query.severities, frozenset({"warning"}))
        self.assertEqual(query.marker_kinds, frozenset({"boundary"}))
        self.assertEqual(query.text, "hello")

    def test_since_until_narrow_with_repeats(self) -> None:
        query = parse_replay_query("since:09:00 since:10:00 until:18:00 until:17:00")
        self.assertEqual(query.since, time(10, 0))
        self.assertEqual(query.until, time(17, 0))


class _Entry:
    """Minimal duck-typed implementation of ``ReplayMatchableEntry``."""

    def __init__(
        self,
        *,
        timestamp: str = "2025-01-01T12:00:00+00:00",
        kind: str = "log",
        label: str = "",
        severity: str | None = None,
        marker_kind: str | None = None,
        agent_id: str | None = None,
        lines: tuple[str, ...] = (),
    ) -> None:
        self.timestamp = timestamp
        self.kind = kind
        self.label = label
        self.severity = severity
        self.marker_kind = marker_kind
        self.agent_id = agent_id
        self.lines = lines


class QueryMatchesTests(unittest.TestCase):
    def test_empty_query_matches_anything(self) -> None:
        self.assertTrue(query_matches(EMPTY_QUERY, _Entry()))

    def test_text_substring_matches_blob_case_insensitive(self) -> None:
        entry = _Entry(label="Fatal: Merge Conflict", kind="log")
        self.assertTrue(query_matches(parse_replay_query("merge"), entry))
        self.assertFalse(query_matches(parse_replay_query("nope"), entry))

    def test_kind_facet_filters(self) -> None:
        entry = _Entry(kind="event")
        self.assertTrue(query_matches(parse_replay_query("kind:event"), entry))
        self.assertFalse(query_matches(parse_replay_query("kind:log"), entry))
        # OR within facet
        self.assertTrue(query_matches(parse_replay_query("kind:event kind:log"), entry))

    def test_severity_and_agent_and_marker_facets(self) -> None:
        entry = _Entry(severity="error", agent_id="planner", marker_kind="activity")
        self.assertTrue(query_matches(parse_replay_query("severity:error"), entry))
        self.assertFalse(query_matches(parse_replay_query("severity:warning"), entry))
        self.assertTrue(query_matches(parse_replay_query("agent:planner"), entry))
        self.assertFalse(query_matches(parse_replay_query("agent:other"), entry))
        self.assertTrue(query_matches(parse_replay_query("marker:activity"), entry))
        self.assertFalse(query_matches(parse_replay_query("marker:error"), entry))

    def test_and_across_facets(self) -> None:
        entry = _Entry(kind="event", severity="error")
        self.assertTrue(query_matches(parse_replay_query("kind:event severity:error"), entry))
        self.assertFalse(query_matches(parse_replay_query("kind:event severity:warning"), entry))

    def test_time_bounds_inclusive(self) -> None:
        entry = _Entry(timestamp="2025-01-01T14:30:00+00:00")
        self.assertTrue(query_matches(parse_replay_query("since:14:30"), entry))
        self.assertFalse(query_matches(parse_replay_query("since:14:31"), entry))
        self.assertTrue(query_matches(parse_replay_query("until:14:30"), entry))
        self.assertFalse(query_matches(parse_replay_query("until:14:29"), entry))

    def test_unparseable_timestamp_fails_time_filter(self) -> None:
        entry = _Entry(timestamp="not-a-timestamp")
        self.assertFalse(query_matches(parse_replay_query("since:00:00"), entry))

    def test_text_matches_lines_blob(self) -> None:
        entry = _Entry(lines=("Some Output Here",))
        self.assertTrue(query_matches(parse_replay_query("output"), entry))


class ChipBuilderTests(unittest.TestCase):
    def test_each_chip_returns_canonical_text(self) -> None:
        self.assertEqual(build_chip_filter_text("errors_only"), "severity:error")
        self.assertEqual(build_chip_filter_text("activity"), "marker:activity")
        self.assertEqual(build_chip_filter_text("tool_calls"), "marker:tool_call")
        self.assertEqual(build_chip_filter_text("clear"), "")

    def test_unknown_chip_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_chip_filter_text("nope")

    def test_chip_text_round_trips_through_parser(self) -> None:
        query = parse_replay_query(build_chip_filter_text("errors_only"))
        self.assertEqual(query, ReplayQuery(severities=frozenset({"error"})))


if __name__ == "__main__":
    unittest.main()
