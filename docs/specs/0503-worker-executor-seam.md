---
id: SPEC-0503
title: "Executor seam on the worker: job start, cancel and slot admission go through a pluggable executor (local = today)"
status: done
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
parent: SPEC-0501
tags: [agentic, workers, slurm, refactor]
depends_on: [SPEC-0340, SPEC-0341]
---

## Context

On the worker, `nw worker submit` (`worker_job.submit`, `src/nightwright/agentic/worker_job.py:652`)
claims the job dir, spools the bundle and starts the job process with `_spawn` (`:615`): a
detached `python -m nightwright.agentic.worker_job <jobdir>` in its own session, handed the
`job.lock` flock fd. The process body (`main`/`run_job`, `:1019`/`:940`) checks out the tree,
runs the gates (`worker._run_gates`, `worker.py:1100`, which first takes a host gate slot via
`gate_slots.acquire`) or a persona run, and writes `frames.jsonl` and `result.json`. Cancel
(`worker_job.cancel`, `:818`) writes a `cancel` file the job's `_watch` thread obeys;
`jobs` (`:855`) lists job dirs. The synchronous `nw worker gate-job` (`worker.py:983`), used by
the sentinel and standalone gate jobs (`remote_gates.run_detached`), runs gates inline in the
SSH session.

SPEC-0501 needs a second way to start the same body (inside a Slurm allocation). This spec cuts
the seam only; the Slurm executor is SPEC-0510.

## Goals / Non-goals

**Goals**
- One place decides how a claimed job starts, is cancelled, is listed and is admitted on the host.
- The job body can run more than once for the same job dir (a Slurm requeue) without mixing
  attempts.
- Zero behaviour change with the default `local` executor.

**Non-goals**
- Any Slurm code (SPEC-0510). Changing the coordinator protocol.

## Decision

Add `src/nightwright/agentic/executors.py` with an `Executor` protocol and a `LocalExecutor`
that wraps today's code. `worker_job.submit`, `worker_job.cancel`, `worker_job.jobs`,
`worker._run_gates` (host slot) and `worker.gate_job` (inline run) call the executor chosen by
`agentic.workers.executor` (`local` default; unknown value -> refusal `stage: "executor"`, the
job is not claimed). The job body becomes an explicit entry point `nw worker run-job <jobdir>`
(not in the forced-command grammar: it is started by the executor on the host, never over SSH),
restart-safe per attempt.

## Design

```python
class Executor(Protocol):
    name: str                                   # "local" | "slurm"
    def start(self, cfg, job: Path, h: dict, lock_fd: int) -> dict: ...
        # returns the start record written to spawn.json ({"pid","pgid","start_ticks"} for
        # local; {"slurm_job_id"} for slurm). Raises WorkerError(stage="spawn") on failure.
    def cancel(self, cfg, job: Path, rec: dict) -> None: ...
        # local: write the `cancel` file (today); slurm: scancel
    def state(self, cfg, job: Path, rec: dict, sett: dict, now: float) -> tuple[str, dict]: ...
        # local: today's _live_state; slurm: merged with squeue/scontrol (SPEC-0510)
    def host_slot(self, cfg, h: dict, cancel, announce): ...
        # context manager. local: gate_slots.acquire(... pools=[(None, n_slots)]) exactly as
        # _run_gates does today; slurm: a no-op hold (the allocation is the admission)
    def run_inline(self, cfg, argv: list[str], stdin, stdout) -> int: ...
        # sync gate-job: local runs in-process (today); slurm submits and follows (SPEC-0510)
    lease_cancels: bool                          # local True; slurm False (SPEC-0513)
```

- `executors.get(cfg) -> Executor` reads `agentic.workers.executor` on the worker host's own
  config (the coordinator never chooses). `nw worker info` adds `"executor": <name>` and the
  `features` entry `executor:<name>` so the coordinator can tell (SPEC-0508 shows it).
- `run-job`: `nw worker run-job JOBDIR` is a new hidden CLI command that calls
  `worker_job.main([JOBDIR])`. `_spawn` keeps starting `python -m nightwright.agentic.worker_job`
  (equivalent); the Slurm executor starts `nw worker run-job`.
