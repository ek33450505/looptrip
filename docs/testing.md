# Testing Methodology

looptrip's testing philosophy is **evidence first, claims second**. Every number in the proof must be reproducible; every test must catch a real detector bug. This document explains the four pillars of that methodology and why each exists.

## Hermetic Fixtures: Byte-Faithful Slices with SHA-256 Pins

The headline proof ($792.96 saved) depends entirely on a single artifact: `src/looptrip/_data/cast_db_runaways.json`. This is a byte-faithful slice of the real cast.db, packaged in the wheel and installable without database access.

**Why this matters:** Any fixture drift — regeneration, trimming, or re-baselining — silently travels with the expected numbers. The detector could break and the proof would stay green.

**The lock:** `tests/test_fixture_integrity.py` pins the fixture byte-for-byte:

- **SHA-256:** `fc966c3f9f00fa15d3ec86de2707c3ad5f7ce52f692aa34ffd5110fa3e70763f`
- **Byte length:** 64,396 bytes
- **Session membership:** Exactly two sessions (2e6c0288, da27b414)
- **Dispatch counts:** Session A has 54 workflow-subagent dispatches; Session B has 49
- **Cost invariants:** Per-session totals computed to the penny using `Decimal` at full precision

Any content drift — a single JSON edit, a field reordered — fails the sha256 check **before** any downstream test sees it. Regeneration, trim, or re-baseline: all fail loudly here.

## Independent Cross-Method Re-Derivation

The proof uses `detect()` to compute the $792.96 savings. But `detect()` is also the system under test. If the detector is wrong AND the expected constant is wrong, they can drift in lockstep and all tests stay green—the oracle and the system-under-test share one source of truth.

**The break:** `tests/test_independent_rederivation.py` computes the saved amount **two independent ways**:

