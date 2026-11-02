---
id: SPEC-0368
title: "Remote gating goes green on current trees: worker runs worktrees.setup, stash-create tolerates a stat-dirty tree, done.changed is exposed"
status: proposed
owner: user
created: 2026-11-01
kind: feature
source_idea: IDEA-701
tags: [agentic, gates, workers, remote]
depends_on: ["SPEC-0338", "SPEC-0340", "SPEC-0341"]
---

## Context

Remote gate dispatch ([SPEC-0338](./0338-remote-gate-dispatch-with-local-fallback.md)), the
worker-owned async jobs ([SPEC-0340](./0340-worker-async-job-protocol.md)) and the coordinator's
re-attach ([SPEC-0341](./0341-coordinator-re-attach-job-records-and-adoption.md)) are in place.
The first real-box run on a current tree (IDEA-409 gate-005, on the worker box) was red for two
independent reasons, and a third gap made it hard to see why. The real-box test of SPEC-0367
depends on a remote gate that is green when the code is green.

1. **The worker never sets the job tree up.** `worker._checkout` (`src/nightwright/agentic/worker.py`)
   makes a `--shared --no-checkout` clone of the cache and checks out the commit. `worker._run_gates`
   then runs the gates straight in it. A local campaign worktree is set up by
   `queue._run_worktree_setup` from the project's `worktrees.setup` (`.nw/project.yaml`:
   `uv sync --extra tui`), but nothing carries that list to the worker. In a tree without that
   environment, `pytest` stops at collection with `ModuleNotFoundError: textual`
   (`src/wt/tui/queue_tab.py:12`, an optional extra). That red does not reflect the code under
   test.
2. **`prepare()` fails on a stat-dirty, content-clean tree.** `remote_gates.prepare` calls
   `_git(wt, "stash", "create")` with `check=True` (`remote_gates.py:239`). `git stash create`
   exits 1 with no output when stat information is stale but no content differs (a touched file,
   a rebase or checkout that rewrote mtimes). That becomes `RemoteFailure("bundle")`, a
   `worker_failed stage=bundle`, and a local fallback that on the real box took about 25 minutes.
   `tree_under_test` (`remote_gates.py:880`) already handles this with `check=False` and a
   `rev-parse HEAD` fallback, so the two disagree on what "the commit under test" is.
3. **`done.changed` is invisible.** The worker reports the paths the gates wrote in its checkout
   (`worker.tree_changes`). The coordinator reads it only inside `_check_changed`, which turns a
   non-empty list into a local re-run. `GateRun` does not keep it, so `nw queue gates --worker
   <w> --json` cannot show it, and nobody can tell a clean remote run from one whose tree writes
   were never checked.

## Goals / Non-goals

**Goals**
- A remote gate job runs the project's configured `worktrees.setup` in the job tree before the
  first gate, with the resulting Python environment cached per lock hash, so a repeat job on an
  unchanged lock does no installs.
- `prepare()` and `tree_under_test()` agree on the commit under test through one helper. A
  stat-dirty, content-clean tree gates HEAD and does not fall back.
- `GateRun` carries the `done` frame's `changed`, and `nw queue gates --json` shows it for a
  remote run.
- One real remote gate on a current tree comes back green with pytest actually collecting and
  running, and a `failed_first` rerun with a genuine failing test (not a collection error) comes
  back red for that test.

**Non-goals**
- Not a new remote protocol or a new `nw.job.v1` schema version. `payload.setup` is an optional
  key; a worker that does not know it ignores it.
- Not caching of non-uv environments. Setup commands run on every job; only the uv environment
  is shared between jobs.
- Not changing the local-run path, local `worktrees.setup` handling, or `cleanup` policy.
- Not recording a fallback reason in the `--json` payload. The `tree`-stage fallback message
  already names the changed paths.

## Decision

The coordinator sends the project's `worktrees.setup` commands in the job header as
`payload.setup`. The worker runs them in the job tree, inside the gate slot and before the first
gate. For a uv project it points `UV_PROJECT_ENVIRONMENT` at a shared environment directory keyed
by repo and lock hash, for both the setup commands and the gates, so `uv sync` converges on a
cached environment instead of building a new `.venv` per job. A failing setup command is a
`setup`-stage worker error, which falls back to a local run like any other transport failure.

`prepare()` and `tree_under_test()` call one `commit_under_test(worktree)` helper. It takes the
stash-create commit when there is one, HEAD when git exits 1 with no output (stat-dirty,
content-clean), and raises `RemoteFailure("bundle")` only on a real git failure.

