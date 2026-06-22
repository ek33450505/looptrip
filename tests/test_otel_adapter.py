"""tests/test_otel_adapter.py — OTelSpanAdapter and helpers unit/integration tests.

Covers:
- from_json_file on the existing flat fixture (scenario selection) + detector round-trips.
- from_jsonl_file: tmp .jsonl of flat spans.
- from_otlp_file and from_json_file OTLP auto-detect on the OTLP fixture.
- OTLP<->FLAT EQUIVALENCE CROSS-CHECK (load-bearing oracle).
- _otlp_attr_value decoding.
- startTimeUnixNano -> ISO round-trip.
- events() ordering with shuffled input.
- Error paths: missing scenario, unrecognized shape, missing file.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from looptrip.adapters.otel import OTelSpanAdapter, _normalize_otlp, _otlp_attr_value, span_to_event
from looptrip.detector import detect_deadlock, detect_ping_pong
from looptrip.detectors.types import DetectionConfig

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

_FLAT_FIXTURE = (
    pathlib.Path(__file__).parent / "fixtures" / "otel_genai_handoff_spans.json"
)
_OTLP_FIXTURE = (
    pathlib.Path(__file__).parent / "fixtures" / "otel_genai_handoff_spans_otlp.json"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_flat() -> dict:
    with _FLAT_FIXTURE.open() as f:
        return json.load(f)


def _load_otlp() -> dict:
    with _OTLP_FIXTURE.open() as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# from_json_file — flat fixture (scenario selection)
# ---------------------------------------------------------------------------


def test_from_json_file_flat_deadlock_scenario():
    """from_json_file loads the deadlock scenario by name and returns Events."""
    adapter = OTelSpanAdapter.from_json_file(str(_FLAT_FIXTURE), scenario="deadlock")
    events = list(adapter.events())
    assert len(events) == 2
    agents = {e.agent for e in events}
    assert agents == {"code-writer", "code-reviewer"}
    assert all(e.handoff_state == "blocked" for e in events)


def test_from_json_file_flat_ping_pong_scenario():
    """from_json_file loads the ping_pong scenario and returns 5 Events."""
    adapter = OTelSpanAdapter.from_json_file(str(_FLAT_FIXTURE), scenario="ping_pong")
    events = list(adapter.events())
    assert len(events) == 5
    assert all(e.handoff_state == "in_progress" for e in events)


def test_from_json_file_flat_control_scenario():
    """from_json_file loads the control scenario; all handoff_state are None."""
    adapter = OTelSpanAdapter.from_json_file(str(_FLAT_FIXTURE), scenario="control")
    events = list(adapter.events())
    assert len(events) == 3
    assert all(e.handoff_state is None for e in events)
    assert all(e.to_agent is None for e in events)


def test_from_json_file_flat_multiple_scenarios_requires_scenario_arg():
    """from_json_file raises ValueError when multiple scenarios and no scenario arg."""
    with pytest.raises(ValueError, match="multiple scenarios"):
        OTelSpanAdapter.from_json_file(str(_FLAT_FIXTURE))


def test_from_json_file_flat_unknown_scenario_raises():
    """from_json_file raises ValueError naming available scenarios for unknown scenario."""
    with pytest.raises(ValueError, match="not found"):
        OTelSpanAdapter.from_json_file(str(_FLAT_FIXTURE), scenario="nonexistent")


def test_from_json_file_flat_raises_names_available_scenarios():
    """ValueError for unknown scenario includes the list of available scenarios."""
    with pytest.raises(ValueError, match="deadlock"):
        OTelSpanAdapter.from_json_file(str(_FLAT_FIXTURE), scenario="bad_name")


# ---------------------------------------------------------------------------
# from_json_file + detectors — round-trip reproduces reference outcomes
# ---------------------------------------------------------------------------


def test_round_trip_deadlock_fires():
    """from_json_file deadlock scenario + detect_deadlock reproduces reference outcome."""
    adapter = OTelSpanAdapter.from_json_file(str(_FLAT_FIXTURE), scenario="deadlock")
    events = list(adapter.events())
    reports = detect_deadlock(events)
    assert len(reports) == 1
    assert reports[0].blocked_agents == frozenset({"code-writer", "code-reviewer"})


def test_round_trip_ping_pong_fires():
    """from_json_file ping_pong scenario + detect_ping_pong reproduces reference outcome."""
    adapter = OTelSpanAdapter.from_json_file(str(_FLAT_FIXTURE), scenario="ping_pong")
    events = list(adapter.events())
    cfg = DetectionConfig(use_handoff_edges=True)
    reports = detect_ping_pong(events, config=cfg)
    assert len(reports) == 1
    assert "planner" in reports[0].members
    assert "code-writer" in reports[0].members


def test_round_trip_control_both_detectors_empty():
    """from_json_file control scenario fires neither deadlock nor ping_pong."""
    adapter = OTelSpanAdapter.from_json_file(str(_FLAT_FIXTURE), scenario="control")
    events = list(adapter.events())
    assert detect_deadlock(events) == []
    cfg = DetectionConfig(use_handoff_edges=True)
    assert detect_ping_pong(events, config=cfg) == []


# ---------------------------------------------------------------------------
# from_jsonl_file
# ---------------------------------------------------------------------------


def test_from_jsonl_file_loads_events(tmp_path):
    """from_jsonl_file loads flat span dicts from a .jsonl file and yields Events."""
    data = _load_flat()
    spans = data["scenarios"]["deadlock"]["spans"]

    jsonl_file = tmp_path / "test_spans.jsonl"
    jsonl_file.write_text("\n".join(json.dumps(s) for s in spans) + "\n")

    adapter = OTelSpanAdapter.from_jsonl_file(str(jsonl_file))
    events = list(adapter.events())

    assert len(events) == 2
    assert {e.agent for e in events} == {"code-writer", "code-reviewer"}


def test_from_jsonl_file_skips_blank_lines(tmp_path):
    """from_jsonl_file skips blank lines without error."""
    data = _load_flat()
    spans = data["scenarios"]["control"]["spans"]

    lines = []
    for span in spans:
        lines.append(json.dumps(span))
        lines.append("")  # blank line after each span

    jsonl_file = tmp_path / "test_blank.jsonl"
    jsonl_file.write_text("\n".join(lines))

    adapter = OTelSpanAdapter.from_jsonl_file(str(jsonl_file))
    events = list(adapter.events())
    assert len(events) == 3


def test_from_jsonl_file_events_sorted(tmp_path):
    """from_jsonl_file events are sorted by (ts, span_id) regardless of input order."""
    data = _load_flat()
    spans = data["scenarios"]["ping_pong"]["spans"]
    reversed_spans = list(reversed(spans))

    jsonl_file = tmp_path / "reversed.jsonl"
    jsonl_file.write_text("\n".join(json.dumps(s) for s in reversed_spans))

    adapter = OTelSpanAdapter.from_jsonl_file(str(jsonl_file))
    events = list(adapter.events())

    tss = [e.ts for e in events]
    assert tss == sorted(tss), f"Events not sorted by ts: {tss}"


# ---------------------------------------------------------------------------
# from_otlp_file and from_json_file OTLP auto-detect
# ---------------------------------------------------------------------------


def test_from_otlp_file_deadlock(tmp_path):
    """from_otlp_file loads the deadlock OTLP sub-doc and returns 2 Events."""
    otlp = _load_otlp()
    sub_doc = otlp["deadlock"]  # {"resourceSpans": [...]}

    tmp_file = tmp_path / "deadlock_otlp.json"
    tmp_file.write_text(json.dumps(sub_doc))

    adapter = OTelSpanAdapter.from_otlp_file(str(tmp_file))
    events = list(adapter.events())

    assert len(events) == 2
    assert {e.agent for e in events} == {"code-writer", "code-reviewer"}
    assert all(e.handoff_state == "blocked" for e in events)


def test_from_json_file_otlp_auto_detect(tmp_path):
    """from_json_file auto-detects the 'resourceSpans' key and normalizes OTLP."""
    otlp = _load_otlp()
    sub_doc = otlp["ping_pong"]  # {"resourceSpans": [...]}

    tmp_file = tmp_path / "ping_pong_otlp.json"
    tmp_file.write_text(json.dumps(sub_doc))

    adapter = OTelSpanAdapter.from_json_file(str(tmp_file))
    events = list(adapter.events())

    assert len(events) == 5
    assert all(e.handoff_state == "in_progress" for e in events)


def test_from_otlp_file_and_from_json_file_produce_same_events(tmp_path):
    """from_otlp_file and from_json_file (auto-detect) produce identical Events for OTLP input."""
    otlp = _load_otlp()
    sub_doc = otlp["control"]

    tmp_file = tmp_path / "control_otlp.json"
    tmp_file.write_text(json.dumps(sub_doc))

    events_otlp = list(OTelSpanAdapter.from_otlp_file(str(tmp_file)).events())
    events_json = list(OTelSpanAdapter.from_json_file(str(tmp_file)).events())

    assert events_otlp == events_json


# ---------------------------------------------------------------------------
# OTLP<->FLAT EQUIVALENCE CROSS-CHECK (load-bearing oracle)
# ---------------------------------------------------------------------------
#
# For each scenario the Event stream from OTLP must equal the stream from the
# flat fixture on (agent, to_agent, handoff_state) IN ORDER, and detectors
# must produce the same reports.  This pins _normalize_otlp against the
# trusted flat mapping.


def _triple(event) -> tuple:
    """Return the equivalence key (agent, to_agent, handoff_state)."""
    return (event.agent, event.to_agent, event.handoff_state)


def _flat_events(scenario: str) -> list:
    adapter = OTelSpanAdapter.from_json_file(str(_FLAT_FIXTURE), scenario=scenario)
    return list(adapter.events())


def _otlp_events(scenario: str, tmp_path: pathlib.Path) -> list:
    otlp = _load_otlp()
    sub_doc = otlp[scenario]
    tmp_file = tmp_path / f"{scenario}_otlp.json"
    tmp_file.write_text(json.dumps(sub_doc))
    adapter = OTelSpanAdapter.from_otlp_file(str(tmp_file))
    return list(adapter.events())


def test_equivalence_deadlock_triples(tmp_path):
    """OTLP deadlock triples (agent, to_agent, handoff_state) match flat fixture IN ORDER."""
    flat = _flat_events("deadlock")
    otlp = _otlp_events("deadlock", tmp_path)
    assert len(flat) == len(otlp)
    for flat_e, otlp_e in zip(flat, otlp):
        assert _triple(flat_e) == _triple(otlp_e), (
            f"Mismatch: flat={_triple(flat_e)} otlp={_triple(otlp_e)}"
        )


def test_equivalence_deadlock_detector(tmp_path):
    """OTLP deadlock events produce the same detect_deadlock report as flat events."""
    flat_reports = detect_deadlock(_flat_events("deadlock"))
    otlp_reports = detect_deadlock(_otlp_events("deadlock", tmp_path))
    assert len(flat_reports) == len(otlp_reports) == 1
    assert flat_reports[0].blocked_agents == otlp_reports[0].blocked_agents


def test_equivalence_ping_pong_triples(tmp_path):
    """OTLP ping_pong triples match flat fixture IN ORDER."""
    flat = _flat_events("ping_pong")
    otlp = _otlp_events("ping_pong", tmp_path)
    assert len(flat) == len(otlp)
    for flat_e, otlp_e in zip(flat, otlp):
        assert _triple(flat_e) == _triple(otlp_e)


def test_equivalence_ping_pong_detector(tmp_path):
    """OTLP ping_pong events produce the same detect_ping_pong report as flat events."""
    cfg = DetectionConfig(use_handoff_edges=True)
    flat_reports = detect_ping_pong(_flat_events("ping_pong"), config=cfg)
    otlp_reports = detect_ping_pong(_otlp_events("ping_pong", tmp_path), config=cfg)
    assert len(flat_reports) == len(otlp_reports) == 1
    assert flat_reports[0].members == otlp_reports[0].members


def test_equivalence_control_triples(tmp_path):
    """OTLP control triples match flat fixture IN ORDER."""
    flat = _flat_events("control")
    otlp = _otlp_events("control", tmp_path)
    assert len(flat) == len(otlp)
    for flat_e, otlp_e in zip(flat, otlp):
        assert _triple(flat_e) == _triple(otlp_e)


def test_equivalence_control_both_detectors(tmp_path):
    """OTLP control events produce [] from both detectors, matching the flat fixture."""
    cfg = DetectionConfig(use_handoff_edges=True)
    otlp = _otlp_events("control", tmp_path)
    assert detect_deadlock(otlp) == []
    assert detect_ping_pong(otlp, config=cfg) == []


# ---------------------------------------------------------------------------
# _otlp_attr_value decode
# ---------------------------------------------------------------------------


def test_otlp_attr_value_string():
    """stringValue decodes to Python str."""
    assert _otlp_attr_value({"stringValue": "hello"}) == "hello"
    assert isinstance(_otlp_attr_value({"stringValue": "hello"}), str)


def test_otlp_attr_value_int_as_string():
    """intValue encoded as a JSON string decodes to Python int."""
    result = _otlp_attr_value({"intValue": "42"})
    assert result == 42
    assert isinstance(result, int)


def test_otlp_attr_value_int_as_number():
    """intValue as a JSON number also decodes to Python int."""
    result = _otlp_attr_value({"intValue": 42})
    assert result == 42
    assert isinstance(result, int)


def test_otlp_attr_value_bool():
    """boolValue decodes to Python bool."""
    assert _otlp_attr_value({"boolValue": True}) is True
    assert _otlp_attr_value({"boolValue": False}) is False


def test_otlp_attr_value_double():
    """doubleValue decodes to Python float."""
    result = _otlp_attr_value({"doubleValue": 3.14})
    assert abs(result - 3.14) < 1e-9
    assert isinstance(result, float)


def test_otlp_attr_value_unknown_kind_returns_none():
    """Unknown value kinds return None (caller should skip the attribute)."""
    assert _otlp_attr_value({"arrayValue": [1, 2, 3]}) is None
    assert _otlp_attr_value({"kvlistValue": {}}) is None
    assert _otlp_attr_value({}) is None


# ---------------------------------------------------------------------------
# startTimeUnixNano -> ISO round-trip
# ---------------------------------------------------------------------------


def test_start_time_unix_nano_whole_second():
    """Whole-second startTimeUnixNano converts to '...Z' ISO string."""
    # 1717200001000000000 ns = 2024-06-01T00:00:01Z
    doc = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "spanId": "0000000000000001",
                                "startTimeUnixNano": "1717200001000000000",
                                "attributes": [
                                    {
                                        "key": "gen_ai.agent.handoff.source.name",
                                        "value": {"stringValue": "test-agent"},
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    spans = _normalize_otlp(doc)
    assert len(spans) == 1
    assert spans[0]["start_time"] == "2024-06-01T00:00:01Z"


def test_start_time_unix_nano_all_flat_timestamps(tmp_path):
    """All OTLP fixture timestamps round-trip to the exact flat fixture ISO strings."""
    flat = _load_flat()
    otlp = _load_otlp()

    for scenario in ("deadlock", "ping_pong", "control"):
        flat_spans = flat["scenarios"][scenario]["spans"]
        sub_doc = otlp[scenario]
        otlp_spans = _normalize_otlp(sub_doc)

        for flat_span, otlp_span in zip(flat_spans, otlp_spans):
            assert otlp_span["start_time"] == flat_span["start_time"], (
                f"{scenario}: OTLP ts {otlp_span['start_time']!r} "
                f"!= flat ts {flat_span['start_time']!r}"
            )


# ---------------------------------------------------------------------------
# events() ordering
# ---------------------------------------------------------------------------


def test_events_sorted_by_ts_then_span_id():
    """OTelSpanAdapter.events() yields events sorted by (start_time, span_id)."""
    spans = [
        {
            "span_id": "zzz",
            "start_time": "2024-01-01T00:00:03Z",
            "attributes": {"gen_ai.agent.handoff.source.name": "agent-c"},
        },
        {
            "span_id": "aaa",
            "start_time": "2024-01-01T00:00:01Z",
            "attributes": {"gen_ai.agent.handoff.source.name": "agent-a"},
        },
        {
            "span_id": "mmm",
            "start_time": "2024-01-01T00:00:02Z",
            "attributes": {"gen_ai.agent.handoff.source.name": "agent-b"},
        },
    ]
    adapter = OTelSpanAdapter(spans)
    events = list(adapter.events())
    assert [e.agent for e in events] == ["agent-a", "agent-b", "agent-c"]
    assert [e.ts for e in events] == [
        "2024-01-01T00:00:01Z",
        "2024-01-01T00:00:02Z",
        "2024-01-01T00:00:03Z",
    ]


def test_events_same_ts_sorted_by_span_id():
    """Events with the same ts are stable-sorted by span_id."""
    spans = [
        {
            "span_id": "z-last",
            "start_time": "2024-01-01T00:00:01Z",
            "attributes": {"gen_ai.agent.handoff.source.name": "agent-z"},
        },
        {
            "span_id": "a-first",
            "start_time": "2024-01-01T00:00:01Z",
            "attributes": {"gen_ai.agent.handoff.source.name": "agent-a"},
        },
    ]
    adapter = OTelSpanAdapter(spans)
    events = list(adapter.events())
    # "a-first" < "z-last" lexicographically
    assert events[0].raw_id == "a-first"
    assert events[1].raw_id == "z-last"


def test_events_shuffled_ping_pong_sorted():
    """Reversed ping_pong spans yield events in the correct ts order."""
    data = _load_flat()
    spans = data["scenarios"]["ping_pong"]["spans"]
    reversed_spans = list(reversed(spans))

    adapter = OTelSpanAdapter(reversed_spans)
    events = list(adapter.events())

    tss = [e.ts for e in events]
    assert tss == sorted(tss)


def test_events_none_ts_coerced_to_empty_string():
    """Spans with start_time=None are coerced to '' for sorting (null-first)."""
    spans = [
        {
            "span_id": "b",
            "start_time": "2024-01-01T00:00:01Z",
            "attributes": {"gen_ai.agent.handoff.source.name": "agent-b"},
        },
        {
            "span_id": "a",
            "start_time": None,
            "attributes": {"gen_ai.agent.handoff.source.name": "agent-a"},
        },
    ]
    adapter = OTelSpanAdapter(spans)
    events = list(adapter.events())
    # None ts sorts first (coerced to ''), so agent-a comes first
    assert events[0].agent == "agent-a"
    assert events[1].agent == "agent-b"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_missing_scenario_raises_value_error():
    """from_json_file raises ValueError for a missing scenario, naming available ones."""
    with pytest.raises(ValueError) as exc_info:
        OTelSpanAdapter.from_json_file(str(_FLAT_FIXTURE), scenario="does_not_exist")
    msg = str(exc_info.value)
    assert "does_not_exist" in msg
    # Should name available scenarios
    assert "deadlock" in msg or "available" in msg.lower()


def test_unrecognized_json_shape_raises_value_error(tmp_path):
    """from_json_file raises ValueError for an unrecognized JSON shape."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps({"some_key": "some_value", "other": 42}))

    with pytest.raises(ValueError, match="unrecognized"):
        OTelSpanAdapter.from_json_file(str(bad_file))


