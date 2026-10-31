---
id: SPEC-0333
title: "Remote persona runs with bundle write-back"
status: accepted
owner: user
created: 2026-10-31
updated: 2026-10-31
source_idea: IDEA-560
parent: SPEC-0310
tags: [agentic, remote, workers, personas, cost]
depends_on: [SPEC-0332]
---

## Context

Persona runs (implementer, triage, reviewer, ...) are the other half of campaign time.
Today each one is a `RunRequest` (`src/nightwright/agentic/runner.py:33`). `ClaudeRunner`
turns it into a `claude -p` argv with `build_argv` (`runner.py:153`) and runs it through the
detached wrapper (`runproc.py`). The result lands in the run dir: `request.json`,
`result.json`, the transcript, and `cost_usd`. A writing persona changes the campaign
worktree, and the queue then commits them (`git.commit_all(worktree, msg)` in
`queue._do_persona`, `queue.py:1834`).

The queue's slot count (`slots_for`, from the profile) caps how many campaigns are active,
so it also caps concurrent persona runs on one host.

[SPEC-0332](./0332-worker-owned-async-jobs.md) provides durable, re-attachable remote jobs.
This spec adds persona jobs on top of them.

## Goals / Non-goals

**Goals**
- A persona run can execute on a worker and produce the same `RunResult` and run-dir files.
  For a writing persona, it also produces the same commits on the campaign branch as a local
  run.
- One campaign's runs stick to one worker while it is healthy (**affinity**), so the
  worker's object cache and tree stay warm.
- A host-independent cap on concurrent persona runs (`agentic.workers.max_personas`), so
  adding workers does not overrun the account's rate limit. On a rate-limit failure,
  dispatch backs off globally.
- Cost is attributed per worker in run records, `nw campaign show`, the campaign export and
  budgets.
- The epic's integration test file, `tests/test_remote_workers_integration.py`.

**Non-goals**
- Coordinator-side `claude` credentials on the worker. The worker uses its own `claude`
  login, and `nw workers check` verifies it.
- Running the conductor or stewards on workers
  ([SPEC-0335](./0335-federation-over-a-networked-mailbox.md)).
- Moving a running persona from one worker to another.

## Decision

Add job kind **`persona`**. The coordinator serializes the `RunRequest` with a new explicit
`runner.request_to_job(req, worktree)` (paths made relative to the worktree), and the worker
rebuilds it with `runner.request_from_job(doc, tree, run_dir)`. The worker runs it with the
runner chosen by `runner.select_runner(<worker cfg>)` (its own `ClaudeRunner`, or
`FakeRunner` under `agentic.runner: fake`) inside the SPEC-0332 job process.

At the end of the run, the worker sends back a **result bundle**: the new commits, plus a
`git stash create` commit for uncommitted changes. The coordinator applies it to the campaign
worktree with a fast-forward and `git stash apply`. The worktree ends up exactly as if the
persona had run locally, and the normal `git.commit_all` step in `queue._do_persona` takes
over from there.

## Design

### Eligibility

A run goes remote only when **all** of these hold:
- an enabled worker with `kinds` containing `persona` has a free persona slot;
- every `add_dirs` entry is inside the worktree (personas that read the data dir or another
  repo stay local);
- the worktree has no untracked, non-ignored files (the SPEC-0338 rule);
- for a writing persona, the worktree is **clean** (no tracked modifications either), so
  write-back starts from exactly `base_head`;
- the global persona cap and the rate-limit backoff allow it.

`kinds` is set with `nw workers add|set NAME --kinds gate,persona`; the default stays
`gate`. Persona slots are a separate pool, `gate-slots/w-<W>/persona-<n>.lock`, sized by
`--persona-slots` (default 1).

### The job (`kind: "persona"`)

The payload is:

```
{run_id, persona, request: <runner.request_to_job(req, worktree)>, json_schema, read_only,
 timeout_s, max_budget_usd}
```

