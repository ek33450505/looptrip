"""detectors/types.py — shared types at the bottom of the detection DAG.

This module is the single source of truth for the data structures that every
detector produces or consumes.  It deliberately sits at the bottom of the
import graph so that the new per-detector modules (``ping_pong``,
``deadlock``, ``non_termination``) can construct :class:`PathologyReport`
instances without importing :mod:`looptrip.detector` — which would create a
circular import because ``detector.py`` imports the per-detector functions.

Import DAG (acyclic, verified)::

    looptrip.normalize
        ↑
    looptrip.detectors.types          ← this module
        ↑
    looptrip.detectors._shared
        ↑
    looptrip.detectors.{ping_pong, deadlock, non_termination}
        ↑
    looptrip.detector

No module below ``detector.py`` on this chain imports anything *above* it.

This module is stdlib-only and defines no global mutable state.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, List, Optional

from looptrip.normalize import Event


# ---------------------------------------------------------------------------
# KIND constants — the canonical family labels for pathology reports
# ---------------------------------------------------------------------------

KIND_DUPLICATE_WORK: str = "duplicate_work"
"""Duplicate-work / iteration-2 pathology: same signature repeated with no
progress delta between occurrences.  The Phase-1 detector."""

KIND_PING_PONG: str = "ping_pong"
"""Ping-pong / livelock pathology: two or more agents cycling indefinitely
with no net state advance — A→B→A→B→… with no progress events."""

KIND_DEADLOCK: str = "deadlock"
"""Deadlock pathology: mutually blocked agents, each waiting on another,
none able to make progress — a directed wait-for cycle in the agent graph."""

KIND_NON_TERMINATION: str = "non_termination"
"""Non-termination / never-terminate pathology: an agent (or group) whose
state-key distribution plateaus while the event count keeps growing — a
token-independent surrogate for an unbounded loop with no terminal state."""

ALL_DETECTORS: tuple = (
    KIND_DUPLICATE_WORK,
    KIND_PING_PONG,
    KIND_DEADLOCK,
    KIND_NON_TERMINATION,
)
"""Canonical ordered tuple of every detector kind, used to drive the
registry in a deterministic, reproducible sweep.  Order matters:
``duplicate_work`` first preserves the Phase-1 primary sort; ``ping_pong``,
``deadlock``, and ``non_termination`` follow in ascending specificity."""


# ---------------------------------------------------------------------------
# PathologyReport — the single output type of every detector
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PathologyReport:
    """A single confirmed pathology, anchored at its trip point.

    The first ten fields are shared by every detector kind and are always
    populated (so that generic consumers — ``proof.py``, ``cli.py`` — can
    read ``agent``, ``occurrences``, ``first_event``, ``trip_event``,
    ``prevented_runs``, ``prevented_cost`` without switching on ``kind``).
    Three trailing fields carry per-kind supplemental data and default to
    empty / ``None`` so that legacy code constructing a report with exactly
    the original ten keyword arguments continues to work without modification.

    Attributes:
        kind:           Pathology family — one of the ``KIND_*`` constants.
        signature:      The offending identity key.  For ``duplicate_work``:
                        ``(agent, tool, args_hash)``; for ``ping_pong``: the
                        canonical directed cycle tuple; for ``deadlock``:
                        ``tuple(sorted(members))``; for ``non_termination``:
                        ``(state_key_name, state_key_value)``.
        agent:          The acting agent most directly tied to the trip.
        occurrences:    Total events associated with the pathology in the
                        stream (kind-specific count; see §2 of the spec).
        trip_index:     1-based ordinal of the tripping event within the
                        pathology's own sequence (``== threshold`` for
                        ``duplicate_work``; ``== cycle_trip_count`` for
                        ``ping_pong``; ``1`` for ``deadlock``; ``==
                        window_size`` for ``non_termination``).
        trip_event:     The event that tripped the detector.
        first_event:    The first event associated with the pathology.
        prevented_cost: Sum of ``cost_usd`` over events strictly after the
                        trip event that would have been averted by acting at
                        the trip point.  ``0.0`` for ``deadlock`` (a hang,
                        not recurring spend).
        prevented_runs: Count of those post-trip events (``0`` for
                        ``deadlock``).
        detail:         A concise human-readable sentence describing the
                        trip.

        members:        Kind-specific auxiliary sequence (trailing; defaults
                        to ``()`` so legacy 10-field construction still
                        works).
                        ``ping_pong``: the canonical DIRECTED cycle in visit
                        order, e.g. ``("code-reviewer", "code-writer")``
                        (order significant — direction-preserving min-
                        rotation canonicalisation).
                        ``deadlock``: the SORTED member tuple (order not
                        significant; all members are co-equal).
                        ``()`` for ``duplicate_work`` and
                        ``non_termination``.

        blocked_agents: Deadlock ONLY — the :class:`frozenset` of mutually
                        blocked agent names forming the wait-for cycle.
                        ``None`` for all other kinds.  (Trailing; defaults
                        to ``None``.)

        window:         Non-termination ONLY — the 4-tuple
                        ``(start_index, end_index_exclusive, unique_states,
                        window_size)`` identifying the qualifying plateau
                        window.  ``None`` for all other kinds.  (Trailing;
                        defaults to ``None``.)
    """

    # --- required fields (10 original; must remain first in declaration order) ---
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

    # --- trailing supplemental fields (defaulted; backward-compatible) ---
    members: tuple = ()
    blocked_agents: Optional[frozenset] = None
    window: Optional[tuple] = None


# ---------------------------------------------------------------------------
# DetectionConfig — unified sensitivity knobs for all four detectors
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DetectionConfig:
    """Immutable sensitivity configuration for all four pathology detectors.

    All fields have safe defaults so ``DetectionConfig()`` is immediately
    usable.  The Phase-1 legacy knobs (``token_tolerance``, ``threshold``,
    ``idempotent_agents``) keep their original names and defaults so that
    existing callers can switch to config-object style without any value
    change.

    Validation is performed in :meth:`__post_init__`; invalid combinations
    raise :class:`ValueError` at construction time, not at detection time.

    Duplicate-work legacy fields
    ----------------------------
    token_tolerance : float
        Maximum fractional difference in ``input_tokens`` between two
        same-signature events for them to be considered the same work.
        ``0.0`` requires exact equality.  Default ``0.05`` (5 %).
    threshold : int
        Number of occurrences that triggers a duplicate-work trip.
        Default ``2`` (trip on the 2nd occurrence).
    idempotent_agents : frozenset
        Agent names that perform legitimately repeatable work and should
        never trigger the duplicate-work detector.  Also respected by the
        new detectors via :func:`~looptrip.detectors._shared._is_exempt`.

    New-detector exemption fields
    ------------------------------
    idempotent_tools : frozenset
        Tool names whose events are exempt from the new detectors.
    retry_allowed : frozenset
        Agent names that are explicitly allowed to retry; exempt from
        ``ping_pong`` and ``non_termination``.
    allowlist_agents : frozenset
        Additional agent names always exempt from new detectors (union
        with ``idempotent_agents`` and ``retry_allowed``).
    allowlist_tools : frozenset
        Additional tool names always exempt from new detectors.

    Progress / terminal / blocked state vocabulary
    -----------------------------------------------
    progress_markers : frozenset
        ``handoff_state`` values that count as a progress delta even when
        ``event.progress`` is ``False``.  Default empty — do not bake any
        framework's vocabulary into the OSS core.
    terminal_states : frozenset
        ``handoff_state`` values that signal the agent has ended (epoch
        reset in ``ping_pong``; window break in ``non_termination``).
        Default empty — CAST callers should pass
        ``{"DONE", "DONE_WITH_CONCERNS"}``.
    blocked_states : frozenset
        ``handoff_state`` leading-word values that signal an agent is
        blocked/waiting, matched CASE-INSENSITIVELY (so CAST's uppercase
        ``"BLOCKED"`` matches the default ``"blocked"`` token without
        leaking CAST-specific tokens into the generic core).
        Default ``frozenset({"blocked", "waiting"})``.

    Ping-pong sensitivity
    ---------------------
    min_cycle_len : int
        Minimum number of distinct agents in a cycle.  Default ``2``
        (excludes self-loops; a single-agent A→A self-loop is never
        ``ping_pong``).
    cycle_trip_count : int
        Number of times the same directed cycle must close before it trips
        the detector.  Default ``2`` (trip on the 2nd closure).
    use_handoff_edges : bool
        When ``True``, extract explicit hop targets from ``handoff_state``
        (where present) rather than using the raw temporal agent-to-agent
        sequence.  Default ``False`` — the pure-temporal, handoff-free path.

    Non-termination sensitivity
    ---------------------------
    window_size : int
        Sliding window length ``N`` for the non-termination detector.
        Default ``20``.
    plateau_ratio : float
        Fraction of ``window_size`` used as the unique-state cap when
        ``plateau_unique_states`` is ``None``.
        ``cap = max(1, floor(window_size * plateau_ratio))``.
        Default ``0.5``.
    plateau_unique_states : Optional[int]
        Absolute unique-state cap override.  ``None`` means derive from
        ``plateau_ratio``.  Must be ``>= 1`` when set.
    state_key : str
        Which field to use as the per-event state identity when computing
        the unique-state count.  One of ``"signature"``, ``"agent"``,
        ``"handoff_state"``.  Default ``"signature"``.
    """

    # --- Phase-1 legacy fields (names and defaults preserved exactly) ---
    token_tolerance: float = 0.05
    threshold: int = 2
    idempotent_agents: frozenset = frozenset()

    # --- new-detector exemption fields ---
    idempotent_tools: frozenset = frozenset()
    retry_allowed: frozenset = frozenset()
    allowlist_agents: frozenset = frozenset()
    allowlist_tools: frozenset = frozenset()

    # --- progress / terminal / blocked state vocabulary ---
    progress_markers: frozenset = frozenset()
    terminal_states: frozenset = frozenset()
    blocked_states: frozenset = frozenset({"blocked", "waiting"})

    # --- ping-pong sensitivity ---
    min_cycle_len: int = 2
    cycle_trip_count: int = 2
    use_handoff_edges: bool = False

    # --- non-termination sensitivity ---
    window_size: int = 20
    plateau_ratio: float = 0.5
    plateau_unique_states: Optional[int] = None
    state_key: str = "signature"

    def __post_init__(self) -> None:
        """Validate field boundaries; raise :class:`ValueError` on violations.

        Catches impossible or unsafe configurations at construction time so
        that detectors never receive a config they cannot meaningfully
        execute.

        Raises:
            ValueError: On any of the following boundary violations:
                ``window_size < 1``, ``threshold < 1``,
                ``cycle_trip_count < 1``, ``min_cycle_len < 2``,
                ``token_tolerance < 0``,
                ``not 0.0 <= plateau_ratio <= 1.0``,
                ``plateau_unique_states is not None and
                plateau_unique_states < 1``,
                ``state_key not in {"signature","agent","handoff_state"}``.
        """
        if self.window_size < 1:
            raise ValueError(
                f"window_size must be >= 1; got {self.window_size!r}"
            )
        if self.threshold < 1:
            raise ValueError(
                f"threshold must be >= 1; got {self.threshold!r}"
            )
        if self.cycle_trip_count < 1:
            raise ValueError(
                f"cycle_trip_count must be >= 1; got {self.cycle_trip_count!r}"
            )
        if self.min_cycle_len < 2:
            raise ValueError(
                f"min_cycle_len must be >= 2 (self-loops are never a cycle); "
                f"got {self.min_cycle_len!r}"
            )
        if self.token_tolerance < 0:
            raise ValueError(
                f"token_tolerance must be >= 0; got {self.token_tolerance!r}"
            )
        if not (0.0 <= self.plateau_ratio <= 1.0):
            raise ValueError(
                f"plateau_ratio must be in [0.0, 1.0]; got {self.plateau_ratio!r}"
            )
        if (
            self.plateau_unique_states is not None
            and self.plateau_unique_states < 1
        ):
            raise ValueError(
                f"plateau_unique_states must be >= 1 when set; "
                f"got {self.plateau_unique_states!r}"
            )
        if self.state_key not in {"signature", "agent", "handoff_state"}:
            raise ValueError(
                f"state_key must be one of 'signature', 'agent', "
                f"'handoff_state'; got {self.state_key!r}"
            )


# ---------------------------------------------------------------------------
# resolve_config — merge caller-supplied overrides onto a base config
# ---------------------------------------------------------------------------


def resolve_config(
    config: Optional[DetectionConfig],
    knobs: dict,
) -> DetectionConfig:
    """Return a resolved :class:`DetectionConfig`, applying ``knobs`` overrides.

    Starts from ``config`` (or a fresh :class:`DetectionConfig` when
    ``None``) and applies any ``knobs`` via :func:`dataclasses.replace`.
    This lets callers supply either a pre-built config object OR ad-hoc
    keyword overrides (or both — ``config`` is the base, ``knobs`` win).

    Args:
        config: Base configuration, or ``None`` to use the default.
        knobs:  Mapping of field-name → new value.  Empty dict is a no-op.

    Returns:
        The resolved :class:`DetectionConfig`.

    Raises:
        TypeError: If ``knobs`` contains a key that is not a valid
            :class:`DetectionConfig` field name.

    Example::

        cfg = resolve_config(None, {"threshold": 3, "window_size": 10})
        # cfg.token_tolerance == 0.05  (default preserved)
        # cfg.threshold == 3
        # cfg.window_size == 10
    """
    base = config if config is not None else DetectionConfig()
    if not knobs:
        return base
    unknown = set(knobs) - {f.name for f in dataclasses.fields(DetectionConfig)}
    if unknown:
        raise TypeError(
            f"unexpected configuration knob(s): {sorted(unknown)}"
        )
    return dataclasses.replace(base, **knobs)
