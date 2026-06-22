# Authoring an Adapter

An **Adapter** is a source-specific producer of normalized `Event` streams. Once you bridge your multi-agent activity log into looptrip's `Event` contract, the pathology detectors work identically—regardless of whether the events came from cast.db, OpenTelemetry, or your own application logs.

## The Adapter Contract

All adapters inherit from `looptrip.normalize.Adapter` and implement a single method:

```python
from looptrip.normalize import Adapter, Event

class MyAdapter(Adapter):
    def events(self) -> Iterator[Event]:
        """Yield normalized events, ordered such that ts is non-decreasing."""
        ...
```

The ordering constraint is **strict**—events must be sorted by their `ts` field in lexicographic order (ISO-8601 strings compare correctly). If you cannot guarantee this, sort before yielding:

```python
def events(self) -> Iterator[Event]:
    sorted_rows = sorted(self._raw_rows, key=lambda r: r.timestamp)
    for row in sorted_rows:
        yield Event(...)
```

## The Event Schema

Each `Event` is a frozen, hashable record:

```python
Event(
    agent: str,              # Identity of the acting agent (load-bearing).
    tool: str,               # Action kind. Constant for sources with no per-action tool column.
    args_hash: Optional[str],  # Stable hash of action args, or None.
    ts: str,                 # ISO-8601 timestamp string.
    handoff_state: Optional[str] = None,  # Bare ## Handoff state token (enrichment; never required).
    to_agent: Optional[str] = None,       # Explicit handoff target agent (enrichment; never required).
    input_tokens: Optional[int] = None,    # Prompt-token count, if known.
    cost_usd: Optional[float] = None,      # Action cost in USD, if known.
    progress: bool = False,  # True iff this event marks a state delta.
    raw_id: Any = None,      # Provenance pointer to the source row (e.g., agent_runs.id).
)
```

The **signature** (load-bearing triple for duplicate-work detection) is:

```python
event.signature() == (agent, tool, args_hash)
```

Two events with identical signatures are duplicate-work candidates; detection confirms a trip using `ts` ordering, input-token variance via `_args_similar()`, and absence of a `progress` delta.

## Field Mapping Guide

### Load-Bearing Fields (Required)

| Event Field | Semantics | How to Populate |
|---|---|---|
| `agent` | Identity of the acting entity | Source column (e.g., `agent_runs.agent` in cast.db, `attributes["service.name"]` in OTel) |
| `tool` | Action kind; constant for sources with no per-action tool column | For structured sources (OTel): hash the operation/endpoint. For unstructured (cast.db): constant like `"dispatch"` |
| `args_hash` | Deterministic SHA-1 digest of action arguments, or None | Call `looptrip.normalize.args_hash_from(*parts)` for structured args; `None` if unavailable (cast.db case) |
| `ts` | ISO-8601 timestamp string; must sort correctly lexicographically | Ensure string format: `"2026-06-21T14:30:00Z"` or similar. Verify ordering before yielding. |

### Enrichment Fields (Optional but Recommended)

| Event Field | Semantics | How to Populate |
|---|---|---|
| `handoff_state` | **Bare** `## Handoff` state token (e.g., `"DONE"`, `"blocked"`, `"waiting"`, `"in_progress"`) — never a packed `"blocked on X"` string | Extract from source if available; `None` for STATUS_CONTRACT_EXEMPT agents or when unavailable. First-class adapters set the bare token directly. Required only by the deadlock detector. |
| `to_agent` | Explicit handoff target agent (the awaited/destination agent), or `None` | First-class adapters (cast.db, OTel) set this directly alongside `handoff_state` — e.g. from `gen_ai.agent.handoff.target.name`. The deadlock detector reads it directly with no delimiter scanning. For a legacy packed `"state on target"` corpus, `looptrip.normalize.split_handoff_state()` is the one-time ingestion seam that splits the string into `(handoff_state, to_agent)`. |
| `input_tokens` | Prompt-token count for the action | Source column (e.g., `agent_runs.input_tokens`). Used by duplicate-work detector for token-variance tolerance. |
| `cost_usd` | Cost in USD, full precision | Source column or computed from input/output tokens. Critical for `prevented_cost` reporting. |
| `progress` | True iff this event marks a progress delta | Extract from source (e.g., `status != "DONE_WITH_CONCERNS"`, or parse `## Handoff` block for explicit progress marker). |
| `raw_id` | Provenance pointer back to the source row | Source primary key or unique identifier; enables forensic backtrace to the original record. |

