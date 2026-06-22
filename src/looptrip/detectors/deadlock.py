"""detectors/deadlock.py — Chandy–Misra–Haas wait-for cycle detector.

Implements :func:`detect_deadlock`, the Phase-2 deadlock pathology detector.
A deadlock is a set of two or more agents each waiting on another member of the
set, forming a directed cycle in the wait-for graph.  No agent in the cycle can
make progress without external intervention.

Algorithm overview (Chandy–Misra–Haas adapted for the single-target grammar)
------------------------------------------------------------------------------

1. **Latest-state-wins pass** — scan the feed-ordered event stream, recording
   for each agent its *latest* event together with its
   :func:`~looptrip.detectors._shared._is_blocked` verdict.  An agent whose
   latest event is *not* blocked is treated as non-blocked, even if earlier
   events indicated blocking (a retry, a timeout, or a successful handoff can
   dissolve a prior wait).

2. **Blocked map** — retain only agents whose latest event is blocked
   (:func:`~looptrip.detectors._shared._is_blocked` returned ``True``).  When
   the map is empty (no blocked agent, or ``handoff_state`` is ``None``
   everywhere) the function returns ``[]`` immediately without error.

3. **Functional wait-for graph** — build a directed graph among the blocked
   agents.  An edge ``u → t`` exists iff ``u``'s ``event.to_agent`` equals
   ``t``, ``t`` is itself blocked, and ``t ≠ u`` (self-loops are excluded; the
   validator enforces ``min_cycle_len ≥ 2``).  The wait-for target is read
   directly from the explicit ``to_agent`` field — no delimiter scanning — so
   at most one named waiter exists per event and the graph is *functional*
   (out-degree ≤ 1).

4. **Memoized O(V) cycle detection** — for each unclassified blocked node,
   walk its unique outgoing edge while recording the current trail.  A
   re-entry of a node already in the trail yields a cycle slice; all cycle
   members are marked ``IN_CYCLE`` and the pre-cycle prefix is marked
   ``ACYCLIC``.  Dead-end nodes (no outgoing edge, or edge to an
   already-classified node) are marked ``ACYCLIC``.  Cycles are deduplicated
   by ``tuple(sorted(members))``.

5. **Report emission** — one :class:`~looptrip.detectors.types.PathologyReport`
   per distinct cycle whose ``len(members) ≥ config.min_cycle_len``.

**Inherent limitation:** this detector REQUIRES ``handoff_state`` to carry a
bare blocked-state token (e.g. ``"blocked"``, ``"BLOCKED"``, ``"waiting"``) and
the wait-for edge requires the explicit ``to_agent`` field to name the awaited
agent.  When every event in the stream has ``handoff_state=None`` — the cast.db
reality for many CAST agents — the blocked map is empty and
:func:`detect_deadlock` returns ``[]`` without error.  This is the accepted,
documented limitation;
:func:`~looptrip.detectors.ping_pong.detect_ping_pong` and
:func:`~looptrip.detectors.non_termination.detect_non_termination` are the
handoff-free complementary detectors.

This module is stdlib-only and defines no global mutable state.  Every call to
:func:`detect_deadlock` builds and discards all local bookkeeping within the
call frame.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from looptrip.normalize import Event
from looptrip.detectors.types import (
    KIND_DEADLOCK,
    DetectionConfig,
    PathologyReport,
    resolve_config,
)
from looptrip.detectors._shared import (
    _is_blocked,
)


def detect_deadlock(
    events: Iterable[Event],
    *,
    config: Optional[DetectionConfig] = None,
    **knobs,
) -> List[PathologyReport]:
    """Detect deadlock via Chandy–Misra–Haas wait-for graph cycle detection.

    Scans ``events`` for a set of mutually-blocked agents forming a directed
    cycle in the wait-for graph.  Implements a memoized functional-graph walk
    for O(V) cycle detection (where V is the count of blocked agents), then
    emits one :class:`~looptrip.detectors.types.PathologyReport` per distinct
    qualifying cycle.

    **Latest-state-wins semantics:** an agent whose *latest* event is
    non-blocked is not considered blocked, even when earlier events indicated
    blocking.  A successful handoff, a timeout handler, or a retry event
    dissolves the prior wait.  Only the final event per agent contributes to
    the blocked map.

    **Inherent limitation:** ``handoff_state`` must carry a bare blocked-state
    token (e.g. ``"blocked"``, ``"BLOCKED"``) and the wait-for edge is read from
    the explicit ``to_agent`` field.  When every event has
    ``handoff_state=None`` the return value is always ``[]`` — no exception, no
    false positives.  This is the documented accepted limitation.

    The function resolves its own configuration via
    :func:`~looptrip.detectors.types.resolve_config` so it can be called
    standalone (e.g. ``detect_deadlock(events, blocked_states={"waiting"})``).

    Args:
        events:  Ordered iterable of :class:`~looptrip.normalize.Event`
                 instances.  The caller is responsible for pre-sorting by
                 ``(ts, raw_id)``; the detector processes events in feed
                 order without re-sorting or mutating the stream.
        config:  Optional pre-built
                 :class:`~looptrip.detectors.types.DetectionConfig`.  When
                 ``None``, a default ``DetectionConfig()`` is used as the
                 base.  ``**knobs`` override specific fields on top of
                 ``config``.
        **knobs: Ad-hoc overrides for individual
                 :class:`~looptrip.detectors.types.DetectionConfig` fields,
                 merged via
                 :func:`~looptrip.detectors.types.resolve_config`.  Key
                 knobs for this detector:

                 * ``blocked_states`` — frozenset of bare state tokens
                   that classify an event's ``handoff_state`` as blocked
                   (matched case-insensitively; default
                   ``{"blocked", "waiting"}``).
                 * ``min_cycle_len`` — minimum number of distinct agents
                   required in a cycle (default ``2``; self-loops always
                   excluded).

    Returns:
        A list of :class:`~looptrip.detectors.types.PathologyReport` instances,
        one per distinct deadlock cycle meeting ``min_cycle_len``.  The
        iteration order follows cycle-discovery order, which is deterministic
        for a fixed input stream.  May be empty.

    Raises:
        TypeError: If ``**knobs`` contains a key that is not a valid
            :class:`~looptrip.detectors.types.DetectionConfig` field name
            (propagated from
            :func:`~looptrip.detectors.types.resolve_config`).
        ValueError: If a config-field boundary is violated (e.g.
            ``min_cycle_len=1``), propagated from
            :meth:`~looptrip.detectors.types.DetectionConfig.__post_init__`.
    """
    cfg = resolve_config(config, knobs)

    # ------------------------------------------------------------------ #
    # Phase 1 — latest-state-wins pass                                     #
    # ------------------------------------------------------------------ #
    # Scan every event in feed order, updating each agent's (latest_event,
    # is_blocked) pair.  Later events overwrite earlier ones, so only the
    # final state per agent is retained.
    latest: dict[str, tuple[Event, bool]] = {}
    for event in events:
        blk = _is_blocked(event.handoff_state, cfg)
        latest[event.agent] = (event, blk)

    # ------------------------------------------------------------------ #
    # Phase 2 — blocked map                                                #
    # ------------------------------------------------------------------ #
    # Keep only agents whose *latest* event is blocked (_is_blocked True).
    # Agents whose latest event is non-blocked are dropped (latest-state wins).
    # When handoff_state is None everywhere _is_blocked is always False →
    # blocked is empty → return [] immediately (the documented inherent
    # limitation).
    blocked: dict[str, tuple[bool, Event]] = {
        agent: (blk, ev)
        for agent, (ev, blk) in latest.items()
        if blk
    }

    if not blocked:
        return []

    # ------------------------------------------------------------------ #
    # Phase 3 — functional wait-for graph                                  #
    # ------------------------------------------------------------------ #
    # Build a directed graph among blocked agents only.
    # Edge u → t iff: u's to_agent == t AND t ∈ blocked AND t != u.
    # Out-degree is at most 1 (functional graph). Absent/invalid targets
    # become None (dead-end in the graph, no outgoing edge).
    graph: dict[str, Optional[str]] = {}
    for u, (_blk, ev) in blocked.items():
        t = ev.to_agent
        if t is not None and t in blocked and t != u:
            graph[u] = t
        else:
            graph[u] = None  # no valid outgoing edge

    # ------------------------------------------------------------------ #
    # Phase 4 — memoized O(V) functional-graph cycle detection             #
    # ------------------------------------------------------------------ #
    # Three classification states for each blocked agent:
    _UNKNOWN = 0   # not yet visited by any _walk call
    _IN_CYCLE = 1  # confirmed member of a wait-for cycle
    _ACYCLIC = 2   # leads to a dead-end or an already-classified node

    # Every blocked agent starts unclassified.
    node_status: dict[str, int] = {a: _UNKNOWN for a in blocked}
    # Deduplicated cycle registry: tuple(sorted(members)) → ordered member list.
    seen_cycles: dict[tuple, list[str]] = {}

    def _walk(start: str) -> None:
        """Classify all nodes reachable from ``start`` via the wait-for graph.

        Maintains an ordered trail of the current walk path.  Re-entry of a
        node already in the trail signals a cycle; nodes preceding the cycle
        anchor and dead-end nodes are classified ACYCLIC.  Already-classified
        nodes (from prior calls) terminate the walk immediately.

        Newly found cycles are registered in ``seen_cycles`` under their
        sorted canonical key and are deduplicated automatically.
        """
        trail: list[str] = []
        trail_idx: dict[str, int] = {}  # node → 0-based position in trail

        node = start
        while True:
            # ---- Already classified by a prior _walk call ----
            # The current trail leads to a classified node.  Nodes on the
            # trail are acyclic (they enter but do not form an undiscovered
            # cycle).
            if node_status[node] != _UNKNOWN:
                for n in trail:
                    node_status[n] = _ACYCLIC
                return

            # ---- Re-entry onto the current trail → cycle found ----
            # node appears at trail_idx[node]; everything from that position
            # onward is the cycle.  Everything before it merely leads to it.
            if node in trail_idx:
                cycle_start_pos = trail_idx[node]
                cycle: list[str] = trail[cycle_start_pos:]

                for n in cycle:
                    node_status[n] = _IN_CYCLE
                for n in trail[:cycle_start_pos]:
                    node_status[n] = _ACYCLIC

                key = tuple(sorted(cycle))
                if key not in seen_cycles:
                    seen_cycles[key] = cycle
                return

            # ---- Extend the trail and follow the unique outgoing edge ----
            trail.append(node)
            trail_idx[node] = len(trail) - 1

            next_node = graph[node]
            if next_node is None:
                # Dead end: no valid outgoing edge; entire trail is ACYCLIC.
                for n in trail:
                    node_status[n] = _ACYCLIC
                return

            node = next_node

    for agent in blocked:
        if node_status[agent] == _UNKNOWN:
            _walk(agent)

    # ------------------------------------------------------------------ #
    # Phase 5 — emit one PathologyReport per distinct qualifying cycle     #
    # ------------------------------------------------------------------ #
    reports: List[PathologyReport] = []
    for key, members in seen_cycles.items():
        if len(members) < cfg.min_cycle_len:
            # Cannot occur with default min_cycle_len=2 and self-loop
            # exclusion, but respected when the caller overrides min_cycle_len.
            # The validator guarantees min_cycle_len >= 2, making this a
            # belt-and-suspenders guard against future config evolution.
            continue

        # The "defining event" for each member is its LATEST blocked event —
        # the event that anchors its current blocked state in the map.
        member_events: list[Event] = [blocked[m][1] for m in members]  # type: ignore[index]
        trip_event: Event = max(member_events, key=lambda e: e.ts)
        first_event: Event = min(member_events, key=lambda e: e.ts)
        sorted_members: tuple = tuple(sorted(members))

        detail = (
            f"Deadlock: {len(members)} agents {sorted_members!r} form a "
            f"directed wait-for cycle — each agent's latest event indicates "
            f"it is blocked waiting on the next member of the cycle.  "
            f"Earliest blocked event: raw_id={first_event.raw_id}; "
            f"latest blocked event: raw_id={trip_event.raw_id}.  "
            f"No agent can make progress without external intervention.  "
            f"(prevented_cost=0.0: a deadlock is a wall-clock hang, not "
            f"recurring spend.)"
        )

        reports.append(
            PathologyReport(
                kind=KIND_DEADLOCK,
                signature=sorted_members,
                agent=min(members),
                occurrences=len(members),
                trip_index=1,
                trip_event=trip_event,
                first_event=first_event,
                prevented_cost=0.0,
                prevented_runs=0,
                detail=detail,
                members=sorted_members,
                blocked_agents=frozenset(members),
            )
        )

    return reports
