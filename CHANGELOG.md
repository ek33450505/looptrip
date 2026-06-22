# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] — 2026-06-22

This is the first published artifact to actually contain the OpenTelemetry
support listed below. **0.1.0 was built and uploaded to PyPI before the Phase 4
code merged**, so it shipped without the `looptrip.adapters.otel` module and the
`looptrip.otel_live` package — the `looptrip[otel]` extra installed the
OpenTelemetry dependencies but the import targets were absent. 0.1.1 rebuilds
from the Phase-4-inclusive tree; relative to the intended 0.1.0 the only package
changes are the Phase 4 modules and the new project metadata.

### Added

- **`[project.urls]` metadata** (Homepage, Repository, Issues, Changelog,
  Documentation) so the PyPI project page links back to the repository, issue
  tracker, changelog, and docs.
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
- **OTelSpanAdapter:** Offline OTel GenAI span adapter
  (`src/looptrip/adapters/otel.py`).  Ingests flat span dicts, real OTLP/JSON
  exports, and JSONL files via three factory methods (`from_json_file`,
  `from_jsonl_file`, `from_otlp_file`).  Auto-detects input shape from
  `resourceSpans`, `scenarios`, or `spans` keys.
- **`_normalize_otlp`:** Flattens real OTLP/JSON `resourceSpans` exports including
  resource-level attribute inheritance, `startTimeUnixNano` → ISO-8601 UTC
  conversion, and typed attribute decoding.
- **`otel:` CLI source:** `looptrip scan` and `looptrip attribute` now accept
  `otel:<path>` and `otel:<path>#<scenario>` sources, including JSONL
  (`otel:<path>.jsonl`).  Bad file paths exit 2 cleanly with no traceback.
- **OTLP fixture:** `tests/fixtures/otel_genai_handoff_spans_otlp.json` — synthetic
  OTLP-shaped fixture encoding all three reference scenarios (deadlock, ping_pong,
  control) with `startTimeUnixNano` values that round-trip to the exact ISO
  timestamps in the flat fixture.
- **`docs/otel-live.md`:** Full documentation for live OTel instrumentation
  including quick start, component API reference, design notes, and known
  limitations.
- **Reference test wired to shipped code:** `tests/test_otel_handoff_reference.py`
  imports `span_to_event` from `looptrip.adapters.otel` (the local
  `otel_span_to_event` function has been removed).  All 16 reference tests
  exercise the shipped mapper.

## [0.1.0] — 2026-06-22 — Phase 1, 2 & 3

> Note: everything below — the Phase-2 structural detectors and the Phase-3
> counterfactual-replay attribution included — was present in the published
> 0.1.0 artifact (it was all in the build tree when 0.1.0 was cut). Only the
> Phase 4 OpenTelemetry work — now shipping in 0.1.1 — was missing from 0.1.0.

### Added

- **cast.db adapter:** Read normalized events from a CAST agent-framework `cast.db` observability database.
- **duplicate-work detector:** Deterministic detection of signature-repeat pathologies with configurable input-token tolerance.
- **iteration-2 trip:** Hermetic proof on two real recorded multi-agent runaways demonstrating $792.96 in prevented waste.
- **looptrip proof CLI:** Packaged, reproducible proof with committed fixture data.
- **Library API:** Programmatic access to event normalization and detector logic.
- **looptrip.detectors subpackage:** Three structural detectors closing the Phase-1 blind spot.
  - `ping_pong` / livelock detector: Detects A→B→A→B cycles token-independently.
  - `deadlock` detector: Detects Chandy-Misra-Haas wait-for cycles (requires `handoff_state`).
  - `non_termination` detector: Detects unbounded event growth with state plateaus, token-independently.
- **DetectionConfig sensitivity knobs:** 17 tunable fields for detector behavior, including token tolerance, cycle lengths, plateau thresholds, and framework-specific state tokens (`terminal_states`, `blocked_states`).
- **detect_all() and detect(detectors=...) library API:** Run all four detectors or select a subset; Phase-1 default unchanged for backward compatibility.
- **counterfactual-replay attribution (Phase 3):** `attribution.py` attributes a confirmed pathology to the decisive handoff by counterfactual replay, and reports *overdetermined* when no single handoff is decisive.
- **`looptrip attribute` subcommand + `scan --all` / `scan --detectors LIST`:** the CLI exposes all four detectors (not just duplicate-work) and the attribution workflow.

### Notes

- All Phase-2 additions are **fully backward compatible.** The default `detect(events)` call continues to run the Phase-1 duplicate-work detector only.
- The `looptrip scan` CLI exposes all four detectors via `--all` and `--detectors LIST` (bare `scan` runs duplicate-work only, for backward compatibility), and `looptrip attribute` runs Phase-3 attribution.

### License

- Apache License 2.0
