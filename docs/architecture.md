# Architecture

looptrip detects multi-agent coordination pathologies deterministically by observing normalized event streams. The detector core is stdlib-only, framework-agnostic, and zero-LLM: it reads ordered events and trips as soon as the structural conditions for a pathology are met.

## Table of Contents

- [Normalized Event Schema](#normalized-event-schema)
- [Adapter Layer](#adapter-layer)
- [The Iteration-2 Safety Predicate](#the-iteration-2-safety-predicate)
- [The Four Detectors](#the-four-detectors)
  - [Duplicate-Work (Phase 1)](#duplicate-work-phase-1)
  - [Ping-Pong / Livelock (Phase 2)](#ping-pong--livelock-phase-2)
  - [Deadlock (Phase 2)](#deadlock-phase-2)
  - [Non-Termination (Phase 2)](#non-termination-phase-2)
- [Phase-1 Blind Spot and Phase-2 Closure](#phase-1-blind-spot-and-phase-2-closure)
- [Detection Configuration](#detection-configuration)
- [Library API](#library-api)
- [Example: Detecting All Pathologies](#example-detecting-all-pathologies)

---

## Normalized Event Schema

Every source of multi-agent activity (cast.db agent runs today, OTel GenAI spans later) is funneled through one uniform `Event` shape so detectors never have to know where their input came from.

```python
@dataclass(frozen=True, slots=True)
class Event:
    agent: str              # who acted (e.g. "workflow-subagent")
    tool: str               # what kind of action (e.g. "dispatch")
    args_hash: Optional[str]  # stable hash of action arguments, or None
    ts: str                 # ISO-8601 timestamp
    handoff_state: Optional[str] = None  # enrichment; bare state token only
    to_agent: Optional[str] = None       # enrichment; explicit handoff target
    input_tokens: Optional[int] = None   # prompt token count
    cost_usd: Optional[float] = None     # action cost in USD
    progress: bool = False               # True if this event marks a progress delta
    raw_id: Any = None                   # provenance back-pointer (e.g. agent_runs.id)

    def signature(self) -> tuple:
        """Return (agent, tool, args_hash) identity triple."""
        return (self.agent, self.tool, self.args_hash)
```

**Three signature fields** form the load-bearing identity:

- **`agent`** — who acted (e.g., `"workflow-subagent"`).
- **`tool`** — action kind. Sources without a per-action tool column (cast.db) set this to `"dispatch"`.
- **`args_hash`** — deterministic SHA-1 hex digest of action arguments. **Can be `None`**: the cast.db adapter has no per-dispatch args column, so it sets `args_hash=None` for every event. Detection there leans on the `(agent, ts)` repeat signal plus input-token variance.

**Enrichment fields:**

- **`handoff_state`** — the **bare** `## Handoff` state token (e.g., `"DONE"`, `"blocked"`, `"waiting"`, `"in_progress"`, a progress marker). It NEVER carries a packed `"blocked on code-writer"` form; the handoff target lives in `to_agent`. Pure enrichment; `None` for status-contract-exempt agents. Never required for detection.
- **`to_agent`** — the explicit handoff target agent (e.g., `"code-writer"`), or `None` when absent. Maps from `gen_ai.agent.handoff.target.name`. Detectors read it directly with no delimiter scanning. **Not** part of `signature()` — `event.agent` is the source, `to_agent` is the destination.
- **`progress`** — `True` if this event marks a state-delta. A repeated signature with no progress delta is the duplicate-work signal.
- **`input_tokens`** / **`cost_usd`** — used for pairwise token-proximity checks and prevented-cost accounting.

---

## Adapter Layer

The `Adapter` interface is an abstract base that concrete sources (cast.db today, OTel later) implement:

```python
class Adapter(abc.ABC):
    @abc.abstractmethod
    def events(self) -> Iterator[Event]:
        """Yield normalized events, ordered such that ts is non-decreasing."""
```

**Why one event shape, three sources?**

- **Single schema:** detectors read only `Event`, never source-specific formats. No if-statements for "is this from cast.db or OTel?"
- **Decoupling:** adding a new source requires one adapter class; detectors stay unchanged.
- **Testability:** fixtures can inject synthetic `Event` streams without touching a real database.

The cast.db adapter (see [adapters.md](adapters.md)) produces `Event` instances with `tool="dispatch"` and `args_hash=None`, relying on timestamp ordering and input-token variance for duplicate detection. OTel adapters (Phase 2+) will populate `args_hash` from span attributes, enabling exact-match deduplication without token fallback.

---

## The Iteration-2 Safety Predicate

A repeated signature with **no progress delta between occurrences** is the core runaway signal. The predicate is:

> A signature's **second occurrence** (and subsequent recurrences) within `token_tolerance` input-token variance of the immediately-preceding occurrence, with no `progress=True` event and no terminal-state handoff between them.

**Fired at:** the second occurrence of the signature (default `threshold=2`), not at the invoice.

**Cost averted:** the sum of `cost_usd` over all same-signature events **strictly after the trip event**. The model is "kill the looping agent at the trip point"; every later dispatch is averted. *(This describes the counterfactual cost-accounting model that an external orchestrator gate could implement — looptrip itself only reports; it never blocks or kills agents.)*

The trip condition is **token-independent at the structural level** (it checks `(agent, tool, args_hash)` only), but the **pairwise duplicate-confirmation** within the duplicate-work detector uses token variance as a signal when exact args hashes are unavailable.

---

## The Four Detectors

All detectors inherit the base detection configuration, produce `PathologyReport` instances, and sort results by `prevented_cost` (costliest runaway first).

### Duplicate-Work (Phase 1)

**Pathology:** same signature fired repeatedly with no progress delta.

**Trip condition:**
- Signature `(agent, tool, args_hash)` recurs.
- **No progress delta** between the baseline occurrence and this one.
- **Pairwise token similarity:** `|input_tokens_current - input_tokens_prev| / max(input_tokens_prev, 1) <= token_tolerance` (default 5%).
- **Recurrence count reaches `threshold`** (default 2 = trip on the 2nd occurrence).

**Token dependency:** **Token-dependent** — uses pairwise input-token proximity when args hashes are unavailable (cast.db case).

**Handoff requirement:** None.

**Exemplar — cast.db runaway:**

```
Event(agent="workflow-subagent", tool="dispatch", args_hash=None, ts="2024-01-01T00:00:00Z", input_tokens=5000, cost_usd=0.01)
Event(agent="workflow-subagent", tool="dispatch", args_hash=None, ts="2024-01-01T00:00:01Z", input_tokens=5100, cost_usd=0.01)  ← TRIP (within 5% token variance)
Event(agent="workflow-subagent", tool="dispatch", args_hash=None, ts="2024-01-01T00:00:02Z", input_tokens=5050, cost_usd=0.01)  ← prevented
```

**Default behavior:** `detect(events)` runs **only** duplicate-work (backward compatible with Phase 1). Phase-2 detectors are opt-in.

---

### Ping-Pong / Livelock (Phase 2)

**Pathology:** two or more agents cycling indefinitely (`A→B→A→B→…`) with no net state advance.

**Trip condition:**
- A **directed cycle of `>= min_cycle_len` distinct agents** (default 2) closes in the temporal agent-visitation sequence.
- The cycle **has no progress or terminal event** inside it (epoch-scoped).
- The **same canonical directed cycle closes `>= cycle_trip_count` times** (default 2 = trip on the 2nd closure).
- The canonical cycle is the **minimum rotation** of the node sequence, so `A→B→C`, `B→C→A`, and `C→A→B` are the same key; `A→C→B` (reverse direction) is distinct.

**Token dependency:** **Token-independent** — requires only `event.agent`, `event.progress`, and `event.handoff_state`.

**Handoff requirement:** None. Works with `handoff_state=None` everywhere.

**Taxonomy note:** **ping-pong == livelock**. Both terms describe the same structural pathology — a pure liveness/fairness issue, distinct from deadlock.

**Exemplar:**

```
agent_sequence = A → B → A → B → (cycle closes 1st time)
                 A → B → A → B → (cycle closes 2nd time) ← TRIP
```

The detector tracks the current directed path since the last epoch reset. When an agent revisits the path, the suffix forms a cycle. If the same canonical cycle closes a second time without an intervening progress/terminal event, the detector trips.

---

### Deadlock (Phase 2)

**Pathology:** mutually-blocked agents forming a directed wait-for cycle, none able to progress.

**Trip condition:**
- Each agent in a **set of `>= min_cycle_len` agents** (default 2) has a **latest event marked as blocked** (its bare `handoff_state` token matches a `blocked_states` token, case-insensitive — no leading-word parsing).
- Each blocked agent is **waiting on another agent in the set** (the awaited agent is named by the explicit `to_agent` field), forming a **directed cycle in the wait-for graph** (e.g., A→B→C→A).
- The cycle is **mutable**: an agent whose latest event is *not* blocked is removed from the cycle, even if earlier events indicated blocking (a retry, timeout, or successful handoff dissolves the wait).

**Token dependency:** **Token-independent** — reads only the bare `handoff_state` token (blocked-state match) and the explicit `to_agent` field (wait-for target).

**Handoff requirement:** **REQUIRED.** Must have events whose `handoff_state` is a bare blocked-state token (e.g., `"blocked"`, `"waiting"`) **and** whose `to_agent` names the awaited agent (e.g., `to_agent="code-writer"`). When every event has `handoff_state=None`, the detector returns `[]` without error (the documented inherent limitation).

**Explicit-field model:**

There is **no delimiter scanning** in the detection path. The wait-for edge is read straight off two explicit fields — `handoff_state` (the bare blocked token) and `to_agent` (the target). First-class adapters (cast.db, OTel) set both directly:

```
handoff_state="blocked", to_agent="code-writer"  →  blocked, waiting on code-writer
handoff_state="waiting", to_agent="agent-x"      →  waiting on agent-x
handoff_state="blocked", to_agent=None           →  blocked; no named target
```

The legacy packed `"state on target"` corpus is split once at ingestion by `split_handoff_state()` (a simple single-`" on "` splitter) — it is the legacy seam, never invoked in the detector.

**Exemplar:**

```
agent="code-writer",   handoff_state="blocked", to_agent="code-reviewer"  → waiting on code-reviewer
agent="code-reviewer", handoff_state="blocked", to_agent="orchestrator"   → waiting on orchestrator
agent="orchestrator",  handoff_state="blocked", to_agent="code-writer"    → waiting on code-writer
                       (cycle: code-writer → code-reviewer → orchestrator → code-writer) ← TRIP
```

---

### Non-Termination (Phase 2)

**Pathology:** unbounded event growth with no terminal state; the distinct-state count plateaus while the event count keeps growing.

**Trip condition:**
- A **sliding window of `window_size` events** (default 20) contains **≤ `plateau_unique_states` distinct state identities** (derived from `floor(window_size * plateau_ratio)` when not explicitly overridden; default cap = 10 distinct states).
- **No progress or terminal event** inside the window.
- **Not all events in the window are exempt**.
- The **window is maximal**: a single maximal run of qualifying windows yields exactly one report, no matter how many events span it (a 10,000-event plateau yields one report).

**Token dependency:** **Token-independent** — reads only the configured state key (default `"signature"`), not token counts.

**Handoff requirement:** None. Works with `handoff_state=None` everywhere. State identity is configurable: `"signature"` (the default, works fully on cast.db), `"agent"`, or `"handoff_state"`.

**Exemplar — single agent looping:**

```
Event(agent="code-writer", tool="dispatch", args_hash="abc123", ts="2024-01-01T00:00:00Z")
Event(agent="code-writer", tool="dispatch", args_hash="abc123", ts="2024-01-01T00:00:01Z")
Event(agent="code-writer", tool="dispatch", args_hash="abc123", ts="2024-01-01T00:00:02Z")
... (20 events total, all signature="abc123")
Window size=20, distinct states=1, cap=10 ← QUALIFIES

Event(agent="code-writer", tool="dispatch", args_hash="abc123", ts="2024-01-01T00:00:20Z")
... (window slides, still 1 distinct state) ← TRIP (plateau window complete)
```

This closes the **Phase-1 blind spot** where a single agent loops with high input-token variance on each dispatch, evading the pairwise token-proximity check of duplicate-work.

---

## Phase-1 Blind Spot and Phase-2 Closure

**The Phase-1 blind spot:**

When a signature's first recurrence is **not within `token_tolerance`** of its predecessor, the duplicate-work detector misses it. Example:

```
Event(agent="A", signature=sig, input_tokens=1000, ts="...T00:00:00Z")  [baseline]
Event(agent="A", signature=sig, input_tokens=2500, ts="...T00:00:01Z")  [token diff = 150%; outside 5% tolerance] ← NOT TRIPPED
Event(agent="A", signature=sig, input_tokens=2550, ts="...T00:00:02Z")  [token diff = 2%; inside 5%] ← TRIP (but 2 occurrences already fired)
```

The detector checks only the **pairwise proximity** between the new event and the baseline; it doesn't group all same-signature events and check their variance collectively. A runaway whose token counts are high-variance will evade the sliding-window check.

**How Phase 2 closes it:**

1. **Ping-pong detector** — detects multi-agent cycles without token checks. Closes the **multi-agent flavor** of the blind spot (two agents bouncing).
2. **Non-termination detector** — detects single-agent plateaus without token checks. Closes the **single-agent flavor** of the blind spot (one agent looping with high-variance tokens).

Both are **structural detectors**: they key off the shape of the event graph, not the numeric properties of individual events.

---

## Detection Configuration

All detectors share a unified configuration object, `DetectionConfig`, with **17 fields**. Every field has a safe default so `DetectionConfig()` is immediately usable. Configuration is applied at call time, never baked into the detector.

```python
@dataclass(frozen=True, slots=True)
class DetectionConfig:
    # --- Phase-1 legacy fields (names and defaults preserved) ---
    token_tolerance: float = 0.05           # pairwise token variance tolerance
    threshold: int = 2                      # recurrence count that triggers duplicate-work
    idempotent_agents: frozenset = frozenset()  # agents exempt from all detectors

    # --- new-detector exemption fields ---
    idempotent_tools: frozenset = frozenset()   # tools exempt from Phase-2 detectors
    retry_allowed: frozenset = frozenset()      # agents explicitly allowed to retry
    allowlist_agents: frozenset = frozenset()   # additional agent exemptions
    allowlist_tools: frozenset = frozenset()    # additional tool exemptions

    # --- progress / terminal / blocked state vocabulary ---
    progress_markers: frozenset = frozenset()   # handoff_state values that count as progress
    terminal_states: frozenset = frozenset()    # handoff_state values that signal epoch end
    blocked_states: frozenset = frozenset({"blocked", "waiting"})  # blocked-state tokens

    # --- ping-pong sensitivity ---
    min_cycle_len: int = 2                  # minimum agents in a cycle
    cycle_trip_count: int = 2               # cycle closures that trigger trip
    use_handoff_edges: bool = False         # extract explicit hops from handoff_state

    # --- non-termination sensitivity ---
    window_size: int = 20                   # sliding window length
    plateau_ratio: float = 0.5              # unique states cap = floor(window_size * ratio)
    plateau_unique_states: Optional[int] = None  # override the derived cap
    state_key: str = "signature"            # state identity: "signature", "agent", or "handoff_state"
```

**Key design principle:**

CAST-specific vocabulary (e.g., `terminal_states={"DONE", "DONE_WITH_CONCERNS"}`) is **passed by the caller**, not baked into the framework-agnostic OSS core. The defaults are conservative and framework-neutral; CAST integrations (and other frameworks) supply their own semantics.

**Validation:**

Configuration is validated at construction time via `__post_init__`. Invalid combinations (e.g., `min_cycle_len < 2`, `threshold < 1`) raise `ValueError` immediately.

---

## Library API

### `detect(events, *, config=None, detectors=None, **knobs)`

Run selected detectors over an ordered event stream and return sorted reports.

**Arguments:**

- **`events`** — ordered iterable of `Event` instances (pre-sorted by `(ts, raw_id)`). Materialized once into a list so multi-detector runs iterate the same sequence.
- **`config`** — optional pre-built `DetectionConfig`; `None` uses defaults.
- **`detectors`** — iterable of `KIND_*` strings (`"duplicate_work"`, `"ping_pong"`, `"deadlock"`, `"non_termination"`). `None` (the default) selects **duplicate-work only** (backward compatible). Pass `ALL_DETECTORS` to run all four.
- **`**knobs`** — ad-hoc field overrides applied on top of `config` (e.g., `detect(events, threshold=3, window_size=10)`).

**Returns:**

All reports from every selected detector, sorted by `prevented_cost` DESCENDING (costliest runaway first).

**Default behavior:**

```python
from looptrip.detector import detect

reports = detect(events)  # runs duplicate-work only
```

### `detect_all(events, *, config=None, **knobs)`

Convenience wrapper that runs all four detectors.

```python
from looptrip.detector import detect_all

reports = detect_all(events)  # equivalent to detect(events, detectors=ALL_DETECTORS)
```

### Per-Detector Functions

Each detector can be called directly:

```python
from looptrip.detectors.ping_pong import detect_ping_pong
from looptrip.detectors.deadlock import detect_deadlock
from looptrip.detectors.non_termination import detect_non_termination

ping_pong_reports = detect_ping_pong(events, min_cycle_len=3)
deadlock_reports = detect_deadlock(events, blocked_states={"waiting"})
non_term_reports = detect_non_termination(events, window_size=10)
```

All accept optional `config` and `**knobs` for on-the-fly tuning.

---

## Example: Detecting All Pathologies

Here's a runnable end-to-end example using the library API:

```python
from looptrip.normalize import Event
from looptrip.detector import detect_all
from looptrip.detectors.types import DetectionConfig

# Synthetic fixture: a two-agent ping-pong (A ↔ B)
def mk_event(agent, raw_id):
    return Event(
        agent=agent,
        tool="dispatch",
        args_hash=None,
        ts=f"2024-01-01T00:00:{raw_id:02d}Z",
        input_tokens=1000,
        cost_usd=0.01,
        progress=False,
        raw_id=raw_id,
    )

events = [
    mk_event("A", 0),
    mk_event("B", 1),
    mk_event("A", 2),
    mk_event("B", 3),  # first cycle closure
    mk_event("A", 4),
    mk_event("B", 5),  # second cycle closure ← PING-PONG TRIP
]

# Run all four detectors
reports = detect_all(events)

for report in reports:
    print(f"Pathology: {report.kind}")
    print(f"  Agent: {report.agent}")
    print(f"  Signature: {report.signature}")
    print(f"  Trip event: {report.trip_event}")
    print(f"  Prevented cost: ${report.prevented_cost:.2f}")
    print(f"  Detail: {report.detail}")
    print()
```

**Run it:**

```bash
cd /Users/edkubiak/Projects/personal/looptrip
.venv/bin/python << 'EOF'
from looptrip.normalize import Event
from looptrip.detector import detect_all

def mk_event(agent, raw_id):
    return Event(
        agent=agent,
        tool="dispatch",
        args_hash=None,
        ts=f"2024-01-01T00:00:{raw_id:02d}Z",
        input_tokens=1000,
        cost_usd=0.01,
        progress=False,
        raw_id=raw_id,
    )

events = [
    mk_event("A", 0),
    mk_event("B", 1),
    mk_event("A", 2),
    mk_event("B", 3),
    mk_event("A", 4),
    mk_event("B", 5),
]

reports = detect_all(events)
for report in reports:
    print(f"{report.kind}: {report.detail}")
EOF
```

Expected output: A ping-pong report for the `("A", "B")` cycle.

---

## Related Documentation

- [adapters.md](adapters.md) — How to implement a concrete adapter for a new event source (cast.db, OTel, custom).
- [usage.md](usage.md) — Library and CLI usage, fixture data, and integration examples.