def test_missing_file_raises_file_not_found_error(tmp_path):
    """from_json_file raises FileNotFoundError for a nonexistent path."""
    nonexistent = str(tmp_path / "no_such_file.json")
    with pytest.raises(FileNotFoundError):
        OTelSpanAdapter.from_json_file(nonexistent)


def test_from_otlp_file_missing_resource_spans_raises(tmp_path):
    """from_otlp_file raises ValueError when 'resourceSpans' key is absent."""
    bad_file = tmp_path / "not_otlp.json"
    bad_file.write_text(json.dumps({"spans": []}))
    with pytest.raises(ValueError, match="resourceSpans"):
        OTelSpanAdapter.from_otlp_file(str(bad_file))


def test_from_jsonl_file_missing_file_raises(tmp_path):
    """from_jsonl_file raises FileNotFoundError for a nonexistent path."""
    with pytest.raises(FileNotFoundError):
        OTelSpanAdapter.from_jsonl_file(str(tmp_path / "no_such.jsonl"))


def test_from_json_file_with_single_scenario_auto_selects(tmp_path):
    """from_json_file auto-selects the scenario when exactly one is present."""
    single_doc = {
        "scenarios": {
            "only_one": {
                "spans": [
                    {
                        "span_id": "s1",
                        "start_time": "2024-01-01T00:00:00Z",
                        "attributes": {
                            "gen_ai.agent.handoff.source.name": "agent-x",
                        },
                    }
                ]
            }
        }
    }
    tmp_file = tmp_path / "single.json"
    tmp_file.write_text(json.dumps(single_doc))

    adapter = OTelSpanAdapter.from_json_file(str(tmp_file))  # no scenario arg
    events = list(adapter.events())
    assert len(events) == 1
    assert events[0].agent == "agent-x"


