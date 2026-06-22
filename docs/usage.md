# looptrip Usage

looptrip is a deterministic detector of multi-agent coordination pathologies. This guide covers the command-line interface and library API.

## Installation

Install from source with editable mode:

```bash
pip install -e .
```

For development and testing:

```bash
pip install -e ".[dev]"
```

To use OpenTelemetry adapters (optional):

```bash
pip install -e ".[otel]"
```

## Command-line Interface

The `looptrip` command (installed via setuptools console script) uses stdlib argparse. Four entry points:

### `looptrip --version`

Print the installed version and exit.

```bash
$ looptrip --version
looptrip 0.1.0
```

Exit code: **0**

### `looptrip proof`

Run the bundled hermetic Phase-1 proof (no external dependencies, fully deterministic). The proof replays two recorded multi-agent runaways from the packaged fixture and reports the cost prevented by tripping at iteration 2.

```bash
$ looptrip proof
looptrip Phase-1 proof - trip at iteration 2 (hermetic fixture replay)
---------------------------------------------------------------------------
session     loop_agent          dispatches   trip_id  prevented       saved
---------------------------------------------------------------------------
2e6c0288    workflow-subagent           54       555         52     $320.16
da27b414    workflow-subagent           49      1080         47     $472.80
---------------------------------------------------------------------------

Model: dispatches #1-2 are the legal baseline; the duplicate-work detector trips at dispatch #2 (the 2nd occurrence of the signature, within 5% input-token variance of the preceding dispatch, no progress delta); every dispatch from #3 onward is the prevented waste.

GRAND TOTAL: $792.96 saved if tripped at iteration 2.
```

Exit code: **0**

This command is exercised in CI on every push ([`.github/workflows/test.yml`](../.github/workflows/test.yml)).

### `looptrip scan <source>`

Scan an event stream for pathologies. By default, runs the `duplicate_work` detector (Phase 1). Use `--all` or `--detectors LIST` to run multiple detectors (Phase 2+).

#### Source formats

- **`fixture:<session_id>`** — Load a session from the packaged hermetic fixture (no database required).
- **`cast-db:<session_id>`** — Query a live `cast.db` (requires the real CAST database).

#### Flags

- **`--all`** — Run all four detectors (duplicate_work, ping_pong, deadlock, non_termination). Output table gains a leading `kind` column. Empty message: "no pathologies detected in <source>". Mutually exclusive with `--detectors`.
- **`--detectors LIST`** — Run a comma-separated subset of detectors. Valid names: `duplicate_work`, `ping_pong`, `deadlock`, `non_termination`. Example: `--detectors duplicate_work,ping_pong`. Output gains a leading `kind` column. Unknown detector name → exit 2 with stderr error message. Mutually exclusive with `--all`.

Default (no flags): runs `duplicate_work` only; output shows agent / occurrences / prevented_runs / prevented_cost columns. Empty message: "no duplicate-work pathologies detected in <source>".

**Note on `deadlock`:** The deadlock detector requires `handoff_state` (blocked-state tokens) in events. On the packaged fixture, deadlock yields no results because the fixture lacks this metadata; it is not a bug.

#### Example: scan from fixture (default detector)

```bash
$ looptrip scan fixture:2e6c0288-b8db-46de-8ec4-164e3685a739
agent                     occurrences  prevented_runs  prevented_cost
---------------------------------------------------------------------
workflow-subagent                  54              52         $320.16
bash-specialist                    14              12           $5.41
commit                             20              11           $0.91
```

Exit code: **0** (clean scan, including no pathologies found)

#### Example: scan with all detectors

```bash
$ looptrip scan --all fixture:2e6c0288-b8db-46de-8ec4-164e3685a739
kind                  agent                     occurrences  prevented_runs  prevented_cost
-------------------------------------------------------------------------------------------
non_termination       test-writer                       113              93         $351.40
duplicate_work        workflow-subagent                  54              52         $320.16
ping_pong             bash-specialist                     3              30           $5.86
duplicate_work        bash-specialist                    14              12           $5.41
```

Exit code: **0**

#### Example: malformed source

```bash
$ looptrip scan bad_source
error: malformed source 'bad_source'; expected 'fixture:<id>' or 'cast-db:<id>'
```

Exit code: **2** (error written to stderr)

### `looptrip attribute <source>`

Attribute pathologies to decisive handoffs using counterfactual-replay analysis. Runs the selected detector(s) (default: `duplicate_work` only), then neutralizes each event in turn and re-runs the detector. An event is marked "decisive" if the pathology vanishes without it.

Verdicts summarize the attribution result:
- **`unique`** — Exactly one decisive handoff was found.
- **`multiple`** — Two or more independently decisive handoffs were found.
- **`overdetermined`** — No single decisive handoff exists; the pathology recurs even when any one handoff is neutralized. The loop is caused by repeated structure, not a single event.

#### Source formats and flags

Same as `looptrip scan` — `fixture:<session_id>` or `cast-db:<session_id>`, plus optional `--all` or `--detectors LIST` (mutually exclusive, same rules as scan).

#### Example: attribute with default detector

```bash
$ looptrip attribute fixture:2e6c0288-b8db-46de-8ec4-164e3685a739
kind                  agent                            verdict  decisive  tested
--------------------------------------------------------------------------------
duplicate_work        workflow-subagent         overdetermined         0     113
duplicate_work        bash-specialist           overdetermined         0     113
duplicate_work        commit                          multiple         2     113

No single decisive handoff: the duplicate_work pathology ('workflow-subagent', 'dispatch', None) survives neutralizing any one of 113 handoffs — it remains tripped (overdetermined; caused by the repeated structure, not a single handoff).
```

Exit code: **0** (clean attribution, including no pathologies to attribute)

