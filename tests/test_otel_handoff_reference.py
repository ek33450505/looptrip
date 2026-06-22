"""Reference test: OTel GenAI handoff spans light up looptrip pathology detectors.

This module proves that looptrip's deadlock and ping-pong (handoff-edge mode)
detectors FIRE on synthetic OpenTelemetry GenAI-shaped spans that carry the
``gen_ai.agent.handoff.*`` attributes proposed by semantic-conventions-genai.

Today those two detectors are "dark" on all real CAST data because the only
adapter (cast.db) emits ``handoff_state=None`` for every event.  This fixture
is the capturable-telemetry evidence that populating the OTel
``gen_ai.agent.handoff.*`` convention lights up looptrip's pathology detection.

Fixture: ``tests/fixtures/otel_genai_handoff_spans.json``

Three labelled scenarios are exercised:

(a) DEADLOCK     — code-writer blocked awaiting code-reviewer, and vice versa;
                   the wait-for graph forms a 2-cycle.
(b) PING-PONG    — planner and code-writer exchange explicit handoffs in an
                   A→B→A→B loop; the directed cycle closes twice within a
                   single epoch when ``use_handoff_edges=True``.
(c) CONTROL      — clean linear handoff chain (A→B→C); no cycle; must NOT
                   trip either detector.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, Optional

from looptrip.detector import (
    KIND_DEADLOCK,
    KIND_PING_PONG,
    detect_deadlock,
    detect_ping_pong,
)
from looptrip.detectors.types import DetectionConfig
from looptrip.normalize import Event

# ---------------------------------------------------------------------------
# Fixture path
# ---------------------------------------------------------------------------

_FIXTURE = (
    pathlib.Path(__file__).parent / "fixtures" / "otel_genai_handoff_spans.json"
)


# ---------------------------------------------------------------------------
# OTel-span → Event reference mapping
# ---------------------------------------------------------------------------


def otel_span_to_event(span: Dict[str, Any]) -> Event:
    """Map one synthetic OTel GenAI handoff span to a looptrip :class:`Event`.

    Attribute provenance
    --------------------
    ``gen_ai.agent.handoff.source.name``
        Adopted verbatim from semantic-conventions-genai PR #98.
        The agent performing the handoff → ``Event.agent``.

    ``gen_ai.agent.handoff.target.name``
        Adopted verbatim from semantic-conventions-genai PR #98.
        The agent receiving the handoff.  Mapped DIRECTLY onto the explicit
        ``Event.to_agent`` field — no string composition.

    ``gen_ai.agent.handoff.state``
        **looptrip-proposed enum** — not yet upstream.  PR #98 only models a
        *completed* transfer.  This attribute distinguishes two operational
        semantics:

        * **PENDING values** (``"blocked"``, ``"waiting"``) — the source agent
          is waiting for the target; the transfer has not yet occurred.  These
          match looptrip's default ``blocked_states`` vocabulary and feed the
          deadlock detector's wait-for graph.
        * **ACTIVE values** (``"in_progress"``) — the source agent is actively
          handing off completed work; the transfer is live.  ``Event.to_agent``
          still names the hop target (enabling ping-pong handoff-edge
          detection) but the bare state token does NOT match ``blocked_states``,
          so the deadlock detector ignores these events entirely.

        The PENDING vs ACTIVE distinction is load-bearing: a livelock
        (ping-pong) is agents *actively bouncing* work — not blocked-waiting.

    Explicit-field mapping
    ----------------------
    The two OTel attributes map onto two SEPARATE explicit ``Event`` fields —
    there is no packed ``"state on target"`` string anymore:

    * ``gen_ai.agent.handoff.state`` → ``Event.handoff_state`` (the bare token).
    * ``gen_ai.agent.handoff.target.name`` → ``Event.to_agent`` (the target).

    Detectors read ``event.handoff_state`` (via
    :func:`~looptrip.detectors._shared._is_blocked`) and ``event.to_agent``
    directly — no delimiter scanning.

    When ``gen_ai.agent.handoff.state`` is present but the target is absent,
    ``to_agent`` is ``None``.  When ``gen_ai.agent.handoff.state`` is absent
    (e.g. a completed transfer as in the CONTROL scenario) BOTH
    ``handoff_state`` and ``to_agent`` are ``None``, leaving the deadlock
    blocked-map empty and the ping-pong handoff-edge substrate inert.

    Args:
        span: A dict with keys ``span_id``, ``start_time``, and
              ``attributes`` (itself a flat dict of OTel attribute strings).

    Returns:
        A frozen :class:`~looptrip.normalize.Event`.
    """
    attrs: Dict[str, Any] = span.get("attributes", {})

    agent: str = attrs["gen_ai.agent.handoff.source.name"]
    tool: str = attrs.get("gen_ai.operation.name", "dispatch")
    ts: str = span["start_time"]
    raw_id: str = span["span_id"]

    state: Optional[str] = attrs.get("gen_ai.agent.handoff.state")
    target: Optional[str] = attrs.get("gen_ai.agent.handoff.target.name")

    if state:
        handoff_state: Optional[str] = state
        to_agent: Optional[str] = target
    else:
        handoff_state = None
        to_agent = None

    return Event(
        agent=agent,
        tool=tool,
        args_hash=None,
        ts=ts,
        handoff_state=handoff_state,
        to_agent=to_agent,
        raw_id=raw_id,
    )


# ---------------------------------------------------------------------------
# Fixture loader helpers
# ---------------------------------------------------------------------------


def _load_fixture() -> Dict[str, Any]:
    with _FIXTURE.open() as f:
        return json.load(f)


def _events_for(scenario: str) -> list:
    data = _load_fixture()
    spans = data["scenarios"][scenario]["spans"]
    return [otel_span_to_event(s) for s in spans]


# ---------------------------------------------------------------------------
# (light) Mapping correctness
# ---------------------------------------------------------------------------


def test_mapping_blocked_state_with_target_sets_explicit_fields():
    """otel_span_to_event sets handoff_state='blocked' and to_agent=<target>."""
    span = {
        "span_id": "test-span-01",
        "start_time": "2024-01-01T00:00:00Z",
        "attributes": {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.agent.handoff.source.name": "agent-a",
            "gen_ai.agent.handoff.target.name": "agent-b",
            "gen_ai.agent.handoff.state": "blocked",
        },
    }
    event = otel_span_to_event(span)
    assert event.handoff_state == "blocked"
    assert event.to_agent == "agent-b"
    assert event.agent == "agent-a"
    assert event.raw_id == "test-span-01"
    assert event.ts == "2024-01-01T00:00:00Z"


def test_mapping_waiting_state_sets_explicit_fields():
    """otel_span_to_event sets handoff_state='waiting' and to_agent=<target>."""
    span = {
        "span_id": "test-span-02",
        "start_time": "2024-01-01T00:00:01Z",
        "attributes": {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.agent.handoff.source.name": "agent-x",
            "gen_ai.agent.handoff.target.name": "agent-y",
            "gen_ai.agent.handoff.state": "waiting",
        },
    }
    event = otel_span_to_event(span)
    assert event.handoff_state == "waiting"
    assert event.to_agent == "agent-y"


def test_mapping_no_state_produces_none_handoff_state_and_to_agent():
    """otel_span_to_event produces handoff_state=None AND to_agent=None when state absent."""
    span = {
        "span_id": "test-span-03",
        "start_time": "2024-01-01T00:00:02Z",
        "attributes": {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.agent.handoff.source.name": "agent-alpha",
            "gen_ai.agent.handoff.target.name": "agent-beta",
        },
    }
    event = otel_span_to_event(span)
    assert event.handoff_state is None
    assert event.to_agent is None


def test_mapping_hyphenated_agent_names_preserved():
    """Hyphenated agent names (e.g. 'code-writer') are preserved exactly."""
    span = {
        "span_id": "test-span-04",
        "start_time": "2024-01-01T00:00:03Z",
        "attributes": {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.agent.handoff.source.name": "code-writer",
            "gen_ai.agent.handoff.target.name": "code-reviewer",
            "gen_ai.agent.handoff.state": "blocked",
        },
    }
    event = otel_span_to_event(span)
    assert event.agent == "code-writer"
    assert event.handoff_state == "blocked"
    assert event.to_agent == "code-reviewer"


def test_mapping_in_progress_state_sets_explicit_fields():
    """otel_span_to_event sets handoff_state='in_progress' and to_agent for active transfers.

    'in_progress' is an ACTIVE-transfer value: ``to_agent`` still names the hop
    target for ping-pong handoff-edge mode, but the bare 'in_progress' token does
    NOT match blocked_states so detect_deadlock ignores it.
    """
    span = {
        "span_id": "test-span-05",
        "start_time": "2024-01-01T00:00:04Z",
        "attributes": {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.agent.handoff.source.name": "planner",
            "gen_ai.agent.handoff.target.name": "code-writer",
            "gen_ai.agent.handoff.state": "in_progress",
        },
    }
    event = otel_span_to_event(span)
    assert event.handoff_state == "in_progress"
    assert event.to_agent == "code-writer"


# ---------------------------------------------------------------------------
# (a) Scenario DEADLOCK
# ---------------------------------------------------------------------------


def test_deadlock_fires_on_otel_handoff_spans():
    """Scenario (a): deadlock detector fires on OTel spans with blocked wait-for cycle.

    code-writer is blocked waiting on code-reviewer; code-reviewer is blocked
    waiting on code-writer.  The deadlock detector must report a 2-cycle.
    """
    events = _events_for("deadlock")
    reports = detect_deadlock(events)

    assert len(reports) == 1, (
        f"Expected exactly 1 deadlock report; got {len(reports)}.  "
        f"handoff_states={[e.handoff_state for e in events]!r}"
    )
    report = reports[0]
    assert report.kind == KIND_DEADLOCK
    assert report.blocked_agents == frozenset({"code-writer", "code-reviewer"})
    assert report.occurrences == 2
    assert report.prevented_cost == 0.0
    assert report.prevented_runs == 0


def test_deadlock_cycle_members_match_otel_source_names():
    """Deadlock members == the gen_ai.agent.handoff.source.name values in the fixture."""
    events = _events_for("deadlock")
    reports = detect_deadlock(events)
    assert len(reports) == 1
    report = reports[0]
    assert "code-writer" in report.members
    assert "code-reviewer" in report.members


def test_deadlock_scenario_does_not_trip_ping_pong():
    """REGRESSION LOCK: a deadlock is NOT a ping-pong.

    The deadlock scenario has only 2 events (one per agent), producing exactly
    1 cycle closure — below the default cycle_trip_count=2 threshold.
    detect_ping_pong must return [] even with use_handoff_edges=True.
    """
    events = _events_for("deadlock")
    cfg = DetectionConfig(use_handoff_edges=True)
    reports = detect_ping_pong(events, config=cfg)
    assert reports == [], (
        f"Deadlock scenario must NOT trip ping-pong; got {reports!r}"
    )


# ---------------------------------------------------------------------------
# (b) Scenario PING-PONG (handoff-edge mode)
# ---------------------------------------------------------------------------


def test_ping_pong_handoff_edge_mode_fires_on_otel_spans():
    """Scenario (b): ping-pong detector (use_handoff_edges=True) fires on OTel spans.

    planner and code-writer exchange explicit handoffs (planner→code-writer→planner…).
    With use_handoff_edges=True the explicit hop edges form a directed A→B→A cycle;
    the detector trips at the 2nd closure within the epoch.
    """
    events = _events_for("ping_pong")
    cfg = DetectionConfig(use_handoff_edges=True)
    reports = detect_ping_pong(events, config=cfg)

    assert len(reports) == 1, (
        f"Expected exactly 1 ping-pong report with use_handoff_edges=True; "
        f"got {len(reports)}.  agents={[e.agent for e in events]!r}, "
        f"handoff_states={[e.handoff_state for e in events]!r}"
    )
    report = reports[0]
    assert report.kind == KIND_PING_PONG
    assert "planner" in report.members
    assert "code-writer" in report.members


def test_ping_pong_handoff_edge_cycle_trip_count_is_two():
    """Ping-pong trips at the 2nd cycle closure (default cycle_trip_count=2)."""
    events = _events_for("ping_pong")
    cfg = DetectionConfig(use_handoff_edges=True)
    reports = detect_ping_pong(events, config=cfg)
    assert len(reports) == 1
    assert reports[0].trip_index == 2


def test_ping_pong_scenario_does_not_trip_deadlock():
    """REGRESSION LOCK: a ping-pong (livelock) is NOT a deadlock.

    Ping-pong spans carry gen_ai.agent.handoff.state='in_progress' (ACTIVE),
    whose leading word does not match blocked_states {'blocked', 'waiting'}.
    detect_deadlock's blocked map is empty → [].
    """
    events = _events_for("ping_pong")
    reports = detect_deadlock(events)
    assert reports == [], (
        f"Ping-pong scenario must NOT trip deadlock; got {reports!r}.  "
        f"handoff_states={[e.handoff_state for e in events]!r}"
    )


# ---------------------------------------------------------------------------
# (c) Scenario CONTROL — must NOT trip either detector
# ---------------------------------------------------------------------------


def test_control_deadlock_returns_empty():
    """Scenario (c): clean linear handoff chain does NOT trip the deadlock detector.

    CONTROL spans carry no gen_ai.agent.handoff.state attribute, so handoff_state
    maps to None for every event.  The deadlock blocked-map is empty → [].
    """
    events = _events_for("control")
    reports = detect_deadlock(events)
    assert reports == [], (
        f"Expected no deadlock on control scenario; got {reports!r}"
    )


def test_control_ping_pong_handoff_edge_returns_empty():
    """Scenario (c): clean linear handoff chain does NOT trip ping-pong (handoff-edge mode).

    CONTROL spans have no gen_ai.agent.handoff.state, so both handoff_state and
    to_agent are None.  With to_agent=None, no synthetic hop edges are inserted by
    the handoff-edge substrate.  The temporal sequence (alpha→beta→gamma) is linear,
    with no repeated-node revisit → no cycle → [].
    """
    events = _events_for("control")
    cfg = DetectionConfig(use_handoff_edges=True)
    reports = detect_ping_pong(events, config=cfg)
    assert reports == [], (
        f"Expected no ping-pong on control scenario; got {reports!r}"
    )


def test_control_ping_pong_temporal_mode_also_returns_empty():
    """Scenario (c): clean linear chain does NOT trip ping-pong in temporal mode either."""
    events = _events_for("control")
    reports = detect_ping_pong(events)  # default use_handoff_edges=False
    assert reports == []


# ---------------------------------------------------------------------------
# Fixture integrity
# ---------------------------------------------------------------------------


def test_fixture_file_exists_and_parses():
    """The fixture JSON exists and contains all three required scenario keys."""
    assert _FIXTURE.exists(), f"Fixture not found: {_FIXTURE}"
    data = _load_fixture()
    for key in ("deadlock", "ping_pong", "control"):
        assert key in data["scenarios"], f"Missing scenario key '{key}' in fixture"


def test_fixture_spans_roundtrip_through_mapping():
    """Every span in the fixture maps to a valid Event without error."""
    data = _load_fixture()
    for scenario_key, scenario in data["scenarios"].items():
        for span in scenario["spans"]:
            event = otel_span_to_event(span)
            assert isinstance(event, Event)
            assert event.agent, f"Empty agent for span {span['span_id']!r}"
            assert event.ts, f"Empty ts for span {span['span_id']!r}"
            assert event.raw_id == span["span_id"]
