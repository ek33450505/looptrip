# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.2] — 2026-06-23

Audit-remediation release: a four-dimension code audit (bugs, security,
performance, test coverage) surfaced 15 findings — all independently verified and
fixed here. No public API was removed. Two observable changes live in the optional
`[otel]` surface (see **Changed**).

### Changed

- **Uniform OTel timestamps (determinism fix).** `unix_nanos_to_iso` now always
  emits a fixed-width 9-digit fractional component, so whole-second values render
  as `2024-06-01T00:00:01.000000000Z` (previously `…01Z`). Downstream ordering is a
  lexicographic string compare, and the old two-shape output could sort an
  exact-second event *after* sub-second events in the same second, corrupting the
  event order fed to the detectors. The uniform shape makes lexicographic order
  equal chronological order.
- **Bounded live `LooptripSpanProcessor`.** The rolling event buffer is now backed
  by a `collections.deque(maxlen=max_window)` and `max_window` has a finite default
  (was `None`/unbounded); the de-duplication fired-set is bounded as well. This caps
  memory and per-span work under long-running or adversarial span streams. Pass
  `max_window=None` explicitly to restore the previous unbounded behavior.
- **Portable cast.db live loader.** The cast.db scripts directory now resolves from
  the `CAST_DB_SCRIPTS_DIR` environment variable (default `~/.claude/scripts`)
  instead of a hardcoded absolute path, so live `cast-db:<id>` mode works on any
  machine.

### Fixed

- **Null-safe event ordering.** The CLI and `looptrip proof` sort paths no longer
  raise `TypeError` when a cast.db row has a `NULL` started_at; the sort key now
  honors the adapter's documented null-first ordering.
- **Clean errors on hostile input.** Deeply-nested or oversized trace files now exit
  2 cleanly instead of escaping the error contract as an uncaught `RecursionError`
  traceback; ingestion enforces a file-size cap and a JSONL span-count cap.
- **No `sys.path` shadowing.** The cast.db loader loads its helper via an explicit
  `importlib` file spec rather than permanently inserting a directory at
  `sys.path[0]`, removing a module-shadowing vector.

### Performance

- Hoisted redundant per-event allocations out of the detector hot loops (skip list
  re-copies when the input is already a list; precompute the non-termination
  exempt-set unions and the deadlock lowercased blocked-states set once per call).
  Behavior-preserving — detection output is unchanged.

### Internal / CI

- New `tests/test_stdlib_core.py` asserts the core package imports with
  OpenTelemetry absent and that `looptrip.otel_live` raises `ImportError`, guarding
  the stdlib-only-core contract. CI adds a no-`[otel]` job and now tests Python
  3.10, 3.11, 3.12, and 3.13 (matching the package classifiers).

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
