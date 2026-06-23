"""detectors/non_termination.py — the non-termination / never-terminate detector.

Detects a *bounded-liveness* failure: an agent stream whose state-key
distribution *plateaus* — the count of distinct state identities stays at or
below a low cap while the event count keeps growing.  This fires
token-independently, so it closes the single-agent flavor of the Phase-1
blind spot where a high-token-variance runaway evades ``detect_duplicate_work``.

The algorithm is a single left-to-right slide of a window of ``N`` events
(``N = config.window_size``) with O(1)-per-step incremental counters.  A full
window qualifies when ALL of the following hold:

1. The number of distinct state keys is at or below ``cap`` (low variety →
   the stream is not advancing through new states).
2. No event in the window is a progress marker (``progress_count == 0``).
3. No event in the window is a terminal-state event (``terminal_count == 0``).
4. Not ALL events in the window are exempt (``exempt_count < N``).

``cap`` is ``config.plateau_unique_states`` when that override is set;
otherwise ``max(1, floor(config.window_size * config.plateau_ratio))``.

One :class:`~looptrip.detectors.types.PathologyReport` is emitted per
**maximal contiguous run** of qualifying windows — a 10 000-event loop yields
exactly ONE report (not thousands).

The state key is determined by
:attr:`~looptrip.detectors.types.DetectionConfig.state_key`:

* ``"signature"`` (default) — ``event.signature()``; works fully when
  ``handoff_state`` is ``None`` everywhere (the cast.db reality).
* ``"agent"``               — coarser; collapses all dispatches by the same
  agent into one state identity.
* ``"handoff_state"``       — ``None`` is a legal, distinct key; all events
  without a handoff state share one state bucket.

Import DAG (acyclic, verified)::

    looptrip.normalize
        ↑
    looptrip.detectors.types
        ↑
    looptrip.detectors._shared
        ↑
    looptrip.detectors.non_termination   ← this module
        ↑
    looptrip.detector

This module is stdlib-only and defines no global mutable state.
"""

from __future__ import annotations

import math
from collections import Counter, deque
from typing import Iterable, List, Optional

from looptrip.normalize import Event
from looptrip.detectors.types import (
    DetectionConfig,
    KIND_NON_TERMINATION,
    PathologyReport,
    resolve_config,
)
from looptrip.detectors._shared import (
    _is_progress,
    _is_terminal,
    _state_key,
)


def detect_non_termination(
    events: Iterable[Event],
    *,
    config: Optional[DetectionConfig] = None,
    **knobs,
) -> List[PathologyReport]:
    """Detect non-termination / never-terminate runaways in an ordered event stream.

    Slides a window of ``config.window_size`` events left-to-right over the
    stream with O(1)-per-step incremental counters.  A full window qualifies
    when the distinct-state count is at or below ``cap`` with no progress,
    terminal, or all-exempt events inside it.  Contiguous qualifying windows are
    merged into a single :class:`~looptrip.detectors.types.PathologyReport`;
    a 10 000-event loop yields exactly one report.

    This detector is **token-independent**: a runaway single agent with
    high-variance token counts (the Phase-1 blind spot) is detected because
    its ``signature()`` is constant, meaning ``distinct == 1`` throughout the
    window.

    The function resolves its own config via
    :func:`~looptrip.detectors.types.resolve_config` so it is independently
    callable::

        reports = detect_non_termination(events, window_size=5)
        reports = detect_non_termination(
            events, config=DetectionConfig(window_size=10)
        )

    Events are processed in the order given.  The caller is responsible for
    pre-sorting by ``(ts, raw_id)``.  The input ``events`` iterable is
    materialized once into a list (required for index-based window accounting
    and post-trip slicing).

    Args:
        events: Ordered stream of :class:`~looptrip.normalize.Event` objects.
        config: Base :class:`~looptrip.detectors.types.DetectionConfig`, or
                ``None`` to use the default.
        **knobs: Ad-hoc field overrides applied on top of ``config`` via
                 :func:`~looptrip.detectors.types.resolve_config`.

    Returns:
        A list of :class:`~looptrip.detectors.types.PathologyReport` objects,
        one per distinct maximal plateau run found in the stream.  Returns
        ``[]`` when:

        * the stream has fewer than ``window_size`` events,
        * every full window has more distinct states than ``cap``,
        * or every qualifying window is broken by a progress or terminal event.

    Raises:
        ValueError: Raised (via :func:`~looptrip.detectors.types.resolve_config`
            → :class:`~looptrip.detectors.types.DetectionConfig.__post_init__`)
            when knobs produce an invalid config (e.g. ``window_size=0``).
        TypeError: Raised by :func:`~looptrip.detectors.types.resolve_config`
            when ``knobs`` contains an unrecognized field name.
    """
    cfg = resolve_config(config, knobs)
    # Materialize once.  detect() already passes a list (the shared
    # ``materialized`` copy), so the common registry path skips a redundant
    # re-copy; a generator / other-iterable caller is still materialized here
    # exactly once.  The list is required for index-based window accounting and
    # post-trip slicing, and is never mutated, so sharing the caller's
    # reference is safe.
    evs: List[Event] = events if isinstance(events, list) else list(events)
    N = cfg.window_size

    if len(evs) < N:
        return []

    # Determine the unique-state cap.
    if cfg.plateau_unique_states is not None:
        cap: int = cfg.plateau_unique_states
    else:
        cap = max(1, math.floor(N * cfg.plateau_ratio))

    # Sliding-window state.
    #
    # Each deque entry is a 5-tuple so the outgoing (leftmost) event can be
    # removed without consulting the original event list:
    #   (event, state_key, is_progress, is_terminal, is_exempt)
    #
    # cnt   — Counter of state-key values currently in the window.
    # *_count — running per-condition tallies; updated in O(1) on each slide.
    win: deque = deque()
    cnt: Counter = Counter()
    progress_count: int = 0
    terminal_count: int = 0
    exempt_count: int = 0

    # run_start      — index `i` (last position of the first qualifying window)
    #                  when a plateau run is open; None otherwise.
    # run_start_unique — distinct count at the moment the run was opened.
    run_start: Optional[int] = None
    run_start_unique: Optional[int] = None

    reports: List[PathologyReport] = []

    # Pre-compute the exemption unions ONCE (mirrors ping_pong's exempt_agents
    # precompute).  Calling _shared._is_exempt per event would rebuild both
    # frozenset unions on every slide; the inline membership test below is
    # identical to _is_exempt's ``agent in exempt_agents or tool in
    # exempt_tools`` and yields the same boolean.
    exempt_agents: frozenset = (
        cfg.idempotent_agents | cfg.retry_allowed | cfg.allowlist_agents
    )
    exempt_tools: frozenset = cfg.idempotent_tools | cfg.allowlist_tools

    for i, ev in enumerate(evs):
        sk = _state_key(ev, cfg)
        is_prog = _is_progress(ev, cfg)
        is_term = _is_terminal(ev, cfg)
        is_ex = ev.agent in exempt_agents or ev.tool in exempt_tools

        # Slide: evict the oldest entry when the window is already at full
        # capacity (i.e. we have seen exactly N events already).
        if len(win) == N:
            _old_ev, old_sk, old_prog, old_term, old_ex = win.popleft()
            cnt[old_sk] -= 1
            if cnt[old_sk] == 0:
                del cnt[old_sk]
            if old_prog:
                progress_count -= 1
            if old_term:
                terminal_count -= 1
            if old_ex:
                exempt_count -= 1

        # Admit the incoming event.
        win.append((ev, sk, is_prog, is_term, is_ex))
        cnt[sk] += 1
        if is_prog:
            progress_count += 1
        if is_term:
            terminal_count += 1
        if is_ex:
            exempt_count += 1

        # Qualification applies only to full windows (first full window ends
        # at index N-1).
        if i < N - 1:
            continue

        distinct = len(cnt)
        qualifies = (
            distinct <= cap
            and progress_count == 0
            and terminal_count == 0
            and exempt_count < N
        )

        if qualifies:
            if run_start is None:
                # Open a new plateau run at this window position.
                run_start = i
                run_start_unique = distinct
        else:
            if run_start is not None:
                # The run closed at the previous window position.
                run_end = i - 1
                reports.append(
                    _make_report(evs, cfg, run_start, run_end, run_start_unique, N)
                )
                run_start = None
                run_start_unique = None

    # End-of-stream: close any open plateau run.
    if run_start is not None:
        run_end = len(evs) - 1
        reports.append(
            _make_report(evs, cfg, run_start, run_end, run_start_unique, N)
        )

    return reports


