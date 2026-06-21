"""Tests for the ping-pong / livelock detector (src/looptrip/detectors/ping_pong.py).

Events are built directly here — no fixture is needed to exercise the state
machine. The ping-pong detector is deterministic and token-independent,
firing on structural cycles in the agent-visitation graph.
"""

from __future__ import annotations

import math

from looptrip.detector import (
    KIND_PING_PONG,
    PathologyReport,
    detect_ping_pong,
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
    handoff_state: str | None = None,
    ts: str | None = None,
) -> Event:
    """Build a normalized Event for testing."""
    return Event(
        agent=agent,
        tool=tool,
        args_hash=args_hash,
        ts=ts or f"2026-06-21T00:00:{raw_id:02d}Z",
        input_tokens=input_tokens,
        cost_usd=cost_usd,
        progress=progress,
        handoff_state=handoff_state,
        raw_id=raw_id,
    )


# ---------------------------------------------------------------------------
# Happy path: basic 2-agent A-B-A-B-A ping-pong cycle
# ---------------------------------------------------------------------------


def test_two_agent_ping_pong_trips_at_second_closure():
    """A↔B cycle (A,B,A,B,A) trips at the 5th event (2nd closure)."""
    events = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="B"),
        _dispatch(3, agent="A"),
        _dispatch(4, agent="B"),
        _dispatch(5, agent="A"),  # 2nd closure: trip here
    ]
    reports = detect_ping_pong(events)
    assert len(reports) == 1
    report = reports[0]
    assert report.kind == KIND_PING_PONG
    assert report.members == ("A", "B")
    assert report.signature == ("A", "B")
    assert report.trip_event.raw_id == 5
    assert report.first_event.raw_id == 3  # event at 1st closure
    assert report.trip_index == 2  # cycle_trip_count default
    assert report.occurrences == 2  # total closures lifetime


def test_two_agent_blocked_at_first_closure_does_not_trip():
    """A,B,A closes once (1st closure); no trip without 2nd closure."""
    events = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="B"),
        _dispatch(3, agent="A"),
    ]
    reports = detect_ping_pong(events)
    assert reports == []


def test_prevented_cost_and_runs_scoped_to_cycle_members():
    """prevented_cost sums only events strictly after trip whose agent ∈ members."""
    events = [
        _dispatch(1, agent="A", cost_usd=1.0),
        _dispatch(2, agent="B", cost_usd=2.0),
        _dispatch(3, agent="A", cost_usd=3.0),
        _dispatch(4, agent="B", cost_usd=4.0),
        _dispatch(5, agent="A", cost_usd=5.0),  # trip (2nd closure)
        _dispatch(6, agent="A", cost_usd=10.0),  # counted: agent in {A,B}
        _dispatch(7, agent="C", cost_usd=100.0),  # not counted: C not in cycle
        _dispatch(8, agent="B", cost_usd=20.0),  # counted: agent in {A,B}
        _dispatch(9, agent="A", cost_usd=30.0),  # counted
    ]
    reports = detect_ping_pong(events)
    assert len(reports) == 1
    report = reports[0]
    # post-trip member events: idx 6 (A,10), 8 (B,20), 9 (A,30) = 60 total
    assert math.isclose(report.prevented_cost, 60.0)
    assert report.prevented_runs == 3


def test_none_cost_treated_as_zero():
    """None cost_usd in post-trip events counts as 0.0."""
    events = [
        _dispatch(1, agent="A", cost_usd=1.0),
        _dispatch(2, agent="B", cost_usd=2.0),
        _dispatch(3, agent="A", cost_usd=3.0),
        _dispatch(4, agent="B", cost_usd=4.0),
        _dispatch(5, agent="A", cost_usd=5.0),  # trip
        _dispatch(6, agent="A", cost_usd=None),
        _dispatch(7, agent="B", cost_usd=10.0),
    ]
    reports = detect_ping_pong(events)
    assert len(reports) == 1
    # post-trip: None counts as 0, + 10 = 10 total
    assert math.isclose(reports[0].prevented_cost, 10.0)
    assert reports[0].prevented_runs == 2


