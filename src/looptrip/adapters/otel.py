"""otel.py — the OpenTelemetry GenAI span adapter for looptrip.

Translates flat or real OTLP/JSON GenAI handoff spans into the normalized
:class:`~looptrip.normalize.Event` stream the detectors consume.

Three sourcing modes:

* :meth:`OTelSpanAdapter.from_json_file` — auto-detects the input shape:
  a dict with ``resourceSpans`` is treated as OTLP/JSON; a dict with
  ``scenarios`` is a multi-scenario flat fixture; a dict with ``spans`` or a
  top-level list is a plain flat-span collection.
* :meth:`OTelSpanAdapter.from_jsonl_file` — one flat span dict per non-blank
  line; e.g. a streaming export.
* :meth:`OTelSpanAdapter.from_otlp_file` — explicit OTLP/JSON entry point;
  always calls :func:`_normalize_otlp`.

OTel GenAI attribute provenance
--------------------------------
``gen_ai.agent.handoff.source.name`` (PR #98, adopted verbatim)
    The agent performing the handoff → ``Event.agent``.

``gen_ai.agent.handoff.target.name`` (PR #98, adopted verbatim)
    The agent receiving the handoff → ``Event.to_agent`` (explicit field).

``gen_ai.agent.handoff.state`` (looptrip-proposed, pending upstream)
    Bare state token → ``Event.handoff_state``. When truthy, ``to_agent``
    is also populated from the target attribute; when absent, both fields
    are ``None``.

``gen_ai.operation.name``
    Action kind → ``Event.tool`` (default: ``"dispatch"``).

``gen_ai.usage.input_tokens``
    Prompt-token count → ``Event.input_tokens`` (optional enrichment).

This module is stdlib-only and defines no global mutable state.
Live SpanProcessor ingestion (Phase 4b) is not included here.
"""

from __future__ import annotations

import datetime
import json
from typing import Any, Dict, Iterator, List, Optional

from looptrip.normalize import Adapter, Event


def span_to_event(span: Dict[str, Any]) -> Event:
    """Map one flat OTel GenAI handoff span dict to a looptrip :class:`Event`.

    The ``span`` argument uses the **flat** shape::

        {
            "span_id":    "<str>",
            "start_time": "<ISO-8601 UTC string>",
            "attributes": {
                "gen_ai.agent.handoff.source.name": "<str>",   # required
                "gen_ai.operation.name": "<str>",              # default "dispatch"
                "gen_ai.agent.handoff.state": "<str>",         # optional
                "gen_ai.agent.handoff.target.name": "<str>",   # optional
                "gen_ai.usage.input_tokens": <int>,            # optional
            },
        }

    Field mapping
    -------------
    ``gen_ai.agent.handoff.source.name`` → ``Event.agent`` (required).
    ``gen_ai.operation.name`` → ``Event.tool`` (default ``"dispatch"``).
    ``start_time`` → ``Event.ts``.
    ``span_id`` → ``Event.raw_id``.
    ``gen_ai.agent.handoff.state`` (truthy) → ``Event.handoff_state`` (bare token).
    ``gen_ai.agent.handoff.target.name`` (when state truthy) → ``Event.to_agent``.
    ``gen_ai.usage.input_tokens`` → ``Event.input_tokens`` (when present).
    ``cost_usd``, ``progress``, ``args_hash`` → always ``None`` / ``False`` / ``None``.

    Args:
        span: A flat span dict as described above.

    Returns:
        A frozen :class:`~looptrip.normalize.Event`.

    Raises:
        ValueError: when required fields (``gen_ai.agent.handoff.source.name``,
            ``start_time``, or ``span_id``) are absent from the span.
    """
    attrs: Dict[str, Any] = span.get("attributes", {})

    if "gen_ai.agent.handoff.source.name" not in attrs:
        raise ValueError(
            "span_to_event: span attributes missing required 'gen_ai.agent.handoff.source.name'"
        )
    agent: str = attrs["gen_ai.agent.handoff.source.name"]
    tool: str = attrs.get("gen_ai.operation.name", "dispatch")
    if "start_time" not in span:
        raise ValueError("span_to_event: span is missing required 'start_time' field")
    ts: str = span["start_time"]
    if "span_id" not in span:
        raise ValueError("span_to_event: span is missing required 'span_id' field")
    raw_id: Any = span["span_id"]

    state: Optional[str] = attrs.get("gen_ai.agent.handoff.state")
    target: Optional[str] = attrs.get("gen_ai.agent.handoff.target.name")

    if state:
        handoff_state: Optional[str] = state
        to_agent: Optional[str] = target
    else:
        handoff_state = None
        to_agent = None

    input_tokens_raw = attrs.get("gen_ai.usage.input_tokens")
    if input_tokens_raw is None:
        input_tokens: Optional[int] = None
    else:
        try:
            input_tokens = int(input_tokens_raw)
        except (TypeError, ValueError):
            input_tokens = None

    return Event(
        agent=agent,
        tool=tool,
        args_hash=None,
        ts=ts,
        handoff_state=handoff_state,
        to_agent=to_agent,
        input_tokens=input_tokens,
        cost_usd=None,
        progress=False,
        raw_id=raw_id,
    )


