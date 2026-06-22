"""Tests for the deadlock detector (src/looptrip/detectors/deadlock.py).

Events are built directly — no fixture is needed to exercise the state machine.
Tests verify Chandy–Misra–Haas wait-for cycle detection on direct in-memory
Event instances.
"""

from __future__ import annotations

import math

from looptrip.detector import (
    KIND_DEADLOCK,
    PathologyReport,
    detect_deadlock,
)
from looptrip.detectors.types import DetectionConfig
from looptrip.normalize import Event


def _dispatch(
    raw_id: int,
    *,
    agent: str = "agent-a",
    tool: str = "dispatch",
    args_hash: str | None = None,
    ts: str | None = None,
    handoff_state: str | None = None,
    to_agent: str | None = None,
    cost_usd: float = 10.0,
) -> Event:
    """Build a dispatch Event with configurable handoff_state for deadlock tests."""
    return Event(
        agent=agent,
        tool=tool,
        args_hash=args_hash,
        ts=ts or f"2026-06-21T00:00:{raw_id:02d}Z",
        handoff_state=handoff_state,
        to_agent=to_agent,
        cost_usd=cost_usd,
        raw_id=raw_id,
    )


# ---------------------------------------------------------------------------
# HAPPY PATH — 2-cycle and 3-cycle deadlocks
# ---------------------------------------------------------------------------


def test_two_cycle_deadlock_happy_path():
    """A blocked on B, B blocked on A → one deadlock report."""
    events = [
        _dispatch(1, agent="A", handoff_state="blocked", to_agent="B"),
        _dispatch(2, agent="B", handoff_state="blocked", to_agent="A"),
    ]
    reports = detect_deadlock(events)
    assert len(reports) == 1
    report = reports[0]
    assert report.kind == KIND_DEADLOCK
    assert report.signature == ("A", "B")
    assert report.agent == "A"  # min(members)
    assert report.occurrences == 2  # len(members)
    assert report.trip_index == 1
    assert report.members == ("A", "B")
    assert report.blocked_agents == frozenset({"A", "B"})
    assert report.prevented_cost == 0.0
    assert report.prevented_runs == 0


def test_three_cycle_deadlock():
    """A→B→C→A all blocked → one deadlock report with sorted members."""
    events = [
        _dispatch(1, agent="A", handoff_state="blocked", to_agent="B"),
        _dispatch(2, agent="B", handoff_state="blocked", to_agent="C"),
        _dispatch(3, agent="C", handoff_state="blocked", to_agent="A"),
    ]
    reports = detect_deadlock(events)
    assert len(reports) == 1
    report = reports[0]
    assert report.kind == KIND_DEADLOCK
    assert report.signature == ("A", "B", "C")
    assert report.agent == "A"  # min
    assert report.occurrences == 3
    assert report.members == ("A", "B", "C")
    assert report.blocked_agents == frozenset({"A", "B", "C"})
    assert report.prevented_cost == 0.0
    assert report.prevented_runs == 0


def test_trip_event_is_latest_blocked_with_max_ts():
    """trip_event is the member's latest blocked event with maximum ts."""
    events = [
        _dispatch(1, agent="A", handoff_state="blocked", to_agent="B", ts="2026-06-21T00:00:01Z"),
        _dispatch(2, agent="B", handoff_state="blocked", to_agent="A", ts="2026-06-21T00:00:05Z"),
        _dispatch(3, agent="A", handoff_state="blocked", to_agent="B", ts="2026-06-21T00:00:10Z"),
    ]
    reports = detect_deadlock(events)
    assert len(reports) == 1
    report = reports[0]
    assert report.trip_event.raw_id == 3  # A's latest blocked event
    assert report.first_event.raw_id == 2  # B's only blocked event (min ts overall)


def test_first_event_is_min_ts_across_members():
    """first_event is the earliest timestamp among all cycle members' latest events."""
    events = [
        _dispatch(1, agent="B", handoff_state="blocked", to_agent="A", ts="2026-06-21T00:00:02Z"),
        _dispatch(2, agent="A", handoff_state="blocked", to_agent="B", ts="2026-06-21T00:00:10Z"),
    ]
    reports = detect_deadlock(events)
    assert len(reports) == 1
    report = reports[0]
    assert report.first_event.raw_id == 1  # min ts


# ---------------------------------------------------------------------------
# EDGE CASES
# ---------------------------------------------------------------------------


