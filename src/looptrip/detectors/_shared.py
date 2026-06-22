"""detectors/_shared.py — stateless parsing and graph primitives shared by all
Phase-2 detectors.

Every function in this module is pure and stateless: no module-level mutable
state is defined, and no function modifies its arguments.  The helpers here
form the shared substrate on which ``ping_pong``, ``deadlock``, and
``non_termination`` are built.

The module provides two categories of primitives:

**Event predicates** — fast boolean classification of a single :class:`~looptrip.normalize.Event`
against a :class:`~looptrip.detectors.types.DetectionConfig`:

* :func:`_is_progress` — marks an epoch reset trigger (progress flag or
  ``handoff_state`` is a declared progress marker).
* :func:`_is_terminal` — marks an epoch-end trigger (``handoff_state`` is a
  declared terminal state).
* :func:`_is_exempt` — marks an event whose agent or tool is exempt from new
  detectors (union of all five exemption sets).
* :func:`_is_blocked` — marks an event whose bare ``handoff_state`` token is a
  declared blocked-state (case-insensitive on both sides).  The wait-for
  TARGET is no longer parsed here — detectors read ``event.to_agent`` directly.
* :func:`_state_key` — extracts the configured state identity of an event for
  non-termination windowing.

**Graph primitives** — cycle canonicalization:

* :func:`_canonical_cycle` — direction-preserving minimum rotation of a cycle
  sequence.  A→B→C and A→C→B remain distinct (never reversed).

Import DAG (acyclic, verified)::

    looptrip.normalize
        ↑
    looptrip.detectors.types        ← provides DetectionConfig
        ↑
    looptrip.detectors._shared      ← this module
        ↑
    looptrip.detectors.{ping_pong, deadlock, non_termination}
        ↑
    looptrip.detector

This module is stdlib-only and defines no global mutable state.
"""

from __future__ import annotations

from typing import Any, Optional

from looptrip.normalize import Event
from looptrip.detectors.types import DetectionConfig


# ---------------------------------------------------------------------------
# Event predicates
# ---------------------------------------------------------------------------


def _is_progress(event: Event, config: DetectionConfig) -> bool:
    """Return ``True`` if ``event`` represents a progress / state-advance delta.

    An event is considered a progress event when EITHER:

    * ``event.progress`` is ``True`` (the explicit flag), OR
    * ``event.handoff_state`` is not ``None`` and its value is in
      ``config.progress_markers`` (a declared handoff_state string that
      counts as a progress delta even when the ``progress`` flag is ``False``).

    This predicate drives epoch resets in :mod:`looptrip.detectors.ping_pong`
    and window breaks in :mod:`looptrip.detectors.non_termination`.

    Args:
        event:  The event to classify.
        config: The active detection configuration, supplying
                :attr:`~looptrip.detectors.types.DetectionConfig.progress_markers`.

    Returns:
        ``True`` iff the event should be treated as a progress delta.
    """
    return event.progress or (
        event.handoff_state is not None
        and event.handoff_state in config.progress_markers
    )


def _is_terminal(event: Event, config: DetectionConfig) -> bool:
    """Return ``True`` if ``event`` signals that the agent's session has ended.

    An event is terminal when its ``handoff_state`` is not ``None`` and its
    value is in ``config.terminal_states``.  Terminal events are epoch-reset
    triggers in :mod:`looptrip.detectors.ping_pong` (same effect as progress)
    and window-break triggers in :mod:`looptrip.detectors.non_termination`.

    The default :attr:`~looptrip.detectors.types.DetectionConfig.terminal_states`
    is an empty frozenset; callers (e.g. CAST) should pass
    ``frozenset({"DONE", "DONE_WITH_CONCERNS"})`` to enable terminal
    classification.

    Args:
        event:  The event to classify.
        config: The active detection configuration, supplying
                :attr:`~looptrip.detectors.types.DetectionConfig.terminal_states`.

    Returns:
        ``True`` iff the event's ``handoff_state`` is a declared terminal
        state.
    """
    return (
        event.handoff_state is not None
        and event.handoff_state in config.terminal_states
    )


def _is_exempt(event: Event, config: DetectionConfig) -> bool:
    """Return ``True`` if ``event`` is exempt from the new Phase-2 detectors.

    An event is exempt when EITHER:

    * ``event.agent`` is in the union of
      :attr:`~looptrip.detectors.types.DetectionConfig.idempotent_agents`,
      :attr:`~looptrip.detectors.types.DetectionConfig.retry_allowed`, and
      :attr:`~looptrip.detectors.types.DetectionConfig.allowlist_agents`, OR
    * ``event.tool`` is in the union of
      :attr:`~looptrip.detectors.types.DetectionConfig.idempotent_tools` and
      :attr:`~looptrip.detectors.types.DetectionConfig.allowlist_tools`.

    This unified exemption check covers all five config-level exemption sets.
    Note that the Phase-1 ``duplicate_work`` detector applies only
    ``idempotent_agents`` directly (via its own ``idempotent_agents`` kwarg)
    and is unaffected by this function.

    Args:
        event:  The event to classify.
        config: The active detection configuration.

    Returns:
        ``True`` iff the event should be skipped by the new detectors.
    """
    exempt_agents = (
        config.idempotent_agents | config.retry_allowed | config.allowlist_agents
    )
    exempt_tools = config.idempotent_tools | config.allowlist_tools
    return event.agent in exempt_agents or event.tool in exempt_tools


