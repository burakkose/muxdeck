"""Console-facing CLI for ``muxdeck``.

Exposes the default Textual app entrypoint plus a ``--perf`` synthetic
runner that exercises the sync pipeline N times and prints a perf
summary table. The runner deliberately bypasses Textual so an operator
can collect ``perf`` numbers from a real environment without standing
up a terminal — useful for WSL / slow filesystems where the UI itself
is the thing being optimized.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from muxdeck.app import build_runtime, run_app
from muxdeck.config import load_config
from muxdeck.perf import SpanSummary, summarize


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point used by the ``muxdeck`` console script."""
    args = _parse_args(argv)
    if args.perf:
        return run_perf(
            cycles=args.perf_cycles,
            config_path=args.config,
            output=sys.stdout,
        )
    return run_app(args.config)


def run_perf(
    *,
    cycles: int,
    config_path: str | Path | None = None,
    output: TextIO | None = None,
) -> int:
    """Run ``cycles`` synthetic sync passes and print a perf summary.

    Mirrors what the live app does on every periodic sync: refreshes
    the runtime synchronizer, builds the dashboard agent items, then
    builds the full dashboard state. All ``timed()`` spans recorded
    along the way are reset before the run and dumped to ``output``
    when it finishes so the table reflects only the synthetic loop.
    """
    if cycles <= 0:
        msg = "perf cycles must be positive"
        raise ValueError(msg)

    stream = output if output is not None else sys.stdout

    config = load_config(config_path)
    runtime = build_runtime(config)
    try:
        synchronizer = runtime.synchronizer
        sync_dashboard = runtime.sync_dashboard
        if synchronizer is None or sync_dashboard is None:
            stream.write("perf: runtime synchronizer or dashboard unavailable; aborting\n")
            return 1

        # Drop anything captured during build_runtime so the summary
        # only reflects the synthetic loop below.
        summarize(reset=True)

        logging.disable(logging.CRITICAL)
        try:
            for _ in range(cycles):
                synchronizer.refresh()
                agent_items = sync_dashboard.build_agent_items()
                sync_dashboard.build_state(precomputed_items=agent_items)
        finally:
            logging.disable(logging.NOTSET)

        stats = summarize(reset=True)
        _write_perf_table(stream, stats, cycles=cycles)
    finally:
        runtime.store.close()
        if runtime.sync_store is not None:
            runtime.sync_store.close()
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="muxdeck",
        description="Launch the muxdeck operator shell, or measure the sync pipeline.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Optional path to a muxdeck config file (overrides discovery).",
    )
    parser.add_argument(
        "--perf",
        action="store_true",
        help="Run synthetic sync cycles and print a perf summary instead of launching the UI.",
    )
    parser.add_argument(
        "--perf-cycles",
        type=int,
        default=10,
        help="Number of sync cycles to execute when --perf is set (default: 10).",
    )
    return parser.parse_args(argv)


def _write_perf_table(stream: TextIO, stats: Sequence[SpanSummary], *, cycles: int) -> None:
    header = f"─── PERF SUMMARY ({cycles} cycle{'s' if cycles != 1 else ''}) ───"
    stream.write(header + "\n")
    if not stats:
        stream.write("  (no spans recorded — runtime synchronizer may be a no-op)\n")
        return
    stream.write(f"  {'span':<40} {'n':>4} {'total':>10} {'avg':>9} {'p95':>9} {'max':>9}\n")
    for span in stats:
        stream.write(
            f"  {span.name:<40} {span.count:>4} "
            f"{span.total_ms:>8.1f}ms {span.avg_ms:>7.1f}ms "
            f"{span.p95_ms:>7.1f}ms {span.max_ms:>7.1f}ms\n"
        )


__all__ = ["main", "run_perf"]
