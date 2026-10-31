---
id: SPEC-0331
title: "Remote gates over SSH on one worker host"
status: superseded
owner: user
created: 2026-10-31
updated: 2026-10-31
source_idea: IDEA-558
parent: SPEC-0310
tags: [agentic, remote, workers, gates, security]
superseded_by: SPEC-0338
---

## Context

The thin first slice of [SPEC-0310](./0310-remote-workers-run-gates-persona-runs-and.md):
run gates on one remote host over SSH, with a local fallback.

## Decision

Superseded before acceptance. Its one spec review (2026-10-31, verdict `split`, size xl)
found 12 acceptance criteria across five subsystems and asked for three parts, ordered
worker, then registry and pools, then dispatch. The design was carried over, with the
review's findings applied (base ref in the bundle, seq claim before the slot, the honest
threat model, `remote: false` opt-out, a `started` frame so the run timeout excludes the
worker's slot wait, a locked registry, retained `have` refs, and criteria for every CLI
surface), into:

- [SPEC-0336](./0336-gate-worker-job-runner-and-forced-command.md) Gate worker job runner and
  forced-command dispatcher;
- [SPEC-0337](./0337-worker-registry-and-multi-pool-gate-slots.md) Worker registry and
  multi-pool gate slots;
- [SPEC-0338](./0338-remote-gate-dispatch-with-local-fallback.md) Remote gate dispatch with
  local fallback (the `superseded_by` target; it depends on the other two).

## Acceptance criteria

- [x] Replaced by SPEC-0336, SPEC-0337 and SPEC-0338 (see their criteria).

## Test plan

See SPEC-0336, SPEC-0337 and SPEC-0338.

## Definition of done

- [x] Superseded before acceptance; the DoD lives in SPEC-0336, SPEC-0337 and SPEC-0338.

## Clock Log

Use `nw idea clock-in IDEA-558` / `nw idea clock-out IDEA-558` (now governing SPEC-0338).
