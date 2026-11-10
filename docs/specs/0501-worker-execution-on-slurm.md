---
id: SPEC-0501
title: "Worker execution on Slurm: Slurm owns admission, placement, priorities and queues for gate and persona jobs"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
kind: epic
tags: [agentic, workers, gates, slurm, scheduling, infra]
depends_on: [SPEC-0338, SPEC-0340, SPEC-0341, SPEC-0381]
source_idea: IDEA-1111
---

## Context

nightwright runs campaign gates (SPEC-0338), standalone gate jobs (SPEC-0381, SPEC-0500), the
base-branch sentinel and cadence jobs (SPEC-0379, SPEC-0489..0493) and remote persona runs
(SPEC-0344) on worker boxes over a forced-command SSH protocol (SPEC-0340/0341). Admission is
nw's own: count-based gate slots (`gate_slots.py`, SPEC-0309/0337/0390/0391), gate-job FIFO
tokens and share caps (`gate_jobs.py`, SPEC-0500), persona slots (`remote_personas.py`),
worker cooldown and cross-worker retry (`remote_gates.py`, `workers.py`, SPEC-0338/0342/0374),
drain (SPEC-0343/0386) and a lease/heartbeat/lost-job kill path (`worker_job.py`, SPEC-0340).
None of it knows how many cores or how much memory a job uses.

A new box (112 threads, 96 GB RAM) is coming; the current box `box` (nw@morais-Desktop) is
12 threads / 31 GB. The human decided to adopt Slurm:

> "The reason I want slurm is because I don't want us to rebuild all of the functionality that
> it will have like the resource management and also queues etc. I want to be able to govern
> jobs and reprioritize, etc."

**Evidence (measured 2026-11-09 from 4,330 gate executions on `box`, 2026-11-01..09; research
report for IDEA-1111, copied here because the report lives in a non-durable scratchpad).**

- 1,240 standalone gate jobs (almost all jerome, 2,889 per-gate rows) and 1,439 remote campaign
  gates. Persona runs on the box: 2 ever. Box busy time 80.5 h.
- While busy, the box ran >= 6 concurrent executions 19 % of the time. Thread demand (pytest
  `-n N` -> N+1, `cargo.sh` -> 6, else 1) exceeded the 12 threads 49 % of busy time and
  doubled them 26 % of busy time.
- Median wall time by concurrency (other executions running alongside):

  | Gate class (n) | conc <= 2 | conc 6-8 | conc >= 9 | slowdown | corr(dur, conc) |
  |---|---|---|---|---|---|
  | nw `pytest-affected`, s per 1k tests (290) | 160 | 225 | 303 | 1.9x | 0.44 |
  | nw `pytest` full (42) | 397 s | 571 s | 748 s | 1.9x | 0.55 |
  | jerome `cargo build --workspace` (220) | 135 s | 310 s | 393 s | 2.9x | 0.36 |
  | jerome `cargo clippy` (223) | 22 s | 45 s | 58 s | 2.7x | 0.03 |
  | jerome `cargo fmt` (319) | 1.8 s | 3.4 s | 3.7 s | 2.0x | 0.51 |
  | `spec-lint` (501) | 3.5 s | 5.5 s | 6.6 s | 1.9x | 0.56 |

- 67 timed-out executions; nightwright's 16 (12 `pytest-affected`, 4 `pytest`): 11 of them ran
  at >= 6 concurrent jobs and thread demand >= 23 (about 2x the cores).
- Confounders of the same order: 205 of 715 `pytest-affected` runs selected > 5,000 tests (a
  full-suite-sized fallback, IDEA-1082 territory; median 933 s vs 572 s overall), and jerome's
  `cargo.sh` waits on an exclusive `flock` for one of 3 shared cargo target dirs inside the job.
- **Memory is not recorded anywhere.** On the new box (96 GB / 112 threads = 0.86 GB per thread)
  memory, not cores, is the binding resource.
- Estimated defaults (re-derived from measured `MaxRSS` once accounting runs, SPEC-0514):

  | Gate type | low-load wall median / p90 | est. CPU | default request |
  |---|---|---|---|
  | nw `pytest-affected` (`-n 2`) | 506 / 953 s | ~2.2 cores | `cpus 3, mem 3G` |
  | nw `pytest` full | ~400 / 530 s | ~4 cores | `cpus 5, mem 5G` |
  | `spec-lint`, `docs-tests`, `fmt` | 3-10 s | 1 core | `cpus 1, mem 1G` |
  | jerome cargo build/clippy/test (`-j 6`) | 134 / 212 s (build) | 5-6 cores | `cpus 6, mem 8G`, license `jerome_target` |
  | persona run (LLM-bound) | minutes | < 0.3 core | `cpus 1, mem 2G` |

