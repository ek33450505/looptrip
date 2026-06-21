"""Adversarial edge/error tests for the duplicate-work detector.

This file is the HARDENING companion to ``test_detector.py``. Every Event here
is constructed directly (no fixture, no adapter) so the state machine is
exercised in isolation. The goal is to PIN the detector's exact, currently-shipping
behavior at the boundaries the audit flagged: the inclusive token-tolerance gate,
the baseline-advances similarity chain, per-signature progress isolation, the
"kill-the-agent-at-trip" prevented-cost window, and the documented blind spots
(high-variance runaways, caller-supplied ordering).

Where the audit flagged a behavior as *questionable but shipping* (the high-variance
blind spot, the detail-string "N occurrences within X% variance" phrasing, the
threshold==1 trip_index), these tests lock the CURRENT behavior on purpose: any
future remediation must then change a test deliberately rather than drift silently.

All assertions were re-derived empirically against the committed detector before
being written down.
"""

from __future__ import annotations

from looptrip.detector import (
    KIND_DUPLICATE_WORK,
    _args_similar,
    detect,
    detect_duplicate_work,
)
from looptrip.normalize import Event

WORKFLOW = "workflow-subagent"


def _ev(
    raw_id: int,
    *,
    agent: str = WORKFLOW,
    tool: str = "dispatch",
    args_hash=None,
    input_tokens=1000,
    cost_usd: float = 10.0,
    progress: bool = False,
    ts: str | None = None,
) -> Event:
    """Build a cast.db-style dispatch Event (args_hash=None by default).

    ``ts`` defaults to a string derived from ``raw_id`` so a list built in
    raw_id order is also ts-sorted — but the detector iterates feed order, not
    ``ts``, which the unsorted-input tests exploit deliberately.
    """
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


# ===========================================================================
# Token-tolerance boundary — the inclusive '<=' gate is the entire cast.db
# trip signal, so it is pinned from both sides and for both directions.
# ===========================================================================

def test_variance_exactly_equal_to_tolerance_trips():
    """ratio == token_tolerance (1000 -> 1050 == 5.0%) is inclusive: it TRIPS."""
    reports = detect_duplicate_work([_ev(1, input_tokens=1000), _ev(2, input_tokens=1050)])
    assert len(reports) == 1
    assert reports[0].trip_event.raw_id == 2


def test_variance_just_under_tolerance_trips():
    """ratio just below tolerance (1000 -> 1040 == 4.0%) trips."""
    reports = detect_duplicate_work([_ev(1, input_tokens=1000), _ev(2, input_tokens=1040)])
    assert len(reports) == 1


def test_variance_just_over_tolerance_does_not_trip():
    """ratio just above tolerance (1000 -> 1051 == 5.1%) does NOT trip."""
    assert detect_duplicate_work([_ev(1, input_tokens=1000), _ev(2, input_tokens=1051)]) == []


def test_variance_is_measured_relative_to_the_baseline_event():
    """Variance divides by the BASELINE (prev), not cur — so it is asymmetric.

    1050 -> 1000 is 50/1050 == 4.76% (trips); 1000 -> 1050 is 50/1000 == 5.0%
    (trips, at the inclusive boundary). The shared 50-token gap yields two
    different ratios because the denominator is the earlier event.
    """
    assert len(detect_duplicate_work([_ev(1, input_tokens=1050), _ev(2, input_tokens=1000)])) == 1
    assert len(detect_duplicate_work([_ev(1, input_tokens=1000), _ev(2, input_tokens=1050)])) == 1


def test_zero_input_token_events_trip_via_divide_by_zero_guard():
    """0 -> 0 input tokens TRIPS: abs(0-0)/max(0,1) == 0.0 <= tolerance.

    Pins the ``max(prev.input_tokens, 1)`` divide-by-zero guard in _args_similar:
    two zero-token events are treated as identical work, not as a crash.
    """
    reports = detect_duplicate_work([_ev(1, input_tokens=0), _ev(2, input_tokens=0)])
    assert len(reports) == 1
    assert reports[0].trip_event.raw_id == 2