# ---------------------------------------------------------------------------
# Three-agent cycle A-B-C
# ---------------------------------------------------------------------------


def test_three_agent_cycle_abc_abc_abc():
    """A,B,C,A,B,C,A trip at 7th event (2nd A, hence 2nd closure)."""
    events = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="B"),
        _dispatch(3, agent="C"),
        _dispatch(4, agent="A"),  # 1st closure: C→A
        _dispatch(5, agent="B"),
        _dispatch(6, agent="C"),
        _dispatch(7, agent="A"),  # 2nd closure: C→A; trip
    ]
    reports = detect_ping_pong(events)
    assert len(reports) == 1
    report = reports[0]
    assert report.members == ("A", "B", "C")
    assert report.trip_event.raw_id == 7
    assert report.first_event.raw_id == 4
    assert report.occurrences == 2


# ---------------------------------------------------------------------------
# Edge case: self-loop (consecutive same agent)
# ---------------------------------------------------------------------------


def test_self_loop_never_forms_cycle():
    """A,A,A self-loop does not trip (self-loops excluded)."""
    events = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="A"),
        _dispatch(3, agent="A"),
        _dispatch(4, agent="A"),
        _dispatch(5, agent="A"),
    ]
    reports = detect_ping_pong(events)
    assert reports == []


def test_self_loop_collapse_in_mixed_sequence():
    """A,A,B,B,A collapses to A,B,A (1 closure), does not trip."""
    events = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="A"),  # collapsed (same as previous)
        _dispatch(3, agent="B"),
        _dispatch(4, agent="B"),  # collapsed (same as previous)
        _dispatch(5, agent="A"),  # closes to A (at position 0), 1st closure
    ]
    reports = detect_ping_pong(events)
    assert reports == []


# ---------------------------------------------------------------------------
# Rotation invariance: B,C,A ≡ A,B,C (canonical form)
# ---------------------------------------------------------------------------


def test_rotation_equivalence_bca_equals_abc():
    """B,C,A,B,C,A,B is equivalent to starting A,B,C,A,B,C,A."""
    events = [
        _dispatch(1, agent="B"),
        _dispatch(2, agent="C"),
        _dispatch(3, agent="A"),
        _dispatch(4, agent="B"),  # 1st closure
        _dispatch(5, agent="C"),
        _dispatch(6, agent="A"),
        _dispatch(7, agent="B"),  # 2nd closure; trip
    ]
    reports = detect_ping_pong(events)
    assert len(reports) == 1
    # Canonical cycle: min rotation of [B,C,A] or [A,B,C] = (A,B,C)
    assert reports[0].members == ("A", "B", "C")


# ---------------------------------------------------------------------------
# Direction distinctness: A→B→C ≠ A→C→B (not reversed)
# ---------------------------------------------------------------------------


def test_reverse_direction_is_distinct():
    """A,C,B,A,C,B,A is distinct from A,B,C,A,B,C,A."""
    events = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="C"),
        _dispatch(3, agent="B"),
        _dispatch(4, agent="A"),  # 1st closure: B→A
        _dispatch(5, agent="C"),
        _dispatch(6, agent="B"),
        _dispatch(7, agent="A"),  # 2nd closure; trip
    ]
    reports = detect_ping_pong(events)
    assert len(reports) == 1
    # Canonical cycle: min rotation of [A,C,B] = (A, B, C) reversed = (A, C, B)
    assert reports[0].members == ("A", "C", "B")


# ---------------------------------------------------------------------------
# Interleaved independence: A,B,A,C,A,B,A keeps (A,B) and (A,C) separate
# ---------------------------------------------------------------------------