def test_from_json_file_top_level_list(tmp_path):
    """from_json_file accepts a top-level JSON array of flat span dicts."""
    spans = [
        {
            "span_id": "x1",
            "start_time": "2024-01-01T00:00:00Z",
            "attributes": {"gen_ai.agent.handoff.source.name": "agent-alpha"},
        }
    ]
    tmp_file = tmp_path / "list.json"
    tmp_file.write_text(json.dumps(spans))

    adapter = OTelSpanAdapter.from_json_file(str(tmp_file))
    events = list(adapter.events())
    assert len(events) == 1
    assert events[0].agent == "agent-alpha"


def test_from_json_file_spans_wrapper(tmp_path):
    """from_json_file accepts a {'spans': [...]} wrapper dict."""
    doc = {
        "spans": [
            {
                "span_id": "y1",
                "start_time": "2024-01-01T00:00:00Z",
                "attributes": {"gen_ai.agent.handoff.source.name": "agent-beta"},
            }
        ]
    }
    tmp_file = tmp_path / "wrapper.json"
    tmp_file.write_text(json.dumps(doc))

    adapter = OTelSpanAdapter.from_json_file(str(tmp_file))
    events = list(adapter.events())
    assert len(events) == 1
    assert events[0].agent == "agent-beta"


# ---------------------------------------------------------------------------
# Resource-level attribute merging
# ---------------------------------------------------------------------------


