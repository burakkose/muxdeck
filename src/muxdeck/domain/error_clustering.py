"""Error message normalization and clustering.

Pure domain helpers used by the Replay insights service to surface the
top recurring failures, even when they vary in path, line number, or
hash. No I/O.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "ErrorCluster",
    "cluster_errors",
    "normalize_error_message",
]


@dataclass(frozen=True, slots=True)
class ErrorCluster:
    canonical: str
    count: int
    examples: tuple[str, ...]


# ── Regex patterns used by ``normalize_error_message`` ──────────────
# Each pattern masks one common source of message variance so that
# semantically identical errors collapse into the same canonical key.

# Absolute POSIX paths (``/usr/lib/foo.py``) and Windows paths
# (``C:\foo\bar``). Run before the line:col masker so stripped path
# tails don't leave dangling ``:N``.
_PATH_POSIX = re.compile(r"(?<![A-Za-z0-9_])/(?:[\w.\-+]+/)*[\w.\-+]+")
_PATH_WIN = re.compile(r"[A-Za-z]:\\[\w.\-+\\]+")
# UUIDs (case-insensitive).
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# Long hex / sha-like blobs (>= 7 chars, plain hex).
_HEX = re.compile(r"\b[0-9a-fA-F]{7,}\b")
# Generic line:col positions like ``:42:7`` or trailing ``:42``.
_LINE_COL = re.compile(r":\d+(?::\d+)?\b")
# Bare integers and floats. Use a left-only non-word lookbehind so
# attached units (``1.50s``, ``200ms``, ``5MB``) still mask cleanly.
# A trailing ``\b`` would refuse to cross into the unit suffix and
# leave fractional tails behind.
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])\d+(?:\.\d+)?")
# Common temp-dir prefixes that survive the POSIX path masker because
# they contain ``=`` or other separators (e.g. ``tmp=/tmp/abc``).
_TEMPDIR = re.compile(r"\b(?:/tmp|/var/folders|/private/tmp)/\S+")


def normalize_error_message(msg: str) -> str:
    """Mask variable bits of an error message.

    Order matters: paths and UUIDs are masked first so the generic
    number/hex masks don't chew up substrings that a more specific
    pattern would have replaced wholesale.
    """

    out = msg.strip()
    out = _TEMPDIR.sub("<PATH>", out)
    out = _PATH_WIN.sub("<PATH>", out)
    out = _PATH_POSIX.sub("<PATH>", out)
    out = _UUID.sub("<UUID>", out)
    out = _HEX.sub("<HEX>", out)
    out = _LINE_COL.sub(":<POS>", out)
    out = _NUMBER.sub("<N>", out)
    # Collapse runs of whitespace introduced by repeated masking.
    return re.sub(r"\s+", " ", out).strip()


def cluster_errors(
    messages: Iterable[str],
    *,
    top_n: int = 3,
) -> tuple[ErrorCluster, ...]:
    """Cluster messages by their normalized canonical form.

    Returns the ``top_n`` clusters by descending count. Ties break on
    the canonical string (stable, alphabetical) so output is
    deterministic for tests.
    """

    if top_n <= 0:
        return ()
    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}
    for raw in messages:
        canonical = normalize_error_message(raw)
        if not canonical:
            continue
        counts[canonical] += 1
        bucket = examples.setdefault(canonical, [])
        if raw not in bucket and len(bucket) < 3:
            bucket.append(raw)
    if not counts:
        return ()
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(
        ErrorCluster(canonical=canonical, count=count, examples=tuple(examples[canonical]))
        for canonical, count in ordered[:top_n]
    )
