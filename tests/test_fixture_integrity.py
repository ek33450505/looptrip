"""tests/test_fixture_integrity.py - PROVENANCE LOCK on the packaged fixture.

The entire $792.96 Phase-1 proof rests on a single artifact:
``looptrip/_data/cast_db_runaways.json``. The behavioral proof (test_proof.py)
and the independent re-derivation (test_independent_rederivation.py) both assume
that artifact is byte-identical to the one whose ground truth was hand-verified.
Nothing else in the suite pins the bytes, so anyone could regenerate, trim, or
re-baseline the fixture and the expected numbers would silently travel with the
data, leaving every other test green.

This file is that missing lock. It asserts the fixture's exact identity (sha256
+ byte length) and the structural / cost invariants the proof depends on, so any
drift in the data fails loudly HERE -- before it can poison a downstream number.

All cost sums are computed independently from the raw JSON using ``Decimal`` at
full precision; rounding happens only for the within-$0.01 comparisons.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from importlib.resources import files

# --- Hand-verified ground truth (independently re-derived from the committed
# --- fixture; see the task brief's VERIFIED GROUND TRUTH block). -------------

EXPECTED_SHA256 = "fc966c3f9f00fa15d3ec86de2707c3ad5f7ce52f692aa34ffd5110fa3e70763f"
EXPECTED_BYTES = 64396

SESSION_A = "2e6c0288-b8db-46de-8ec4-164e3685a739"
SESSION_B = "da27b414-f9f1-4c91-bd50-1a6096555066"

EXPECTED_ROW_COUNTS = {SESSION_A: 113, SESSION_B: 56}
EXPECTED_WORKFLOW_DISPATCHES = {SESSION_A: 54, SESSION_B: 49}
EXPECTED_SESSION_TOTAL = {SESSION_A: 355.96, SESSION_B: 502.05}
EXPECTED_LOOP_COST = {SESSION_A: 342.11, SESSION_B: 495.99}
EXPECTED_NULL_COST_IDS = {SESSION_A: {628, 637}, SESSION_B: {1137}}
EXPECTED_FIRST_TWO_DISPATCH_IDS = {SESSION_A: (554, 555), SESSION_B: (1079, 1080)}

CENT = Decimal("0.01")
WORKFLOW_AGENT = "workflow-subagent"


# ---------------------------------------------------------------------------
# Raw-byte / JSON loaders -- the single source of fixture truth for this file.
# ---------------------------------------------------------------------------

def _fixture_bytes() -> bytes:
    """Return the packaged fixture's raw bytes via importlib.resources."""
    return files("looptrip").joinpath("_data/cast_db_runaways.json").read_bytes()


def _fixture_data() -> dict:
    """Parse the packaged fixture JSON."""
    return json.loads(_fixture_bytes().decode("utf-8"))


def _rows(session_id: str) -> list:
    return _fixture_data()["sessions"][session_id]


def _workflow_rows_ordered(session_id: str) -> list:
    """workflow-subagent rows for a session, ordered by (started_at, id)."""
    rows = [r for r in _rows(session_id) if r.get("agent") == WORKFLOW_AGENT]
    return sorted(rows, key=lambda r: (r["started_at"], r["id"]))


def _decimal_sum(rows) -> Decimal:
    """Full-precision Decimal sum of cost_usd over rows, skipping None."""
    return sum(
        (Decimal(str(r["cost_usd"])) for r in rows if r.get("cost_usd") is not None),
        Decimal("0"),
    )


# ---------------------------------------------------------------------------
# Identity lock -- exact bytes. Any content drift fails here, loudly.
# ---------------------------------------------------------------------------

def test_fixture_sha256_is_pinned():
    """The fixture's SHA-256 matches the hand-verified digest, byte for byte."""
    digest = hashlib.sha256(_fixture_bytes()).hexdigest()
    assert digest == EXPECTED_SHA256


def test_fixture_byte_length_is_pinned():
    """The fixture is exactly 64396 bytes -- any trim/regenerate fails here."""
    assert len(_fixture_bytes()) == EXPECTED_BYTES


# ---------------------------------------------------------------------------
# Structural lock -- top-level shape and session membership.
# ---------------------------------------------------------------------------

