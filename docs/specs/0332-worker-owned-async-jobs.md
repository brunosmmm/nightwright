---
id: SPEC-0332
title: "Worker-owned async jobs"
status: superseded
owner: user
created: 2026-10-31
updated: 2026-11-01
source_idea: IDEA-559
parent: SPEC-0310
tags: [agentic, remote, workers, lease, reliability]
superseded_by: SPEC-0340
---

## Context

The second slice of [SPEC-0310](./0310-remote-workers-run-gates-persona-runs-and.md):
make remote jobs outlive the SSH connection, so the coordinator can re-attach, adopt jobs
after a restart and retry on another worker.

## Decision

Superseded before it was built. Its one spec review (verdict `split`, size xl) found nine
compound acceptance criteria across independent subsystems and asked for a split. The
design was carried over unchanged in scope, with the review's findings already applied in
the text (per-tick lease renewal while attached, rebuilding every `GateResult` on adoption,
`gate-job` keeping its stdin-EOF cancel, a slot re-acquired on adoption, the attempt in the
`job_id`, bootstrap registering after the key is installed, an escalation criterion, and
criteria for every CLI surface), into four parts, split along the worker and the
coordinator and ordered by dependency:

- [SPEC-0340](./0340-worker-async-job-protocol.md) Worker-side async job protocol: submit,
  attach, poll, cancel (the worker commands, lease, `lost`, `gate-job` compatibility);
- [SPEC-0341](./0341-coordinator-re-attach-job-records-and-adoption.md) Coordinator
  re-attach, job records and adoption after a restart (depends on SPEC-0340);
- [SPEC-0342](./0342-lost-job-retry-across-workers-and-escalation.md) Lost-job retry across
  workers and escalation (depends on SPEC-0341);
- [SPEC-0343](./0343-worker-bootstrap-and-drain.md) Worker bootstrap and drain (depends on
  SPEC-0341; parallel with SPEC-0342).

## Acceptance criteria

- [x] Replaced by SPEC-0340, SPEC-0341, SPEC-0342 and SPEC-0343 (see their criteria).

## Test plan

See SPEC-0340, SPEC-0341, SPEC-0342 and SPEC-0343.

## Definition of done

- [x] Superseded before it was built; the DoD lives in SPEC-0340 to SPEC-0343.

## Clock Log

Use `nw idea clock-in IDEA-559` / `nw idea clock-out IDEA-559` (now governing SPEC-0340 to SPEC-0343).
