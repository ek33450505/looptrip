"""tests/test_independent_rederivation.py - BREAK THE CIRCULARITY.

The headline savings ($320.16 / $472.80 / $792.96) are otherwise verified only
against ``run_proof()``, which self-asserts the SAME numbers against the SAME
hardcoded constants through the SAME ``detect()`` code path. System-under-test
and oracle share one source of truth, so a coordinated drift (a detector
off-by-one PLUS a matching EXPECTED_SAVED edit, or a regenerated fixture) stays
green. That defeats the stated goal: "provably accurate data you can stand
behind."

This file supplies the missing INDEPENDENT ORACLE in two stages:

1. ``test_*_brute_force_*`` derive the saved amount straight from the fixture
   JSON -- filter agents containing "workflow", sort by (started_at, id), sum
   ``cost_usd`` over rows[2:] -- WITHOUT importing ``looptrip.detector`` or
   ``proof.run_proof``. This is the oracle: data -> number, no detector.

2. ``test_detector_matches_brute_force_*`` then run the REAL pipeline
   (from_fixture -> sort by (ts, raw_id) -> detect -> top report) and assert the
   detector's ``prevented_cost`` equals the brute-force oracle per session, and
   that the trip event's ``raw_id`` is 555 / 1080. This closes the loop:
   data->brute-force and data->detector independently yield the same dollars.

The detector / adapter imports are intentionally LOCAL to the stage-2 tests so
the stage-1 oracle is provably derived without them.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from importlib.resources import files

SESSION_A = "2e6c0288-b8db-46de-8ec4-164e3685a739"
SESSION_B = "da27b414-f9f1-4c91-bd50-1a6096555066"

# Hand-verified per-session and total prevented waste (USD), full precision.
EXPECTED_SAVED = {SESSION_A: Decimal("320.16"), SESSION_B: Decimal("472.80")}
EXPECTED_TOTAL = Decimal("792.96")
EXPECTED_TRIP_RAW_ID = {SESSION_A: 555, SESSION_B: 1080}

CENT = Decimal("0.01")


# ---------------------------------------------------------------------------
# Stage 1: the INDEPENDENT ORACLE. Pure data -> dollars. No detector, no proof.
# ---------------------------------------------------------------------------

def _load_fixture() -> dict:
    """Load the packaged fixture JSON via importlib.resources (no detector)."""
    raw = files("looptrip").joinpath("_data/cast_db_runaways.json").read_text("utf-8")
    return json.loads(raw)


def _brute_force_saved(session_id: str) -> Decimal:
    """Re-derive prevented waste straight from the fixture, bypassing looptrip.

    Mirrors the proof's stated model with no detector involvement: keep the rows
    whose agent contains "workflow", order them by (started_at, id), and sum
    ``cost_usd`` over everything from the 3rd dispatch onward (rows[2:]) -- the
    baseline (#1) and the trip (#2) are excluded; every later dispatch is waste.
    Uses ``Decimal`` so the comparison is exact, not float-fuzzy.
    """
    sessions = _load_fixture()["sessions"]
    rows = [r for r in sessions[session_id] if "workflow" in (r.get("agent") or "")]
    rows.sort(key=lambda r: (r["started_at"], r["id"]))
    return sum(
        (Decimal(str(r["cost_usd"])) for r in rows[2:] if r.get("cost_usd") is not None),
        Decimal("0"),
    )


def test_brute_force_session_a_saved_is_320_16():
    """Pure-data oracle: session A waste quantizes to $320.16 (no detector)."""
    saved = _brute_force_saved(SESSION_A)
    assert saved.quantize(CENT) == EXPECTED_SAVED[SESSION_A]
    assert abs(saved - EXPECTED_SAVED[SESSION_A]) < CENT


def test_brute_force_session_b_saved_is_472_80():
    """Pure-data oracle: session B waste quantizes to $472.80 (no detector)."""
    saved = _brute_force_saved(SESSION_B)
    assert saved.quantize(CENT) == EXPECTED_SAVED[SESSION_B]
    assert abs(saved - EXPECTED_SAVED[SESSION_B]) < CENT


def test_brute_force_grand_total_is_792_96():
    """The two oracle sums add to $792.96 -- the headline, derived from data."""
    total = _brute_force_saved(SESSION_A) + _brute_force_saved(SESSION_B)
    assert total.quantize(CENT) == EXPECTED_TOTAL
    assert abs(total - EXPECTED_TOTAL) < CENT


# ---------------------------------------------------------------------------
# Stage 2: CROSS-CHECK. The detector must reproduce the oracle to the penny.
# detector / adapter imports are deliberately local to keep stage 1 clean.
# ---------------------------------------------------------------------------

def _detector_top_report(session_id: str):
    """Run the real pipeline and return the costliest report for a session."""
    from looptrip.adapters.cast_db import CastDbAdapter
    from looptrip.detector import detect

    adapter = CastDbAdapter.from_fixture(session_id)
    events = sorted(adapter.events(), key=lambda e: (e.ts, e.raw_id))
    reports = detect(events)
    assert reports, f"detector found no pathology for {session_id!r}"
    return max(reports, key=lambda r: r.prevented_cost)


def test_detector_matches_brute_force_session_a():
    """detect()'s prevented_cost for session A equals the oracle, to the penny,
    and the loop trips at the 2nd workflow-subagent dispatch (raw_id 555)."""
    oracle = _brute_force_saved(SESSION_A)
    report = _detector_top_report(SESSION_A)
    assert abs(Decimal(str(report.prevented_cost)) - oracle) < CENT
    # Float accumulation can differ by ~1e-10 across CPython versions (3.10 vs
    # 3.14); use a tolerance comparison instead of exact equality.
    assert math.isclose(report.prevented_cost, float(oracle), rel_tol=1e-9, abs_tol=1e-6)
    assert report.trip_event.raw_id == EXPECTED_TRIP_RAW_ID[SESSION_A]


def test_detector_matches_brute_force_session_b():
    """detect()'s prevented_cost for session B equals the oracle, to the penny,
    and the loop trips at the 2nd workflow-subagent dispatch (raw_id 1080)."""
    oracle = _brute_force_saved(SESSION_B)
    report = _detector_top_report(SESSION_B)
    assert abs(Decimal(str(report.prevented_cost)) - oracle) < CENT
    # Float accumulation can differ by ~1e-10 across CPython versions (3.10 vs
    # 3.14); use a tolerance comparison instead of exact equality.
    assert math.isclose(report.prevented_cost, float(oracle), rel_tol=1e-9, abs_tol=1e-6)
    assert report.trip_event.raw_id == EXPECTED_TRIP_RAW_ID[SESSION_B]


def test_detector_grand_total_matches_brute_force_total():
    """Both detector outputs sum to the same $792.96 the data oracle yields."""
    oracle_total = _brute_force_saved(SESSION_A) + _brute_force_saved(SESSION_B)
    detector_total = (
        _detector_top_report(SESSION_A).prevented_cost
        + _detector_top_report(SESSION_B).prevented_cost
    )
    assert abs(Decimal(str(detector_total)) - oracle_total) < CENT
    assert Decimal(str(detector_total)).quantize(CENT) == EXPECTED_TOTAL
