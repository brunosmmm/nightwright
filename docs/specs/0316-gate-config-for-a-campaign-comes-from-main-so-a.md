---
id: SPEC-0316
title: "A campaign's gate timeouts are the max of main's and its own branch's `.wt/project.yaml`"
status: proposed
owner: user
created: 2026-10-31
kind: feature
source_idea: IDEA-438
tags: [agentic, gates, project-config, close]
---

## Context

The queue resolves a campaign's project config once per runner process, from the **main
checkout**: `_Ctx.project` (`src/wt/agentic/queue.py:1138`) calls `P.project_config`
(`src/wt/agentic/project.py:344`), which reads `<project_root>/.wt/project.yaml` where the
root is the main repo, never the campaign worktree. That dict is passed to every gate run:
`_do_gate` (`queue.py:1855`) and the close's re-gate on the rebased tree (`prepare_close`,
`src/wt/agentic/close.py:547`) both call `G.run_all(pcfg, ...)`, and `_run_all_held`
(`gates.py:431`) iterates `project_cfg["gates"]`, killing a gate at its `timeout_s`
(`gates.py:179`, default 1200).

So a campaign whose purpose is to raise a gate's `timeout_s` (typical fix after a gate
timed out on a growing suite or a loaded host, see
[SPEC-0311](./0311-gate-timeouts-under-machine-load-are-triaged-as.md)) edits
`.wt/project.yaml` **in its branch**, but every gate it runs, including the close-time
re-gate that decides whether it may merge, still uses main's old, too-small timeout. The
very change that fixes the timeout cannot pass the timeout it is fixing: the campaign times
out, re-gates (SPEC-0311), times out again and parks `gate_timeout`, until a human edits
the yaml in the main checkout by hand, which the workflow forbids.

## Goals / Non-goals

**Goals**
- A branch that raises a gate's `timeout_s` in its `.wt/project.yaml` has the raised value
  honored by its own gate runs, in particular the close-time re-gate.
- A branch can never *lower* a gate's limit below main's, so a campaign cannot weaken the
  gate that judges it.
- Nothing else in a gate definition becomes branch-controlled: which gates exist, their
  `cmd`, `required`, `retries`, `results` and `cwd` keep coming from main.
- The raise is visible in the ledger, so a reviewer sees that a campaign ran under limits
  other than main's.

**Non-goals**
- Loading the whole project config (merge policy, setup, agents, worktree root, `base`)
  from the branch. Only gate `timeout_s` is merged.
- Changing the wt-level `gate_budget_s` (`events.settings`) or the in-gate retry ladder
  ([SPEC-0289](./0289-gate-ladder-v2.md)); neither lives in `project.yaml`.
- Adding or removing gates, or changing a gate's `required` flag, from a branch.
- Changing how main's config is cached or reloaded
  (`queue.py:4392` already restarts the runner on a main `project.yaml` change).

## Decision