- `request_to_job` / `request_from_job` are explicit field-by-field serializers (not the
  run dir's `request.json`), with a round-trip test.
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
     a long time, and the job process is already detached and leased (SPEC-0332).
- **Progress frames.** This spec adds the `progress` frame type to `nw.job.frame.v1`: every
  30 s the job emits `progress {ts, transcript_bytes, last_line_ts}`. The coordinator
  records the latest one in the job's `remote-jobs/` record (SPEC-0332 liveness) and mirrors
  it into the local run dir as `progress.json`, for `nw campaign show` and the dashboard.
  The detectors need nothing new: the coordinator writes the same `run.start` and `run.end`
  ledger entries a local run writes, so `run_overdue` (age of the open `run.start` against
  `timeout_s`, `events.py:785`) and `campaign_silent` (skipped while a run is open) behave
  exactly as for a local run.
- The `done` frame carries the `RunResult` fields, plus the run-dir files (`result.json`,
  the transcript, `request.json`), gzip+base64 and capped at 16 MiB each. For a
  non-read-only persona it also carries the **result bundle**:
  - `head`: the tree's HEAD after the run;
  - `wip`: `git stash create` of what is left uncommitted, or null;
  - `bundle_b64`: the bundle bytes, with the job's input commit as the base.

### Write-back (coordinator)

1. Before it submits a writing persona job, the coordinator records `base_head` (the
   worktree HEAD). The worktree is clean (Eligibility).
2. On `done`:
   - fetch the bundle into the campaign repo under `refs/nw/jobs/<job_id>/{head,wip}`;
   - check that the worktree HEAD is still `base_head`. Affinity and the one-step-per-campaign
     rule make a moved HEAD a bug, so it is detected rather than merged.
   - `git merge --ff-only <head>` in the worktree, then `git stash apply <wip>` if `wip` is
     set;
   - delete the refs.
3. A failure at any point (fetch, a moved HEAD, a non-ff, a stash conflict) leaves the
   worktree untouched. It is reported as `run_failed` with `transient: true` and
   `error: "write-back: <reason>"`, so the existing retry path re-runs the persona, locally
   this time, because the worker is excluded for that run. The fetched refs stay under
   `refs/nw/jobs/<job_id>/` and are listed in the run record as `saved_refs` (so the work
   can be inspected); they are pruned when the campaign closes.
4. Retry rule: a write-back failure retries **locally only**. A lost job (the worker or
   connection died) follows SPEC-0332's lost-job path instead.
5. A read-only persona has no write-back. Only the verdict and the files come back.

### Affinity, global cap, backoff

- **Affinity.** Campaign state gains `remote_affinity: <worker>|null`, set by the first
  remote persona run. Later persona runs and gate runs for the campaign prefer that worker's
  pools and fall through to others only when it is full or failing. Affinity is cleared on
  `worker_job_lost`.
- **Global cap.** `agentic.workers.max_personas` (default: the local queue slots) bounds
  live persona runs across local and all workers. Live runs are counted from local live run
  dirs plus `remote-jobs/` records with `kind: persona` that are not final.
- **Rate limit.** A new `events.RATE_LIMIT_MARKERS = ("rate limit", "429", "overloaded",
  "529")`, a subset of `events._TRANSIENT_MARKERS` (`events.py:89`). A run failure whose
  error matches one sets `<agentic_dir>/rate-limit.json {until, backoff_s}`, written
  atomically. `usage_limit` and `session limit` keep their existing handling. The backoff
  starts at 60 s and doubles up to 15 min; a successful run resets it. No new persona job is dispatched, local or remote,
  before `until`. A `rate_limited {until, backoff_s}` queue event is appended once per
  backoff.

### Cost per worker

- `RunResult` is unchanged.
- The run record (`write_run_record`) gains `worker` (null when local).
- `campaign_export` and `nw campaign show` gain `cost_by_worker`.
- Budgets (`max_budget_usd` per run and the campaign caps) count remote costs exactly like
  local ones, because the cost comes from the same `claude` result.
- The `run` and `run_failed` events carry `worker` when the run was remote.

### `nw workers check`

`check` adds: `claude --version` on the worker, and a 1-token `claude -p` auth probe, run
only with `--auth` because it costs money. A worker with `kinds` containing `persona` and no
working `claude` fails the check. This needs one more allowed subcommand on the worker,
`nw worker claude-check [--auth]`, added to the SPEC-0336 `ssh-command` grammar.

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
- **Per-worker rate limits.** The limit belongs to the account, not to the host, so the cap
  and the backoff are global.

## Acceptance criteria

- [ ] A writing persona run on a worker leaves the campaign worktree identical (HEAD, tree,
      uncommitted changes) to the same `FakeRunner` script run locally.
- [ ] A read-only persona run on a worker returns the same verdict and run-dir files as a
      local run.
- [ ] A write-back failure leaves the worktree untouched and becomes a transient
      `run_failed`, and the run is retried locally.
- [ ] A persona with an `add_dirs` entry outside the worktree, or with an untracked file in
      the worktree, runs locally.
- [ ] Affinity: three consecutive runs of one campaign land on the same worker while it has
      slots.
- [ ] The global persona cap holds across local and remote runs. A rate-limit failure stops
      all persona dispatch until the backoff ends.
- [ ] A remote persona run writes the same `run.start`/`run.end` ledger entries as a local
      one, so `run_overdue` fires past `timeout_s` and `campaign_silent` stays quiet while
      it is open; `progress` frames land in `progress.json`.
- [ ] Run records, `nw campaign show`, the export and budgets attribute cost per worker.
- [ ] `nw workers set --kinds/--persona-slots` updates the record, and `nw workers check
      --auth` passes with a working (fake) `claude` and fails a persona worker without one.
- [ ] A failed write-back keeps the fetched refs as `saved_refs` in the run record.
- [ ] `runner.request_to_job` / `request_from_job` round-trip a `RunRequest`.
- [ ] `tests/test_remote_workers_integration.py` covers the epic's acceptance criteria that
      concern gate and persona workers and passes; the pull-worker and signing cases are
      added by SPEC-0334.

## Test plan

- **Automated tests** (`tests/test_remote_personas.py`, offline): the loopback workers
  (SPEC-0338 and SPEC-0332), with the worker configured for `FakeRunner` (`agentic.runner:
  fake`). The cases:
  - **write-back equality:** a fake implementer script commits one file and leaves one
    uncommitted. The remote and local outcomes compare equal (commit tree hashes and the
    `git diff` of the worktree);
  - **read-only reviewer:** the verdict and the files are equal;
  - **write-back failures:** HEAD moved, a stash conflict and a corrupt bundle each give a
    transient `run_failed`, an untouched worktree and a local retry;
  - **eligibility:** `add_dirs` outside the tree, an untracked file, and a writing persona
    with a modified tracked file;
  - **serialization:** `request_to_job` / `request_from_job` round-trip;
  - **env:** an allowed key passes, a stripped key is re-set by the worker, another key is
    refused;
  - **saved refs:** a failed write-back lists `saved_refs` and the refs exist;
  - **workers set / check --auth:** `CliRunner` with a fake `claude` script on the worker's
    PATH (working, and failing);
  - **affinity:** placement across three runs, and affinity cleared on loss;
  - **global cap:** with `max_personas: 2` and two workers plus local, only 2 run at once;
  - **rate limit:** a fake 429 error sets the backoff, nothing dispatches before `until`, a
    success resets it, and one `rate_limited` event is appended; a `usage_limit` error does
    not set it;
  - **progress and detectors:** `progress.json` updates from frames; with a short
    `timeout_s` an open remote run fires `run_overdue`, and `campaign_silent` does not fire
    while it is open;
  - **cost:** `worker` in the run record, `cost_by_worker` in the export, budget caps
    counted.
- **Integration tests** (`tests/test_remote_workers_integration.py`, the epic's file). A
  whole fake campaign runs through the queue with two loopback workers, from admission to
  merge. The cases:
  - no workers: a golden ledger comparison;
  - with workers: same final branch and the same event types, plus `worker` keys;
  - a worker killed mid-run;
  - a coordinator restart mid-job;
  - no git credentials on the worker side (the env is checked).
- **Manual verification:**
  1. Log `claude` in on the second machine and run `nw workers check box --auth`.
  2. Set `--kinds gate,persona`.
  3. Let one real campaign run its implementer on `box`. The merged result must be correct
     and `nw campaign show` must show `cost_by_worker`. Check the write-back: `git log -p
     base_head..HEAD` in the campaign worktree equals the worker branch `nw/job/<job_id>`
     (from the job's `frames.jsonl`), and every file the transcript shows being edited
     appears in that diff.
- **Regression guard:** the full `uv run pytest`. The persona-run and queue tests run
  unchanged with no workers.

## Clock Log

Use `nw idea clock-in IDEA-560` / `nw idea clock-out IDEA-560`.

## Rollout / migration

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

None. Decided while writing this spec:
- The worker uses its own `claude` login.
- Write-back is a fast-forward plus a stash apply, with a moved HEAD treated as a failure.
- The persona cap and the rate-limit backoff are global, with a 60 s to 15 min backoff.
- Persona jobs are opt-in per worker.
- A writing persona goes remote only from a clean worktree.
- The env is an allowlist mirroring `personas.py`; worker-local paths are re-set by the
  worker.
- A write-back failure retries locally only and keeps its refs for inspection.