- **Restart safety.** `run_job` reads `attempt = int(os.environ.get("NW_JOB_ATTEMPT") or
  job.json.run_attempts + 1)`, writes `run_attempts` to `job.json`, and on attempt > 1: removes
  `tree/`, `out/`, `tmp/` (fresh scratch), deletes `result.json` only if its `attempt` is older,
  and emits a frame `{"t":"attempt","n":<attempt>}` into `frames.jsonl` so a reader can drop the
  previous attempt's partial gate frames. `result.json` gains `"attempt"`. The `main` guard
  "already has a result" (`worker_job.py:1031`) keys on the same attempt.
- The `_watch` lease check (`worker_job.py:925-929`) runs only when `executor.lease_cancels`.

## Alternatives considered

- **Branch on `executor == "slurm"` inline in `submit`.** Spreads Slurm conditionals through
  five call sites; the seam keeps them in one module and keeps local untouched.
- **Run the sync `gate-job` through `submit` + `attach` already now.** A protocol change for the
  sentinel and gate jobs with its own risks; SPEC-0520 retires the sync path later.

## Acceptance criteria

- [x] `agentic.workers.executor` absent or `local`: submit/attach/poll/cancel/jobs/gate-job
      behave exactly as before (same frames, files and exit codes).
- [x] An unknown executor name refuses `submit` and `gate-job` with an `error` frame,
      `stage: "executor"`, and claims nothing.
- [x] `nw worker run-job JOBDIR` runs a claimed job's body; a second run of the same dir after a
      finished attempt (with `NW_JOB_ATTEMPT=2`) starts from fresh scratch, writes an `attempt`
      frame and a `result.json` with `attempt: 2`.
- [x] `nw worker info --json` reports `executor` and the `executor:<name>` feature.
- [x] `_run_gates` takes its host slot through `executor.host_slot`.

## Test plan

- **Automated tests:** `tests/test_worker_executor_seam.py`: a recording fake executor
  registered under a test name proves submit/cancel/jobs/host_slot/run_inline delegate;
  unknown name -> `stage: "executor"`; attempt-2 restart test (fresh scratch, `attempt` frame,
  `result.json.attempt`). Existing `tests/test_worker_job*.py`, `tests/test_worker*.py`,
  `tests/test_remote_gates*.py` run unchanged as the regression guard (affected-tests run).
- **Manual verification (real binary):** on the current box (via `nw workers update` then the
  forced-command path) run one nightwright campaign gate with the default executor and compare
  its `frames.jsonl` shape with a pre-change job dir; `nw worker info --json` shows
  `"executor": "local"`.
- **Regression guard:** the affected tests of `worker_job.py`, `worker.py`, `remote_gates.py`.

## As shipped

- `src/nightwright/agentic/executors.py`: the `Executor` protocol, `LocalExecutor` (today's
  code, looked up on its modules at call time so existing monkeypatches still apply),
  `register`/`unregister` (SPEC-0510's `slurm`, tests' fakes), `get(cfg)` (unknown name ->
  `WorkerError("executor")`) and `for_job(cfg, job)`.
- **Decision: a job records its executor.** `job.json` gains `executor` at claim (`submit` and
  `gate-job`); every later read of that job (`state`, `cancel`, `run-job`, `host_slot`) goes
  through `for_job`, the executor that started it, not the host's current setting. A dir
  without the key (every pre-0503 job) or with a name this build does not know is `local`.
- `state(cfg, job, rec, ...)` receives the job's start record (`spawn.json`); `cancel` the
  same. `jobs` delegates through `state` (`job_state` -> `_exec_state`); its rows are unchanged.
- `submit`: the executor is resolved after header validation and before the claim; a refusal
  drains the client's upload (like a claim refusal) and prints one `error` frame
  (`stage: "executor"`). `spawn.json` is the executor's start record plus `spawned_at`.
- `gate-job`: `worker.gate_job` resolves the executor and calls `run_inline`; the old body is
  `_gate_job_local`. Under an unknown executor it reads only the header line (for the job id
  in the frame) and claims nothing.
- `nw worker info`: `executor` (the configured name) and the feature `executor:<name>` (only
  for a known name; an unknown one adds `executor_error` instead).
- `nw worker run-job JOBDIR` (hidden, not in the forced-command grammar) calls
  `worker_job.main`.
