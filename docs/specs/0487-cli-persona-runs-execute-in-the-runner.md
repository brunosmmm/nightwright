---
id: SPEC-0487
title: CLI persona runs execute in the runner, which holds the credentials
status: done
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, runner, credentials, spec-review, doctor]
depends_on: [SPEC-0483]
---

## Context

SPEC-0483 made the systemd user unit `nw-runner.service` the runner's supervisor and the runner
the only process that resolves `agentic.credentials` (for example
`CURSOR_API_KEY: pass:cursor/sdk`) into its own `os.environ`. Persona runs the runner spawns
inherit the key. `nw spec review` spawns the `spec_reviewer` persona from the caller's process
(`spec_review.run_spec_review` -> `select_runner(cfg).run(req)`), so from a human shell or an
agent session the cursor wrapper starts without the key and fails with
`CURSOR_API_KEY is not set` (`cursor_exec.execute`, error_code `startup`).

**The human's decision (binding):** the CLI must not resolve `pass` entries itself. The key lives
in the runner's (systemd service) environment, and every agent runs from there. It has to work
on a clean machine reinstall with no hand-edited local files and no manual exports: whatever
setup is needed ships in the repo and is done by the installed unit (`nw queue install`) and
`nw doctor`.

`spec_review.run_spec_review` is the only CLI-reachable call that spawns a persona. The other
`select_runner` callers are the queue tick and the worker's remote persona host, which already
run inside the runner or a worker.

## Goals / Non-goals

**Goals**
- `nw spec review` works from any shell or agent session while the runner is up: the reviewer
  runs as a child of the runner process and inherits its environment.
- With no runner up, a cursor-backed review fails before it spawns, with an error that says what
  to do (`nw queue install` / start `nw-runner.service`, `nw doctor`). Other backends still run
  locally as before.
- `nw doctor` reports the credential status the runner itself has (set / resolved / locked /
  missing) without reading any secret.
- No secret is written, logged or passed through a job file. Job files name the spec, the
  profile and the paths only.

**Non-goals**
- Moving the queue's own persona steps; they already run in the runner.
- A network or socket API. The runner and the CLI share the data dir on one host.
- Running jobs for a runner on another host. A runner whose lease names another host counts as
  "no runner here".

## Decision

Add a file-drop **runner job** inbox at `<data_dir>/agentic/runner-jobs/<job-id>/`. While a
runner holds the lease on this host and the caller is not already a runner job, `nw spec review`
writes `request.json` (kind `spec_review`, spec id, resolved spec path, origin, profile) and
waits. At the top of every tick, and on every pass of the wait loop between ticks, the runner
claims pending requests by renaming `request.json` to `claimed.json` and starts a detached child
`python -m nightwright queue runner-job <dir>` with its own environment, which includes the
credentials it resolved. The child runs the ordinary `run_spec_review` and writes `result.json`
(`{ok, review}` or `{ok: false, error}`). The CLI prints the result as if the review had run
locally. With no runner up, the review runs locally as before. When its backend is cursor and
`CURSOR_API_KEY` is missing from the environment, it stops before it spawns, with an actionable
error. The runner publishes `ensure()`'s `{VAR: status}` in its lease info, and a new doctor
check `agentic.credentials` reads it.

## Design

New module `src/nightwright/agentic/runner_jobs.py`:

- `jobs_dir(cfg)` = `agentic_dir(cfg) / "runner-jobs"`. A job id is
  `<kind>-<SPEC-ID>-<utc stamp>-<pid>-<hex4>`.
- `JOB_ENV = "NW_RUNNER_JOB"` is set in the child's environment. `in_job()` is true there, so a
  CLI call inside a job never delegates again.
- `runner_here(cfg) -> dict | None`: the lease status when a runner holds the lease, is not
  stale and runs on this host, else None. `delegate(cfg)` is `runner_here(cfg)` when not
  `in_job()` and the runner serves jobs (`serves_jobs`: its lease has a `credentials` key,
  even `{}`). A runner that predates this spec never claims, so the CLI does not wait 300 s on
  it. It runs locally, and the cursor no-key error says "restart it" instead.
- `submit(cfg, kind, fields) -> Path` writes `request.json` atomically, with
  `schema: nw.runner.job.v1`, kind, fields, `submitted_at` and `submitter_pid`.
- `cancel(job) -> bool` renames `request.json` to `cancelled.json`. Claim and cancel are both
  `os.rename` of the same file, so exactly one of them wins.
- `service(cfg, *, spawn=None, stop=None) -> list[str]` runs on the runner side and never
  raises. It reaps finished children it started (each `Popen` is polled), then claims each
  pending request. If `stop` is set, it claims nothing. A claimed job is spawned with
  `[sys.executable, "-m", "nightwright", "queue", "runner-job", <dir>]`, `start_new_session`,
  stdin closed, stdout/stderr to `<dir>/job.log` and env `os.environ` plus `NW_RUNNER_JOB`. Its
  pid is written to `<dir>/child.json`. If the spawn fails, the runner writes `result.json` with
  the error. The children outlive a code-change re-exec (same pid) and a unit restart
  (`KillMode=process`). Job dirs whose result is older than `KEEP_S` (7 days) are pruned.
  Each claim is journaled `runner_job_started {job, job_kind, spec_id}`.
