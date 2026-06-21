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

**Phase 2 detectors** are now implemented in the :mod:`looptrip.detectors`
subpackage and are opt-in via :func:`detect` ``detectors=`` parameter or the
:func:`detect_all` convenience.  The three additional pathologies covered:

* **ping-pong / livelock** (:func:`detect_ping_pong`) — A→B→A→B with no net
  state advance; detected by counting directed-cycle closures in the temporal
  agent-visitation sequence, independent of token counts.
* **deadlock** (:func:`detect_deadlock`) — mutually-blocked agents, each
  waiting on another, none progressing; detected via a wait-for graph over
  ``handoff_state`` edges (requires ``handoff_state`` to be non-``None``).
* **non-termination** (:func:`detect_non_termination`) — unbounded growth with
  no terminal state; detected by a sliding-window unique-state count plateau,
  token-independent.

The :func:`detect` default (``detectors=None``) remains **duplicate-work-only**
for full backward compatibility with Phase-1 callers and tests.  Pass
``detectors=ALL_DETECTORS`` or call :func:`detect_all` to enable all four.

This module is stdlib-only and holds no global mutable state: every call to
:func:`detect` / :func:`detect_duplicate_work` builds and discards its own
per-signature bookkeeping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from looptrip.normalize import Event
from looptrip.detectors.types import (
    PathologyReport,
    DetectionConfig,
    KIND_DUPLICATE_WORK,
    KIND_PING_PONG,
    KIND_DEADLOCK,
    KIND_NON_TERMINATION,
    ALL_DETECTORS,
    resolve_config,
)
from looptrip.detectors.ping_pong import detect_ping_pong
from looptrip.detectors.deadlock import detect_deadlock
from looptrip.detectors.non_termination import detect_non_termination

__all__ = [
    "detect",
    "detect_all",
    "detect_duplicate_work",
    "detect_ping_pong",
    "detect_deadlock",
    "detect_non_termination",
    "PathologyReport",
    "DetectionConfig",
    "KIND_DUPLICATE_WORK",
    "KIND_PING_PONG",
    "KIND_DEADLOCK",
    "KIND_NON_TERMINATION",
    "ALL_DETECTORS",
    "_args_similar",
]


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


def _run_duplicate_work(
    events: List[Event],
    cfg: DetectionConfig,
) -> List[PathologyReport]:
    """Adapter that calls :func:`detect_duplicate_work` with the three legacy knobs.

    Extracts only the fields that ``detect_duplicate_work`` accepts
    (``token_tolerance``, ``threshold``, ``idempotent_agents``) from the
    unified :class:`DetectionConfig`, ensuring that the new sensitivity knobs
    (``retry_allowed``, ``allowlist_agents``, etc.) never reach the
    duplicate-work detector — which keeps its behavior provably byte-identical
    to its Phase-1 implementation regardless of how the caller configures the
    broader detection session.

    Args:
        events:  Pre-materialized event list (already copied by
                 :func:`detect`; safe to pass directly).
        cfg:     Resolved :class:`DetectionConfig` for this detection run.

    Returns:
        The raw (un-sorted) list of :class:`PathologyReport` instances from
        :func:`detect_duplicate_work`.
    """
    return detect_duplicate_work(
        events,
        token_tolerance=cfg.token_tolerance,
        threshold=cfg.threshold,
        idempotent_agents=cfg.idempotent_agents,
    )


_REGISTRY: dict = {
    KIND_DUPLICATE_WORK: _run_duplicate_work,
    KIND_PING_PONG: lambda evs, cfg: detect_ping_pong(evs, config=cfg),
    KIND_DEADLOCK: lambda evs, cfg: detect_deadlock(evs, config=cfg),
    KIND_NON_TERMINATION: lambda evs, cfg: detect_non_termination(evs, config=cfg),
}
"""Mapping from each ``KIND_*`` constant to its runner callable.

Each runner has the signature ``(events: List[Event], cfg: DetectionConfig)
-> List[PathologyReport]``.  The registry is a plain module-level ``dict``
(not a mutable class attribute) and is never mutated after module load, so it
holds no global mutable state."""


def detect(
    events: Iterable[Event],
    *,
    config: Optional[DetectionConfig] = None,
    detectors: Optional[Iterable[str]] = None,
    **knobs,
) -> List[PathologyReport]:
    """Run selected detectors over ``events`` and return sorted reports.

    The default (``detectors=None``) runs **duplicate-work only**, preserving
    full backward compatibility with Phase-1 callers.  Pass
    ``detectors=ALL_DETECTORS`` or call :func:`detect_all` to enable all four
    detectors.

    Args:
        events:    Ordered event stream.  The caller is responsible for
                   pre-sorting by ``(ts, raw_id)``.  The stream is
                   materialised once into a list (a reference copy; no
                   reorder or mutation) so that multi-detector runs can each
                   iterate the same sequence.
        config:    Pre-built :class:`DetectionConfig`; ``None`` uses defaults.
        detectors: Iterable of ``KIND_*`` strings naming which detectors to
                   run.  ``None`` (the default) selects duplicate-work only.
                   Order does not matter — the canonical :data:`ALL_DETECTORS`
                   order is always used when iterating.
        **knobs:   Ad-hoc overrides forwarded to :func:`resolve_config`
                   (merged on top of ``config``).  Unknown keys raise
                   :class:`TypeError`.

    Returns:
        All reports from every selected detector, sorted by
        ``prevented_cost`` DESCENDING (costliest runaway first).

    Raises:
        TypeError: If ``knobs`` contains an unrecognised configuration key.
    """
    cfg = resolve_config(config, knobs)
    selected = (KIND_DUPLICATE_WORK,) if detectors is None else tuple(detectors)
    materialized = list(events)   # copy WITHOUT reorder/mutate; allows multi-detector iteration
    reports: List[PathologyReport] = []
    for kind in ALL_DETECTORS:    # canonical, deterministic order
        if kind in selected:
            reports.extend(_REGISTRY[kind](materialized, cfg))
    return sorted(reports, key=lambda r: r.prevented_cost, reverse=True)


def detect_all(
    events: Iterable[Event],
    *,
    config: Optional[DetectionConfig] = None,
    **knobs,
) -> List[PathologyReport]:
    """Run ALL four detectors over ``events`` and return sorted reports.

    Convenience wrapper around :func:`detect` with ``detectors=ALL_DETECTORS``.
    Equivalent to ``detect(events, config=config, detectors=ALL_DETECTORS, **knobs)``.

    Args:
        events:  Ordered event stream (pre-sorted by ``(ts, raw_id)``).
        config:  Pre-built :class:`DetectionConfig`; ``None`` uses defaults.
        **knobs: Ad-hoc sensitivity overrides (see :func:`detect`).

    Returns:
        All reports from all four detectors, sorted by ``prevented_cost``
        DESCENDING.
    """
    return detect(events, config=config, detectors=ALL_DETECTORS, **knobs)
