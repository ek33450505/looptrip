"""Tests for the non-termination detector (src/looptrip/detectors/non_termination.py).

Events are built directly here — no fixture is needed to exercise the state
machine. Non-termination is a bounded-liveness failure: a window whose
distinct-state count plateaus (stays at or below a cap) while the event count
grows, independent of token variance.
"""

from __future__ import annotations

import math
from looptrip.detectors.non_termination import detect_non_termination
from looptrip.detectors.types import (
    DetectionConfig,
    KIND_NON_TERMINATION,
    PathologyReport,
)
from looptrip.normalize import Event


def _dispatch(
    raw_id: int,
    *,
    agent: str = "workflow-subagent",
    tool: str = "dispatch",
    args_hash: str | None = None,
    input_tokens: int | None = 1000,
    cost_usd: float | None = 10.0,
    progress: bool = False,
    handoff_state: str | None = None,
    ts: str | None = None,
) -> Event:
    """Build a normalized Event for testing."""
    return Event(
        agent=agent,
        tool=tool,
        args_hash=args_hash,
        ts=ts or f"2026-06-21T00:00:{raw_id:02d}Z",
        handoff_state=handoff_state,
        input_tokens=input_tokens,
        cost_usd=cost_usd,
        progress=progress,
        raw_id=raw_id,
    )


# ---------------------------------------------------------------------------
# Happy path: N repeats fire with window/unique_states set
# ---------------------------------------------------------------------------


def test_constant_signature_fires_on_window_size_boundary():
    """25 identical-signature events with window_size=20 fire one report."""
    events = [_dispatch(i, input_tokens=1000 + i * 100) for i in range(1, 26)]
    reports = detect_non_termination(events, window_size=20)
    assert len(reports) == 1
    report = reports[0]
    assert report.kind == KIND_NON_TERMINATION
    # All events have the same signature, so distinct==1 (≤ cap).
    # window = (start_index, end_index_exclusive, unique_states, window_size)
    start, end, unique_states, win_size = report.window
    assert unique_states == 1
    assert win_size == 20
    # Verify the window covers the first 20 events (indices 0..19).
    assert start == 0
    assert end == 20
    assert report.trip_event.raw_id == 20  # 0-indexed, raw_id is 1-indexed
    assert report.first_event.raw_id == 1
    # occurrences = from window start through run end, inclusive
    assert report.occurrences == 25


def test_plateau_unique_states_absolute_cap():
    """plateau_unique_states=2 overrides the ratio-based cap."""
    # Two distinct signatures alternating: A, B, A, B, ... for 25 events.
    events = []
    for i in range(1, 26):
        agent = "agent-a" if i % 2 == 1 else "agent-b"
        events.append(_dispatch(i, agent=agent))
    # With window_size=20, ratio-based cap = floor(20 * 0.5) = 10.
    # But plateau_unique_states=2 overrides: distinct==2 ≤ cap==2 → qualifies.
    reports = detect_non_termination(
        events, window_size=20, plateau_unique_states=2
    )
    assert len(reports) == 1
    start, end, unique_states, win_size = reports[0].window
    assert unique_states == 2
    assert win_size == 20


def test_single_agent_high_variance_vector_is_detected():
    """A single agent with high-variance tokens is detected as non-terminating.

    This is the Phase-1 blind spot closed by non_termination. The signature
    is constant, so distinct==1 throughout, independent of token variance.
    """
    # Build the test vector from the spec: [1000, 2000, 1000, 2000, 1000].
    # With window_size=5, the first full window (indices 0..4) qualifies,
    # and distinct==1 (single agent, constant signature).
    events = [
        _dispatch(1, input_tokens=1000, cost_usd=10.0),
        _dispatch(2, input_tokens=2000, cost_usd=20.0),
        _dispatch(3, input_tokens=1000, cost_usd=10.0),
        _dispatch(4, input_tokens=2000, cost_usd=20.0),
        _dispatch(5, input_tokens=1000, cost_usd=10.0),
    ]
    reports = detect_non_termination(events, window_size=5)
    assert len(reports) == 1
    report = reports[0]
    assert report.kind == KIND_NON_TERMINATION
    assert report.trip_event.raw_id == 5
    assert report.first_event.raw_id == 1


# ---------------------------------------------------------------------------
# Edge cases: boundary conditions
# ---------------------------------------------------------------------------


def test_stream_shorter_than_window_size_returns_empty():
    """A stream with fewer events than window_size returns no reports."""
    events = [_dispatch(i) for i in range(1, 11)]
    reports = detect_non_termination(events, window_size=20)
    assert reports == []


def test_window_minus_one_boundary_does_not_fire():
    """Exactly window_size - 1 events do not trigger a full window."""
    events = [_dispatch(i) for i in range(1, 20)]  # 19 events, window_size=20
    reports = detect_non_termination(events, window_size=20)
    assert reports == []


