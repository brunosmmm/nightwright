---
id: SPEC-0344
title: "Remote persona jobs with bundle write-back"
status: accepted
owner: user
created: 2026-11-01
updated: 2026-11-02
source_idea: IDEA-560
parent: SPEC-0310
tags: [agentic, remote, workers, personas]
depends_on: [SPEC-0342]
supersedes: [SPEC-0333]
---

## Context

Persona runs (implementer, triage, reviewer, ...) are the other half of campaign time.
Today each one is a `RunRequest` (`src/nightwright/agentic/runner.py:33`). `ClaudeRunner`
turns it into a `claude -p` argv with `build_argv` (`runner.py:153`) and runs it through the
detached wrapper (`runproc.py`). The result lands in the run dir: `request.json`,
`result.json`, the transcript, and `cost_usd`. A writing persona changes the campaign
worktree, and the queue then commits them (`git.commit_all(worktree, msg)` in
`queue._do_persona`).

[SPEC-0340](./0340-worker-async-job-protocol.md),
[SPEC-0341](./0341-coordinator-re-attach-job-records-and-adoption.md) and
[SPEC-0342](./0342-lost-job-retry-across-workers-and-escalation.md) provide durable,
re-attachable remote jobs with retry. This spec adds persona jobs on top of them.

This is the first part of the split of
[SPEC-0333](./0333-remote-persona-runs-with-bundle-write-back.md) (its spec review
recommended a split). The other parts are
[SPEC-0345](./0345-persona-cap-rate-limit-backoff-affinity-and-cost-by-worker.md) (account
cap, backoff, affinity, cost per worker) and
[SPEC-0346](./0346-remote-workers-integration-suite.md) (the epic's integration suite).

## Goals / Non-goals

**Goals**
- A persona run can execute on a worker and produce the same verdict and the same run-dir
  files (equal up to worker-local paths, timings and session data). For a writing persona, it also produces the same commits on the campaign branch as a local
  run.
- A remote run writes the same `run.start`/`run.end` ledger entries as a local run, so the
  stuck-run detectors work unchanged.
- A failed write-back never leaves the worktree half-changed, and the run is retried
  locally.
- `nw workers set --kinds/--persona-slots` and `nw workers check --auth` for persona
  workers.

**Non-goals**
- Coordinator-side `claude` credentials on the worker. The worker uses its own `claude`
  login, and `nw workers check` verifies it.
- The global persona cap, rate-limit backoff, affinity and `cost_by_worker` (SPEC-0345).
- The epic's integration test file (SPEC-0346).
- Running the conductor or stewards on workers
  ([SPEC-0335](./0335-federation-over-a-networked-mailbox.md)).
- Moving a running persona from one worker to another.

## Decision

Add job kind **`persona`**. The coordinator serializes the `RunRequest` with a new explicit
`runner.request_to_job(req, worktree)` (paths made relative to the worktree), and the worker
rebuilds it with `runner.request_from_job(doc, tree, run_dir)`. The worker runs it with the
runner chosen by `runner.select_runner(<worker cfg>)` (its own `ClaudeRunner`, or
`FakeRunner` under `agentic.runner: fake`) inside the SPEC-0340 job process.

Remote dispatch is a new detachable `RemoteRunner` (a `PersonaRunner`, see Design ›
Dispatch) that `queue._do_persona` uses in place of the local runner when Eligibility
holds; the rest of `_do_persona` is unchanged.

At the end of the run, the worker sends back a **result bundle**: the new commits, plus a
`git add -A` + `git stash create` commit for everything left uncommitted (including new
untracked, non-ignored files). The coordinator applies it to the campaign worktree with a
fast-forward and `git stash apply --index`. The worktree ends up exactly as if the persona
had run locally, and the normal `git.commit_all` step (`git add -A`) in `queue._do_persona`
takes over from there.

## Design

### Eligibility

A run goes remote only when **all** of these hold:
- an enabled worker with `kinds` containing `persona` has a free persona slot;
- every `add_dirs` entry is inside the worktree (personas that read the data dir or another
  repo stay local);
- the worktree has no untracked, non-ignored files (the SPEC-0338 rule);
- for a writing persona, the worktree is **clean** (no tracked modifications either), so
  write-back starts from exactly `base_head`;
- the persona is not `conflict_resolver` and the worktree has no rebase or sequencer in
  progress (`gitops.rebase_in_progress`, `gitops.sequencer_in_progress`), since write-back's
  fast-forward and stash apply are meaningless mid-rebase;
- the step's state does not list the worker in `remote_excluded` (see Write-back step 4),
  and `request_to_job` accepts the request (`allowed_tools`, `add_dirs`, env; see The job).

(The global persona cap and the rate-limit backoff are added to this list by SPEC-0345.)

`kinds` is set with `nw workers add|set NAME --kinds gate,persona`; the default stays
`gate`. Persona slots are a separate pool, `gate-slots/w-<W>/persona-<n>.lock`, sized by
`--persona-slots` (default 1).

### Dispatch

Remote dispatch is a `RemoteRunner` class in `src/nightwright/agentic/runner.py`
implementing `PersonaRunner`. `queue._do_persona` picks it per run: when Eligibility holds
it calls `RemoteRunner(worker, ...).run(req, hooks=...)` instead of `ctx.runner.run(...)`;
otherwise it uses `ctx.runner` as today. `RemoteRunner.run`:

1. records `base_head` (the worktree HEAD) and submits the job through SPEC-0340. The
   SPEC-0341 job record under `remote-jobs/` stores `job_id`, `worker`, `run_id`, `persona`,
   `base_head`, `read_only` and the step snapshot, so write-back survives a coordinator
   restart;
2. writes the same `run.start` ledger entry a local run writes;
3. follows the job's frames; on `done` it performs write-back (below), writes the run-dir
   files and `run.end`, deletes the job record, and returns the `RunResult`.

`RemoteRunner` is **detachable** like `ClaudeRunner` (SPEC-0225): on a stop or restart it
returns `DETACHED` and leaves the job record. On the next start, the existing SPEC-0341
adoption in `queue` (`_adoptable` on live remote job records) re-attaches a `kind: persona`
record to a `RemoteRunner` built from the record. When the `done` frame is read it applies
write-back **once**: the record is marked `writeback: applied` (with the resulting HEAD)
before it is deleted, and a record already marked `applied` is not applied again. Then
`_do_persona` continues with `adopted` set, exactly as for an adopted local run.

### The job (`kind: "persona"`)

The payload is:

```
{run_id, persona, request: <runner.request_to_job(req, worktree)>, json_schema, read_only,
 timeout_s, max_budget_usd}
```

- `request_to_job` / `request_from_job` are explicit field-by-field serializers (not the
  run dir's `request.json`), with a round-trip test.
- `request_to_job` rewrites worktree-absolute paths in `allowed_tools` (for example a
  `Bash(<worktree>/...)` gate pattern from `personas._gate_tool_patterns`) to
  worktree-relative form, and `request_from_job` re-anchors them at the worker's `tree`. If
  an `allowed_tools` entry references an absolute path outside the worktree, or an
  `add_dirs` entry is outside it, `request_to_job` raises `NotRemotable` and the run stays
  local. `permission_mode` passes verbatim.
- The env allowlist follows the persona env set in `personas.py`. `NW_CAMPAIGN`,
  `NW_SPEC_ID`, `NW_BASE`, `NW_PERSONA_RUN` and `NW_REBASE_TARGET` pass verbatim.
  `NW_WORKTREE`, the `tmp_env` keys (`TMPDIR` and the others), `NW_DATA_DIR` and
  `NW_CONFIG_DIR` are stripped by the coordinator and re-set by the worker to its own
  values. Any other key is refused, as in SPEC-0336.
- `job_id` is `<campaign>-persona-<run_id>-a<attempt>`.
- The worker:
  1. checks the commit out (SPEC-0336 and SPEC-0338, `have` and the bundle);
  2. creates a branch `nw/job/<job_id>` at it;
  3. builds `RunRequest(cwd=tree, run_dir=<job>/run, add_dirs=tree/...)`;
  4. runs `select_runner(<worker cfg>).run(req)` in the job process. A persona run may take
     a long time, and the job process is already detached and leased (SPEC-0340).
- **Progress frames.** This spec adds the `progress` frame type to `nw.job.frame.v1`: every
  30 s the job emits `progress {ts, transcript_bytes, last_line_ts}`. The coordinator
  records the latest one in the job's `remote-jobs/` record (SPEC-0341 liveness) and mirrors
  it into the local run dir as `progress.json`, for `nw campaign show` and the dashboard.
  The detectors need nothing new: the coordinator writes the same `run.start` and `run.end`
  ledger entries a local run writes, so `run_overdue` (age of the open `run.start` against
  `timeout_s`, `events.py`) and `campaign_silent` (skipped while a run is open) behave
  exactly as for a local run.
- The `done` frame carries the `RunResult` fields, plus the run-dir files (`result.json`,
  the transcript, `request.json`), gzip+base64 and capped at 16 MiB each. For a
  non-read-only persona it also carries the **result bundle**:
  - `head`: the tree's HEAD after the run;
  - `wip`: after the run the worker runs `git add -A` and then `git stash create`, so new
    untracked, non-ignored files are included; or null when `git status --porcelain` is
    empty;
  - `bundle_b64`: the bundle bytes, with the job's input commit as the base.
- The run record (`write_run_record`) gains `worker` (null when local), and the `run` and
  `run_failed` events carry `worker` when the run was remote.

### Write-back (coordinator)

1. Before it submits a writing persona job, the coordinator records `base_head` (the
   worktree HEAD). The worktree is clean (Eligibility).
2. On `done`:
   - fetch the bundle into the campaign repo under `refs/nw/jobs/<job_id>/{head,wip}`;
   - check that the worktree HEAD is still `base_head`. Affinity and the one-step-per-campaign
     rule make a moved HEAD a bug, so it is detected rather than merged; failure reason
     `write-back: head moved`;
   - check that the worktree is still clean (`git status --porcelain` empty), else fail
     with `write-back: worktree dirty`;
   - `git merge --ff-only <head>` in the worktree, then `git stash apply --index <wip>` if
     `wip` is set (this restores the new files as well as the modifications; the later
     `commit_all` re-adds everything with `git add -A`);
   - delete the refs.
3. A failure before the fast-forward (fetch, a corrupt bundle, a moved HEAD, a dirty
   worktree) leaves the worktree untouched. If any step after the fast-forward fails (a
   stash conflict), the coordinator restores the worktree with `git reset --hard
   <base_head>` followed by `git clean -fd -- <paths>`, limited to the paths the `wip`
   commit adds (`git diff --name-only --diff-filter=A <head> <wip>`), before reporting. In
   both cases the worktree ends at `base_head` with the same status it had before the
   fast-forward.
4. A write-back failure is reported and retried like this:
   - `RemoteRunner` writes the run's `run.end` ledger entry with `is_error: true`,
     `result_tail: "write-back: <reason>"`, `writeback_failed: true` and `worker`. `"write-back:"`
     is added to `events._TRANSIENT_MARKERS`, so the projected `run_failed` has
     `transient: true`.
   - `RemoteRunner` adds the worker to the step's state key `remote_excluded` (a list of
     worker names in the campaign state, cleared when the step completes) and returns the
     failed `RunResult` with `writeback_failed` set.
   - `_do_persona`, seeing `writeback_failed`, immediately re-runs the same step **once**
     in-process with the local runner (`ctx.runner`, i.e. `select_runner(cfg)`), under a
     new `run_id`, without going through autopark. Eligibility checks `remote_excluded`, so
     the retry cannot be dispatched back to that worker.
   - A write-back failure retries **locally only**. A lost job (the worker or connection
     died) follows SPEC-0342's lost-job path instead.
5. The fetched refs stay under `refs/nw/jobs/<job_id>/` and are listed in the run record as
   `saved_refs` (so the work can be inspected); they are pruned when the campaign closes.
6. A read-only persona has no write-back. Only the verdict and the files come back.

### `nw workers check` and `set`

`check` adds: `claude --version` on the worker, and a 1-token `claude -p` auth probe, run
only with `--auth` because it costs money. A worker with `kinds` containing `persona` and no
working `claude` fails the check. This needs one more allowed subcommand on the worker,
`nw worker claude-check [--auth]`, added to the `ssh-command` grammar (SPEC-0340).

`nw workers set NAME [--kinds gate,persona] [--persona-slots N]` is new (SPEC-0337 has no
`set`); it updates the record through `workers.update`.

## Alternatives considered

- **Use the coordinator's credentials on the worker** (forwarding `ANTHROPIC_API_KEY` or the
  OAuth token). That moves a secret across hosts for nothing: the worker can be logged in
  once.
