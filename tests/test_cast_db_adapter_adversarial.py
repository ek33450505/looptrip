"""Adversarial edge/error tests for the cast.db adapter.

Companion to ``test_cast_db_adapter.py`` (the happy-path / ground-truth file).
This file is deliberately hostile: it proves the adapter never touches a real
database when it must not, that it binds the session id as a parameter rather
than splicing it into SQL, that ordering is enforced regardless of input order,
that ``None`` cost flows through, and it PINS the current (un-handled) behavior
of a ``started_at=None`` row so any future change to it is a deliberate, tested
decision rather than a silent drift.

stdlib + pytest only. No real cast.db is ever read (sandbox-denied + absent in
CI); every "DB" here is an injected fake callable.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import sqlite3
import tempfile

import pytest

from looptrip.adapters.cast_db import CastDbAdapter, _AGENT_RUNS_SQL

RUNAWAY_SESSION = "2e6c0288-b8db-46de-8ec4-164e3685a739"


def _row(id, started_at, *, agent="workflow-subagent", input_tokens=1000, cost_usd=1.0):
    """Build one agent_runs-shaped dict row with the columns the adapter reads."""
    return {
        "id": id,
        "session_id": "sess-x",
        "agent": agent,
        "model": "claude-sonnet",
        "started_at": started_at,
        "ended_at": started_at,
        "input_tokens": input_tokens,
        "output_tokens": 10,
        "cost_usd": cost_usd,
        "status": "DONE",
    }


# ---------------------------------------------------------------------------
# from_fixture — unknown session / alternative path / no 'sessions' key
# ---------------------------------------------------------------------------

def test_unknown_session_id_is_empty_stream_not_error():
    """An unknown session id yields an empty list and raises nothing.

    Adversarial framing: it must be a CLEAN empty stream (``== []``), not a
    KeyError, and distinguishable from the real session which is non-empty.
    """
    unknown = CastDbAdapter.from_fixture("does-not-exist-deadbeef")
    assert list(unknown.events()) == []
    # Sanity contrast: the real session is decidedly non-empty.
    assert list(CastDbAdapter.from_fixture(RUNAWAY_SESSION).events()) != []


def test_from_fixture_explicit_path_loads_and_selects_only_that_session():
    """from_fixture(path=...) reads an alternative JSON and selects one session.

    Rows from other sessions in the same file must NOT bleed into the stream.
    """
    payload = {
        "_meta": {"note": "synthetic"},
        "sessions": {
            "sel": [_row(2, "2026-06-21T00:00:02Z"), _row(1, "2026-06-21T00:00:01Z")],
            "other": [_row(9, "2026-06-21T00:00:09Z", agent="commit")],
        },
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(payload, handle)
        path = handle.name
    try:
        events = list(CastDbAdapter.from_fixture("sel", path=path).events())
    finally:
        os.unlink(path)

    # Only the selected session's rows, ordered by (started_at, id).
    assert [e.raw_id for e in events] == [1, 2]
    assert all(e.agent == "workflow-subagent" for e in events)
    # The 'other' session's commit row never appears.
    assert "commit" not in {e.agent for e in events}


def test_from_fixture_path_without_sessions_key_is_empty_not_crash():
    """A fixture JSON lacking a top-level 'sessions' key yields an empty stream."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump({"_meta": {"note": "no sessions key here"}}, handle)
        path = handle.name
    try:
        events = list(CastDbAdapter.from_fixture("anything", path=path).events())
    finally:
        os.unlink(path)
    assert events == []


# ---------------------------------------------------------------------------
# Injected rows must take ABSOLUTE precedence over any DB access
# ---------------------------------------------------------------------------

def test_injected_rows_beat_a_db_query_that_explodes_if_called():
    """With rows injected, a db_query that RAISES on call is never consulted.

    This is the strongest possible proof that no DB access happens: if the
    adapter so much as touched the query path, the test would error out.
    """

    def exploding_db_query(sql, params):  # pragma: no cover - must never run
        raise AssertionError(
            "db_query was called even though rows were injected (DB access leak!)"
        )

    rows = [_row(2, "2026-06-21T00:00:02Z"), _row(1, "2026-06-21T00:00:01Z")]
    adapter = CastDbAdapter("sess-x", rows=rows, db_query=exploding_db_query)
    # Iterate twice — caching must not fall through to the exploding query either.
    first = list(adapter.events())
    second = list(adapter.events())
    assert [e.raw_id for e in first] == [1, 2]
    assert [e.raw_id for e in second] == [1, 2]


# ---------------------------------------------------------------------------
# Live mode — session id is a BOUND parameter, never spliced into SQL text
# ---------------------------------------------------------------------------

