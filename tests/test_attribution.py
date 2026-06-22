"""Comprehensive pytest suite for looptrip.attribution module.

Tests counterfactual-replay attribution of confirmed pathologies to their
decisive event(s). Covers the decisive/overdetermined/multiple semantics,
error handling, edge cases, and invariants.
"""

from __future__ import annotations

import pytest
from looptrip.attribution import AttributionResult, attribute, attribute_all
from looptrip.detector import detect, detect_all
from looptrip.detectors.types import (
    PathologyReport,
    KIND_DUPLICATE_WORK,
    KIND_PING_PONG,
    KIND_DEADLOCK,
    KIND_NON_TERMINATION,
    ALL_DETECTORS,
)
from looptrip.normalize import Event
from looptrip.adapters.cast_db import CastDbAdapter


def ev(
    agent: str,
    raw_id: int,
    *,
    tokens: int = 100,
    cost: float = 1.0,
    hs: str | None = None,
    to_agent: str | None = None,
    progress: bool = False,
) -> Event:
    """Helper to build test events (minimal cast.db style)."""
    return Event(
        agent=agent,
        tool="dispatch",
        args_hash=None,
        ts=f"2024-01-01T00:00:{raw_id:02d}Z",
        handoff_state=hs,
        to_agent=to_agent,
        input_tokens=tokens,
        cost_usd=cost,
        progress=progress,
        raw_id=raw_id,
    )


# ---------------------------------------------------------------------------
# VERIFIED VECTOR 1: UNIQUE (deadlock asymmetry)
# ---------------------------------------------------------------------------


def test_deadlock_asymmetry_yields_unique_verdict():
    """Deadlock with asymmetric wait-for: raw_id=2 is the sole decisive event.

    Stream: A blocked on B, A blocked on B (both recorded twice to establish
    the wait state), then B blocked on A (the asymmetry; only one B event
    needed to tip the graph).  Removing the B→A wait (raw_id=2) averts the
    deadlock; removing either A→B wait is insufficient (both needed).
    """
    stream = [
        ev("A", 0, hs="BLOCKED", to_agent="B"),
        ev("A", 1, hs="BLOCKED", to_agent="B"),
        ev("B", 2, hs="BLOCKED", to_agent="A"),
    ]
    rep = detect(stream, detectors=(KIND_DEADLOCK,))[0]
    assert rep.kind == KIND_DEADLOCK

    res = attribute(stream, rep)

    assert res.verdict == "unique"
    assert res.is_decisive is True
    assert len(res.decisive) == 1
    assert res.decisive[0].raw_id == 2
    assert res.tested == 3
    assert res.fingerprint == (KIND_DEADLOCK, ("A", "B"))
    assert "raw_id=2" in res.detail


def test_deadlock_asymmetry_decisive_tuple_in_stream_order():
    """Decisive events are returned in stream order."""
    stream = [
        ev("A", 0, hs="BLOCKED", to_agent="B"),
        ev("A", 1, hs="BLOCKED", to_agent="B"),
        ev("B", 2, hs="BLOCKED", to_agent="A"),
    ]
    rep = detect(stream, detectors=(KIND_DEADLOCK,))[0]
    res = attribute(stream, rep)

    # Only one decisive, so stream order is trivial; verify tuple structure
    assert isinstance(res.decisive, tuple)
    assert len(res.decisive) == 1


# ---------------------------------------------------------------------------
# VERIFIED VECTOR 2: MULTIPLE, all decisive (canonical ping-pong livelock)
# ---------------------------------------------------------------------------


def test_ping_pong_all_handoffs_decisive():
    """Canonical A-B-A-B-A ping-pong: all 5 events are independently decisive.

    Exactly 2 closures (counts), and each of the 5 handoffs is on the critical
    path. Removing any one drops below the trip threshold (cycle_trip_count=2).
    """
    stream = [
        ev("A", 0),
        ev("B", 1),
        ev("A", 2),
        ev("B", 3),
        ev("A", 4),
    ]
    rep = detect(stream, detectors=(KIND_PING_PONG,))[0]
    assert rep.kind == KIND_PING_PONG

    res = attribute(stream, rep)

    assert res.verdict == "multiple"
    assert len(res.decisive) == 5
    decisive_ids = tuple(e.raw_id for e in res.decisive)
    assert decisive_ids == (0, 1, 2, 3, 4)
    assert res.tested == 5
    assert res.fingerprint == (KIND_PING_PONG, ("A", "B"))


