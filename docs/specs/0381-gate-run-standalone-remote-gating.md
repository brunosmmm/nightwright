---
id: SPEC-0381
title: "nw gate run: standalone remote gating of a worktree or branch without a campaign"
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, gates, workers, cli]
depends_on: [SPEC-0338, SPEC-0374, SPEC-0379]
source_idea: IDEA-791
---

## Context

Today, the worker box's gate slots are reachable only through a queue campaign
([SPEC-0338](./0338-remote-gate-dispatch-with-local-fallback.md)) or the base-branch sentinel
([SPEC-0379](./0379-time-based-sentinel-on-worker-slots.md)). Some callers want an answer to "are my gates
green on this tree?" without opening a campaign:

- an agent in a hand-run worktree
- a human before pushing
- a steward checking a branch

Today they run `uv run pytest` on the host. That competes with the runner for the CPU, which is
exactly what the worker box exists to avoid. IDEA-791 asks for a campaign-less entry point to the
same transport.

## Goals / Non-goals

**Goals**

- `nw gate run` gates the commit under test of a worktree (HEAD, or `git stash create` of a dirty
  tree) or a branch tip on a worker box. It uses the SPEC-0338 cut/bundle/dispatch path.
- The job gets bookkeeping without a campaign:
  - a job id and a log dir
  - a gate-slot holder that names the job
  - an event
  - a row in the queue dashboard and in `nw queue gate-slots`
  - `status`, `logs`, `cancel` and `list` commands
  - retention
- Fair sharing:
  - campaigns win slot contention
  - per-requester and total caps
  - a feature switch
- Distinct exit codes and a JSON result schema.
- Idempotence: the same tree, project, base and gates attach to the running job.

**Non-goals**

- A local fallback. Standalone jobs only ever run on worker pools.
- Preempting a running job, or touching campaigns, the ledger or merges.
- Async `submit`/`attach` jobs (SPEC-0341). A standalone job uses the synchronous `gate-job`,
  like the sentinel.

## Decision

Add a new `nw gate` command group (`agentic/cli_gate.py`) backed by `agentic/gate_jobs.py`. A job
works like a small campaign-shaped state, the same one the sentinel uses:

- state: `{id: GATEJOB-NNNN, project, spec_id: None, repo: {worktree, base, commit}}`
- it runs `remote_gates.run_detached` under `gate_slots.acquire` on worker pools only
- the hold file carries the job's holder metadata

Caps are flock tokens taken before the slot, so they are atomic and die with the process. Campaign
priority is a `skip` predicate: a worker pool is passed over while any campaign or sentinel gate
waits for a slot.

## Design

### Command surface

```
nw gate run [--project P] [--worktree PATH | --branch B] [--base REF] [--tier loop|close|all]
            [--cmd CMD ...] [--by WHO] [--wait | --detach] [--wait-s S] [--json]
nw gate status ID [--json]      nw gate logs ID [GATE]      nw gate cancel ID
nw gate list [--json]
```

`gate` is registered in `agentic/cli.py` like `workers` and `herdr`. Shell completion is click's
dynamic completion, so registering the group is enough. A hidden `nw gate _exec ID` runs a
`--detach`ed job in a child process (`python -m nightwright gate _exec ID`, `start_new_session`).

### Target and project

- **Worktree mode** (`--worktree`, default the cwd):
  - The commit under test is `remote_gates.commit_under_test`: HEAD, or the `git stash create`
    commit of a dirty tree.
  - Untracked, non-ignored files fail the run with exit 2 and an instruction to `git add` them,
    because the bundle carries commits only and a green result without them would be a lie.