`GateRun` gets a `changed` field, filled from the `done` frame on a remote run and `None` on a
local one. It is exposed in `as_payload`, so `nw queue gates --json` and the `gate` event carry
it.

## Design

### 1. Worker runs the project's setup

- **Header.** `remote_gates._header` adds `payload["setup"]`, the list from
  `project_cfg["worktrees"]["setup"]` (empty list when absent; the same strings
  `queue._run_worktree_setup` runs). `worker.validate_header` accepts `payload.setup` as an
  optional list of strings without NUL bytes and rejects anything else with the usual
  `WorkerError("validate", …)`. The key is optional so an older coordinator's header still
  validates.
- **Where it runs.** `worker._run_gates` runs the setup after `started` is emitted (so it counts
  against the worker's slot and `gate_budget_s`, like gate time) and before the first gate. Each
  command is run with `shell=True`, `cwd=<job tree>`, the same env the gates get (`env` in
  `_run_gates`) and a per-command timeout `SETUP_TIMEOUT_S = 900`, ended on `cancel`. `job_timeout_s`
  in `remote_gates.py` adds one `SETUP_TIMEOUT_S` when the setup list is non-empty, so the
  coordinator's kill timer does not fire during a cold `uv sync`.
- **Environment cache.** When the tree has a `uv.lock`, the worker computes
  `lock_hash = sha256(uv.lock + pyproject.toml + the setup command list)[:16]`. The shared
  environment is `<worker_dir>/envs/<repo_key>/<lock_hash>/venv`. The worker sets
  `UV_PROJECT_ENVIRONMENT` to it in the env of the setup commands and of the gates. Setup for
  one `(repo_key, lock_hash)` runs under an `flock` on `<envs dir>/<lock_hash>.lock`, so two jobs
  racing on a cold cache do one `uv sync` and the second finds it in sync. The gates run after
  the lock is released and only read the environment. A tree without `uv.lock` sets no
  `UV_PROJECT_ENVIRONMENT`; its setup commands run in the tree as written.
- **Cache lifetime.** `worker gc` removes `envs/<repo_key>/<lock_hash>` directories not used by
  a live job and untouched for `--keep-days` (default `DEFAULT_KEEP_DAYS`), using the directory's
  mtime, touched at each use.
- **Baseline for `changed`.** Setup can leave untracked, non-ignored files in the tree. `_run_gates`
  records `tree_changes(tree)` after setup and `done.changed` excludes those paths, so setup
  output does not trigger the `tree` fallback. Only what the gates wrote counts.
- **Failure.** A setup command that exits non-zero or times out ends the job with an `error`
  frame, `stage="setup"`, `error="<cmd> exited <code>:\n<last 20 lines>"` (the shape of
  `_run_worktree_setup`'s message). No `done` frame follows. The coordinator maps it to
  `RemoteFailure("setup", …)` with a cooldown (the cause is the worker host: network, disk,
  missing `uv`), and the gates then run locally under the same seq. The `error` frame is
  handled in both the synchronous loop and the async `_follow` path, which already treat any
  `error` frame as a `RemoteFailure`.

### 2. One definition of the commit under test

`remote_gates.commit_under_test(worktree) -> str`:

1. Run `git stash create` directly (not through `_git`, which hides stderr when `check=False`).
2. Exit 0 with output: that commit.
3. Exit 0 with no output (clean tree), or exit 1 with empty stdout and empty stderr (stat-dirty,
   content-clean): `git rev-parse HEAD`.
4. Any other failure (non-zero with stderr, or `OSError`): `RemoteFailure("bundle", …,
   cooldown=False)`, as today.

`prepare` uses it for `sha`; `tree_under_test` uses it in place of its own `check=False` line.
`tree_under_test` keeps raising `RemoteFailure` on a real git failure, which `adoptable` already
maps to "unknown". The stash stack stays untouched (`stash create` never writes it).

### 3. `done.changed` on the run

- `gates.GateRun` gets `changed: tuple | None = None` ("SPEC-0368: paths the gates wrote in the
  worker's checkout; None on a local run").
- `remote_gates._check_changed` returns the validated list (it still raises on a non-empty or
  malformed one), and both builders of the remote `GateRun` (the synchronous loop at the `done`
  frame and `_Follow._done` for async jobs) pass it as `changed=tuple(changed)`. An accepted
  remote run therefore has `changed == ()`; the field records that the worker checked.
- `gates.as_payload` emits `"changed": list(run.changed)` only when `run.changed is not None`,
  so a local run's payload is byte-for-byte unchanged. `nw queue gates --worker <w> --json`
  prints it through the existing `**G.as_payload(run)`; no CLI change is needed beyond that.
- `tests/data/remote_gates_golden.json` is the golden for a remote run's record; it gains the
  `changed: []` key and is regenerated with the test's record mode.

## Alternatives considered

- **Run `uv sync` in the worker unconditionally, ignoring the project config** — it
  would get this project green but breaks every project whose setup differs (npm, make). The
  project already declares its setup; the worker should run that.
- **Per-job `.venv` in the tree, no cache** — correct, but every gate run pays a full
  `uv sync --extra tui` (tens of seconds to minutes), which defeats the speed reason for remote
  gates.
- **Copy a cached `.venv` into each job tree** — venvs embed absolute paths in scripts, so a
  copy breaks. `UV_PROJECT_ENVIRONMENT` avoids the move.
- **Treat a stat-dirty tree by running `git update-index --refresh` first** — it writes the
  coordinator's index, which this path has avoided so far (`stash create` leaves the stash stack
  and the index alone).
- **Put `changed` only in the CLI JSON** — `gate` events and `last_gate` would still not show
  that the worker checked the tree; one field on `GateRun` covers all three.

## Acceptance criteria

- [ ] `remote_gates._header` puts the project's `worktrees.setup` list in `payload.setup`
      (empty list when the project has none); `validate_header` accepts a header with or without
      it and rejects a non-list or a non-string/NUL entry with a `validate` error.
- [ ] A remote gate job runs each `payload.setup` command in the job tree after `started` and
      before the first gate, with the gates' env, and the commands' output does not reach the
      coordinator's results.
- [ ] For a tree with `uv.lock`, setup and gates both see `UV_PROJECT_ENVIRONMENT` set to
      `<worker_dir>/envs/<repo_key>/<lock_hash>/venv`. A second job at the same lock hash runs
      setup against the existing environment (no new environment directory is created). A
      changed `uv.lock` yields a new `lock_hash` directory.
- [ ] Two concurrent jobs at the same cold `(repo_key, lock_hash)` run the setup commands one
      after the other, not in parallel.
- [ ] A setup command that fails or times out yields an `error` frame with `stage="setup"`
      naming the command and exit code, no `done` frame, and a coordinator fallback to a local
      run of the same gates under the same seq, with a worker cooldown.
- [ ] Files that setup leaves untracked and not ignored do not appear in `done.changed`; a file
      a gate writes does.
- [ ] `job_timeout_s` is larger by `SETUP_TIMEOUT_S` when the project has setup commands, and
      unchanged when it has none.
- [ ] `commit_under_test` returns HEAD on a stat-dirty, content-clean tree (`git stash create`
      exit 1, no output), the stash commit on a content-dirty tree, HEAD on a clean tree, and
      raises `RemoteFailure("bundle")` when git fails with an error message.
- [ ] `prepare()` on a stat-dirty, content-clean tree returns a header whose `commit` is HEAD
      and the run goes to the worker; it no longer produces `worker_failed stage=bundle`.
- [ ] `prepare()` and `tree_under_test()` both call `commit_under_test` (no second
      `stash create` call site in `remote_gates.py`).
- [ ] A remote `GateRun` carries `changed == ()`; `nw queue gates <id> --worker <w> --json`
      output includes `"changed": []`; a local run's `--json` output has no `changed` key.
- [ ] Real-box: one remote gate on a current tree (with `textual` available only via
      `uv sync --extra tui`) is green and its pytest log shows tests passing, not a collection
      error.
- [ ] Real-box: a `failed_first` rerun of that gate with one deliberately failing test is red
      and the log names that test as the failure (an assertion, not `ModuleNotFoundError` or a
      collection error).

## Test plan

- **Automated tests**
  - `tests/test_remote_gates.py`:
    - `test_header_carries_worktrees_setup`: a project with
      `worktrees: {setup: ["echo a"]}` yields `payload.setup == ["echo a"]`; a project without
      yields `[]`.
    - `test_commit_under_test_cases`: parametrized over a clean tree, a content-dirty tracked
      tree, a **stat-dirty tree** (`touch` a tracked file, or rewrite it with identical content,
      without `git update-index --refresh`, so `stash create` exits 1 with no output) and a real
      git failure (a repository with a broken `index`/`.git` lock). Asserts HEAD / stash commit /
      HEAD / `RemoteFailure("bundle")`.
    - `test_stat_dirty_tree_runs_remotely`: with the `world`/`remote` fixtures, a stat-dirty
      tree runs on the worker with `header["commit"] == HEAD`, `run.worker` set, no fallback
      finding.
    - `test_tree_under_test_matches_prepare`: `tree_under_test` and `prepare` agree on the tree
      for a stat-dirty tree.
    - `test_remote_run_records_changed`: a remote run's `GateRun.changed == ()`, its
      `as_payload` has `changed: []`, and a local run's `as_payload` has no such key. The golden
      `tests/data/remote_gates_golden.json` is updated (`test_golden_record_or_compare`).
    - `test_setup_failure_falls_back_locally`: a failing setup command makes the worker send
      `error` with `stage="setup"`; the run completes locally under the same seq and the worker
      is on cooldown.
    - `test_job_timeout_includes_setup`.
  - `tests/test_worker.py` / `tests/test_worker_jobs.py`:
    - `test_validate_header_setup`: accepts absent/empty/valid; rejects non-list, non-string, NUL.
    - `test_setup_runs_before_gates_in_the_tree`: a setup command writes a marker file that the
      first gate's `test -f` checks (green only if setup ran first), in both the synchronous
      `gate-job` path and the async job path (`worker_job.run_job`).
    - `test_uv_env_cache_per_lock_hash`: with a stub `uv` on `PATH` that records
      `UV_PROJECT_ENVIRONMENT`, two jobs on the same `uv.lock` see the same directory, a job on
      an edited `uv.lock` sees a different one; the tree has no `.venv`.
    - `test_setup_serialized_per_lock_hash`: two concurrent jobs, a setup stub that records
      start/end timestamps; intervals do not overlap.
    - `test_setup_output_is_not_changed`: setup creates an untracked file; `done.changed` omits
      it; a gate that writes a file shows it in `changed`.
    - `test_gc_removes_stale_env_dirs`: an env dir older than `--keep-days` and not in use is
      removed; a fresh one is kept.
  - Affected-tests run while iterating: `python3 tools/affected_tests.py --run uv run pytest -q`
    plus the files above.
- **Manual verification** (real worker box, per the validate-integrations-against-the-real-binary
  rule; fakes do not close this):
  1. On a current tree of this repo where the worker has no prior environment, run
     `uv run nw queue gates <campaign> --worker <w> --json`. Expect `ok: true`, `worker: <w>`,
     `changed: []`, and a pytest log showing passed tests (no `ModuleNotFoundError`). Record the
     run's wall time.
  2. Run it again without changing `uv.lock`: expect green, and a setup step that finishes in
     seconds (the cached environment).
  3. `touch` a tracked file in the campaign worktree (no content change) and rerun: expect the
     run on the worker, not a `worker_failed stage=bundle` finding or a local fallback.
  4. Add a deliberately failing test in the worktree and rerun with a `failed_first` rerun
     config: expect red, the failing test named in the log, and the rerun targeting it. Remove
     the test.
  5. Note the results in the spec's "As shipped" section when moving to `done`.
- **Regression guard:** the existing `tests/test_remote_gates.py` golden and
  `test_remote_run_matches_a_local_one` keep a remote run identical to a local one apart from the
  new `changed` key; `tests/test_agentic_gates.py` passes unchanged (local `as_payload` has no
  new key); `test_worker.py` header-validation tests for older headers (no `payload.setup`) stay
  green; the queue's close gate runs the full `uv run pytest`.

## Rollout / migration

1. Land the coordinator and worker changes together. A coordinator that sends `payload.setup`
   to an older worker is safe (the key is ignored, today's behavior); a newer worker given an
   older coordinator's header (no `payload.setup`) runs no setup, also today's behavior.
2. Update each registered worker's `nw` (`nw workers bootstrap`, SPEC-0354) so it runs setup.
   Until then a worker still produces the collection-error red; the real-box check in the
   test plan is run after the update.
3. Existing `envs/` does not exist; the first job at each lock hash builds it.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected automated tests pass; the real-box manual steps performed
      and noted in the spec; the queue's close gate runs the full suite.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped (an "As shipped" note with the real-box results).
- [ ] `docs/LEDGER.md` regenerated with `python3 tools/spec_lint.py --write-ledger`.
- [ ] `python3 tools/spec_lint.py` exits 0.

## Open questions

- None. Setup failure policy (fall back locally with a cooldown), the 900 s per-command setup
  timeout and the `uv.lock`-keyed cache are decided above and can be tuned from real-box
  evidence.
