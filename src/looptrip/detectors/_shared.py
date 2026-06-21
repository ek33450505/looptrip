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
* :func:`_state_key` — extracts the configured state identity of an event for
  non-termination windowing.

**Graph primitives** — cycle canonicalization and blocked-state parsing:

* :func:`_canonical_cycle` — direction-preserving minimum rotation of a cycle
  sequence.  A→B→C and A→C→B remain distinct (never reversed).
* :func:`_parse_target` — extracts the agent name following the first
  recognised delimiter in a ``handoff_state`` string.
* :func:`_parse_blocked` — parses a ``handoff_state`` into a
  :class:`BlockedWait` when its leading word is a declared blocked-state
  token (case-insensitive); returns ``None`` otherwise.

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

import typing
from typing import Any, Optional

from looptrip.normalize import Event
from looptrip.detectors.types import DetectionConfig


# ---------------------------------------------------------------------------
# BlockedWait — the parsed result of a blocked-state handoff string
# ---------------------------------------------------------------------------

BlockedWait = typing.NamedTuple("BlockedWait", [("target", Optional[str])])
"""The parsed result of a blocked ``handoff_state`` string.

A :class:`BlockedWait` is produced by :func:`_parse_blocked` for any
``handoff_state`` whose leading word matches a declared
:attr:`~looptrip.detectors.types.DetectionConfig.blocked_states` token.  It
carries the downstream agent being waited on, or ``None`` when the
``handoff_state`` signals blocking without naming a specific target (no
directed wait-for edge).

Attributes:
    target: The agent being waited on, or ``None`` when no explicit target
            is present.  Extracted by :func:`_parse_target`.
"""


# ---------------------------------------------------------------------------
# Internal delimiter table (private)
# ---------------------------------------------------------------------------

# The spec defines four recognised delimiters; we must identify the one that
# appears at the EARLIEST POSITION in the string (not the one listed first).
_DELIMITERS: tuple = ("=", ":", " on ", " to ")
"""Delimiter search set for :func:`_parse_target` / :func:`_parse_blocked`.

The priority rule is positional: whichever delimiter occurs at the smallest
character index wins.  This tuple is only the search set; the implementation
scans all four and picks the winner by index.

Agent names may contain hyphens — ``"-"`` is never in this set.
"""


def _find_first_delimiter(s: str) -> tuple:
    """Return ``(first_pos, first_delim)`` for the earliest delimiter in ``s``.

    Scanning is CASE-INSENSITIVE: ``s.lower()`` is searched against each
    delimiter in :data:`_DELIMITERS` (which are already lowercase).  The
    returned ``first_pos`` is an index into the ORIGINAL string ``s``, so
    callers can slice ``s`` directly to preserve its original casing.

    Returns ``(None, None)`` when no recognised delimiter is found.

    .. note::
        Index alignment between ``s.lower()`` and ``s`` relies on the ASCII
        assumption that ``.lower()`` preserves string length — true for all
        agent-name / handoff-state vocabulary in scope (ASCII identifiers and
        the fixed word delimiters ``" on "`` / ``" to "``).

    Args:
        s: The string to scan (not lowercased beforehand — this function
           handles that internally).

    Returns:
        A 2-tuple ``(first_pos, first_delim)`` where ``first_pos`` is the
        character index in ``s`` where the delimiter starts, and
        ``first_delim`` is the matched delimiter string.  Both elements are
        ``None`` when no delimiter was found.
    """
    s_lower = s.lower()
    first_pos: Optional[int] = None
    first_delim: Optional[str] = None
    for delim in _DELIMITERS:
        idx = s_lower.find(delim)
        if idx != -1 and (first_pos is None or idx < first_pos):
            first_pos = idx
            first_delim = delim
    return (first_pos, first_delim)


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


# ---------------------------------------------------------------------------
# Blocked-state parsing
# ---------------------------------------------------------------------------