- **Write back by pushing a branch.** This needs a shared remote and credentials (see
  SPEC-0338). A bundle needs neither.
- **A three-way merge on write-back.** A moved HEAD means two writers on one campaign, which
  the queue forbids. Detecting it is safer than merging.

## Acceptance criteria

- [ ] A writing persona run on a worker leaves the campaign worktree identical (HEAD, tree,
      uncommitted changes, including a newly created untracked file) to the same
      `FakeRunner` script run locally.
- [ ] A read-only persona run on a worker returns the same `verdict` and `verdict_ok`, and
      the run dir contains `result.json`, the transcript and `request.json`; `request.json`
      equals the local one after normalizing `cwd`, `run_dir`, `add_dirs` and the re-set env
      keys.
- [ ] A write-back failure (moved HEAD, dirty worktree, stash conflict, corrupt bundle)
      leaves the worktree at `base_head` with its prior status (after a stash conflict, via
      `git reset --hard <base_head>` plus `git clean -fd` on the stash's added paths). The
      run's `run.end` has `is_error: true`, `result_tail: "write-back: <reason>"` and
      `writeback_failed: true`; the projected `run_failed` has `transient: true` (via the
      `write-back:` entry in `_TRANSIENT_MARKERS`); the worker is listed in the step's
      `remote_excluded`; and `_do_persona` re-runs the step once in-process with the local
      runner, without autopark.
- [ ] A persona job whose coordinator restarts mid-run is re-attached from its SPEC-0341
      job record (which holds `base_head`, `worker`, `job_id`) by a `RemoteRunner`, and its
      write-back is applied exactly once.
- [ ] A failed write-back keeps the fetched refs as `saved_refs` in the run record.
- [ ] A persona with an `add_dirs` entry outside the worktree, with an `allowed_tools` path
      outside the worktree, with an untracked file in the worktree, (if writing) with a
      modified tracked file, the `conflict_resolver` persona, or a worktree with a rebase or
      sequencer in progress, runs locally.
- [ ] A remote persona run writes the same `run.start`/`run.end` ledger entries as a local
      one, so `run_overdue` fires past `timeout_s` and `campaign_silent` stays quiet while
      it is open; `progress` frames land in `progress.json`.
- [ ] `runner.request_to_job` / `request_from_job` round-trip a `RunRequest`, with
      worktree-absolute `allowed_tools` paths made relative and re-anchored at the worker's
      tree. An env key outside the allowlist is refused; stripped keys are re-set by the
      worker.
- [ ] The run record, and the `run` / `run_failed` events, carry `worker` for a remote run.
- [ ] `nw workers set --kinds/--persona-slots` updates the record, and `nw workers check
      --auth` passes with a working (fake) `claude` and fails a persona worker without one.
- [ ] With no persona-capable worker (the default `kinds` is `[gate]`), persona runs are
      unchanged.

## Test plan

- **Automated tests** (`tests/test_remote_personas.py`, offline): the loopback workers
  (SPEC-0338 and SPEC-0340), with the worker configured for `FakeRunner` (`agentic.runner:
  fake`). The cases:
  - **write-back equality:** a fake implementer script commits one file and leaves
    uncommitted changes; the uncommitted change includes a newly created (untracked) file
    and a modified tracked file. The remote and local outcomes compare equal (commit tree
    hashes, `git status --porcelain`, and the `git diff` of the worktree), and after
    `commit_all` the committed trees are equal;
  - **read-only reviewer:** `verdict` and `verdict_ok` are equal; the run dir has
    `result.json`, the transcript and `request.json`, and `request.json` equals the local one
    after normalizing `cwd`, `run_dir`, `add_dirs` and the re-set env keys;
  - **write-back failures:** HEAD moved (a commit made in the worktree after submission), a
    dirty worktree (an untracked file created after submission), a stash conflict (a tracked
    file the fake script also edits is modified in the worktree after submission, with the
    clean check stubbed so the run reaches `stash apply`), and a corrupt bundle. Each gives a
    `run.end` with `is_error`/`writeback_failed` and `result_tail` starting `write-back:`,
    a projected `run_failed` with `transient: true`, a worktree back at `base_head` with its
    prior status (no leftover stash files after the conflict case), the worker in
    `remote_excluded`, and exactly one local re-run by `_do_persona` with no autopark entry;
  - **restart / adoption:** stop the coordinator loop while a remote persona job runs
    (`RemoteRunner` returns `DETACHED`), start a new one, let the job finish: the record is
    adopted, write-back is applied once (HEAD advanced once, a second adoption of an
    `applied` record changes nothing), and `_do_persona` commits with `adopted` set;
  - **eligibility:** `add_dirs` outside the tree, an `allowed_tools` path outside the tree,
    an untracked file, a writing persona with a modified tracked file, the
    `conflict_resolver` persona, and a worktree with a rebase in progress, each run locally;
  - **serialization:** `request_to_job` / `request_from_job` round-trip, including a
    worktree-absolute `allowed_tools` gate pattern made relative and re-anchored;
  - **env:** an allowed key passes, a stripped key is re-set by the worker, another key is
    refused;
  - **saved refs:** a failed write-back lists `saved_refs` and the refs exist;
  - **progress and detectors:** `progress.json` updates from frames; with a short
    `timeout_s` an open remote run fires `run_overdue`, and `campaign_silent` does not fire
    while it is open;
  - **worker key:** `worker` in the run record and events;
  - **workers set / check --auth:** `CliRunner` with a fake `claude` script on the worker's
    PATH (working, and failing).
- **Manual verification:**
  1. Log `claude` in on the second machine and run `nw workers check box --auth`.
  2. Set `--kinds gate,persona`.
  3. Let one real campaign run its implementer on `box`. The merged result must be correct.
     Check the write-back: `git log -p base_head..HEAD` in the campaign worktree equals the
     worker branch `nw/job/<job_id>` (from the job's `frames.jsonl`), and every file the
     transcript shows being edited appears in that diff.
- **Regression guard:** affected tests while iterating. The persona-run and queue tests run
  unchanged with no workers.

## Rollout / migration

Implementation sessions clock in and out on the linked idea (`nw idea clock-in IDEA-560` /
`nw idea clock-out IDEA-560`).

1. Ships with persona jobs disabled: the default `kinds` is `[gate]`.
2. The human turns persona jobs on per worker with `--kinds gate,persona`, after
   `check --auth`.
3. `nw workers set box --kinds gate` returns persona runs to local.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while splitting SPEC-0333:
- The worker uses its own `claude` login.
- Write-back is a fast-forward plus a stash apply, with a moved HEAD treated as a failure.
- Persona jobs are opt-in per worker.
- A writing persona goes remote only from a clean worktree.
- The env is an allowlist mirroring `personas.py`; worker-local paths are re-set by the
  worker.
- A write-back failure retries locally only (once, in-process, the worker excluded via
  `remote_excluded`) and keeps its refs for inspection.
- Remote dispatch is a detachable `RemoteRunner`; `base_head` lives in the SPEC-0341 job
  record so write-back survives a restart.