def test_live_mode_binds_session_id_and_never_interpolates_it():
    """The session id reaches db_query as the bound ('<id>',) tuple only.

    The id MUST NOT appear anywhere in the SQL string (injection guard), and the
    exact parameterized query the adapter ships must be used verbatim.
    """
    captured = {}

    # A session id containing SQL metacharacters: if it were string-interpolated
    # the apostrophe/semicolon would corrupt the query text — proving it is not.
    nasty_id = "abc'; DROP TABLE agent_runs;--"

    def fake_db_query(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return [_row(1, "2026-06-21T00:00:01Z")]

    adapter = CastDbAdapter(nasty_id, db_query=fake_db_query)
    events = list(adapter.events())

    assert captured["params"] == (nasty_id,)
    assert nasty_id not in captured["sql"]
    assert "DROP TABLE" not in captured["sql"]
    assert "WHERE session_id = ?" in captured["sql"]
    # The adapter ships its single canonical parameterized query, unmodified.
    assert captured["sql"] == _AGENT_RUNS_SQL
    assert len(events) == 1


def test_live_mode_handles_sqlite3_row_objects():
    """Live mode works against real sqlite3.Row rows (cast_db's row_factory).

    The injected-dict tests do not represent the real row shape; this pins that
    column access by name works on sqlite3.Row too, and ordering still applies.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    def fake_db_query(sql, params):
        cur = conn.execute(
            "SELECT 2 AS id, ? AS session_id, 'workflow-subagent' AS agent, "
            "'2026-06-21T00:00:02Z' AS started_at, 1200 AS input_tokens, "
            "2.5 AS cost_usd "
            "UNION ALL "
            "SELECT 1, ?, 'workflow-subagent', '2026-06-21T00:00:01Z', 1190, 1.5",
            (params[0], params[0]),
        )
        return cur.fetchall()

    events = list(CastDbAdapter("sess-x", db_query=fake_db_query).events())
    assert [e.raw_id for e in events] == [1, 2]  # ordered by (started_at, id)
    assert [e.cost_usd for e in events] == [1.5, 2.5]
    assert all(e.tool == "dispatch" and e.args_hash is None for e in events)


# ---------------------------------------------------------------------------
# Lazy import — merely importing the adapter must NOT pull in cast_db
# ---------------------------------------------------------------------------

def test_importing_adapter_does_not_import_cast_db(monkeypatch):
    """Re-importing looptrip.adapters.cast_db with the CAST scripts dir absent
    from sys.path leaves 'cast_db' out of sys.modules.

    Proves the cast_db dependency is resolved lazily (only when a live query is
    actually forced), so the package imports cleanly in CI where cast_db is
    absent.
    """
    from looptrip.adapters import cast_db as cdb

    scripts_dir = cdb._CAST_DB_SCRIPTS_DIR
    mod_name = "looptrip.adapters.cast_db"

    # Simulate a clean CI machine: scripts dir off sys.path, neither module loaded.
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != scripts_dir])
    monkeypatch.delitem(sys.modules, "cast_db", raising=False)
    monkeypatch.delitem(sys.modules, mod_name, raising=False)

    importlib.import_module(mod_name)

    assert "cast_db" not in sys.modules
    assert scripts_dir not in sys.path


# ---------------------------------------------------------------------------
# Ordering is enforced regardless of input order
# ---------------------------------------------------------------------------

def test_ordering_applied_even_when_injected_rows_are_shuffled():
    """Deliberately scrambled rows come out sorted by (started_at, id).

    Includes a started_at tie (ids 3 and 4 share a timestamp) to exercise the
    id tiebreak inside the composite sort key.
    """
    shuffled = [
        _row(5, "2026-06-21T00:00:05Z"),
        _row(4, "2026-06-21T00:00:03Z"),  # tie with id 3 on started_at
        _row(1, "2026-06-21T00:00:01Z"),
        _row(3, "2026-06-21T00:00:03Z"),  # tie with id 4 on started_at
        _row(2, "2026-06-21T00:00:02Z"),
    ]
    events = list(CastDbAdapter("sess-x", rows=shuffled).events())
    assert [e.raw_id for e in events] == [1, 2, 3, 4, 5]
    keys = [(e.ts, e.raw_id) for e in events]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# None cost_usd passes through faithfully
# ---------------------------------------------------------------------------

def test_none_cost_rows_pass_through_as_none_without_crashing():
    """Rows whose cost_usd is None surface as cost_usd=None, not 0 or a crash.

    The prevented-waste accounting treats a None cost as $0 downstream; the
    adapter's job is faithful passthrough, which this pins. ``input_tokens=None``
    rides along to prove null token counts are likewise preserved.
    """
    rows = [
        _row(1, "2026-06-21T00:00:01Z", cost_usd=None, input_tokens=None),
        _row(2, "2026-06-21T00:00:02Z", cost_usd=3.5, input_tokens=900),
    ]
    events = list(CastDbAdapter("sess-x", rows=rows).events())
    assert [e.cost_usd for e in events] == [None, 3.5]
    assert [e.input_tokens for e in events] == [None, 900]
    # The None-cost row is not silently dropped or coerced.
    assert events[0].raw_id == 1


# ---------------------------------------------------------------------------
# started_at=None — pin the CURRENT (un-handled) behavior precisely
# ---------------------------------------------------------------------------

def test_started_at_none_among_other_rows_sorts_none_first():
    """A row with started_at=None alongside string-timestamped rows does NOT
    raise TypeError; the None-timestamp row sorts before string-timestamp rows
    (null-first ordering, matching SQLite NULL convention).

    Previously (pre-remediation) the adapter raised TypeError here because
    Python's sorted() cannot compare None and str. The null-safe sort key
    ``(r["started_at"] or "", r["id"])`` coerces None to "" so rows with a
    None timestamp sort deterministically before any real ISO timestamp.
    """
    rows = [
        _row(2, "2026-06-21T00:00:02Z"),
        _row(1, None),
    ]
    adapter = CastDbAdapter("sess-x", rows=rows)
    events = list(adapter.events())
    assert len(events) == 2
    # The None-timestamp row (id=1) sorts BEFORE the string-timestamp row (id=2).
    assert events[0].raw_id == 1
    assert events[0].ts is None
    assert events[1].raw_id == 2


def test_single_started_at_none_row_does_not_raise():
    """A lone row with started_at=None does NOT raise (sorted() of one element
    performs no comparison) — it yields one event whose ts is None.

    This is the boundary that makes the multi-row case above a comparison
    problem rather than a value problem: the crash is about ORDERING two rows,
    not about a None timestamp per se.
    """
    events = list(CastDbAdapter("sess-x", rows=[_row(1, None)]).events())
    assert len(events) == 1
    assert events[0].ts is None
    assert events[0].raw_id == 1