# ===========================================================================
# Baseline-advances similarity chain — sliding drift forms ONE chain even when
# end-to-end divergence exceeds tolerance; an all-pairs-above-tolerance stream
# is the documented Phase-1 blind spot.
# ===========================================================================

def test_sliding_window_drift_forms_one_chain():
    """+4%/step drift chains across 6 events though end-to-end divergence is 21.6%.

    Because the baseline advances to each accepted event, every CONSECUTIVE pair
    stays within 5% even though 1000 -> 1216 is 21.6% apart. Locks the
    baseline-advances semantic: occurrences==6, trip at #2, prevented_runs==4.
    """
    drift = [1000, 1040, 1082, 1125, 1170, 1216]
    events = [_ev(i + 1, input_tokens=t, cost_usd=10.0) for i, t in enumerate(drift)]
    reports = detect_duplicate_work(events)
    assert len(reports) == 1
    report = reports[0]
    assert report.occurrences == 6
    assert report.trip_index == 2
    assert report.trip_event.raw_id == 2
    assert report.prevented_runs == 4
    assert report.prevented_cost == 40.0  # events 3..6, 10.0 each


def test_high_variance_runaway_is_the_documented_blind_spot():
    """A same-signature runaway whose every consecutive pair exceeds tolerance
    returns [] — Phase 1 silently misses high-token-variance loops.

    This is the realistic LLM case (both fixture sessions show 32/53 and 42/48
    consecutive pairs breaking the 5% gate). Locking [] here makes any future
    move to an anchored/structural similarity signal a deliberate, tested change.
    """
    tokens = [1000, 2000, 1000, 2000, 1000]  # every step >= 50% apart
    events = [_ev(i + 1, input_tokens=t) for i, t in enumerate(tokens)]
    assert detect_duplicate_work(events) == []


# ===========================================================================
# threshold knob
# ===========================================================================

def test_threshold_delays_trip_to_the_nth_occurrence():
    """threshold=4 trips on the 4th occurrence; trip_index == threshold."""
    events = [_ev(i, cost_usd=10.0) for i in range(1, 7)]  # 6 occurrences
    reports = detect_duplicate_work(events, threshold=4)
    assert len(reports) == 1
    report = reports[0]
    assert report.trip_index == 4
    assert report.trip_event.raw_id == 4
    assert report.prevented_runs == 2  # ids 5,6 strictly after the trip


def test_threshold_one_cannot_trip_a_single_occurrence():
    """threshold=1 with a lone event still yields no report (nothing recurs)."""
    assert detect_duplicate_work([_ev(1)], threshold=1) == []


def test_threshold_one_trip_index_is_two_not_one():
    """threshold=1 trips at the 2nd event with trip_index==2, NOT 1.

    The trip check lives inside the recurrence branch, which the first event
    never enters. So the documented ``trip_index == threshold`` invariant does
    NOT hold for threshold==1: the earliest possible trip is occurrence 2. This
    test pins the current (audit-flagged) behavior so a future fix is explicit.
    """
    reports = detect_duplicate_work([_ev(1), _ev(2)], threshold=1)
    assert len(reports) == 1
    assert reports[0].trip_index == 2


# ===========================================================================
# idempotent_agents — monotonic suppression
# ===========================================================================

def test_idempotent_set_monotonically_suppresses_reports():
    """Adding an agent to idempotent_agents never INCREASES the report count and
    suppresses exactly that agent's signature, leaving others tripping.

    Stream has two independent runaways (agent-a, agent-b). The report count is
    monotone non-increasing as the suppression set grows: 2 -> 1 -> 0.
    """
    a, b = "agent-a", "agent-b"
    stream = [_ev(1, agent=a), _ev(2, agent=a), _ev(3, agent=b), _ev(4, agent=b)]

    none = detect_duplicate_work(stream)
    supp_a = detect_duplicate_work(stream, idempotent_agents=frozenset({a}))
    supp_b = detect_duplicate_work(stream, idempotent_agents=frozenset({b}))
    supp_ab = detect_duplicate_work(stream, idempotent_agents=frozenset({a, b}))

    # Monotonicity: count never rises as the set grows.
    assert len(none) == 2
    assert len(supp_a) == 1
    assert len(supp_b) == 1
    assert len(supp_ab) == 0
    assert len(supp_a) <= len(none)
    assert len(supp_ab) <= len(supp_a)

    # Exact suppression: only the named agent is silenced, the other survives.
    assert supp_a[0].agent == b
    assert supp_b[0].agent == a


