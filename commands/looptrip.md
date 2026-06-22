---
description: Scan a source for multi-agent coordination pathologies (read-only observer)
---

Run the `looptrip` detector over the given source and report any multi-agent coordination
pathologies it finds. looptrip is a **read-only observer** — it never modifies state, never
blocks, and never proposes that you kill or gate anything. It only reports what it sees.

Source to scan: $ARGUMENTS

A source must be one of looptrip's source specs (it is not a bare file path):

- `cast-db:<session_id>` — handoffs from the local CAST observability DB (`~/.claude/cast.db`)
- `otel:<path>[#scenario]` — an OpenTelemetry GenAI handoff span export (JSON or JSONL)
- `fixture:<session_id>` — a packaged hermetic fixture; the session id is a full UUID, e.g.
  `fixture:da27b414-f9f1-4c91-bd50-1a6096555066` (one of the two $792.96 proof-run sessions)

Steps:

1. Run `looptrip scan --all $ARGUMENTS` via Bash. (`--all` runs all four detectors:
   duplicate_work, ping_pong, deadlock, non_termination.) The `looptrip` CLI must be installed —
   `pip install looptrip` (published on PyPI). A Homebrew formula is also available once the
   public tap is published: `brew install ek33450505/looptrip/looptrip`.
2. If the source spec is missing or malformed, show the user the valid forms above and stop.
3. Summarize the findings table: for each pathology that tripped, give the kind, the signature,
   and where it first tripped. If nothing tripped, say so plainly.
4. Do not take any action on the findings — no fixes, no kills, no config changes. looptrip
   observes; deciding what to do about a loop is the human's call.