**Verified facts about the current box** (coordinator, 2026-11-09): Arch Linux; packages
`slurm-llnl 26.05.5.1-1` and `munge 0.5.18`; config dir `/etc/slurm-llnl` (only `*.example`
present before setup); units `slurmctld`, `slurmd`, `slurmdbd`, `munge` installed but inactive
and disabled; cgroup v2 with the `cpuset cpu io memory` controllers; `slurmd -C`: `CPUs=12
CoresPerSocket=6 ThreadsPerCore=2 RealMemory=31955` (plus a GT 1030 GPU, ignored); users `nw`
(uid 1001, `Linger=no`) and `slurm` exist; root steps need the human's password. The box is the
human's desktop, so its config reserves `CoreSpecCount=1` and `MemSpecLimit=6000`. The
coordinator wrote the root setup script (`setup-slurm-box.sh`, scratchpad of the 2026-11-09
session; its content is summarized in SPEC-0510): `auth/munge`, `select/cons_tres`
`CR_Core_Memory`, `proctrack/cgroup`, `task/cgroup,task/affinity`, `jobacct_gather/cgroup`,
`accounting_storage/none` for now, partition `nw` with `MaxTime=04:00:00`.

Related bug ideas folded into Phase 0: IDEA-1080 (no reason logged for a local fallback),
IDEA-1082 (a ledger regenerated on the worker re-runs the whole gate locally), IDEA-1083 (a
timeout re-gate that never starts), IDEA-1112 (no worker capacity/load view).

## Goals / Non-goals

**Goals**
- Slurm does admission (cores + memory), placement across both boxes, priorities and queueing
  for every nw worker job; nw stops building those.
- The human governs queued nw jobs (see, hold, release, move up, change class, requeue, cancel)
  from `nw queue jobs` by nw id, and with raw `squeue/scontrol` on the box.
- Every gate job declares cpu/memory; every gate record carries the measured CPU time and peak
  memory, so requests come from evidence.
- Jobs survive a sleeping laptop coordinator.
- nw upgrades stay sudo-free (`nw workers update`); Slurm itself is root-managed by the human.
- The design works whether the new box is nw-only or shared with other heavy compute.

**Non-goals**
- Moving the campaign state machine, conductor, triage, tree staging or results into Slurm:
  Slurm orders jobs that exist; nw decides which jobs exist.
- slurmrestd / JWT: the CLI with `--parsable` / `--json` suffices for two nodes.
- Containers, a backup controller, GPUs/GRES.

## Decision

Adopt Slurm as the worker executor, in four phases:

0. **Prep (no Slurm needed).** An executor seam on the worker (`local` = today's detached
   process), per-gate resource requests in project config and measured usage in gate records,
   gate commands sized from their allocation, and the four bug ideas that inflate load or hide
   why work moved (IDEA-1080/1082/1083/1112). Behaviour unchanged.
1. **Single-node Slurm on the current box** (installed by the human; verified first). A `slurm`
   executor submits each worker job with `sbatch` (the sync `gate-job` submits and follows),
   maps Slurm state into nw's job states, cancels with `scancel`, drops the lease self-cancel
   for Slurm jobs, maps job classes onto a QOS ladder, exposes governance as
   `nw queue jobs …`, records Slurm accounting into gate records and turns OOM/TIMEOUT/PREEMPTED into infra outcomes.
2. **New box arrives**: it becomes the controller (`slurmctld` + `slurmdbd`/MariaDB) and the
   big compute node; `box` joins as a configless `slurmd`; a shared job spool; the shared-box
   policy if the human shares the box.
3. **Retire** nw's own admission, placement, cooldown/retry, drain internals, lost-job kill
   paths and pull workers, superseding their specs at that time.

**Status of the children.** Phase 0 and Phase 1 children are `accepted`: the human asked to turn
the research into specs and proceed, and both phases run on hardware that exists today (the
current box has Slurm installed). Phase 2 and Phase 3 children are `draft`: Phase 2 depends on
the new box's arrival and on whether it is shared (undecided), Phase 3 on Phase 1's results (a
week of green gates under Slurm admission and a measured MaxRSS table). They are promoted when
those inputs exist.

