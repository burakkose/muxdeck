"""Console entrypoint for muxdeck."""

from __future__ import annotations

from muxdeck.app import run_app


def main() -> int:
    """Launch the Textual operator shell."""
    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
