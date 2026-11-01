---
id: SPEC-0333
title: "Remote persona runs with bundle write-back"
status: superseded
owner: user
created: 2026-10-31
updated: 2026-11-01
source_idea: IDEA-560
parent: SPEC-0310
tags: [agentic, remote, workers, personas, cost]
superseded_by: SPEC-0344
---

## Context

The third slice of [SPEC-0310](./0310-remote-workers-run-gates-persona-runs-and.md): run
persona runs on workers and bring their commits back.

## Decision

Superseded before it was built. Its one spec review (verdict `split`) asked for a split into
persona jobs with write-back, account-wide limits with affinity and cost, and the epic's
integration suite. The design was carried over unchanged in scope, with the review's
findings already applied in the text (explicit `RunRequest` serializers, an env allowlist
that matches `personas.py`, a clean worktree for writing personas, `saved_refs` on a failed
write-back, a defined marker list for rate limits, criteria for every CLI surface), into:

- [SPEC-0344](./0344-remote-persona-jobs-with-bundle-write-back.md) Remote persona jobs
  with bundle write-back (depends on SPEC-0342);
- [SPEC-0345](./0345-persona-cap-rate-limit-backoff-affinity-and-cost-by-worker.md) Persona
  cap, rate-limit backoff, campaign affinity and cost by worker (depends on SPEC-0344);
- [SPEC-0346](./0346-remote-workers-integration-suite.md) Remote workers integration suite
  for gate and persona workers (depends on SPEC-0345).

## Acceptance criteria

- [x] Replaced by SPEC-0344, SPEC-0345 and SPEC-0346 (see their criteria).

## Test plan

See SPEC-0344, SPEC-0345 and SPEC-0346.

## Definition of done

- [x] Superseded before it was built; the DoD lives in SPEC-0344 to SPEC-0346.

## Clock Log

Use `nw idea clock-in IDEA-560` / `nw idea clock-out IDEA-560` (now governing SPEC-0344 to SPEC-0346).
