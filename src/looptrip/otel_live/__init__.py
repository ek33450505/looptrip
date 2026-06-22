"""looptrip.otel_live — live OpenTelemetry SDK integration for looptrip.

This package bridges the OpenTelemetry SDK's live span stream into
looptrip's pathology detection pipeline.  It provides:

* :class:`LooptripSpanProcessor` — a
  :class:`~opentelemetry.sdk.trace.SpanProcessor` that converts handoff
  spans to :class:`~looptrip.normalize.Event` objects and runs the
  configured detectors on each arrival.
* :class:`HandoffRecordingSampler` — a composable
  :class:`~opentelemetry.sdk.trace.sampling.Sampler` that guarantees
  handoff spans are always recorded regardless of the host application's
  sampling strategy.
* :class:`LooptripLogEmitter` — emits one OTel log record per detected
  pathology, with structured attributes for downstream dashboards.
* :func:`readable_span_to_event` — the low-level bridge from a live
  :class:`~opentelemetry.sdk.trace.ReadableSpan` to a looptrip
  :class:`~looptrip.normalize.Event`.

Requires the ``[otel]`` extra (``opentelemetry-sdk``).  Importing this
package without it installed raises :class:`ImportError` — that is
intentional; the core ``looptrip`` package must remain stdlib-only.

Quick start::

    from opentelemetry.sdk.trace import TracerProvider
    from looptrip.otel_live import (
        HandoffRecordingSampler,
        LooptripLogEmitter,
        LooptripSpanProcessor,
    )
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import (
        SimpleLogRecordProcessor, ConsoleLogExporter,
    )

    # Optional: emit log records for each detection.
    lp = LoggerProvider()
    lp.add_log_record_processor(SimpleLogRecordProcessor(ConsoleLogExporter()))

    processor = LooptripSpanProcessor(
        emitter=LooptripLogEmitter(logger_provider=lp),
        on_detection=lambda r: print(f"Detected: {r.kind} on {r.agent}"),
    )
    provider = TracerProvider(sampler=HandoffRecordingSampler())
    provider.add_span_processor(processor)

See ``docs/otel-live.md`` for full documentation including known limitations.
"""

from .bridge import readable_span_to_event
from .emit import LooptripLogEmitter
from .processor import LooptripSpanProcessor
from .sampler import HandoffRecordingSampler

__all__ = [
    "readable_span_to_event",
    "LooptripLogEmitter",
    "LooptripSpanProcessor",
    "HandoffRecordingSampler",
]
