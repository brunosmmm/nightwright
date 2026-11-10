---
id: SPEC-0504
title: "Per-gate resource requests in project config, and measured CPU time and peak memory in every gate record"
status: in-progress
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
parent: SPEC-0501
tags: [agentic, gates, slurm, config, measurement]
depends_on: [SPEC-0503]
---

## Context

A gate entry in `.nw/project.yaml` (validated in `src/nightwright/agentic/project.py:383`,
allowed keys `name, cmd, cwd, timeout_s, required, results, retries, on_fail, retry_cmd,
failed_first, when, paths, remote, reuse_from`) says nothing about cores or memory. The SPEC-0501
evidence found thread demand above the box's 12 threads for 49 % of busy time, and **no memory
measurement anywhere**. Slurm (SPEC-0510) needs `--cpus-per-task` and `--mem` per job, and the
requests must come from data, not code.

## Goals / Non-goals

**Goals**
- A gate may declare `resources: {cpus, mem, licenses}`; unset values get per-kind defaults.
- A worker job's request is derived from the gates it runs.
- Every gate record (campaign `gates/*.json`, gate-job rows, the ledger `gate` event) carries the
  requested resources and what the gate used, from today on, with or without Slurm.

**Non-goals**
- Enforcing the request on the local executor (that is Slurm's cgroup, SPEC-0510).
- Auto-tuning requests (SPEC-0514 suggests; the human edits the yaml).

## Decision

Add an optional `resources` mapping to gate entries, nw defaults per gate kind, a pure
`job_request(gates, tier)` that turns a job's gates into one Slurm-shaped request, and per-gate
measurement from `os.wait4` rusage on the gate process (local and remote alike). The header
payload carries the request to the worker.

## Design

- **Config** (`project.py`): new allowed key `resources` with
  `cpus: int >= 1`, `mem: str` matching `^\d+(\.\d+)?[MG]$` (e.g. `3G`, `512M`),
  `licenses: [str]` (each `^[a-z][a-z0-9_]*(:\d+)?$`). Unknown keys inside `resources` are a
  config problem, like the other gate keys.
- **Defaults** (`src/nightwright/agentic/gate_resources.py`, new), keyed by a gate's
  `results.format` and name, overridable globally by `agentic.gates.resource_defaults`:

  | match | cpus | mem |
  |---|---|---|
  | `results.format: pytest` and name `pytest` (full suite) | 5 | 5G |
  | `results.format: pytest` (any other) | 3 | 3G |
  | anything else | 1 | 1G |
  | persona job (`kind: persona`) | 1 | 2G |

  These are the research report's low-load estimates (SPEC-0501 table). jerome declares its
  cargo gates explicitly (`cpus: 6, mem: 8G, licenses: [jerome_target]`) in its own yaml.
- **Job request** `job_request(gates) -> {"cpus", "mem_mb", "licenses", "time_s"}`: gates run
  sequentially inside one job, so `cpus` and `mem_mb` are the **max** over the job's gates,
  `licenses` the union (max count per name), `time_s` = `remote_gates.job_timeout_s` (sum of
  timeouts + `RUN_TIMEOUT_MARGIN_S`, today's value). `remote_gates._header` puts it in
  `payload.resources`; `worker.validate_header` accepts and bounds-checks it (an absent block is
  valid: an old coordinator).
- **Measurement.** The gate runner in `gates.py` (the `_LIVE` Popen path) reaps each gate's
  process with `os.wait4(pid, 0)` and records `cpu_s = ru_utime + ru_stime` (the gate and every
  descendant it waited for) and `max_rss_mb = ru_maxrss / 1024` (the largest single process:
  pytest-xdist workers are separate processes, so this under-counts a parallel gate's total,
  recorded as `rss_scope: "max_process"`). `GateResult` gains `resources: {"request": {...},
  "used": {"cpu_s", "max_rss_mb", "rss_scope", "wall_s"}}`; the `gate` frame from the worker
  carries it; the coordinator writes it into `gates/<seq>-<name>.json` and the ledger `gate`
  event; `gate_jobs._rows` copies it. SPEC-0514 later overwrites `used` with Slurm's cgroup
  numbers (`rss_scope: "job_cgroup"`).

## Alternatives considered

- **Requests in code per project.** Violates "requests come from config"; jerome and nightwright
  differ.
- **Sum of gate requests per job.** Gates run one after another; summing over-reserves 2-3x.
- **Sampling `/proc` for total RSS.** A sampler thread per gate is more code than `wait4`, and
  Slurm's cgroup numbers replace it within Phase 1.

## Acceptance criteria

- [x] The project config validation (`project.py`, surfaced by `nw projects config`) accepts `resources: {cpus: 3, mem: 3G,
      licenses: [jerome_target]}` and rejects `cpus: 0`, `mem: 3GB`, unknown sub-keys.
- [x] `job_request` returns max cpus/mem, union licenses and the existing job timeout for a
      mixed gate list; defaults apply per the table.
- [x] A remote job header carries `payload.resources`; a header without it is still valid.
- [x] Every gate record written after this ships (local and remote, campaign and gate job)
      has `resources.request` and `resources.used` with `cpu_s`, `max_rss_mb`, `wall_s`.
- [x] nightwright's own `.nw/project.yaml` declares `resources` on `pytest-affected`
      (`cpus: 3, mem: 3G`), `pytest` (`cpus: 5, mem: 5G`), `docs-tests` (`cpus: 3, mem: 3G`).

## Test plan

- **Automated tests:** `tests/test_gate_resources.py` (validation table, defaults, `job_request`
  max/union/time); `tests/test_gates_rusage.py` runs a gate `python -c` that burns ~0.5 s CPU and
  allocates ~100 MB and asserts `cpu_s >= 0.4` and `max_rss_mb >= 90`; a remote-gate test with
  the existing worker fixtures asserts the frame and the gate record carry `resources`.
- **Manual verification (real binary):** after deploy to the box, one nightwright campaign gate:
  `jq .resources` on its `gates/*.json` shows plausible numbers (pytest-affected `cpu_s` in the
  hundreds, `max_rss_mb` in the hundreds).
- **Regression guard:** affected tests of `project.py`, `gates.py`, `remote_gates.py`,
  `worker.py`, `gate_jobs.py`.

## As shipped

- `src/nightwright/agentic/gate_resources.py`: config check, the defaults table (keys
  `pytest-full`, `pytest`, `default`, `persona`; `agentic.gates.resource_defaults.<key>:
  {cpus, mem}` overrides per host, a malformed override is ignored), `gate_request`,
  `persona_request`, `job_request`, `check_payload` (the worker's bounds check).
- **Shapes.** A request is `{"cpus": int, "mem_mb": int, "licenses": {name: count}}` (a mapping,
  not a list: the max-count union needs counts). `job_request(gates, cfg, time_s=None)` adds
  `time_s` (default `remote_gates.job_timeout_s(cfg, gates) + RUN_TIMEOUT_MARGIN_S`, the
  coordinator's real deadline for the job) and `gates: {name: request}`.
- **Decision: `resources` never travels inside the header's gate list.** `remote_gates._header`
  drops it with the other coordinator-only keys and sends the resolved job request as
  `payload.resources` (with the per-gate requests). An older worker validates the gate list
  against its own project schema and would refuse an unknown `resources` key on every job;
  it ignores an extra payload key. The worker hands each gate its request
  (`gate_resources.REQUEST_KEY`), a local run resolves it with the host's defaults
  (`_run_all_held`). A persona header carries `payload.resources` = `persona_request` +
  `time_s`.
- **Measurement.** `gates._MeteredPopen` overrides `Popen._try_wait` (and `poll`) to reap with
  `os.wait4`: the one reap the existing wait/kill code already did, now with rusage, so the
  timeout and kill paths are unchanged and a timed-out gate still has its numbers. `used` =
  `{cpu_s, max_rss_mb, rss_scope: "max_process", wall_s}` (`None` values only if the process
  was never reaped through `wait4`). A retried gate's `used` sums CPU and wall over all its
  attempts (and a `failed_first` prelude) and keeps the peak RSS.
- Where it lands: `GateResult.resources` -> the `gate` frame (`asdict`), the sidecar
  `gates/<seq>-<name>.json` (written by `_write_sidecar`, kept by `_rewrite_sidecar`; for a
  remote gate the worker's sidecar is the file the coordinator writes), the ledger `gate`
  event (`as_payload`; dropped when `None`, an older worker's result), `gate_jobs._rows`.
  `remote_gates._bad_result_field` accepts only a mapping (or null) for `resources`.
- `.nw/project.yaml` declares the three requests from the AC.
- Tests: `tests/test_gate_resources.py` (validation table, defaults, overrides, `job_request`,
  payload bounds, header with and without the block, the dogfood config);
  `tests/test_gates_rusage.py` (a real gate burning ~0.5 s CPU and ~100 MB: `cpu_s >= 0.4`,
  `max_rss_mb >= 90`; a timed-out gate keeps usage; retries sum; an in-process worker job's
  `gate` frame, `_take_gate`'s sidecar and `as_payload` carry it; the header strips per-gate
  `resources`). `tests/test_agentic_gates.py`'s pinned payload key set now includes
  `resources` (the box run `GATEJOB-1466` caught it).
- **Real binary, done:** this branch's gate runner on the real `spec-lint` gate of this repo:
  `{"request": {"cpus": 1, "mem_mb": 1024}, "used": {"cpu_s": 4.73, "max_rss_mb": 47.9,
  "wall_s": 4.82}}`. **Pending deploy:** the same check on a campaign's `gates/*.json` from the
  box (`pytest-affected` in the hundreds of `cpu_s`/MB) needs the coordinator restarted on this
  code and the worker updated (`nw workers update`), not run in this lane.

- **Box runs (one lane, SPEC-0503..0509 together):** loop tier `GATEJOB-1461` (0503, green),
  `GATEJOB-1466` (0504, red: a pinned payload key set, fixed), `GATEJOB-1470` (0509, green),
  `GATEJOB-1476` (0508, red: the list table wrapped at 80 columns, fixed); integration+remote
  affected (`--cmd`, `-m 'integration or remote'`) `GATEJOB-1465` (10 pinned shapes/goldens,
  fixed) and `GATEJOB-1475` (1 env allowlist, fixed). The lane's final loop gate on its last
  commit is recorded below. **Status stays `in-progress`** until the real-binary step that
  needs the deployed worker build is done (not run in this lane).

## Rollout / migration

Additive. Old records simply lack `resources`; readers treat it as optional. Supersedes nothing.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual step performed and noted.
- [ ] No regressions.
- [x] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Risks

- `wait4` must reap the exact pid the existing timeout/kill code waits on; a double reap would
  break `_wait_deadline`. The implementation replaces the `proc.wait` call, it does not add one.

## Open questions

- None.
