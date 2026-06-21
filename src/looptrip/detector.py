"""detector.py — the deterministic, zero-LLM pathology state machine.

Phase 1 implements ONE rule: **duplicate-work / iteration-2**. Multi-agent
runaways manifest as a single signature ``(agent, tool, args_hash)`` recurring
with no progress delta between occurrences — the same dispatch fired again and
again, billing every time. The detector groups events by signature and trips
the moment a signature's recurrence count reaches ``threshold`` (default 2):
i.e. on the *second* occurrence, the first repeat — long before the invoice.

The model stated plainly: occurrences #1..(threshold-1) are the legal baseline;
the duplicate-work detector trips at occurrence #threshold (the 2nd occurrence
of the signature, within ``token_tolerance`` input-token variance of the
immediately-preceding occurrence, with no progress delta); every occurrence
from #(threshold+1) onward is prevented waste.

**Phase 1 detection limitation:** the trip signal is *pairwise* input-token
proximity to the immediately-preceding occurrence (a sliding window).
"Within tolerance" is never group-wide — only the tripping pair (the
occurrence that caused the trip and the one just before it) is checked for
token proximity.  A runaway whose first repeat is NOT within token tolerance
of its predecessor is a known Phase-1 blind spot; Phase 2 adds structural /
graph-based detection independent of token similarity.

The remaining three CAST pathologies are explicitly **out of scope for Phase 1**
and will land later as their own detectors:

* ping-pong / livelock — A→B→A→B with no net state advance,
* deadlock — mutually-blocked agents, none progressing,
* never-terminate — unbounded growth with no terminal state.

This module is stdlib-only and holds no global mutable state: every call to
:func:`detect` / :func:`detect_duplicate_work` builds and discards its own
per-signature bookkeeping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from looptrip.normalize import Event

KIND_DUPLICATE_WORK = "duplicate_work"


@dataclass(frozen=True, slots=True)
class PathologyReport:
    """A single confirmed pathology, anchored at its trip point.

    Attributes:
        kind:           Pathology family — ``"duplicate_work"`` in Phase 1.
        signature:      The offending ``(agent, tool, args_hash)`` triple.
        agent:          The acting agent (``signature[0]``, surfaced for ease).
        occurrences:    Total events sharing this signature in the stream.
        trip_index:     1-based ordinal of the occurrence that tripped the
                        detector (== ``threshold``; 2 by default).
        trip_event:     The event that tripped the detector (the 2nd occurrence).
        first_event:    The first occurrence of the signature (the baseline).
        prevented_cost: Sum of ``cost_usd`` over EVERY same-signature event
                        strictly AFTER the trip event — the waste a real trip
                        would have averted.  Model: killing the looping agent
                        at the trip point averts all of its later dispatches,
                        regardless of whether they remain within token
                        tolerance of each other ("kill-the-agent-at-trip").
        prevented_runs: Count of those post-trip events.
        detail:         A concise human-readable sentence describing the trip.
    """

    kind: str
    signature: tuple
    agent: str
    occurrences: int
    trip_index: int
    trip_event: Event
    first_event: Event
    prevented_cost: float
    prevented_runs: int
    detail: str


@dataclass(slots=True)
class _SigState:
    """Mutable per-signature bookkeeping, local to a single detector call."""

    members: List[Event] = field(default_factory=list)
    baseline: Optional[Event] = None       # previous counted occurrence
    recurrence_count: int = 0              # length of the current similar chain
    progress_since: bool = False          # progress delta seen since baseline
    tripped: bool = False
    trip_event: Optional[Event] = None
    trip_index: int = 0
    trip_pos: int = 0                     # index of trip_event within members


def _args_similar(prev: Event, cur: Event, token_tolerance: float) -> bool:
    """Decide whether two same-signature events represent the same work.

    Prefers an exact ``args_hash`` match when both events carry one (the OTel
    case). Falls back to input-token proximity within ``token_tolerance`` when
    args hashes are unavailable (the cast.db case, where ``args_hash`` is always
    ``None``). When neither signal is present there is insufficient evidence, so
    the events are treated as dissimilar and never trip.
    """
    if prev.args_hash is not None and cur.args_hash is not None:
        return prev.args_hash == cur.args_hash
    if prev.input_tokens is not None and cur.input_tokens is not None:
        return abs(cur.input_tokens - prev.input_tokens) / max(prev.input_tokens, 1) <= token_tolerance
    return False


def detect_duplicate_work(
    events: Iterable[Event],
    *,
    token_tolerance: float = 0.05,
    threshold: int = 2,
    idempotent_agents: frozenset = frozenset(),
) -> List[PathologyReport]:
    """Detect duplicate-work / iteration-2 runaways in an ordered event stream.

    Events are processed in the order given — the caller is responsible for
    pre-sorting by ``(ts, raw_id)``. Events are grouped by
    ``signature() == (agent, tool, args_hash)``. Within a group, an event is a
    duplicate-work RECURRENCE of the previous counted occurrence when BOTH:

    1. no ``progress=True`` event has occurred in the group since that previous
       counted occurrence (and the event itself is not a progress delta), and
    2. :func:`_args_similar` holds for ``(baseline, event)`` — a pairwise,
       sliding-window check against the immediately-preceding occurrence only.

    When a signature's recurrence count reaches ``threshold`` it TRIPS: the
    tripping event is recorded as ``trip_event`` and exactly one report is
    emitted for that signature (further occurrences only accrue prevented cost).
    ``prevented_cost`` is the sum of ``cost_usd`` over ALL same-signature events
    strictly after the trip event — not only those within token tolerance.
    The model is "kill the looping agent at the trip point", so every later
    dispatch is averted.
    Agents in ``idempotent_agents`` perform legitimately repeatable work and
    never trip.

    Returns one :class:`PathologyReport` per tripped signature, in trip order.
    """
    states: dict[tuple, _SigState] = {}

    for event in events:
        sig = event.signature()
        state = states.get(sig)
        if state is None:
            state = _SigState()
            states[sig] = state

        state.members.append(event)

        if state.baseline is None:
            # First occurrence of this signature: the legal baseline.
            state.baseline = event
            state.recurrence_count = 1
            state.progress_since = event.progress
            continue

        is_recurrence = (
            not state.progress_since
            and not event.progress
            and _args_similar(state.baseline, event, token_tolerance)
        )

        if is_recurrence:
            state.recurrence_count += 1
            if (
                not state.tripped
                and state.recurrence_count >= threshold
                and event.agent not in idempotent_agents
            ):
                state.tripped = True
                state.trip_event = event
                state.trip_index = state.recurrence_count
                state.trip_pos = len(state.members) - 1
            # Advance the baseline; reset the progress window.
            state.baseline = event
            state.progress_since = False
        else:
            # Chain break (progress delta or dissimilar args): start a fresh
            # baseline. A progress delta carries forward to block the next event.
            state.baseline = event
            state.recurrence_count = 1
            state.progress_since = event.progress

    reports: List[PathologyReport] = []
    for sig, state in states.items():
        if not state.tripped or state.trip_event is None:
            continue
        trip_event = state.trip_event
        first_event = state.members[0]
        post_trip = state.members[state.trip_pos + 1 :]
        prevented_cost = sum(ev.cost_usd or 0.0 for ev in post_trip)
        prevented_runs = len(post_trip)
        occurrences = len(state.members)
        detail = (
            f"{trip_event.agent!r} repeated signature {sig} with no progress delta: "
            f"{occurrences} same-agent dispatches; tripped at occurrence "
            f"{state.trip_index} (within {token_tolerance:.0%} input-token variance "
            f"of the preceding dispatch); {prevented_runs} subsequent dispatch(es) "
            f"worth ${prevented_cost:.2f} would have been averted "
            f"(raw_id={trip_event.raw_id})."
        )
        reports.append(
            PathologyReport(
                kind=KIND_DUPLICATE_WORK,
                signature=sig,
                agent=trip_event.agent,
                occurrences=occurrences,
                trip_index=state.trip_index,
                trip_event=trip_event,
                first_event=first_event,
                prevented_cost=prevented_cost,
                prevented_runs=prevented_runs,
                detail=detail,
            )
        )

    return reports


def detect(events: Iterable[Event], **knobs) -> List[PathologyReport]:
    """Run all Phase-1 detectors over ``events``.

    Phase 1 dispatches solely to :func:`detect_duplicate_work`. Reports are
    returned sorted by ``prevented_cost`` DESCENDING, so the costliest runaway
    surfaces first.
    """
    reports = detect_duplicate_work(events, **knobs)
    return sorted(reports, key=lambda report: report.prevented_cost, reverse=True)
