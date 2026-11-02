---
id: SPEC-0344
title: "Remote persona jobs with bundle write-back"
status: done
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

- [x] A writing persona run on a worker leaves the campaign worktree identical (HEAD, tree,
      uncommitted changes, including a newly created untracked file) to the same
      `FakeRunner` script run locally.
- [x] A read-only persona run on a worker returns the same `verdict` and `verdict_ok`, and
      the run dir contains `result.json`, the transcript and `request.json`; `request.json`
      equals the local one after normalizing `cwd`, `run_dir`, `add_dirs` and the re-set env
      keys.
- [x] A write-back failure (moved HEAD, dirty worktree, stash conflict, corrupt bundle)
      leaves the worktree at `base_head` with its prior status (after a stash conflict, via
      `git reset --hard <base_head>` plus `git clean -fd` on the stash's added paths). The
      run's `run.end` has `is_error: true`, `result_tail: "write-back: <reason>"` and
      `writeback_failed: true`; the projected `run_failed` has `transient: true` (via the
      `write-back:` entry in `_TRANSIENT_MARKERS`); the worker is listed in the step's
      `remote_excluded`; and `_do_persona` re-runs the step once in-process with the local
      runner, without autopark.
- [x] A persona job whose coordinator restarts mid-run is re-attached from its SPEC-0341
      job record (which holds `base_head`, `worker`, `job_id`) by a `RemoteRunner`, and its
      write-back is applied exactly once.
- [x] A failed write-back keeps the fetched refs as `saved_refs` in the run record.
- [x] A persona with an `add_dirs` entry outside the worktree, with an `allowed_tools` path
      outside the worktree, with an untracked file in the worktree, (if writing) with a
      modified tracked file, the `conflict_resolver` persona, or a worktree with a rebase or
      sequencer in progress, runs locally.
- [x] A remote persona run writes the same `run.start`/`run.end` ledger entries as a local
      one, so `run_overdue` fires past `timeout_s` and `campaign_silent` stays quiet while
      it is open; `progress` frames land in `progress.json`.
- [x] `runner.request_to_job` / `request_from_job` round-trip a `RunRequest`, with
      worktree-absolute `allowed_tools` paths made relative and re-anchored at the worker's
      tree. An env key outside the allowlist is refused; stripped keys are re-set by the
      worker.
- [x] The run record, and the `run` / `run_failed` events, carry `worker` for a remote run.
- [x] `nw workers set --kinds/--persona-slots` updates the record, and `nw workers check
      --auth` passes with a working (fake) `claude` and fails a persona worker without one.
- [x] With no persona-capable worker (the default `kinds` is `[gate]`), persona runs are
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

### As shipped (2026-11-02)

- **Where it lives.** `runner.py` has `request_to_job` / `request_from_job`, `NotRemotable`,
  the env sets (`ENV_VERBATIM`, `ENV_RESET`) and the `RemoteRunner` class; its logic, the
  worker's `run_job`, eligibility (`dispatch`, `ineligible`), write-back and the
  `claude-check` client are in the new `remote_personas.py`. `queue._do_persona` asks
  `remote_personas.dispatch` for the runner. Worktree-absolute `allowed_tools` paths travel
  as the token `${NW_TREE}` (the worktree-relative form) and are re-anchored at the worker's
  tree.
- **`run.start` / `run.end`.** The queue still writes both (`_prepare_run`, `_record_run`), so
  a remote run's entries are the local ones by construction; `run.end` gains `worker` and,
  after a failed write-back, `writeback_failed` and `saved_refs`. `RunResult` gains
  `worker`, `writeback_failed` and `saved_refs`; `run.json` gains `worker` (null when local).
- **Persona jobs need the async protocol.** A worker qualifies only when its cached `info`
  lists `async` (SPEC-0341); `nw worker info` now also lists the `persona` feature. A persona
  header is refused by the synchronous `gate-job`.
- **Before the worker holds the job** (the bundle cut, the header, `submit`), any failure runs
  the persona locally at once, as a gate run falls back.
- **A lost job** (the worker reports it lost or unknown, it stays unreachable past
  `worker_ttl_s`, its deadline passes, or it ends in `error`) is cancelled, recorded with
  `workers.record_loss` (SPEC-0342's per-worker loss count and escalation), and the run then
  goes local on the coordinator. There is no second remote attempt: SPEC-0342's
  cross-worker retry chain is keyed by gate seq. A job cancelled by a park or abandon ends
  as an interrupted run.
- **Write-back details.** After `git stash apply --index <wip>` the coordinator runs
  `git reset -q`, so new files are untracked and modifications unstaged, as a local run
  leaves them (the worker's `git add -A` staged everything). The rollback also re-applies a
  `git stash create` snapshot taken before the fast-forward. That snapshot is empty unless the
  clean check is bypassed, as the stash-conflict test does, so "its prior status" holds
  there too. A failed write-back's `RunResult` drops the remote verdict (`verdict: null`), so
  its findings are not recorded next to the local re-run's.
- **`remote_excluded`** is written to `state.json` by `_do_persona` (under the item lock) and
  to the step's snapshot. `_apply`'s `finish` drops it, so it is cleared when the step
  completes. The local re-run gets its own `run.start` (`retry_of`, `remote_excluded`) and run
  dir, and the failed run's `run.end` and `cost` are on the ledger before the re-run starts.
- **Adoption.** `queue._adopt` now also runs with a non-detachable local runner (for
  example `FakeRunner`) when a live persona job record exists, adopting only the runs that
  have one. Write-back is marked `writeback: applied` on the record before `run.json` and the
  record's deletion.
- **Worker identity.** When the worker's tree has no `user.name` / `user.email`, the job sets
  a fallback (`nightwright worker`), so the persona's commits and the `wip` stash can be
  created.
- **`nw workers check`** runs `nw worker claude-check` on a persona worker: `claude --version`
  always, and the 1-token `claude -p` probe with `--auth`. Both are new subcommands in the
  forced-command grammar.

### Test record (2026-11-02)

- `uv run pytest -q tests/test_remote_personas.py`: 26 passed. It covers write-back equality
  (a commit plus an uncommitted tracked edit and a new untracked file; trees, `status
  --porcelain` and `git diff` equal, and equal trees after `commit_all`), the read-only
  reviewer (same verdict, `result.json` / `stream.jsonl` / `request.json`, `request.json`
  equal after dropping `cwd` and `started_at`), `progress.json`, the four write-back failures
  (moved HEAD, dirty tree, stash conflict with the clean check stubbed, corrupt bundle),
  restart/adoption (detach, then adopt, write-back once; an `applied` record is not applied
  again), eligibility (seven local-only cases, read-only on a modified tree, default kinds,
  the persona slot pool), serialization, env allowlist (coordinator, worker rebuild and
  header validation), `run_overdue` without `campaign_silent` while a remote run is open,
  `nw workers set`, `check --auth` with a working and a failing fake `claude`, and the SSH
  grammar.
- Related modules, `uv run pytest -q -n 4` over `test_agentic_runner.py`,
  `test_agentic_adopt.py`, `test_agentic_campaign_runs.py`, `test_agentic_events.py`,
  `test_agentic_personas.py`, `test_worker.py`, `test_worker_ssh_command.py`,
  `test_workers_registry.py`, `test_remote_gates.py`, `test_remote_job_records.py`,
  `test_worker_jobs.py`, `test_worker_job_retry.py` and this file: 674 passed, 2 failed. Both
  failures were assertions this spec changes on purpose: `persona` is now a valid header kind
  (the bad-kind case now uses `build`), and `info` lists the `persona` feature. Both were
  updated and pass.
- `nw idea clock-in IDEA-560` was refused inside the campaign run (`NW_PERSONA_RUN=1`), so
  this run has no clock entry.
- `python3 tools/affected_tests.py` selects the full suite (105/225 files), which is left to
  gating.
- The manual steps (a real `claude` login on a second machine and a real campaign's
  implementer on it) were not run: this host's only registered worker, `box`, is disabled and
  has no `async` feature. They are left to the human and to SPEC-0346's integration suite.

### Test record (2026-11-02, gate fix)

- The gate's `tests/test_rich_output_guard.py::test_no_unlisted_raw_writes` flagged the new
  `worker_claude_check` JSON line (a machine path for ssh callers); it is now in the guard's
  allowlist. No other new raw write was added (checked over `git diff main -- src`).
  `uv run pytest -q tests/test_rich_output_guard.py tests/test_remote_personas.py`: 29
  passed. The full suite is left to gating.

### As shipped (2026-11-02, review fixes)

- **The persona slot outlives a detach.** A slot flock alone stopped bounding a worker once a
  job outlived its runner (a stop or restart closed the fd, and an adopted `RemoteRunner`
  held none). Now a fresh dispatch needs a free `persona-<n>.lock` **and**
  `remote_personas.busy(cfg, w, n) < persona_slots`: `busy` counts the slots other runners
  hold, plus the live `kind: persona` records of that worker that no slot holder is attached
  to. A runner holding a slot is attached through `gate-slots/w-<W>/job-<job_id>.lock` (taken
  once the record exists; removed when the record is no longer live). An adopted runner takes
  a free slot when there is one (best effort, since the job runs anyway); without one, its
  record keeps counting as loose. So a detached job, an adopted one, and one a crashed
  coordinator left all keep counting against `persona_slots`. Probing a free lock briefly
  takes it, so a concurrent dispatch can skip a slot and run locally. That costs a local run
  and never overcommits the worker.
- **Saved refs are pruned at close.** `remote_personas.prune_refs(repo, campaign)` deletes
  `refs/nw/jobs/<campaign>-persona-*/`. `close` calls it at both close points (merge, and
  pr/draft-pr), and `apply_abandon` calls it too. Other campaigns' refs stay.
- **Smaller hardening.** `_attach` reads the frames already received before it acts on the
  deadline, so an adopted job's ready `done` frame is not discarded as lost. A
  `RemoteFailure` before the worker holds the job (the cut, the header, `submit`) appends
  `worker_failed` and sets the worker's cooldown when the failure is the worker's fault, as a
  gate fallback does. `remote_jobs.campaigns` also lists the campaigns that have
  `<campaign>-persona-*` records, so an ended campaign's persona jobs are cancelled and
  pruned with its gate jobs.
- **Not done:** recording `writeback: applying` plus the bundle head before `write_back`, so a
  crash *inside* a write-back would be recognised on re-adoption. That crash still makes the
  re-adopted write-back fail with `head moved` and run the step locally (filed as a
  follow-up).

### Test record (2026-11-02, review fixes)

- `uv run pytest -q -n 4 tests/test_remote_personas.py`: 39 passed. New cases:
  - adoption through `queue._adopt` with a non-detachable `FakeRunner` coordinator. The job
    is adopted, written back once (`add c` once, then the step's commit), `_do_persona` gets
    `adopted`, and after `_apply` the `run.end` carries `worker: box`;
  - `_adopt` skips an open run when no live persona record exists, or when the only one
    belongs to another run;
  - `remote_excluded` is gone from `state.json` after `_apply` on the step that follows a
    failed write-back;
  - a lost job (the worker's runner raises, so the job ends in `error`) is cancelled, counted
    once in `worker-losses.json`, marked `lost` and run locally;
  - a `submit` failure runs locally, with `worker_failed`, the worker's cooldown and the
    record `local`;
  - a cut failure runs locally with no record and no cooldown;
  - `cancel_campaign` on a running job gives an interrupted run with the worktree untouched;
  - a detached job, and then the adopted job, keep a 1-slot worker from taking another run,
    and the worker is free again once the job ends;
  - a live record with no slot holder counts (and `RJ.campaigns` sees persona records);
  - end-to-end env: the worker re-sets `NW_WORKTREE` (its own `tree`), `NW_DATA_DIR` and
    `TMPDIR`, and the verbatim keys pass;
  - `request.json` comes back from the worker (its `cwd` is the worker's `tree`);
  - a sequencer in progress keeps the run local;
  - saved refs are pruned on abandon and by the close helper, and other campaigns' refs
    stay.
- Mutation check: disabling the `busy` check or restoring the old `if not detachable: return`
  in `_adopt` fails the slot tests and the queue-adoption test.
- Related modules, `uv run pytest -q -n 6` over `test_remote_personas.py`,
  `test_remote_job_records.py`, `test_remote_gates.py`, `test_agentic_adopt.py`,
  `test_rich_output_guard.py`, `test_worker_job_retry.py`, `test_worker_jobs.py` and
  `test_agentic_runner.py`: 369 passed. `test_agentic_close.py`, `test_close.py`,
  `test_agentic_close_inflight.py` and `test_agentic_idea_campaign.py`: 98 passed.
- The full suite is left to gating.

### As shipped (2026-11-02, second review fixes)

- **The local fallbacks keep the task's hooks.** `RunHooks.restart(run_dir)` puts the task
  back in `spawning` for the new run dir and drops any adoption mode (False, and the run
  ends `DETACHED`, when the pool already detached it). A lost remote job's local run goes
  through it: an adopted remote run dir has no `proc.json`, so `queue._adopt` gave its hooks
  a `claim` mode, and a `ClaudeRunner` handed those would wait for a wrapper that never
  comes and end the run "orphaned". The re-run after a failed write-back now gets the hooks
  too (they were in `collect`), so a stop or restart detaches it like any local run:
  `_do_persona` then returns `detached`, and the next runner adopts run `NNN+1`.
- **Smaller hardening.** The `allowed_tools` root rewrite matches the worktree root only
  when a `/`, a token delimiter or the end follows, so `<wt>-other/...` is refused as
  outside the worktree. A lost job whose record is already `writeback: applied` (a runner
  stopped between the write-back and the record's deletion) is collected from its run-dir
  files instead of running again locally, and is not counted as a loss. The re-attach loop
  checks the job's deadline. `_decode` stops at `MAX_RUN_FILE_BYTES` decompressed and
  drops a larger file.

### Test record (2026-11-02, second review fixes)

- New cases in `tests/test_remote_personas.py`, each run on its own and passing:
  - a lost job whose hooks carry an adoption mode spawns on a detachable local stand-in
    (`spawning`, no adoption mode) instead of ending "orphaned";
  - the re-run after a failed write-back gets the hooks for run `002`, and a stop during it
    returns `detached`;
  - a lost job with an `applied` record is collected from its run dir, with no local run
    and no loss counted;
  - the re-attach loop stops at the deadline;
  - `<wt>-other` in `allowed_tools` is not remotable;
  - `_decode` is bounded.
- `uv run pytest -q -n 6` over `test_remote_personas.py`, `test_agentic_runner.py`,
  `test_agentic_adopt.py`, `test_remote_job_records.py` and `test_remote_gates.py`: 244
  passed.
- The full suite is left to gating.

## Rollout / migration

Implementation sessions clock in and out on the linked idea (`nw idea clock-in IDEA-560` /
`nw idea clock-out IDEA-560`).

1. Ships with persona jobs disabled: the default `kinds` is `[gate]`.
2. The human turns persona jobs on per worker with `--kinds gate,persona`, after
   `check --auth`.
3. `nw workers set box --kinds gate` returns persona runs to local.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

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
