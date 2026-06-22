"""processor.py — live OpenTelemetry SpanProcessor for looptrip detection.

:class:`LooptripSpanProcessor` is an OpenTelemetry
:class:`~opentelemetry.sdk.trace.SpanProcessor` that watches the live span
stream, converts handoff spans into looptrip
:class:`~looptrip.normalize.Event` objects, runs the configured pathology
detectors, and fires callbacks or emits OTel log records when a pathology
is detected — all without blocking or interrupting the instrumented
application.

Design principles
-----------------
**Observer, never a gate.**  ``on_start`` and ``on_end`` must never raise
into the application.  Both methods wrap their bodies in a broad
``try/except`` that swallows internal errors.  A bug in looptrip must never
crash the instrumented app.

**Detection at span start.**  The ``gen_ai.agent.handoff.*`` convention sets
handoff attributes at span *creation* time, so they are available in
``on_start``.  Running :func:`~looptrip.detector.detect` in ``on_start``
gives the earliest possible pathology signal.  ``on_end`` is intentionally
a no-op (see its docstring); offline ``looptrip attribute`` covers post-hoc
attribution analysis.

**Thread safety.**  ``on_start`` is called from application threads.  The
rolling event list (``_events``) and the fired-fingerprint set (``_fired``)
are both guarded by a :class:`threading.Lock`.

**Detection complexity.**  Running :func:`~looptrip.detector.detect` on
every handoff span arrival is O(N) in the number of buffered events (bounded
by ``max_window``).  For typical multi-agent workflows with a bounded number
of handoffs, this is acceptable.  If you need tighter bounds, set
``max_window`` to a small value (e.g. ``50``); the oldest events are dropped
when the window is exceeded, which may cause some pathologies to be missed
but keeps the per-span overhead constant.

**De-duplication.**  Once a pathology fingerprint has fired
``(report.kind, report.signature)``, it is added to ``_fired`` and will
never fire again for the lifetime of the processor, regardless of how many
more events accumulate.  This prevents callback storms on long-running
runaways.

Example::

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

    # Wire the log emitter (optional).
    lp = LoggerProvider()
    lp.add_log_record_processor(SimpleLogRecordProcessor(ConsoleLogExporter()))

    processor = LooptripSpanProcessor(
        emitter=LooptripLogEmitter(logger_provider=lp),
        on_detection=lambda r: print(f"Detected: {r.kind} on {r.agent}"),
    )
    provider = TracerProvider(sampler=HandoffRecordingSampler())
    provider.add_span_processor(processor)

.. note::

    Live capture validation against a real production multi-agent workload
    is future work — this implementation is synthetic/unit-tested only.
    See docs/otel-live.md for known limitations.

This module is part of the ``looptrip[otel]`` extra and imports from the
OpenTelemetry SDK.  Importing it without the SDK installed raises
:class:`ImportError` — that is intentional.
"""

from __future__ import annotations

import threading
from typing import Callable, List, Optional, Set, Tuple

# SDK import — lives only in looptrip.otel_live.*.
from opentelemetry.sdk.trace import SpanProcessor  # type: ignore[import]

from looptrip.detector import detect
from looptrip.detectors.types import DetectionConfig, PathologyReport
from looptrip.normalize import Event

from .bridge import readable_span_to_event
from .emit import LooptripLogEmitter

__all__ = ["LooptripSpanProcessor"]


