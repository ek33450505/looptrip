"""Integration tests for Phase 2 detector: backward-compat lock and multi-detector surface.

Tests the integration of Phase 2's new detectors (ping_pong, deadlock,
non_termination) with the existing duplicate_work detector, ensuring:

1. All legacy imports from looptrip.detector still resolve
2. detect() default (duplicate-work-only) is unchanged
3. detect_all() and detectors= parameter enable the new detectors
4. Combined output sorts by prevented_cost DESC
5. PathologyReport new fields default correctly
6. proof.py results are unchanged (792.96, trip raw_ids 555/1080)
"""

from __future__ import annotations

import math
from looptrip.detector import (
    detect,
    detect_all,
    detect_duplicate_work,
    detect_ping_pong,
    detect_deadlock,
    detect_non_termination,
    PathologyReport,
    DetectionConfig,
    KIND_DUPLICATE_WORK,
    KIND_PING_PONG,
    KIND_DEADLOCK,
    KIND_NON_TERMINATION,
    ALL_DETECTORS,
    _args_similar,
)
from looptrip.normalize import Event
from looptrip.proof import run_proof


def _ev(
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
    """Helper to build test events (cast.db style)."""
    return Event(
        agent=agent,
        tool=tool,
        args_hash=args_hash,
        ts=ts or f"2026-06-21T00:00:{raw_id:02d}Z",
        input_tokens=input_tokens,
        cost_usd=cost_usd,
        progress=progress,
        raw_id=raw_id,
        handoff_state=handoff_state,
    )


# ---------------------------------------------------------------------------
# LEGACY IMPORTS: enumerate every export from looptrip.detector
# ---------------------------------------------------------------------------

def test_detect_importable_from_looptrip_detector():
    """detect function is importable and callable."""
    assert callable(detect)


def test_detect_all_importable_from_looptrip_detector():
    """detect_all function is importable and callable."""
    assert callable(detect_all)


def test_detect_duplicate_work_importable_from_looptrip_detector():
    """detect_duplicate_work function is importable and callable."""
    assert callable(detect_duplicate_work)


def test_detect_ping_pong_importable_from_looptrip_detector():
    """detect_ping_pong function is importable and callable."""
    assert callable(detect_ping_pong)


def test_detect_deadlock_importable_from_looptrip_detector():
    """detect_deadlock function is importable and callable."""
    assert callable(detect_deadlock)


def test_detect_non_termination_importable_from_looptrip_detector():
    """detect_non_termination function is importable and callable."""
    assert callable(detect_non_termination)


def test_pathology_report_importable_from_looptrip_detector():
    """PathologyReport class is importable and callable."""
    assert callable(PathologyReport)


def test_detection_config_importable_from_looptrip_detector():
    """DetectionConfig class is importable and callable."""
    assert callable(DetectionConfig)


def test_kind_duplicate_work_importable_from_looptrip_detector():
    """KIND_DUPLICATE_WORK constant is importable."""
    assert KIND_DUPLICATE_WORK == "duplicate_work"


def test_kind_ping_pong_importable_from_looptrip_detector():
    """KIND_PING_PONG constant is importable."""
    assert KIND_PING_PONG == "ping_pong"


def test_kind_deadlock_importable_from_looptrip_detector():
    """KIND_DEADLOCK constant is importable."""
    assert KIND_DEADLOCK == "deadlock"


def test_kind_non_termination_importable_from_looptrip_detector():
    """KIND_NON_TERMINATION constant is importable."""
    assert KIND_NON_TERMINATION == "non_termination"


def test_all_detectors_importable_from_looptrip_detector():
    """ALL_DETECTORS tuple is importable and has all four kinds."""
    assert ALL_DETECTORS == (
        KIND_DUPLICATE_WORK,
        KIND_PING_PONG,
        KIND_DEADLOCK,
        KIND_NON_TERMINATION,
    )


def test_args_similar_importable_from_looptrip_detector():
    """_args_similar function is importable and callable."""
    assert callable(_args_similar)


# ---------------------------------------------------------------------------
# BACKWARD-COMPAT: detect() default is duplicate-work-only
# ---------------------------------------------------------------------------

def test_detect_default_is_duplicate_work_only():
    """detect(stream) with no detectors= param returns only duplicate_work reports."""
    events = [
        _ev(1, agent="a", input_tokens=1000, cost_usd=10.0),
        _ev(2, agent="a", input_tokens=1010, cost_usd=11.0),
        _ev(3, agent="a", input_tokens=1000, cost_usd=12.0),
    ]
    reports = detect(events)
    assert len(reports) == 1
    assert reports[0].kind == KIND_DUPLICATE_WORK
    assert reports[0].agent == "a"
    assert reports[0].occurrences == 3


def test_detect_default_matches_detect_duplicate_work():
    """detect(stream) bit-equals sorted(detect_duplicate_work(stream), prevented_cost DESC)."""
    events = [
        _ev(1, agent="a", input_tokens=1000, cost_usd=5.0),
        _ev(2, agent="a", input_tokens=1010, cost_usd=6.0),
        _ev(3, agent="a", input_tokens=1000, cost_usd=7.0),
        _ev(4, agent="b", input_tokens=2000, cost_usd=20.0),
        _ev(5, agent="b", input_tokens=2050, cost_usd=21.0),
        _ev(6, agent="b", input_tokens=2000, cost_usd=22.0),
    ]
    from_detect = detect(events)
    from_dup = sorted(detect_duplicate_work(events), key=lambda r: r.prevented_cost, reverse=True)

    assert len(from_detect) == len(from_dup)
    for d, dup in zip(from_detect, from_dup):
        assert d.kind == dup.kind
        assert d.agent == dup.agent
        assert d.trip_event.raw_id == dup.trip_event.raw_id
        assert math.isclose(d.prevented_cost, dup.prevented_cost, abs_tol=0.01)


def test_detect_with_legacy_knobs_forward_to_duplicate_work():
    """detect(stream, threshold=3, token_tolerance=0.1, idempotent_agents=...) forwards to detect_duplicate_work."""
    events = [
        _ev(1, agent="a", input_tokens=1000),
        _ev(2, agent="a", input_tokens=1100),  # +10%
        _ev(3, agent="a", input_tokens=1200),  # +9%
        _ev(4, agent="a", input_tokens=1300),  # +8%
    ]
    # With default 5% tolerance, no trip. With 10% tolerance, trip at 2nd occurrence.
    reports_tight = detect(events, token_tolerance=0.05)
    assert len(reports_tight) == 0

    reports_loose = detect(events, token_tolerance=0.10)
    assert len(reports_loose) == 1
    # Trip at event 2 (2nd occurrence, within 10% of event 1's 1000 tokens)
    assert reports_loose[0].trip_event.raw_id == 2


def test_detect_threshold_3_delays_trip():
    """detect(stream, threshold=3) trips at 3rd occurrence, not 2nd."""
    events = [
        _ev(1, agent="a", input_tokens=1000),
        _ev(2, agent="a", input_tokens=1010),
        _ev(3, agent="a", input_tokens=1020),
    ]
    reports = detect(events, threshold=3)
    assert len(reports) == 1
    assert reports[0].trip_event.raw_id == 3


def test_detect_idempotent_agents_suppresses_report():
    """detect(stream, idempotent_agents={"a"}) suppresses report for agent "a"."""
    events = [
        _ev(1, agent="a", input_tokens=1000),
        _ev(2, agent="a", input_tokens=1010),
    ]
    reports = detect(events, idempotent_agents=frozenset({"a"}))
    assert len(reports) == 0


# ---------------------------------------------------------------------------
# INTERLEAVED STREAM: detect() stays duplicate-work-only (locks default)
# ---------------------------------------------------------------------------

def test_interleaved_stream_detect_returns_only_duplicate_work():
    """The interleaved c,p,c,p,c,p stream in test_detector_adversarial.py:375
    contains a genuine 2-cycle (code-reviewer <-> code-writer) closed by ping_pong.
    LOCKED: detect() MUST return ONLY the 2 duplicate_work reports, NOT the ping_pong.
    This is the backward-compat assertion that keeps proof.py unaffected.
    """
    c, p = "code-reviewer", "code-writer"
    stream = [
        _ev(1, agent=c, cost_usd=10.0),
        _ev(2, agent=p, cost_usd=11.0),
        _ev(3, agent=c, cost_usd=12.0),
        _ev(4, agent=p, cost_usd=13.0),
        _ev(5, agent=c, cost_usd=14.0),
        _ev(6, agent=p, cost_usd=15.0),
    ]
    reports = detect(stream)

    # Must be exactly 2 duplicate_work reports (c and p each trip at 3rd occurrence)
    assert len(reports) == 2
    assert all(r.kind == KIND_DUPLICATE_WORK for r in reports)

    # Sorted by prevented_cost DESC
    by_agent = {r.agent: r for r in reports}
    assert c in by_agent and p in by_agent


def test_interleaved_stream_detect_all_includes_ping_pong():
    """The same interleaved stream with detect_all() includes the ping_pong report."""
    c, p = "code-reviewer", "code-writer"
    stream = [
        _ev(1, agent=c, cost_usd=10.0),
        _ev(2, agent=p, cost_usd=11.0),
        _ev(3, agent=c, cost_usd=12.0),
        _ev(4, agent=p, cost_usd=13.0),
        _ev(5, agent=c, cost_usd=14.0),
        _ev(6, agent=p, cost_usd=15.0),
    ]
    reports = detect_all(stream)

    # Must include the 2 duplicate_work + 1 ping_pong = 3 total
    kinds = [r.kind for r in reports]
    assert kinds.count(KIND_DUPLICATE_WORK) == 2
    assert kinds.count(KIND_PING_PONG) == 1
    assert len(reports) == 3


def test_interleaved_stream_combined_output_sorted_by_prevented_cost():
    """detect_all() output is sorted by prevented_cost DESC."""
    c, p = "code-reviewer", "code-writer"
    stream = [
        _ev(1, agent=c, cost_usd=100.0),  # code-reviewer expensive
        _ev(2, agent=p, cost_usd=1.0),    # code-writer cheap
        _ev(3, agent=c, cost_usd=101.0),
        _ev(4, agent=p, cost_usd=2.0),
        _ev(5, agent=c, cost_usd=102.0),
        _ev(6, agent=p, cost_usd=3.0),
    ]
    reports = detect_all(stream)

    # Verify sorted DESC
    costs = [r.prevented_cost for r in reports]
    assert costs == sorted(costs, reverse=True)

    # First report should be code-reviewer (highest cost)
    assert reports[0].agent == c


# ---------------------------------------------------------------------------
# PATHOLOGY_REPORT: new fields default correctly (frozen contract preserved)
# ---------------------------------------------------------------------------

def test_pathology_report_is_frozen():
    """PathologyReport is frozen (immutable)."""
    report = _ev(1)  # dummy event
    report = PathologyReport(
        kind=KIND_DUPLICATE_WORK,
        signature=(report.agent, report.tool, report.args_hash),
        agent=report.agent,
        occurrences=1,
        trip_index=1,
        trip_event=report,
        first_event=report,
        prevented_cost=0.0,
        prevented_runs=0,
        detail="",
    )
    from dataclasses import FrozenInstanceError
    try:
        report.kind = "changed"
        assert False, "Should have raised FrozenInstanceError"
    except FrozenInstanceError:
        pass


def test_pathology_report_new_fields_default_to_none_or_empty():
    """The 3 new fields (members, blocked_agents, window) default to (),
    None, None respectively, preserving backward compat with legacy 10-field
    construction."""
    report = _ev(1)
    report = PathologyReport(
        kind=KIND_DUPLICATE_WORK,
        signature=(report.agent, report.tool, report.args_hash),
        agent=report.agent,
        occurrences=1,
        trip_index=1,
        trip_event=report,
        first_event=report,
        prevented_cost=0.0,
        prevented_runs=0,
        detail="",
    )
    assert report.members == ()
    assert report.blocked_agents is None
    assert report.window is None


def test_pathology_report_can_set_new_fields():
    """New fields can be set via keyword construction."""
    report = _ev(1)
    members = ("a", "b")
    report = PathologyReport(
        kind=KIND_PING_PONG,
        signature=members,
        agent=report.agent,
        occurrences=2,
        trip_index=2,
        trip_event=report,
        first_event=report,
        prevented_cost=10.0,
        prevented_runs=1,
        detail="",
        members=members,
        blocked_agents=None,
        window=None,
    )
    assert report.members == members
    assert report.blocked_agents is None
    assert report.window is None


# ---------------------------------------------------------------------------
# PROOF.PY INVARIANT: run_proof() == 792.96, trip raw_ids 555/1080
# ---------------------------------------------------------------------------

def test_run_proof_grand_total_unchanged():
    """run_proof() still returns grand_total_saved == 792.96 within $0.01."""
    result = run_proof()
    assert abs(result["grand_total_saved"] - 792.96) < 0.01


def test_run_proof_trip_raw_ids_unchanged():
    """run_proof() identifies the same trip points: 555 and 1080."""
    result = run_proof()
    sessions = result["sessions"]

    # Find the two session entries
    trip_ids = {entry["trip_dispatch_raw_id"] for entry in sessions}
    assert 555 in trip_ids, "Expected trip raw_id 555 in proof fixture"
    assert 1080 in trip_ids, "Expected trip raw_id 1080 in proof fixture"


# ---------------------------------------------------------------------------
# DETECT_ALL() and DETECTORS= parameter
# ---------------------------------------------------------------------------

def test_detect_with_detectors_none_equals_duplicate_work_only():
    """detect(..., detectors=None) uses default (duplicate-work-only)."""
    events = [
        _ev(1, agent="a", input_tokens=1000),
        _ev(2, agent="a", input_tokens=1010),
    ]
    reports_default = detect(events)
    reports_explicit = detect(events, detectors=None)

    assert len(reports_default) == len(reports_explicit)
    assert reports_default[0].kind == reports_explicit[0].kind


def test_detect_with_detectors_duplicate_work_only():
    """detect(..., detectors=(KIND_DUPLICATE_WORK,)) uses only duplicate_work."""
    events = [
        _ev(1, agent="a", input_tokens=1000),
        _ev(2, agent="a", input_tokens=1010),
    ]
    reports = detect(events, detectors=(KIND_DUPLICATE_WORK,))
    assert len(reports) == 1
    assert reports[0].kind == KIND_DUPLICATE_WORK


def test_detect_with_detectors_all_detectors():
    """detect(..., detectors=ALL_DETECTORS) runs all four detectors."""
    # Ping-pong stream
    a, b = "a", "b"
    events = [
        _ev(1, agent=a, input_tokens=1000),
        _ev(2, agent=b, input_tokens=1000),
        _ev(3, agent=a, input_tokens=1000),
        _ev(4, agent=b, input_tokens=1000),
        _ev(5, agent=a, input_tokens=1000),
    ]
    reports = detect(events, detectors=ALL_DETECTORS)

    # Should have at least duplicate_work (on a and b) and ping_pong (on (a,b))
    kinds = [r.kind for r in reports]
    assert KIND_DUPLICATE_WORK in kinds
    assert KIND_PING_PONG in kinds


def test_detect_all_convenience():
    """detect_all(stream) is a convenience for detect(..., detectors=ALL_DETECTORS)."""
    events = [
        _ev(1, agent="a", input_tokens=1000),
        _ev(2, agent="b", input_tokens=1000),
        _ev(3, agent="a", input_tokens=1000),
        _ev(4, agent="b", input_tokens=1000),
        _ev(5, agent="a", input_tokens=1000),
    ]
    reports_all = detect_all(events)
    reports_explicit = detect(events, detectors=ALL_DETECTORS)

    assert len(reports_all) == len(reports_explicit)
    for r1, r2 in zip(reports_all, reports_explicit):
        assert r1.kind == r2.kind
        assert r1.agent == r2.agent


# ---------------------------------------------------------------------------
# COMBINED OUTPUT SORTING
# ---------------------------------------------------------------------------

def test_detect_all_sorts_by_prevented_cost_desc():
    """detect_all() output is sorted by prevented_cost DESC."""
    events = [
        _ev(1, agent="expensive", cost_usd=1000.0),
        _ev(2, agent="expensive", cost_usd=1000.0),
        _ev(3, agent="cheap", cost_usd=1.0),
        _ev(4, agent="cheap", cost_usd=1.0),
    ]
    reports = detect_all(events)

    # Verify sorted DESC
    costs = [r.prevented_cost for r in reports]
    assert costs == sorted(costs, reverse=True)


def test_detect_cost_sum_deterministic():
    """Prevented cost calculations are deterministic (use math.isclose for assertions)."""
    events = [
        _ev(1, cost_usd=10.33),
        _ev(2, cost_usd=10.33),
        _ev(3, cost_usd=10.33),
    ]
    reports = detect(events)
    assert len(reports) == 1
    # Three events, first two in baseline, so prevented_cost = cost of event 3 = 10.33
    assert math.isclose(reports[0].prevented_cost, 10.33, abs_tol=0.01)


# ---------------------------------------------------------------------------
# MATERIALIZATION: detect() does not mutate or re-sort caller's stream
# ---------------------------------------------------------------------------

def test_detect_materializes_stream_preserving_order():
    """detect() materializes the input stream once and feeds it to each detector,
    preserving order and Event identity."""
    events = [
        _ev(1, agent="a"),
        _ev(2, agent="b"),
        _ev(3, agent="a"),
    ]
    snapshot = list(events)

    reports = detect(events)

    # Verify stream unchanged
    assert events == snapshot
    assert [e.raw_id for e in events] == [1, 2, 3]


# ---------------------------------------------------------------------------
# RESOLUTION OF CONFIG AND KNOBS
# ---------------------------------------------------------------------------

def test_detect_rejects_unknown_knob():
    """detect(stream, unknown_knob=True) raises TypeError."""
    events = [_ev(1)]
    try:
        detect(events, unknown_knob=True)
        assert False, "Should have raised TypeError"
    except TypeError as e:
        assert "unexpected" in str(e).lower() or "knob" in str(e).lower()


def test_detect_config_param_is_used():
    """detect(stream, config=DetectionConfig(...)) uses the provided config."""
    events = [
        _ev(1, agent="a", input_tokens=1000),
        _ev(2, agent="a", input_tokens=2000),  # +100% variance > 5%
    ]
    # Default tolerance 5% -> no trip
    reports_default = detect(events)
    assert len(reports_default) == 0

    # Custom config with 110% tolerance -> trip
    config = DetectionConfig(token_tolerance=1.1)
    reports_custom = detect(events, config=config)
    assert len(reports_custom) == 1


def test_detect_knobs_override_config():
    """detect(stream, config=cfg, token_tolerance=0.2) overrides cfg.token_tolerance."""
    events = [
        _ev(1, agent="a", input_tokens=1000),
        _ev(2, agent="a", input_tokens=1150),  # +15% variance
    ]
    config = DetectionConfig(token_tolerance=0.05)  # Tight

    # Knob overrides -> 50% tolerance
    reports = detect(events, config=config, token_tolerance=0.50)
    assert len(reports) == 1


# ---------------------------------------------------------------------------
# NEW DETECTORS ARE CALLABLE
# ---------------------------------------------------------------------------

def test_detect_ping_pong_callable():
    """detect_ping_pong(...) can be called directly."""
    events = [
        _ev(1, agent="a"),
        _ev(2, agent="b"),
        _ev(3, agent="a"),
        _ev(4, agent="b"),
        _ev(5, agent="a"),
    ]
    reports = detect_ping_pong(events)
    # At least the ping-pong should fire
    assert any(r.kind == KIND_PING_PONG for r in reports)


def test_detect_deadlock_callable():
    """detect_deadlock(...) can be called directly."""
    events = [
        _ev(1, agent="a", handoff_state="blocked: b"),
        _ev(2, agent="b", handoff_state="blocked: a"),
    ]
    reports = detect_deadlock(events)
    # May fire if parse succeeds (depends on _parse_blocked logic)
    assert isinstance(reports, list)


def test_detect_non_termination_callable():
    """detect_non_termination(...) can be called directly."""
    # 25 events, same signature -> should fire with window_size=20
    events = [_ev(i, agent="loop") for i in range(1, 26)]
    reports = detect_non_termination(events)
    # Should have at least one report for the plateau
    assert any(r.kind == KIND_NON_TERMINATION for r in reports)


# ---------------------------------------------------------------------------
# IMPORTS: verify re-exports work correctly
# ---------------------------------------------------------------------------

def test_all_exports_have_correct_values():
    """Verify that all re-exported names have the expected types/values."""
    assert callable(detect)
    assert callable(detect_all)
    assert callable(detect_duplicate_work)
    assert callable(detect_ping_pong)
    assert callable(detect_deadlock)
    assert callable(detect_non_termination)

    assert callable(PathologyReport)
    assert callable(DetectionConfig)

    assert isinstance(KIND_DUPLICATE_WORK, str)
    assert isinstance(KIND_PING_PONG, str)
    assert isinstance(KIND_DEADLOCK, str)
    assert isinstance(KIND_NON_TERMINATION, str)

    assert isinstance(ALL_DETECTORS, tuple)
    assert len(ALL_DETECTORS) == 4

    assert callable(_args_similar)


# ---------------------------------------------------------------------------
# MUTATION-TESTING GAP: new-detector knobs must NOT suppress duplicate-work
# ---------------------------------------------------------------------------

def test_duplicate_work_baseline_trips_with_default_config():
    """Baseline: a clear single-agent duplicate-work runaway trips under detect()."""
    events = [
        _ev(1, agent="looper", tool="dispatch", args_hash=None, input_tokens=1000, cost_usd=5.0),
        _ev(2, agent="looper", tool="dispatch", args_hash=None, input_tokens=1050, cost_usd=6.0),
        _ev(3, agent="looper", tool="dispatch", args_hash=None, input_tokens=1000, cost_usd=7.0),
    ]
    reports = detect(events)

    assert len(reports) == 1
    assert reports[0].kind == KIND_DUPLICATE_WORK
    assert reports[0].agent == "looper"
    assert reports[0].trip_index == 2
    assert reports[0].occurrences == 3


def test_duplicate_work_still_trips_with_retry_allowed_naming_agent():
    """retry_allowed DOES NOT suppress duplicate-work: agent in retry_allowed still trips."""
    events = [
        _ev(1, agent="looper", tool="dispatch", args_hash=None, input_tokens=1000, cost_usd=5.0),
        _ev(2, agent="looper", tool="dispatch", args_hash=None, input_tokens=1050, cost_usd=6.0),
        _ev(3, agent="looper", tool="dispatch", args_hash=None, input_tokens=1000, cost_usd=7.0),
    ]

    # Pass retry_allowed naming the agent
    reports = detect(events, retry_allowed=frozenset({"looper"}))

    # Must still trip (retry_allowed only affects new detectors, not duplicate-work)
    assert len(reports) == 1
    assert reports[0].kind == KIND_DUPLICATE_WORK
    assert reports[0].agent == "looper"


def test_duplicate_work_still_trips_with_allowlist_agents_naming_agent():
    """allowlist_agents DOES NOT suppress duplicate-work: agent in allowlist_agents still trips."""
    events = [
        _ev(1, agent="looper", tool="dispatch", args_hash=None, input_tokens=1000, cost_usd=5.0),
        _ev(2, agent="looper", tool="dispatch", args_hash=None, input_tokens=1050, cost_usd=6.0),
        _ev(3, agent="looper", tool="dispatch", args_hash=None, input_tokens=1000, cost_usd=7.0),
    ]

    # Pass allowlist_agents naming the agent
    reports = detect(events, allowlist_agents=frozenset({"looper"}))

    # Must still trip (allowlist_agents only affects new detectors)
    assert len(reports) == 1
    assert reports[0].kind == KIND_DUPLICATE_WORK
    assert reports[0].agent == "looper"


def test_duplicate_work_still_trips_with_idempotent_tools_naming_tool():
    """idempotent_tools DOES NOT suppress duplicate-work: tool in idempotent_tools still trips."""
    events = [
        _ev(1, agent="looper", tool="dispatch", args_hash=None, input_tokens=1000, cost_usd=5.0),
        _ev(2, agent="looper", tool="dispatch", args_hash=None, input_tokens=1050, cost_usd=6.0),
        _ev(3, agent="looper", tool="dispatch", args_hash=None, input_tokens=1000, cost_usd=7.0),
    ]

    # Pass idempotent_tools naming the tool
    reports = detect(events, idempotent_tools=frozenset({"dispatch"}))

    # Must still trip (idempotent_tools only affects new detectors)
    assert len(reports) == 1
    assert reports[0].kind == KIND_DUPLICATE_WORK
    assert reports[0].agent == "looper"


def test_duplicate_work_still_trips_with_allowlist_tools_naming_tool():
    """allowlist_tools DOES NOT suppress duplicate-work: tool in allowlist_tools still trips."""
    events = [
        _ev(1, agent="looper", tool="dispatch", args_hash=None, input_tokens=1000, cost_usd=5.0),
        _ev(2, agent="looper", tool="dispatch", args_hash=None, input_tokens=1050, cost_usd=6.0),
        _ev(3, agent="looper", tool="dispatch", args_hash=None, input_tokens=1000, cost_usd=7.0),
    ]

    # Pass allowlist_tools naming the tool
    reports = detect(events, allowlist_tools=frozenset({"dispatch"}))

    # Must still trip (allowlist_tools only affects new detectors)
    assert len(reports) == 1
    assert reports[0].kind == KIND_DUPLICATE_WORK
    assert reports[0].agent == "looper"


def test_duplicate_work_still_trips_via_detect_all_with_retry_allowed():
    """detect_all with retry_allowed still includes duplicate-work report."""
    events = [
        _ev(1, agent="looper", tool="dispatch", args_hash=None, input_tokens=1000, cost_usd=5.0),
        _ev(2, agent="looper", tool="dispatch", args_hash=None, input_tokens=1050, cost_usd=6.0),
        _ev(3, agent="looper", tool="dispatch", args_hash=None, input_tokens=1000, cost_usd=7.0),
    ]

    # Use detect_all with retry_allowed naming the agent
    reports = detect_all(events, retry_allowed=frozenset({"looper"}))

    # Must have at least one duplicate-work report (one or more new-detector reports may also be present)
    dup_work = [r for r in reports if r.kind == KIND_DUPLICATE_WORK]
    assert len(dup_work) == 1
    assert dup_work[0].agent == "looper"
    assert dup_work[0].trip_event.raw_id == 2  # Trips at 2nd occurrence


def test_duplicate_work_ONLY_suppressed_by_idempotent_agents():
    """CONTROL: idempotent_agents IS the sole knob that suppresses duplicate-work for an agent."""
    events = [
        _ev(1, agent="looper", tool="dispatch", args_hash=None, input_tokens=1000, cost_usd=5.0),
        _ev(2, agent="looper", tool="dispatch", args_hash=None, input_tokens=1050, cost_usd=6.0),
        _ev(3, agent="looper", tool="dispatch", args_hash=None, input_tokens=1000, cost_usd=7.0),
    ]

    # Pass idempotent_agents naming the agent
    reports = detect(events, idempotent_agents=frozenset({"looper"}))

    # Must NOT trip (idempotent_agents suppresses duplicate-work)
    dup_work = [r for r in reports if r.kind == KIND_DUPLICATE_WORK and r.agent == "looper"]
    assert len(dup_work) == 0


def test_duplicate_work_combined_new_knobs_still_trip():
    """Multiple new-detector knobs together still do not suppress duplicate-work."""
    events = [
        _ev(1, agent="looper", tool="dispatch", args_hash=None, input_tokens=1000, cost_usd=5.0),
        _ev(2, agent="looper", tool="dispatch", args_hash=None, input_tokens=1050, cost_usd=6.0),
        _ev(3, agent="looper", tool="dispatch", args_hash=None, input_tokens=1000, cost_usd=7.0),
    ]

    # Pass all new-detector knobs together naming the agent/tool
    reports = detect(
        events,
        retry_allowed=frozenset({"looper"}),
        allowlist_agents=frozenset({"looper"}),
        idempotent_tools=frozenset({"dispatch"}),
        allowlist_tools=frozenset({"dispatch"}),
    )

    # Must still trip (none of these knobs suppress duplicate-work)
    assert len(reports) == 1
    assert reports[0].kind == KIND_DUPLICATE_WORK
    assert reports[0].agent == "looper"
