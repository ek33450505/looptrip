"""cli.py - the ``looptrip`` command-line entry point.

Stdlib ``argparse`` only. One top-level flag plus two subcommands:

* ``looptrip --version``      - print ``looptrip <__version__>`` and exit 0.
* ``looptrip proof``          - run the hermetic Phase-1 proof, print its
                                headline, exit 0.
* ``looptrip scan <source>``  - replay a source through the detector and print
                                its duplicate-work reports, costliest first.
                                Source forms:
                                  - ``fixture:<session_id>`` - packaged
                                    hermetic fixture (no cast.db).
                                  - ``cast-db:<session_id>`` - the live cast.db
                                    (requires the real database).

``main(argv=None)`` returns an ``int`` status; the ``looptrip`` console script
and ``python -m looptrip`` both route through it. Stdlib-only, no global state.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from typing import List, Optional

from looptrip import __version__
from looptrip.adapters.cast_db import CastDbAdapter
from looptrip.detector import PathologyReport, detect
from looptrip.proof import format_proof, run_proof


def _scan_source(source: str) -> List[PathologyReport]:
    """Resolve a ``scan`` source string to detector reports, costliest first.

    ``fixture:<id>`` builds an adapter from the packaged hermetic fixture;
    ``cast-db:<id>`` queries the live database. The events are sorted by
    ``(ts, raw_id)`` before detection. Raises :class:`ValueError` for a
    malformed or unknown source so the caller can surface a clean error.
    """
    scheme, sep, session_id = source.partition(":")
    if not sep or not session_id:
        raise ValueError(
            f"malformed source {source!r}; expected 'fixture:<id>' or 'cast-db:<id>'"
        )
    if scheme == "fixture":
        adapter: CastDbAdapter = CastDbAdapter.from_fixture(session_id)
    elif scheme == "cast-db":
        adapter = CastDbAdapter(session_id)
    else:
        raise ValueError(
            f"unknown source scheme {scheme!r}; expected 'fixture' or 'cast-db'"
        )
    events = sorted(adapter.events(), key=lambda event: (event.ts, event.raw_id))
    return detect(events)


def _cmd_proof() -> int:
    """Run the bundled proof and print its rendered headline. Returns 0."""
    print(format_proof(run_proof()))
    return 0


def _cmd_scan(source: str) -> int:
    """Scan a source and print its duplicate-work reports, costliest first.

    Returns 0 on a clean scan (even with no pathologies) and 2 on a malformed
    or unknown source, printing the error to stderr.
    """
    try:
        reports = _scan_source(source)
    except (ValueError, ModuleNotFoundError, ImportError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not reports:
        print(f"no duplicate-work pathologies detected in {source}")
        return 0

    header = (
        f"{'agent':<24}  {'occurrences':>11}  "
        f"{'prevented_runs':>14}  {'prevented_cost':>14}"
    )
    print(header)
    print("-" * len(header))
    for report in reports:
        saved = f"${report.prevented_cost:,.2f}"
        print(
            f"{report.agent:<24}  {report.occurrences:>11}  "
            f"{report.prevented_runs:>14}  {saved:>14}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for the ``looptrip`` CLI."""
    parser = argparse.ArgumentParser(
        prog="looptrip",
        description="Deterministic detector of multi-agent coordination pathologies.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the looptrip version and exit",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("proof", help="run the hermetic Phase-1 proof")
    scan = subparsers.add_parser(
        "scan", help="scan a source for duplicate-work pathologies"
    )
    scan.add_argument(
        "source",
        help="event source: 'fixture:<session_id>' or 'cast-db:<session_id>'",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Parse ``argv`` and dispatch to a subcommand. Returns an int status.

    ``--version`` short-circuits before any subcommand. With no subcommand the
    help text is printed and 0 is returned.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"looptrip {__version__}")
        return 0
    if args.command == "proof":
        return _cmd_proof()
    if args.command == "scan":
        return _cmd_scan(args.source)

    parser.print_help()
    return 0
