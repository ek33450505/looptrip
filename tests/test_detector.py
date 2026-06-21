"""Tests for the duplicate-work detector (src/looptrip/detector.py).

Events are built directly here — no fixture is needed to exercise the state
machine. The cast.db fixture proof lives separately.
"""

from __future__ import annotations

from looptrip.detector import (
    KIND_DUPLICATE_WORK,
    PathologyReport,
    detect,
    detect_duplicate_work,
)
from looptrip.normalize import Event


def _dispatch(
    raw_id: int,
    *,
    agent: str = "workflow-subagent",
    tool: str = "dispatch",
    args_hash=None,
    input_tokens=1000,
    cost_usd: float = 10.0,
    progress: bool = False,
    ts: str | None = None,
) -> Event:
    """Build a cast.db-style dispatch Event (args_hash=None by default)."""
    return Event(
        agent=agent,
        tool=tool,
        args_hash=args_hash,
        ts=ts or f"2026-06-21T00:00:{raw_id:02d}Z",
        input_tokens=input_tokens,
        cost_usd=cost_usd,
        progress=progress,
        raw_id=raw_id,
    )


# ---------------------------------------------------------------------------
# Trips at the 2nd occurrence
# ---------------------------------------------------------------------------

def test_trips_at_second_occurrence_within_tolerance():
    """Two same-signature events within token tolerance trip at occurrence #2."""
    events = [
        _dispatch(1, input_tokens=1000, cost_usd=10.98),
        _dispatch(2, input_tokens=1020, cost_usd=10.97),  # +2% variance <= 5%
    ]
    reports = detect_duplicate_work(events)
    assert len(reports) == 1
    report = reports[0]
    assert report.kind == KIND_DUPLICATE_WORK
    assert report.trip_index == 2
    assert report.trip_event.raw_id == 2
    assert report.first_event.raw_id == 1
    assert report.occurrences == 2


def test_exact_args_hash_match_trips_regardless_of_tokens():
    """When both events carry an args_hash, an exact match is the trip signal."""
    events = [
        _dispatch(1, args_hash="abc", input_tokens=10, cost_usd=1.0),
        _dispatch(2, args_hash="abc", input_tokens=9999, cost_usd=1.0),
    ]
    reports = detect_duplicate_work(events)
    assert len(reports) == 1
    assert reports[0].trip_index == 2


# ---------------------------------------------------------------------------
# Does NOT trip
# ---------------------------------------------------------------------------

def test_no_trip_when_variance_exceeds_tolerance():
    """Input-token variance above tolerance is not a duplicate — no trip."""
    events = [
        _dispatch(1, input_tokens=1000),
        _dispatch(2, input_tokens=2000),  # +100% variance > 5%
    ]
    assert detect_duplicate_work(events) == []


def test_no_trip_on_single_occurrence():
    """A signature seen exactly once cannot recur — no trip."""
    events = [_dispatch(1, input_tokens=1000)]
    assert detect_duplicate_work(events) == []


def test_no_trip_when_no_args_hash_and_no_tokens():
    """Insufficient signal (no args_hash, no input_tokens) never trips."""
    events = [
        _dispatch(1, args_hash=None, input_tokens=None),
        _dispatch(2, args_hash=None, input_tokens=None),
    ]
    assert detect_duplicate_work(events) == []


def test_distinct_args_hash_does_not_trip():
    """Two events with different non-None args hashes are not duplicates."""
    events = [
        _dispatch(1, args_hash="abc"),
        _dispatch(2, args_hash="xyz"),
    ]
    assert detect_duplicate_work(events) == []


# ---------------------------------------------------------------------------
# Prevented-cost / prevented-runs accounting
# ---------------------------------------------------------------------------

def test_prevented_cost_and_runs_on_four_event_group():
    """costs [10, 10, 5, 5] -> trip at #2, prevented 10 over 2 runs."""
    events = [
        _dispatch(1, cost_usd=10.0),
        _dispatch(2, cost_usd=10.0),
        _dispatch(3, cost_usd=5.0),
        _dispatch(4, cost_usd=5.0),
    ]
    reports = detect_duplicate_work(events)
    assert len(reports) == 1
    report = reports[0]
    assert report.trip_event.raw_id == 2
    assert report.prevented_runs == 2
    assert report.prevented_cost == 10.0
    assert report.occurrences == 4


def test_prevented_cost_treats_none_cost_as_zero():
    """A None cost_usd among post-trip events counts as 0.0."""
    events = [
        _dispatch(1, cost_usd=10.0),
        _dispatch(2, cost_usd=10.0),
        _dispatch(3, cost_usd=None),
        _dispatch(4, cost_usd=5.0),
    ]
    report = detect_duplicate_work(events)[0]
    assert report.prevented_runs == 2
    assert report.prevented_cost == 5.0


