# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