def test_handoff_state_none_everywhere_returns_empty_list():
    """When handoff_state is None everywhere, deadlock detection returns [].

    This is the documented inherent limitation: a deadlock is defined in
    wait-for graph terms, which requires handoff_state naming a blocked state.
    When all events have handoff_state=None, the blocked map is empty → [].
    """
    events = [
        _dispatch(1, agent="A", handoff_state=None),
        _dispatch(2, agent="B", handoff_state=None),
        _dispatch(3, agent="A", handoff_state=None),
    ]
    reports = detect_deadlock(events)
    assert reports == []


def test_blocked_on_non_blocked_agent_no_cycle():
    """A blocked on B, but B's latest event is not blocked → no cycle, no report."""
    events = [
        _dispatch(1, agent="A", handoff_state="blocked", to_agent="B"),
        _dispatch(2, agent="B", handoff_state="blocked", to_agent="A"),
        _dispatch(3, agent="B", handoff_state=None),  # B now non-blocked (latest-state-wins)
    ]
    reports = detect_deadlock(events)
    assert reports == []


def test_self_wait_excluded():
    """An agent blocked on itself does not form a cycle (min_cycle_len >= 2).

    The graph-build phase excludes self-loops (t != u), so a self-wait
    creates no outgoing edge and terminates the walk as acyclic.
    """
    events = [
        _dispatch(1, agent="A", handoff_state="blocked", to_agent="A"),
    ]
    reports = detect_deadlock(events)
    assert reports == []


def test_unknown_target_not_in_blocked_set_dropped():
    """A blocks on agent-x, but agent-x never appears in the stream → no edge."""
    events = [
        _dispatch(1, agent="A", handoff_state="blocked", to_agent="agent-x"),
    ]
    reports = detect_deadlock(events)
    assert reports == []


def test_blocked_on_non_blocked_target_no_edge():
    """A blocked on B, but B is not in the blocked map (B's latest is non-blocked)
    → A has no outgoing edge in the graph → acyclic walk.
    """
    events = [
        _dispatch(1, agent="A", handoff_state="blocked", to_agent="B"),
        _dispatch(2, agent="B", handoff_state="waiting"),  # no target → blocked, but...
        _dispatch(3, agent="B", handoff_state=None),  # B's latest is non-blocked
    ]
    reports = detect_deadlock(events)
    assert reports == []


def test_latest_state_flip_to_non_blocked_dissolves_cycle():
    """A↔B cycle, but B's latest event flips to non-blocked → dissolved."""
    events = [
        _dispatch(1, agent="A", handoff_state="blocked", to_agent="B"),
        _dispatch(2, agent="B", handoff_state="blocked", to_agent="A"),
        _dispatch(3, agent="B", handoff_state="DONE"),  # B recovers
    ]
    reports = detect_deadlock(events)
    assert reports == []


def test_two_disjoint_deadlocks_two_reports():
    """Two independent cycles in the same stream → two distinct reports."""
    events = [
        _dispatch(1, agent="A", handoff_state="blocked", to_agent="B"),
        _dispatch(2, agent="B", handoff_state="blocked", to_agent="A"),
        _dispatch(3, agent="C", handoff_state="blocked", to_agent="D"),
        _dispatch(4, agent="D", handoff_state="blocked", to_agent="C"),
    ]
    reports = detect_deadlock(events)
    assert len(reports) == 2
    # Order is deterministic based on cycle discovery
    sigs = {r.signature for r in reports}
    assert sigs == {("A", "B"), ("C", "D")}
    # Both have prevented_cost=0.0
    for report in reports:
        assert report.prevented_cost == 0.0
        assert report.prevented_runs == 0


# ---------------------------------------------------------------------------
# ERROR HANDLING
# ---------------------------------------------------------------------------


def test_non_blocked_token_no_crash():
    """handoff_state tokens not in blocked_states classify as non-blocked, no crash."""
    events = [
        _dispatch(1, agent="A", handoff_state="garbage", to_agent="B"),
        _dispatch(2, agent="B", handoff_state="in_progress", to_agent="A"),
    ]
    # Neither bare token is in the default blocked_states {"blocked", "waiting"},
    # so the blocked map is empty → [] (no crash).
    reports = detect_deadlock(events)
    assert reports == []


def test_blocked_no_target_is_dead_end_node():
    """'blocked' with to_agent=None → dead-end node (no outgoing edge)."""
    events = [
        _dispatch(1, agent="A", handoff_state="blocked", to_agent="B"),
        _dispatch(2, agent="B", handoff_state="blocked", to_agent=None),  # no target
    ]
    # A blocks on B; B blocks but names no target.
    # B's edge is None (dead-end) → A has no cycle → no report.
    reports = detect_deadlock(events)
    assert reports == []


# ---------------------------------------------------------------------------
# CASE-INSENSITIVE BLOCKED-STATE MATCHING
# ---------------------------------------------------------------------------


