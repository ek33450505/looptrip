"""cli.py - the ``looptrip`` command-line entry point.

Stdlib ``argparse`` only. One top-level flag plus three subcommands:

* ``looptrip --version``        - print ``looptrip <__version__>`` and exit 0.
* ``looptrip proof``            - run the hermetic Phase-1 proof, print its
                                  headline, exit 0.
* ``looptrip scan <source>``    - replay a source through the detector and print
                                  its pathology reports, costliest first.
                                  Source forms:
                                    - ``fixture:<session_id>`` - packaged
                                      hermetic fixture (no cast.db).
                                    - ``cast-db:<session_id>`` - the live
                                      cast.db (requires the real database).
                                    - ``otel:<path>`` - OTel GenAI span JSON
                                      or JSONL file.  For multi-scenario flat
                                      fixtures append ``#<scenario>`` to select
                                      a scenario (e.g. ``otel:spans.json#deadlock``).
                                  Detector flags (mutually exclusive):
                                    - ``--all`` - run all four detectors.
                                    - ``--detectors LIST`` - comma-separated
                                      kind names (e.g. ``ping_pong,deadlock``).
                                  Default (no flags): duplicate-work only, same
                                  output as Phase 1.
* ``looptrip attribute <source>`` - run the selected detectors then attribute
                                  each confirmed pathology via counterfactual
                                  replay, printing a verdict table followed by
                                  per-report detail lines.  Same source forms
                                  and detector-selection flags as ``scan``.

``main(argv=None)`` returns an ``int`` status; the ``looptrip`` console script
and ``python -m looptrip`` both route through it. Stdlib-only, no global state.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import List, Optional

from looptrip import __version__
from looptrip.adapters.cast_db import CastDbAdapter
from looptrip.adapters.otel import OTelSpanAdapter
from looptrip.attribution import attribute_all
from looptrip.detector import detect
from looptrip.detectors.types import ALL_DETECTORS
from looptrip.normalize import Event
from looptrip.proof import format_proof, run_proof


def _source_events(source: str) -> List[Event]:
    """Resolve a source string to a sorted event list.

    ``fixture:<id>`` builds an adapter from the packaged hermetic fixture;
    ``cast-db:<id>`` queries the live database; ``otel:<path>`` (or
    ``otel:<path>#<scenario>``) loads OTel GenAI spans from a JSON or JSONL
    file. Events are sorted by ``(ts, raw_id)``. Raises :class:`ValueError`
    on a malformed or unknown source so callers can surface a clean exit-2
    error.
    """
    scheme, sep, rest = source.partition(":")
    if not sep or not rest:
        raise ValueError(
            f"malformed source {source!r}; expected 'fixture:<id>', "
            f"'cast-db:<id>', or 'otel:<path>[#scenario]'"
        )
    if scheme == "fixture":
        adapter: CastDbAdapter = CastDbAdapter.from_fixture(rest)
    elif scheme == "cast-db":
        adapter = CastDbAdapter(rest)
    elif scheme == "otel":
        path, _sep, scenario = rest.partition("#")
        if path.endswith(".jsonl"):
            if scenario:
                raise ValueError("scenario selection is not supported for JSONL sources")
            otel_adapter = OTelSpanAdapter.from_jsonl_file(path)
        else:
            otel_adapter = OTelSpanAdapter.from_json_file(path, scenario or None)
        return sorted(otel_adapter.events(), key=lambda event: (event.ts or "", event.raw_id or ""))
    else:
        raise ValueError(
            f"unknown source scheme {scheme!r}; expected 'fixture', 'cast-db', or 'otel'"
        )
    return sorted(adapter.events(), key=lambda event: (event.ts, event.raw_id))


def _resolve_kinds(all_flag: bool, detectors_csv: Optional[str]) -> Optional[tuple[str, ...]]:
    """Return the detector tuple to pass to ``detect()``, or ``None`` for default.

    ``None`` — default path: duplicate-work only, unchanged Phase-1 behaviour.
    :data:`ALL_DETECTORS` — when ``all_flag`` is ``True``.
    A validated tuple of kind names — when ``detectors_csv`` is provided.

    Raises :class:`ValueError` with a clear message naming the offending kind
    on any unknown detector name, so the caller can emit a clean exit-2 error.
    """
    if not all_flag and detectors_csv is None:
        return None
    if all_flag:
        return ALL_DETECTORS
    names = [n for n in (tok.strip() for tok in detectors_csv.split(",")) if n]
    if not names:
        raise ValueError("no detector names given in --detectors")
    for name in names:
        if name not in ALL_DETECTORS:
            raise ValueError(
                f"unknown detector {name!r}; expected one of: {sorted(ALL_DETECTORS)}"
            )
    return tuple(names)


def _cmd_proof() -> int:
    """Run the bundled proof and print its rendered headline. Returns 0."""
    print(format_proof(run_proof()))
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    """Scan a source and print its pathology reports, costliest first.

    With no detector flags: duplicate-work-only (Phase-1 default path; table
    has no ``kind`` column, and the empty message is
    ``no duplicate-work pathologies detected in <source>``).

    With ``--all`` or ``--detectors``: all selected detectors run; table gains
    a leading ``kind`` column; empty message is
    ``no pathologies detected in <source>``.

    Returns 0 on a clean scan (even with no pathologies) and 2 on a malformed
    or unknown source or an unknown detector name, printing the error to stderr.
    """
    try:
        kinds = _resolve_kinds(args.all, args.detectors)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        events = _source_events(args.source)
    except (ValueError, ModuleNotFoundError, ImportError, sqlite3.Error,
            FileNotFoundError, OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    reports = detect(events) if kinds is None else detect(events, detectors=kinds)

    if not reports:
        if kinds is None:
            print(f"no duplicate-work pathologies detected in {args.source}")
        else:
            print(f"no pathologies detected in {args.source}")
        return 0

    if kinds is None:
        # Default table: agent-first, no kind column — exactly as Phase 1.
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
    else:
        # Extended table: kind column first, then agent — disambiguates multiple
        # pathology kinds in the same output; cost remains the last token.
        header = (
            f"{'kind':<20}  {'agent':<24}  {'occurrences':>11}  "
            f"{'prevented_runs':>14}  {'prevented_cost':>14}"
        )
        print(header)
        print("-" * len(header))
        for report in reports:
            saved = f"${report.prevented_cost:,.2f}"
            print(
                f"{report.kind:<20}  {report.agent:<24}  {report.occurrences:>11}  "
                f"{report.prevented_runs:>14}  {saved:>14}"
            )
    return 0


def _cmd_attribute(args: argparse.Namespace) -> int:
    """Attribute confirmed pathologies to decisive events via counterfactual replay.

    Resolves the source, runs the selected detectors (same flags as ``scan``),
    then attributes every confirmed report via
    :func:`~looptrip.attribution.attribute_all` and prints a verdict table
    (columns: kind, agent, verdict, decisive, tested)
    followed by each report's per-attribution ``detail`` line.

    Returns 0 when attribution completes (including the empty-reports case) and
    2 on a bad source or unknown detector name, printing the error to stderr.
    """
    try:
        kinds = _resolve_kinds(args.all, args.detectors)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        events = _source_events(args.source)
    except (ValueError, ModuleNotFoundError, ImportError, sqlite3.Error,
            FileNotFoundError, OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    reports = detect(events) if kinds is None else detect(events, detectors=kinds)

    if not reports:
        print(f"no pathologies detected to attribute in {args.source}")
        return 0

    results = attribute_all(events, reports)

    header = (
        f"{'kind':<20}  {'agent':<24}  {'verdict':>14}  "
        f"{'decisive':>8}  {'tested':>6}"
    )
    print(header)
    print("-" * len(header))
    for res in results:
        print(
            f"{res.report.kind:<20}  {res.report.agent:<24}  {res.verdict:>14}  "
            f"{len(res.decisive):>8}  {res.tested:>6}"
        )
    print()
    for res in results:
        print(res.detail)
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
        "scan",
        help="scan a source for coordination pathologies (default: duplicate-work only)",
    )
    scan.add_argument(
        "source",
        help=(
            "event source: 'fixture:<session_id>', 'cast-db:<session_id>', "
            "or 'otel:<path>[#scenario]' (OTel GenAI span JSON/JSONL file)"
        ),
    )
    scan_group = scan.add_mutually_exclusive_group()
    scan_group.add_argument(
        "--all",
        action="store_true",
        help=(
            "run all four detectors: duplicate_work, ping_pong, deadlock, "
            "non_termination (adds a 'kind' column to the output table)"
        ),
    )
    scan_group.add_argument(
        "--detectors",
        metavar="LIST",
        help=(
            "comma-separated detector kinds to run "
            "(e.g. 'ping_pong,duplicate_work'); adds a 'kind' column"
        ),
    )

    attr = subparsers.add_parser(
        "attribute",
        help=(
            "attribute confirmed pathologies to decisive events via "
            "counterfactual replay"
        ),
    )
    attr.add_argument(
        "source",
        help=(
            "event source: 'fixture:<session_id>', 'cast-db:<session_id>', "
            "or 'otel:<path>[#scenario]' (OTel GenAI span JSON/JSONL file)"
        ),
    )
    attr_group = attr.add_mutually_exclusive_group()
    attr_group.add_argument(
        "--all",
        action="store_true",
        help="run all four detectors before attributing",
    )
    attr_group.add_argument(
        "--detectors",
        metavar="LIST",
        help="comma-separated detector kinds to run before attributing",
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
        return _cmd_scan(args)
    if args.command == "attribute":
        return _cmd_attribute(args)

    parser.print_help()
    return 0
