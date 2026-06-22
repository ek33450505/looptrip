# The Phase-1 Proof: Trip at Iteration 2

looptrip detects multi-agent coordination loops and stops them at **iteration 2** — before the waste accumulates. The proof is reproducible in one command, grounded in real recorded runaway sessions, and independently verified via two separate computational paths.

## The Headline

Two real CAST runaway sessions, replayed through the duplicate-work detector, show that tripping at the second workflow-subagent dispatch would have prevented **$792.96** in unnecessary compute:

```
session     loop_agent          dispatches   trip_id  prevented       saved
2e6c0288    workflow-subagent           54       555         52     $320.16
da27b414    workflow-subagent           49      1080         47     $472.80

GRAND TOTAL: $792.96 saved if tripped at iteration 2.
```

Both sessions are real, recorded in the Anthropic CAST observability database, and committed to this repository as a hermetic test fixture (`src/looptrip/_data/cast_db_runaways.json`). No live cast.db required; no external data sources.

## One-Command Reproduction

Install the package and run the proof:

```bash
pip install -e .
looptrip proof
```

Exit code: 0 on success. The proof is a built-in CLI command, CI-exercised on every merge, and self-asserting — it raises `AssertionError` if the saved amounts drift.

<details>
<summary><strong>Expected output</strong></summary>

```
looptrip Phase-1 proof - trip at iteration 2 (hermetic fixture replay)
---------------------------------------------------------------------------
session     loop_agent          dispatches   trip_id  prevented       saved
---------------------------------------------------------------------------
2e6c0288    workflow-subagent           54       555         52     $320.16
da27b414    workflow-subagent           49      1080         47     $472.80
---------------------------------------------------------------------------

Model: dispatches #1-2 are the legal baseline; the duplicate-work detector
trips at dispatch #2 (the 2nd occurrence of the signature, within 5%
input-token variance of the preceding dispatch, no progress delta); every
dispatch from #3 onward is the prevented waste.

GRAND TOTAL: $792.96 saved if tripped at iteration 2.
```

</details>

## The Fixture

The proof relies on a single committed artifact:

- **Path**: `src/looptrip/_data/cast_db_runaways.json`
- **SHA-256**: `fc966c3f9f00fa15d3ec86de2707c3ad5f7ce52f692aa34ffd5110fa3e70763f`
- **Size**: 64,396 bytes

This is a byte-faithful slice of the real CAST observability database, extracted on 2026-06-21 and packaged in the wheel. It contains:
- Session 2e6c0288: 113 events, 54 workflow-subagent dispatches
- Session da27b414: 56 events, 49 workflow-subagent dispatches

Both the SHA-256 and byte length are pinned in tests (`tests/test_fixture_integrity.py`). Any accidental trim, regeneration, or modification of the fixture will fail the test suite loudly before it can poison the headline numbers.

## The Model: Kill-the-Agent-at-Trip

**Baseline**: Dispatches #1–2 are the legal system baseline. A human might legitimately ask an agent the same question twice, or the agent might retry a blocked operation. These first two occurrences are not pathologies.

*Note: "kill-the-agent-at-trip" describes the counterfactual cost-accounting model for what an external orchestrator gate could have averted. looptrip itself is an observer — it reports pathologies, never blocks or kills agents.*

**Trip**: The duplicate-work detector identifies the 2nd occurrence of an identical `(agent, tool, args_hash)` signature where:
- Input tokens fall within 5% variance of the immediately preceding dispatch, AND
- No progress delta was recorded between them

For cast.db every dispatch carries `tool="dispatch"` and `args_hash=None` (the source has no per-dispatch arguments column), so the signature collapses to the **agent identity** and the duplicate is confirmed by the token-proximity check above — not by hashing and matching the actual arguments.

**Prevented waste**: If the detector trips at dispatch #2 and an external orchestrator gate acts on it, every later dispatch (#3..#N) by the same looping agent is averted. **The looped dispatches are not identical** — input-token counts and per-dispatch costs vary widely across each loop (`2e6c0288`: 22.4k–68.1k input tokens, \$2.85–\$21.69 per dispatch; `da27b414`: 19.8k–79.9k tokens, \$2.65–\$18.03 per dispatch). The agent is simply re-dispatched again and again with no terminating progress; only the *trip pair* (#1↔#2) is checked for ≤5% token proximity. The prevented amount is therefore the **real sum of the actual recorded `cost_usd`** values for dispatches #3..#N — not an extrapolation from a uniform per-dispatch cost.

For session 2e6c0288:
- Total workflow-subagent dispatches: 54
- Baseline (legal): dispatches #1–2
- Trip triggered: dispatch #2 (raw_id 555)
- Prevented: dispatches #3–54 (52 dispatches, $320.16)