When a campaign's gates run, the effective `timeout_s` of each gate is the **maximum** of
main's value and the value in the `.wt/project.yaml` of the worktree being gated, matched by
gate `name`. Everything else in the gate comes from main. The branch yaml is read from the
worktree at the moment of the run, so the close re-gate (after rebase) sees the file as it
would be on `base` once merged, and `_do_gate` sees the branch as implemented so far. A
branch yaml that is missing, unreadable, invalid, or names a gate main does not have is
ignored for that gate (main's config applies unchanged); it never fails the run.

Taking the max (rather than the branch value outright) is the conservative choice: it
solves the bug (raise) while a branch that lowers a limit, or a rebase that reverts it,
cannot make gates easier than main already requires.

## Design

- `project.py`: add `gate_limits_overlay(project_cfg, worktree) -> tuple[dict, list[dict]]`.
  It reads `config_path(worktree)` with `load_project_config`; on a missing file or any
  `ValueError`/`OSError`/`UnicodeDecodeError` it returns `(project_cfg, [])`. Otherwise it
  returns a shallow copy of `project_cfg` whose `gates` list has, for each main gate that has
  a same-named branch gate with a larger `timeout_s`, a copied gate dict with that
  `timeout_s`, plus a list of `{"gate", "main_timeout_s", "branch_timeout_s"}` records for
  every gate it raised. Gates with equal or smaller branch values, and branch-only gates,
  are left alone. The input dict is never mutated.
- `queue.py` `_do_gate`: run `G.run_all(overlaid_pcfg, ...)` with the overlay computed from
  `state["repo"]["worktree"]`.
- `close.py` `prepare_close`, step 5 (re-gate): compute the overlay after the rebase and
  `_regenerate_after_rebase`, so it reads the rebased file; pass the overlaid config to
  `G.run_all`. When the overlay raised anything, append a `finding` event
  (`persona: "close"`, `severity: "info"`, `gate: "gate-limits"`, title "gate timeouts
  raised by the branch's .wt/project.yaml", detail listing each `gate: main -> branch`).
  `_do_gate` appends the same finding through its `StepResult` events for its run.
- The same overlay is applied wherever else the queue invokes `G.run_all` or
  `G.run_and_log` for a campaign worktree (`gates.py:run_and_log`, used by `wt gate run`
  and `wt queue gates`), so a manual re-run reproduces what the queue ran. The gate
  sidecar already records the `timeout_s` actually used (`gates.py:153`), so the effective
  limit is auditable per run.
- Docs: a short note in the `.wt/project.yaml` reference (`docs/` page for SPEC-0170
  config, wherever `timeout_s` is documented) stating that a branch may raise a gate's
  `timeout_s` and never lower it.

## Alternatives considered

- **Use the branch's whole `project.yaml` for the campaign** — lets a campaign rewrite its
  own `cmd`, `required`, merge policy, worktree root; far more trust than the bug needs.
- **Use the branch's `timeout_s` outright** — a branch (or a stale rebase) could then lower
  a limit and make its own gate easier than main's; max has no downside for the fix.
- **Have a human edit the yaml in main first** — what happens today; forbidden by the
  worktree-only rule and defeats an unattended queue.
- **Raise timeouts globally / adaptive timeouts** — hides slow suites and was ruled out as
  a non-goal by SPEC-0311.

## Acceptance criteria

- [ ] With main `timeout_s: 1` for a gate and a worktree yaml with `timeout_s: 30` for the
      same gate, the gate run uses 30 (the sidecar `timeout_s` and kill threshold).
- [ ] With main `timeout_s: 30` and the worktree yaml `timeout_s: 1`, the run uses 30.
- [ ] A branch-only gate, or a branch change to `cmd`, `required`, `retries`, `results` or
      `cwd`, has no effect on the run.
- [ ] A missing, unparsable, schema-invalid or non-UTF-8 worktree yaml leaves main's config
      in force and does not fail or park the run.
- [ ] `prepare_close` reads the yaml after the rebase: a campaign whose branch raises the
      timeout and which rebases onto a moved base passes its re-gate on the raised value.
- [ ] `_do_gate` and `prepare_close` each append a `gate-limits` info `finding` event when
      (and only when) a limit was raised, naming the gate and both values.
- [ ] `wt gate run` for a campaign applies the same overlay.
- [ ] `gate_limits_overlay` does not mutate the config dict it is given.

## Test plan

- **Automated tests** (new `tests/test_agentic_gate_limits.py`, plus additions to
  `tests/test_agentic_close.py`):
  - unit tests of `gate_limits_overlay` for: raise, equal, lower, branch-only gate, changed
    `cmd`/`required` ignored, missing file, malformed YAML, invalid schema, input not
    mutated, returned raise records;
  - a gate run through `G.run_all` against a real worktree dir whose gate sleeps longer
    than main's `timeout_s` but shorter than the branch's: passes with the overlay, times
    out without it; asserts the sidecar `timeout_s`;
  - a close test (existing `test_agentic_close.py` fixtures) where the branch raises the
    timeout, main's base moved, and the re-gate runs after rebase: outcome is the merge, not
    `TRIAGING`; the `gate-limits` finding is in the events; and a control where nothing is
    raised yields no such finding;
  - `_do_gate` test with the same fixture asserting the finding and the raised limit;
  - a `wt gate run` CLI test asserting the overlaid timeout is used.
  Use `tests/waits.py` for any waiting; keep gate sleeps in the 1-3 second range.
- **Manual verification:** in a scratch project, set a gate `cmd: sleep 5` with
  `timeout_s: 2` on main, queue a campaign whose branch sets that gate to `timeout_s: 30`,
  and confirm `wt queue` merges it with a `gate-limits` finding and no `gate_timeout` park.
- **Regression guard:** full `uv run pytest` plus `python3 tools/spec_lint.py`; existing
  gate and close tests (without a branch yaml difference) must pass unchanged, proving the
  no-difference path is a no-op.

## Rollout / migration

No data migration. Ship the helper and its call sites together; campaigns in flight pick up
the behavior on their next gate run. Without a branch yaml difference behavior is identical
to today.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided: max-of-both, `timeout_s` only, matched by gate name, silently ignoring an
unusable branch yaml.
