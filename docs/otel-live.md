# Live OTel Instrumentation (Phase 4b)

`looptrip.otel_live` brings looptrip's pathology detection into your live
OpenTelemetry pipeline.  Register one `TracerProvider` processor and
looptrip watches the span stream in real time, firing callbacks or emitting
OTel log records the moment a coordination pathology is detected.

> **Requires the `[otel]` extra:**
> ```
> pip install looptrip[otel]
> ```
> The `looptrip.otel_live` package imports from `opentelemetry-sdk` and
> raises `ImportError` if the SDK is absent.  The core `looptrip` package
> remains stdlib-only.

## Quick start

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    SimpleLogRecordProcessor,
    ConsoleLogExporter,
)
from looptrip.otel_live import (
    HandoffRecordingSampler,
    LooptripLogEmitter,
    LooptripSpanProcessor,
)

# Optional: emit a structured OTel log record for each detected pathology.
log_provider = LoggerProvider()
log_provider.add_log_record_processor(
    SimpleLogRecordProcessor(ConsoleLogExporter())
)

# Build the processor.
processor = LooptripSpanProcessor(
    emitter=LooptripLogEmitter(logger_provider=log_provider),
    on_detection=lambda r: print(f"[looptrip] {r.kind} on {r.agent!r}"),
)

# Wire into your TracerProvider.
provider = TracerProvider(sampler=HandoffRecordingSampler())
provider.add_span_processor(processor)
```

## Components

### `LooptripSpanProcessor`

A `SpanProcessor` that converts handoff spans to looptrip `Event` objects
and runs the configured detectors on each arrival.

```python
LooptripSpanProcessor(
    config=None,         # DetectionConfig (None = looptrip defaults)
    detectors=None,      # tuple of KIND_* strings (None = duplicate-work only)
    on_detection=None,   # Callable[[PathologyReport], None]
    emitter=None,        # LooptripLogEmitter
    max_window=None,     # int: rolling buffer size (None = unbounded)
)
```

To enable all four detectors:

```python
from looptrip.detectors.types import ALL_DETECTORS, DetectionConfig

proc = LooptripSpanProcessor(
    config=DetectionConfig(use_handoff_edges=True),
    detectors=ALL_DETECTORS,
    on_detection=my_callback,
)
```

### `HandoffRecordingSampler`

A composable `Sampler` that forces `RECORD_AND_SAMPLE` for any span
carrying `gen_ai.agent.handoff.source.name`, regardless of the host
application's sampling strategy.  All other spans are delegated to the
optional `delegate` sampler (default: `ALWAYS_ON`).

```python
from looptrip.otel_live import HandoffRecordingSampler

provider = TracerProvider(sampler=HandoffRecordingSampler())
# Or wrap an existing sampler:
provider = TracerProvider(sampler=HandoffRecordingSampler(delegate=my_sampler))
```

### `LooptripLogEmitter`

Emits one OTel log record per detected pathology:

```python
LooptripLogEmitter(
    logger=None,           # Explicit OTel Logger (highest priority)
    logger_provider=None,  # LoggerProvider; calls get_logger("looptrip")
    # If neither is provided, uses opentelemetry._logs.get_logger("looptrip")
)
```

Emitted log record attributes:

| Attribute | Type | Description |
|---|---|---|
| `looptrip.kind` | `str` | Pathology kind (`"duplicate_work"`, `"ping_pong"`, etc.) |
| `looptrip.agent` | `str` | Acting agent most directly tied to the trip |
| `looptrip.occurrences` | `int` | Total event count for the pathology |
| `looptrip.prevented_runs` | `int` | Post-trip dispatch count that would have been averted |
| `looptrip.prevented_cost_usd` | `float` | Sum of `cost_usd` over post-trip events |

Event name: `"looptrip.pathology"`.  Severity: `WARN`.

> **OTel Logs are pre-stable.** The `opentelemetry._logs` API is marked as
> pre-stable upstream and may change in future SDK releases.

### `readable_span_to_event`

The low-level bridge from a live `ReadableSpan` to a looptrip `Event`:

```python
from looptrip.otel_live import readable_span_to_event

ev = readable_span_to_event(span)  # None for non-handoff spans
```

Reuses `looptrip.adapters.otel.span_to_event` so live and offline
ingestion paths produce byte-identical `Event` instances for the same
attributes.

## Design notes

### Observer, never a gate

`LooptripSpanProcessor.on_start` and `on_end` wrap their bodies in a broad
`try/except` that swallows all internal errors.  A bug in looptrip can
never raise into the instrumented application and interrupt a running span.

### Detection at span start

The `gen_ai.agent.handoff.*` convention sets handoff attributes at span
*creation* time, so they are available in `on_start`.  Running detection
in `on_start` gives the earliest possible pathology signal.  `on_end` is
a no-op; offline `looptrip attribute` handles post-hoc attribution analysis.

### De-duplication

Each pathology fingerprint `(report.kind, report.signature)` fires at most
once per processor lifetime, regardless of how many subsequent events
accumulate.  This prevents callback storms on long-running runaways.

### Thread safety

The rolling event list and fired-fingerprint set are both guarded by a
`threading.Lock`.  `on_start` can safely be called from multiple
application threads simultaneously.

### Bounded memory

Set `max_window` to keep memory bounded for long-running services:

```python
proc = LooptripSpanProcessor(max_window=100)
```

When the buffer exceeds `max_window`, the oldest event is dropped.  This
may cause some pathologies to be missed; tune to your workload.

## Known limitations

- **Synthetic / unit-tested only.** Live-capture validation against a real
  production multi-agent workload is future work.  This implementation is
  validated via the looptrip test suite with a synthetic OTel TracerProvider
  only.
- **`args_hash` is always `None` for live spans.** The `span_to_event`
  mapper sets `args_hash=None`; duplicate-work detection therefore relies on
  `gen_ai.usage.input_tokens` token-variance proximity rather than exact
  argument comparison.  Set `gen_ai.usage.input_tokens` on your handoff
  spans for reliable duplicate-work detection.
- **OTel Logs pre-stable.** The `opentelemetry._logs` API may change in
  future SDK releases; pin `opentelemetry-sdk` to a tested version.
- **Detection complexity is O(N) per handoff span** (N = buffered events).
  For long-lived services with many handoffs, set `max_window` to keep
  detection bounded.
