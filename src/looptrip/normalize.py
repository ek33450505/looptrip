"""normalize.py — the normalized event contract for looptrip.

Every source of multi-agent activity (cast.db agent runs today; OTel GenAI
spans later) is funnelled through one uniform shape — the :class:`Event` — so
the pathology detectors never have to know where their input came from.

The schema, in field order, is::

    (agent, tool, args_hash, ts, handoff_state, to_agent, input_tokens,
     cost_usd, progress, raw_id)

Detection keys off the ``signature() == (agent, tool, args_hash)`` triple plus
``ts`` ordering. The three signature fields are deliberately the load-bearing
identity of an event:

* ``agent``     — who acted (e.g. "workflow-subagent").
* ``tool``      — what kind of action. Sources without a per-action tool column
                  (the cast.db case) set this to a constant like ``"dispatch"``.
* ``args_hash`` — a stable hash of the action's arguments, or ``None`` when the
                  source cannot supply one.

``args_hash`` MAY be ``None``. The cast.db ``agent_runs`` table has no
per-dispatch tool/args columns, so its adapter emits ``tool="dispatch"`` and
``args_hash=None`` for every event; detection there relies on the
``(agent, ts)`` repeat signal plus input-token variance rather than on an args
hash. ``handoff_state`` is pure enrichment (``None`` for status-contract-exempt
agents) and is NEVER required for detection.

This module is stdlib-only and defines no global mutable state.
"""

from __future__ import annotations

import abc
import hashlib
from dataclasses import dataclass
from typing import Any, Iterator, Optional


@dataclass(frozen=True, slots=True)
class Event:
    """One normalized unit of multi-agent activity.

    Frozen (immutable) and ``slots``-backed: events are hashable, cheap, and
    safe to share across detectors without defensive copying. Reassigning a
    field on an instance raises :class:`dataclasses.FrozenInstanceError`.

    Attributes:
        agent:         Identity of the acting agent (load-bearing; signature).
        tool:          Action kind. Constant (e.g. ``"dispatch"``) for sources
                       with no per-action tool column (signature).
        args_hash:     Stable hash of the action's arguments, or ``None`` when
                       the source cannot supply one — e.g. the cast.db adapter
                       (signature). See :func:`args_hash_from`.
        ts:            ISO-8601 timestamp string. Chosen so events sort
                       correctly by lexicographic comparison on ``ts``.
        handoff_state: Parsed ``## Handoff`` state — enrichment only; ``None``
                       for STATUS_CONTRACT_EXEMPT agents. Never required for
                       detection.
        to_agent:      Explicit handoff target agent — enrichment only; ``None``
                       when absent. Maps from
                       ``gen_ai.agent.handoff.target.name``. NOT part of
                       :meth:`signature`; detectors read it directly with no
                       delimiter scanning.
        input_tokens:  Prompt-token count for the action, if known.
        cost_usd:      Cost of the action in USD, if known. Used at full
                       precision for prevented-waste accounting; rounded only
                       for display.
        progress:      ``True`` iff this event marks a progress / state delta.
                       A repeated signature with no progress delta is the
                       duplicate-work signal.
        raw_id:        Provenance back-pointer to the source row (e.g.
                       ``agent_runs.id``).
    """

    agent: str
    tool: str
    args_hash: Optional[str]
    ts: str
    handoff_state: Optional[str] = None
    to_agent: Optional[str] = None
    input_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    progress: bool = False
    raw_id: Any = None

    def signature(self) -> tuple:
        """Return the duplicate-work identity triple ``(agent, tool, args_hash)``.

        Two events with equal signatures are candidate duplicates; the detector
        confirms a trip using ``ts`` ordering, input-token variance, and the
        absence of a progress delta.
        """
        return (self.agent, self.tool, self.args_hash)


class Adapter(abc.ABC):
    """Source-specific producer of normalized :class:`Event` streams.

    Concrete adapters (cast.db today, OTel later) translate their native
    records into :class:`Event` instances. The detectors consume only this
    interface, so they remain framework-agnostic.
    """

    @abc.abstractmethod
    def events(self) -> Iterator[Event]:
        """Yield normalized events, ordered such that ``ts`` is non-decreasing."""
        ...


def args_hash_from(*parts: str) -> str:
    """Return a deterministic, order-sensitive SHA-1 hex digest of ``parts``.

    The parts are joined with ``"|"`` before hashing, so argument order is
    significant: ``args_hash_from("a", "b") != args_hash_from("b", "a")``. This
    is for adapters that actually have per-action arguments to hash (OTel
    later); the cast.db adapter has no args and uses ``args_hash=None`` instead.
    """
    joined = "|".join(parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def split_handoff_state(s: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Split a LEGACY packed ``"state on target"`` string into ``(state, target)``.

    First-class adapters (cast.db, OTel) set ``handoff_state`` and ``to_agent``
    directly — they never need this. This helper exists ONLY for ingesting a
    legacy / human-pasted ``"state on target"`` corpus where the two values were
    packed into a single string.

    The split is a SIMPLE single-delimiter splitter: it splits on the FIRST
    case-insensitive ``" on "`` into ``(state, target)``, each ``.strip()``'d
    (an empty result becomes ``None``). When ``s`` is falsy or contains no
    ``" on "``, returns ``(s or None, None)``.

    This deliberately does NOT port the old four-delimiter machinery
    (``=`` / ``:`` / ``" on "`` / ``" to "``); that brittleness is exactly what
    the explicit ``to_agent`` field retires.

    Args:
        s: The legacy packed string to split, or ``None``.

    Returns:
        A 2-tuple ``(state, target)`` where each element is the stripped text or
        ``None`` when absent.
    """
    if not s:
        return (None, None)

    lower = s.lower()
    idx = lower.find(" on ")
    if idx == -1:
        state = s.strip()
        return (state or None, None)

    state = s[:idx].strip()
    target = s[idx + len(" on "):].strip()
    return (state or None, target or None)