- `run_job(cfg, job_dir) -> int` is the child (hidden command `nw queue runner-job DIR`). It
  dispatches on kind (`spec_review` -> `SR.run_spec_review(cfg, path, spec_id, origin=...,
  profile=...)`) and always writes `result.json`. A `ValueError` becomes `{ok: false, error}`.
  Any other exception becomes `{ok: false, error: "<Type>: <msg>"}`.
- `wait(cfg, job, *, claim_timeout_s=CLAIM_TIMEOUT_S, poll_s=0.5, clock, sleep, note)` runs on
  the CLI side. Until it is claimed, the job is cancelled and a `ValueError` is raised if the
  runner lets go of the lease or `claim_timeout_s` (300 s; a long tick can delay the claim) has
  passed. The error says "the runner did not pick up the review" and points to `nw doctor` /
  `nw queue status`. After the claim, `wait` returns `result.json` once it exists. If
  `child.json`'s pid is gone (or a zombie) and there is still no result after a 2 s grace, it
  raises "the runner's job ended without a result (see <dir>/job.log)". A `KeyboardInterrupt`
  while waiting cancels an unclaimed job, or leaves a claimed one running (its review is stored
  anyway), and is re-raised.

`spec_review.run_spec_review`: after the backend kind is known
(`os.environ NW_AGENTIC_RUNNER or spawn_runner or resolve_runner(cfg)`) and before anything is
journaled, a `cursor` kind with no `CURSOR_API_KEY` in `os.environ` raises `ValueError`:

- Inside a runner job: "the runner has no CURSOR_API_KEY (agentic.credentials: <source>); run
  `nw doctor` for its credential status".
- Elsewhere: "CURSOR_API_KEY is not in this process's environment and no runner is up to run
  the reviewer with its credentials: start it (`nw queue install`, or `systemctl --user start
  nw-runner`) and re-run; `nw doctor` shows the runner's credential status".

`cli.spec_review_cmd`: the existing argument checks, the `--show` path and the second-round
`--force` guard are unchanged. Before the reviewer runs, if `RJ.delegate(cfg)`, it prints
`… · via the runner (pid N)`, submits, waits, and then prints or emits JSON from the returned
review exactly as the local path does. An `{ok: false}` result becomes a `ClickException`.

Runner (`queue.py`): `tick()` calls `RJ.service(cfg, stop=stop)` right after `CR.ensure`, and
`lease.update_info(credentials=<ensure result>)` when it has a lease. `run()` publishes the
start-time `ensure` result the same way and calls `RJ.service` on each pass of the wait loop
between ticks (every 0.2 s or less). `lease.LOOP_KEYS` gains `credentials`, so `lease_status`
returns it.

Doctor (`doctor.py`): a new check `agentic.credentials` follows `agentic.runner`:

- skip: `agentic.credentials` is empty.
- warn: no runner is up ("the runner resolves them at start; start it: `nw queue install`").
- ok: every configured var is `set` / `resolved` in the runner.
- fail: a var is `locked` / `missing`, naming var, source and status, with `CR.fix_text`.
- warn: the runner predates this spec (no `credentials` in its lease), with the fix "restart
  it".