# ---------------------------------------------------------------------------
# VERIFIED VECTOR 3: MULTIPLE, discriminating (ping-pong with boundaries)
# ---------------------------------------------------------------------------


def test_ping_pong_boundary_events_not_decisive():
    """A-B-A-B-A-B stream: interior events decisive, boundary events not.

    With 6 events (3 closures), the first event (raw_id=0) and last event
    (raw_id=5) are NOT on the critical path — removing either still leaves
    2+ closures. Interior events [1,2,3,4] are all decisive.
    """
    stream = [
        ev("A", 0),
        ev("B", 1),
        ev("A", 2),
        ev("B", 3),
        ev("A", 4),
        ev("B", 5),
    ]
    rep = detect(stream, detectors=(KIND_PING_PONG,))[0]
    assert rep.kind == KIND_PING_PONG

    res = attribute(stream, rep)

    assert res.verdict == "multiple"
    decisive_ids = tuple(e.raw_id for e in res.decisive)
    # Interior events are decisive
    assert 1 in decisive_ids
    assert 2 in decisive_ids
    assert 3 in decisive_ids
    assert 4 in decisive_ids
    # Boundary events are NOT decisive (removing them leaves 2+ closures)
    assert 0 not in decisive_ids
    assert 5 not in decisive_ids


# ---------------------------------------------------------------------------
# VERIFIED VECTOR 4: OVERDETERMINED (synthetic runaway)
# ---------------------------------------------------------------------------


def test_duplicate_work_three_identical_events_overdetermined():
    """Three identical same-signature events: no single event is decisive.

    All three carry the same agent, tool, args_hash (None), and token counts,
    triggering duplicate_work. The pathology is the repeated structure itself,
    not any one event. Removing any single event still leaves 2+ occurrences,
    tripping the detector.
    """
    stream = [
        ev("W", 0, tokens=100, cost=1.0),
        ev("W", 1, tokens=100, cost=1.0),
        ev("W", 2, tokens=100, cost=1.0),
    ]
    rep = detect(stream)[0]  # default: duplicate_work
    assert rep.kind == KIND_DUPLICATE_WORK

    res = attribute(stream, rep)

    assert res.verdict == "overdetermined"
    assert res.is_decisive is False
    assert res.decisive == ()
    assert res.tested == 3
    assert "overdetermined" in res.detail


# ---------------------------------------------------------------------------
# VERIFIED VECTOR 5: MULTIPLE (minimal duplicate-work)
# ---------------------------------------------------------------------------


def test_duplicate_work_two_identical_events_multiple():
    """Two identical events: both are independently decisive.

    Removing either drops below threshold (2), averts the trip.
    """
    stream = [
        ev("W", 0, tokens=100, cost=1.0),
        ev("W", 1, tokens=100, cost=1.0),
    ]
    rep = detect(stream)[0]
    assert rep.kind == KIND_DUPLICATE_WORK

    res = attribute(stream, rep)

    assert res.verdict == "multiple"
    assert len(res.decisive) == 2
    assert tuple(e.raw_id for e in res.decisive) == (0, 1)


# ---------------------------------------------------------------------------
# VERIFIED VECTOR 6: MULTIPLE (symmetric deadlock)
# ---------------------------------------------------------------------------


def test_symmetric_deadlock_both_events_decisive():
    """Symmetric deadlock: A→B, B→A. Both waits are equally decisive."""
    stream = [
        ev("A", 0, hs="BLOCKED", to_agent="B"),
        ev("B", 1, hs="BLOCKED", to_agent="A"),
    ]
    rep = detect(stream, detectors=(KIND_DEADLOCK,))[0]
    assert rep.kind == KIND_DEADLOCK

    res = attribute(stream, rep)

    assert res.verdict == "multiple"
    assert len(res.decisive) == 2
    assert tuple(e.raw_id for e in res.decisive) == (0, 1)


