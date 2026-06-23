"""tests/test_ingestion_robustness.py — core ingestion & CLI robustness locks.

Covers three defects in the ingestion/CLI path:

* **B1 (determinism)** — :func:`looptrip.adapters.otel.unix_nanos_to_iso` now
  always emits a fixed-width 9-digit fractional component, so the lexicographic
  string order downstream code sorts by equals chronological order. A bare
  whole-second form (``'...:01Z'``) would sort AFTER same-second sub-second
  values (``'.'`` 0x2E < ``'Z'`` 0x5A), flipping an A→B→A sequence.
* **B2 (edge-case)** — the CLI's non-otel sort key is null-safe, so a cast-db
  source mixing a ``None`` timestamp (NULL ``started_at``) with string
  timestamps sorts cleanly instead of raising ``TypeError``.
* **S2 (input-hardening)** — a deeply-nested ``otel:`` JSON file (``json.load``
  raising :class:`RecursionError`) and an oversized input both exit 2 cleanly,
  with no traceback.

Stdlib + pytest only; no real cast.db (cast-db rows are injected).
"""

from __future__ import annotations

import json

import pytest

from looptrip import cli
from looptrip.adapters import otel as otel_mod
from looptrip.adapters.cast_db import CastDbAdapter
from looptrip.adapters.otel import OTelSpanAdapter, _normalize_otlp, unix_nanos_to_iso

# Two nanosecond timestamps within the SAME wall-clock second: one exact, one
# sub-second. 1717200001000000000 ns == 2024-06-01T00:00:01(.000000000)Z.
_EXACT_SECOND_NS = "1717200001000000000"
_SUB_SECOND_NS = "1717200001500000000"


# ---------------------------------------------------------------------------
# B1 — uniform fixed-width timestamps; lexicographic == chronological order
# ---------------------------------------------------------------------------


def test_b1_whole_and_sub_second_share_uniform_shape():
    """Whole-second and sub-second values both carry a 9-digit fractional part."""
    assert unix_nanos_to_iso(int(_EXACT_SECOND_NS)) == "2024-06-01T00:00:01.000000000Z"
    assert unix_nanos_to_iso(int(_SUB_SECOND_NS)) == "2024-06-01T00:00:01.500000000Z"
    assert unix_nanos_to_iso(1717200001123456789) == "2024-06-01T00:00:01.123456789Z"


def test_b1_exact_second_sorts_before_subsecond_lexicographically():
    """The exact-second value sorts BEFORE the same-second sub-second value.

    This is the crux of B1: under the old bare-second form ``'...:01Z'`` the
    exact second sorted AFTER ``'...:01.5...Z'`` (``'Z'`` > ``'.'``), inverting
    chronological order. The uniform shape restores it.
    """
    exact = unix_nanos_to_iso(int(_EXACT_SECOND_NS))
    sub = unix_nanos_to_iso(int(_SUB_SECOND_NS))
    assert exact < sub
    # Regression witness: the retired bare form would have sorted AFTER.
    assert not ("2024-06-01T00:00:01Z" < sub)


def test_b1_single_source_mixed_timestamps_sort_chronologically():
    """One OTLP source mixing exact- and sub-second spans yields chronological order.

    Two handoff spans in the same second (the later given a higher span_id so
    only the timestamp can drive order): the exact-second span (agent-early)
    must precede the sub-second span (agent-late).
    """
    doc = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            # Sub-second span listed FIRST in the doc (input order
                            # must not survive — the sort governs).
                            {
                                "spanId": "aaa-subsecond",
                                "startTimeUnixNano": _SUB_SECOND_NS,
                                "attributes": [
                                    {
                                        "key": "gen_ai.agent.handoff.source.name",
                                        "value": {"stringValue": "agent-late"},
                                    }
                                ],
                            },
                            # Exact-second span, alphabetically LATER span_id so
                            # span_id cannot rescue the order — only ts can.
                            {
                                "spanId": "zzz-exact",
                                "startTimeUnixNano": _EXACT_SECOND_NS,
                                "attributes": [
                                    {
                                        "key": "gen_ai.agent.handoff.source.name",
                                        "value": {"stringValue": "agent-early"},
                                    }
                                ],
                            },
                        ]
                    }
                ]
            }
        ]
    }
    spans = _normalize_otlp(doc)
    events = list(OTelSpanAdapter(spans).events())
    assert [e.agent for e in events] == ["agent-early", "agent-late"]
    tss = [e.ts for e in events]
    assert tss == sorted(tss)


# ---------------------------------------------------------------------------
# B2 — cast-db NULL started_at mixed with timestamps sorts without TypeError
# ---------------------------------------------------------------------------


def _cast_db_rows_with_null_started_at():
    """Rows for one session: a NULL started_at row mixed with timestamped rows."""
    return [
        {
            "id": 2,
            "agent": "alpha",
            "started_at": "2024-01-01T00:00:02Z",
            "input_tokens": 100,
            "cost_usd": 1.0,
        },
        {
            "id": 1,
            "agent": "beta",
            "started_at": None,  # NULL started_at — the B2 trigger
            "input_tokens": 100,
            "cost_usd": 1.0,
        },
        {
            "id": 3,
            "agent": "gamma",
            "started_at": "2024-01-01T00:00:03Z",
            "input_tokens": 100,
            "cost_usd": 1.0,
        },
    ]


