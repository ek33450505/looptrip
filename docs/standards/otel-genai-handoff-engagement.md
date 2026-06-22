# OTel GenAI Handoff Engagement — looptrip Phase 7

**Status:** DRAFT — for Ed's review. Not for upstream submission.
**Date:** 2026-06-22
**Branch:** feature/phase7-otel-semconv

> **Volatility caveat:** The upstream `open-telemetry/semantic-conventions-genai` repository
> moves weekly. Every issue number, PR state, and attribute name in this document was
> verified against the GitHub API on 2026-06-22. Re-verify all claims against the live
> repository before posting any comment, issue, or PR. Stability status: everything
> referenced here is `stability:development` — nothing is Stable.

---

## Re-Anchored Thesis

Phase 7's original framing ("author the `gen_ai.handoff.*` convention / co-author
traceloop RFC #3460") was built on a stale premise. The official authoring venue is
`open-telemetry/semantic-conventions-genai` (not the traceloop vendor repo), and the
handoff identity (`gen_ai.agent.handoff.source.name` / `.target.name`) is already under
active review there (PR #98). looptrip does not own or re-author that work.

The re-anchored strategy has two parts:

1. **PRIMARY venue: `open-telemetry/semantic-conventions-genai`** — ADOPT the upstream
   handoff identity verbatim. Contribute the *pathology layer* the in-progress handoff
   work omits: a pending/blocking wait-for STATE (required for deadlock detection) and
   loop-termination / non-termination semantics.

2. **SECONDARY venue: traceloop RFC #3460** — Engage as an upstream-feeder/ally. The RFC
   has no cross-link to the official work and minimal maintainer traction; a brief
   alignment comment can surface that connection and flag a design conflict (see §6).

looptrip's role throughout: **observer, never a gate**. The project reports pathologies;
it does not block, kill agents, or make orchestration decisions. No claim of authorship
or ownership of the handoff convention.

---

## 1. Upstream Landscape (as of 2026-06-22)

### 1.1 Repository structure

The GenAI semantic conventions split into their own repository
`open-telemetry/semantic-conventions-genai` on 2026-05-05. The original gen_ai.agent.*
attributes in the monorepo (`open-telemetry/semantic-conventions`) are deprecated and
have moved. All active authoring is in the split repo.

### 1.2 Active threads relevant to looptrip

| Thread | Role for looptrip | Status (2026-06-22) |
|--------|-------------------|---------------------|
| **PR #98** — `gen-ai: model agent-to-agent handoff as execute_tool span` | PRIMARY — adds `gen_ai.agent.handoff.source.name` and `.target.name` as attributes on `gen_ai.execute_tool` spans. looptrip **adopts these verbatim**. | OPEN, unmerged. 2 APPROVED / 1 CHANGES_REQUESTED / 12 COMMENTED — contested, not settled. Mergeable. |
| **Closed PR #3614** — `gen_ai.agent.invocation.trigger` | Precedent — SIG closed this in favour of the execute_tool model. | CLOSED 2026-05-10. Signals SIG prefers no new top-level handoff span. |
| **Issue #243** — How to represent multiple agents on the same telemetry | GATE — this framing fork gates both PR #98 and issue #254. Highest-leverage single thread for looptrip to comment on. | OPEN |
| **Issue #254** — Agent-to-Agent (A2A) protocol telemetry | Cross-process A2A; proposes a dedicated `a2a.*` namespace reusing invoke_agent + conversation.id | OPEN |
| **Issue #171 + PR #238** — `gen_ai.agent.finish_reason` | ALIGNMENT — agent-loop termination semantics. looptrip proposes a `non_termination` / `livelock` value here. | OPEN, unmerged |
| **PR #267** — adds `time_budget` value to finish_reason | Adjacent to #238 | OPEN |
| **Issue #332** — `gen_ai.agent.turn` (conversation turn number) | ALIGNMENT — looptrip's iteration counting ("trips at iteration 2") maps here | OPEN 2026-06-22 |
| **Issue #159** — async/long-running agent lifecycle events (`gen_ai.agent.paused` / `.resumed` / `.checkpointed`) | ADJACENT — pending-wait state proposal is conceptually adjacent | OPEN |
| **Meta-issue #35** — "Semantic Conventions for Generative AI Agentic Systems" | UMBRELLA — label: `area:agent-orchestration` | OPEN |

### 1.3 What has NOT merged

Nothing in the handoff or agent-termination space has merged to `main/registry.yaml` as
of 2026-06-22. Every attribute and enum value cited in this document — upstream or
looptrip-proposed — is still under discussion.

---

## 2. looptrip's Proposed Contribution

The following table separates what is upstream-existing from what looptrip is proposing.

| Attribute / Value | Status | Notes |
|---|---|---|
| `gen_ai.agent.handoff.source.name` | **UPSTREAM-EXISTING** (PR #98, unmerged) | The agent performing the handoff. looptrip adopts verbatim. |
| `gen_ai.agent.handoff.target.name` | **UPSTREAM-EXISTING** (PR #98, unmerged) | The agent receiving the handoff. looptrip adopts verbatim. |
| `gen_ai.agent.handoff.state` | **LOOPTRIP-PROPOSED** — not yet upstream | Enum distinguishing transfer states. Described below. |
| `gen_ai.agent.finish_reason = "non_termination"` | **LOOPTRIP-PROPOSED** (aligned with issue #171, PR #238) | A livelock / non-termination value for the finish_reason enum. |
| `gen_ai.agent.turn` | **UPSTREAM-PROPOSED** (issue #332) | Conversation turn counter. looptrip's trip-at-iteration-2 logic maps here. |

### 2.1 `gen_ai.agent.handoff.state` — looptrip-proposed enum

PR #98 models a *completed* transfer — source hands off to target, transfer is done.
This is insufficient for pathology detection. Two additional states are required:

**PENDING values** — the source agent is *waiting* for the target; the transfer has NOT
yet occurred. These are the wait-for edges that the deadlock detector needs.

| Value | Semantics |
|---|---|
| `"blocked"` | Source agent is blocked, waiting for target to return a result. Hard stop. |
| `"waiting"` | Source agent is waiting (softer signal — scheduled, queued, or yielded). |

**ACTIVE values** — the source agent is actively handing off completed work; the
transfer is live and in motion.

| Value | Semantics |
|---|---|
| `"in_progress"` | A live transfer in progress. Agents are active (not blocked-waiting). Relevant for ping-pong / livelock detection. |

The PENDING vs ACTIVE distinction is load-bearing:

- A **deadlock** (directed wait-for cycle) requires PENDING state on all edges in the
  cycle. Agents are blocked-waiting. `detect_deadlock()` only fires on PENDING values.
- A **ping-pong / livelock** is agents *actively* bouncing work. Agents are executing,
  not blocked. `detect_ping_pong(use_handoff_edges=True)` fires on ACTIVE values.
  A livelock is NOT a deadlock.

Without this distinction, a completed-transfer model (PR #98 as-is) cannot express
either pathology.

### 2.2 `gen_ai.agent.finish_reason = "non_termination"` — looptrip-proposed value

Issue #171 and PR #238 propose a `gen_ai.agent.finish_reason` attribute for agent-loop
termination. PR #267 adds a `time_budget` value. looptrip proposes an additional value:

- `"non_termination"` (or `"livelock"`) — the agent loop exited because a non-termination
  pathology was detected (ping-pong cycle, runaway dispatch chain). Distinct from
  `"max_iterations"` (budget exhausted) — non-termination means the loop had no inherent
  stopping condition.

looptrip's deterministic, SDK-independent detector is the evidence the SIG asks for when
proposing a new enum value (see §3 — reference implementation).

### 2.3 `gen_ai.agent.turn` alignment — issue #332

Issue #332 proposes `gen_ai.agent.turn` to track conversation turn numbers. looptrip
already counts agent iterations ("trips at iteration 2"). The existing iteration counter
maps directly to this attribute and provides a concrete implementation reference.

### 2.4 Constructive review point on PR #98

PR #98 uses `gen_ai.agent.handoff.source.name` and `.target.name` (display names).
looptrip's contribution here is a review argument: **source and target should reference a
stable `gen_ai.agent.id`, not a display name**, because cycle detection requires stable
identity across time. An agent that changes its display name between invocations (or
two agents that share a display name) breaks the wait-for graph. Stable IDs are the
correct key for graph-based pathology detection.

---

## 3. Reference Implementation and Evidence

looptrip ships a deterministic, SDK-independent reference detector that proves the
proposed attributes light up real pathology detection. Two files constitute the
capturable-telemetry evidence:

**`tests/fixtures/otel_genai_handoff_spans.json`** — Synthetic OTel GenAI spans using
the real upstream attribute keys from PR #98. Three labelled scenarios:

- **(a) DEADLOCK** — `code-writer` blocked waiting on `code-reviewer`; `code-reviewer`
  blocked waiting on `code-writer`. Directed wait-for 2-cycle. Uses
  `gen_ai.agent.handoff.state = "blocked"` (PENDING — looptrip-proposed).
- **(b) PING-PONG** — `planner` and `code-writer` actively bounce completed work
  (`planner → code-writer → planner → …`). Uses `gen_ai.agent.handoff.state =
  "in_progress"` (ACTIVE — looptrip-proposed). 5 spans; trip at cycle-closure #2.
- **(c) CONTROL** — Clean linear handoff: `agent-alpha → agent-beta → agent-gamma`.
  No `gen_ai.agent.handoff.state` attribute (completed transfers). Must NOT trip either
  detector. Confirms the absence of state keeps the detection substrate inert.

**`tests/test_otel_handoff_reference.py`** — 16 tests across mapping, all three scenarios, and fixture integrity:

- Mapping correctness: `otel_span_to_event()` produces the correct `handoff_state`
  encoding for each state value (blocked, waiting, in_progress, absent).
- Scenario (a): `detect_deadlock()` fires once; `blocked_agents ==
  {"code-writer", "code-reviewer"}`; `detect_ping_pong()` returns `[]`.
- Scenario (b): `detect_ping_pong(use_handoff_edges=True)` fires once at `trip_index=2`;
  `detect_deadlock()` returns `[]` (livelock is NOT a deadlock).
- Scenario (c): both detectors return `[]` under all modes.

This test suite is the evidence that:
1. Populating `gen_ai.agent.handoff.state` on OTel spans lights up looptrip's pathology
   detection without any SDK-specific instrumentation.
2. The PENDING / ACTIVE distinction correctly separates deadlock from livelock.
3. The control scenario confirms no false positives on clean linear handoffs.

The fixture doubles as a standards evidence artifact — it uses the real upstream
attribute keys verbatim, so any OTel-instrumented system that emits these spans will
produce the same detection results.

---

## 4. Contribution Sequence

The `open-telemetry/semantic-conventions-genai` CONTRIBUTING process is issue-first.
A cold full-namespace PR with no prior issue is very unlikely to land. The correct
sequence:

1. **File a focused issue** using the `[semconv] propose ...` title pattern. For
   looptrip's contribution, the right entry point is either:
   - A comment on **PR #98** (see §5 — the stable-id argument and pending-wait gap), or
   - A focused issue proposing `gen_ai.agent.handoff.state` as a new attribute with the
     PENDING/ACTIVE enum, citing PR #98's gap and referencing the reference
     implementation.
2. **GenAI SIG discussion** — CNCF Slack `#otel-genai-instrumentation` + weekly SIG
   meeting. Surface the proposal before drafting a PR.
3. **Small model YAML PR** — `model/gen-ai/registry.yaml` changes only; run
   `make generate-all` to regen derived files; `make check-policies`.
4. **Add a reference scenario** — a scenario YAML demonstrating the pending-wait /
   non-termination pathology.
5. **Towncrier changelog fragment** — required by the CONTRIBUTING process.
6. **CLA + Code of Conduct** — required before any PR is reviewed.

Key maintainers: Liudmila Molkova (`@lmolkova`), Trask Stalnaker (`@trask`). Approvals
via CODEOWNERS group `open-telemetry/semconv-genai-approvers`.

---

## 5. Engage Both Venues

### Primary: `open-telemetry/semantic-conventions-genai`

Entry points in priority order:

1. **Issue #243** — the framing fork that gates PR #98 and #254. A comment here on the
   causation / multiple-agent observation model is the highest-leverage first step.
2. **PR #98** — the stable-id argument + pending-wait gap comment (see §2.4).
3. **Issue #171 / PR #238** — propose `non_termination` finish_reason value with the
   reference implementation as evidence.
4. After SIG discussion: file a focused issue for `gen_ai.agent.handoff.state`.

### Secondary: traceloop RFC #3460

RFC #3460 is a single-author open draft issue in a vendor repo. It proposes a flat
`gen_ai.handoff.*` namespace with a dedicated `gen_ai.agent.handoff` span and 9
attributes (source_agent, target_agent, timestamp, reason, intent, type, context_transferred,
arguments_json, response_summary). It has no OTel-maintainer engagement and no
cross-link to the official `-genai` handoff work.

Engagement goal: a brief alignment comment encouraging convergence with the official
execute_tool model (PR #98) and flagging one design conflict:

**Broadcast conflict:** RFC #3460 includes `type = "broadcast"` (one-to-many handoff).
looptrip's functional wait-for invariant requires `out-degree ≤ 1` (a blocked agent
waits on exactly one target — otherwise the wait-for graph cannot form a simple
cycle and deadlock detection breaks). A broadcast model would violate this invariant and
is incompatible with looptrip's pathology semantics. This is worth flagging to the RFC
author as a design consideration.

---

## 6. Reputation Boundary

**This section is non-negotiable.** These are the actions that require Ed's personal
participation and cannot be delegated:

- **CLA signature** — the Linux Foundation CLA for `open-telemetry/semantic-conventions-genai`
  must be signed by Ed under his own name.
- **SIG channel participation** — comments in CNCF Slack `#otel-genai-instrumentation`,
  attendance at the GenAI SIG weekly meeting, and any verbal representation of this
  proposal must come from Ed personally.
- **Issue and PR submission** — all GitHub issues, PR reviews, and PR submissions in the
  official OTel repository must be filed by Ed under his own GitHub account.
- **RFC #3460 comment** — the traceloop RFC comment is Ed's post under his own name.

Agents draft; Ed authors, signs, and submits. Agents do not post to any external forum.

---

## Sources

- `open-telemetry/semantic-conventions-genai` — verified via GitHub API 2026-06-22
  - PR #98 (handoff as execute_tool span)
  - Issue #243 (multiple agents on same telemetry)
  - Issue #254 (A2A protocol telemetry)
  - Issue #171, PR #238, PR #267 (finish_reason)
  - Issue #332 (agent turn)
  - Issue #159 (async lifecycle events)
  - Meta-issue #35 (umbrella)
  - Closed PR #3614 (handoff span — closed in favour of execute_tool model)
- `traceloop/openllmetry` RFC #3460 — verified via GitHub API 2026-06-22
- `tests/fixtures/otel_genai_handoff_spans.json` — looptrip reference fixture (in-repo)
- `tests/test_otel_handoff_reference.py` — looptrip reference test suite (in-repo)
- `docs/framing.md` — looptrip framing guardrails (in-repo)