`check_cursor_agent` reports ok when the doctor's own process lacks the key but the runner's lease
shows `CURSOR_API_KEY` set / resolved ("the runner holds it; CLI persona runs go through the
runner"). With no runner up, it keeps its SPEC-0483 probe. Doctor never puts the value in its
own environment.

Clean reinstall: `nw queue install` (SPEC-0483) writes the unit, the unit runs the runner, and
the runner resolves the credentials and serves the jobs. Nothing else needs setting up.

## Alternatives considered

- **The CLI resolves `pass` itself** (call `CR.ensure` in `nw spec review`). The human ruled this
  out.
- **Copy the runner's environment** (`/proc/<pid>/environ`, `systemctl --user
  set-environment`). Both copy the secret into another process or into the user manager's
  environment for every unit. They break "the key lives in the runner".
- **Run the job on a runner thread.** A re-exec or a stop would kill the review halfway, and the
  drain logic would have to wait for it. A detached child is adopted for free and keeps the
  runner's changes small.
- **A queue campaign per review.** That is too heavy: it brings admission, slots and ledger
  states for a one-shot read-only run.
- **A Unix socket API.** It needs a server thread and a protocol. The data dir is already the
  shared channel and a rename is an atomic claim.

## Acceptance criteria

- [x] With a runner lease held on this host, `nw spec review SPEC-X` writes a request,
      the runner's `service` claims it and spawns the child with the runner's env plus
      `NW_RUNNER_JOB`, and the CLI prints or emits the child's stored review.
- [x] The job files (`request.json`, `claimed.json`, `child.json`, `result.json`) never
      contain a credential value.
- [x] An unclaimed job is cancelled when the runner goes away or the claim timeout passes,
      and the CLI error names `nw doctor`.
- [x] A claimed job whose child exits without a result gives an error naming `job.log`.
- [x] Claim and cancel race safely: exactly one rename wins.
- [x] With no runner up and a cursor backend without `CURSOR_API_KEY`, `run_spec_review` raises
      the actionable error before it journals or spawns anything.
- [x] Inside a runner job (`NW_RUNNER_JOB` set), the CLI never delegates again.
- [x] `tick()` publishes `credentials` in the lease, and `lease_status` returns it.
- [x] `nw doctor` `agentic.credentials` returns skip, warn, ok or fail as designed, and its
      output never holds a value. `agentic.cursor` is ok when the runner holds the key.
- [x] Existing local-review tests (fake runner, no lease) pass unchanged.

## Test plan

- **Automated tests** (`tests/test_runner_jobs.py`):
  - `submit` -> `service` (injected `spawn` that records argv/env and runs `run_job` in
    process against a fake-runner cfg) -> `wait` returns the review; the request is renamed to
    claimed and `child.json` has the pid; `NW_RUNNER_JOB` is in the spawn env.
  - A full CLI round trip: hold the lease with `lease.acquire` in the test, then run
    `nw spec review SPEC-0001 --json` through `run_cli` with a background thread calling
    `RJ.service` with the in-process spawn. The output equals the stored review, and the
    output line says "via the runner".
  - The secret never appears in the job files: set `CURSOR_API_KEY` to a sentinel in the
    service env and grep every file under the jobs dir.
  - Claim timeout: no service, so `wait` raises with `nw doctor` and `cancelled.json` exists.
    The lease is released while waiting, so `wait` cancels and raises.
  - A dead child: a `child.json` pid that has exited and no result, so `wait` raises with
    `job.log`.
  - Race: `cancel` after a claim returns False, and `service` after `cancel` claims nothing.
  - `delegate` is None when `NW_RUNNER_JOB` is set, when no lease is held, and when the lease
    host differs.
  - `run_spec_review` with `agentic.runner: cursor` and no key raises the no-runner text and
    writes no journal entry. With `NW_RUNNER_JOB` set it raises the runner text.
  - `run_job` with a bad spec path writes `{ok: false}` and returns non-zero.
  - Doctor: `check_credentials` gives skip with no config, warn with no runner, ok/fail from
    lease info, and warn for an old runner. `check_cursor_agent` is ok from the lease. A sentinel
    value never appears.
  - `tick()` with a lease publishes `credentials`.
- **Manual verification:** from the worktree, `uv run nw queue runner-job --help` shows the
  hidden command. A live check against the human's running `nw-runner.service` is left to the
  human after merge. The steps are restart the runner (`nw queue restart`), run
  `nw spec review SPEC-0476 --force` from a plain shell, and expect "via the runner" and a
  stored review. This agent must not touch the human's runner.
- **Regression guard:** run the affected tests (`python3 tools/affected_tests.py --run uv run
  pytest -q`), including `test_agentic_spec_review*.py`, `test_credentials.py`,
  `test_doctor.py`, `test_runner_unit.py` and `test_lease*`. `spec_lint` must be clean.

## As shipped

- `runner_jobs.py` matches the Design, plus `serves_jobs(st)`. The `run_spec_review` no-key
  error has a third text for a runner on this host that predates the spec: "the runner (pid N)
  predates runner jobs … restart it (`nw queue restart` …)".
- The journal record is `runner_job_started {job, job_kind, spec_id}`. `kind` is the
  journal's own record field; the first draft passed `kind=` and the swallowed `TypeError` hid
  the record until a test caught it.
- In `queue.py` the module is imported as `RNJ`, because `RJ` is already `remote_jobs`. The tick
  calls `_publish_credentials(lease, CR.ensure(cfg))` then `RNJ.service(cfg, stop=stop)`. `run()`
  publishes the start-time `ensure` result and calls `RNJ.service` on each pass of the wait
  loop.
- Doctor adds `check_credentials` (`agentic.credentials`, registered right after
  `agentic.runner`) and `_runner_credentials`. `check_cursor_agent` returns ok ("the runner
  holds CURSOR_API_KEY; CLI persona runs go through the runner") from the lease, before it falls
  back to the SPEC-0483 probe.
- Tests: `tests/test_runner_jobs.py` (17 cases, covering every Test plan item, plus the real
  spawn argv, stop/spawn-error handling and the hidden command).
- Manual: `uv run nw queue runner-job --help` shows the command, and `nw queue --help` does not
  list it. The live check against the human's `nw-runner.service` is still the human's step
  after merge (Test plan).

## Rollout / migration

1. Merge. The runner picks up the code through its usual code-change re-exec. Until then, an
   old runner never claims jobs, and the CLI's claim timeout reports it.
2. Old runners have no `credentials` in their lease, and doctor says "restart it".

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