## Cast.db Adapter: The Worked Example

The cast.db `agent_runs` table maps directly to the simplest adapter case: a source with **no per-dispatch tool/args columns**.

### Source Schema (Relevant Columns)

```sql
agent_runs (
  id INTEGER PRIMARY KEY,
  session_id TEXT,
  agent TEXT,           -- Load-bearing: maps directly to Event.agent
  started_at TEXT,      -- ISO-8601; maps to Event.ts
  input_tokens INTEGER,
  cost_usd REAL,
  status TEXT           -- "DONE", "DONE_WITH_CONCERNS", "BLOCKED", etc.
)
```

### Implementation

```python
from looptrip.normalize import Adapter, Event

class CastDbAdapter(Adapter):
    def __init__(self, session_id: str, *, rows=None, db_query=None):
        self._session_id = session_id
        self._rows = rows
        self._db_query = db_query

    def events(self) -> Iterator[Event]:
        """Yield cast.db agent_runs rows as normalized events."""
        for row in self._ordered_rows():
            yield Event(
                agent=row["agent"],
                tool="dispatch",           # Constant: no per-row tool column
                args_hash=None,            # No args hash available in schema
                ts=row["started_at"],
                handoff_state=None,        # Not available in agent_runs
                to_agent=None,             # No explicit handoff target in agent_runs
                input_tokens=row["input_tokens"],
                cost_usd=row["cost_usd"],
                progress=False,            # Never recorded in agent_runs
                raw_id=row["id"],
            )
```

**Key design decisions:**

- `tool="dispatch"` is a constant—every row is a dispatch, and there is no per-action tool distinction in the schema.
- `args_hash=None` because the table has no args column. Detection relies on `(agent, ts)` repeat + token variance.
- `handoff_state=None` and `to_agent=None` because the table does not store the `## Handoff` block. The deadlock detector will find no wait-for edges and return `[]`.
- `progress=False` because agent_runs does not record intra-run progress deltas. Status moves (e.g., `DONE` → `DONE_WITH_CONCERNS`) are not captured.

See the full implementation: [`src/looptrip/adapters/cast_db.py`](../src/looptrip/adapters/cast_db.py).

## OpenTelemetry GenAI Spans Adapter

`OTelSpanAdapter` in [`src/looptrip/adapters/otel.py`](../src/looptrip/adapters/otel.py) is the shipped offline OTel GenAI span adapter. It translates flat or real OTLP/JSON handoff spans into the normalized `Event` stream the detectors consume.

> **Scope:** This adapter reads from files only.  Live `SpanProcessor`
> ingestion (streaming from the OTel SDK) is implemented in Phase 4b —
> see [docs/otel-live.md](otel-live.md).

### Input shapes

`OTelSpanAdapter` accepts three distinct JSON shapes via three factory methods:

#### 1. Flat span dicts (looptrip native)

Used by `tests/fixtures/otel_genai_handoff_spans.json` and for hand-authored test data:

```json
{
  "span_id":    "span-dl-001",
  "start_time": "2024-06-01T00:00:01Z",
  "attributes": {
    "gen_ai.operation.name":              "execute_tool",
    "gen_ai.agent.handoff.source.name":   "code-writer",
    "gen_ai.agent.handoff.target.name":   "code-reviewer",
    "gen_ai.agent.handoff.state":         "blocked"
  }
}
```

#### 2. Real OTLP/JSON export

The shape produced by an OTel SDK or Collector exporter:

```json
{
  "resourceSpans": [
    {
      "resource": {"attributes": [...]},
      "scopeSpans": [
        {
          "spans": [
            {
              "spanId": "0000000000000001",
              "startTimeUnixNano": "1717200001000000000",
              "attributes": [
                {"key": "gen_ai.agent.handoff.source.name", "value": {"stringValue": "code-writer"}},
                {"key": "gen_ai.agent.handoff.state",       "value": {"stringValue": "blocked"}}
              ]
            }
          ]
        }
      ]
    }
  ]
}
```

`startTimeUnixNano` is a string-encoded int64 nanosecond timestamp; the adapter converts it to ISO-8601 UTC.

#### 3. Multi-scenario flat fixture

A `{"scenarios": {"name": {"spans": [...]}}}` dict (e.g. the packaged fixture). Use the `scenario=` argument or `#scenario` CLI suffix to select one.

### Usage