# ===========================================================================
# progress delta — per-signature isolation, blocks/delays, accrues to cost
# ===========================================================================

def test_progress_between_recurrences_blocks_trip_control_trips_without_it():
    """A progress=True event between two would-be recurrences blocks the trip;
    the same stream WITHOUT the progress flag trips at occurrence 2."""
    blocked = [_ev(1), _ev(2, progress=True), _ev(3)]
    assert detect_duplicate_work(blocked) == []

    control = [_ev(1), _ev(2, progress=False), _ev(3)]
    reports = detect_duplicate_work(control)
    assert len(reports) == 1
    assert reports[0].trip_event.raw_id == 2


def test_progress_on_the_tripping_event_itself_blocks_the_trip():
    """If the 2nd occurrence is itself a progress delta, it is not a duplicate."""
    assert detect_duplicate_work([_ev(1), _ev(2, progress=True)]) == []


def test_progress_on_the_first_occurrence_blocks_the_immediate_recurrence():
    """A progress=True baseline (occurrence #1) blocks the next same-signature
    event from tripping — progress_since carries forward from the baseline."""
    assert detect_duplicate_work([_ev(1, progress=True), _ev(2)]) == []


def test_single_progress_blip_only_delays_it_does_not_immunize_the_signature():
    """[#1, #2(progress=True), #3, #4] re-trips at raw_id 4: a lone progress blip
    resets the chain once but the signature trips on the next clean pair."""
    reports = detect_duplicate_work([_ev(1), _ev(2, progress=True), _ev(3), _ev(4)])
    assert len(reports) == 1
    assert reports[0].trip_event.raw_id == 4


def test_progress_is_isolated_per_signature():
    """A progress=True event in signature B between two signature-A occurrences
    does NOT block signature A's trip (progress is tracked per signature)."""
    a, b = "agent-a", "agent-b"
    reports = detect_duplicate_work([_ev(1, agent=a), _ev(2, agent=b, progress=True), _ev(3, agent=a)])
    assert len(reports) == 1
    assert reports[0].agent == a
    assert reports[0].trip_event.raw_id == 3


# ===========================================================================
# args_hash exact-match path vs signature partitioning
# ===========================================================================

def test_exact_args_hash_match_trips_independent_of_input_tokens():
    """When both events carry an identical args_hash, the match is the trip
    signal regardless of input_tokens — even with input_tokens=None on both,
    and even with wildly divergent token counts."""
    none_tokens = detect_duplicate_work([
        _ev(1, args_hash="abc", input_tokens=None),
        _ev(2, args_hash="abc", input_tokens=None),
    ])
    assert len(none_tokens) == 1

    huge_token_gap = detect_duplicate_work([
        _ev(1, args_hash="abc", input_tokens=10),
        _ev(2, args_hash="abc", input_tokens=999_999),
    ])
    assert len(huge_token_gap) == 1


def test_differing_args_hash_never_trips_even_with_identical_tokens():
    """Different non-None args hashes are different SIGNATURES, so identical
    input_tokens cannot bring them together — they never trip."""
    events = [
        _ev(1, args_hash="abc", input_tokens=1000),
        _ev(2, args_hash="xyz", input_tokens=1000),
    ]
    assert detect_duplicate_work(events) == []


