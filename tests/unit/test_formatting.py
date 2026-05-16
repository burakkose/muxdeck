from __future__ import annotations

import pytest

from muxdeck.formatting import format_duration_seconds


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (1, "1s"),
        (59, "59s"),
        (60, "1m00s"),
        (61, "1m01s"),
        (2617, "43m37s"),
        (3599, "59m59s"),
        (3600, "1h00m"),
        (3661, "1h01m"),
        (14724, "4h05m"),
        (86399, "23h59m"),
        (86400, "1d00h"),
        (90061, "1d01h"),
        (172800, "2d00h"),
    ],
)
def test_format_duration_seconds_known_values(seconds: int, expected: str) -> None:
    assert format_duration_seconds(seconds) == expected


def test_format_duration_seconds_clamps_negative() -> None:
    assert format_duration_seconds(-1) == "0s"
    assert format_duration_seconds(-10_000) == "0s"


def test_format_duration_seconds_truncates_fraction() -> None:
    # Truncation, not rounding, so a value of 59.9s does not pretend
    # to be a minute it hasn't reached yet.
    assert format_duration_seconds(59.9) == "59s"
    assert format_duration_seconds(60.99) == "1m00s"
    assert format_duration_seconds(3599.5) == "59m59s"