def test_normalize_otlp_resource_attrs_lower_precedence():
    """Resource-level attributes are overridden by span-level attributes on collision."""
    doc = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "gen_ai.agent.handoff.source.name",
                            "value": {"stringValue": "resource-agent"},
                        },
                        {
                            "key": "resource.only",
                            "value": {"stringValue": "from-resource"},
                        },
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "spanId": "r1",
                                "startTimeUnixNano": "1717200001000000000",
                                "attributes": [
                                    {
                                        "key": "gen_ai.agent.handoff.source.name",
                                        "value": {"stringValue": "span-agent"},
                                    }
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    spans = _normalize_otlp(doc)
    assert len(spans) == 1
    attrs = spans[0]["attributes"]
    # span-level wins
    assert attrs["gen_ai.agent.handoff.source.name"] == "span-agent"
    # resource-only attr is inherited
    assert attrs["resource.only"] == "from-resource"


# ---------------------------------------------------------------------------
# input_tokens enrichment via span_to_event
# ---------------------------------------------------------------------------


def test_span_to_event_input_tokens_present():
    """gen_ai.usage.input_tokens populates Event.input_tokens when present."""
    span = {
        "span_id": "it1",
        "start_time": "2024-01-01T00:00:00Z",
        "attributes": {
            "gen_ai.agent.handoff.source.name": "agent-a",
            "gen_ai.usage.input_tokens": 512,
        },
    }
    event = span_to_event(span)
    assert event.input_tokens == 512


def test_span_to_event_input_tokens_absent():
    """Event.input_tokens is None when gen_ai.usage.input_tokens is absent."""
    span = {
        "span_id": "it2",
        "start_time": "2024-01-01T00:00:00Z",
        "attributes": {"gen_ai.agent.handoff.source.name": "agent-b"},
    }
    event = span_to_event(span)
    assert event.input_tokens is None


def test_span_to_event_cost_usd_and_progress_fixed():
    """cost_usd is always None and progress is always False from span_to_event."""
    span = {
        "span_id": "cp1",
        "start_time": "2024-01-01T00:00:00Z",
        "attributes": {"gen_ai.agent.handoff.source.name": "agent-c"},
    }
    event = span_to_event(span)
    assert event.cost_usd is None
    assert event.progress is False
    assert event.args_hash is None


# ---------------------------------------------------------------------------
# T1 (F1 regression): _normalize_otlp filters non-handoff spans
# ---------------------------------------------------------------------------


def test_normalize_otlp_filters_non_handoff_spans():
    """_normalize_otlp on a doc mixing a chat span (no source.name) + a handoff span
    yields ONLY the handoff event — the non-handoff span is silently skipped.
    """
    doc = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            # Non-handoff span: has gen_ai.operation.name='chat' but NO source.name
                            {
                                "spanId": "nonhandoff001",
                                "startTimeUnixNano": "1717200001000000000",
                                "attributes": [
                                    {
                                        "key": "gen_ai.operation.name",
                                        "value": {"stringValue": "chat"},
                                    },
                                    {
                                        "key": "gen_ai.request.model",
                                        "value": {"stringValue": "claude"},
                                    },
                                ],
                            },
                            # Valid handoff span: has source.name
                            {
                                "spanId": "handoff001",
                                "startTimeUnixNano": "1717200002000000000",
                                "attributes": [
                                    {
                                        "key": "gen_ai.agent.handoff.source.name",
                                        "value": {"stringValue": "planner"},
                                    },
                                    {
                                        "key": "gen_ai.agent.handoff.state",
                                        "value": {"stringValue": "blocked"},
                                    },
                                ],
                            },
                        ]
                    }
                ]
            }
        ]
    }
    spans = _normalize_otlp(doc)
    assert len(spans) == 1, f"Expected 1 handoff span, got {len(spans)}"
    assert spans[0]["attributes"]["gen_ai.agent.handoff.source.name"] == "planner"


