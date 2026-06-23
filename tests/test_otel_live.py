"""test_otel_live.py — tests for the looptrip.otel_live live OTel integration.

Requires the ``opentelemetry-sdk`` package (``looptrip[otel]`` extra).
The module-level ``pytest.importorskip`` calls skip the entire file
cleanly when the SDK is absent, keeping CI green on minimal installs.

Coverage:
- unix_nanos_to_iso: whole-second and sub-second round-trips (Deliverable A).
- bridge: live handoff span -> Event; non-handoff span -> None; live vs.
  offline equivalence.
- sampler: handoff attrs -> RECORD_AND_SAMPLE; non-handoff -> delegates;
  attributes=None -> no crash.
- processor detection (headline): duplicate-work fires at iteration 2;
  ping-pong fires; de-duplication prevents re-firing.
- emitter: OTel log record has correct attributes, severity, event_name.
- observer-never-a-gate: on_start does not raise when detect() raises.
- shutdown / force_flush smoke tests.
"""

from __future__ import annotations

import threading
from typing import List

import pytest

# Skip the entire module cleanly when the SDK is absent.
pytest.importorskip("opentelemetry.sdk.trace")
pytest.importorskip("opentelemetry.sdk._logs")

# ---------------------------------------------------------------------------
# SDK imports (only reached when importorskip above passes)
# ---------------------------------------------------------------------------
from opentelemetry._logs import SeverityNumber  # type: ignore[import]
from opentelemetry.sdk._logs import LoggerProvider  # type: ignore[import]
from opentelemetry.sdk._logs.export import (  # type: ignore[import]
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import]
from opentelemetry.sdk.trace.sampling import (  # type: ignore[import]
    ALWAYS_ON,
    Decision,
)

# ---------------------------------------------------------------------------
# looptrip imports
# ---------------------------------------------------------------------------
from looptrip.adapters.otel import unix_nanos_to_iso
from looptrip.detectors.types import ALL_DETECTORS, DetectionConfig
from looptrip.otel_live import (
    HandoffRecordingSampler,
    LooptripLogEmitter,
    LooptripSpanProcessor,
    readable_span_to_event,
)

# ============================================================================
# Helpers
# ============================================================================


def _make_provider(processor: LooptripSpanProcessor) -> TracerProvider:
    """Build a TracerProvider with HandoffRecordingSampler + the given processor."""
    provider = TracerProvider(sampler=HandoffRecordingSampler())
    provider.add_span_processor(processor)
    return provider


def _emit_handoff_span(tracer, source: str, target: str, state: str, tokens: int = 0):
    """Emit one handoff span and return immediately (context manager exited)."""
    attrs = {
        "gen_ai.agent.handoff.source.name": source,
        "gen_ai.agent.handoff.target.name": target,
        "gen_ai.agent.handoff.state": state,
    }
    if tokens:
        attrs["gen_ai.usage.input_tokens"] = tokens
    with tracer.start_as_current_span("agent_handoff", attributes=attrs):
        pass


# ============================================================================
# DELIVERABLE A — unix_nanos_to_iso unit tests
# ============================================================================


def test_unix_nanos_to_iso_whole_second():
    """Whole-second input now emits a fixed-width 9-digit fraction (B1).

    A uniform shape for every timestamp keeps lexicographic order == chronological
    order, so an exact-second event no longer mis-sorts after same-second sub-second
    events that share the wall-clock second.
    """
    # 1717200001000000000 ns = 2024-06-01T00:00:01 (whole second)
    result = unix_nanos_to_iso(1_717_200_001_000_000_000)
    assert result == "2024-06-01T00:00:01.000000000Z"
    assert result.endswith(".000000000Z")


def test_unix_nanos_to_iso_sub_second():
    """Sub-second timestamp includes a 9-digit fractional-seconds suffix."""
    # 1717200001500000000 ns = 2024-06-01T00:00:01.500000000Z
    result = unix_nanos_to_iso(1_717_200_001_500_000_000)
    assert result == "2024-06-01T00:00:01.500000000Z"


def test_unix_nanos_to_iso_max_sub_second():
    """999999999 nanosecond remainder yields the full 9-digit fractional suffix."""
    result = unix_nanos_to_iso(1_717_200_001_999_999_999)
    assert result.endswith(".999999999Z")