def test_no_args_hash_and_no_tokens_is_insufficient_signal_over_long_stream():
    """With neither args_hash nor input_tokens on any event there is no evidence
    of duplicate work — even a long all-None stream never trips."""
    events = [_ev(i, args_hash=None, input_tokens=None) for i in range(1, 6)]
    assert detect_duplicate_work(events) == []


# ===========================================================================
# prevented-cost window — the load-bearing 'kill-the-agent-at-trip' semantic
# ===========================================================================

def test_prevented_cost_counts_all_post_trip_members_across_chain_breaks():
    """prevented_cost/prevented_runs sum ALL same-signature members strictly
    after the trip event, INCLUDING ones that break the similarity chain
    (a progress delta, then a dissimilar-token event, then a fresh duplicate).

    Stream: [dup $10, dup(trip) $10, progress=True $50, dissimilar $80, dup $7].
    Everything after the trip is counted -> prevented_runs==3, cost==137.0. This
    pins the 'cost of every subsequent dispatch a loop-kill would avert' semantic
    that the headline $792.96 rests on.
    """
    events = [
        _ev(1, input_tokens=1000, cost_usd=10.0),
        _ev(2, input_tokens=1000, cost_usd=10.0),                  # trip
        _ev(3, input_tokens=1000, cost_usd=50.0, progress=True),   # chain break (progress)
        _ev(4, input_tokens=2000, cost_usd=80.0),                  # chain break (dissimilar)
        _ev(5, input_tokens=1000, cost_usd=7.0),                   # fresh duplicate
    ]
    reports = detect_duplicate_work(events)
    assert len(reports) == 1
    report = reports[0]
    assert report.trip_event.raw_id == 2
    assert report.prevented_runs == 3
    assert report.prevented_cost == 137.0
    assert report.occurrences == 5


def test_post_trip_none_cost_contributes_zero():
    """A None cost_usd among post-trip members counts as 0.0, not a crash."""
    events = [
        _ev(1, cost_usd=10.0),
        _ev(2, cost_usd=10.0),     # trip
        _ev(3, cost_usd=None),     # counts as 0.0
        _ev(4, cost_usd=5.0),
    ]
    report = detect_duplicate_work(events)[0]
    assert report.prevented_runs == 2
    assert report.prevented_cost == 5.0


# ===========================================================================
# Interleaved signatures — independent per-signature bookkeeping
# ===========================================================================

def test_interleaved_signatures_yield_two_independent_reports():
    """A,B,A,B,A,B in one stream produces two reports, each with its own trip
    point and prevented_runs — the signatures do not contaminate each other."""
    a, b = "agent-a", "agent-b"
    stream = [
        _ev(1, agent=a), _ev(2, agent=b),
        _ev(3, agent=a), _ev(4, agent=b),
        _ev(5, agent=a), _ev(6, agent=b),
    ]
    reports = detect_duplicate_work(stream)
    assert len(reports) == 2
    by_agent = {r.agent: r for r in reports}

    assert by_agent[a].trip_event.raw_id == 3
    assert by_agent[a].occurrences == 3
    assert by_agent[a].prevented_runs == 1

    assert by_agent[b].trip_event.raw_id == 4
    assert by_agent[b].occurrences == 3
    assert by_agent[b].prevented_runs == 1


# ===========================================================================
# detect() ordering — sorts by prevented_cost DESC, stable on ties, never
# reorders the caller's input stream
# ===========================================================================

def test_detect_sorts_descending_and_leaves_input_stream_untouched():
    """detect() returns reports costliest-first while leaving the caller's input
    list unmodified (same order, same Event identities)."""
    cheap = [_ev(1, agent="c", cost_usd=1.0), _ev(3, agent="c", cost_usd=1.0), _ev(5, agent="c", cost_usd=1.0)]
    pricey = [_ev(2, agent="p", cost_usd=100.0), _ev(4, agent="p", cost_usd=100.0), _ev(6, agent="p", cost_usd=100.0)]
    inp = [cheap[0], pricey[0], cheap[1], pricey[1], cheap[2], pricey[2]]
    snapshot = list(inp)

    reports = detect(inp)
    assert [r.agent for r in reports] == ["p", "c"]
    assert reports[0].prevented_cost == 100.0
    assert reports[1].prevented_cost == 1.0

    # Input stream is not reordered or mutated in place.
    assert inp == snapshot
    assert all(inp[i] is snapshot[i] for i in range(len(inp)))


