# looptrip

**Deterministic, framework-agnostic detection of multi-agent coordination pathologies — caught at iteration 2, not on the invoice.**

looptrip watches a multi-agent run as a stream of normalized events and flags the coordination pathologies that make agent systems burn money and spin: duplicate-work loops, ping-pong / livelock, deadlock, and non-termination. It is **detection-first** — it works over data you already have (OpenTelemetry GenAI spans, or a CAST `cast.db`) — and **deterministic / zero-LLM**: the same event stream always yields the same verdict. looptrip is an **observer, never a gate**; it reports, it never blocks.

> **Phase 2 (this release)** ships full pathology coverage (duplicate-work, ping-pong / livelock, deadlock, non-termination), configurable sensitivity controls, and the `cast.db` adapter with reproducible proof on real data. Counterfactual handoff attribution and the live OpenTelemetry `SpanProcessor` land in later phases — see [Roadmap](#roadmap).

## The headline

On two **real** recorded multi-agent runaway sessions, a single `workflow-subagent` dispatch recurred 54 and 49 times with no progress between repeats. Tripping at the *second* dispatch — the first repeat — instead of letting the loop run to exhaustion would have saved:

| session | runaway loop | dispatches | trip point | saved |
|---|---|---|---|---|
| `2e6c0288` | `workflow-subagent` | 54 | dispatch #2 | **$320.16** |
| `da27b414` | `workflow-subagent` | 49 | dispatch #2 | **$472.80** |
| | | | **total** | **$792.96** |

Reproduce it yourself — no database required, the data is a committed fixture:

```sh
pip install -e .
looptrip proof
```

## Why "iteration 2"

Native runaway guards are blunt total-step counters that trip at N=10–25 — *after* the waste has compounded. looptrip's trip is a **safety predicate keyed on the pathology signature**: *no signature `(agent, tool, args_hash)` may recur without an intervening progress delta.* The instant a signature is seen a second time (within a configurable input-token tolerance, with no progress marker between), it fires — before the third wasted turn and the O(N²) context-cost compounding. "2" is the default threshold, not a magic number. The approach (signature-keyed detection with configurable thresholds) is what matters — the detector itself is not the moat; the durable asset is standards authorship of the open `gen_ai.handoff` semantic convention.

The worst real runaways are the hardest to catch: a `workflow-subagent` loop emits no structured handoff contract at all. So looptrip detects from the `(agent, ts)` repeat signal plus input-token variance alone; any handoff metadata only *enriches* the signal — it is never required.

## Usage

```sh
looptrip proof                       # reproduce the $792.96 headline on the committed fixture
looptrip scan fixture:<session_id>   # scan a session from the packaged fixture
looptrip scan cast-db:<session_id>   # scan a live cast.db session (CAST hosts only)
looptrip --version
```

## How it works

1. **One normalized event** — `(agent, tool, args_hash, ts, handoff_state)` plus optional cost/token metadata. An **adapter** maps each source's fields onto this schema, so detection logic never touches source-specific span-attribute renames.
2. **Detection-first** — Phase 1 ships a `cast.db` adapter; a live OTel `SpanProcessor` and a generic JSONL adapter follow. Because `agent_runs` carries no per-dispatch args, the adapter sets `args_hash=None` and detection leans on the token-variance signal.
3. **Stdlib state machine** — the detector groups events by signature and trips on the 2nd same-signature occurrence with no progress delta. The core is **stdlib-only**; OpenTelemetry is an optional `[otel]` extra, never imported by the detector.
4. **False-positive control is first-class** — a configurable input-token tolerance, a progress-delta marker, and an `idempotent_agents` allowlist keep legitimately-repeatable work (commits, reviews) from tripping. looptrip is meant to be run detect-then-print and dogfooded before any signal is trusted.

## Honest framing

This project tries hard not to oversell:

- **Attribution numbers.** Published LLM-prompting baselines for "which handoff broke the run" sit around ~14% — but that is the *prompting* baseline; structured / deterministic methods reach 29–52%. Adding structure is the lever, and looptrip's deterministic replay (Phase 3) is the limit case of that frontier — not a fix for a permanent ceiling. We don't anchor to "14%."
- **Cost numbers.** The $792.96 here is verifiable from the committed fixture. Larger figures circulate — e.g. a widely-reported "$47K" agent-loop bill — but those are **unverified**, and we label them as such.
- **Prior art.** The market gap is real, but the durable asset is the *standard*, not the ~200-line detector. A direct competitor exists — **Watchtower** (MIT, LangGraph-only, trips at 3+ repeats, no handoff contract, no attribution). looptrip differentiates on framework-agnosticism, speed, and authorship of an open `gen_ai.handoff` semantic convention.

## Roadmap

- **Phase 1** — `cast.db` adapter + duplicate-work / iteration-2 detector + reproducible proof.
- **Phase 2** — full pathology coverage (ping-pong / livelock, deadlock, non-termination) + sensitivity controls.
- **Phase 3** — counterfactual replay attribution ("which handoff was decisive").
- **Phase 4** — live OpenTelemetry `SpanProcessor` (`on_start` detection / `on_end` attribution, AlwaysRecord sampler).
- **Phase 5** — packaging (Claude Code plugin, Homebrew).
- **Phase 6** — documentation (reference deep-dives, examples, architecture notes).
- **Phase 7** — OpenTelemetry GenAI `gen_ai.handoff.*` semantic-convention contribution, with looptrip as the reference implementation.
- **Phase 8** — launch.

## Documentation

- **[Proof](docs/proof.md)** — Reproduce the $792.96 headline. Evidence that the fixture is real and reproducible.
- **[Usage](docs/usage.md)** — CLI and library API reference, adapters, and configuration.
- **[Architecture](docs/architecture.md)** — Detector design, event normalization, signature matching, and phase-by-phase roadmap.
- **[Adapters](docs/adapters.md)** — Implementing a custom adapter for your event source (OTel spans, custom JSON, etc.).
- **[Testing](docs/testing.md)** — Test structure, mutation sanity, fixture integrity, and independent re-derivation.
- **[Framing](docs/framing.md)** — Attribution, cost baselines, related work (Watchtower), and the role of standards.
- **[Case Studies](docs/cases/)** — Real runaways: `workflow-subagent` loops, deadlock scenarios, and non-termination traces.
- **[Contributing](CONTRIBUTING.md)** — How to contribute, issue triage, and development setup.

## License

Apache-2.0. See [LICENSE](LICENSE).

<sub>Developed under the internal codename "cyclops."</sub>