# ---------------------------------------------------------------------------
# VERIFIED VECTOR 7: OVERDETERMINED on real packaged fixture
# ---------------------------------------------------------------------------


def test_real_fixture_session_b_headline_runaway_overdetermined():
    """Real fixture session B (headline runaway): 56 events, 0 decisive.

    The costliest pathology (duplicate_work on workflow-subagent) spans 49
    dispatches with no single decisive event. This is the honest result: the
    loop is structural, not caused by any one handoff.
    """
    session_id = "da27b414-f9f1-4c91-bd50-1a6096555066"
    adapter = CastDbAdapter.from_fixture(session_id)
    events = sorted(adapter.events(), key=lambda e: (e.ts, e.raw_id))

    reports = detect(events)
    assert len(reports) > 0

    # Take the costliest report (should be the workflow-subagent loop)
    rep = reports[0]

    res = attribute(events, rep)

    assert res.verdict == "overdetermined"
    assert res.is_decisive is False
    assert res.decisive == ()
    assert res.tested == len(events)
    assert len(events) == 56


# ---------------------------------------------------------------------------
# ERROR CASE 1: Invalid kind
# ---------------------------------------------------------------------------


def test_attribute_rejects_invalid_kind():
    """attribute() raises ValueError for unknown detector kind."""
    stream = [ev("A", 0)]
    bad_report = PathologyReport(
        kind="bogus_detector",
        signature=("A", "dispatch", None),
        agent="A",
        occurrences=1,
        trip_index=1,
        trip_event=stream[0],
        first_event=stream[0],
        prevented_cost=0.0,
        prevented_runs=0,
        detail="test",
    )
    with pytest.raises(ValueError) as exc_info:
        attribute(stream, bad_report)

    err_msg = str(exc_info.value)
    assert "bogus_detector" in err_msg
    assert "not a recognised detector kind" in err_msg
    # Error message lists valid kinds
    for kind in ALL_DETECTORS:
        assert kind in err_msg


def test_attribute_error_message_lists_all_detectors():
    """Error message for invalid kind enumerates all valid detectors."""
    stream = [ev("A", 0)]
    bad_report = PathologyReport(
        kind="invalid",
        signature=(),
        agent="A",
        occurrences=1,
        trip_index=1,
        trip_event=stream[0],
        first_event=stream[0],
        prevented_cost=0.0,
        prevented_runs=0,
        detail="test",
    )
    with pytest.raises(ValueError) as exc_info:
        attribute(stream, bad_report)

    err_msg = str(exc_info.value)
    for kind in ALL_DETECTORS:
        assert kind in err_msg


# ---------------------------------------------------------------------------
# ERROR CASE 2: Non-reproducible report
# ---------------------------------------------------------------------------


def test_attribute_raises_for_non_reproducible_report():
    """attribute() raises ValueError if the pathology cannot be re-detected."""
    # Build a stream that trips duplicate_work
    stream1 = [
        ev("W", 0, tokens=100),
        ev("W", 1, tokens=100),
    ]
    rep1 = detect(stream1)[0]

    # Different stream that does NOT trip the same pathology
    stream2 = [
        ev("X", 0, tokens=100),
        ev("X", 1, tokens=100),
    ]

    # Attempt to attribute stream1's report over stream2
    with pytest.raises(ValueError) as exc_info:
        attribute(stream2, rep1)

    err_msg = str(exc_info.value)
    assert "not reproducible" in err_msg or "Pathology" in err_msg


def test_attribute_non_reproducible_error_instructs_config_check():
    """Error for non-reproducible report mentions config/detection context."""
    stream1 = [
        ev("W", 0, tokens=100),
        ev("W", 1, tokens=100),
    ]
    rep1 = detect(stream1)[0]

    stream2 = [
        ev("X", 0, tokens=100),
    ]

    with pytest.raises(ValueError) as exc_info:
        attribute(stream2, rep1)

    err_msg = str(exc_info.value)
    assert "config" in err_msg.lower() or "stream has changed" in err_msg