class LooptripSpanProcessor(SpanProcessor):
    """Live OTel :class:`~opentelemetry.sdk.trace.SpanProcessor` for pathology detection.

    Registers with a :class:`~opentelemetry.sdk.trace.TracerProvider` and
    watches the live span stream.  Handoff spans (those carrying
    ``gen_ai.agent.handoff.source.name``) are converted to looptrip
    :class:`~looptrip.normalize.Event` objects and fed into
    :func:`~looptrip.detector.detect` on every arrival.

    Args:
        config:       A :class:`~looptrip.detectors.types.DetectionConfig`
                      controlling detection sensitivity.  ``None`` uses the
                      looptrip defaults (duplicate-work only; see
                      :func:`~looptrip.detector.detect`).
        detectors:    Tuple of ``KIND_*`` strings selecting which detectors
                      to run.  ``None`` runs duplicate-work only (the
                      :func:`~looptrip.detector.detect` default).  Pass
                      :data:`~looptrip.detectors.types.ALL_DETECTORS` to
                      enable all four.
        on_detection: Callable invoked with each new
                      :class:`~looptrip.detectors.types.PathologyReport` the
                      first time its fingerprint is seen.  Called outside the
                      internal lock (safe for I/O).
        emitter:      A :class:`LooptripLogEmitter` that emits one OTel log
                      record per new report.  Called outside the internal
                      lock.
        max_window:   Maximum number of events kept in the rolling buffer.
                      When exceeded, the oldest event is dropped (FIFO).
                      ``None`` means unbounded (suitable for finite-duration
                      workflows; set a value for long-running services).
    """

    def __init__(
        self,
        config: Optional[DetectionConfig] = None,
        detectors: Optional[Tuple[str, ...]] = None,
        on_detection: Optional[Callable] = None,
        emitter: Optional[LooptripLogEmitter] = None,
        max_window: Optional[int] = None,
    ) -> None:
        self._config = config
        self._detectors = detectors
        self._on_detection = on_detection
        self._emitter = emitter
        self._max_window = max_window

        self._events: List[Event] = []
        self._fired: Set[Tuple] = set()
        self._lock = threading.Lock()

    def on_start(self, span, parent_context=None) -> None:
        """Called when a span is started.

        Converts the span to a looptrip :class:`~looptrip.normalize.Event`
        (skipping non-handoff spans), appends it to the rolling buffer,
        runs the selected detectors, and dispatches any new pathology reports.

        **Observer rule:** this method never raises.  Any internal exception
        is swallowed so that a looptrip bug cannot crash the instrumented app.

        **Detection:** Uses attributes present at span start time, consistent
        with the ``gen_ai.agent.handoff.*`` convention which sets handoff
        attributes at span creation.

        Args:
            span:           The newly started OTel span (a ``ReadableSpan``
                            at start time).
            parent_context: The parent :class:`~opentelemetry.context.Context`
                            (may be ``None``).
        """
        try:
            ev = readable_span_to_event(span)
            if ev is None:
                return

            with self._lock:
                self._events.append(ev)
                if self._max_window and len(self._events) > self._max_window:
                    # Drop oldest to maintain the bounded window.
                    self._events = self._events[-self._max_window:]
                reports = detect(
                    list(self._events),
                    config=self._config,
                    detectors=self._detectors,
                )
                new_reports: List[PathologyReport] = []
                for report in reports:
                    fp = (report.kind, report.signature)
                    if fp not in self._fired:
                        self._fired.add(fp)
                        new_reports.append(report)

            # Dispatch outside the lock so callbacks are not held under it.
            for report in new_reports:
                self._dispatch(report)

        except Exception:
            # Observer never a gate: swallow all internal errors.
            pass

    def on_end(self, span) -> None:
        """No-op: live counterfactual attribution is intentionally deferred.

        Detection runs at :meth:`on_start` because handoff attributes are
        set at span creation (per the ``gen_ai.agent.handoff.*`` convention).
        Re-running detection in ``on_end`` would produce duplicate reports
        for the same events without yielding new information.

        Post-hoc attribution analysis (which requires completed spans with
        full timing data) is handled by the offline ``looptrip attribute``
        CLI command — not by this processor.
        """
        pass

    def _dispatch(self, report: PathologyReport) -> None:
        """Invoke registered callbacks for a newly detected pathology.

        Both ``on_detection`` and the ``emitter`` are called, in that order.
        This method does not hold the internal lock and is safe for I/O.

        Args:
            report: The newly detected :class:`~looptrip.detectors.types.PathologyReport`.
        """
        if self._on_detection is not None:
            self._on_detection(report)
        if self._emitter is not None:
            self._emitter.emit(report)

    def shutdown(self) -> None:
        """Clear internal state; called when the :class:`~opentelemetry.sdk.trace.TracerProvider` shuts down."""
        with self._lock:
            self._events.clear()
            self._fired.clear()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """No-op flush; always returns ``True``.

        Detection is synchronous and in-process: there is no async export
        pipeline to flush.

        Args:
            timeout_millis: Ignored.

        Returns:
            Always ``True``.
        """
        return True