**Honest framing:** The headline runaway (workflow-subagent) is **overdetermined** — no single handoff caused it. This is the correct, intended result and validates the "trip the loop at iteration 2, don't blame one handoff" observer thesis.

### Exit codes

| Code | Condition |
|------|-----------|
| 0    | Success (`--version`, `proof`, clean scan, clean attribution, or no pathologies found) |
| 2    | Malformed/unknown source, unknown detector name, or import failure |

## Library API

Use looptrip as a library for programmatic access to all four detectors and full configuration control.

### Basic usage: duplicate-work only (Phase 1)

```python
from looptrip.detector import detect
from looptrip.adapters.cast_db import CastDbAdapter

# Load events from the packaged fixture
adapter = CastDbAdapter.from_fixture("2e6c0288-b8db-46de-8ec4-164e3685a739")
events = sorted(adapter.events(), key=lambda e: (e.ts, e.raw_id))

# Detect duplicate-work pathologies (Phase-1 detector, the default)
reports = detect(events)
for report in reports:
    print(f"{report.agent}: {report.prevented_cost:.2f} prevented")
```

### All four detectors

```python
from looptrip.detector import detect_all, detect, ALL_DETECTORS

# Run all four detectors at once
reports = detect_all(events)

# Or use detect() with explicit selection
reports = detect(
    events,
    detectors=[
        "duplicate_work",   # Phase 1: same signature, no progress
        "ping_pong",        # Phase 2: A→B→A→B cycle
        "deadlock",         # Phase 2: mutually blocked agents
        "non_termination",  # Phase 2: unbounded state plateau
    ]
)
```

### The four detectors

#### 1. `duplicate_work` (Phase 1, default)

Same signature `(agent, tool, args_hash)` recurring with no progress delta. Trips at the second occurrence within 5% input-token variance of the immediately-preceding occurrence.

**Known Phase-1 blind spot:** A runaway whose first repeat exceeds token tolerance is missed. Phase-2 detectors close this gap.

#### 2. `ping_pong` / livelock (Phase 2)

Directed-cycle closures in the temporal agent sequence (e.g., A→B→A→B with no progress). Detects structural cycles token-independently.

#### 3. `deadlock` (Phase 2)

Mutually-blocked agents forming a directed wait-for cycle. Requires `handoff_state` (blocked-state tokens) in events; returns empty when `handoff_state` is absent everywhere.

#### 4. `non_termination` (Phase 2)

Unbounded event growth with no new distinct states (sliding-window unique-state plateau). Detects single-agent and multi-agent variants token-independently.

### Configuration and sensitivity tuning

Use `DetectionConfig` to customize detector behavior:

```python
from looptrip.detector import detect, DetectionConfig

config = DetectionConfig(
    token_tolerance=0.05,           # 5% input-token variance (Phase 1)
    threshold=2,                     # Trip at 2nd occurrence
    idempotent_agents=frozenset(["cron-job", "monitor"]),  # Never trip these
    terminal_states={"DONE", "DONE_WITH_CONCERNS"},        # Terminal / epoch-end states
    blocked_states={"blocked", "waiting"},                 # Deadlock detection
    min_cycle_len=2,                # Minimum agents in a cycle
    cycle_trip_count=2,             # Cycles to tolerate before trip
    window_size=20,                 # Sliding window for non_termination
    plateau_ratio=0.5,              # Unique-state threshold
)

reports = detect(events, config=config)
```

See `src/looptrip/detectors/types.py` for all 17 configuration fields. CAST-specific vocabulary (`terminal_states`, `blocked_states`) is passed by the caller — it is deliberately not baked into the framework-agnostic core.

### Advanced: per-detector access

```python
from looptrip.detector import detect_duplicate_work, detect_ping_pong, detect_deadlock, detect_non_termination
from looptrip.detectors.types import DetectionConfig

config = DetectionConfig()

# Run individual detectors
dup_reports = detect_duplicate_work(events, token_tolerance=0.05)
cycle_reports = detect_ping_pong(events, config=config)
deadlock_reports = detect_deadlock(events, config=config)
term_reports = detect_non_termination(events, config=config)
```

### Event data structure

Events are normalized `Event` objects with:

```python
from looptrip.normalize import Event

Event(
    agent: str,                          # Agent name
    tool: str,                           # Tool invoked (e.g., "dispatch")
    args_hash: Optional[str],            # SHA-1 of tool arguments
    ts: str,                             # ISO-8601 timestamp string
    input_tokens: Optional[int] = None,  # Input token count
    cost_usd: Optional[float] = None,    # Cost of this event
    progress: bool = False,              # Progress flag
    handoff_state: Optional[str] = None, # Handoff status (for deadlock detection)
    raw_id: Optional[int] = None,        # Original row ID
)
```

The `signature()` method returns `(agent, tool, args_hash)` for identity matching.

### Adapters

#### `CastDbAdapter` — cast.db (CAST agent framework)

```python
from looptrip.adapters.cast_db import CastDbAdapter

# From packaged fixture (no DB required)
adapter = CastDbAdapter.from_fixture(session_id)

# From live database (requires cast.db at `~/.claude/cast.db`)
adapter = CastDbAdapter(session_id)

events = adapter.events()
```

## CLI coverage

The CLI exposes all four detectors via `scan --all` and `scan --detectors LIST`, and counterfactual-replay attribution via `looptrip attribute`. Fine-grained `DetectionConfig` tuning and per-detector functions (e.g. `detect_duplicate_work()`, `detect_ping_pong()`) remain library-only.

## Architecture and internals

See [architecture.md](./architecture.md) for Phase-1 and Phase-2 design, the detection state machine, event normalization, and testing strategy.