- **Branch mode** (`--branch B`): the commit is `rev-parse B^{commit}` in the project's root
  (or the cwd's repo when there is no project). The untracked check does not apply.
- `--project` defaults to `projects.find_project_for_cwd` of the worktree. The gates come from
  `P.gates_for(pcfg, tier)`:
  - The default tier is `loop`: spec-lint plus the affected tests.
  - `close` runs the close tier without a paths skip or reuse, as the sentinel does.
  - `all` runs every gate.
  - Gates with `remote: false` are dropped and named on stderr, since nothing runs locally.
  - A project with `remote_gates: false` is refused with exit 2.
- `--cmd CMD` (repeatable) replaces the project's gates with ad-hoc gates
  `{name: cmd-N, cmd: CMD, results: {format: pytest}}`. It works without a registered project.
  With no project and no `--cmd`, the run fails with exit 2 and this message: "no registered
  project with .nw/project.yaml gates; pass --project or --cmd".
- **Base:** `--base`, else the project's `merge.base` / `base`, else `main`.
  - The worker gets `base: {name, sha}` and `NW_BASE = name`, as campaigns do.
  - `remote_gates.cut` now takes the merge-base from the commit under test instead of `HEAD`.
    For a worktree the two give the same merge-base. For a branch it is the branch tip's
    merge-base, so `tools/affected_tests.py` diffs the right range.
- `cut` reads an optional `repo.commit` override (branch mode, and worktree mode, which computes
  the commit once for the idempotence key). Campaign states never set it.
- The worker's `validate_header` already accepts this header with no new worker code:
  - `campaign: GATEJOB-NNNN` matches its key pattern
  - `spec_id: "-"`
  - the env is the allowlisted `NW_CAMPAIGN`, `NW_SPEC_ID` and `NW_BASE`
- A gate that writes files in the worker's checkout (spec-lint's `--write-ledger`) does not fail
  a standalone job. Campaigns re-run locally in that case, but standalone jobs have no local run.
  `run_detached(..., keep_changed=True)` keeps such a run's verdict, and `changed` is recorded
  in the result.

### Config (`agentic.gate_jobs`)

| key | default | meaning |
|---|---|---|
| `enabled` | `true` | `false`: `nw gate run` exits 2 "standalone gate jobs are disabled" |
| `max_per_requester` | `2` | standalone jobs one requester may have waiting or running at once |
| `max_slots` | `max(1, placeable worker gate slots // 2)` | standalone jobs in flight box-wide |
| `retention_days` | `7` | finished job dirs older than this are deleted at the next `nw gate run` |

A bad value (a bool, a non-positive number, a non-number) falls back to its default.

### Placement, priority and caps

1. If `workers.gate_pools(cfg)` has no worker pool, the run exits 3 "box unavailable".
   `nw gate run` filters out the local pool (`None`), so `local_overflow` never applies.
2. **Requester token:** a flock on `gatejobs/tokens/req-<sha256(requester)[:12]>/slot-k.lock`
   for some `k < max_per_requester`.
3. **Share token:** a flock on `gatejobs/tokens/share/slot-k.lock` for some `k < max_slots`.
4. **The slot:** `gate_slots.acquire(cfg, job_id, pools=<worker pools>, repool=<worker pools>,
   skip=campaign_first, holder={job, by, project, branch}, stop=<wait stop>)`.
   `campaign_first(pool)` is true while any live waiter marker belongs to a non-`GATEJOB-` owner
   (a campaign or the sentinel). A standalone job therefore takes a worker slot only when no
   campaign gate is waiting for one.
   - The rule is deliberately conservative: a campaign waiting only for the local pool also
     defers jobs. Waiter markers do not record their pools, and the case is rare (an ineligible
     campaign while workers are up).
5. Steps 2 to 4 share one deadline, `--wait-s` (default 300 s), from the start of the wait. When
   the deadline passes, the wait stops and the job ends `unavailable` with exit 3. A cancel during
   the wait ends it `cancelled` with exit 130.
6. Once the slot is held, the wait stop no longer applies. Only a cancel or a signal stops the
   run. Running jobs are never preempted. The caps bound how many slots jobs can occupy.

### Job record, ids, lifecycle

- Everything lives under `<agentic>/gatejobs/`.
  - A counter file and a flock `.lock` give sequential ids `GATEJOB-0001`, `GATEJOB-0002`, ...
  - Each job has `<id>/job.json` (atomic writes) and `<id>/gates/`, which holds the gate logs as
    the worker sends them.
- `job.json` holds:
  - identity: `schema: nw.gatejob.record.v1`, `id`, `key`, `pid`
  - status: `status`, `exit`
  - target: `project`, `requester`, `branch`, `worktree`, `commit`, `tree`, `base`, `tier`,
    `gates` (names)
  - timing: `created_at`, `started_at`, `finished_at`, `waited_s`
  - result: `worker`, `result` (per gate), `failing`, `changed`, `error`
