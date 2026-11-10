---
id: SPEC-0503
title: "Executor seam on the worker: job start, cancel and slot admission go through a pluggable executor (local = today)"
status: accepted
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

- [ ] `agentic.workers.executor` absent or `local`: submit/attach/poll/cancel/jobs/gate-job
      behave exactly as before (same frames, files and exit codes).
- [ ] An unknown executor name refuses `submit` and `gate-job` with an `error` frame,
      `stage: "executor"`, and claims nothing.
- [ ] `nw worker run-job JOBDIR` runs a claimed job's body; a second run of the same dir after a
      finished attempt (with `NW_JOB_ATTEMPT=2`) starts from fresh scratch, writes an `attempt`
      frame and a `result.json` with `attempt: 2`.
- [ ] `nw worker info --json` reports `executor` and the `executor:<name>` feature.
- [ ] `_run_gates` takes its host slot through `executor.host_slot`.

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

## Rollout / migration

Merge; nothing changes until a host sets `executor: slurm` (SPEC-0510). Supersedes nothing.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual step performed and noted.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Risks

- The `run-job` restart path must never delete a newer attempt's files: attempts are numbered and
  compared, not inferred from file presence.

## Open questions

- None.