# ---------------------------------------------------------------------------
# EDGE CASE 1: attribute_all() basic functionality
# ---------------------------------------------------------------------------


def test_attribute_all_basic():
    """attribute_all(events, reports) attributes every report in order."""
    stream = [
        ev("A", 0),
        ev("B", 1),
        ev("A", 2),
        ev("B", 3),
        ev("A", 4),
    ]
    reports = detect(stream, detectors=(KIND_PING_PONG,))
    assert len(reports) > 0

    results = attribute_all(stream, reports)

    assert len(results) == len(reports)
    # Each result corresponds to the corresponding report
    for i, res in enumerate(results):
        assert res.report is reports[i]
        assert isinstance(res, AttributionResult)


def test_attribute_all_empty_reports():
    """attribute_all(events, []) returns []."""
    stream = [ev("A", 0), ev("B", 1)]
    results = attribute_all(stream, [])
    assert results == []


def test_attribute_all_order_preserved():
    """attribute_all() preserves the order of reports."""
    stream = [
        ev("A", 0),
        ev("B", 1),
        ev("A", 2),
        ev("B", 3),
        ev("A", 4),
    ]
    reports = detect(stream, detectors=(KIND_PING_PONG,))

    results = attribute_all(stream, reports)

    for i, res in enumerate(results):
        assert res.report is reports[i]


# ---------------------------------------------------------------------------
# EDGE CASE 2: Generator input (iterator not double-consumed)
# ---------------------------------------------------------------------------


def test_attribute_all_materializes_generator_once():
    """attribute_all() consumes a generator input exactly once.

    If reports contains ≥2 items, and events is a generator, it must be
    materialized once and shared across all attribute() calls. This test
    builds reports from a materialized list, then passes a fresh generator
    to attribute_all.
    """

    def events_gen():
        """Yield events one by one."""
        for i in range(5):
            yield ev(["A", "B"][i % 2], i)

    # Materialize once to build reports
    stream_list = list(events_gen())
    reports = detect(stream_list, detectors=(KIND_PING_PONG,))
    assert len(reports) >= 1

    # Now pass a fresh generator to attribute_all
    results = attribute_all(events_gen(), reports)

    # Should succeed: attribute_all materializes the generator internally
    assert len(results) == len(reports)
    for res in results:
        assert isinstance(res, AttributionResult)


# ---------------------------------------------------------------------------
# EDGE CASE 3: Input not mutated
# ---------------------------------------------------------------------------


def test_attribute_does_not_mutate_input_list():
    """attribute() leaves the input list unchanged."""
    stream = [
        ev("A", 0),
        ev("B", 1),
        ev("A", 2),
        ev("B", 3),
        ev("A", 4),
    ]
    original_stream = list(stream)  # copy for comparison

    rep = detect(stream, detectors=(KIND_PING_PONG,))[0]
    attribute(stream, rep)

    # Stream unchanged: same objects, same order, same length
    assert len(stream) == len(original_stream)
    for orig, after in zip(original_stream, stream):
        assert orig is after


def test_attribute_all_does_not_mutate_input_list():
    """attribute_all() leaves the input list unchanged."""
    stream = [
        ev("A", 0),
        ev("B", 1),
        ev("A", 2),
        ev("B", 3),
        ev("A", 4),
    ]
    original_stream = list(stream)

    reports = detect(stream, detectors=(KIND_PING_PONG,))
    attribute_all(stream, reports)

    assert len(stream) == len(original_stream)
    for orig, after in zip(original_stream, stream):
        assert orig is after


# ---------------------------------------------------------------------------
# EDGE CASE 4: Determinism
# ---------------------------------------------------------------------------