# ---------------------------------------------------------------------------
# Progress delta blocks the trip
# ---------------------------------------------------------------------------

def test_progress_event_between_occurrences_blocks_trip():
    """A progress=True event between two occurrences resets/blocks the trip."""
    events = [
        _dispatch(1, input_tokens=1000),
        _dispatch(2, input_tokens=1000, progress=True),  # real work, state delta
        _dispatch(3, input_tokens=1000),
    ]
    assert detect_duplicate_work(events) == []


def test_without_progress_the_same_stream_would_trip():
    """Control for the progress test: drop the progress flag and it trips."""
    events = [
        _dispatch(1, input_tokens=1000),
        _dispatch(2, input_tokens=1000, progress=False),
        _dispatch(3, input_tokens=1000),
    ]
    reports = detect_duplicate_work(events)
    assert len(reports) == 1
    assert reports[0].trip_event.raw_id == 2


# ---------------------------------------------------------------------------
# idempotent_agents suppression
# ---------------------------------------------------------------------------

def test_idempotent_agent_never_trips():
    """An otherwise-tripping signature is suppressed for idempotent agents."""
    events = [
        _dispatch(1, agent="test-runner", input_tokens=1000),
        _dispatch(2, agent="test-runner", input_tokens=1000),
    ]
    assert detect_duplicate_work(events, idempotent_agents=frozenset({"test-runner"})) == []


def test_idempotent_set_does_not_suppress_other_agents():
    """Only the named agents are exempt; others still trip."""
    events = [
        _dispatch(1, agent="workflow-subagent", input_tokens=1000),
        _dispatch(2, agent="workflow-subagent", input_tokens=1000),
    ]
    reports = detect_duplicate_work(events, idempotent_agents=frozenset({"test-runner"}))
    assert len(reports) == 1
    assert reports[0].agent == "workflow-subagent"


# ---------------------------------------------------------------------------
# threshold knob
# ---------------------------------------------------------------------------

def test_higher_threshold_delays_the_trip():
    """threshold=3 trips on the 3rd occurrence, not the 2nd."""
    events = [
        _dispatch(1, cost_usd=10.0),
        _dispatch(2, cost_usd=10.0),
        _dispatch(3, cost_usd=10.0),
        _dispatch(4, cost_usd=10.0),
    ]
    reports = detect_duplicate_work(events, threshold=3)
    assert len(reports) == 1
    report = reports[0]
    assert report.trip_index == 3
    assert report.trip_event.raw_id == 3
    assert report.prevented_runs == 1  # only id 4 is strictly after the trip


# ---------------------------------------------------------------------------
# Emit at most one report per signature
# ---------------------------------------------------------------------------

def test_one_report_per_signature():
    """A long runaway still yields exactly one report for its signature."""
    events = [_dispatch(i, cost_usd=1.0) for i in range(1, 11)]
    reports = detect_duplicate_work(events)
    assert len(reports) == 1
    assert reports[0].occurrences == 10
    assert reports[0].prevented_runs == 8  # ids 3..10 are after the trip at #2


# ---------------------------------------------------------------------------
# detect() sorts by prevented_cost DESC
# ---------------------------------------------------------------------------

def test_detect_sorts_by_prevented_cost_descending():
    """Two distinct runaways are returned costliest-first."""
    cheap = [
        _dispatch(1, agent="agent-cheap", cost_usd=1.0),
        _dispatch(2, agent="agent-cheap", cost_usd=1.0),
        _dispatch(3, agent="agent-cheap", cost_usd=1.0),
    ]
    pricey = [
        _dispatch(4, agent="agent-pricey", cost_usd=100.0),
        _dispatch(5, agent="agent-pricey", cost_usd=100.0),
        _dispatch(6, agent="agent-pricey", cost_usd=100.0),
    ]
    reports = detect(cheap + pricey)
    assert len(reports) == 2
    assert reports[0].agent == "agent-pricey"
    assert reports[0].prevented_cost == 100.0
    assert reports[1].agent == "agent-cheap"
    assert reports[1].prevented_cost == 1.0


def test_detect_empty_stream_returns_empty_list():
    """No events -> no reports."""
    assert detect([]) == []


def test_report_is_frozen_dataclass():
    """PathologyReport is immutable like Event."""
    events = [_dispatch(1, cost_usd=10.0), _dispatch(2, cost_usd=10.0)]
    report = detect_duplicate_work(events)[0]
    assert isinstance(report, PathologyReport)
    try:
        report.kind = "mutated"  # type: ignore[misc]
    except Exception as exc:  # FrozenInstanceError is a subclass of AttributeError-ish
        assert exc.__class__.__name__ == "FrozenInstanceError"
    else:  # pragma: no cover
        raise AssertionError("PathologyReport should be frozen")
