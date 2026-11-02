---
id: SPEC-0368
title: "Remote gates gate a stat-dirty tree at HEAD instead of falling back, and expose done.changed on the run"
status: in-progress
owner: user
created: 2026-11-01
updated: 2026-11-01
kind: feature
source_idea: IDEA-701
tags: [agentic, gates, workers, remote]
depends_on: ["SPEC-0338", "SPEC-0341"]
---

## Context

Remote gate dispatch ([SPEC-0338](./0338-remote-gate-dispatch-with-local-fallback.md)) and the
coordinator's re-attach ([SPEC-0341](./0341-coordinator-re-attach-job-records-and-adoption.md))
are in place. IDEA-701 collects three gaps seen on the first real-box run on a current tree
(IDEA-409 gate-005, on the worker box). That idea is too big for one spec: it spans the
worker's job setup and the coordinator's commit-under-test and result handling. This spec is the
**coordinator-only** part (`remote_gates.py`, `gates.py`); the worker-setup part is a follow-up
spec (see Non-goals) that depends on this one.

1. **`prepare()` fails on a stat-dirty, content-clean tree.** `remote_gates.prepare` calls
   `_git(wt, "stash", "create")` with `check=True` (`remote_gates.py:239`). `git stash create`
   exits 1 with no output when stat information is stale but no content differs (a touched file,
   a rebase or checkout that rewrote mtimes). `_git` turns that into `RemoteFailure("bundle")`,
   a `worker_failed stage=bundle`, and a local fallback that on the real box took about
   25 minutes. `tree_under_test` (`remote_gates.py:880`) handles the same case with
   `check=False` and a `rev-parse HEAD` fallback, but it also treats a *real* git failure as
   HEAD, so the two call sites disagree on what "the commit under test" is, and neither checks
   that a silent exit 1 really means "content-clean".
2. **`done.changed` is invisible.** The worker reports the paths the gates wrote in its checkout
   (`worker.tree_changes`). The coordinator reads it only inside `_check_changed`
   (`remote_gates.py:418`), which turns a non-empty list into a local re-run. `GateRun`
   (`gates.py:67`) does not keep it, so `nw queue gates --worker <w> --json` cannot show it, and
   nobody can tell a clean remote run from one whose tree writes were never checked.

## Goals / Non-goals

**Goals**
- `prepare()` and `tree_under_test()` agree on the commit under test through one helper. A
  stat-dirty, content-clean tree gates HEAD and does not fall back; a tree with uncommitted
  content is never gated at HEAD by mistake.
- `GateRun` carries the `done` frame's `changed`, and `nw queue gates --json`, the `gate` event
  and `last_gate` show it for a remote run.

**Non-goals**
- The worker running the project's `worktrees.setup` in the job tree (`payload.setup`, a
  per-lock-hash uv environment cache with its lock and gc, a `changed` baseline after setup, the
  `setup` error stage and the setup timeout in `job_timeout_s`). This is the fix for the
  `ModuleNotFoundError: textual` red, and is a separate follow-up spec with
  `depends_on: SPEC-0368`. IDEA-701 stays open until that spec ships.
- The idea's real-box test (a remote gate on a current tree comes back green with pytest
  actually running, and a `failed_first` rerun with a genuine failing test comes back red). It
  needs the worker setup and belongs to the follow-up spec. This spec's only real-box step is
  the stat-dirty tree (Test plan, manual 2).
- Any change to the remote protocol, to `nw.job.v1`, or to the worker.
- Recording a fallback reason in the `--json` payload. The `tree`-stage fallback message
  already names the changed paths.
- Changing what a non-empty `changed` does (a local re-run without cooldown).

## Decision

`prepare()` and `tree_under_test()` call one `commit_under_test(worktree)` helper. It takes the
`stash create` commit when there is one, and HEAD when the tree is clean or when git exits 1
silently *and* `git diff --quiet HEAD --` confirms there is no content difference. Any other
outcome raises `RemoteFailure("bundle")`, so a tree that has content `stash create` could not
commit still falls back to a local run instead of being gated at HEAD.

`GateRun` gets a `changed` field, filled from the `done` frame on a remote run and `None` on a
local one. It is exposed in `as_payload`, so `nw queue gates --json` and the `gate` event carry
it.

## Design

### 1. One definition of the commit under test

`remote_gates.commit_under_test(worktree) -> str`:

1. Run `git -C <worktree> stash create` directly with `subprocess.run` (not through `_git`,
   which hides stderr when `check=False`).