def test_attribute_is_deterministic():
    """Calling attribute() twice on identical inputs yields identical results."""
    stream = [
        ev("A", 0),
        ev("B", 1),
        ev("A", 2),
        ev("B", 3),
        ev("A", 4),
    ]
    rep = detect(stream, detectors=(KIND_PING_PONG,))[0]

    res1 = attribute(stream, rep)
    res2 = attribute(stream, rep)

    assert res1.verdict == res2.verdict
    assert res1.tested == res2.tested
    assert tuple(e.raw_id for e in res1.decisive) == tuple(
        e.raw_id for e in res2.decisive
    )


# ---------------------------------------------------------------------------
# EDGE CASE 5: Invariants
# ---------------------------------------------------------------------------


def test_attribute_fingerprint_matches_report():
    """res.fingerprint == (res.report.kind, res.report.signature)."""
    stream = [ev("W", 0, tokens=100), ev("W", 1, tokens=100)]
    rep = detect(stream)[0]

    res = attribute(stream, rep)

    assert res.fingerprint == (rep.kind, rep.signature)


def test_attribute_tested_equals_stream_length():
    """res.tested == len(events) for any successful attribution."""
    stream = [
        ev("A", 0),
        ev("B", 1),
        ev("A", 2),
        ev("B", 3),
        ev("A", 4),
    ]
    rep = detect(stream, detectors=(KIND_PING_PONG,))[0]

    res = attribute(stream, rep)

    assert res.tested == len(stream)


def test_attribute_result_is_frozen():
    """AttributionResult is a frozen dataclass (immutable)."""
    stream = [ev("W", 0, tokens=100), ev("W", 1, tokens=100)]
    rep = detect(stream)[0]

    res = attribute(stream, rep)

    # Attempt to mutate a field should raise FrozenInstanceError
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        res.verdict = "modified"


# ---------------------------------------------------------------------------
# EDGE CASE 6: is_decisive property
# ---------------------------------------------------------------------------


def test_is_decisive_true_when_verdict_unique():
    """is_decisive is True iff verdict == 'unique'."""
    stream = [
        ev("A", 0, hs="BLOCKED", to_agent="B"),
        ev("A", 1, hs="BLOCKED", to_agent="B"),
        ev("B", 2, hs="BLOCKED", to_agent="A"),
    ]
    rep = detect(stream, detectors=(KIND_DEADLOCK,))[0]

    res = attribute(stream, rep)

    assert res.verdict == "unique"
    assert res.is_decisive is True


def test_is_decisive_false_when_verdict_overdetermined():
    """is_decisive is False when verdict == 'overdetermined'."""
    stream = [
        ev("W", 0, tokens=100),
        ev("W", 1, tokens=100),
        ev("W", 2, tokens=100),
    ]
    rep = detect(stream)[0]

    res = attribute(stream, rep)

    assert res.verdict == "overdetermined"
    assert res.is_decisive is False


def test_is_decisive_false_when_verdict_multiple():
    """is_decisive is False when verdict == 'multiple'."""
    stream = [
        ev("A", 0),
        ev("B", 1),
        ev("A", 2),
        ev("B", 3),
        ev("A", 4),
    ]
    rep = detect(stream, detectors=(KIND_PING_PONG,))[0]

    res = attribute(stream, rep)

    assert res.verdict == "multiple"
    assert res.is_decisive is False


# ---------------------------------------------------------------------------
# EDGE CASE 7: Detail string content
# ---------------------------------------------------------------------------


def test_unique_detail_string_mentions_raw_id_and_count():
    """Unique verdict detail mentions the raw_id and tested count."""
    stream = [
        ev("A", 0, hs="BLOCKED", to_agent="B"),
        ev("A", 1, hs="BLOCKED", to_agent="B"),
        ev("B", 2, hs="BLOCKED", to_agent="A"),
    ]
    rep = detect(stream, detectors=(KIND_DEADLOCK,))[0]

    res = attribute(stream, rep)

    assert "raw_id" in res.detail
    assert "2" in res.detail or "raw_id=2" in res.detail
    assert "3" in res.detail or "tested" in res.detail