def _make_report(
    evs: List[Event],
    cfg: DetectionConfig,
    p: int,
    run_end: int,
    unique_states: Optional[int],
    N: int,
) -> PathologyReport:
    """Build one :class:`~looptrip.detectors.types.PathologyReport` for a plateau run.

    Called once per maximal contiguous run of qualifying windows.  All index
    arithmetic follows the spec (§4c / §2 of the locked spec):

    * ``p``        — last index of the FIRST qualifying window (the trip point).
    * ``run_end``  — last index of the LAST qualifying window.
    * ``p - N + 1``— start index of the first qualifying window (``first_event``).

    Args:
        evs:          The full materialized event list (0-indexed).
        cfg:          The active detection configuration.
        p:            Index of the last event in the first qualifying window.
        run_end:      Index of the last event in the last qualifying window.
        unique_states: Distinct-state count recorded when the run was opened
                      (``run_start_unique``).
        N:            ``cfg.window_size``.

    Returns:
        One :class:`~looptrip.detectors.types.PathologyReport` for the run.
    """
    trip_event: Event = evs[p]
    first_event: Event = evs[p - N + 1]

    # window = (start_index, end_index_exclusive, unique_states, window_size)
    window_start: int = p - N + 1
    window: tuple = (window_start, p + 1, unique_states, N)

    # occurrences: events from the window start through the run end, inclusive.
    occurrences: int = run_end - window_start + 1

    # prevented cost/runs: events strictly after the trip point through run end.
    post: List[Event] = evs[p + 1 : run_end + 1]
    prevented_cost: float = sum(ev.cost_usd or 0.0 for ev in post)
    prevented_runs: int = len(post)

    # signature encodes both the key selector and the value at the trip event.
    sk_value = _state_key(trip_event, cfg)
    signature: tuple = (cfg.state_key, sk_value)

    detail: str = (
        f"non-termination plateau detected: {trip_event.agent!r} repeated "
        f"state key {sk_value!r} (selector={cfg.state_key!r}) with "
        f"{unique_states} distinct state(s) across a window of {N} events; "
        f"maximal plateau run spans {occurrences} event(s) from index "
        f"{window_start} to {run_end} (raw_id={trip_event.raw_id})."
    )

    return PathologyReport(
        kind=KIND_NON_TERMINATION,
        signature=signature,
        agent=trip_event.agent,
        occurrences=occurrences,
        trip_index=N,
        trip_event=trip_event,
        first_event=first_event,
        prevented_cost=prevented_cost,
        prevented_runs=prevented_runs,
        detail=detail,
        window=window,
    )