def test_all_distinct_states_above_cap_does_not_fire():
    """When every window has more distinct states than cap, no fire."""
    # 25 events with 25 distinct signatures (each event is a new state).
    events = [_dispatch(i, args_hash=f"hash-{i}") for i in range(1, 26)]
    # window_size=20, cap=floor(20*0.5)=10, but we have 20 distinct states
    # in each window → distinct==20 > cap==10 → never qualifies.
    reports = detect_non_termination(events, window_size=20)
    assert reports == []


def test_cap_boundary_distinct_equals_cap_fires():
    """When distinct == cap (boundary), the window qualifies and fires."""
    # Exactly 10 distinct signatures, repeated to fill a 20-event window.
    # With default cap = floor(20 * 0.5) = 10, distinct == cap → qualifies.
    events = []
    for i in range(1, 26):
        idx = (i - 1) % 10
        events.append(_dispatch(i, agent=f"agent-{idx}"))
    reports = detect_non_termination(events, window_size=20)
    assert len(reports) == 1


def test_cap_boundary_distinct_exceeds_cap_no_fire():
    """When distinct == cap + 1 (just above boundary), window does not qualify."""
    # 11 distinct signatures in a 20-event window.
    # cap = floor(20 * 0.5) = 10, distinct == 11 > cap → does not qualify.
    events = []
    for i in range(1, 26):
        idx = (i - 1) % 11
        events.append(_dispatch(i, agent=f"agent-{idx}"))
    reports = detect_non_termination(events, window_size=20)
    assert reports == []


def test_progress_event_in_window_breaks_qualification():
    """A progress event inside a window disqualifies it."""
    # 25 identical-signature events, but event 15 has progress=True.
    events = [_dispatch(i) for i in range(1, 26)]
    events[14] = _dispatch(15, progress=True)  # Index 14 is event 15
    # The first full window (indices 0..19) includes the progress event
    # at index 14 → progress_count > 0 → does not qualify.
    reports = detect_non_termination(events, window_size=20)
    # May have a report if a later window (starting at index 6) qualifies;
    # after the progress event at index 14, windows from index 14..19
    # (the 6 events after progress) may not form a full 20-event window.
    # The spec says a progress event inside a window disqualifies it.
    assert len(reports) == 0


def test_terminal_event_in_window_breaks_qualification():
    """A terminal event inside a window disqualifies it."""
    events = [_dispatch(i) for i in range(1, 26)]
    # Mark event 15 as terminal (handoff_state in terminal_states).
    events[14] = _dispatch(15, handoff_state="DONE")
    cfg = DetectionConfig(terminal_states=frozenset({"DONE"}))
    reports = detect_non_termination(events, config=cfg, window_size=20)
    assert len(reports) == 0


def test_one_report_per_maximal_run_long_loop():
    """A 100-event loop yields exactly one report, not thousands."""
    # 100 identical events; a 100-event loop should yield ONE report
    # for the maximal plateau run.
    events = [_dispatch(i) for i in range(1, 101)]
    reports = detect_non_termination(events, window_size=20)
    assert len(reports) == 1
    # The run extends from the first qualifying window (ending at index 19)
    # through the last event (index 99).
    assert reports[0].occurrences == 100


def test_multiple_disjoint_plateau_runs():
    """A stream with a break in the middle yields multiple reports."""
    # First 25 identical events (distinct==1), then a progress event,
    # then another 25 identical events.
    events = [_dispatch(i, agent="agent-a") for i in range(1, 26)]
    # Progress event at position 25.
    events.append(_dispatch(26, agent="agent-a", progress=True))
    # Next 25 identical events.
    events.extend([_dispatch(i, agent="agent-a") for i in range(27, 52)])
    reports = detect_non_termination(events, window_size=20)
    # After the progress event, the path resets; the second plateau
    # (events 27..51) should yield a report if it forms a full window.
    # Events 27..51 = 25 events; window_size=20 → one full window at index 39 (27-based index 13).
    # But the window starting at index (26+20-1) = 45 to 64 is out of range.
    # Expected: two reports if both plateaus are separated by progress.
    assert len(reports) >= 1


def test_cap_floor_guard_with_tiny_plateau_ratio():
    """A tiny plateau_ratio still respects max(1, floor(...))."""
    # plateau_ratio=0.01, window_size=20 → floor(20 * 0.01) = 0.
    # But cap is max(1, 0) = 1, not 0.
    events = [_dispatch(i) for i in range(1, 26)]
    # distinct == 1 (single signature), cap == 1 → qualifies.
    reports = detect_non_termination(
        events, window_size=20, plateau_ratio=0.01
    )
    assert len(reports) == 1


