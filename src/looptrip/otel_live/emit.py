"""emit.py — OpenTelemetry log emitter for looptrip pathology reports.

:class:`LooptripLogEmitter` translates a
:class:`~looptrip.detectors.types.PathologyReport` into an OTel log record
emitted via the OTel Logs API.  The record carries structured attributes so
that log processors, exporters, and downstream dashboards can filter and
aggregate looptrip detections without parsing the human-readable body.

Example::

    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import (
        SimpleLogRecordProcessor,
        ConsoleLogExporter,
    )
    from looptrip.otel_live import LooptripLogEmitter, LooptripSpanProcessor

    lp = LoggerProvider()
    lp.add_log_record_processor(
        SimpleLogRecordProcessor(ConsoleLogExporter())
    )
    emitter = LooptripLogEmitter(logger_provider=lp)
    processor = LooptripSpanProcessor(emitter=emitter)

.. note::

    The OTel Logs API (``opentelemetry._logs``) is marked as pre-stable
    upstream and may change in future SDK releases.

This module is part of the ``looptrip[otel]`` extra and imports from the
OpenTelemetry SDK.  Importing it without the SDK installed raises
:class:`ImportError` — that is intentional.
"""

from __future__ import annotations

# SDK imports — live only in looptrip.otel_live.*.
from opentelemetry._logs import SeverityNumber, get_logger  # type: ignore[import]

__all__ = ["LooptripLogEmitter"]


class LooptripLogEmitter:
    """Emit OTel log records for looptrip :class:`~looptrip.detectors.types.PathologyReport` objects.

    Resolves an OTel :class:`~opentelemetry._logs.Logger` from one of three
    sources (in priority order):

    1. An explicit ``logger`` argument (highest priority; use for testing).
    2. A ``logger_provider`` argument — calls
       ``logger_provider.get_logger("looptrip")``.
    3. The global OTel logger via :func:`opentelemetry._logs.get_logger`
       (lowest priority; suitable for applications that configure a global
       :class:`~opentelemetry.sdk._logs.LoggerProvider`).

    Args:
        logger:          A pre-built OTel :class:`~opentelemetry._logs.Logger`.
                         Takes priority over all other arguments.
        logger_provider: An OTel
                         :class:`~opentelemetry.sdk._logs.LoggerProvider` from
                         which a ``"looptrip"`` logger is obtained.
    """

    def __init__(self, logger=None, logger_provider=None) -> None:
        if logger is not None:
            self._logger = logger
        elif logger_provider is not None:
            self._logger = logger_provider.get_logger("looptrip")
        else:
            self._logger = get_logger("looptrip")

    def emit(self, report) -> None:
        """Emit one OTel log record for a pathology report.

        Emits a ``WARN``-severity log record with
        ``event_name="looptrip.pathology"`` and structured attributes so that
        log exporters and dashboards can filter and aggregate looptrip events
        without parsing the body text.

        The body is a concise human-readable sentence naming the ``kind``,
        ``agent``, and ``occurrences``.

        Attributes emitted
        ------------------
        ``looptrip.kind``             — pathology kind string (e.g.
                                        ``"duplicate_work"``).
        ``looptrip.agent``            — the acting agent tied to the trip.
        ``looptrip.occurrences``      — total event count for the pathology.
        ``looptrip.prevented_runs``   — post-trip dispatch count that would
                                        have been averted.
        ``looptrip.prevented_cost_usd`` — sum of ``cost_usd`` over post-trip
                                          events (float; ``0.0`` when unknown).

        Args:
            report: A :class:`~looptrip.detectors.types.PathologyReport`
                from any of the four detectors.
        """
        body = (
            f"looptrip detected {report.kind} on agent {report.agent!r} "
            f"({report.occurrences} occurrences)"
        )
        self._logger.emit(
            severity_number=SeverityNumber.WARN,
            severity_text="WARN",
            body=body,
            attributes={
                "looptrip.kind": report.kind,
                "looptrip.agent": report.agent,
                "looptrip.occurrences": report.occurrences,
                "looptrip.prevented_runs": report.prevented_runs,
                "looptrip.prevented_cost_usd": report.prevented_cost,
            },
            event_name="looptrip.pathology",
        )