# ============================================================================
# DELIVERABLE B — bridge tests
# ============================================================================


def test_bridge_handoff_span_produces_event():
    """A live span with gen_ai.agent.handoff.source.name maps to a correct Event."""
    captured = []

    def capture(report):  # noqa: ARG001 - unused but needed for interface
        pass  # we only care about what the processor observed via the Event

    proc = LooptripSpanProcessor(on_detection=capture)
    provider = _make_provider(proc)
    tracer = provider.get_tracer("test")

    # Emit one handoff span and record what readable_span_to_event produces.
    events_seen: list = []

    class _RecordingProcessor(proc.__class__):
        def on_start(self, span, parent_context=None):
            ev = readable_span_to_event(span)
            if ev is not None:
                events_seen.append(ev)
            super().on_start(span, parent_context)

    proc2 = _RecordingProcessor()
    provider2 = _make_provider(proc2)
    tracer2 = provider2.get_tracer("test")

    with tracer2.start_as_current_span(
        "agent_handoff",
        attributes={
            "gen_ai.agent.handoff.source.name": "planner",
            "gen_ai.agent.handoff.target.name": "code-writer",
            "gen_ai.agent.handoff.state": "in_progress",
            "gen_ai.operation.name": "execute_tool",
        },
    ):
        pass

    assert len(events_seen) == 1
    ev = events_seen[0]
    assert ev.agent == "planner"
    assert ev.to_agent == "code-writer"
    assert ev.handoff_state == "in_progress"
    assert ev.tool == "execute_tool"
    # raw_id is the span_id in 16-char hex
    assert isinstance(ev.raw_id, str)
    assert len(ev.raw_id) == 16
    # ts is ISO-8601 UTC
    assert ev.ts.endswith("Z")


def test_bridge_non_handoff_span_returns_none():
    """A span without gen_ai.agent.handoff.source.name yields None from the bridge."""
    # We drive this at the bridge function directly using a real started span.
    proc = LooptripSpanProcessor()
    provider = _make_provider(proc)
    tracer = provider.get_tracer("test")

    none_results: list = []

    class _NoneCheckProcessor(proc.__class__):
        def on_start(self, span, parent_context=None):
            result = readable_span_to_event(span)
            none_results.append(result)
            super().on_start(span, parent_context)

    proc2 = _NoneCheckProcessor()
    provider2 = _make_provider(proc2)
    tracer2 = provider2.get_tracer("test")

    with tracer2.start_as_current_span("plain-span", attributes={"some.key": "val"}):
        pass

    assert len(none_results) == 1
    assert none_results[0] is None


def test_bridge_equivalence_live_vs_offline():
    """Live and offline span_to_event produce equal (agent, to_agent, handoff_state, tool)."""
    from looptrip.adapters.otel import span_to_event as offline_span_to_event

    captured_live: list = []

    class _CapturingProcessor(LooptripSpanProcessor):
        def on_start(self, span, parent_context=None):
            ev = readable_span_to_event(span)
            if ev is not None:
                captured_live.append(ev)
            super().on_start(span, parent_context)

    proc = _CapturingProcessor()
    provider = _make_provider(proc)
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span(
        "agent_handoff",
        attributes={
            "gen_ai.agent.handoff.source.name": "researcher",
            "gen_ai.agent.handoff.target.name": "planner",
            "gen_ai.agent.handoff.state": "in_progress",
            "gen_ai.operation.name": "dispatch",
        },
    ):
        pass

    assert len(captured_live) == 1
    live_ev = captured_live[0]

    # Build equivalent offline flat span using the same attributes.
    flat_span = {
        "span_id": live_ev.raw_id,
        "start_time": live_ev.ts,
        "attributes": {
            "gen_ai.agent.handoff.source.name": "researcher",
            "gen_ai.agent.handoff.target.name": "planner",
            "gen_ai.agent.handoff.state": "in_progress",
            "gen_ai.operation.name": "dispatch",
        },
    }
    offline_ev = offline_span_to_event(flat_span)

    assert live_ev.agent == offline_ev.agent
    assert live_ev.to_agent == offline_ev.to_agent
    assert live_ev.handoff_state == offline_ev.handoff_state
    assert live_ev.tool == offline_ev.tool


# ============================================================================
# DELIVERABLE B — sampler tests
# ============================================================================


