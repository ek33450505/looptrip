# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — Phase 4b

### Added

- **`looptrip.otel_live` package:** Live OpenTelemetry SDK integration for
  real-time pathology detection.  Requires the `[otel]` extra
  (`opentelemetry-sdk`); the core `looptrip` package remains stdlib-only.
  - `LooptripSpanProcessor` — a `SpanProcessor` that converts handoff spans
    to `Event` objects and runs the configured detectors on each arrival.
    Observer-never-a-gate: `on_start`/`on_end` never raise into the
    application.  Thread-safe; de-duplicates pathology reports by fingerprint.
  - `HandoffRecordingSampler` — a composable `Sampler` that forces
    `RECORD_AND_SAMPLE` for any span carrying
    `gen_ai.agent.handoff.source.name`, delegating all other spans to the
    host application's existing sampler.
  - `LooptripLogEmitter` — emits one OTel log record per detected pathology
    (`event_name="looptrip.pathology"`, severity `WARN`, structured
    attributes `looptrip.kind`, `looptrip.agent`, etc.).
  - `readable_span_to_event` — low-level bridge from a live `ReadableSpan`
    to a looptrip `Event`, reusing `span_to_event` so live and offline
    ingestion paths are identical.
- **`unix_nanos_to_iso` helper** extracted from `_normalize_otlp` as a
  module-level function in `looptrip.adapters.otel` so that both offline
  OTLP flattening and live span bridging share the identical nanosecond →
  ISO-8601 UTC conversion logic.
- **`docs/otel-live.md`:** Full documentation for live OTel instrumentation
  including quick start, component API reference, design notes, and known
  limitations.

## [Unreleased] — Phase 4a

### Added

- **OTelSpanAdapter:** Shipped offline OTel GenAI span adapter (`src/looptrip/adapters/otel.py`).
  Ingests flat span dicts, real OTLP/JSON exports, and JSONL files via three factory methods
  (`from_json_file`, `from_jsonl_file`, `from_otlp_file`).  Auto-detects input shape from
  `resourceSpans`, `scenarios`, or `spans` keys.
- **`_normalize_otlp`:** Flattens real OTLP/JSON `resourceSpans` exports including resource-level
  attribute inheritance, `startTimeUnixNano` → ISO-8601 UTC conversion, and typed attribute decoding.
- **`otel:` CLI source:** `looptrip scan` and `looptrip attribute` now accept `otel:<path>` and
  `otel:<path>#<scenario>` sources, including JSONL (`otel:<path>.jsonl`).  Bad file paths exit 2
  cleanly with no traceback.
- **OTLP fixture:** `tests/fixtures/otel_genai_handoff_spans_otlp.json` — synthetic OTLP-shaped
  fixture encoding all three reference scenarios (deadlock, ping_pong, control) with
  `startTimeUnixNano` values that round-trip to the exact ISO timestamps in the flat fixture.
- **Reference test wired to shipped code:** `tests/test_otel_handoff_reference.py` now imports
  `span_to_event` from `looptrip.adapters.otel` (the local `otel_span_to_event` function has been
  removed).  All 16 reference tests exercise the shipped mapper.

## [Unreleased] — Phase 2

### Added

- **looptrip.detectors subpackage:** Three new structural detectors closing the Phase-1 blind spot.
  - `ping_pong` / livelock detector: Detects A→B→A→B cycles token-independently.
  - `deadlock` detector: Detects Chandy-Misra-Haas wait-for cycles (requires `handoff_state`).
  - `non_termination` detector: Detects unbounded event growth with state plateaus, token-independently.
- **DetectionConfig sensitivity knobs:** 17 tunable fields for detector behavior, including token tolerance, cycle lengths, plateau thresholds, and framework-specific state tokens (`terminal_states`, `blocked_states`).
- **detect_all() and detect(detectors=...) library API:** Run all four detectors or select a subset; Phase-1 default unchanged for backward compatibility.
- **Structural pathology support:** Frame-agnostic detection of livelock, deadlock, and non-termination in addition to duplicate-work.

### Notes

- All Phase-2 additions are **fully backward compatible.** The default `detect(events)` call continues to run the Phase-1 duplicate-work detector only.
- The `looptrip scan` CLI currently exposes the Phase-1 detector; CLI support for Phase-2 detectors via flags is planned.

## [0.1.0] — Phase 1

### Added

- **cast.db adapter:** Read normalized events from a CAST agent-framework `cast.db` observability database.
- **duplicate-work detector:** Deterministic detection of signature-repeat pathologies with configurable input-token tolerance.
- **iteration-2 trip:** Hermetic proof on two real recorded multi-agent runaways demonstrating $792.96 in prevented waste.
- **looptrip proof CLI:** Packaged, reproducible proof with committed fixture data.
- **Library API:** Programmatic access to event normalization and detector logic.

### License

- Apache License 2.0