class OTelSpanAdapter(Adapter):
    """Adapter from flat OTel GenAI handoff span dicts to normalized events.

    Holds a list of flat span dicts (after any OTLP normalization) and yields
    one :class:`~looptrip.normalize.Event` per span, sorted by
    ``(start_time, span_id)`` to satisfy the non-decreasing-ts contract
    regardless of input order.

    Args:
        spans: A list of flat span dicts; see :func:`span_to_event`.
    """

    def __init__(self, spans: Optional[List[Dict[str, Any]]] = None) -> None:
        self._spans: List[Dict[str, Any]] = spans if spans is not None else []

    @classmethod
    def from_json_file(
        cls,
        path: str,
        scenario: Optional[str] = None,
    ) -> "OTelSpanAdapter":
        """Load spans from a JSON file, auto-detecting the input shape.

        Accepted shapes (checked in this order):

        1. ``{"resourceSpans": [...]}`` — OTLP/JSON export; delegates to
           :func:`_normalize_otlp`.
        2. ``{"scenarios": {"<name>": {"spans": [...]}, ...}}`` — multi-scenario
           flat fixture. ``scenario`` must name an available key unless exactly
           one non-``_``-prefixed scenario is present.
        3. ``{"spans": [...]}`` — single flat-span wrapper dict.
        4. ``[...]`` (top-level list) — bare list of flat span dicts.

        Args:
            path:     Filesystem path to the JSON file.
            scenario: Scenario key for ``{"scenarios": ...}`` files. Ignored
                      for other shapes.

        Returns:
            An :class:`OTelSpanAdapter` holding the loaded spans.

        Raises:
            ValueError: on an unrecognized JSON shape or an unknown/missing
                        scenario name.
            FileNotFoundError: when ``path`` does not exist.
            json.JSONDecodeError: on malformed JSON.
        """
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)

        spans = cls._dispatch_shape(doc, scenario, path)
        return cls(spans)

    @classmethod
    def from_jsonl_file(cls, path: str) -> "OTelSpanAdapter":
        """Load flat span dicts from a JSON Lines file.

        Each non-blank line must be a JSON object (flat span dict). Lines are
        loaded in file order; :meth:`events` will sort them by
        ``(start_time, span_id)``.

        Args:
            path: Filesystem path to the ``.jsonl`` file.

        Returns:
            An :class:`OTelSpanAdapter` holding the loaded spans.

        Raises:
            FileNotFoundError: when ``path`` does not exist.
            json.JSONDecodeError: on a malformed line.
        """
        spans: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    spans.append(json.loads(stripped))
        return cls(spans)

    @classmethod
    def from_otlp_file(cls, path: str) -> "OTelSpanAdapter":
        """Load spans from a real OTLP/JSON export file.

        The file must contain a single ``{"resourceSpans": [...]}`` document.
        Delegates to :func:`_normalize_otlp` for flattening.

        Args:
            path: Filesystem path to the OTLP JSON file.

        Returns:
            An :class:`OTelSpanAdapter` holding the normalized flat spans.

        Raises:
            FileNotFoundError: when ``path`` does not exist.
            json.JSONDecodeError: on malformed JSON.
            ValueError: when ``resourceSpans`` key is absent.
        """
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict) or "resourceSpans" not in doc:
            raise ValueError(
                f"from_otlp_file: expected a dict with 'resourceSpans' key; "
                f"got {sorted(doc.keys()) if isinstance(doc, dict) else type(doc).__name__!r}"
            )
        spans = _normalize_otlp(doc)
        return cls(spans)

    def events(self) -> Iterator[Event]:
        """Yield normalized events sorted by ``(start_time, span_id)`` (non-decreasing ts).

        ``None`` timestamps are coerced to ``''`` for the sort key (null-first,
        matching SQLite NULL ordering convention) to prevent :class:`TypeError`
        when mixed with string-timestamped spans.
        """
        ordered = sorted(
            self._spans,
            key=lambda s: (s.get("start_time") or "", s.get("span_id") or ""),
        )
        for span in ordered:
            yield span_to_event(span)

    @classmethod
    def _dispatch_shape(
        cls,
        doc: Any,
        scenario: Optional[str],
        path: str,
    ) -> List[Dict[str, Any]]:
        """Detect the JSON shape of ``doc`` and return a list of flat span dicts.

        Args:
            doc:      The decoded JSON value from the file.
            scenario: Scenario name (for the ``scenarios`` shape only).
            path:     Original file path for error messages.

        Returns:
            A list of flat span dicts.

        Raises:
            ValueError: on unrecognized shape or missing/unknown scenario.
        """
        if isinstance(doc, list):
            return list(doc)

        if not isinstance(doc, dict):
            raise ValueError(
                f"unrecognized JSON shape in {path!r}: expected a dict or list; "
                f"got {type(doc).__name__!r}"
            )

        if "resourceSpans" in doc:
            return _normalize_otlp(doc)

        if "scenarios" in doc:
            scenarios: Dict[str, Any] = doc["scenarios"]
            available = sorted(k for k in scenarios if not k.startswith("_"))
            if scenario is None:
                if len(available) == 1:
                    scenario = available[0]
                else:
                    raise ValueError(
                        f"file {path!r} contains multiple scenarios "
                        f"({available!r}); specify one via scenario= "
                        f"or 'otel:<path>#<scenario>'"
                    )
            if scenario not in scenarios:
                raise ValueError(
                    f"scenario {scenario!r} not found in {path!r}; "
                    f"available: {available!r}"
                )
            scen = scenarios[scenario]
            if not isinstance(scen, dict) or "spans" not in scen:
                raise ValueError(f"scenario {scenario!r} in {path!r} has no 'spans' list")
            return list(scen["spans"])

        if "spans" in doc:
            return list(doc["spans"])

        raise ValueError(
            f"unrecognized JSON shape in {path!r}: expected a dict with "
            f"'resourceSpans', 'scenarios', or 'spans' key; or a top-level list. "
            f"Got keys: {sorted(doc.keys())!r}"
        )