def test_case_insensitive_blocked_token_matching():
    """'BLOCKED' (uppercase) matches the default blocked_states (lowercase 'blocked')."""
    events = [
        _dispatch(1, agent="A", handoff_state="BLOCKED", to_agent="B"),
        _dispatch(2, agent="B", handoff_state="BLOCKED", to_agent="A"),
    ]
    reports = detect_deadlock(events)
    assert len(reports) == 1
    assert reports[0].kind == KIND_DEADLOCK


def test_waiting_token_matches_default_blocked_states():
    """'waiting' is in the default blocked_states."""
    events = [
        _dispatch(1, agent="A", handoff_state="waiting", to_agent="B"),
        _dispatch(2, agent="B", handoff_state="waiting", to_agent="A"),
    ]
    reports = detect_deadlock(events)
    assert len(reports) == 1


def test_custom_blocked_states_config():
    """Custom blocked_states configuration recognized (e.g. 'stalled')."""
    events = [
        _dispatch(1, agent="A", handoff_state="stalled", to_agent="B"),
        _dispatch(2, agent="B", handoff_state="stalled", to_agent="A"),
    ]
    cfg = DetectionConfig(blocked_states=frozenset({"stalled"}))
    reports = detect_deadlock(events, config=cfg)
    assert len(reports) == 1


# ---------------------------------------------------------------------------
# PREVENTED_COST AND PREVENTED_RUNS
# ---------------------------------------------------------------------------


def test_prevented_cost_always_zero_for_deadlock():
    """Deadlock prevented_cost=0.0 (wall-clock hang, not recurring spend)."""
    events = [
        _dispatch(1, agent="A", handoff_state="blocked", to_agent="B", cost_usd=100.0),
        _dispatch(2, agent="B", handoff_state="blocked", to_agent="A", cost_usd=200.0),
    ]
    reports = detect_deadlock(events)
    assert len(reports) == 1
    assert reports[0].prevented_cost == 0.0


def test_prevented_runs_always_zero_for_deadlock():
    """Deadlock prevented_runs=0 (blocked agents not actively running)."""
    events = [
        _dispatch(1, agent="A", handoff_state="blocked", to_agent="B"),
        _dispatch(2, agent="B", handoff_state="blocked", to_agent="A"),
    ]
    reports = detect_deadlock(events)
    assert len(reports) == 1
    assert reports[0].prevented_runs == 0


