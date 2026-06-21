"""Tests for the cast.db adapter (src/looptrip/adapters/cast_db.py).

The ``from_fixture`` tests run against the REAL packaged hermetic fixture
(``_data/cast_db_runaways.json``) so they double as a proof that the adapter
reproduces the verified ground truth for the runaway session. Live mode is
exercised with an INJECTED fake ``db_query`` — no real database is ever
touched.
"""

from __future__ import annotations

from looptrip.adapters.cast_db import CastDbAdapter
from looptrip.normalize import Adapter, Event

# The real runaway session captured in the packaged fixture.
RUNAWAY_SESSION = "2e6c0288-b8db-46de-8ec4-164e3685a739"


# ---------------------------------------------------------------------------
# from_fixture — real packaged fixture, ordering & normalization
# ---------------------------------------------------------------------------

def test_from_fixture_is_an_adapter():
    """CastDbAdapter honors the Adapter contract."""
    adapter = CastDbAdapter.from_fixture(RUNAWAY_SESSION)
    assert isinstance(adapter, Adapter)


def test_from_fixture_orders_by_started_at_then_id():
    """Events come out sorted non-decreasing on (ts, raw_id)."""
    events = list(CastDbAdapter.from_fixture(RUNAWAY_SESSION).events())
    assert events, "expected a non-empty stream for the runaway session"
    keys = [(e.ts, e.raw_id) for e in events]
    assert keys == sorted(keys)


def test_from_fixture_normalizes_every_event_to_dispatch():
    """Every cast.db event is tool='dispatch' with no args_hash or handoff."""
    events = list(CastDbAdapter.from_fixture(RUNAWAY_SESSION).events())
    assert all(isinstance(e, Event) for e in events)
    assert all(e.tool == "dispatch" for e in events)
    assert all(e.args_hash is None for e in events)
    assert all(e.handoff_state is None for e in events)
    assert all(e.progress is False for e in events)


def test_from_fixture_passes_cost_through_faithfully():
    """cost_usd flows through verbatim: populated for completed runs, None for
    the two failed rows that have no cost in the source (the prevented-waste
    accounting depends on this fidelity)."""
    events = list(CastDbAdapter.from_fixture(RUNAWAY_SESSION).events())
    # Every workflow-subagent dispatch (the duplicate-work loop) carries a cost.
    workflow = [e for e in events if e.agent == "workflow-subagent"]
    assert workflow and all(e.cost_usd is not None for e in workflow)
    # Failed source rows with no cost surface as None rather than 0 or a crash.
    none_cost_ids = {e.raw_id for e in events if e.cost_usd is None}
    assert none_cost_ids == {628, 637}


def test_from_fixture_first_workflow_subagent_dispatch_is_554():
    """Ground truth: the workflow-subagent loop is 54 dispatches; #1=id554, #2=id555.

    Note: the very first event of the *full* stream is an earlier "Explore"
    run (id 527). 554 is the first event of the duplicate-work signature
    ("workflow-subagent"), which is what the detector trips on at #2.
    """
    events = list(CastDbAdapter.from_fixture(RUNAWAY_SESSION).events())
    workflow = [e for e in events if e.agent == "workflow-subagent"]
    assert len(workflow) == 54
    assert workflow[0].raw_id == 554
    assert workflow[1].raw_id == 555
    # raw_id provenance + cost flow through verbatim from the source row.
    assert workflow[0].cost_usd == 10.981367
    assert workflow[0].input_tokens is not None


def test_from_fixture_unknown_session_yields_empty_stream():
    """An unknown session id is a clean empty stream, not an error."""
    adapter = CastDbAdapter.from_fixture("does-not-exist-00000000")
    assert list(adapter.events()) == []


# ---------------------------------------------------------------------------
# Live mode — injected fake db_query, asserting no real DB access
# ---------------------------------------------------------------------------

def _canned_rows():
    """Two rows deliberately out of (started_at, id) order to prove sorting."""
    return [
        {
            "id": 2,
            "session_id": "sess-x",
            "agent": "workflow-subagent",
            "model": "claude-sonnet",
            "started_at": "2026-06-21T00:00:02Z",
            "ended_at": "2026-06-21T00:00:03Z",
            "input_tokens": 1200,
            "output_tokens": 50,
            "cost_usd": 2.5,
            "status": "DONE",
        },
        {
            "id": 1,
            "session_id": "sess-x",
            "agent": "workflow-subagent",
            "model": "claude-sonnet",
            "started_at": "2026-06-21T00:00:01Z",
            "ended_at": "2026-06-21T00:00:02Z",
            "input_tokens": 1190,
            "output_tokens": 40,
            "cost_usd": 1.5,
            "status": "DONE",
        },
    ]


def test_live_mode_uses_injected_db_query_and_binds_session_param():
    """Live mode calls the injected db_query with the session id bound as '?'."""
    calls = []

    def fake_db_query(sql, params):
        calls.append((sql, params))
        return _canned_rows()

    adapter = CastDbAdapter("sess-x", db_query=fake_db_query)
    events = list(adapter.events())

    # The fake was the only data source — no real DB was reachable here.
    assert len(calls) == 1
    sql, params = calls[0]
    # Session id is a bound parameter, never interpolated into the SQL text.
    assert params == ("sess-x",)
    assert "sess-x" not in sql
    assert "WHERE session_id = ?" in sql

    # Rows are sorted by (started_at, id) and normalized like fixture rows.
    assert [e.raw_id for e in events] == [1, 2]
    assert [e.cost_usd for e in events] == [1.5, 2.5]
    assert all(e.tool == "dispatch" and e.args_hash is None for e in events)
    assert all(e.agent == "workflow-subagent" for e in events)


def test_live_mode_caches_rows_across_multiple_iterations():
    """Iterating events() twice triggers at most one db_query call."""
    calls = []

    def fake_db_query(sql, params):
        calls.append((sql, params))
        return _canned_rows()

    adapter = CastDbAdapter("sess-x", db_query=fake_db_query)
    first = list(adapter.events())
    second = list(adapter.events())

    assert len(calls) == 1
    assert [e.raw_id for e in first] == [e.raw_id for e in second]


def test_injected_rows_take_precedence_and_skip_db_entirely():
    """When rows are supplied, db_query is never consulted."""

    def exploding_db_query(sql, params):  # pragma: no cover - must not run
        raise AssertionError("db_query must not be called when rows are injected")

    rows = _canned_rows()
    adapter = CastDbAdapter("sess-x", rows=rows, db_query=exploding_db_query)
    events = list(adapter.events())
    assert [e.raw_id for e in events] == [1, 2]
