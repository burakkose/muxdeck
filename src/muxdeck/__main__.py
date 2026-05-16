"""Console entrypoint for muxdeck."""

from __future__ import annotations

from muxdeck.cli import main


def _entry() -> int:
    return main()


if __name__ == "__main__":
    raise SystemExit(_entry())


__all__ = ["main"]