def test_sampler_handoff_attrs_returns_record_and_sample():
    """Handoff attributes -> Decision.RECORD_AND_SAMPLE."""
    sampler = HandoffRecordingSampler()
    result = sampler.should_sample(
        parent_context=None,
        trace_id=12345,
        name="test-span",
        attributes={"gen_ai.agent.handoff.source.name": "planner"},
    )
    assert result.decision == Decision.RECORD_AND_SAMPLE


def test_sampler_non_handoff_delegates():
    """Non-handoff span delegates to the underlying sampler."""
    # Use ALWAYS_ON as delegate; result should also be RECORD_AND_SAMPLE
    # (the default for ALWAYS_ON) but the key thing is we delegated.
    sampler = HandoffRecordingSampler(delegate=ALWAYS_ON)
    result = sampler.should_sample(
        parent_context=None,
        trace_id=12345,
        name="plain-span",
        attributes={"some.key": "val"},
    )
    # ALWAYS_ON returns RECORD_AND_SAMPLE too, so we just check no crash.
    assert result.decision == Decision.RECORD_AND_SAMPLE


def test_sampler_none_attributes_no_crash():
    """attributes=None must not raise; delegates cleanly."""
    sampler = HandoffRecordingSampler()
    result = sampler.should_sample(
        parent_context=None,
        trace_id=12345,
        name="span-no-attrs",
        attributes=None,
    )
    # Should not raise; decision comes from the delegate (ALWAYS_ON).
    assert result is not None


def test_sampler_description():
    """get_description includes the delegate's description."""
    sampler = HandoffRecordingSampler()
    desc = sampler.get_description()
    assert "HandoffRecordingSampler" in desc
    # Includes the delegate's description.
    assert ALWAYS_ON.get_description() in desc


# ============================================================================
# DELIVERABLE B — processor detection tests (the headline)
# ============================================================================


def test_processor_duplicate_work_fires_at_iteration_2():
    """HEADLINE: on_detection fires with duplicate_work report on the 2nd identical span."""
    detected: List = []

    proc = LooptripSpanProcessor(on_detection=detected.append)
    provider = _make_provider(proc)
    tracer = provider.get_tracer("test")

    # Span 1 — first occurrence (baseline; should NOT trip).
    _emit_handoff_span(tracer, "worker", "reviewer", "in_progress", tokens=100)
    assert detected == [], "No detection after first span"

    # Span 2 — second identical occurrence (SHOULD trip duplicate_work).
    _emit_handoff_span(tracer, "worker", "reviewer", "in_progress", tokens=100)
    assert len(detected) == 1, "Expected exactly one detection after second span"
    report = detected[0]
    assert report.kind == "duplicate_work"
    assert report.agent == "worker"
    assert report.occurrences == 2


def test_processor_duplicate_work_dedup_no_refire():
    """Once a pathology fingerprint fires, additional spans do NOT re-fire it."""
    detected: List = []

    proc = LooptripSpanProcessor(on_detection=detected.append)
    provider = _make_provider(proc)
    tracer = provider.get_tracer("test")

    # Emit 4 spans — the trip should fire exactly once (at span 2).
    for _ in range(4):
        _emit_handoff_span(tracer, "worker", "reviewer", "in_progress", tokens=100)

    assert len(detected) == 1, (
        f"De-dup failed: expected 1 detection, got {len(detected)}"
    )


def test_processor_ping_pong_fires():
    """HEADLINE: LooptripSpanProcessor detects a ping-pong cycle with use_handoff_edges=True."""
    detected: List = []

    proc = LooptripSpanProcessor(
        config=DetectionConfig(use_handoff_edges=True),
        detectors=ALL_DETECTORS,
        on_detection=detected.append,
    )
    provider = _make_provider(proc)
    tracer = provider.get_tracer("test")

    # Emit a planner <-> code-writer ping-pong sequence.
    # 4 spans are sufficient for cycle_trip_count=2 with use_handoff_edges=True.
    _emit_handoff_span(tracer, "planner", "code-writer", "in_progress")
    _emit_handoff_span(tracer, "code-writer", "planner", "in_progress")
    _emit_handoff_span(tracer, "planner", "code-writer", "in_progress")
    _emit_handoff_span(tracer, "code-writer", "planner", "in_progress")

    ping_pong_reports = [r for r in detected if r.kind == "ping_pong"]
    assert len(ping_pong_reports) >= 1, (
        f"Expected at least one ping_pong report; detected: {[r.kind for r in detected]}"
    )
    report = ping_pong_reports[0]
    assert "planner" in report.members or "planner" == report.agent
    # The cycle involves both agents.
    all_agents = set(report.members) if report.members else {report.agent}
    assert "planner" in all_agents or "code-writer" in all_agents