def test_interleaved_cycles_independent():
    """A,B,A,C,A,B,A trips on both (A,B) and (A,C) independently."""
    events = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="B"),
        _dispatch(3, agent="A"),  # closes (A,B), closure 1
        _dispatch(4, agent="C"),
        _dispatch(5, agent="A"),  # closes (A,C), closure 1 for (A,C)
        _dispatch(6, agent="B"),
        _dispatch(7, agent="A"),  # closes (A,B), closure 2; trip on (A,B)
    ]
    reports = detect_ping_pong(events)
    assert len(reports) == 1
    # (A,B) tripped; (A,C) not reached closure 2 yet
    assert reports[0].members == ("A", "B")
    # But if we extend to trigger (A,C):
    events2 = events + [
        _dispatch(8, agent="C"),
        _dispatch(9, agent="A"),  # closes (A,C), closure 2; trip
    ]
    reports2 = detect_ping_pong(events2)
    assert len(reports2) == 2
    members = {r.members for r in reports2}
    assert members == {("A", "B"), ("A", "C")}


# ---------------------------------------------------------------------------
# Epoch reset on progress event
# ---------------------------------------------------------------------------


def test_progress_event_resets_path_and_blocks_ongoing_closures():
    """A,B,A (1st closure), then progress, then B,A fails to close (different epoch)."""
    events = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="B"),
        _dispatch(3, agent="A"),  # would close (A,B) at closure 1
        _dispatch(4, agent="B", progress=True),  # epoch reset
        _dispatch(5, agent="A"),
    ]
    reports = detect_ping_pong(events)
    assert reports == []  # (A,B) never reaches closure 2


def test_progress_between_closures_resets_closure_counter():
    """Progress event after closure 1 clears path; closure count reset to 0."""
    events = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="B"),
        _dispatch(3, agent="A"),  # closure 1 for (A,B)
        _dispatch(4, agent="B", progress=True),  # progress: clears path (does not add node)
        _dispatch(5, agent="A"),
        _dispatch(6, agent="B"),
        _dispatch(7, agent="A"),  # would be closure 1 for new epoch if path allowed
    ]
    # After progress at idx 4, path is reset. Next cycle starts fresh at idx 5.
    # A,B,A closes to form cycle, but closure count for that key resets (epoch-scoped).
    # We need more events for a second closure to happen.
    reports = detect_ping_pong(events)
    # After progress, (A,B) starts fresh with closure count 0 at idx 7.
    # Only closure 1, no trip yet.
    assert reports == []


def test_terminal_event_resets_path_like_progress():
    """Terminal handoff_state event resets path (if terminal_states configured)."""
    events = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="B"),
        _dispatch(3, agent="A"),
        _dispatch(4, agent="B", handoff_state="DONE"),  # terminal
        _dispatch(5, agent="A"),
    ]
    reports = detect_ping_pong(events, terminal_states=frozenset({"DONE"}))
    assert reports == []


# ---------------------------------------------------------------------------
# handoff_state=None: full strength (no edges needed)
# ---------------------------------------------------------------------------


def test_ping_pong_works_with_handoff_state_none():
    """Temporal sequence works even when handoff_state is None everywhere."""
    events = [
        _dispatch(1, agent="A", handoff_state=None),
        _dispatch(2, agent="B", handoff_state=None),
        _dispatch(3, agent="A", handoff_state=None),
        _dispatch(4, agent="B", handoff_state=None),
        _dispatch(5, agent="A", handoff_state=None),
    ]
    reports = detect_ping_pong(events)
    assert len(reports) == 1
    assert reports[0].members == ("A", "B")


# ---------------------------------------------------------------------------
# use_handoff_edges: happy path when explicit targets are present
# ---------------------------------------------------------------------------