# ---------------------------------------------------------------------------
# OTLP/JSON normalization helpers
# ---------------------------------------------------------------------------


def _otlp_attr_value(value_obj: Dict[str, Any]) -> Any:
    """Decode one OTLP attribute value object to a Python scalar.

    OTLP encodes each attribute value as a one-key typed wrapper::

        {"stringValue": "hello"}
        {"intValue": "42"}       # JSON-safe int64 — the value is a STRING
        {"boolValue": true}
        {"doubleValue": 3.14}

    Unknown wrapper kinds return ``None``; the caller should skip the attribute.

    Args:
        value_obj: The attribute ``value`` dict from an OTLP attribute entry.

    Returns:
        A Python ``str``, ``int``, ``bool``, or ``float``, or ``None`` for
        unknown kinds.
    """
    if "stringValue" in value_obj:
        return str(value_obj["stringValue"])
    if "intValue" in value_obj:
        # Protobuf int64 is encoded as a JSON string to preserve precision.
        return int(value_obj["intValue"])
    if "boolValue" in value_obj:
        return bool(value_obj["boolValue"])
    if "doubleValue" in value_obj:
        return float(value_obj["doubleValue"])
    return None  # unknown kind — caller skips this attribute


def _normalize_otlp(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten a real OTLP/JSON export document into a list of flat span dicts.

    Input shape (``resourceSpans`` export)::

        {
            "resourceSpans": [
                {
                    "resource": {                           # optional
                        "attributes": [{"key": "...", "value": {...}}]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "..."},      # ignored
                            "spans": [
                                {
                                    "spanId": "<16 hex chars>",
                                    "traceId": "...",      # ignored
                                    "name": "...",         # ignored
                                    "startTimeUnixNano": "<int ns as STRING>",
                                    "attributes": [
                                        {"key": "...", "value": {...}}
                                    ],
                                },
                                ...
                            ],
                        },
                        ...
                    ],
                },
                ...
            ]
        }

    Output — one flat span dict per OTLP span::

        {
            "span_id":    "<spanId>",
            "start_time": "<ISO-8601 UTC string>",
            "attributes": {<flat key → scalar dict>},
        }

    Attribute resolution: resource-level attributes have lower precedence;
    span-level attributes win on collision.

    ``startTimeUnixNano`` is converted to ISO-8601 UTC with trailing ``'Z'``.
    Whole-second values produce ``'%Y-%m-%dT%H:%M:%SZ'``; sub-second values
    include fractional seconds.

    Args:
        doc: A ``{"resourceSpans": [...]}`` dict.

    Returns:
        A list of flat span dicts, in document order.
    """
    result: List[Dict[str, Any]] = []

    for resource_span in doc.get("resourceSpans", []):
        # Decode resource-level attributes (lower precedence).
        resource_attrs: Dict[str, Any] = {}
        resource = resource_span.get("resource", {})
        for attr in resource.get("attributes", []):
            key = attr.get("key")
            val = _otlp_attr_value(attr.get("value", {}))
            if key and val is not None:
                resource_attrs[key] = val

        for scope_span in resource_span.get("scopeSpans", []):
            for span in scope_span.get("spans", []):
                # Decode span-level attributes (higher precedence).
                span_attrs: Dict[str, Any] = {}
                for attr in span.get("attributes", []):
                    key = attr.get("key")
                    val = _otlp_attr_value(attr.get("value", {}))
                    if key and val is not None:
                        span_attrs[key] = val

                # Merge: resource attrs first, span attrs override on collision.
                merged_attrs = {**resource_attrs, **span_attrs}

                # Convert startTimeUnixNano (int64 as string) → ISO-8601 UTC.
                nano_str = span.get("startTimeUnixNano", "0")
                ns = int(nano_str)
                secs, rem = divmod(ns, 1_000_000_000)
                dt = datetime.datetime.fromtimestamp(secs, tz=datetime.timezone.utc)
                if rem == 0:
                    start_time = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                else:
                    # Sub-second precision: integer formatting avoids float rounding.
                    start_time = dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{rem:09d}Z"

                # Only include handoff spans — non-handoff spans (chat, tool, etc.)
                # lack gen_ai.agent.handoff.source.name and are silently skipped.
                if "gen_ai.agent.handoff.source.name" not in merged_attrs:
                    continue
                result.append(
                    {
                        "span_id": span.get("spanId", ""),
                        "start_time": start_time,
                        "attributes": merged_attrs,
                    }
                )

    return result