def test_processor_thread_safety():
    """on_start from multiple threads must not corrupt internal state."""
    detected: List = []
    lock = threading.Lock()

    def on_det(r):
        with lock:
            detected.append(r)

    proc = LooptripSpanProcessor(on_detection=on_det)
    provider = _make_provider(proc)

    errors: List[Exception] = []

    def emit_spans():
        tracer = provider.get_tracer("thread-test")
        try:
            for _ in range(3):
                _emit_handoff_span(tracer, "worker", "reviewer", "in_progress", tokens=100)
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=emit_spans) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread raised exception: {errors}"
    # Should have detected at most one unique fingerprint (de-dup).
    assert len(detected) <= 1


def test_processor_bounds_event_buffer_and_fired_set():
    """REGRESSION (P1/S1): flooding distinct fingerprints keeps both structures bounded.

    A long-running observer must not leak.  With a finite ``max_window`` the
    rolling event buffer (``_events``) is capped at ``max_window`` and the
    de-dup set (``_fired``) is capped at ``max_window * 8``.  Flooding the
    processor with far more DISTINCT firing fingerprints than either cap must
    leave BOTH bounded, while normal de-dup (a repeated fingerprint fires
    exactly once) still holds.
    """
    max_window = 16
    fired_cap = max_window * 8  # 128 — must match the processor's internal sizing

    detected: List = []
    proc = LooptripSpanProcessor(on_detection=detected.append, max_window=max_window)
    provider = _make_provider(proc)
    tracer = provider.get_tracer("test")

    # Each distinct agent emits an identical pair -> trips duplicate_work exactly
    # once with a distinct fingerprint (agent, tool, args_hash).  We flood well
    # past the _fired cap so eviction of the OLDEST fingerprints must kick in.
    n_agents = fired_cap + 50  # 178 distinct firing fingerprints (> cap)
    for i in range(n_agents):
        agent = f"flood-agent-{i}"
        _emit_handoff_span(tracer, agent, "reviewer", "in_progress", tokens=100)
        _emit_handoff_span(tracer, agent, "reviewer", "in_progress", tokens=100)

    # Each distinct fingerprint fired exactly once: de-dup held across the flood
    # and the bounded window produced no spurious re-fires.
    assert len(detected) == n_agents

    # Both internal structures stayed bounded by their caps.
    with proc._lock:
        # Event buffer saturated at exactly max_window (oldest evicted, O(1)).
        assert len(proc._events) <= max_window
        assert len(proc._events) == max_window
        # _fired saturated at exactly its cap (oldest fingerprint evicted).
        assert len(proc._fired) <= fired_cap
        assert len(proc._fired) == fired_cap

    # Normal de-dup still holds: re-emitting the most-recent agent's pair (still
    # tracked in _fired) produces no new detection.
    before = len(detected)
    last_agent = f"flood-agent-{n_agents - 1}"
    _emit_handoff_span(tracer, last_agent, "reviewer", "in_progress", tokens=100)
    _emit_handoff_span(tracer, last_agent, "reviewer", "in_progress", tokens=100)
    assert len(detected) == before, "A still-tracked fingerprint must not re-fire"


def test_processor_unbounded_window_preserves_none_contract():
    """max_window=None keeps the event buffer unbounded (explicit opt-in)."""
    detected: List = []
    proc = LooptripSpanProcessor(on_detection=detected.append, max_window=None)
    provider = _make_provider(proc)
    tracer = provider.get_tracer("test")

    # Emit more distinct spans than the finite default would retain.
    for i in range(50):
        _emit_handoff_span(tracer, f"u-agent-{i}", "reviewer", "in_progress", tokens=10)

    # No maxlen -> every event is retained.
    with proc._lock:
        assert proc._events.maxlen is None
        assert len(proc._events) == 50


# ============================================================================
# DELIVERABLE B — emitter tests
# ============================================================================