def _is_blocked(handoff_state: Optional[str], config: DetectionConfig) -> bool:
    """Return ``True`` if ``handoff_state`` is a declared blocked-state token.

    Operates on the BARE state token only: under the explicit-``to_agent``
    contract, ``handoff_state`` carries just the state word (``"blocked"``,
    ``"waiting"``, ``"DONE"``, …) and never a packed ``"blocked on target"``
    string.  The wait-for target lives in ``event.to_agent``; this predicate
    does NO delimiter scanning.

    The comparison is CASE-INSENSITIVE on BOTH sides: the stripped, lowercased
    ``handoff_state`` is tested against the lowercased members of
    :attr:`~looptrip.detectors.types.DetectionConfig.blocked_states`.  This lets
    a caller pass either casing on either side (e.g. ``blocked_states={"STALLED"}``
    matches ``handoff_state="stalled"`` and vice versa).

    Returns ``False`` for a falsy ``handoff_state`` (``None`` or empty string).

    Args:
        handoff_state: The bare state token to classify, or ``None``.
        config:        The active detection configuration, supplying
                       :attr:`~looptrip.detectors.types.DetectionConfig.blocked_states`.

    Returns:
        ``True`` iff ``handoff_state`` names a declared blocked state.
    """
    return bool(handoff_state) and handoff_state.strip().lower() in {
        s.lower() for s in config.blocked_states
    }


def _state_key(event: Event, config: DetectionConfig) -> Any:
    """Return the configured state-identity value for ``event``.

    Dispatches on :attr:`~looptrip.detectors.types.DetectionConfig.state_key`:

    * ``"signature"`` → ``event.signature()`` — the ``(agent, tool, args_hash)``
      triple.  This is the default and works even when ``handoff_state`` is
      ``None`` everywhere.
    * ``"agent"``     → ``event.agent`` — coarser grouping; collapses all
      dispatches by the same agent into one state identity.
    * ``"handoff_state"`` → ``event.handoff_state`` — ``None`` is a legal,
      distinct key; all events without a handoff state share one state bucket.

    Used by :mod:`looptrip.detectors.non_termination` to populate the sliding
    window's state counter.

    Args:
        event:  The event whose state key is required.
        config: The active detection configuration, supplying
                :attr:`~looptrip.detectors.types.DetectionConfig.state_key`.

    Returns:
        The state identity value (type depends on ``config.state_key``).

    Raises:
        ValueError: If ``config.state_key`` is not one of the three valid
            options (guarded by :meth:`~looptrip.detectors.types.DetectionConfig.__post_init__`
            at config construction time, so this should never fire in
            practice).
    """
    sk = config.state_key
    if sk == "signature":
        return event.signature()
    if sk == "agent":
        return event.agent
    if sk == "handoff_state":
        return event.handoff_state
    # Belt-and-suspenders: __post_init__ should have caught this already.
    raise ValueError(
        f"state_key must be one of 'signature', 'agent', 'handoff_state'; "
        f"got {sk!r}"
    )


# ---------------------------------------------------------------------------
# Graph / cycle primitives
# ---------------------------------------------------------------------------


def _canonical_cycle(seq: list) -> tuple:
    """Return the direction-preserving minimum rotation of ``seq``.

    Given a directed cycle expressed as a sequence of node labels, returns the
    lexicographically smallest rotation — i.e. the rotation starting from the
    "smallest" node label.  Direction is NEVER reversed: ``A→B→C`` and
    ``A→C→B`` are distinct directed cycles and will produce distinct canonical
    keys::

        _canonical_cycle(["A", "B", "C"]) == ("A", "B", "C")
        _canonical_cycle(["B", "C", "A"]) == ("A", "B", "C")  # same cycle
        _canonical_cycle(["A", "C", "B"]) == ("A", "C", "B")  # opposite dir

    This is the standard "minimum rotation" approach with O(n²) string
    comparison, appropriate for the small cycle sizes expected in multi-agent
    pathology detection.

    Args:
        seq: An ordered list of node labels forming one directed cycle.
             Must be non-empty; behaviour is undefined for empty input.

    Returns:
        The minimum rotation of ``seq`` as a tuple.
    """
    n = len(seq)
    return min(tuple(seq[k:] + seq[:k]) for k in range(n))