- Status flow: `queued`, then `waiting` (tokens and slot), then `running` (slot held,
  dispatched). It ends in one of:
  - `passed` (exit 0)
  - `failed` (exit 1)
  - `unavailable` (exit 3)
  - `transport_error` (exit 4)
  - `cancelled` (exit 130)
  - `error` (exit 2: a precondition found after the record was made, e.g. a header the worker
    would refuse)
- A non-final record whose pid is dead reads as `lost`. `nw gate status` and `nw gate list`
  report it that way, and the record is not rewritten.
- `branch` is `--branch`, else the worktree's current branch (`HEAD` when detached).
- **Idempotence:** `key = sha256(repo root, project, tree under test, base, gates JSON)[:16]`.
  The tree is used, not the commit, because a dirty tree's stash commit is new on every call.
  Under the `.lock`, a non-final job with the same key and a live pid is returned instead of a
  new one. The output's `reused: true` says so.
  - `--wait` follows that job until it finishes and exits with its code.
  - `--detach` prints its id.
- **Cancel:** `nw gate cancel ID` sends SIGTERM to the job's pid. The job's process sets its stop
  event: a wait ends at once, and a dispatched run sends the worker its cancel (SPEC-0229 path).
  The job ends `cancelled`. Ctrl-C on a `--wait` run does the same.
- **Retention:** each `nw gate run` first deletes job dirs whose status is final (or `lost`) and
  whose `finished_at`, or `created_at` when that is missing, is older than `retention_days`.

### Events and dashboard

- **Event `gate_job`** (required keys `job_id`, `status`; plus `by`, `project`, `branch`,
  `worker`, `exit`): one when the job is dispatched (`running`) and one when it finishes. The
  conductor's quiet stream holds it (`quiet.RULES`, mirrored in the nw-conduct skill's
  "Always held" list). A job is information for the conductor, never a decision.
- **Holder metadata:**
  - `gate_slots.acquire(..., holder=dict)` adds the holder's keys to the hold file and the
    waiter marker.
  - `_probe_held` and `occupancy()` return `job`, `by`, `project` and `branch` when they are
    present.
  - Campaign holds are unchanged.
- **`nw queue watch`:** `_gate_pools_view` gains `gate_jobs: [{job, by, project, branch,
  where}]` (`where` is a worker or `waiting`). The workers line renders each one after the
  campaigns as `⚙ GATEJOB-0003 by X`, with the pool marker.
- **`nw queue gate-slots`:** worker-pool holds by a gate job are listed as `holds` rows, with slot
  `W:n` and campaign `GATEJOB-0003 (by X, P@branch)`. Job waiters show in the existing `waits`
  rows. The JSON form carries the holder keys.

### Output: `nw.gatejob.v1`

```json
{"schema": "nw.gatejob.v1", "id": "GATEJOB-0003", "status": "failed", "exit": 1,
 "gates": [{"name": "pytest-affected", "ok": false, "required": true, "exit_code": 1,
            "duration_s": 41.2, "log": "/…/gatejobs/GATEJOB-0003/gates/001-pytest-affected.log",
            "summary": {"format": "pytest", "passed": 10, "failed": 1, "errors": 0,
                        "skipped": 0, "failing": ["tests/test_x.py::test_y"]}}],
 "failing": ["tests/test_x.py::test_y"], "log_dir": "/…/gatejobs/GATEJOB-0003/gates",
 "worker": "box", "duration": 47.9, "project": "Meta-Tools", "branch": "feat/x",
 "commit": "…", "requester": "agent:foo", "reused": false, "error": null}
```

`failing` is the failing test ids of the required red gates when their results are parsed, else
those gates' names, as the sentinel computes it. Text output prints one line per gate, then
`passed`, `FAILED: <ids>` with the log dir, or the error.

### Exit codes