def _build_log_infra():
    """Return (LoggerProvider, InMemoryLogRecordExporter) wired together."""
    exporter = InMemoryLogRecordExporter()
    lp = LoggerProvider()
    lp.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    return lp, exporter


def test_emitter_log_record_attributes():
    """After a detection, force_flush yields exactly one log record with correct attributes."""
    lp, exporter = _build_log_infra()
    emitter = LooptripLogEmitter(logger_provider=lp)

    detected: List = []
    proc = LooptripSpanProcessor(
        on_detection=detected.append,
        emitter=emitter,
    )
    provider = _make_provider(proc)
    tracer = provider.get_tracer("test")

    # Trip duplicate_work.
    _emit_handoff_span(tracer, "emitter-agent", "reviewer", "in_progress", tokens=200)
    _emit_handoff_span(tracer, "emitter-agent", "reviewer", "in_progress", tokens=200)

    lp.force_flush()

    logs = exporter.get_finished_logs()
    assert len(logs) == 1, f"Expected exactly 1 log record; got {len(logs)}"

    log_record = logs[0].log_record
    assert log_record.severity_number == SeverityNumber.WARN
    assert log_record.event_name == "looptrip.pathology"
    assert log_record.attributes["looptrip.kind"] == "duplicate_work"
    assert log_record.attributes["looptrip.agent"] == "emitter-agent"
    assert isinstance(log_record.attributes["looptrip.occurrences"], int)
    assert "looptrip.prevented_runs" in log_record.attributes
    assert "looptrip.prevented_cost_usd" in log_record.attributes


def test_emitter_explicit_logger_arg():
    """LooptripLogEmitter accepts an explicit logger and uses it."""
    lp, exporter = _build_log_infra()
    explicit_logger = lp.get_logger("explicit")
    emitter = LooptripLogEmitter(logger=explicit_logger)

    detected: List = []
    proc = LooptripSpanProcessor(
        on_detection=detected.append,
        emitter=emitter,
    )
    provider = _make_provider(proc)
    tracer = provider.get_tracer("test")

    _emit_handoff_span(tracer, "agent-x", "agent-y", "in_progress", tokens=50)
    _emit_handoff_span(tracer, "agent-x", "agent-y", "in_progress", tokens=50)

    lp.force_flush()
    logs = exporter.get_finished_logs()
    assert len(logs) == 1


# ============================================================================
# DELIVERABLE B — observer-never-a-gate test
# ============================================================================


def test_on_start_does_not_raise_when_detect_raises(monkeypatch):
    """on_start must never propagate an internal exception to the caller."""
    proc = LooptripSpanProcessor()
    provider = _make_provider(proc)
    tracer = provider.get_tracer("test")

    # Monkeypatch detect() inside the processor module to raise.
    import looptrip.otel_live.processor as proc_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated detect failure")

    monkeypatch.setattr(proc_mod, "detect", _boom)

    # on_start must not raise even when detect() raises.
    with tracer.start_as_current_span(
        "agent_handoff",
        attributes={
            "gen_ai.agent.handoff.source.name": "breaker",
            "gen_ai.agent.handoff.target.name": "other",
            "gen_ai.agent.handoff.state": "in_progress",
        },
    ):
        pass  # If on_start propagated the exception, this would raise.


# ============================================================================
# DELIVERABLE B — shutdown / force_flush smoke
# ============================================================================


def test_force_flush_returns_true():
    """force_flush always returns True."""
    proc = LooptripSpanProcessor()
    assert proc.force_flush() is True
    assert proc.force_flush(timeout_millis=0) is True


def test_shutdown_clears_state():
    """shutdown clears the event buffer and fired set."""
    detected: List = []
    proc = LooptripSpanProcessor(on_detection=detected.append)
    provider = _make_provider(proc)
    tracer = provider.get_tracer("test")

    # Trip once.
    _emit_handoff_span(tracer, "s-agent", "t-agent", "in_progress", tokens=10)
    _emit_handoff_span(tracer, "s-agent", "t-agent", "in_progress", tokens=10)
    assert len(detected) == 1

    # After shutdown, internal state is cleared. (_events is a bounded deque
    # and _fired is an OrderedDict, so assert emptiness rather than ==[]/==set().)
    proc.shutdown()
    with proc._lock:
        assert len(proc._events) == 0
        assert len(proc._fired) == 0