2. Exit 0 with output: return that commit.
3. Exit 0 with no output (nothing to stash: a clean tree): return `git rev-parse HEAD`.
4. Exit 1 with empty stdout and empty stderr: this is `stash create`'s "my in-memory refresh
   found no changes" exit (stat-dirty). Do not assume it. Confirm the tree is content-clean with
   `git diff --quiet HEAD --`, run with `GIT_OPTIONAL_LOCKS=0` so it reads the index and never
   writes the refreshed one back. If that exits 0, return `git rev-parse HEAD`. Otherwise raise
   `RemoteFailure("bundle", "stash create found changes but made no commit", cooldown=False)`.
5. Any other failure (non-zero with stderr or stdout text, exit 1 with output, or `OSError`):
   `RemoteFailure("bundle", …, cooldown=False)`, as `_git(check=True)` raises today.

`prepare` uses it for `sha` (replacing line 239). `tree_under_test` uses it in place of its own
`check=False` line. `tree_under_test` therefore now raises `RemoteFailure` on a real git failure
where it used to answer with HEAD's tree; `adoptable` already catches that and returns `None`
("unknown: do not cancel"), which is the safe answer for a failing git. The stash stack and the
coordinator's index stay untouched (`stash create` never writes the stack; step 4 does not write
the index). After the change `remote_gates.py` has no `stash create` call outside the helper.

### 2. `done.changed` on the run

- `gates.GateRun` gets `changed: tuple | None = None`, with the comment "SPEC-0368: paths the
  gates wrote in the worker's checkout; None on a local run".
- `remote_gates._check_changed` returns the validated list as a tuple (it still raises on a
  non-empty, `null`, or malformed one). Both builders of the remote `GateRun` (the synchronous
  loop at the `done` frame, `remote_gates.py:629`, and `_Follow._done`, `remote_gates.py:1012`
  for async jobs) pass it as `changed=`. A cut-short run (`cut`) skips `_check_changed` today
  and keeps `changed=None`. An accepted remote run therefore has `changed == ()`; the field
  records that the worker checked.
- `gates.as_payload` emits `"changed": list(run.changed)` only when `run.changed is not None`,
  so a local run's payload is byte-for-byte unchanged. `nw queue gates --worker <w> --json`
  prints it through the existing `**G.as_payload(run)`; no CLI change is needed.
- `tests/data/remote_gates_golden.json` is the golden for a remote run's record; it gains the
  `changed: []` key and is regenerated with the test's record mode.

## Alternatives considered

- **Treat any silent exit 1 as content-clean** (the shortest fix, and what `tree_under_test`
  does today) — a path that exits 1 silently for a different reason would gate HEAD for a tree
  with uncommitted content and come back green for code that is not the tree's. Today's
  `prepare` fails safe to a local run there; the diff check keeps that.
- **Run `git update-index --refresh` first** — it writes the coordinator's index, which this
  path has avoided so far (`stash create` leaves the stash stack and the index alone).
- **Run `git status --porcelain` for the content check** — it also lists untracked files, which
  `stash create` ignores, so an untracked scratch file would turn a good stat-dirty tree into a
  fallback.
- **Put `changed` only in the CLI JSON** — `gate` events and `last_gate` would still not show
  that the worker checked the tree; one field on `GateRun` covers all three.

## Acceptance criteria

- [ ] `commit_under_test` returns the stash commit on a content-dirty tree, HEAD on a clean
      tree, and HEAD on a stat-dirty, content-clean tree (`git stash create` exits 1 with no
      output and `git diff --quiet HEAD --` exits 0).
- [ ] `commit_under_test` raises `RemoteFailure("bundle")` when `stash create` exits 1 silently
      but `git diff --quiet HEAD --` reports a difference (tested with a stubbed git), and the
      failure has `cooldown=False`.
- [ ] `commit_under_test` raises `RemoteFailure("bundle", …, cooldown=False)` when git fails
      with an error message, exits 1 with output, or cannot start.
- [ ] `prepare()` on a stat-dirty, content-clean tree returns a header whose `commit` is HEAD
      and the run goes to the worker: no `worker_failed stage=bundle` and no fallback finding.
      The coordinator's stash list and index file are unchanged afterwards.
- [ ] `prepare()` and `tree_under_test()` both call `commit_under_test`, and `remote_gates.py`
      has no other `stash create` call; on a stat-dirty tree they agree on the commit and its
      tree; `adoptable` returns `None` (not `False`) when `commit_under_test` raises.
- [ ] A remote `GateRun` from the synchronous loop and one from `_Follow._done` both carry
      `changed == ()`; a local run and a cut-short remote run carry `changed is None`.
- [ ] A remote run whose `done` frame lists changed paths still falls back to a local run
      without cooldown, as before.
- [ ] `gates.as_payload` includes `"changed": []` for a remote run and no `changed` key for a
      local run; `nw queue gates <id> --worker <w> --json` output shows `"changed": []`; the
      golden `tests/data/remote_gates_golden.json` contains it.