def test_use_handoff_edges_inserts_synthetic_hop_to_known_agent():
    """When use_handoff_edges=True, synthetic hop targets are added after events."""
    events = [
        _dispatch(1, agent="A", handoff_state="dispatching to B"),
        _dispatch(2, agent="B", handoff_state="dispatching to A"),
        _dispatch(3, agent="A", handoff_state="dispatching to B"),
        _dispatch(4, agent="B", handoff_state="dispatching to A"),
        _dispatch(5, agent="A", handoff_state="dispatching to B"),
    ]
    # With use_handoff_edges=True: A+(synthetic B), B+(synthetic A), A+(synthetic B), B+(synthetic A), A+(synthetic B)
    # Node sequence: A,B,B,A,A,B,B,A,A,B → collapses to A,B,A,B,A,B (self-loop collapse)
    # Path evolution: A, A,B, A,B,A → closes at 3rd node
    reports = detect_ping_pong(events, use_handoff_edges=True)
    # Should close at index 2 (A node after synthetic B at position 2)
    assert len(reports) == 1
    assert reports[0].members == ("A", "B")


def test_use_handoff_edges_false_ignores_targets_uses_temporal():
    """When use_handoff_edges=False (default), only temporal order matters."""
    events = [
        _dispatch(1, agent="A", handoff_state="dispatching to B"),
        _dispatch(2, agent="B", handoff_state="dispatching to A"),
        _dispatch(3, agent="C", handoff_state="none"),
        _dispatch(4, agent="A", handoff_state="dispatching to B"),
    ]
    # Temporal: A,B,C,A; no full cycle of length >= 2 that repeats
    reports = detect_ping_pong(events, use_handoff_edges=False)
    # A closes at index 0, so we have (A,B,C), closure 1; then A repeats at (A)
    # next event would be needed to form closure 2
    assert reports == []


# ---------------------------------------------------------------------------
# Exemption: fully exempt cycles are suppressed
# ---------------------------------------------------------------------------


def test_fully_exempt_cycle_suppressed():
    """A cycle where all members are in idempotent_agents is suppressed."""
    events = [
        _dispatch(1, agent="test-runner"),
        _dispatch(2, agent="code-reviewer"),
        _dispatch(3, agent="test-runner"),
        _dispatch(4, agent="code-reviewer"),
        _dispatch(5, agent="test-runner"),
    ]
    reports = detect_ping_pong(
        events,
        idempotent_agents=frozenset({"test-runner", "code-reviewer"}),
    )
    assert reports == []


def test_partially_exempt_cycle_not_suppressed():
    """If only some members are exempt, the cycle still trips."""
    events = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="test-runner"),
        _dispatch(3, agent="A"),
        _dispatch(4, agent="test-runner"),
        _dispatch(5, agent="A"),
    ]
    reports = detect_ping_pong(
        events,
        idempotent_agents=frozenset({"test-runner"}),
    )
    assert len(reports) == 1
    assert reports[0].members == ("A", "test-runner")


def test_retry_allowed_contributes_to_exemption():
    """retry_allowed agents contribute to the exemption union."""
    events = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="B"),
        _dispatch(3, agent="A"),
        _dispatch(4, agent="B"),
        _dispatch(5, agent="A"),
    ]
    reports = detect_ping_pong(
        events,
        retry_allowed=frozenset({"A", "B"}),
    )
    assert reports == []


# ---------------------------------------------------------------------------
# Empty / single-event streams
# ---------------------------------------------------------------------------


def test_empty_stream_returns_empty():
    """Empty event list returns no reports."""
    assert detect_ping_pong([]) == []


def test_single_event_returns_empty():
    """Single event cannot form a cycle."""
    events = [_dispatch(1, agent="A")]
    assert detect_ping_pong(events) == []


def test_two_different_agents_one_each_no_cycle():
    """A,B is not a cycle (no revisit)."""
    events = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="B"),
    ]
    assert detect_ping_pong(events) == []


# ---------------------------------------------------------------------------
# cycle_trip_count knob (trip at higher occurrence)
# ---------------------------------------------------------------------------


