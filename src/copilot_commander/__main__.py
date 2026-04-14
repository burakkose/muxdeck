"""Console entrypoint for copilot commander."""

from __future__ import annotations

from copilot_commander.app import run_app


def main() -> int:
    """Launch the Textual operator shell."""
    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
