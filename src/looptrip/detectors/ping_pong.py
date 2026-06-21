"""detectors/ping_pong.py — ping-pong / livelock pathology detector.

A ping-pong (livelock) is a directed cycle of two or more agents that keeps
closing with no net state advance: A→B→A→B→… without any progress or terminal
event between closures.  Unlike the duplicate-work detector, this pathology is
entirely token-independent — it fires on the *structural* pattern of repeated
mutual dispatch, regardless of how the input-token counts vary.

Algorithm
---------
A single forward pass builds a **path stack** of agent nodes.  The key
invariant is that ``path`` always holds a *simple* (no-repeated-node) directed
path of agents visited since the last epoch reset.  When a revisit is detected
(the incoming agent is already on the path), the suffix of the path back to
that agent forms a directed cycle.  If the cycle has closed ``cycle_trip_count``
times within the current epoch without an intervening progress/terminal event,
the detector trips.

State tables (life-times):

* **Epoch-scoped** (cleared on every progress/terminal event):
  ``path``, ``pos``, ``closures``, ``first_closure``.
* **Lifetime** (persist across epoch resets):
  ``total_closures``, ``tripped``, ``trip_records``.

Edge substrate
--------------
Default (``use_handoff_edges=False``): the temporal agent-visitation sequence —
each event contributes its ``agent`` as a node; consecutive duplicate agents are
collapsed (self-loop suppression).

Optional (``use_handoff_edges=True``): for each event, after recording the
event's own agent node, an explicit hop to the parsed handoff target is inserted
if ``_parse_target(event.handoff_state)`` names a known agent.  This extends
the temporal graph with any explicit agent-routing information present in
``handoff_state``.  Falls back to the temporal edge when no parseable known
target is found.

This module is stdlib-only and defines no global mutable state.  Every call
builds and discards its own local bookkeeping tables.

Import DAG (acyclic)::

    looptrip.normalize
        ↑
    looptrip.detectors.types
        ↑
    looptrip.detectors._shared
        ↑
    looptrip.detectors.ping_pong   ← this module
        ↑
    looptrip.detector
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from looptrip.normalize import Event
from looptrip.detectors.types import (
    DetectionConfig,
    KIND_PING_PONG,
    PathologyReport,
    resolve_config,
)
from looptrip.detectors._shared import (
    _canonical_cycle,
    _is_progress,
    _is_terminal,
    _parse_target,
)


def detect_ping_pong(
    events: Iterable[Event],
    *,
    config: Optional[DetectionConfig] = None,
    **knobs,
) -> List[PathologyReport]:
    """Detect ping-pong / livelock cycles in an ordered event stream.

    A ping-pong cycle is a directed cycle of ``>= config.min_cycle_len``
    distinct agents that closes ``config.cycle_trip_count`` times within a
    single progress/terminal-free epoch.  The canonical directed cycle is
    computed as the *minimum rotation* of the cycle node sequence, so
    ``A→B→C`` and ``B→C→A`` map to the same key while ``A→C→B`` (the reverse
    direction) remains distinct.

    The detector is deliberately **token-independent**: it requires only
    ``event.agent``, ``event.progress``, ``event.handoff_state`` (for epoch
    resets and optional edge extraction), and ``event.cost_usd`` / ``event.raw_id``
    (for prevented-cost accounting and provenance).  It fires on the pure
    structural pattern of repeated cyclic dispatch, which is the documented
    Phase-1 blind spot closed by Phase 2.

    Args:
        events:  Ordered event stream (pre-sorted by ``(ts, raw_id)`` by the
                 caller; this function never re-sorts or mutates the input).
        config:  A pre-built :class:`~looptrip.detectors.types.DetectionConfig`,
                 or ``None`` to use defaults.
        **knobs: Ad-hoc field overrides applied on top of ``config`` via
                 :func:`~looptrip.detectors.types.resolve_config`.  For
                 example: ``detect_ping_pong(events, min_cycle_len=3)``.

    Returns:
        A list of :class:`~looptrip.detectors.types.PathologyReport` instances,
        one per distinct tripped directed cycle, in ascending trip-position
        order (i.e. in the order the trips were encountered in the stream).

    Report field population (per spec §2)
    --------------------------------------
    ``kind``            ``KIND_PING_PONG`` (``"ping_pong"``)
    ``signature``       The canonical directed cycle tuple, e.g.
                        ``("code-reviewer", "code-writer")``.
    ``agent``           ``trip_event.agent`` — the agent whose re-visit
                        triggered the final closure.
    ``occurrences``     Lifetime total closures of the cycle across the
                        entire stream (including across epoch resets).
    ``trip_index``      ``config.cycle_trip_count`` (the closure ordinal that
                        triggered the trip).
    ``trip_event``      The event that completed the ``cycle_trip_count``-th
                        closure (the tripping event).
    ``first_event``     The event that completed the 1st closure in the
                        current epoch.
    ``prevented_cost``  Sum of ``cost_usd`` over events strictly after the
                        trip position whose ``agent`` is in the cycle member
                        set.
    ``prevented_runs``  Count of those post-trip cycle-member events.
    ``members``         Same as ``signature`` — the canonical directed cycle
                        tuple (``members`` field populated for convenience).

    Example — the canonical two-agent livelock vector::

        from looptrip.normalize import Event
        from looptrip.detectors.ping_pong import detect_ping_pong

        def ev(agent, raw_id):
            return Event(agent=agent, tool="dispatch", args_hash=None,
                         ts=f"2024-01-01T00:00:0{raw_id}Z", raw_id=raw_id)

        stream = [ev("A", 0), ev("B", 1), ev("A", 2), ev("B", 3), ev("A", 4)]
        reports = detect_ping_pong(stream)
        assert len(reports) == 1
        assert reports[0].members == ("A", "B")
        assert reports[0].trip_event.raw_id == 4   # 5th event, index 4
        assert reports[0].first_event.raw_id == 2  # 3rd event, index 2
    """
    cfg = resolve_config(config, knobs)
    ev_list: List[Event] = list(events)

    if len(ev_list) < 2:
        return []

    # Pre-compute the union of all exempt agent names once; avoids rebuilding
    # the set on every cycle-close check.
    exempt_agents: frozenset = (
        cfg.idempotent_agents | cfg.retry_allowed | cfg.allowlist_agents
    )

    # ------------------------------------------------------------------
    # Build the node sequence: list of (agent_node, event, ev_list_index).
    #
    # Default (use_handoff_edges=False): one entry per event; agent_node is
    # always event.agent.
    #
    # Optional (use_handoff_edges=True): after each event's own node we
    # insert a synthetic node for the explicit handoff target (when it names
    # a known agent).  Both the synthetic entry and the originating event
    # share the same event object and ev_list_index so that epoch resets,
    # cost accounting, and provenance fields remain consistent.
    # ------------------------------------------------------------------
    if cfg.use_handoff_edges:
        known_agents: frozenset = frozenset(e.agent for e in ev_list)
        node_seq: List[tuple] = []
        for i, e in enumerate(ev_list):
            node_seq.append((e.agent, e, i))
            t = _parse_target(e.handoff_state)
            if t is not None and t in known_agents:
                # Explicit directed hop from e.agent to t; insert t as an
                # additional node before the next temporal event.
                node_seq.append((t, e, i))
    else:
        node_seq = [(e.agent, e, i) for i, e in enumerate(ev_list)]

    # ------------------------------------------------------------------
    # Epoch-scoped path-stack state (cleared on progress/terminal events).
    # ------------------------------------------------------------------
    path: List[str] = []                     # simple directed path; no repeated nodes
    pos: dict[str, int] = {}                 # agent → its index in path
    closures: dict[tuple, int] = {}          # canonical cycle key → epoch closure count
    first_closure: dict[tuple, Event] = {}   # key → event at the 1st epoch closure

    # ------------------------------------------------------------------
    # Lifetime state (persists across epoch resets for the whole call).
    # ------------------------------------------------------------------
    total_closures: dict[tuple, int] = {}    # key → lifetime closure count
    tripped: set[tuple] = set()              # keys that have already tripped
    # Each entry: (canonical_key, first_closure_event, trip_event, ev_list_index)
    trip_records: List[tuple] = []

    # ------------------------------------------------------------------
    # Main forward pass.
    # ------------------------------------------------------------------
    for v, e, ev_idx in node_seq:

        # --------------------------------------------------------------
        # 1. Epoch reset: a progress or terminal event clears the path.
        #    The triggering event is NOT added as a node; any synthetic
        #    node sharing the same event is likewise skipped.
        # --------------------------------------------------------------
        if _is_progress(e, cfg) or _is_terminal(e, cfg):
            path = []
            pos = {}
            closures = {}
            first_closure = {}
            continue

        # --------------------------------------------------------------
        # 2. Self-loop collapse: consecutive identical agents are not a
        #    hop and never form a ping-pong cycle.
        # --------------------------------------------------------------
        if path and path[-1] == v:
            continue

        # --------------------------------------------------------------
        # 3. Close or Push.
        #
        # Close: v is already on the path at index j, so the suffix
        #   path[j:] forms a directed cycle back to v.  Count the
        #   closure; trip if the cycle_trip_count threshold is reached
        #   (and the cycle is not fully exempt).
        #   Always unwind the path back to j (regardless of cycle length)
        #   so that v remains the tail and future events can extend from it.
        #
        # Push: v is new; extend the path.
        # --------------------------------------------------------------
        if v in pos:
            j = pos[v]
            loop = path[j:]   # suffix from j inclusive; this is the cycle

            if len(loop) >= cfg.min_cycle_len:
                key: tuple = _canonical_cycle(loop)
                total_closures[key] = total_closures.get(key, 0) + 1
                closures[key] = closures.get(key, 0) + 1

                if closures[key] == 1:
                    # Record the event that first completed this cycle in
                    # the current epoch; becomes first_event if we trip.
                    first_closure[key] = e

                # Trip on the cycle_trip_count-th closure within the
                # current epoch, provided the cycle is not fully exempt.
                fully_exempt = all(a in exempt_agents for a in loop)
                if (
                    key not in tripped
                    and closures[key] == cfg.cycle_trip_count
                    and not fully_exempt
                ):
                    tripped.add(key)
                    trip_records.append((key, first_closure[key], e, ev_idx))

            # Unwind: remove every node after j from the path; v stays
            # as the head at position j.  This is correct whether or not
            # the loop was long enough to count.
            for a in path[j + 1:]:
                del pos[a]
            path = path[:j + 1]
            # v is already in pos at index j — no update needed.

        else:
            # Push v as a new node at the end of the path.
            path.append(v)
            pos[v] = len(path) - 1

    # ------------------------------------------------------------------
    # Build PathologyReport objects from trip_records.
    # trip_records are in ascending ev_idx order (encounter order), so
    # the output list is already in ascending trip-position order.
    # ------------------------------------------------------------------
    reports: List[PathologyReport] = []
    for key, fc, te, trip_idx in trip_records:
        members: tuple = key           # canonical directed cycle tuple
        occurrences: int = total_closures[key]
        member_set: set = set(key)

        # Prevented cost: events strictly after the trip position whose
        # agent is in the cycle member set (the "kill-at-trip" model).
        post: List[Event] = [
            ev for ev in ev_list[trip_idx + 1:] if ev.agent in member_set
        ]
        prevented_cost: float = sum(ev.cost_usd or 0.0 for ev in post)
        prevented_runs: int = len(post)

        detail = (
            f"Ping-pong cycle {list(members)!r} closed {occurrences} time(s) "
            f"(lifetime); tripped at the {cfg.cycle_trip_count}-th closure "
            f"within the current epoch (trip raw_id={te.raw_id!r}); "
            f"{prevented_runs} post-trip cycle-member dispatch(es) "
            f"worth ${prevented_cost:.4f} would have been averted."
        )
        reports.append(
            PathologyReport(
                kind=KIND_PING_PONG,
                signature=members,
                agent=te.agent,
                occurrences=occurrences,
                trip_index=cfg.cycle_trip_count,
                trip_event=te,
                first_event=fc,
                prevented_cost=prevented_cost,
                prevented_runs=prevented_runs,
                detail=detail,
                members=members,
            )
        )

    return reports