def _patch_cast_db(monkeypatch, rows):
    """Make the CLI's ``cast-db:`` scheme yield an adapter over injected rows."""
    monkeypatch.setattr(
        cli, "CastDbAdapter", lambda session_id: CastDbAdapter(session_id, rows=rows)
    )


def test_b2_source_events_cast_db_null_ts_sorts_null_first(monkeypatch):
    """_source_events sorts a NULL-ts cast-db stream null-first without TypeError."""
    _patch_cast_db(monkeypatch, _cast_db_rows_with_null_started_at())
    events = cli._source_events("cast-db:sess")
    # Null-first ordering (NULL started_at coerced to '' for the sort key).
    assert [e.ts for e in events] == [None, "2024-01-01T00:00:02Z", "2024-01-01T00:00:03Z"]
    assert [e.raw_id for e in events] == [1, 2, 3]


def test_b2_scan_cast_db_null_started_at_exits_clean(monkeypatch, capsys):
    """``scan cast-db:<id>`` with a NULL started_at row exits 0 with empty stderr."""
    _patch_cast_db(monkeypatch, _cast_db_rows_with_null_started_at())
    rc = cli.main(["scan", "cast-db:sess"])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Traceback" not in captured.out


# ---------------------------------------------------------------------------
# S2a — deeply-nested otel: JSON yields a clean exit-2, not a traceback
# ---------------------------------------------------------------------------


def test_s2a_recursion_error_maps_to_clean_exit2(monkeypatch, tmp_path, capsys):
    """A RecursionError from JSON loading is caught and surfaced as exit 2.

    Deterministic: replaces ``from_json_file`` with a RecursionError-raising
    stub so the curated-exception contract is exercised independently of the
    interpreter's json recursion threshold.
    """

    def _boom(path, scenario=None):
        raise RecursionError("maximum recursion depth exceeded while decoding JSON")

    monkeypatch.setattr(OTelSpanAdapter, "from_json_file", staticmethod(_boom))
    p = tmp_path / "x.json"
    p.write_text("[]")
    rc = cli.main(["scan", f"otel:{p}", "--all"])
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error:")
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_s2a_deeply_nested_json_file_exits_clean(tmp_path, capsys):
    """A pathologically deep ``otel:`` JSON file exits 2 cleanly (no traceback).

    Skips only on an interpreter whose ``json.loads`` does not raise
    RecursionError at the chosen depth (so the test never fails spuriously).
    """
    depth = 600_000
    deep = "[" * depth + "]" * depth
    try:
        json.loads(deep)
    except RecursionError:
        pass
    else:  # pragma: no cover - depends on interpreter json internals
        pytest.skip("interpreter does not raise RecursionError at the chosen JSON depth")

    p = tmp_path / "deep.json"
    p.write_text(deep)
    rc = cli.main(["scan", f"otel:{p}", "--all"])
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error:")
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# S2b — oversized inputs are rejected cleanly (size + JSONL span caps)
# ---------------------------------------------------------------------------


def test_s2b_guard_input_size_raises_for_oversized(tmp_path, monkeypatch):
    """_guard_input_size raises a clean ValueError above the byte cap."""
    monkeypatch.setattr(otel_mod, "_MAX_INPUT_BYTES", 4)
    p = tmp_path / "f.json"
    p.write_text("123456789")  # 9 bytes > 4-byte cap
    with pytest.raises(ValueError, match="exceeding"):
        otel_mod._guard_input_size(str(p))


def test_s2b_guard_input_size_allows_normal(tmp_path):
    """_guard_input_size is a no-op for a normal-sized file (default 256 MiB cap)."""
    p = tmp_path / "f.json"
    p.write_text("123")
    otel_mod._guard_input_size(str(p))  # must not raise


def test_s2b_guard_input_size_missing_file_is_silent(tmp_path):
    """A missing path is left for the caller's open() to raise — guard stays silent."""
    otel_mod._guard_input_size(str(tmp_path / "no_such_file.json"))  # must not raise


def test_s2b_scan_oversized_file_exits_clean(tmp_path, monkeypatch, capsys):
    """``scan otel:<oversized>.json`` exits 2 cleanly with an 'error:' line."""
    monkeypatch.setattr(otel_mod, "_MAX_INPUT_BYTES", 16)
    span = {
        "span_id": "s1",
        "start_time": "2024-06-01T00:00:01Z",
        "attributes": {"gen_ai.agent.handoff.source.name": "agent-a"},
    }
    p = tmp_path / "big.json"
    p.write_text(json.dumps([span]))  # comfortably over 16 bytes
    rc = cli.main(["scan", f"otel:{p}", "--all"])
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error:")
    assert "exceeding" in captured.err
    assert "Traceback" not in captured.err


def test_s2b_jsonl_span_cap_raises(tmp_path, monkeypatch):
    """from_jsonl_file raises a clean ValueError above the accumulated-span cap."""
    monkeypatch.setattr(otel_mod, "_MAX_JSONL_SPANS", 1)
    span = {
        "span_id": "s",
        "start_time": "2024-06-01T00:00:01Z",
        "attributes": {"gen_ai.agent.handoff.source.name": "agent-a"},
    }
    p = tmp_path / "many.jsonl"
    p.write_text("\n".join(json.dumps(span) for _ in range(3)))
    with pytest.raises(ValueError, match="exceeds the maximum"):
        OTelSpanAdapter.from_jsonl_file(str(p))