For session da27b414:
- Total workflow-subagent dispatches: 49
- Baseline (legal): dispatches #1–2
- Trip triggered: dispatch #2 (raw_id 1080)
- Prevented: dispatches #3–49 (47 dispatches, $472.80)

The $792.96 is not an assumption that all 54 (or 49) dispatches cost the same. It is the real sum of the actual cost_usd values from the fixture data for the prevented dispatches — the dispatch-level billing data faithfully recorded in cast.db.

## Independent Re-Derivation: The Number is Computed, Not Asserted

A headline number is only trustworthy if it survives two independent derivations. The $792.96 is verified in two ways:

### 1. Data → Brute-Force Oracle (No Detector)

The test `tests/test_independent_rederivation.py` derives the prevented cost directly from the fixture JSON, without importing the detector:

```python
# Load the fixture
sessions = json.loads(fixture_json)
rows = [r for r in sessions[session_id] if "workflow" in r.get("agent", "")]
rows.sort(by (started_at, id))

# Sum rows[2:] (everything from the 3rd dispatch onward)
prevented_cost = sum(r['cost_usd'] for r in rows[2:])
```

This oracle computes:
- Session 2e6c0288: **$320.16** (sum of dispatches #3–54 from the fixture)
- Session da27b414: **$472.80** (sum of dispatches #3–49 from the fixture)
- **Total: $792.96**

### 2. Data → Detector → Report (The Real Pipeline)

The same test then runs the full looptrip pipeline:

```python
adapter = CastDbAdapter.from_fixture(session_id)
events = sorted(adapter.events(), key=lambda e: (e.ts, e.raw_id))
reports = detect(events)
top_report = max(reports, key=lambda r: r.prevented_cost)
```

The detector independently produces:
- Session 2e6c0288: `top_report.prevented_cost` = **$320.16**
- Session da27b414: `top_report.prevented_cost` = **$472.80**
- **Total: $792.96**

Both paths converge on the same dollar amounts. The number is computed from the data, not asserted. If either the fixture or the detector drifts, the test fails.

See `tests/test_independent_rederivation.py` and `tests/test_fixture_integrity.py` for the full test suite.

## Fixture Provenance: Hermetic & No Cast.db Required

The fixture is a slice of the real CAST cast.db, but:

- **No live database required**: The proof runs entirely from the packaged JSON file, installable via `pip install -e .`
- **Byte-pinned**: The fixture's exact content is locked by SHA-256; any accidental change is caught immediately
- **Cost data is faithful**: Per-dispatch cost_usd values are real billing figures from the source database, not computed or estimated
- **Events are ordered**: Rows are sorted by (started_at, id) before summing, ensuring chronological fidelity

The fixture is hermetic — it does not depend on the state of any live system, and it reproduces identically across all runs.

## Testing & Validation

- **Regression lock** (`tests/test_proof.py`): Asserts the per-session and grand-total savings match their hand-verified ground truth ($320.16, $472.80, $792.96) within $0.01. If the fixture or detector drifts, this goes red.

- **Independent re-derivation** (`tests/test_independent_rederivation.py`): Two-stage verification—brute-force oracle from raw JSON, then detector pipeline—both yielding the same numbers. Breaks the circularity between test and implementation.

- **Fixture integrity** (`tests/test_fixture_integrity.py`): Pins the fixture's SHA-256, byte length, row counts, session structure, and cost invariants. Any trim, regeneration, or modification fails here first.

- **CI coverage**: The full test suite (356 passing) runs on Python 3.10 and 3.11, including `looptrip proof`, on every merge.

## What the Proof Covers & Doesn't

**Covered**:
- Deterministic detection of duplicate-work loops at iteration 2
- Cost calculation from real multi-agent runaway sessions
- Reproducibility via a hermetic packaged fixture
- Mutation resistance (every meaningful one-line detector change is caught by the suite)

**Not covered (Phase 2+)**:
- The three additional detectors (ping_pong, deadlock, non_termination) — these are library-API only, not in the CLI headline
- Live cast.db scanning with custom calibration
- Hand-tuning detection sensitivity for specific domains

See [Testing](testing.md) for full test coverage and mutation analysis. See [Architecture](architecture.md) for detector design and sensitivity tuning.

## Further Reading

- [API reference](usage.md#library-api) — `detect()` and `DetectionConfig`
- [Testing & Mutation Analysis](testing.md) — Full suite, phase-2 detectors, blind spots
- [Architecture](architecture.md) — Detector design, event schema, adapters