def test_fixture_top_level_keys_are_meta_and_sessions():
    """The fixture's top-level keys are exactly {'_meta', 'sessions'}."""
    data = _fixture_data()
    assert set(data.keys()) == {"_meta", "sessions"}


def test_fixture_contains_exactly_the_two_expected_sessions():
    """Both expected session ids are present and they are the only ones."""
    sessions = _fixture_data()["sessions"]
    assert SESSION_A in sessions
    assert SESSION_B in sessions
    assert set(sessions.keys()) == {SESSION_A, SESSION_B}


# ---------------------------------------------------------------------------
# Row-count + dispatch-count locks, per session.
# ---------------------------------------------------------------------------

def test_session_a_row_count_is_113():
    assert len(_rows(SESSION_A)) == EXPECTED_ROW_COUNTS[SESSION_A]


def test_session_b_row_count_is_56():
    assert len(_rows(SESSION_B)) == EXPECTED_ROW_COUNTS[SESSION_B]


def test_session_a_has_54_workflow_subagent_dispatches():
    rows = [r for r in _rows(SESSION_A) if r.get("agent") == WORKFLOW_AGENT]
    assert len(rows) == EXPECTED_WORKFLOW_DISPATCHES[SESSION_A]


def test_session_b_has_49_workflow_subagent_dispatches():
    rows = [r for r in _rows(SESSION_B) if r.get("agent") == WORKFLOW_AGENT]
    assert len(rows) == EXPECTED_WORKFLOW_DISPATCHES[SESSION_B]


# ---------------------------------------------------------------------------
# Cost-fidelity locks -- independently summed session totals and loop costs.
# ---------------------------------------------------------------------------

def test_session_a_total_cost_within_one_cent_of_355_96():
    total = _decimal_sum(_rows(SESSION_A))
    assert abs(total - Decimal(str(EXPECTED_SESSION_TOTAL[SESSION_A]))) < CENT


def test_session_b_total_cost_within_one_cent_of_502_05():
    total = _decimal_sum(_rows(SESSION_B))
    assert abs(total - Decimal(str(EXPECTED_SESSION_TOTAL[SESSION_B]))) < CENT


def test_session_a_loop_cost_within_one_cent_of_342_11():
    loop = _decimal_sum(_workflow_rows_ordered(SESSION_A))
    assert abs(loop - Decimal(str(EXPECTED_LOOP_COST[SESSION_A]))) < CENT


def test_session_b_loop_cost_within_one_cent_of_495_99():
    loop = _decimal_sum(_workflow_rows_ordered(SESSION_B))
    assert abs(loop - Decimal(str(EXPECTED_LOOP_COST[SESSION_B]))) < CENT


# ---------------------------------------------------------------------------
# Null-cost provenance -- the failed source rows that surface as None and drive
# the prevented-waste accounting's reliance on faithful cost passthrough.
# ---------------------------------------------------------------------------

def test_session_a_null_cost_ids_are_628_and_637():
    ids = {r["id"] for r in _rows(SESSION_A) if r.get("cost_usd") is None}
    assert ids == EXPECTED_NULL_COST_IDS[SESSION_A]


def test_session_b_null_cost_id_is_1137():
    ids = {r["id"] for r in _rows(SESSION_B) if r.get("cost_usd") is None}
    assert ids == EXPECTED_NULL_COST_IDS[SESSION_B]


# ---------------------------------------------------------------------------
# Trip-anchor lock -- the first two workflow-subagent dispatches (ordered by
# (started_at, id)) are the baseline (#1) and the trip event (#2).
# ---------------------------------------------------------------------------

def test_session_a_first_two_dispatch_ids_are_554_then_555():
    wf = _workflow_rows_ordered(SESSION_A)
    assert (wf[0]["id"], wf[1]["id"]) == EXPECTED_FIRST_TWO_DISPATCH_IDS[SESSION_A]


def test_session_b_first_two_dispatch_ids_are_1079_then_1080():
    wf = _workflow_rows_ordered(SESSION_B)
    assert (wf[0]["id"], wf[1]["id"]) == EXPECTED_FIRST_TWO_DISPATCH_IDS[SESSION_B]