| code | meaning |
|---|---|
| 0 | every required gate passed |
| 1 | a required gate is red (failing ids and the log dir are printed) |
| 2 | usage or precondition error: feature disabled, no project or gates, untracked files, `remote_gates: false`, a header the worker would refuse |
| 3 | box unavailable: no placeable worker, or no token or slot within `--wait-s` |
| 4 | transport failure (`RemoteFailure`: ssh, upload, protocol, worker error) |
| 130 | cancelled (`nw gate cancel`, Ctrl-C, SIGTERM) |

`--detach` exits 0 once the job is recorded and its child has started, or 2 or 3 when the job
cannot start. It prints the id, or `nw.gatejob.v1` with `status: queued`.

## Alternatives considered

- **Count held slots instead of flock tokens for the caps:** two jobs checking at the same
  moment could both pass. Tokens are atomic and die with the process.
- **Hold a priority lock that campaigns take:** that would change the campaign path. The `skip`
  predicate leaves campaigns untouched.
- **Use the commit sha as the idempotence key:** a dirty tree's stash commit is new on every
  call, so the key would never match. The tree sha is stable.
- **Fail a run whose gates wrote files in the worker's checkout (the campaign "tree" fallback):**
  there is no local run to fall back to, and spec-lint's ledger write would fail every stale
  worktree.

## Acceptance criteria

- [x] `nw gate run` with a stubbed worker dispatch returns exit 0 for a green run and exit 1 for
      a red one. The red run prints the failing ids and the log dir. `--json` emits
      `nw.gatejob.v1` with `id, status, exit, gates, failing, log_dir, worker, duration`.
- [x] No placeable worker gives exit 3 "box unavailable", and nothing runs locally. The local
      pool is never offered to `acquire`, even with `local_overflow: true`.
- [x] A `RemoteFailure` from the dispatch gives exit 4.
- [x] A waiter marker from a live campaign makes a job skip a free worker slot until the marker
      is gone. With `--wait-s` elapsed it ends with exit 3.
- [x] `max_per_requester` and `max_slots` are enforced: a job over either cap waits and then
      exits 3 when its `--wait-s` passes.
- [x] A second `nw gate run` for the same tree, project, base and gates while the first is live
      returns the first job's id (`reused: true`) instead of starting another.
- [x] `agentic.gate_jobs.enabled: false` exits 2. A tree with untracked files exits 2. No project
      and no `--cmd` exits 2.
- [x] `--cmd` runs ad-hoc gates named `cmd-N` with `results: {format: pytest}`.
- [x] The hold file and `occupancy()` carry `job`/`by`/`project`/`branch`. `_gate_pools_view`
      lists `gate_jobs`. `nw queue gate-slots` shows the job's hold.
- [x] A `gate_job` event is appended when the job is dispatched and when it ends. `quiet.RULES`
      holds it, and the skill list matches.
- [x] `nw gate status|logs|cancel|list` work on a recorded job. `cancel` stops a waiting job
      (status `cancelled`). Finished jobs older than `retention_days` are removed by the next
      run.
- [x] Campaign paths are unchanged. The existing remote-gates and gate-slots tests pass.

## Test plan

- **Automated tests:** `tests/test_gate_jobs.py`, with a tmp cfg, a seeded worker registry, a
  tmp git repo and `remote_gates.run_detached` monkeypatched to a stub (no SSH):
  - green and red exit codes and the JSON schema
  - no worker gives exit 3, and no local run happens with `local_overflow: true`
  - a stub raising `RemoteFailure` gives exit 4
  - campaign priority: a live campaign waiter marker blocks the job, which ends with exit 3
    after a short `--wait-s`, and the job takes the slot once the marker is gone (threaded, waits
    via `tests/waits.py`)
  - per-requester and global caps via held tokens
  - idempotence: a live record with the same key is returned
  - disabled, untracked-file and no-project exits
  - `--cmd` gate shape
  - holder metadata in the hold file, `occupancy`, `_gate_pools_view` and the gate-slots CLI
  - `gate_job` events
  - status, list and cancel of a waiting job (a real child process)
  - retention GC
  - `cut` with a `repo.commit` override
- **Regression guard:** `tests/test_gate_slots.py`, `tests/test_remote_gates.py` and the
  `tests/test_skills.py` quiet-list check, run with `-n 4`.
- **Manual verification:** if the worker `box` is reachable and its installed nw accepts the
  header, one real `nw gate run --cmd "python3 -c 'print(1)'"` from the worktree, run only when
  no campaign gate is waiting.