def _parse_target(handoff_state: Optional[str]) -> Optional[str]:
    """Extract the wait-for agent from a blocked ``handoff_state`` string.

    Scans ``handoff_state`` for the earliest occurrence of any recognised
    delimiter (``"="``, ``":"``, ``" on "``, ``" to "``) — CASE-INSENSITIVELY
    — and returns the stripped text that follows it, preserving the original
    casing of the returned agent name.  Returns ``None`` when no delimiter is
    found.

    Agent names may contain hyphens (e.g. ``"code-writer"``); the function
    NEVER splits on ``"-"``.

    Example extractions::

        _parse_target("blocked on code-writer")   # → "code-writer"
        _parse_target("BLOCKED ON code-writer")   # → "code-writer"
        _parse_target("BLOCKED: agent-x")         # → "agent-x"
        _parse_target("waiting=orchestrator")     # → "orchestrator"
        _parse_target("WAITING TO commit-agent")  # → "commit-agent"
        _parse_target("BLOCKED ON Code-Writer")   # → "Code-Writer"  (original case)
        _parse_target("BLOCKED")                  # → None  (no delimiter)
        _parse_target(None)                       # → None

    Args:
        handoff_state: The raw ``handoff_state`` string to parse, or ``None``.

    Returns:
        The agent name after the first delimiter, stripped with original casing,
        or ``None`` when no delimiter is present.
    """
    if not handoff_state:
        return None

    # Find the earliest delimiter using case-insensitive scan (via the helper).
    # first_pos is an index into the ORIGINAL handoff_state string.
    first_pos, first_delim = _find_first_delimiter(handoff_state)

    if first_pos is None or first_delim is None:
        return None

    # Slice from the ORIGINAL string to preserve the agent name's original case.
    remainder = handoff_state[first_pos + len(first_delim):]
    return remainder.strip() or None


def _parse_blocked(
    handoff_state: Optional[str],
    config: DetectionConfig,
) -> Optional[BlockedWait]:
    """Parse ``handoff_state`` into a :class:`BlockedWait` when it signals blocking.

    The leading word of ``handoff_state`` (the text before the first recognised
    delimiter, or the whole string when no delimiter is present) is compared
    CASE-INSENSITIVELY against
    :attr:`~looptrip.detectors.types.DetectionConfig.blocked_states`.  When it
    matches, returns ``BlockedWait(target=_parse_target(handoff_state))``; the
    target may be ``None`` when the agent is blocked without naming a specific
    waiter (no directed edge in the wait-for graph).

    Delimiter scanning is CASE-INSENSITIVE: ``"BLOCKED ON x"`` and
    ``"Blocked On x"`` are both recognised (the space-word delimiters
    ``" on "`` / ``" to "`` match regardless of case in the input).

    Returns ``None`` when:

    * ``handoff_state`` is falsy (``None``, empty string), OR
    * the leading word is not in
      ``{s.lower() for s in config.blocked_states}``.

    The default
    :attr:`~looptrip.detectors.types.DetectionConfig.blocked_states` is
    ``frozenset({"blocked", "waiting"})``, matched case-insensitively so that
    CAST's uppercase ``"BLOCKED"`` token matches without leaking CAST-specific
    vocabulary into the generic core.

    Examples::

        cfg = DetectionConfig()
        _parse_blocked("blocked on code-writer", cfg)
        # → BlockedWait(target="code-writer")

        _parse_blocked("BLOCKED: agent-x", cfg)
        # → BlockedWait(target="agent-x")

        _parse_blocked("BLOCKED", cfg)
        # → BlockedWait(target=None)  (blocked; no named target)

        _parse_blocked("DONE", cfg)
        # → None  (not a blocked state)

        _parse_blocked(None, cfg)
        # → None

    Args:
        handoff_state: The raw ``handoff_state`` string to parse, or ``None``.
        config:        The active detection configuration, supplying
                       :attr:`~looptrip.detectors.types.DetectionConfig.blocked_states`.

    Returns:
        A :class:`BlockedWait` when the event is blocked, otherwise ``None``.
    """
    if not handoff_state:
        return None

    # Extract the leading word: text before the first delimiter (or whole
    # string when no delimiter is present).  The scan is CASE-INSENSITIVE
    # via _find_first_delimiter; first_pos indexes into the ORIGINAL string.
    first_pos, _ = _find_first_delimiter(handoff_state)

    if first_pos is None:
        leading = handoff_state
    else:
        leading = handoff_state[:first_pos]

    blocked_set = {s.lower() for s in config.blocked_states}
    if leading.strip().lower() not in blocked_set:
        return None

    return BlockedWait(target=_parse_target(handoff_state))