def test_cycle_trip_count_3_delays_trip():
    """With cycle_trip_count=3, trip on 3rd closure, not 2nd."""
    events = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="B"),
        _dispatch(3, agent="A"),  # closure 1
        _dispatch(4, agent="B"),
        _dispatch(5, agent="A"),  # closure 2
        _dispatch(6, agent="B"),
        _dispatch(7, agent="A"),  # closure 3; trip
    ]
    reports = detect_ping_pong(events, cycle_trip_count=3)
    assert len(reports) == 1
    assert reports[0].trip_event.raw_id == 7
    assert reports[0].trip_index == 3
    assert reports[0].occurrences == 3


# ---------------------------------------------------------------------------
# min_cycle_len knob (exclude short cycles)
# ---------------------------------------------------------------------------


def test_min_cycle_len_2_requires_at_least_two_agents():
    """min_cycle_len=2 (default) requires cycle of >= 2 distinct agents."""
    # Self-loop A,A is excluded by the collapse, so always >= 2 distinct
    # But we can test the boundary with a single-node cycle:
    # Actually, a real cycle needs at least 2 nodes by definition.
    # Test: A,B,A (cycle [A,B], len=2) vs hypothetical A,A (collapsed away).
    events_2_node = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="B"),
        _dispatch(3, agent="A"),
        _dispatch(4, agent="B"),
        _dispatch(5, agent="A"),
    ]
    reports = detect_ping_pong(events_2_node, min_cycle_len=2)
    assert len(reports) == 1


def test_min_cycle_len_4_requires_at_least_four_agents():
    """min_cycle_len=4 requires a 4-node cycle."""
    events_3_node = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="B"),
        _dispatch(3, agent="C"),
        _dispatch(4, agent="A"),
        _dispatch(5, agent="B"),
        _dispatch(6, agent="C"),
        _dispatch(7, agent="A"),
    ]
    reports = detect_ping_pong(events_3_node, min_cycle_len=4)
    assert reports == []  # (A,B,C) is only 3 nodes


def test_min_cycle_len_4_with_four_node_cycle():
    """min_cycle_len=4 fires on a 4-node cycle."""
    events = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="B"),
        _dispatch(3, agent="C"),
        _dispatch(4, agent="D"),
        _dispatch(5, agent="A"),  # closure 1
        _dispatch(6, agent="B"),
        _dispatch(7, agent="C"),
        _dispatch(8, agent="D"),
        _dispatch(9, agent="A"),  # closure 2; trip
    ]
    reports = detect_ping_pong(events, min_cycle_len=4)
    assert len(reports) == 1
    assert reports[0].members == ("A", "B", "C", "D")


# ---------------------------------------------------------------------------
# Multi-agent blind-spot vector: the documented Phase-1 gap
# ---------------------------------------------------------------------------


def test_multi_agent_blind_spot_vector_from_spec():
    """O/W alternating [O(1000), W(8000), O(1200), W(300), O(4000)] catches the gap.

    The duplicate-work detector misses this because:
    - O recurs at idx 0,2,4; args_hash=None; consecutive variances are within 5%
      (1000→1200 is 20%, but the detector pairs with immediately preceding O
      which is idx 0, 2000 tokens back, outside tolerance).
    - W recurs at idx 1,3; 8000→300 is massive variance, outside tolerance.

    But ping-pong catches it as a (O,W) 2-cycle structurally.
    """
    events = [
        _dispatch(1, agent="O", input_tokens=1000, cost_usd=1.0),
        _dispatch(2, agent="W", input_tokens=8000, cost_usd=1.0),
        _dispatch(3, agent="O", input_tokens=1200, cost_usd=1.0),
        _dispatch(4, agent="W", input_tokens=300, cost_usd=1.0),
        _dispatch(5, agent="O", input_tokens=4000, cost_usd=1.0),
    ]
    reports = detect_ping_pong(events)
    assert len(reports) == 1
    assert reports[0].members == ("O", "W")
    assert reports[0].trip_event.raw_id == 5
    # occurrences = 2 closures: O→W→O at idx 3 (1st), O→W→O at idx 5 (2nd)
    assert reports[0].occurrences == 2


