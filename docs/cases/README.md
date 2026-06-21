# Case Ledger: Real Pathologies Detected by looptrip

This directory contains verified case studies of multi-agent coordination pathologies caught by looptrip while dogfooding the detector against real CAST sessions.

Each case documents a **real recorded runaway**, the pathology signature, the detection point, and the verifiable cost delta if the detector had tripped at iteration 2.

## Seed Cases (Phase 1)

| Session | Agent Loop | Dispatches | Trip Point | Prevented Cost | Case |
|---------|----------|-----------|------------|-----------------|------|
| `2e6c0288` | `workflow-subagent` | 54 | dispatch #2 (raw_id 555) | **$320.16** | [2e6c0288.md](2e6c0288.md) |
| `da27b414` | `workflow-subagent` | 49 | dispatch #2 (raw_id 1080) | **$472.80** | [da27b414.md](da27b414.md) |
| | | | **Total** | **$792.96** | |

All numbers are reproducible via the packaged hermetic fixture (no `cast.db` required):

```sh
looptrip proof
```

## Adding a New Case

When a pathology is detected in a new session:

1. **Capture the session ID** from the cast.db (full UUID format: `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`).
2. **Identify the pathology signature**: the `(agent, tool, args_hash)` tuple or the directed-cycle closure (for ping-pong) or the wait-for cycle (for deadlock).
3. **Find the trip point**: the event `raw_id` where the detector first fired (`trip_event.raw_id` in the report).
4. **Note the prevented cost**: the sum of `cost_usd` over all events after the trip point for that signature (emitted by `looptrip scan fixture:<session_id>`).
5. **Create a case file** at `docs/cases/<session_short>.md` (where `session_short` is the first 8 characters of the session UUID).
6. **Reproduce the pathology** via `looptrip scan fixture:<full_session_id>` and paste the output.
7. **Add an entry** to the table above, linking to the new case file.

## Case Write-Up Structure

Each case file contains:

- **Header with session metadata** (full UUID, date, agents involved, dispatch count).
- **The Pathology**: a plain-language explanation of what went wrong in the multi-agent run.
- **The Signature**: the repeated `(agent, tool, args_hash)` tuple or structural pattern.
- **Detection & Cost**: the raw_id where looptrip tripped, and the prevented-cost calculation.
- **Reproduction**: exact command to scan the session from the fixture.
- **Reference**: link back to [proof.md](../proof.md) for the hermetic fixture and detection methodology.

See [2e6c0288.md](2e6c0288.md) and [da27b414.md](da27b414.md) for examples.
