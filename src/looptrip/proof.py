"""proof.py — the hermetic Phase-1 proof that looptrip trips at iteration 2.

Replays two REAL CAST runaway sessions (exported into the packaged fixture
``looptrip/_data/cast_db_runaways.json`` — no cast.db required) through the
duplicate-work detector and shows, per session, the dollar waste a trip at the
*second* workflow-subagent dispatch would have prevented.

The model, stated plainly: dispatches #1-2 are the legal baseline; the
duplicate-work detector trips at dispatch #2 (the 2nd occurrence of the
signature, within 5% input-token variance of the preceding dispatch, no
progress delta); every dispatch from #3 onward is the prevented waste.

Verified ground truth this proof reproduces exactly:

* session 2e6c0288 - workflow-subagent loop of 54 dispatches, trip at id 555,
  **$320.16** saved (dispatches #3..#54).
* session da27b414 - workflow-subagent loop of 49 dispatches, trip at id 1080,
  **$472.80** saved (dispatches #3..#49).
* GRAND TOTAL - **$792.96** saved if tripped at iteration 2.

Runnable (``python -m looptrip proof`` or ``looptrip proof``) or importable
(``run_proof()`` returns a result dict and self-asserts the savings).
Stdlib-only; works with no cast.db.
"""

from __future__ import annotations

from typing import Any, Dict, List

from looptrip.adapters.cast_db import CastDbAdapter
from looptrip.detector import PathologyReport, detect

# The two real runaway sessions in the packaged fixture, with the verified
# per-session prevented waste each must reproduce. Full-precision cost flows
# through the detector; we round only for display and the within-$0.01 check.
SESSIONS: tuple = (
    "2e6c0288-b8db-46de-8ec4-164e3685a739",
    "da27b414-f9f1-4c91-bd50-1a6096555066",
)
EXPECTED_SAVED: Dict[str, float] = {
    "2e6c0288-b8db-46de-8ec4-164e3685a739": 320.16,
    "da27b414-f9f1-4c91-bd50-1a6096555066": 472.80,
}
EXPECTED_TOTAL: float = 792.96
TOLERANCE: float = 0.01

TRANSPARENCY_NOTE = (
    "Model: dispatches #1-2 are the legal baseline; the duplicate-work detector "
    "trips at dispatch #2 (the 2nd occurrence of the signature, within 5% "
    "input-token variance of the preceding dispatch, no progress delta); every "
    "dispatch from #3 onward is the prevented waste."
)


def _top_report(session_id: str) -> PathologyReport:
    """Replay one session's fixture rows and return its costliest pathology.

    Builds the adapter from the packaged fixture (never touching cast.db),
    materializes and sorts the events by ``(ts, raw_id)``, runs the detector,
    and returns the report with the greatest prevented cost — the
    workflow-subagent loop. Raises :class:`AssertionError` if no pathology is
    detected, so a silently broken fixture fails loudly.
    """
    adapter = CastDbAdapter.from_fixture(session_id)
    events = sorted(adapter.events(), key=lambda event: (event.ts, event.raw_id))
    reports = detect(events)
    if not reports:
        raise AssertionError(f"no pathology detected for session {session_id!r}")
    return max(reports, key=lambda report: report.prevented_cost)


def run_proof() -> Dict[str, Any]:
    """Replay both runaway sessions and return a structured proof result.

    Returns a dict with one entry per session plus the grand total and the
    transparency note. Self-checks each session's prevented cost against the
    verified ground truth (within $0.01) and the grand total against $792.96,
    raising :class:`AssertionError` on any mismatch so the proof fails loudly if
    the fixture or detector drifts.
    """
    per_session: List[Dict[str, Any]] = []
    grand_total = 0.0
    for session_id in SESSIONS:
        report = _top_report(session_id)
        grand_total += report.prevented_cost
        per_session.append(
            {
                "session_id": session_id,
                "session_short": session_id[:8],
                "loop_agent": report.agent,
                "total_dispatches": report.occurrences,
                "first_dispatch_raw_id": report.first_event.raw_id,
                "trip_dispatch_raw_id": report.trip_event.raw_id,
                "prevented_runs": report.prevented_runs,
                "prevented_cost": report.prevented_cost,
            }
        )

    # Self-check: every session and the grand total must match the ground truth.
    for entry in per_session:
        expected = EXPECTED_SAVED[entry["session_id"]]
        actual = entry["prevented_cost"]
        if abs(actual - expected) > TOLERANCE:
            raise AssertionError(
                f"session {entry['session_short']} prevented_cost "
                f"${actual:.2f} != expected ${expected:.2f}"
            )
    if abs(grand_total - EXPECTED_TOTAL) > TOLERANCE:
        raise AssertionError(
            f"grand total ${grand_total:.2f} != expected ${EXPECTED_TOTAL:.2f}"
        )

    return {
        "sessions": per_session,
        "grand_total_saved": grand_total,
        "transparency_note": TRANSPARENCY_NOTE,
    }


def format_proof(result: Dict[str, Any]) -> str:
    """Render the proof result as a human-readable table, note, and headline."""
    cols = ("session", "loop_agent", "dispatches", "trip_id", "prevented", "saved")
    header = (
        f"{cols[0]:<10}  {cols[1]:<18}  {cols[2]:>10}  "
        f"{cols[3]:>8}  {cols[4]:>9}  {cols[5]:>10}"
    )
    rule = "-" * len(header)
    lines = [
        "looptrip Phase-1 proof - trip at iteration 2 (hermetic fixture replay)",
        rule,
        header,
        rule,
    ]
    for entry in result["sessions"]:
        saved = f"${entry['prevented_cost']:,.2f}"
        lines.append(
            f"{entry['session_short']:<10}  {entry['loop_agent']:<18}  "
            f"{entry['total_dispatches']:>10}  {entry['trip_dispatch_raw_id']:>8}  "
            f"{entry['prevented_runs']:>9}  {saved:>10}"
        )
    lines.append(rule)
    lines.append("")
    lines.append(result["transparency_note"])
    lines.append("")
    total = f"${result['grand_total_saved']:,.2f}"
    lines.append(f"GRAND TOTAL: {total} saved if tripped at iteration 2.")
    return "\n".join(lines)


def main() -> int:
    """Run the proof, print the table and headline, return a 0/1 exit status."""
    result = run_proof()
    print(format_proof(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
