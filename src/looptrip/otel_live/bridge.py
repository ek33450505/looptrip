"""bridge.py — map a live ReadableSpan to a looptrip Event.

This module is the seam between the OpenTelemetry SDK's live span objects
and looptrip's normalized :class:`~looptrip.normalize.Event` contract.  It
reuses :func:`~looptrip.adapters.otel.span_to_event` — the same trusted
mapper used by the offline :class:`~looptrip.adapters.otel.OTelSpanAdapter`
— so that live and offline ingestion paths produce byte-identical
:class:`~looptrip.normalize.Event` instances for the same attributes.

Only handoff spans (those whose attributes include
``gen_ai.agent.handoff.source.name``) are mapped; all other spans return
``None`` so the processor can skip them cheaply.

This module is part of the ``looptrip[otel]`` extra and imports from the
OpenTelemetry SDK.  Importing it without the SDK installed raises
:class:`ImportError` — that is intentional.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

# SDK import — lives only in this package (looptrip.otel_live.*).
from opentelemetry.sdk.trace import ReadableSpan  # type: ignore[import]

from looptrip.adapters.otel import span_to_event, unix_nanos_to_iso
from looptrip.normalize import Event

if TYPE_CHECKING:  # pragma: no cover
    pass

__all__ = ["readable_span_to_event"]


def readable_span_to_event(span: ReadableSpan) -> Optional[Event]:
    """Map a live :class:`opentelemetry.sdk.trace.ReadableSpan` to a looptrip
    :class:`~looptrip.normalize.Event`, or return ``None`` for non-handoff
    spans.

    A span is a handoff span if its attributes include the key
    ``gen_ai.agent.handoff.source.name``; all other spans are skipped
    (mirrors the filter in :func:`~looptrip.adapters.otel._normalize_otlp`).

    The conversion path deliberately reuses
    :func:`~looptrip.adapters.otel.span_to_event` so that the live and
    offline ingestion paths share one mapper and cannot drift apart.

    Field derivation
    ----------------
    ``span_id``    — ``format(span.context.span_id, "016x")`` (int → 16-char
                     hex string matching the OTLP ``spanId`` format).
    ``start_time`` — :func:`~looptrip.adapters.otel.unix_nanos_to_iso` applied
                     to ``span.start_time`` (an integer nanosecond timestamp).
    ``attributes`` — ``dict(span.attributes or {})``; a plain Python dict so
                     that :func:`~looptrip.adapters.otel.span_to_event` can
                     perform ordinary ``dict.get()`` lookups.

    Args:
        span: A live ``ReadableSpan`` from the OpenTelemetry SDK.  Both
              started-but-not-ended spans (received by
              :meth:`~looptrip.otel_live.processor.LooptripSpanProcessor.on_start`)
              and finished spans are accepted.

    Returns:
        A frozen :class:`~looptrip.normalize.Event` for handoff spans;
        ``None`` for all other spans.
    """
    attrs = dict(span.attributes or {})
    if "gen_ai.agent.handoff.source.name" not in attrs:
        return None
    flat = {
        "span_id": format(span.context.span_id, "016x"),
        "start_time": unix_nanos_to_iso(span.start_time),
        "attributes": attrs,
    }
    return span_to_event(flat)