```python
from looptrip.adapters.otel import OTelSpanAdapter
from looptrip.detector import detect_deadlock, detect_ping_pong
from looptrip.detectors.types import DetectionConfig

# Load from a flat multi-scenario fixture
adapter = OTelSpanAdapter.from_json_file("spans.json", scenario="deadlock")
events = list(adapter.events())
reports = detect_deadlock(events)

# Load from a real OTLP/JSON export (auto-detected or explicit)
adapter = OTelSpanAdapter.from_json_file("otlp_export.json")    # auto-detect via resourceSpans
adapter = OTelSpanAdapter.from_otlp_file("otlp_export.json")    # explicit OTLP entry point

# Load from a JSONL file (one flat span dict per non-blank line)
adapter = OTelSpanAdapter.from_jsonl_file("spans.jsonl")

# CLI: otel: source
# looptrip scan otel:spans.json#deadlock --all
# looptrip scan otel:otlp_export.json
# looptrip scan otel:spans.jsonl
```

### Attribute mapping

| OTel attribute | Event field | Notes |
|---|---|---|
| `gen_ai.agent.handoff.source.name` | `agent` | Required (PR #98, adopted verbatim) |
| `gen_ai.operation.name` | `tool` | Default `"dispatch"` when absent |
| `start_time` / `startTimeUnixNano` | `ts` | ISO-8601 UTC string |
| `span_id` / `spanId` | `raw_id` | Provenance pointer |
| `gen_ai.agent.handoff.state` | `handoff_state` | Bare token; `None` when absent (looptrip-proposed) |
| `gen_ai.agent.handoff.target.name` | `to_agent` | `None` when `handoff_state` absent (PR #98) |
| `gen_ai.usage.input_tokens` | `input_tokens` | Optional enrichment |
| — | `cost_usd` | Always `None` (not in handoff span attrs) |
| — | `args_hash` | Always `None` |
| — | `progress` | Always `False` |

`handoff_state` and `to_agent` are two separate explicit `Event` fields — never packed into one string. When `gen_ai.agent.handoff.state` is absent (completed transfers, CONTROL scenario) both are `None`, leaving the deadlock blocked-map empty.

### OTLP attribute value decoding

The internal `_otlp_attr_value()` helper decodes OTLP typed value wrappers:

| OTLP wrapper | Python type | Note |
|---|---|---|
| `{"stringValue": "..."}` | `str` | |
| `{"intValue": "42"}` | `int` | int64 JSON-encoded as string |
| `{"boolValue": true}` | `bool` | |
| `{"doubleValue": 3.14}` | `float` | |
| unknown kind | skip | attribute is silently omitted |

### CLI source

The `otel:` scheme is accepted by `looptrip scan` and `looptrip attribute`:

```bash
# Flat fixture — select scenario with #
looptrip scan otel:tests/fixtures/otel_genai_handoff_spans.json#deadlock --all

# OTLP export — auto-detected
looptrip scan otel:my_export.json --detectors deadlock,ping_pong

# JSONL
looptrip scan otel:spans.jsonl --all
```

A bad path or malformed JSON exits with code 2 and a clean error message.

## Generic JSONL Adapter (Guidance)

For unstructured logs or simple JSON Line files:

```python
from looptrip.normalize import Adapter, Event, args_hash_from
import json

class JSONLAdapter(Adapter):
    def __init__(self, file_path: str):
        self.file_path = file_path

    def events(self) -> Iterator[Event]:
        lines = []
        with open(self.file_path, "r") as f:
            for line in f:
                if line.strip():
                    lines.append(json.loads(line))
        
        # Sort by timestamp before yielding.
        sorted_lines = sorted(lines, key=lambda x: x.get("timestamp", ""))
        
        for record in sorted_lines:
            # Map your JSONL fields to Event fields.
            # Example: { "agent": "...", "action": "...", "args": {...}, "ts": "...", ... }
            
            args_hash = None
            if "args" in record and record["args"]:
                args_hash = args_hash_from(*[str(v) for v in record["args"].values()])
            
            yield Event(
                agent=record["agent"],
                tool=record.get("action", "unknown"),
                args_hash=args_hash,
                ts=record["timestamp"],
                handoff_state=record.get("state"),       # bare state token
                to_agent=record.get("to_agent"),         # explicit handoff target, or None
                input_tokens=record.get("tokens"),
                cost_usd=record.get("cost"),
                progress=record.get("progress", False),
                raw_id=record.get("id"),
            )
```

**Key points:**

- Always sort by `ts` before yielding, to satisfy the non-decreasing ordering constraint.
- Map your `action` or `operation` field to `tool`; use a namespace prefix if needed (e.g., `"jsonl/my-action"`).
- For `args_hash`, collect action-specific parameters into a stable tuple and pass to `args_hash_from()`. If args are complex objects, serialize them (e.g., `json.dumps(..., sort_keys=True)`) before hashing.
- `handoff_state` is optional enrichment; set it to the bare state token if your logs capture agent state transitions. Put any handoff target in the separate `to_agent` field — never pack them as `"state on target"`. If your legacy logs only have the packed form, run it through `looptrip.normalize.split_handoff_state()` once at ingestion to produce `(handoff_state, to_agent)`.

## Complete Runnable Example

Here is a minimal custom adapter that yields three events, then detects a duplicate-work pathology:

```python
from looptrip.normalize import Adapter, Event
from looptrip.detector import detect

class SimpleAdapter(Adapter):
    def events(self):
        # Three identical events (same signature) with no progress delta.
        # The second is the duplicate-work trip; the third is prevented.
        yield Event(
            agent="test-agent",
            tool="process",
            args_hash=None,
            ts="2026-06-21T10:00:00Z",
            input_tokens=100,
            cost_usd=0.01,
            progress=False,
            raw_id=1,
        )
        yield Event(
            agent="test-agent",
            tool="process",
            args_hash=None,
            ts="2026-06-21T10:01:00Z",
            input_tokens=101,  # Within 5% token tolerance
            cost_usd=0.01,
            progress=False,
            raw_id=2,
        )
        yield Event(
            agent="test-agent",
            tool="process",
            args_hash=None,
            ts="2026-06-21T10:02:00Z",
            input_tokens=102,  # Within 5% token tolerance
            cost_usd=0.01,
            progress=False,
            raw_id=3,
        )

adapter = SimpleAdapter()
reports = detect(adapter.events())

for report in reports:
    print(f"Detected: {report.kind}")
    print(f"  Agent: {report.agent}")
    print(f"  Occurrences: {report.occurrences}")
    print(f"  Trip at: occurrence #{report.trip_index}")
    print(f"  Prevented cost: ${report.prevented_cost:.2f}")
    print(f"  Detail: {report.detail}")

print(f"\nTotal reports: {len(reports)}")
```

### Running the Example

Save the above code and run it via the looptrip venv:

```bash
python /path/to/example.py
```

Actual output:

```
Detected: duplicate_work
  Agent: test-agent
  Occurrences: 3
  Trip at: occurrence #2
  Prevented cost: $0.01
  Detail: 'test-agent' repeated signature ('test-agent', 'process', None) with no progress delta: 3 same-agent dispatches; tripped at occurrence 2 (within 5% input-token variance of the preceding dispatch); 1 subsequent dispatch(es) worth $0.01 would have been averted (raw_id=2).

Total reports: 1
```

## Integration with detect()

Once you have an adapter, feed it into `detect()` or `detect_all()`:

```python
from looptrip.detector import detect, detect_all

adapter = MyAdapter(...)
reports = detect(adapter.events())

# Or with all four detectors:
reports = detect_all(adapter.events())

# Or with custom sensitivity:
reports = detect(
    adapter.events(),
    token_tolerance=0.10,
    threshold=3,
    idempotent_agents={"background-worker"},
)
```

See [architecture.md](architecture.md) for the detector taxonomy and [usage.md](usage.md) for CLI-level scanning.

## Summary Checklist

When authoring an adapter:

- [ ] Implement `events() -> Iterator[Event]` with **strictly non-decreasing `ts`**.
- [ ] Populate `agent`, `tool`, `args_hash`, and `ts` (load-bearing fields).
- [ ] When `args_hash` is unavailable, set it to `None` and rely on token-variance detection.
- [ ] Populate `input_tokens` and `cost_usd` if available (critical for cost reporting).
- [ ] Populate `handoff_state` (bare state token) and `to_agent` (explicit target) only if your source captures agent state; leave both `None` for STATUS_CONTRACT_EXEMPT agents. Never pack the target into `handoff_state`.
- [ ] Set `progress=True` only when the event explicitly marks a state delta in your source.
- [ ] Return `raw_id` for forensic traceability.
- [ ] Test your adapter by feeding `adapter.events()` into `detect()` and verifying the output matches expectations.
