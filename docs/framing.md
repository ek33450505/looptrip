# Framing Guardrails — looptrip

Locked guardrails for consistent, honest framing across all looptrip documentation and public posts. Consistency reviewers enforce these rules.

---

## Guardrail 1: The Baseline Anchors Attribution, Not a Moat

**The Claim:** Do NOT anchor looptrip to "~14% attribution" or treat 14% as a permanent ceiling.

**The Evidence:**
- 14% is the LLM-prompting baseline from Who-Watches-the-Watchers (ICML 2025): large language models, when asked to attribute agent decisions, recover ~14% of actual causal credit.
- Structured and deterministic methods reach **29–52%** (e.g., CHIEF, FALAT) in the same evaluation regime.
- The lever is *adding structure*, not fixing LLMs. looptrip's deterministic replay (Phase 3, future) is the limit case of that frontier—moving *toward* 100% via deterministic instrumentation, not apologizing for 14%.

**Why It Matters:**
Anchoring to 14% frames the problem as "LLMs are broken"—correct, but incomplete. The real story is "structured methods outperform prompting." Readers should understand that looptrip is part of a broader movement toward reproducible, verifiable agent observability, not a patch for a statistical shortcoming.

---

## Guardrail 2: Cost Claims Anchor on Verifiable Data; Label Speculation

**The Claim:** All cost figures must root to one of two sources: reproducible fixture data or explicitly-labeled unverified claims.

**Verifiable Numbers** (from committed fixture `src/looptrip/_data/cast_db_runaways.json`):
- Session 2e6c0288: $320.16 saved (54-dispatch runaway, trip at dispatch #2)
- Session da27b414: $472.80 saved (49-dispatch runaway, trip at dispatch #2)
- **GRAND TOTAL: $792.96** (computed via two independent methods; verified by `tests/test_independent_rederivation.py`)

**Unverified Claims:**
- Any figure like "$47K" reported in industry sources MUST be labeled: "$47K (widely reported, UNVERIFIED)." Do not assert that it corresponds to any specific incident—keep the claim isolated from cited evidence.
- Anthropic claude-code#4095 is a real, citable incident illustrating the multi-agent cost pathology class. Cite it separately as an example of the problem, not as the source or validation of speculation.

**Why It Matters:**
$792.96 is computed, not asserted—it comes from two independent paths (oracle Decimal brute-force and detector logic) that agree. Readers can reproduce it by running `looptrip proof` on the fixture. Speculation dressed as fact erodes credibility; transparency about what we know (fixture) vs. what we've heard (other orgs) maintains it.

---

## Guardrail 3: Acknowledge and Differentiate from Watchtower

**The Competitor:** Watchtower (MIT license, LangGraph-only, trips at 3+ repeats, no handoff-state contract, no attribution).

**The Differentiation:**
looptrip is **framework-agnostic**, **fast** (deterministic, zero-LLM), and **standards-engaged** (adopting the OTel GenAI `gen_ai.agent.handoff.*` convention and contributing the agent-loop pathology semantics). Watchtower is a solid single-framework tool; looptrip is the cross-framework observer.

**Why It Matters:**
Watchtower exists and is worth mentioning—it proves deterministic loop detection is feasible. Not acknowledging it signals ignorance or fear. Naming it and stating our advantages honestly signals confidence and technical rigor.

---

## Guardrail 4: The Moat Is Standards Engagement, Not the Detector Code

**The Claim:** looptrip's competitive advantage is **standards engagement in the OpenTelemetry GenAI agent-observability conventions** — adopting the upstream `gen_ai.agent.handoff.*` handoff identity and contributing the agent-loop *pathology* layer looptrip uniquely detects — **not** the ~200-line detector implementation, and **not** sole authorship of a handoff namespace.

**Why:**
- The detector algorithm is straightforward (signature matching, cycle detection, window analysis). It's implementable in a weekend by a competent engineer.
- Handoff *identity* is already being standardized upstream: `gen_ai.agent.handoff.source.name` / `gen_ai.agent.handoff.target.name`, modeled as attributes on a `gen_ai.execute_tool` span (`open-telemetry/semantic-conventions-genai` PR #98, in progress). looptrip **adopts** that identity rather than competing with it.
- looptrip's defensible contribution is the layer the in-progress handoff work omits: a **pending/blocking wait-for state** (what deadlock detection requires — distinct from a *completed* transfer) and **loop-termination / non-termination semantics** (aligned with the upstream `gen_ai.agent.finish_reason` and `gen_ai.agent.turn` proposals), backed by a deterministic, SDK-independent reference detector.
- The moat is strategic (a sustained relationship with the OTel GenAI SIG and ownership of the pathology layer), not technical (lines of code).

**Why It Matters:**
Claiming the moat is the algorithm invites someone to rewrite it in a weekend and claim parity. Claiming *sole authorship* of `gen_ai.handoff` — when the upstream convention already exists under a different name (`gen_ai.agent.handoff.*`) and is owned by the SIG — is both inaccurate and reputationally risky in front of the maintainers who must approve any contribution. The honest, stronger claim: looptrip co-shapes the *pathology layer* of an emerging standard and ships the reference implementation, so its value compounds as adoption grows.

**Volatility note:** the upstream conventions are `stability:development` (nothing Stable yet) and move weekly. Re-verify every public claim about upstream state against `open-telemetry/semantic-conventions-genai` before publication.

---

## Guardrail 5: Observer, Never a Gate; Deterministic, Zero-LLM

**The Claim:** looptrip reports what it observes. It **never blocks, never kills agents, never makes decisions**. Blocking is a different tool's job.

**Architecture:**
- **Deterministic:** looptrip's output is fully determined by its input (event stream, config). No randomness, no LLM calls.
- **Zero-LLM:** no neural inference. All detection is replay of recorded events and graph algorithms.
- **Non-prescriptive:** looptrip identifies pathologies and returns structured findings. The *orchestrator* or *gate* decides whether to block, retry, alert, or log.

**Why It Matters:**
Conflating "detection" with "action" is how tools fail—they over-block, under-alert, or introduce non-determinism into the control path. Keeping looptrip as a pure observer means operators can compose it with any decision-making layer (sync gates, async dashboards, training feedback loops) without risk of black-box logic in the critical path.

---

## Enforcement

These guardrails apply to:
- README and docs (all `.md` files under `docs/`)
- Blog posts, talks, or public announcements about looptrip
- Licensing and attribution statements (always Apache-2.0; credit Watchtower and OTel working groups)

Consistency reviewers will flag any public content that violates a guardrail *before publication*. Corrections are expected.