def test_prevented_cost_with_math_isclose_precision():
    """Prevented costs use math.isclose for deterministic floating-point comparison."""
    events = [
        _dispatch(1, agent="A", handoff_state="blocked", to_agent="B", cost_usd=0.0),
        _dispatch(2, agent="B", handoff_state="blocked", to_agent="A", cost_usd=0.0),
    ]
    reports = detect_deadlock(events)
    assert len(reports) == 1
    assert math.isclose(reports[0].prevented_cost, 0.0, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# CONFIGURATION VALIDATION
# ---------------------------------------------------------------------------


def test_min_cycle_len_config():
    """min_cycle_len filters cycles shorter than the threshold.

    Default is 2, but can be overridden. A 1-node self-cycle is always excluded
    by the graph-build phase (t != u), so min_cycle_len >= 2 is effectively
    the only observable case.
    """
    events = [
        _dispatch(1, agent="A", handoff_state="blocked", to_agent="B"),
        _dispatch(2, agent="B", handoff_state="blocked", to_agent="A"),
    ]
    # Default min_cycle_len=2 includes this 2-cycle.
    reports = detect_deadlock(events)
    assert len(reports) == 1

    # Even with min_cycle_len=3, a 2-cycle is filtered.
    cfg = DetectionConfig(min_cycle_len=3)
    reports = detect_deadlock(events, config=cfg)
    assert len(reports) == 0


def test_empty_stream_returns_empty_list():
    """Empty stream → no events → no cycles → []."""
    reports = detect_deadlock([])
    assert reports == []


def test_single_event_returns_empty_list():
    """Single event cannot form a cycle → []."""
    events = [_dispatch(1, agent="A", handoff_state="blocked", to_agent="B")]
    reports = detect_deadlock(events)
    assert reports == []


# ---------------------------------------------------------------------------
# HYPHENATED AGENT NAMES
# ---------------------------------------------------------------------------


def test_hyphenated_agent_names_in_cycle():
    """Agent names with hyphens (e.g. 'code-writer') work as explicit to_agent targets."""
    events = [
        _dispatch(1, agent="code-writer", handoff_state="blocked", to_agent="code-reviewer"),
        _dispatch(2, agent="code-reviewer", handoff_state="blocked", to_agent="code-writer"),
    ]
    reports = detect_deadlock(events)
    assert len(reports) == 1
    assert reports[0].signature == ("code-reviewer", "code-writer")
    assert "code-writer" in reports[0].blocked_agents
    assert "code-reviewer" in reports[0].blocked_agents


# ---------------------------------------------------------------------------
# EXPLICIT to_agent TARGET (replaces the retired packed-delimiter grammar)
# ---------------------------------------------------------------------------


def test_explicit_to_agent_target_forms_edge():
    """The wait-for edge is read directly from event.to_agent (no delimiter scan)."""
    events = [
        _dispatch(1, agent="A", handoff_state="blocked", to_agent="B"),
        _dispatch(2, agent="B", handoff_state="blocked", to_agent="A"),
    ]
    reports = detect_deadlock(events)
    assert len(reports) == 1
    assert reports[0].signature == ("A", "B")


# ---------------------------------------------------------------------------
# FROZEN AND HASHABLE REPORTS
# ---------------------------------------------------------------------------


def test_pathology_report_is_frozen():
    """PathologyReport is frozen (@dataclass(frozen=True, slots=True))."""
    events = [
        _dispatch(1, agent="A", handoff_state="blocked", to_agent="B"),
        _dispatch(2, agent="B", handoff_state="blocked", to_agent="A"),
    ]
    reports = detect_deadlock(events)
    report = reports[0]
    # Attempting to reassign a field raises FrozenInstanceError.
    try:
        report.prevented_cost = 999.0
        assert False, "Expected FrozenInstanceError"
    except Exception as e:
        assert "frozen" in str(e).lower() or "FrozenInstanceError" in type(e).__name__


def test_blocked_agents_is_frozenset():
    """blocked_agents is a frozenset (immutable and hashable)."""
    events = [
        _dispatch(1, agent="A", handoff_state="blocked", to_agent="B"),
        _dispatch(2, agent="B", handoff_state="blocked", to_agent="A"),
    ]
    reports = detect_deadlock(events)
    report = reports[0]
    assert isinstance(report.blocked_agents, frozenset)
    assert report.blocked_agents == frozenset({"A", "B"})


# ---------------------------------------------------------------------------
# LARGE CYCLES
# ---------------------------------------------------------------------------


def test_four_cycle_deadlock():
    """A→B→C→D→A all blocked → one deadlock report."""
    events = [
        _dispatch(1, agent="A", handoff_state="blocked", to_agent="B"),
        _dispatch(2, agent="B", handoff_state="blocked", to_agent="C"),
        _dispatch(3, agent="C", handoff_state="blocked", to_agent="D"),
        _dispatch(4, agent="D", handoff_state="blocked", to_agent="A"),
    ]
    reports = detect_deadlock(events)
    assert len(reports) == 1
    assert len(reports[0].blocked_agents) == 4
    assert reports[0].occurrences == 4


def test_five_cycle_deadlock():
    """A→B→C→D→E→A all blocked."""
    events = [
        _dispatch(1, agent="A", handoff_state="blocked", to_agent="B"),
        _dispatch(2, agent="B", handoff_state="blocked", to_agent="C"),
        _dispatch(3, agent="C", handoff_state="blocked", to_agent="D"),
        _dispatch(4, agent="D", handoff_state="blocked", to_agent="E"),
        _dispatch(5, agent="E", handoff_state="blocked", to_agent="A"),
    ]
    reports = detect_deadlock(events)
    assert len(reports) == 1
    assert len(reports[0].blocked_agents) == 5


# ---------------------------------------------------------------------------
# UPPERCASE BLOCKED TOKEN — case-insensitive blocked_states membership
# ---------------------------------------------------------------------------
# Under the explicit-to_agent contract, handoff_state carries only the bare
# state token.  An uppercase "BLOCKED" token must still match the default
# lowercase blocked_states set (case-insensitive on both sides), so a deadlock
# built from uppercase handoff states remains visible.


def test_uppercase_blocked_token_2_cycle_deadlock():
    """2-cycle deadlock with an uppercase 'BLOCKED' token is detected.

    Locks case-insensitive blocked_states membership: handoff_state="BLOCKED"
    matches the default lowercase 'blocked', and the edge comes from the
    explicit to_agent field.
    """
    events = [
        _dispatch(1, agent="A", handoff_state="BLOCKED", to_agent="B"),
        _dispatch(2, agent="B", handoff_state="BLOCKED", to_agent="A"),
    ]
    reports = detect_deadlock(events)
    assert len(reports) == 1
    report = reports[0]
    assert report.kind == KIND_DEADLOCK
    assert report.blocked_agents == frozenset({"A", "B"})
