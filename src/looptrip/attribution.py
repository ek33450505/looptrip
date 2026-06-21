"""attribution.py — counterfactual replay attribution for confirmed pathologies.

This module answers the question: *which individual handoff events are decisive
for a confirmed pathology?*  Given a :class:`~looptrip.detectors.types.PathologyReport`
P and the :class:`~looptrip.normalize.Event` stream it was detected in, the
module determines which events, if **individually neutralized** (removed from
the stream and the detector re-run), cause P to no longer trip.

Counterfactual replay model
---------------------------
The attribution algorithm runs one single-event neutralization trial per event
in the stream — hence O(N) detector re-runs, each costing O(N) to process the
N-1-length stream, giving an overall **O(N²)** complexity.  This is intentional
and acceptable at fixture scale (streams of tens to hundreds of events, as used
in CAST integration tests and offline analysis).  For production-scale streams
of thousands of events, callers should pre-filter the stream to the pathology's
own time window before calling ``attribute``.

The pathology is identified across replays by its **fingerprint**
``fp = (report.kind, report.signature)``.  A neutralized replay "averts" P when
it produces NO report whose ``(kind, signature)`` matches ``fp``.

Decisive / overdetermined / multiple semantics
----------------------------------------------
* **decisive** — the set of events each of which, if individually removed,
  averts P.  A decisive event is sometimes called the "fault point" or
  "breaking link"; removing it alone is sufficient to prevent the pathology.
* **unique** — exactly one decisive event exists.  This is the crisp answer:
  one handoff, one fix.
* **multiple** — two or more events are independently decisive.  Each one
  alone would avert P, but P is not robust to any single removal.
* **overdetermined** — *no* single neutralization averts P.  The pathology is
  caused by the repeated *structure* of the stream, not by any one individual
  handoff.  Removing any single event still leaves the remaining stream
  sufficient to trip the detector.  This is an honest, required outcome — the
  module does NOT overstate a unique decisive handoff when none exists.

Documented limitation
---------------------
The fingerprint is ``(kind, signature)``.  If two coexisting pathology reports
of the same kind share the same ``signature`` value (e.g. two simultaneous
``non_termination`` plateaus on the same state key), they are treated as a
single identity by this module.  Attribution will avert *both* when it averts
the fingerprint, which may over-report decisive events.  This limitation is
accepted at Phase 3 scope; a future phase may use ``report.trip_event.raw_id``
as a tie-breaker.

Import DAG
----------
This module sits ABOVE :mod:`looptrip.detector` in the import graph and is
imported by nothing in the library itself — it is a leaf consumer.  Importing
:func:`~looptrip.detector.detect` here is therefore safe and creates no
circular import::

    looptrip.normalize
        ↑
    looptrip.detectors.types
        ↑
    looptrip.detectors._shared
        ↑
    looptrip.detectors.{ping_pong, deadlock, non_termination}
        ↑
    looptrip.detector
        ↑
    looptrip.attribution   ← this module (leaf; not imported by anything below)

This module is stdlib-only and defines no global mutable state.  Every call
builds and discards its own locals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

from looptrip.normalize import Event
from looptrip.detectors.types import (
    PathologyReport,
    DetectionConfig,
    resolve_config,
    ALL_DETECTORS,
)
from looptrip.detector import detect

__all__ = ["AttributionResult", "attribute", "attribute_all"]


@dataclass(frozen=True, slots=True)
class AttributionResult:
    """The outcome of a counterfactual-replay attribution run for one pathology.

    Each field documents both *what was found* and the *methodology* used to
    find it, so that callers can surface the result in audit logs, CLI output,
    or downstream analysis without re-running the attribution.

    Attributes:
        report:       The :class:`~looptrip.detectors.types.PathologyReport`
                      that was attributed.  The pathology identity — kind and
                      signature — is embedded here; ``fingerprint`` below
                      mirrors it for convenient cross-replay lookup.
        decisive:     A ``tuple[Event, ...]`` of events that are each
                      individually decisive: removing any one of them alone
                      from the stream averts the pathology.  In stream order
                      (i.e. ascending index order from the source stream).
                      Empty when ``verdict == "overdetermined"``.
        tested:       Total number of single-event neutralization trials
                      evaluated — equals ``len(events)`` passed to
                      :func:`attribute`.
        fingerprint:  The cross-replay identity of the pathology:
                      ``(report.kind, report.signature)``.  Stable across
                      neutralized replays; used to match reports across runs.
        verdict:      One of three string values:
                      ``"unique"`` — exactly one decisive event;
                      ``"overdetermined"`` — no decisive event (structure
                      causes the pathology, not any single event);
                      ``"multiple"`` — two or more independently decisive
                      events.
        detail:       A concise, honest human-readable sentence describing
                      the attribution outcome.  Worded differently per
                      verdict to avoid misleading framing (e.g. overdetermined
                      pathologies are never described as having a "decisive
                      handoff").
    """

    report: PathologyReport
    decisive: tuple[Event, ...]
    tested: int
    fingerprint: tuple[str, tuple]  # (report.kind, report.signature)
    verdict: str              # "unique" | "overdetermined" | "multiple"
    detail: str

    @property
    def is_decisive(self) -> bool:
        """Return ``True`` iff exactly one decisive handoff was found.

        Equivalent to ``verdict == "unique"``.  Provided as a convenience
        property for callers that want a simple boolean gate (e.g. "can we
        name the single root-cause event?") without pattern-matching on the
        verdict string.
        """
        return self.verdict == "unique"


def attribute(
    events: Iterable[Event],
    report: PathologyReport,
    *,
    config: Optional[DetectionConfig] = None,
    **knobs,
) -> "AttributionResult":
    """Attribute a confirmed pathology to its decisive event(s) via counterfactual replay.

    For each event in ``events``, removes that event from the stream and
    re-runs the detector for ``report.kind``.  An event is **decisive** when
    its removal alone causes the pathology to no longer appear in the
    neutralized stream (matched by fingerprint ``(kind, signature)``).

    The algorithm is O(N²): N neutralization trials, each running the detector
    over an N-1 length stream.

    Args:
        events:  The ordered event stream from which ``report`` was originally
                 detected.  Materialised once into a list; the caller's input
                 is never mutated or reordered.  An iterator is consumed exactly
                 once.
        report:  The confirmed :class:`~looptrip.detectors.types.PathologyReport`
                 to attribute.  Must be reproducible over ``events`` under
                 ``config`` (verified by a base-case sanity re-run).
        config:  Pre-built :class:`~looptrip.detectors.types.DetectionConfig`,
                 or ``None`` to use defaults.  **Must match the config used to
                 produce ``report``** — a mismatch will cause the sanity guard
                 to raise :class:`ValueError`.
        **knobs: Ad-hoc field overrides applied on top of ``config`` via
                 :func:`~looptrip.detectors.types.resolve_config`.

    Returns:
        An :class:`AttributionResult` with ``verdict`` set to ``"unique"``,
        ``"multiple"``, or ``"overdetermined"`` and ``decisive`` populated with
        the events (in stream order) that are each individually decisive.

    Raises:
        ValueError: If ``report.kind`` is not in
            :data:`~looptrip.detectors.types.ALL_DETECTORS`.
        ValueError: If the pathology is not reproducible over ``events`` under
            ``config`` — i.e. no report in the base re-run has
            ``(kind, signature)`` matching ``report``.  This indicates either
            a stale report (from a different stream version) or a config
            mismatch.  The error message names the expected fingerprint and
            instructs the caller to pass the same config used during initial
            detection.
    """
    # Step 1 — materialise once; never mutate or reorder the caller's input.
    evs: List[Event] = list(events)

    # Step 2 — resolve config (merges knobs on top of config or defaults).
    cfg: DetectionConfig = resolve_config(config, knobs)

    # Step 3 — validate that report.kind is a known detector.
    if report.kind not in ALL_DETECTORS:
        raise ValueError(
            f"report.kind {report.kind!r} is not a recognised detector kind. "
            f"Expected one of: {sorted(ALL_DETECTORS)}."
        )

    # Step 4 — build the cross-replay fingerprint.
    fp: tuple = (report.kind, report.signature)

    # Step 5 — sanity re-derivation guard: verify the report is reproducible.
    base = detect(evs, config=cfg, detectors=(report.kind,))
    if not any((r.kind, r.signature) == fp for r in base):
        raise ValueError(
            f"Pathology {fp!r} is not reproducible over the supplied stream "
            f"under the given config.  Either the stream has changed since the "
            f"report was produced, or the config passed here does not match the "
            f"config used during initial detection.  Pass the same "
            f"DetectionConfig that was used when detect() produced this report."
        )

    # Step 6 — single-event neutralization trials.
    decisive: List[Event] = []
    for i in range(len(evs)):
        neutralized = evs[:i] + evs[i + 1:]
        reps = detect(neutralized, config=cfg, detectors=(report.kind,))
        if not any((r.kind, r.signature) == fp for r in reps):
            decisive.append(evs[i])

    # Step 7 — verdict.
    k = len(decisive)
    if k == 0:
        verdict = "overdetermined"
    elif k == 1:
        verdict = "unique"
    else:
        verdict = "multiple"

    # Step 8 — tested count.
    tested = len(evs)

    # Step 9 — build detail string.
    kind = report.kind
    sig_str = str(report.signature)
    if verdict == "unique":
        r = decisive[0]
        detail = (
            f"Decisive handoff: neutralizing raw_id={r.raw_id!r} "
            f"(agent {r.agent!r}) averts the {kind} pathology {sig_str}. "
            f"1 of {tested} handoffs tested was decisive."
        )
    elif verdict == "overdetermined":
        detail = (
            f"No single decisive handoff: the {kind} pathology {sig_str} "
            f"survives neutralizing any one of {tested} handoffs — it remains "
            f"tripped (overdetermined; caused by the repeated structure, not a "
            f"single handoff)."
        )
    else:
        raw_ids = tuple(r.raw_id for r in decisive)
        detail = (
            f"{k} independently-decisive handoffs (raw_ids {raw_ids!r}): "
            f"neutralizing any one alone averts the {kind} pathology {sig_str}; "
            f"{k} of {tested} tested."
        )

    # Step 10 — return result.
    return AttributionResult(
        report=report,
        decisive=tuple(decisive),
        tested=tested,
        fingerprint=fp,
        verdict=verdict,
        detail=detail,
    )


def attribute_all(
    events: Iterable[Event],
    reports: Iterable[PathologyReport],
    *,
    config: Optional[DetectionConfig] = None,
    **knobs,
) -> List[AttributionResult]:
    """Attribute every report in ``reports`` over the same event stream.

    Materialises ``events`` once and passes the same list to each
    :func:`attribute` call, so that iterator-style ``events`` inputs are not
    double-consumed.  Each report is attributed independently with the same
    ``config`` and ``**knobs``.

    Args:
        events:   The ordered event stream.  Consumed exactly once regardless
                  of how many reports are in ``reports``.
        reports:  An iterable of confirmed
                  :class:`~looptrip.detectors.types.PathologyReport` instances
                  to attribute.  May be empty (returns ``[]``).
        config:   Pre-built :class:`~looptrip.detectors.types.DetectionConfig`,
                  or ``None`` for defaults.
        **knobs:  Ad-hoc sensitivity overrides (see :func:`attribute`).

    Returns:
        A ``list[AttributionResult]``, one per report, in the same order as
        ``reports``.

    Raises:
        ValueError: Forwarded from :func:`attribute` for any report whose kind
            is unrecognised or whose pathology cannot be reproduced.
    """
    # Materialise events once; pass the same list to every attribute() call
    # so that an iterator input is not double-consumed.
    evs: List[Event] = list(events)
    return [attribute(evs, r, config=config, **knobs) for r in reports]