def test_detect_tie_break_is_stable_first_seen_signature_order():
    """Two runaways with EQUAL prevented_cost are returned in first-seen-signature
    order for BOTH input orderings — the descending sort is stable on ties."""
    a, b = "agent-a", "agent-b"
    a_run = [_ev(1, agent=a, cost_usd=10.0), _ev(2, agent=a, cost_usd=10.0), _ev(3, agent=a, cost_usd=10.0)]
    b_run = [_ev(4, agent=b, cost_usd=10.0), _ev(5, agent=b, cost_usd=10.0), _ev(6, agent=b, cost_usd=10.0)]

    r_ab = detect(a_run + b_run)
    r_ba = detect(b_run + a_run)

    # Equal prevented_cost -> the tie-break must be first-seen order, not random.
    assert r_ab[0].prevented_cost == r_ab[1].prevented_cost == 10.0
    assert [r.agent for r in r_ab] == [a, b]
    assert [r.agent for r in r_ba] == [b, a]


def test_detect_does_not_re_sort_caller_supplied_order():
    """The detector processes events in feed order and never re-sorts by ts.

    Same three events, two feed orders, two different results: feeding them
    ts-sorted (a progress delta sits between the duplicates) yields NO trip;
    feeding them out of ts order (progress delta last) lets the duplicates sit
    adjacent and TRIP. This pins the 'caller must pre-sort; detect does not
    re-sort' contract.
    """
    dup1 = _ev(1)
    progress = _ev(2, progress=True)
    dup3 = _ev(3)

    ts_sorted = [dup1, progress, dup3]
    out_of_order = [dup1, dup3, progress]

    assert detect_duplicate_work(ts_sorted) == []
    assert len(detect_duplicate_work(out_of_order)) == 1


# ===========================================================================
# Degenerate streams
# ===========================================================================

def test_empty_stream_yields_no_reports():
    assert detect_duplicate_work([]) == []
    assert detect([]) == []


def test_single_event_stream_yields_no_reports():
    assert detect_duplicate_work([_ev(1)]) == []
    assert detect([_ev(1)]) == []


# ===========================================================================
# detect() forwards **knobs verbatim to detect_duplicate_work
# ===========================================================================

def test_detect_forwards_threshold_knob():
    events = [_ev(i, cost_usd=10.0) for i in range(1, 5)]
    assert detect(events, threshold=3)[0].trip_index == 3


def test_detect_forwards_idempotent_agents_knob():
    events = [_ev(1), _ev(2)]
    assert detect(events, idempotent_agents=frozenset({WORKFLOW})) == []


def test_detect_forwards_token_tolerance_knob():
    """A 1000 -> 1001 (0.1%) pair trips at the default 5% tolerance but NOT at
    token_tolerance=0.0 — proving detect() forwards the knob unchanged."""
    events = [_ev(1, input_tokens=1000), _ev(2, input_tokens=1001)]
    assert len(detect(events)) == 1
    assert detect(events, token_tolerance=0.0) == []


# ===========================================================================
# _args_similar — direct unit coverage of the similarity primitive itself
# (test_distinct_args_hash_does_not_trip exercises signature partitioning, NOT
# this function, so the both-hash-differ branch is otherwise unreachable).
# ===========================================================================

def test_args_similar_both_hashes_equal_is_true_regardless_of_tokens():
    eq_none = _args_similar(_ev(1, args_hash="x", input_tokens=None),
                            _ev(2, args_hash="x", input_tokens=None), 0.05)
    eq_gap = _args_similar(_ev(1, args_hash="x", input_tokens=1),
                           _ev(2, args_hash="x", input_tokens=10_000), 0.05)
    assert eq_none is True
    assert eq_gap is True