- [ ] Real-box: with a worker registered on the real worker box, a stat-dirty campaign worktree
      (a tracked file touched, no content change) is gated on the worker (`worker: <w>` in the
      `--json` output), not by a `stage=bundle` fallback.

## Test plan

- **Automated tests**
  - `tests/test_remote_gates.py` (using the module's fixed-date repo helpers):
    - `test_commit_under_test_cases`: parametrized over a clean tree, a content-dirty tracked
      tree, and a **stat-dirty tree** (rewrite a tracked file with identical content and a new
      mtime, without `git update-index --refresh`, so `stash create` exits 1 with no output).
      Asserts HEAD / the stash commit / HEAD.
    - `test_commit_under_test_silent_exit_1_but_dirty`: a stubbed `subprocess.run` where
      `stash create` exits 1 silently and `git diff --quiet HEAD --` exits 1; asserts
      `RemoteFailure` with `stage == "bundle"` and `cooldown is False`, and that no
      `rev-parse HEAD` was consulted.
    - `test_commit_under_test_git_failure`: stubbed git exiting 128 with stderr, exiting 1 with
      stdout text, and `OSError`; each raises `RemoteFailure("bundle")`.
    - `test_commit_under_test_leaves_index_alone`: after a stat-dirty call, the stash list and
      the bytes of `.git/index` are unchanged.
    - `test_stat_dirty_tree_runs_remotely`: with the `world`/`remote` fixtures, a stat-dirty
      tree runs on the worker with `header["commit"] == HEAD`, `run.worker` set, and no
      fallback finding.
    - `test_prepare_and_tree_under_test_share_the_helper`: both agree on a stat-dirty tree;
      a source check (the existing style of the repo's guard tests) asserts one `stash create`
      call site in `remote_gates.py`.
    - `test_remote_run_records_changed`: a remote `GateRun.changed == ()` for the synchronous
      loop and for the async `_Follow._done` path; `as_payload` has `changed: []`; a local
      run's and a cut-short run's `as_payload` has no such key; a `done` frame with changed
      paths still falls back locally (existing `tree`-stage test stays green).
    - `test_golden_record_or_compare` is regenerated for the new key.
  - `tests/test_remote_job_records.py`: `test_adoptable_unknown_when_commit_under_test_fails`
    (a held index lock or stubbed git failure gives `None`).
  - `tests/test_queue_gates_cli.py` (or the existing `nw queue gates` CLI test module):
    `--worker <w> --json` shows `"changed": []`, and the same command without `--worker` has no
    `changed` key.
  - Affected-tests run while iterating: `python3 tools/affected_tests.py --run uv run pytest -q`
    plus the files above.
- **Manual verification** (real worker box, per the validate-integrations-against-the-real-binary
  rule; fakes do not close this):
  1. In a campaign worktree on the coordinator, run
     `uv run nw queue gates <campaign> --worker <w> --json` on a clean tree. Expect `worker:
     <w>` and `changed: []`. (Whether the gates are green depends on the worker setup follow-up;
     this step only checks the dispatch and the `changed` field.)
  2. `touch` a tracked file in the worktree (no content change; confirm `git stash create`
     exits 1 with no output) and rerun: expect the run on the worker, not a
     `worker_failed stage=bundle` finding or a local fallback, and `git stash list` unchanged.
  3. Record the results in the spec's "As shipped" section when moving to `done`.
- **Regression guard:** the existing `tests/test_remote_gates.py` golden and
  `test_remote_run_matches_a_local_one` keep a remote run identical to a local one apart from
  the new `changed` key; `tests/test_agentic_gates.py` passes unchanged (a local `as_payload`
  has no new key); the stash-stack assertion at `tests/test_remote_gates.py:298-303` stays
  green; the queue's close gate runs the full `uv run pytest`.

## Rollout / migration

1. Coordinator-only change; it needs no worker update and no protocol change. A worker that
   already sends `changed` in `done` (all current ones) is enough.
2. No data migration. Persisted `last_gate` payloads and `gate` events from before the change
   simply lack `changed`; readers must treat the key as optional (they already do for other
   remote-only keys such as `worker`).
3. The worker-setup follow-up spec builds on this one and is what turns the real-box gate green.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected automated tests pass; the real-box manual steps performed
      and noted in the spec; the queue's close gate runs the full suite.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped (an "As shipped" note with the real-box results).
- [ ] `docs/LEDGER.md` regenerated with `python3 tools/spec_lint.py --write-ledger`.
- [ ] `python3 tools/spec_lint.py` exits 0.

## Open questions

- None. The content-clean check (`git diff --quiet HEAD --` with `GIT_OPTIONAL_LOCKS=0`) and the
  choice to make a failing `tree_under_test` mean "unknown" are decided above and can be tuned
  from real-box evidence.