**No existing spec is superseded now.** Each child names the spec(s) it supersedes when it
ships; the `supersedes`/`superseded_by` edits happen in that child's implementation.

## Architecture / cross-cutting design

**Topology (target, Phase 2).**

| Role | Where |
|---|---|
| `slurmctld`, `slurmdbd` + MariaDB | new box (`big`); always on. Never the laptop. |
| `slurmd` | `big` and `box` (`box` configless: `slurmd --conf-server big`) |
| auth | the current box uses `auth/munge`; Phase 2 decides munge vs `auth/slurm` once for both nodes (SPEC-0516) |
| submit host | `big` only, as the `nw` user, reached by the existing forced-command SSH |
| spool | job dirs on a shared `/srv/nw`; checkouts on node-local scratch (SPEC-0517) |

Until Phase 2 the current box is controller, compute node and submit host at once.

**Invariants every child respects.**

1. The coordinator protocol is unchanged (`nw worker have|submit|attach|poll|cancel|jobs|info`
   over forced-command SSH, SPEC-0340/0341). Only what happens on the worker behind `submit`,
   `gate-job`, `cancel` and `jobs` changes.
2. The nw job id stays primary. Slurm `--job-name` = nw job id; `--comment` = compact JSON
   `{"campaign","seq","attempt","kind","requester"}`; `--wckey` = project (when accounting is on);
   `job.json` records `slurm_job_id`.
3. The executor is a per-worker-host setting: `agentic.workers.executor: local|slurm` (default
   `local`). `local` keeps today's behaviour byte for byte.
4. Resource requests come from project config (`gates[].resources`), with per-kind defaults
   from the table above; nothing auto-tunes silently; the human edits the yaml.
5. Slurm commands run as the `nw` user, never with sudo; their environment carries
   `SLURM_CONF` from `agentic.slurm.conf` when set (the current box keeps its config in
   `/etc/slurm-llnl`).
6. nw's governance wrappers act only on `--user nw` jobs. The human governs anything with raw
   Slurm commands.
7. The local executor and the coordinator's local fallback (SPEC-0338) stay as the no-cluster
   path, and every fallback says why (SPEC-0505).
8. Integrations are validated against the real binaries on the current box. Fake
   `sbatch/squeue/sacct/scontrol/scancel/sstat` shims (a shared fixture, `tests/slurm_fake.py`,
   introduced by SPEC-0510) prove the logic; each child's Test plan also makes one real call.

**QOS ladder (SPEC-0511).** `nw-campaign` (1000) > `nw-gatejob` (500) > `nw-persona` (300) >
`nw-bulk` (100, REQUEUE-preemptible by `nw-campaign`); a shared box adds `human` (700, never
preempted by nw). `priority/multifactor`, QOS weight dominant, age weight small (FIFO inside a
class, which is SPEC-0500's first-come-first-served).

## Breakdown / sub-specs

Phase 0, prep (accepted):

- [ ] SPEC-0503 — Executor seam on the worker (`local` = today), restart-safe `run-job`.
- [ ] SPEC-0504 — Per-gate resource requests in project config; measured CPU/peak memory in gate records.
- [ ] SPEC-0505 — A local gate fallback records why (IDEA-1080).
- [ ] SPEC-0506 — A ledger regenerated on the worker is applied, not re-run locally (IDEA-1082).
- [ ] SPEC-0507 — An exclusive timeout re-gate cannot starve (IDEA-1083).
- [ ] SPEC-0508 — Worker capacity and load in `nw worker info` / `nw workers list` (IDEA-1112).
- [ ] SPEC-0509 — Gate commands size from their allocation (`NW_GATE_CPUS`/`NW_GATE_MEM_MB`); lint `-n auto`/`nproc`.
- [ ] jerome `tools/gate/cargo.sh` Slurm-aware: size `-j` from `NW_GATE_CPUS`/`SLURM_CPUS_PER_TASK`, skip `systemd-run --user` and `ulimit -v` inside a Slurm job, move the 3 target-dir flocks to a Slurm license (project: jerome) — outbound work for the jerome repo, exported with `nw spec export` once SPEC-0509 is accepted; SPEC-0509 lists the exact requirements.

Phase 1, single-node Slurm on the current box (accepted):

- [ ] SPEC-0510 — Slurm executor: install verification, `sbatch` submit (sync gate-job submits and follows), state, cancel.
- [ ] SPEC-0511 — QOS ladder and job-class mapping (with a no-accounting fallback).
- [ ] SPEC-0512 — `nw queue jobs` governance over `squeue`/`scontrol`/`scancel`.
- [ ] SPEC-0513 — Slurm jobs survive laptop sleep (no lease self-cancel).
- [ ] SPEC-0514 — Slurm accounting into gate records; request sizing from MaxRSS/TotalCPU.
- [ ] SPEC-0515 — OOM, Slurm timeout, preemption and node failure are infra outcomes.

Phase 2, the new box (draft):

- [ ] SPEC-0516 — New box as controller and second node; configless `box`; `nw workers bootstrap --slurm`.
- [ ] SPEC-0517 — Multi-node job spool (shared `/srv/nw`, node-local scratch).
- [ ] SPEC-0518 — Shared-box policy (only if the box is shared).
- [ ] TUI pool strip with Slurm state and pending reasons — not yet specced; scaffold after SPEC-0512 ships.

Phase 3, retire (draft):

- [ ] SPEC-0519 — Retire count slots and gate-job tokens.
- [ ] SPEC-0520 — Retire placement, cooldown, cross-worker retry, drain internals and lost-job kill paths.
- [ ] SPEC-0521 — Retire pull workers.

Sequencing: SPEC-0503 first (every Phase 1 child plugs into it); SPEC-0504 and SPEC-0509 before
SPEC-0510 (the executor needs requests and the gates need sizing env). SPEC-0505..0508 are
independent and can go in parallel. SPEC-0511..0515 follow SPEC-0510; SPEC-0514's history half
and SPEC-0511's QOS half need `slurmdbd` on the current box (a later root step of the human; both
specs work in a reduced mode until then). Phase 2 needs the new box; Phase 3 needs Phase 1's
exit criterion.