# ---------------------------------------------------------------------------
# Multiple distinct cycles in one stream
# ---------------------------------------------------------------------------


def test_multiple_disjoint_cycles():
    """A,B,A,B (cycle 1) followed by C,D,C,D (cycle 2) → two reports."""
    events = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="B"),
        _dispatch(3, agent="A"),  # closure 1 for (A,B)
        _dispatch(4, agent="B"),
        _dispatch(5, agent="A"),  # closure 2 for (A,B); trip
        # now C,D starts fresh
        _dispatch(6, agent="C"),
        _dispatch(7, agent="D"),
        _dispatch(8, agent="C"),  # closure 1 for (C,D)
        _dispatch(9, agent="D"),
        _dispatch(10, agent="C"),  # closure 2 for (C,D); trip
    ]
    reports = detect_ping_pong(events)
    assert len(reports) == 2
    members = {r.members for r in reports}
    assert members == {("A", "B"), ("C", "D")}


# ---------------------------------------------------------------------------
# Deterministic output order (trip_pos ascending)
# ---------------------------------------------------------------------------


def test_output_order_is_trip_position_ascending():
    """Reports are emitted in ascending trip-position order."""
    events = [
        _dispatch(1, agent="X"),
        _dispatch(2, agent="Y"),
        _dispatch(3, agent="X"),  # closure 1 for (X,Y)
        _dispatch(4, agent="Y"),
        _dispatch(5, agent="X"),  # closure 2 for (X,Y); trip at idx 5
        _dispatch(6, agent="Z"),
        _dispatch(7, agent="W"),
        _dispatch(8, agent="Z"),  # closure 1 for (Z,W)
        _dispatch(9, agent="W"),
        _dispatch(10, agent="Z"),  # closure 2 for (Z,W); trip at idx 10
    ]
    reports = detect_ping_pong(events)
    assert len(reports) == 2
    # (X,Y) trips at idx 5, (Z,W) trips at idx 10
    assert reports[0].trip_event.raw_id == 5
    assert reports[1].trip_event.raw_id == 10


# ---------------------------------------------------------------------------
# Report fields validation
# ---------------------------------------------------------------------------


def test_report_fields_structure():
    """Verify all required fields are populated in PathologyReport."""
    events = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="B"),
        _dispatch(3, agent="A"),
        _dispatch(4, agent="B"),
        _dispatch(5, agent="A", cost_usd=99.99),
    ]
    reports = detect_ping_pong(events)
    report = reports[0]
    # All 10 required fields populated
    assert report.kind is not None
    assert report.signature is not None
    assert report.agent is not None
    assert report.occurrences is not None
    assert report.trip_index is not None
    assert report.trip_event is not None
    assert report.first_event is not None
    assert report.prevented_cost is not None
    assert report.prevented_runs is not None
    assert report.detail is not None
    # members field is also populated for ping_pong
    assert report.members is not None
    # blocked_agents and window are defaults (None/None for ping_pong)
    assert report.blocked_agents is None
    assert report.window is None


def test_report_is_frozen():
    """PathologyReport is frozen (immutable)."""
    events = [
        _dispatch(1, agent="A"),
        _dispatch(2, agent="B"),
        _dispatch(3, agent="A"),
        _dispatch(4, agent="B"),
        _dispatch(5, agent="A"),
    ]
    report = detect_ping_pong(events)[0]
    # Attempting to modify a field should raise FrozenInstanceError
    try:
        report.members = ("X", "Y")
        assert False, "Should have raised FrozenInstanceError"
    except Exception as e:
        # FrozenInstanceError message is "cannot assign to field '...'"
        assert "cannot assign" in str(e).lower()
