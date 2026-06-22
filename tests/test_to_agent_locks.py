"""Behavior-pinning regression locks for the explicit ``to_agent`` refactor.

These tests pin the latent behavior changes and contract guarantees (LOCKs A-E)
introduced when the brittle packed-string ``handoff_state`` parser was retired in
favour of an explicit :attr:`~looptrip.normalize.Event.to_agent` field:

* **Bare-token membership goes live** — ``handoff_state`` now carries only a
  bare state token (``"blocked"``, ``"in_progress"``, …), so a token like
  ``"in_progress"`` is now a *full-string* member of ``progress_markers`` /
  ``terminal_states`` (LOCK A).  Pre-refactor, the packed ``"in_progress on B"``
  form would never have matched.
* **state_key bucket granularity** — with ``state_key="handoff_state"`` the
  non-termination detector buckets purely on the bare token; ``to_agent`` does
  NOT split a bucket (LOCK B).  The packed form would have produced one bucket
  per target.
* **Config-side case folding + packed-string inertness** — ``_is_blocked``
  lowercases BOTH sides, and the legacy packed string is no longer parsed by
  the deadlock detector (LOCKs C and D).
* **Handoff-edge mode genuinely reads ``to_agent``** — the ping-pong
  handoff-edge substrate trips on the explicit ``to_agent`` hops even when the
  temporal agent order does not (LOCK E).  The old test silently fell back to
  temporal order, hiding whether ``to_agent`` was exercised at all.

A future CAST / OTel config change cannot silently regress any of these without
turning one of these locks red.

Events are built directly — no fixture is needed to exercise the state machines.
None of these touch the packaged proof (tests/fixtures/, cast_db_runaways.json).
"""

from __future__ import annotations

from looptrip.detector import (
    KIND_DEADLOCK,
    KIND_NON_TERMINATION,
    KIND_PING_PONG,
    detect_deadlock,
    detect_non_termination,
    detect_ping_pong,
)
from looptrip.detectors.types import DetectionConfig
from looptrip.normalize import Event


def _ev(
    raw_id: int,
    *,
    agent: str,
    tool: str = "dispatch",
    args_hash: str | None = None,
    handoff_state: str | None = None,
    to_agent: str | None = None,
    cost_usd: float = 10.0,
    progress: bool = False,
    ts: str | None = None,
) -> Event:
    """Build a normalized Event with explicit handoff_state / to_agent."""
    return Event(
        agent=agent,
        tool=tool,
        args_hash=args_hash,
        ts=ts or f"2026-06-21T00:00:{raw_id:02d}Z",
        handoff_state=handoff_state,
        to_agent=to_agent,
        cost_usd=cost_usd,
        progress=progress,
        raw_id=raw_id,
    )


# ===========================================================================
# LOCK A — bare-token membership goes live (config-gated)
# ===========================================================================
#
# A bare "in_progress" token is now a full-string member candidate for
# progress_markers / terminal_states.  Default (empty) progress_markers means
# the stream is a live ping-pong; declaring "in_progress" a progress marker
# turns every event into a per-event epoch reset, dissolving the cycle.


def _alternating_in_progress_stream() -> list[Event]:
    """A->B->A->B->A with handoff_state='in_progress' and alternating to_agent."""
    targets = ["B", "A", "B", "A", "B"]
    agents = ["A", "B", "A", "B", "A"]
    return [
        _ev(i + 1, agent=agents[i], handoff_state="in_progress", to_agent=targets[i])
        for i in range(5)
    ]


def test_lockA_ping_pong_trips_with_default_empty_progress_markers():
    """Default (empty) progress_markers: the in_progress stream is a live ping-pong.

    With use_handoff_edges=True the explicit to_agent hops reinforce the A<->B
    cycle; the bare 'in_progress' token is NOT a progress marker by default, so
    no epoch reset fires and the cycle closes twice -> trips.
    """
    events = _alternating_in_progress_stream()
    reports = detect_ping_pong(events, use_handoff_edges=True)
    assert len(reports) == 1
    assert reports[0].kind == KIND_PING_PONG
    assert reports[0].members == ("A", "B")


def test_lockA_ping_pong_no_trip_when_in_progress_is_a_progress_marker():
    """progress_markers={'in_progress'}: every event is now a per-event epoch reset.

    The bare token 'in_progress' is a full-string member of progress_markers,
    so _is_progress() fires on every event, clearing the path before any cycle
    can close. Pre-refactor a packed 'in_progress on B' could never have matched
    the bare 'in_progress' marker.
    """
    events = _alternating_in_progress_stream()
    reports = detect_ping_pong(
        events,
        use_handoff_edges=True,
        progress_markers=frozenset({"in_progress"}),
    )
    assert reports == []


def test_lockA_mirror_ping_pong_no_trip_when_in_progress_is_terminal():
    """Mirror for terminal_states: 'in_progress' as a terminal token resets epochs too."""
    events = _alternating_in_progress_stream()
    reports = detect_ping_pong(
        events,
        use_handoff_edges=True,
        terminal_states=frozenset({"in_progress"}),
    )
    assert reports == []