## Acceptance criteria

- [ ] Every remote gate, gate job, sentinel/cadence job and persona run on a Slurm worker is a
      Slurm job named by its nw id, with explicit cpus and memory, inside a cgroup that enforces
      both.
- [ ] No two jobs together exceed a node's schedulable cores or memory (Slurm admission); nw's
      own slot/token admission is gone (Phase 3).
- [ ] `nw queue jobs` lists, holds, releases, moves up, re-classes, requeues and cancels nw jobs
      by nw id against the real Slurm.
- [ ] Gate records carry requested and measured cpu/memory for every remote gate.
- [ ] A coordinator asleep for longer than the old 900 s lease does not lose a Slurm job's result.
- [ ] Both boxes take jobs from one Slurm cluster (Phase 2).
- [ ] `nw workers update` stays sudo-free on every node.

## Test plan

- **Integration/e2e tests:** `tests/test_slurm_e2e.py` (marked `remote`) drives a campaign gate,
  an `nw gate run` job and a persona run through a worker configured `executor: slurm` against
  the fake shim fixture: submit -> PENDING (reason surfaced) -> RUNNING -> COMPLETED -> gate
  record with `resources`; and hold/release/top through `nw queue jobs`.
- **Manual verification (real binaries, current box):** with the box's worker set to
  `executor: slurm`, run one campaign gate and one `nw gate run` of nightwright; observe
  `squeue -u nw` (as `bruno@morais-Desktop`) showing both jobs named by nw id with their
  cpus/mem; hold and release one with `nw queue jobs`; confirm the gate record's `resources`
  block. Phase 2 repeats this with jobs landing on both nodes.
- **Exit criterion of Phase 1 (gates Phase 3):** one week of gates on the current box under
  Slurm admission, green, with `TotalCPU/Elapsed` <= requested cpus and a first measured MaxRSS
  table (SPEC-0514 `nw gate stats`).

## Rollout / sequencing

Phase 0 merges with behaviour unchanged. Phase 1 flips the current box to `executor: slurm`
(one config key on the box) and raises its registry `slots` so Slurm is the only real
admission (otherwise jobs wait in nw where `squeue` cannot show them); flipping back is the same
key. Phase 2 adds the new box. Phase 3 deletes the replaced code after the exit criterion.

## Definition of done

- [ ] All child specs `done` or `superseded` (the linter gates this).
- [ ] Integration test plan executed; affected tests green.
- [ ] Acceptance criteria met; epic body reflects what shipped.
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

- Whether the new box is nw-only or shared is undecided; SPEC-0518 covers the shared branch and
  stays `draft` until the human decides. This does not block Phases 0-1.