def test_multiple_detail_string_mentions_count_of_decisive():
    """Multiple verdict detail mentions the count of decisive events."""
    stream = [
        ev("A", 0),
        ev("B", 1),
        ev("A", 2),
        ev("B", 3),
        ev("A", 4),
    ]
    rep = detect(stream, detectors=(KIND_PING_PONG,))[0]

    res = attribute(stream, rep)

    assert "independently-decisive" in res.detail or "5" in res.detail


def test_overdetermined_detail_string_mentions_overdetermined():
    """Overdetermined verdict detail explicitly mentions 'overdetermined'."""
    stream = [
        ev("W", 0, tokens=100),
        ev("W", 1, tokens=100),
        ev("W", 2, tokens=100),
    ]
    rep = detect(stream)[0]

    res = attribute(stream, rep)

    assert "overdetermined" in res.detail


# ---------------------------------------------------------------------------
# EDGE CASE 8: Single-event stream
# ---------------------------------------------------------------------------


def test_attribute_single_event_stream():
    """A single-event stream cannot trip any detector; test edge case gracefully."""
    stream = [ev("W", 0, tokens=100)]

    # Single event cannot trip duplicate_work (needs threshold=2)
    reps = detect(stream)
    assert len(reps) == 0


def test_attribute_all_on_empty_stream():
    """attribute_all() on empty events and empty reports returns []."""
    results = attribute_all([], [])
    assert results == []


# ---------------------------------------------------------------------------
# EDGE CASE 9: Very long stream
# ---------------------------------------------------------------------------


def test_attribute_long_stream_with_simple_pathology():
    """A longer stream with one simple pathology can be attributed."""
    # 20-event stream: ping-pong cycle A-B repeated many times
    events = []
    for i in range(20):
        agent = "A" if i % 2 == 0 else "B"
        events.append(ev(agent, i))

    rep = detect(events, detectors=(KIND_PING_PONG,))[0]
    res = attribute(events, rep)

    # Should complete without error
    assert res.tested == 20
    assert res.verdict in ("unique", "multiple", "overdetermined")


# ---------------------------------------------------------------------------
# EDGE CASE 10: Multiple reports from same stream
# ---------------------------------------------------------------------------


def test_attribute_all_with_multiple_different_pathologies():
    """attribute_all() attributes multiple reports of different kinds.

    Build a stream that triggers both ping-pong and duplicate_work, then
    attribute all reports.
    """
    # Construct a stream with both pathologies
    # First, a duplicate-work signature that repeats
    # Then, a ping-pong cycle
    stream = [
        # Duplicate work: W repeats with same tokens
        ev("W", 0, tokens=100, cost=1.0),
        ev("W", 1, tokens=100, cost=1.0),
        # Ping-pong: A-B-A-B-A cycle
        ev("A", 2),
        ev("B", 3),
        ev("A", 4),
        ev("B", 5),
        ev("A", 6),
    ]

    reports = detect_all(stream)
    assert len(reports) >= 1

    results = attribute_all(stream, reports)

    assert len(results) == len(reports)
    for res in results:
        assert res.tested == len(stream)


# ---------------------------------------------------------------------------
# Additional coverage: Config parameter
# ---------------------------------------------------------------------------


def test_attribute_with_explicit_config():
    """attribute() accepts an explicit DetectionConfig."""
    from looptrip.detectors.types import DetectionConfig

    stream = [
        ev("A", 0),
        ev("B", 1),
        ev("A", 2),
        ev("B", 3),
        ev("A", 4),
    ]

    cfg = DetectionConfig()  # Default config
    rep = detect(stream, config=cfg, detectors=(KIND_PING_PONG,))[0]
    res = attribute(stream, rep, config=cfg)

    assert res.verdict in ("unique", "multiple", "overdetermined")


def test_attribute_with_knobs():
    """attribute() accepts **knobs for ad-hoc config overrides."""
    stream = [
        ev("W", 0, tokens=100),
        ev("W", 1, tokens=100),
    ]

    # Pass config and knobs to detect
    rep = detect(stream, threshold=2)[0]

    # Pass the same knobs to attribute
    res = attribute(stream, rep, threshold=2)

    assert res.verdict in ("unique", "multiple", "overdetermined")