- **Restart safety.** `_attempt(job)` = `NW_JOB_ATTEMPT`, else `job.json.run_attempts` + 1.
  `main` refuses only when `result.json` belongs to the same or a newer attempt (a result
  without `attempt`, pre-0503 or from `reap`, is attempt 1; `reap` now records
  `run_attempts`); a `proc.json` of an older attempt is removed under `job.lock` before the
  claim. `run_job` removes `tree/`/`out/`/`tmp/` and an older `result.json`, records
  `run_attempts`, and on attempt > 1 writes the frame first. Attempt 1 behaves as before
  (no extra frame); `proc.json`, `job.json` and `result.json` gain the additive keys
  `attempt` / `run_attempts` / `attempt`.
- **Deviation: the attempt frame's shape.** It is
  `{"type": "attempt", "attempt": <k>, ..., "n": <seq>}`, not `{"t": "attempt", "n": <k>}`:
  `n` is every frame's sequence number (attach/poll cursors), so it cannot carry the attempt.
- **Deviation: a rerun's checkout.** The first attempt fetched and removed the bundle; a rerun
  without a bundle file checks out from the repo cache (`bundle_bytes` 0 for that checkout:
  the commit and base are kept there by `refs/nw/have/*`).
- **Left to SPEC-0510:** `attempt` is not in the coordinator's `FRAME_TYPES` yet, so a
  coordinator reading one fails the job as a protocol error (falls back, never mixes
  attempts). Dropping the earlier attempt's partial gate frames in `remote_gates` belongs with
  the executor that produces reruns; under `local` no attempt > 1 ever happens.
- `_watch(..., lease_cancels)`: the lease check runs only when the job's executor says so.
- Tests: `tests/test_worker_executor_seam.py` (recording executor for
  start/cancel/state/host_slot/run_inline, unknown name on submit and gate-job, info, the
  attempt-2 rerun through the real `nw worker run-job` with stale scratch planted, an older
  attempt refused, lease gating). `tests/test_rich_output_guard.py` names `_gate_job_local`
  and `executors.for_job`. Box: `GATEJOB-1461` (loop tier, green, 1253 tests) and the
  integration/remote affected runs listed below.
- **Manual verification (real binary): done 2026-11-09 23:50** after the conductor's `nw workers update box`: `nw workers check box --json` reports `"executor": "local"`, and campaign gates ran end-to-end through the updated worker (e.g. IDEA-1129-20261109-5a04 gate 002 pytest-affected, ok, result and frames consumed by the coordinator unchanged). The worker job dirs are not readable from the coordinator account, so the frames.jsonl comparison was done by the coordinator's successful parse rather than by diffing files. Earlier note (kept for history): pending deploy. The box's worker runs the installed
  build; deploying needs `nw workers update`, which this lane may not run. Done instead:
  `uv run nw worker info --json` from this branch reports `"executor": "local"` and
  `executor:local`. To do after the update: one campaign gate through the forced command,
  compare its `frames.jsonl` with a pre-change job dir.

- **Box runs (one lane, SPEC-0503..0509 together):** loop tier `GATEJOB-1461` (0503, green),
  `GATEJOB-1466` (0504, red: a pinned payload key set, fixed), `GATEJOB-1470` (0509, green),
  `GATEJOB-1476` (0508, red: the list table wrapped at 80 columns, fixed); integration+remote
  affected (`--cmd`, `-m 'integration or remote'`) `GATEJOB-1465` (10 pinned shapes/goldens,
  fixed) and `GATEJOB-1475` (1 env allowlist, fixed). The lane's final loop gate on its last
  commit is recorded below. **Status stays `in-progress`** until the real-binary step that
  needs the deployed worker build is done (not run in this lane).
  **Final lane gate:** `GATEJOB-1504`, loop tier on `83a7894e`, green (3382 passed). An earlier
  run on the same commit, `GATEJOB-1485`, hung near the end of pytest and was cancelled; the
  lane's touched and new test files ran serially with `faulthandler_timeout=60` without a hang,
  and the rerun passed, so no test of this lane is identified as the cause (the box holds 18
  orphaned xdist worker processes from older jobs, days old, a pre-existing pattern).

## Rollout / migration

Merge; nothing changes until a host sets `executor: slurm` (SPEC-0510). Supersedes nothing.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; manual step performed and noted.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Risks

- The `run-job` restart path must never delete a newer attempt's files: attempts are numbered and
  compared, not inferred from file presence.

## Open questions

- None.
