---
id: SPEC-0344
title: "Remote persona jobs with bundle write-back"
status: accepted
owner: user
created: 2026-11-01
updated: 2026-11-01
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
- A persona run can execute on a worker and produce the same `RunResult` and run-dir files.
  For a writing persona, it also produces the same commits on the campaign branch as a local
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
  write-back starts from exactly `base_head`.

(The global persona cap and the rate-limit backoff are added to this list by SPEC-0345.)

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
  - `wip`: `git stash create` of what is left uncommitted, or null;
  - `bundle_b64`: the bundle bytes, with the job's input commit as the base.
- The run record (`write_run_record`) gains `worker` (null when local), and the `run` and
  `run_failed` events carry `worker` when the run was remote.

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
   connection died) follows SPEC-0342's lost-job path instead.
5. A read-only persona has no write-back. Only the verdict and the files come back.

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
      uncommitted changes) to the same `FakeRunner` script run locally.
- [ ] A read-only persona run on a worker returns the same verdict and run-dir files as a
      local run.
- [ ] A write-back failure leaves the worktree untouched and becomes a transient
      `run_failed`, and the run is retried locally.
- [ ] A failed write-back keeps the fetched refs as `saved_refs` in the run record.
- [ ] A persona with an `add_dirs` entry outside the worktree, with an untracked file in the
      worktree, or (if writing) with a modified tracked file, runs locally.
- [ ] A remote persona run writes the same `run.start`/`run.end` ledger entries as a local
      one, so `run_overdue` fires past `timeout_s` and `campaign_silent` stays quiet while
      it is open; `progress` frames land in `progress.json`.
- [ ] `runner.request_to_job` / `request_from_job` round-trip a `RunRequest`. An env key
      outside the allowlist is refused; stripped keys are re-set by the worker.
- [ ] The run record, and the `run` / `run_failed` events, carry `worker` for a remote run.
- [ ] `nw workers set --kinds/--persona-slots` updates the record, and `nw workers check
      --auth` passes with a working (fake) `claude` and fails a persona worker without one.
- [ ] With no persona-capable worker (the default `kinds` is `[gate]`), persona runs are
      unchanged.

## Test plan

- **Automated tests** (`tests/test_remote_personas.py`, offline): the loopback workers
  (SPEC-0338 and SPEC-0340), with the worker configured for `FakeRunner` (`agentic.runner:
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

None. Decided while splitting SPEC-0333:
- The worker uses its own `claude` login.
- Write-back is a fast-forward plus a stash apply, with a moved HEAD treated as a failure.
- Persona jobs are opt-in per worker.
- A writing persona goes remote only from a clean worktree.
- The env is an allowlist mirroring `personas.py`; worker-local paths are re-set by the
  worker.
- A write-back failure retries locally only and keeps its refs for inspection.
