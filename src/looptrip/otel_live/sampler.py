"""sampler.py — OpenTelemetry sampler that guarantees handoff spans are recorded.

:class:`HandoffRecordingSampler` is a composable
:class:`opentelemetry.sdk.trace.sampling.Sampler` that forces
``RECORD_AND_SAMPLE`` for any span carrying the
``gen_ai.agent.handoff.source.name`` attribute, regardless of what the
delegate sampler would decide.  All other spans are delegated unchanged so
that the host application's existing sampling policy is not disturbed.

Typical usage::

    from opentelemetry.sdk.trace import TracerProvider
    from looptrip.otel_live import HandoffRecordingSampler, LooptripSpanProcessor

    provider = TracerProvider(
        sampler=HandoffRecordingSampler(),
    )
    provider.add_span_processor(LooptripSpanProcessor(on_detection=my_callback))

This module is part of the ``looptrip[otel]`` extra and imports from the
OpenTelemetry SDK.  Importing it without the SDK installed raises
:class:`ImportError` — that is intentional.
"""

from __future__ import annotations

from typing import Optional, Sequence

# SDK imports — live only in looptrip.otel_live.*.
from opentelemetry.sdk.trace.sampling import (  # type: ignore[import]
    ALWAYS_ON,
    Decision,
    Sampler,
    SamplingResult,
)

__all__ = ["HandoffRecordingSampler"]


class HandoffRecordingSampler(Sampler):
    """A composable sampler that ensures handoff spans are always recorded.

    For spans that carry ``gen_ai.agent.handoff.source.name`` in their
    attributes, this sampler returns ``Decision.RECORD_AND_SAMPLE``
    unconditionally — the span will be recorded by the SDK and exported.

    For all other spans, the decision is delegated to the ``delegate``
    sampler (default :data:`opentelemetry.sdk.trace.sampling.ALWAYS_ON`).
    This preserves the host application's existing sampling strategy while
    guaranteeing that looptrip never misses a handoff event.

    Args:
        delegate: Fallback :class:`~opentelemetry.sdk.trace.sampling.Sampler`
            for non-handoff spans.  Defaults to
            :data:`opentelemetry.sdk.trace.sampling.ALWAYS_ON`.

    Notes:
        ``attributes`` passed to :meth:`should_sample` may be ``None``
        (the OTel SDK calls samplers before the span exists, and attributes
        are optional).  This class handles ``None`` attributes safely.
    """

    def __init__(self, delegate: Optional[Sampler] = None) -> None:
        self._delegate: Sampler = delegate if delegate is not None else ALWAYS_ON

    def should_sample(
        self,
        parent_context,
        trace_id,
        name,
        kind=None,
        attributes=None,
        links=None,
        trace_state=None,
    ) -> SamplingResult:
        """Return RECORD_AND_SAMPLE for handoff spans; delegate all others.

        Args:
            parent_context: Parent span context (may be ``None``).
            trace_id:       Trace ID for the new span.
            name:           Span name.
            kind:           Span kind (optional).
            attributes:     Span attributes at creation time (may be ``None``
                            or a mapping).  Checked for the presence of
                            ``gen_ai.agent.handoff.source.name``.
            links:          Span links (optional).
            trace_state:    Trace state (optional).

        Returns:
            A :class:`~opentelemetry.sdk.trace.sampling.SamplingResult`
            with ``Decision.RECORD_AND_SAMPLE`` for handoff spans, or the
            delegate's result for all other spans.
        """
        if attributes and "gen_ai.agent.handoff.source.name" in attributes:
            return SamplingResult(Decision.RECORD_AND_SAMPLE, attributes, trace_state)
        return self._delegate.should_sample(
            parent_context, trace_id, name, kind, attributes, links, trace_state
        )

    def get_description(self) -> str:
        """Return a human-readable description including the delegate's description."""
        return f"HandoffRecordingSampler({self._delegate.get_description()})"