## Clock Log

Tracked with `nw idea clock-in/clock-out IDEA-791`.

## Rollout / migration

This is additive. With no `agentic.gate_jobs` config the feature is on with the defaults. The
worker side needs no change. Campaign behavior is unchanged: `cut` computes the same merge-base
for campaign states, and the `holder` and `keep_changed` arguments default off.

## As shipped

- **Code:**
  - `src/nightwright/agentic/gate_jobs.py`: settings, records, resolve, create, execute, payload, gc.
  - `src/nightwright/agentic/cli_gate.py`: the `nw gate` group (`run`, `status`, `logs`, `cancel`,
    `list`, hidden `_exec`), registered in `agentic/cli.py` like `workers`.
  - `gate_slots.acquire(..., holder=)`: the holder keys go into the hold file, the waiter marker
    and `occupancy()`.
  - `remote_gates`: `cut` honours `repo.commit`, with a merge-base against that commit;
    `run_detached(..., keep_changed=)`.
  - `events` / `quiet`: a `gate_job` event, held.
  - `queue._gate_pools_view` gains `gate_jobs`; `cli_queue._workers_line` renders them as
    `⚙ GATEJOB-N by X`; `nw queue gate-slots` lists the job's worker hold (`box:n`) and its
    waiter.
  - Docs: a `gate` row in `docs/product/reference/index.md`.
- **Decisions made while building:**
  - `--worktree` and `--branch` are exclusive. Branch mode resolves the branch in the
    project's root, or else in the cwd's repository; untracked files do not matter there.
  - An unknown job id is a usage error (exit 2).
  - A reused job under `--detach` returns at once. Under `--wait` it follows the other
    process's record; if that process died (`lost`), the exit is 4.
  - The detached child is `sys.executable -m nightwright gate _exec ID`. It inherits the
    environment (and so the config) in a new session, and logs to
    `gatejobs/<id>/exec.log`.
- **Automated tests:** `tests/test_gate_jobs.py`, 26 tests with a stubbed `run_detached`.
  - They cover:
    - exit codes 0/1/2/3/4/130 and the JSON
    - local pool exclusion with `local_overflow`
    - campaign priority (a live waiter wins the slot; a live marker holds a job to exit 3)
    - both caps
    - idempotence
    - untracked, dirty-tree and branch modes
    - holder metadata in the views
    - events, status/logs/list, cancel while waiting and while running
    - lost and gc
  - One test spawns a real `--detach` child on a tmp config and SIGTERMs it with `nw gate cancel`.
  - Regression guard, run with `-n 4`: `tests/test_gate_slots.py`, `tests/test_remote_gates.py`,
    `tests/test_cli_gate_slots.py`, `tests/test_docs_product_reference.py`, `tests/test_skills.py`.
  - `test_skills.py::test_conduct_skill_rule_ids` fails on `main` without this change
    (row id `message.request.human_order` fails `RULE_RE`). That failure predates
    this spec; the other tests in the file pass.
  - Unrelated fix: the conduct decision-table row id `message.request.human_order` (cdfcef19) had three
    segments, which `RULE_RE` (SPEC-0301, `<event>[.<qualifier>]`; `a.b.c` is a tested reject) rejects, so
    `nw queue decision --rule` would have failed too; renamed it `message.human_order` in `skills/nw-conduct/SKILL.md`.
  - Not covered by a unit test: the `cut` `repo.commit` override. The real dispatch below
    exercised it (branch mode).
- **Manual verification (real binary):**
  - Command: `nw gate run --branch main --cmd "python3 -c 'print(1)'" --wait-s 60 --json`,
    from this worktree's build against the real worker `box`. The box had 1 of 6 slots held and
    no waiters.
  - Result: `GATEJOB-0001` `passed` on `box` in 1.4s, exit 0, and the gate log contains `1`.
  - The box's installed nw accepted the `GATEJOB-` header unchanged, so the worker side needs
    no update.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed or the skip explained.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- None blocking. Possible later follow-ups: async jobs that survive a coordinator restart, and
  a per-requester fairness order among queued standalone jobs (today it is flock luck).
