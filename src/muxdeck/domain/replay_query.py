"""Faceted-search query model for the Replay screen.

This module is part of the **domain** layer. It is pure: no I/O, no
Textual, no controller imports. The parser is tolerant — malformed
tokens are folded into the freeform ``text`` field rather than raising,
so users typing partial syntax always get a sensible substring filter.

Supported token grammar
-----------------------

``kind:event``           → restrict to one entry kind (``event`` or ``log``)
``severity:error``       → restrict to a severity label
``agent:<id>``           → restrict to a specific agent id
``marker:activity``      → restrict to a marker kind
``since:14:30``          → wall-clock lower bound (inclusive), HH:MM[:SS]
``until:15:00:30``       → wall-clock upper bound (inclusive)
``text:"foo bar"``       → freeform text term (quoted phrases are
                           preserved verbatim and joined into ``text``)
bare token / phrase      → joined into ``text``
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Protocol

__all__ = [
    "EMPTY_QUERY",
    "ReplayMatchableEntry",
    "ReplayQuery",
    "build_chip_filter_text",
    "parse_replay_query",
    "query_matches",
]


@dataclass(frozen=True, slots=True)
class ReplayQuery:
    """Parsed faceted-search query.

    All sets are ``frozenset`` for deterministic hashing. ``text`` is
    case-folded substring match against the entry search blob; the
    facet sets behave as **AND across facets, OR within a facet**.
    """

    text: str | None = None
    kinds: frozenset[str] = field(default_factory=frozenset)
    severities: frozenset[str] = field(default_factory=frozenset)
    agents: frozenset[str] = field(default_factory=frozenset)
    marker_kinds: frozenset[str] = field(default_factory=frozenset)
    since: time | None = None
    until: time | None = None

    @property
    def is_empty(self) -> bool:
        return (
            not self.text
            and not self.kinds
            and not self.severities
            and not self.agents
            and not self.marker_kinds
            and self.since is None
            and self.until is None
        )


EMPTY_QUERY: ReplayQuery = ReplayQuery()


class ReplayMatchableEntry(Protocol):
    """Minimal Protocol of the fields ``query_matches`` reads.

    Defined here (instead of importing ``ReplayTranscriptEntryView``)
    so the domain layer stays free of controller imports. Members are
    declared as ``@property`` so frozen dataclass instances satisfy
    the Protocol (mypy requires read-only attributes for
    frozen-dataclass values).
    """

    @property
    def timestamp(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def label(self) -> str: ...
    @property
    def severity(self) -> str | None: ...
    @property
    def marker_kind(self) -> str | None: ...
    @property
    def agent_id(self) -> str | None: ...
    @property
    def lines(self) -> tuple[str, ...]: ...


_FIELD_ALIASES: dict[str, str] = {
    "kind": "kinds",
    "kinds": "kinds",
    "severity": "severities",
    "severities": "severities",
    "agent": "agents",
    "agents": "agents",
    "marker": "marker_kinds",
    "markers": "marker_kinds",
    "marker_kind": "marker_kinds",
    "since": "since",
    "from": "since",
    "until": "until",
    "to": "until",
    "text": "text",
    "q": "text",
}


def parse_replay_query(raw: str) -> ReplayQuery:
    """Parse ``raw`` into a :class:`ReplayQuery`.

    Tolerant of malformed input: any token that does not parse cleanly
    falls through to the freeform ``text`` field, preserving the
    legacy "type a substring and it just works" behaviour.
    """

    stripped = raw.strip()
    if not stripped:
        return EMPTY_QUERY

    try:
        tokens = shlex.split(stripped, posix=True)
    except ValueError:
        # Unbalanced quotes — fall back to the raw string so the user
        # still sees results while finishing their query.
        return ReplayQuery(text=stripped.casefold())

    text_parts: list[str] = []
    kinds: set[str] = set()
    severities: set[str] = set()
    agents: set[str] = set()
    marker_kinds: set[str] = set()
    since: time | None = None
    until: time | None = None

    for token in tokens:
        # A bare "kind:" or ":foo" is malformed → treat it as text.
        if ":" not in token or token.startswith(":") or token.endswith(":"):
            text_parts.append(token)
            continue
        head, _, tail = token.partition(":")
        canonical = _FIELD_ALIASES.get(head.casefold())
        if canonical is None or not tail:
            text_parts.append(token)
            continue
        if canonical == "text":
            text_parts.append(tail)
        elif canonical == "kinds":
            kinds.add(tail.casefold())
        elif canonical == "severities":
            severities.add(tail.casefold())
        elif canonical == "agents":
            agents.add(tail)
        elif canonical == "marker_kinds":
            marker_kinds.add(tail.casefold())
        elif canonical == "since":
            parsed = _parse_time(tail)
            if parsed is None:
                text_parts.append(token)
            else:
                # Last writer wins; further ``since:`` tokens narrow.
                since = parsed if since is None else max(since, parsed)
        elif canonical == "until":
            parsed = _parse_time(tail)
            if parsed is None:
                text_parts.append(token)
            else:
                until = parsed if until is None else min(until, parsed)

    text = " ".join(text_parts).strip().casefold() if text_parts else None
    return ReplayQuery(
        text=text or None,
        kinds=frozenset(kinds),
        severities=frozenset(severities),
        agents=frozenset(agents),
        marker_kinds=frozenset(marker_kinds),
        since=since,
        until=until,
    )


def _parse_time(raw: str) -> time | None:
    """Parse ``HH:MM`` or ``HH:MM:SS`` (24h). Returns ``None`` on
    malformed input rather than raising."""

    try:
        return time.fromisoformat(raw)
    except ValueError:
        return None


def query_matches(query: ReplayQuery, entry: ReplayMatchableEntry) -> bool:
    """Return ``True`` iff ``entry`` satisfies every facet of ``query``.

    Semantics:
      * AND across facet keys, OR within a facet's value set.
      * ``text`` is a case-insensitive substring match against the
        entry's search blob (timestamp, kind, label, severity,
        marker_kind, lines).
      * ``since`` / ``until`` compare the entry's wall-clock time
        component (HH:MM:SS) to the bound, inclusive on both ends.
    """

    if query.is_empty:
        return True
    if query.kinds and entry.kind.casefold() not in query.kinds:
        return False
    if query.severities:
        severity = (entry.severity or "").casefold()
        if severity not in query.severities:
            return False
    if query.agents:
        agent = entry.agent_id or ""
        if agent not in query.agents:
            return False
    if query.marker_kinds:
        marker = (entry.marker_kind or "").casefold()
        if marker not in query.marker_kinds:
            return False
    if query.since is not None or query.until is not None:
        entry_time = _entry_time(entry.timestamp)
        if entry_time is None:
            # Unparseable timestamps fail the time filter rather than
            # silently passing.
            return False
        if query.since is not None and entry_time < query.since:
            return False
        if query.until is not None and entry_time > query.until:
            return False
    if query.text and query.text not in _search_blob(entry):  # noqa: SIM103
        return False
    return True


def _entry_time(timestamp: str) -> time | None:
    try:
        return datetime.fromisoformat(timestamp).time()
    except ValueError:
        return None


def _search_blob(entry: ReplayMatchableEntry) -> str:
    parts = (
        entry.timestamp,
        entry.kind,
        entry.label,
        entry.severity or "",
        entry.marker_kind or "",
        entry.agent_id or "",
        *entry.lines,
    )
    return "\n".join(parts).casefold()


# ── Quick-filter chips ──────────────────────────────────────────────


# `severity:error` alone covers both event entries (event.severity ==
# "error") and parsed log entries (the controller mirrors marker_kind
# "error" into severity). Using two facets here would AND them and
# silently drop event-only errors.
_CHIP_ERRORS_ONLY = "severity:error"
_CHIP_ACTIVITY = "marker:activity"
_CHIP_TOOL_CALLS = "marker:tool_call"


def build_chip_filter_text(chip: str) -> str:
    """Return the canonical filter-text snippet for a chip name.

    Pure helper exposed to controllers/screens so the canonical
    chip strings live in one place.
    """

    match chip:
        case "errors_only":
            return _CHIP_ERRORS_ONLY
        case "activity":
            return _CHIP_ACTIVITY
        case "tool_calls":
            return _CHIP_TOOL_CALLS
        case "clear":
            return ""
    msg = f"unknown replay chip: {chip!r}"
    raise ValueError(msg)