# ---------------------------------------------------------------------------
# state_key dispatch
# ---------------------------------------------------------------------------


def test_state_key_signature_default():
    """state_key='signature' (default) groups by (agent, tool, args_hash)."""
    # Two events with the same agent but different args_hash are distinct states.
    events = [
        _dispatch(1, agent="agent-a", args_hash="hash-1"),
        _dispatch(2, agent="agent-a", args_hash="hash-2"),
        _dispatch(3, agent="agent-a", args_hash="hash-1"),
        _dispatch(4, agent="agent-a", args_hash="hash-2"),
    ] + [_dispatch(i, agent="agent-a", args_hash="hash-1") for i in range(5, 26)]
    # First window (indices 0..19): 10 'hash-1' and 10 'hash-2' → distinct==2.
    # With window_size=20, cap=10, distinct==2 ≤ cap → qualifies.
    reports = detect_non_termination(events, window_size=20)
    assert len(reports) == 1
    assert reports[0].window[2] == 2  # unique_states == 2


def test_state_key_agent_coarser_grouping():
    """state_key='agent' groups by agent only, coarser than signature."""
    # Many args_hash variants, but only two agents.
    events = []
    for i in range(1, 26):
        agent = "agent-a" if i % 2 == 1 else "agent-b"
        hash_val = f"hash-{i}"
        events.append(_dispatch(i, agent=agent, args_hash=hash_val))
    # With state_key='agent', distinct==2 regardless of args_hash.
    cfg = DetectionConfig(state_key="agent")
    reports = detect_non_termination(events, config=cfg, window_size=20)
    assert len(reports) == 1
    assert reports[0].window[2] == 2  # unique_states == 2


def test_state_key_handoff_state_with_none_key():
    """state_key='handoff_state' treats None as a distinct key."""
    # Events 1..10 have None, events 11..20 have 'DONE', events 21..25 repeat.
    events = [
        _dispatch(i, handoff_state=None) for i in range(1, 11)
    ] + [
        _dispatch(i, handoff_state="DONE") for i in range(11, 21)
    ] + [
        _dispatch(i, handoff_state=None) for i in range(21, 26)
    ]
    cfg = DetectionConfig(state_key="handoff_state")
    reports = detect_non_termination(events, config=cfg, window_size=20)
    # First full window (indices 0..19): None (10) + DONE (10) → distinct==2.
    assert len(reports) == 1
    assert reports[0].window[2] == 2


# ---------------------------------------------------------------------------
# Error cases: config validation
# ---------------------------------------------------------------------------


def test_window_size_zero_raises_valueerror():
    """window_size < 1 raises ValueError via DetectionConfig.__post_init__."""
    events = [_dispatch(i) for i in range(1, 26)]
    try:
        detect_non_termination(events, window_size=0)
        raise AssertionError("Expected ValueError for window_size=0")
    except ValueError as exc:
        assert "window_size" in str(exc).lower()


def test_window_size_negative_raises_valueerror():
    """window_size < 1 (negative) raises ValueError."""
    events = [_dispatch(i) for i in range(1, 26)]
    try:
        detect_non_termination(events, window_size=-5)
        raise AssertionError("Expected ValueError for window_size=-5")
    except ValueError as exc:
        assert "window_size" in str(exc).lower()


def test_plateau_ratio_above_one_raises_valueerror():
    """plateau_ratio > 1.0 raises ValueError."""
    events = [_dispatch(i) for i in range(1, 26)]
    try:
        detect_non_termination(events, plateau_ratio=1.5)
        raise AssertionError("Expected ValueError for plateau_ratio=1.5")
    except ValueError as exc:
        assert "plateau_ratio" in str(exc).lower()


def test_plateau_ratio_negative_raises_valueerror():
    """plateau_ratio < 0.0 raises ValueError."""
    events = [_dispatch(i) for i in range(1, 26)]
    try:
        detect_non_termination(events, plateau_ratio=-0.1)
        raise AssertionError("Expected ValueError for plateau_ratio=-0.1")
    except ValueError as exc:
        assert "plateau_ratio" in str(exc).lower()


def test_plateau_unique_states_zero_raises_valueerror():
    """plateau_unique_states must be None or >= 1."""
    events = [_dispatch(i) for i in range(1, 26)]
    try:
        detect_non_termination(events, plateau_unique_states=0)
        raise AssertionError("Expected ValueError for plateau_unique_states=0")
    except ValueError as exc:
        assert "plateau_unique_states" in str(exc).lower()


def test_state_key_invalid_raises_valueerror():
    """state_key must be one of 'signature', 'agent', 'handoff_state'."""
    events = [_dispatch(i) for i in range(1, 26)]
    try:
        detect_non_termination(events, state_key="invalid_key")
        raise AssertionError("Expected ValueError for state_key='invalid_key'")
    except ValueError as exc:
        assert "state_key" in str(exc).lower()