1. **Brute-force oracle (no detector):** Load the fixture JSON, filter rows by agent="workflow-subagent", sort by (started_at, id), sum `cost_usd` over rows[2:] (dispatches #3 onward). This is pure data → dollars, no `looptrip.detector` imported.

2. **Detector pipeline:** Run the real `detect()` path (CastDbAdapter.from_fixture → events() → detect() → max by prevented_cost). Then assert the detector's `prevented_cost` matches the brute-force oracle to the penny, and that the trip event's `raw_id` is 555 or 1080.

Both stages must agree. The brute-force oracle is the source of truth; the detector must reproduce it exactly. This breaks the circularity: if the fixture drifts, the oracle changes and the detector test fails. If the detector breaks, the oracle is unaffected and the mismatch is caught.

**Verification:** All three tests pass:
- `test_brute_force_session_a_saved_is_320_16()` → $320.16 (oracle)
- `test_brute_force_session_b_saved_is_472_80()` → $472.80 (oracle)
- `test_detector_matches_brute_force_session_a/b()` → detector agrees to the penny; trip raw_ids are 555 / 1080

## Mutation-Sanity Testing

A deliberately-broken detector must turn the suite red. If a mutation survives all tests, the suite has a blind spot.

**The standard:** On 2026-06-21, every meaningful one-line mutation to the detector was exercised (token tolerance boundary, threshold comparison, timestamp ordering, cost accumulation). All caught. Only equivalent mutants—code changes that don't alter behavior—survived.

**Where this shows up:** `tests/test_detector_adversarial.py` is the hardening companion. It constructs Events directly (no fixture, no adapter) to exercise the state machine at the boundaries:

- Token-tolerance gate: `_args_similar()` threshold is inclusive at the ±5% band
- Baseline-advances similarity chain: the detector steps through the input in feed order
- Per-signature progress isolation: progress is checked per (agent, tool, args_hash)
- The "kill-the-agent-at-trip" cost window: prevented waste includes all dispatches from the trip onward
- Documented blind spots: e.g., a runaway whose first repeat exceeds token tolerance is missed

These tests lock the **current, shipping behavior** on purpose. Any future fix must change a test deliberately rather than drift silently. The audit flagged certain behaviors as "questionable but shipping" (e.g., the high-variance blind spot); those tests remain as written, pinning the contract until a deliberate remediation changes them.

## Multi-Version CI with Proof Gate

The CI pipeline ensures the headline command cannot drift from implementation.

**Matrix:** Python 3.10 and 3.11.

**.github/workflows/test.yml** executes:

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
looptrip proof
```

1. **pytest tests/ -v** runs all 356 tests, including:
   - Fixture integrity (sha256, byte length, row counts)
   - Independent re-derivation (oracle vs. detector)
   - Detector behavior at boundaries (adversarial tests)
   - All four detector implementations (duplicate_work, ping_pong, deadlock, non_termination)
   - CLI surfaces (version, proof, scan)

2. **looptrip proof** runs the hermetic proof. The output **must** match the headline:
   ```
   2e6c0288    workflow-subagent           54       555         52     $320.16
   da27b414    workflow-subagent           49      1080         47     $472.80
   GRAND TOTAL: $792.96 saved if tripped at iteration 2.
   ```

If the proof output changes (savings, trip IDs, dispatch counts), the CI fails. The CLI output is a machine-checkable contract.

## Adversarial Audit Before Any Number Is Trusted

Beyond the standard test suite, the detector is audited at decision points:

- **Inclusive boundaries:** Token-tolerance gate is `|a - b| <= 0.05 * min(a, b)` (inclusive, not exclusive)
- **Input ordering independence:** The detector iterates feed order, not sorted order. Unsorted-input tests verify this doesn't hide pathologies
- **Per-signature isolation:** Progress is checked within each (agent, tool, args_hash) signature; one signature's progress doesn't affect another
- **Cost attribution:** Prevented cost includes all dispatches strictly after the trip event for an agent, not just the trip itself

See `test_detector_adversarial.py` and `test_cli_adversarial.py` for the full audit suite.

## Test Coverage

**356 passing tests** organized by concern:

| Module | Tests | Purpose |
|--------|-------|---------|
| test_detector_config.py | 84 | DetectionConfig knob sensitivity |
| test_detector_phase2_integration.py | 49 | Cross-detector consistency, report ranking |
| test_detector_adversarial.py | 38 | Boundary conditions, inclusive gates, cost windows |
| test_detector_deadlock.py | 32 | Phase-2 deadlock detection (blocked wait-for cycles) |
| test_detector_ping_pong.py | 31 | Phase-2 livelock detection (directed cycles) |
| test_detector_non_termination.py | 30 | Phase-2 non-termination detection (state plateau) |
| test_detector.py | 17 | Core duplicate-work pathology detection |
| test_normalize.py | 17 | Event signature, hash stability, Adapter ABC |
| test_fixture_integrity.py | 16 | SHA-256 pinning, byte length, row counts, cost fidelity |
| test_cast_db_adapter_adversarial.py | 11 | Adapter edge cases (NULL costs, missing fields) |
| test_cast_db_adapter.py | 9 | Event normalization, adapter interface |
| test_cli_adversarial.py | 8 | CLI error handling |
| test_proof.py | 8 | Headline regression lock, CLI surfaces (version, proof, scan) |
| test_independent_rederivation.py | 6 | Brute-force oracle vs. detector agreement |

All tests pass on Python 3.10 and 3.11.

## Running the Tests

**Standard:** Install with dev extras and run pytest:

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

**Proof only** (hermetic, no cast.db required):

```bash
looptrip proof
```

This outputs the headline and self-asserts the savings per session. Exit code 0 = proof holds; non-zero = proof drift detected.

**Single test file** (e.g., verify fixture integrity):

```bash
python -m pytest tests/test_fixture_integrity.py -v
```

**With coverage:**

```bash
python -m pytest tests/ --cov=src/looptrip --cov-report=html
```

## Philosophy: Trust Through Reproducibility

Each pillar addresses a failure mode:

- **Fixture pinning** prevents silent drift in the source data
- **Independent re-derivation** prevents circularity between oracle and implementation
- **Mutation sanity** prevents blind spots; every meaningful bug is caught
- **Multi-version CI** prevents CLI/detector divergence
- **Adversarial audit** pins the shipping behavior at boundaries, so future changes are deliberate

The proof ($792.96) is not asserted; it is **computed independently two ways** (brute-force oracle and detector) from a byte-pinned fixture. Numbers you can stand behind.

---

See [proof.md](proof.md) for the Phase-1 proof narrative and model.
