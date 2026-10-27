---
id: SPEC-0258
title: "Merge campaigns run worktrees.setup in an adopted worktree before gating"
status: accepted
owner: user
created: 2026-10-26
updated: 2026-10-26
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, merge, worktree, setup, bug]
source_idea: IDEA-340
depends_on: [SPEC-0233]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md); fixes
`IDEA-340` (kind: bug). `queue._do_open_merge` (src/wt/agentic/queue.py) runs the project's
`worktrees.setup` commands only when `merge["own_worktree"]` is true (a worktree wt created).
For an adopted worktree (`wt queue merge --worktree <path>`, SPEC-0233) the `else` branch only
validates it (not main checkout, exists, right branch, clean). Its `.venv` may lack optional
extras, so the gates die at collection: MERGE-20261026-6c2c and MERGE-20261026-9199 parked
`gates_failed` with `ImportError: textual` in `tests/test_tui_*.py`.

## Goals / Non-goals

**Goals**
- G1. An adopted worktree gets the project's `worktrees.setup` commands before gating, exactly
  as a created one does.
- G2. A failing setup parks the campaign with a clear reason; nothing is gated.

**Non-goals**
- Detecting whether setup is needed (lockfile or extras comparison). Setup commands are
  idempotent by contract (`uv sync`), so always running them is simpler.
- Cleaning setup output: the adopted worktree is the requester's.

## Decision

`_do_open_merge` runs `worktrees.setup` for both worktree kinds, after the adopted worktree's
validation (including the clean check) passes. Setup failure raises `GitError`. The handler
(`res.error = getattr(e, "tail", None) or str(e)`, queue.py:1046 and :997) uses only `tail`
when it is non-empty, which drops the command. So `_run_worktree_setup` builds the tail itself:
`GitError([cmd], code, f"{cmd} exited {code}:\n{last 20 output lines}")`. `res.error`, and
therefore the `open_failed` park detail, then always contains the command, the exit code and
the output tail, whether or not the command printed anything.

## Design

- Extract the loop at queue.py `_do_open_merge` (`for cmd in ... setup: subprocess.run(cmd,
  shell=True, cwd=worktree, ...)`) into `_run_worktree_setup(pcfg, worktree)`; `_do_open`
  (line ~980) can share it but is otherwise untouched.
- The shared helper owns the error text (above); the created-worktree merge branch and
  `_do_open` pick up the better message for free, with no other behavior change.
- In `_do_open_merge`, call it in the created branch (as now) and at the end of the adopted
  branch, after `git.is_clean(worktree, untracked=True)`. Order matters: setup creates
  untracked files (`.venv`, caches), which would fail the clean check if it ran first. The
  `.venv` is normally gitignored; if a project's setup writes tracked-visible output, the later
  gate-time cleanliness logic (close.py: an adopted worktree is never `git clean`ed) is
  unchanged and the existing `worktree_dirty` park applies.
- Invariant to keep: `repo["head"] = git.rev_parse(worktree)` stays after the setup call, so a
  setup that changes tracked files (lockfile rewrite) is visible to the existing close dirty
  check rather than hidden.
- Add a campaign ledger `finding` (`severity: info`, `gate: setup`, title `worktree setup ran
  in adopted worktree`) on success so the extra run is visible. Mechanism: `_do_open_merge`
  appends the event to `res.events_before` (which `_apply` seeds its events from, queue.py:1436);
  no separate ledger write from the worker. Only an adopted worktree gets this finding; a
  created one already ran setup before this change and stays silent. On failure the park detail
  is `res.error` (above). Reason code reuses `open_failed`.
- Resume re-enters `_do_open_merge`, whose clean check runs before setup. If a failed setup left
  untracked, non-ignored output, the resume parks "uncommitted or untracked changes" instead of
  re-running setup. This is expected: the requester cleans their own worktree (a non-goal for
  wt to clean it) and resumes.

## Alternatives considered

- **Verify the venv before gating, setup only on mismatch.** Rejected: fragile heuristics for
  something `uv sync` already makes a fast no-op.
- **Run setup in the requester's wrapper (`wt queue merge`).** Rejected: the runner, not the
  requester process, owns the worktree during the campaign, and resume must repeat it.

## Acceptance criteria

- [ ] `_do_open_merge` on an adopted worktree runs every `worktrees.setup` command with
      `cwd=<worktree>` after validation and before the campaign proceeds to gating.
- [ ] A created worktree still runs setup exactly once (no double run).
- [ ] A failing command parks `open_failed`; `res.error` and the park detail contain the
      command, its exit code and the output tail (also when the command printed output); no
      gate runs.
- [ ] A dirty adopted worktree still parks before any setup command runs.
- [ ] An `info` `setup` finding is recorded on the campaign ledger on success for an adopted
      worktree, and not for a created one.
- [ ] The Test plan passes and `uv run pytest` is green.

## Test plan

- **Automated tests** (`tests/test_agentic_merge_request.py` or the file that covers SPEC-0233;
  locate with `grep -l own_worktree tests`):
  - `test_adopted_worktree_runs_setup`: project config `worktrees.setup: ["touch
    .setup-ran"]`; adopted clean worktree; after `_do_open_merge`, the marker exists in it
    (and was written in the worktree, not the root).
  - `test_created_worktree_runs_setup_once`: count via an appended-line marker.
  - `test_adopted_setup_failure_parks`: setup `echo boom; exit 3` -> `res.error` contains the
    command text, `exit` code 3 and `boom`; through the queue, state is `PARKED`/`open_failed`,
    the park detail contains the same, and no gate ran. (A silent `exit 3` would pass even
    with the old handler, hence the output.)
  - In `test_adopted_worktree_runs_setup`, run through `_apply`/the queue and assert a
    `finding` event (`gate: setup`, `severity: info`) is on the ledger; in
    `test_created_worktree_runs_setup_once` assert it is absent.
  - `test_adopted_dirty_worktree_parks_before_setup`: untracked file present; marker absent.
- **Sabotage checks:** deleting the new call fails the adopted test; running setup before the
  clean check fails `test_adopted_dirty_worktree_parks_before_setup` or a gitignored-less
  variant; swallowing the nonzero exit fails the failure test; building the tail without the command
  (back to bare output) fails the command assertion in the failure test.
- **Manual verification:** `wt queue merge --worktree <worktree without textual> --by
  conductor --yes`; gates collect `tests/test_tui_*` without ImportError.
- **Regression guard:** full `uv run pytest`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; `uv run pytest` passes.
- [ ] Spec body updated to match what shipped; ledger regenerated.

## Open questions

None.