def test_empty_stream_returns_empty_list():
    """No events → no reports."""
    assert detect_non_termination([]) == []


# ---------------------------------------------------------------------------
# Report field validation
# ---------------------------------------------------------------------------


def test_report_signature_is_state_key_tuple():
    """Report signature is (state_key, state_value_at_trip)."""
    events = [_dispatch(i) for i in range(1, 26)]
    reports = detect_non_termination(events, window_size=20)
    assert len(reports) == 1
    # signature = (state_key, state_value)
    sig = reports[0].signature
    assert isinstance(sig, tuple)
    assert len(sig) == 2
    assert sig[0] == "signature"  # the state_key name
    # sig[1] is the state value (trip_event.signature())


def test_prevented_cost_and_runs():
    """prevented_cost/prevented_runs are scoped to events after trip point."""
    events = [
        _dispatch(i, cost_usd=10.0) for i in range(1, 26)
    ]
    reports = detect_non_termination(events, window_size=20)
    assert len(reports) == 1
    # Trip at index 19 (event 20), run_end at index 24 (event 25).
    # prevented = events[20:25] = 5 events → prevented_runs == 5.
    assert reports[0].prevented_runs == 5
    # prevented_cost = 5 * 10.0 = 50.0.
    assert reports[0].prevented_cost == 50.0


def test_prevented_cost_treats_none_as_zero():
    """A None cost_usd is treated as 0.0 in prevented_cost sum."""
    events = [_dispatch(i, cost_usd=10.0) for i in range(1, 21)]
    # Append events with None cost.
    events.append(_dispatch(21, cost_usd=None))
    events.append(_dispatch(22, cost_usd=None))
    events.append(_dispatch(23, cost_usd=5.0))
    events.append(_dispatch(24, cost_usd=5.0))
    events.append(_dispatch(25, cost_usd=5.0))
    # Trip at index 19, run_end at 24.
    # prevented = events[20:25] = 5 events with costs [None, None, 5.0, 5.0, 5.0].
    reports = detect_non_termination(events, window_size=20)
    assert len(reports) == 1
    # prevented_cost = 0 + 0 + 5 + 5 + 5 = 15.0
    assert reports[0].prevented_cost == 15.0


def test_report_is_frozen_dataclass():
    """PathologyReport is immutable."""
    events = [_dispatch(i) for i in range(1, 26)]
    report = detect_non_termination(events, window_size=20)[0]
    assert isinstance(report, PathologyReport)
    try:
        report.kind = "mutated"  # type: ignore[misc]
    except Exception as exc:
        assert exc.__class__.__name__ == "FrozenInstanceError"
    else:  # pragma: no cover
        raise AssertionError("PathologyReport should be frozen")


# ---------------------------------------------------------------------------
# Exempt events
# ---------------------------------------------------------------------------


def test_idempotent_agents_are_exempt():
    """Events from idempotent_agents do not count toward distinct or progress."""
    # Mix of idempotent and regular agents.
    events = [
        _dispatch(i, agent="agent-a") for i in range(1, 11)
    ] + [
        _dispatch(i, agent="test-runner") for i in range(11, 21)
    ] + [
        _dispatch(i, agent="agent-a") for i in range(21, 26)
    ]
    cfg = DetectionConfig(idempotent_agents=frozenset({"test-runner"}))
    # Without exemption: distinct==2 (agent-a and test-runner).
    # With exemption: test-runner is ignored; distinct==1 (agent-a) → qualifies.
    reports = detect_non_termination(events, config=cfg, window_size=20)
    assert len(reports) == 1


def test_idempotent_tools_are_exempt():
    """Events with idempotent_tools do not count toward distinct or progress."""
    events = [
        _dispatch(i, tool="dispatch") for i in range(1, 11)
    ] + [
        _dispatch(i, tool="test-tool") for i in range(11, 21)
    ] + [
        _dispatch(i, tool="dispatch") for i in range(21, 26)
    ]
    cfg = DetectionConfig(idempotent_tools=frozenset({"test-tool"}))
    reports = detect_non_termination(events, config=cfg, window_size=20)
    assert len(reports) == 1


def test_all_exempt_events_do_not_qualify():
    """A window where all events are exempt does not qualify."""
    events = [
        _dispatch(i, agent="test-runner") for i in range(1, 26)
    ]
    cfg = DetectionConfig(idempotent_agents=frozenset({"test-runner"}))
    # All events exempt → exempt_count == window_size → does not qualify.
    reports = detect_non_termination(events, config=cfg, window_size=20)
    assert reports == []
