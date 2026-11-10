---
id: SPEC-0519
title: "Retire nw's own admission: count-based gate slots, persona slots and SPEC-0500 gate-job tokens give way to Slurm"
status: draft
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
parent: SPEC-0501
tags: [agentic, workers, slurm, cleanup]
depends_on: [SPEC-0510, SPEC-0511, SPEC-0514]
---

## Context

Draft until SPEC-0501's Phase 1 exit criterion holds (a week of green gates under Slurm
admission, `TotalCPU/Elapsed` <= requested cpus, a measured MaxRSS table). nw's admission code
then duplicates Slurm's: `gate_slots.py` (293 lines) with coordinator-side worker pools
`w-<name>/slot-N.lock` and host slots (SPEC-0309, 0337, 0390, 0391), `gate_jobs.py`'s token /
line / share cap / campaigns-win `skip` (~200 of 584 lines, SPEC-0500), and persona slots in
`remote_personas.py` (`acquire_slot`, `busy`, `persona_slots`, ~120 lines). Research estimate:
~570 removable source lines plus `test_gate_slots.py` (487) and `test_workers_set_slots.py`.
During Phase 1 the registry slots were raised so Slurm was the real admission (two queues
otherwise hide waiting jobs from `squeue`).

## Goals / Non-goals

**Goals**
- Slurm workers are admitted by Slurm only; nw keeps a local-only limiter for the
  `executor: local` fallback path (the coordinator's own machine).
- Lower the gate `timeout_s` values padded for a 2x oversubscribed box (research: low-load p90
  is about half the all-load p90), from `nw gate stats`.

**Non-goals**
- Removing the local executor.

## Decision

When it ships, this spec supersedes SPEC-0337 (multi-pool slots), SPEC-0390 (`workers set
--slots`), SPEC-0391 (host slots from registry) and SPEC-0500 (gate-job FIFO/share), and amends
SPEC-0309's slot part. `gate_slots.acquire` keeps only the local pool; remote placement is
"submit to the Slurm submit host"; `gate_jobs.execute` submits directly (class `gatejob`,
SPEC-0511) with no tokens; persona dispatch drops `acquire_slot`.

## Design

- Delete: `gate_slots` worker pools and host-slot path (`_run_gates`' `host_slot` local branch
  stays for the local executor only), `gate_jobs._take_token/_line_*/_wait_token/
  requester_tokens/share_tokens/campaign_waiting/campaign_active`, `remote_personas.acquire_slot/
  busy/persona_slots/_slots_dir`, `nw workers set --slots/--persona-slots`, registry `slots`.
- `nw queue gate-slots` shows Slurm's view (SPEC-0512 list) for Slurm workers.
- Timeouts: one commit lowering nightwright's `pytest-affected` `timeout_s` to 1.5x its measured
  low-load p95, recorded in As shipped.
- Superseding: set `superseded_by: SPEC-0519` on the superseded specs in the same change.

## Alternatives considered

- **Keep both queues.** Jobs wait in nw where `squeue` cannot show or govern them.

## Acceptance criteria

- [ ] No Slurm-worker job waits on an nw slot or token; `squeue` shows every waiting job.
- [ ] The local fallback still limits local concurrency.
- [ ] Superseded specs marked; ledger regenerated.

## Test plan

- **Automated tests:** delete the removed modules' tests; new tests that a Slurm-worker gate,
  gate job and persona submit without touching slot dirs; local fallback limiter test.
- **Manual verification (real binary):** a burst of 15 nightwright gate jobs: all are in
  `squeue` immediately, none in `nw queue gate-slots` waits.
- **Regression guard:** affected tests of `gates.py`, `gate_jobs.py`, `remote_personas.py`.

## Rollout / migration

After the Phase 1 exit criterion. Supersedes SPEC-0337, SPEC-0390, SPEC-0391, SPEC-0500 at ship.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual steps performed and recorded.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Risks

- A Slurm outage now means no remote admission at all; the local fallback (SPEC-0505 reason
  `transport`) covers it.

## Open questions

- Exact line counts are re-measured at implementation.