# ---------------------------------------------------------------------------
# T2 (F5): sub-second timestamp uses integer formatting
# ---------------------------------------------------------------------------


def _make_minimal_otlp_doc(nano_str: str) -> dict:
    """Build a one-span OTLP doc with the given startTimeUnixNano string."""
    return {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "spanId": "t2test001",
                                "startTimeUnixNano": nano_str,
                                "attributes": [
                                    {
                                        "key": "gen_ai.agent.handoff.source.name",
                                        "value": {"stringValue": "test-agent"},
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }


def test_start_time_sub_second_500ms():
    """1717200001500000000 ns -> '2024-06-01T00:00:01.500000000Z' (integer formatting)."""
    doc = _make_minimal_otlp_doc("1717200001500000000")
    spans = _normalize_otlp(doc)
    assert len(spans) == 1
    assert spans[0]["start_time"] == "2024-06-01T00:00:01.500000000Z"


def test_start_time_sub_second_123456789ns():
    """1717200001123456789 ns -> '2024-06-01T00:00:01.123456789Z' (no float rounding)."""
    doc = _make_minimal_otlp_doc("1717200001123456789")
    spans = _normalize_otlp(doc)
    assert len(spans) == 1
    assert spans[0]["start_time"] == "2024-06-01T00:00:01.123456789Z"


# ---------------------------------------------------------------------------
# T3 (F5 unknown kind): array-valued attributes are skipped, not crashed on
# ---------------------------------------------------------------------------


def test_normalize_otlp_array_value_attr_skipped():
    """An attribute with an unknown kind (arrayValue) is NOT present in flat attrs;
    a normal stringValue attr IS present. Span must have source.name to survive filter.
    """
    doc = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "spanId": "t3-001",
                                "startTimeUnixNano": "1717200001000000000",
                                "attributes": [
                                    {
                                        "key": "gen_ai.agent.handoff.source.name",
                                        "value": {"stringValue": "agent-x"},
                                    },
                                    {
                                        "key": "bad.array.attr",
                                        "value": {"arrayValue": [1, 2, 3]},
                                    },
                                    {
                                        "key": "good.string.attr",
                                        "value": {"stringValue": "yes"},
                                    },
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    spans = _normalize_otlp(doc)
    assert len(spans) == 1
    attrs = spans[0]["attributes"]
    assert "bad.array.attr" not in attrs
    assert attrs.get("good.string.attr") == "yes"
    assert attrs.get("gen_ai.agent.handoff.source.name") == "agent-x"


# ---------------------------------------------------------------------------
# T4 (F6): span_to_event tool field mapping
# ---------------------------------------------------------------------------


def test_span_to_event_tool_execute_tool():
    """gen_ai.operation.name='execute_tool' maps to event.tool == 'execute_tool'."""
    span = {
        "span_id": "t4-001",
        "start_time": "2024-06-01T00:00:01Z",
        "attributes": {
            "gen_ai.agent.handoff.source.name": "agent-x",
            "gen_ai.operation.name": "execute_tool",
        },
    }
    event = span_to_event(span)
    assert event.tool == "execute_tool"


def test_span_to_event_tool_default_dispatch():
    """Absent gen_ai.operation.name defaults event.tool to 'dispatch'."""
    span = {
        "span_id": "t4-002",
        "start_time": "2024-06-01T00:00:01Z",
        "attributes": {
            "gen_ai.agent.handoff.source.name": "agent-y",
        },
    }
    event = span_to_event(span)
    assert event.tool == "dispatch"


# ---------------------------------------------------------------------------
# T5 (F2 adapter-level): span_to_event raises ValueError on missing required fields
# ---------------------------------------------------------------------------


def test_span_to_event_missing_source_name_raises_value_error():
    """span_to_event raises ValueError (not KeyError) when source.name is absent."""
    span = {
        "span_id": "t5-001",
        "start_time": "2024-06-01T00:00:01Z",
        "attributes": {},
    }
    with pytest.raises(ValueError, match="gen_ai.agent.handoff.source.name"):
        span_to_event(span)


def test_span_to_event_missing_start_time_raises_value_error():
    """span_to_event raises ValueError when start_time is absent."""
    span = {
        "span_id": "t5-002",
        "attributes": {"gen_ai.agent.handoff.source.name": "agent-x"},
    }
    with pytest.raises(ValueError, match="start_time"):
        span_to_event(span)


def test_span_to_event_missing_span_id_raises_value_error():
    """span_to_event raises ValueError when span_id is absent."""
    span = {
        "start_time": "2024-06-01T00:00:01Z",
        "attributes": {"gen_ai.agent.handoff.source.name": "agent-x"},
    }
    with pytest.raises(ValueError, match="span_id"):
        span_to_event(span)


# ---------------------------------------------------------------------------
# T6 (F7): OTLP cross-detector negative locks
# ---------------------------------------------------------------------------


def test_otlp_deadlock_scenario_produces_no_ping_pong(tmp_path):
    """OTLP deadlock scenario: detect_ping_pong(use_handoff_edges=True) returns []."""
    otlp = _load_otlp()
    sub_doc = otlp["deadlock"]
    tmp_file = tmp_path / "deadlock.json"
    tmp_file.write_text(json.dumps(sub_doc))
    adapter = OTelSpanAdapter.from_otlp_file(str(tmp_file))
    events = list(adapter.events())
    cfg = DetectionConfig(use_handoff_edges=True)
    assert detect_ping_pong(events, config=cfg) == []


def test_otlp_ping_pong_scenario_produces_no_deadlock(tmp_path):
    """OTLP ping_pong scenario: detect_deadlock returns []."""
    otlp = _load_otlp()
    sub_doc = otlp["ping_pong"]
    tmp_file = tmp_path / "ping_pong.json"
    tmp_file.write_text(json.dumps(sub_doc))
    adapter = OTelSpanAdapter.from_otlp_file(str(tmp_file))
    events = list(adapter.events())
    assert detect_deadlock(events) == []