def test_lockA_mirror_non_termination_in_progress_membership():
    """Mirror for non_termination: bare 'in_progress' membership gates the plateau.

    A single-agent constant-signature stream of 'in_progress' events plateaus
    (distinct==1) and fires by default. Declaring 'in_progress' a progress
    marker OR a terminal state breaks every window -> no report.
    """
    events = [
        _ev(i + 1, agent="A", handoff_state="in_progress", to_agent=None)
        for i in range(5)
    ]

    # Default: bare token is inert vocabulary -> the plateau fires.
    default_reports = detect_non_termination(events, window_size=3)
    assert len(default_reports) == 1
    assert default_reports[0].kind == KIND_NON_TERMINATION

    # progress marker membership breaks every window.
    assert (
        detect_non_termination(
            events, window_size=3, progress_markers=frozenset({"in_progress"})
        )
        == []
    )
    # terminal-state membership likewise breaks every window.
    assert (
        detect_non_termination(
            events, window_size=3, terminal_states=frozenset({"in_progress"})
        )
        == []
    )


# ===========================================================================
# LOCK B — state_key="handoff_state" bucket granularity
# ===========================================================================
#
# With state_key="handoff_state" the non-termination unique-state count buckets
# purely on the bare handoff_state token. An alternating to_agent does NOT split
# the bucket: all "blocked" events collapse to ONE state, regardless of target.


def test_lockB_state_key_handoff_state_buckets_ignore_to_agent():
    """All-'blocked' window with alternating to_agent counts as ONE state bucket.

    state_key='handoff_state' reads the bare token only. Five 'blocked' events
    whose to_agent alternates 'B'/'C' yield distinct==1 across the window, so
    the plateau qualifies (cap = floor(3*0.5) = 1) and fires.

    PRE-REFACTOR the packed handoff_state form ('blocked on B' vs 'blocked on C')
    would have produced TWO distinct buckets, distinct==2 > cap==1, and this
    window would NOT have fired.
    """
    targets = ["B", "C", "B", "C", "B"]
    events = [
        _ev(i + 1, agent="A", handoff_state="blocked", to_agent=targets[i])
        for i in range(5)
    ]
    reports = detect_non_termination(
        events, window_size=3, state_key="handoff_state"
    )
    assert len(reports) == 1
    # window == (start_index, end_index_exclusive, unique_states, window_size)
    _start, _end, unique_states, _n = reports[0].window
    assert unique_states == 1  # ONE bucket — to_agent did not split it


# ===========================================================================
# LOCK C — _is_blocked lowercases the CONFIG side
# ===========================================================================


def test_lockC_is_blocked_lowercases_config_side():
    """blocked_states={'STALLED'} (UPPERCASE config) matches bare 'stalled'.

    _is_blocked lowercases BOTH the event's handoff_state AND the config's
    blocked_states members, so an uppercase config token matches a lowercase
    event token. A 2-cycle of 'stalled' agents therefore produces a deadlock.
    """
    events = [
        _ev(1, agent="A", handoff_state="stalled", to_agent="B"),
        _ev(2, agent="B", handoff_state="stalled", to_agent="A"),
    ]
    cfg = DetectionConfig(blocked_states=frozenset({"STALLED"}))
    reports = detect_deadlock(events, config=cfg)
    assert len(reports) == 1
    assert reports[0].kind == KIND_DEADLOCK
    assert reports[0].blocked_agents == frozenset({"A", "B"})


# ===========================================================================
# LOCK D — legacy packed string is INERT
# ===========================================================================


def test_lockD_legacy_packed_string_is_inert():
    """A packed 'blocked on B' handoff_state (no to_agent) is no longer parsed.

    Under the explicit-to_agent contract _is_blocked tests the WHOLE bare token
    against blocked_states. 'blocked on b' is not a member of the default
    {'blocked', 'waiting'}, so the blocked map is empty and detect_deadlock
    returns []. This proves the packed form is dead on the detection path.
    """
    events = [
        _ev(1, agent="A", handoff_state="blocked on B", to_agent=None),
        _ev(2, agent="B", handoff_state="blocked on A", to_agent=None),
    ]
    assert detect_deadlock(events) == []


# ===========================================================================
# LOCK E — use_handoff_edges genuinely drives the trip
# ===========================================================================


def test_lockE_handoff_edges_drive_trip_temporal_does_not():
    """The to_agent hops close the cycle twice; the temporal order does not.

    The four agent nodes alone are A,B,A,B: in pure temporal mode the (A,B)
    cycle closes only ONCE (below cycle_trip_count=2) -> no trip -> [].

    With use_handoff_edges=True each event also contributes its explicit
    to_agent hop, doubling node visits to A,B,A,B,A so (A,B) closes TWICE and
    the detector trips. The trip is driven entirely by to_agent — the very
    signal the old test could silently skip by falling back to temporal order.
    """
    targets = ["B", "A", "B", "A"]
    agents = ["A", "B", "A", "B"]
    events = [
        _ev(i + 1, agent=agents[i], handoff_state="in_progress", to_agent=targets[i])
        for i in range(4)
    ]

    # Temporal mode (default): (A,B) closes once -> no trip.
    assert detect_ping_pong(events) == []

    # Handoff-edge mode: the to_agent hops close (A,B) twice -> trips.
    handoff_reports = detect_ping_pong(events, use_handoff_edges=True)
    assert len(handoff_reports) == 1
    assert handoff_reports[0].kind == KIND_PING_PONG
    assert handoff_reports[0].members == ("A", "B")