def test_args_similar_both_hashes_differ_is_false_even_with_identical_tokens():
    assert _args_similar(_ev(1, args_hash="x", input_tokens=1000),
                         _ev(2, args_hash="y", input_tokens=1000), 0.05) is False


def test_args_similar_falls_back_to_token_proximity_when_a_hash_is_missing():
    """When either hash is None the function ignores hashes and uses the
    input-token proximity fallback."""
    within = _args_similar(_ev(1, args_hash="x", input_tokens=1000),
                           _ev(2, args_hash=None, input_tokens=1010), 0.05)
    over = _args_similar(_ev(1, args_hash=None, input_tokens=1000),
                         _ev(2, args_hash="y", input_tokens=2000), 0.05)
    assert within is True
    assert over is False


def test_args_similar_token_fallback_boundary_is_inclusive():
    assert _args_similar(_ev(1, input_tokens=1000), _ev(2, input_tokens=1050), 0.05) is True
    assert _args_similar(_ev(1, input_tokens=1000), _ev(2, input_tokens=1051), 0.05) is False


def test_args_similar_no_signal_is_false():
    """Both hashes None and both token counts None -> insufficient signal -> False."""
    assert _args_similar(_ev(1, args_hash=None, input_tokens=None),
                         _ev(2, args_hash=None, input_tokens=None), 0.05) is False


# ===========================================================================
# detail string — pins the current human-facing sentence, INCLUDING the phrasing
# the audit flagged as inaccurate for high-variance streams, so a future reword
# is a deliberate, test-breaking change rather than a silent edit.
# ===========================================================================

def test_detail_reports_accurate_trip_and_prevented_facts():
    """The load-bearing, accurate facts in the detail string are present:
    the agent, the trip occurrence ordinal, the tripping raw_id, and the
    prevented run-count / dollar figure."""
    events = [
        _ev(1, input_tokens=1000, cost_usd=10.0),
        _ev(2, input_tokens=1010, cost_usd=10.0),  # trip at #2
        _ev(3, input_tokens=5000, cost_usd=10.0),  # post-trip, chain-broken
        _ev(4, input_tokens=6000, cost_usd=10.0),
    ]
    detail = detect_duplicate_work(events)[0].detail
    assert "workflow-subagent" in detail
    assert "tripped at occurrence 2" in detail
    assert "raw_id=2" in detail
    assert "2 subsequent dispatch(es) worth" in detail
    assert "$20.00" in detail


def test_detail_string_accurate_phrasing_after_remediation():
    """Checks the corrected detail string phrasing (audit remediation).

    For the stream below only the 1000->1010 pair is within 5%; the later pairs
    (1010->5000, 5000->6000) are far outside tolerance. The detail must:
    - Report the total same-agent dispatch count (4) without falsely claiming
      all 4 were within token variance of each other.
    - State the trip occurrence ordinal and that it was within tolerance of
      the PRECEDING (not group-wide) dispatch.
    - NOT contain the misleading "occurrences within" substring.
    """
    events = [
        _ev(1, input_tokens=1000),
        _ev(2, input_tokens=1010),
        _ev(3, input_tokens=5000),
        _ev(4, input_tokens=6000),
    ]
    detail = detect_duplicate_work(events)[0].detail
    # The false claim must be gone.
    assert "occurrences within" not in detail
    # Accurate count of same-agent dispatches and pairwise trip condition.
    assert "4 same-agent dispatches" in detail
    assert "tripped at occurrence 2" in detail
    assert "within 5% input-token variance of the preceding dispatch" in detail


# ===========================================================================
# Report shape sanity
# ===========================================================================

def test_tripped_report_kind_and_signature_are_well_formed():
    events = [_ev(1, args_hash=None), _ev(2, args_hash=None)]
    report = detect_duplicate_work(events)[0]
    assert report.kind == KIND_DUPLICATE_WORK
    assert report.signature == (WORKFLOW, "dispatch", None)
    assert report.agent == WORKFLOW
    assert report.first_event.raw_id == 1
