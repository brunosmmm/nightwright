---
id: SPEC-0359
title: "Close-tier reuse of a green loop gate, with a full-suite sentinel on the base branch"
status: accepted
owner: user
created: 2026-11-01
kind: feature
source_idea: IDEA-649
tags: [agentic, gates, close, merge, tests]
depends_on: ["SPEC-0358"]
supersedes: ["SPEC-0355"]
---

## Context

[SPEC-0328](./0328-tiered-gates-affected-test-selection.md) split gates into a loop tier
(`pytest-affected`: `tools/affected_tests.py` picks the tests a branch affects) and a close tier
(the full `pytest` suite once, on the rebased tree). [SPEC-0350](./0350-per-gate-path-sensitivity.md)
skips close gates whose `paths` the branch does not touch. Only docs-only merges got faster
(24 s instead of about 600 s). This spec is part 3 of 3 of the split of SPEC-0355 (which it
supersedes together with the other two parts). It covers the **close and merge tier**.

The 2026-11-01 ledgers and gate logs show: the full `pytest` gate takes 566-822 s for any `src/**`
or `tools/**` change. It runs even when the loop tier just proved the same branch green and the
rebase only brought in unrelated main commits. Slot waits of 317-671 s add to this, and shorter
gate runs make those waits shorter too.

This spec builds on the earlier parts of the split: part 1 (the `NW_GATE_REPORT` selection report and
a JSON CLI mode of `tools/affected_tests.py` that exposes the selection and the forward closure)
supplies the inputs for rules R5 and R6, and part 2 (failed-first rounds) shares the gate-result
plumbing in `run_all`.

## Goals / Non-goals

**Goals**
- G2. A close-tier gate may reuse the last green loop-tier result instead of running, when the
  rebase is *clean* (defined under Design). Each reuse is recorded as a `gate.reused` ledger event
  with its evidence. Each decline is recorded as a `gate.reuse_declined` event.
- G3. A periodic **full-suite sentinel** on the base branch catches what reuse lets through. A red
  sentinel turns reuse off until main is green again. Reuse also declines while the sentinel is
  overdue.

**Non-goals**
- Narrowing the affected-test fallbacks and writing the gate report (part 1, the spec that adds
  `NW_GATE_REPORT` and the JSON CLI mode of `affected_tests.py`). This spec consumes them.
- Failed-first loop rounds (part 2, [SPEC-0358](./0358-failed-first-loop-rounds.md)): the
  `failed_first` gate key and the prelude run.
- Coverage-based or predictive selection (SPEC-0328 phase 3).
- Batched close trains or bisecting (SPEC-0328 phase 2).
- Changing `merge.docs_only_paths` or SPEC-0350 `paths`.
- Reuse for `nw queue merge` campaigns. They have no loop tier, so they always run the close gates.

## Decision

1. **Record head, tree and report on gate payloads.** Every gate run records the worktree `HEAD` and
   `HEAD^{tree}` at its start, and carries the part-1 selection report. The campaign state keeps
   `last_green_loop`, the last green loop-tier payload.
2. **Close reuse.** A close gate with `reuse_from: <loop gate name>` is skipped when the rebase is
   clean by rules R1-R7 below. The loop gate's green result then stands for it. Every rule fails
   closed. This is opt-in per gate (the generic default is no reuse), and the escape hatches force
   the full run.
3. **Sentinel.** The queue runner runs the project's close-tier gates on the base HEAD, in a
   dedicated detached worktree, whenever reuses have accumulated (see Design). A red sentinel sets
   `force_full_close` on the project and escalates to the conductor with the list of suspect merges.
   Reuse declines while the sentinel is overdue, so reuse cannot run unchecked for long.

## Design

### 1. Recording

`run_gate` sets `NW_GATE_REPORT=<campaign>/gates/<seq:03>-<name>.report.json` for every gate. A gate
result may carry `report` (the parsed JSON, under 64 KiB; anything larger is dropped). The `gate`
ledger payload and `last_gate` gain optional `head` and `tree`: the worktree's `HEAD` and
`HEAD^{tree}` when the run started. The campaign state keeps `last_green_loop`, a copy of the last
loop-tier payload with `ok` true and no `interrupted`. Any later red loop run clears it.

### 2. Config

A gate may set `reuse_from: <name>` (validated: the gate is `when: close`, and the name is a
`when: loop` gate of the same config). The project may set `close.force_full: true` (the escape
hatch in the config), and env `NW_CLOSE_FULL=1` does the same for one runner. Both turn reuse off.
The project schema is strict (unknown keys fail), so `project.py` gains two optional top-level
sections, validated like `merge` with defaults in `PROJECT_DEFAULTS`:
- `close`: `force_full: false`, `reuse_max_age_s: 86400`, `reuse_max_delta: 200`.
- `sentinel`: `enabled: true`, `after_reuses: 3`, `max_age_s: 86400`.

### 3. Clean rebase, decided in `close._reuse_loop_green` before `G.run_all(..., tier="close")`

Let `G` be `last_green_loop.head`, `H` the rebased HEAD, `T` the target sha, `B` =
`changed_files(H vs T)` (the branch's own changes after the rebase) and `D` =
`git diff --name-only G H` (everything that differs between the gated tree and the close tree).
Reuse requires all of the following:

- **R1 enabled.** The gate has `reuse_from`. `close.force_full`, `NW_CLOSE_FULL` and the project's
  sentinel `force_full_close` are all off, and the sentinel is not overdue (it is due by the age rule
  of section 5 and has not run since).
- **R2 green source.** `last_green_loop` exists. Its `reuse_from` gate result is `ok`, has a
  `report`, and that report's `head_sha` equals `G`.
- **R3 branch unchanged.** `D ∩ B = ∅`: every file the branch changes has the same blob in `H` as in
  `G`. So no fix commits came after the green round, and the rebase changed no branch file (a
  conflict resolution or a regenerated file in `B` breaks this).
- **R4 main delta mappable.** No path in `D` is a full trigger (pyproject, lockfile, conftest, waits,
  removed module) and none is a `.nw/**` gate-definition change. The selection for `D`, computed on
  `H`, is not full.
- **R5 selection did not grow.** `sel_H(B)`, the branch's selection recomputed on `H`'s import graph,
  is a subset of the report's `tests`, or the report was full.
- **R6 no interaction.** The forward static closure of `sel_H(B)` (imports plus reference scan) on
  `H` contains no path in `D`. The forward closure of `sel_H(D)` contains no path in `B`. In words:
  the tests that proved the branch read nothing main changed, and the tests main's changes affect
  read nothing the branch changed.
- **R7 bounded age.** `G` was gated less than `close.reuse_max_age_s` ago (default 86400) and `D`
  has at most `close.reuse_max_delta` paths (default 200). A large or old delta is not worth the
  static reasoning.

`sel_H` and the forward closure come from the JSON CLI mode of `tools/affected_tests.py` (part 1).

When every rule holds, the gate is removed from the close run (like a SPEC-0350 skip) and a
`gate.reused` event is appended. Required keys: `gate`, `from_gate`, `from_gate_seq`, `gated_head`,
`close_head`, `target_sha`, `delta_count`, `selection_count`, `rules`. `rules` maps R1-R7 to `true`.
Optional keys are `selection_reason` and `full_source` (whether the source run was full). A reused
required gate counts as satisfied. When a rule fails, `gate.reuse_declined` is appended (required
keys `gate`, `rule`, `detail`) and the gate runs as today. Both events go into
`schemas.EVENT_KEYS`.

**Why this definition.** R3 and R5 make sure the branch's tested content is byte-identical and its
selection has not grown. R6 is the only new trust. It extends to the main-plus-branch combination
the same static reasoning the loop tier already trusts for the branch alone, and it fails closed
whenever the two change sets touch shared test dependencies. The failures that remain possible come
from dynamic coupling, covered under Risks, and the sentinel backs those up. The rule uses the
static graph, so the decision takes seconds, not minutes.

### 4. Sentinel (periodic full suite on base)

**Where it runs.** The queue runner gets a new step, `sentinel`, on a worker thread. It is scheduled
only when no campaign is waiting for a gate slot. It runs `gates_for(cfg, "close")` with no `paths`
skip and no reuse, on a detached worktree at `<agentic>/sentinel/<project>`, reset to the base HEAD
before each run (`worktrees.setup` runs on creation). It takes a normal host gate slot (SPEC-0309).

**State.** The project file `<agentic>/projects/<project>/gate_reuse.json` holds
`{reuses_since_green, last_green_sha, last_green_at, force_full_close, suspects}`. Every
`gate.reused` increments `reuses_since_green` and appends `{campaign, merge_sha}` to `suspects`.

### 5. When the sentinel is due

The sentinel is due when `reuses_since_green >= sentinel.after_reuses` (default 3), or when
`reuses_since_green >= 1` and `last_green_at` is older than `sentinel.max_age_s` (default 86400).
That gives at least one full run per day while reuse is in use, and none when nothing was reused.
The sentinel is **overdue** when it has been due and not run for longer than one `sentinel.max_age_s`
past that point, or when `sentinel.enabled` is false while reuses have accumulated past
`after_reuses`; rule R1 then declines reuse. This keeps reuse from running unchecked when the queue
is too busy for the sentinel to be scheduled.

**Outcome.** On green: reset the counter and suspects, record `last_green_sha`, and append the queue
event `sentinel` (`ok: true`). On red: set `force_full_close` (all later closes of the project run
full), append `sentinel` (`ok: false`, failing ids, suspects), and file an escalation to the
conductor naming the suspect merges. A green sentinel or `nw queue sentinel clear --project P`
resets the flag. `nw queue sentinel run --project P` runs it on demand.

### Dogfood config (`.nw/project.yaml`)

- `pytest` (close): `reuse_from: pytest-affected`.
- No `close.force_full`. Sentinel defaults.

### Implementation order

If the change is too large for one campaign, ship the sentinel (sections 4-5, `gate_reuse.json`,
the runner step and the CLI) first. It is idle until a reuse is recorded, so it is safe alone.
Reuse (sections 1-3 and the dogfood `reuse_from`) follows. The spec stays one unit; the order is a
sequencing hint only.

## Alternatives considered

- **Re-run only the selection at close instead of reusing.** This still costs one selected run per
  close and gives nothing that R6 does not. Reuse at zero cost is the point.
- **Reuse on equal tree hash only (`H^{tree} == G^{tree}`).** This is never true after a rebase onto
  a moved main, which is the common case. It would almost never fire.
- **Periodic full run inside the next campaign's close.** It would blame and triage an unrelated
  campaign for a regression an earlier reuse let in. A separate sentinel on base keeps the
  attribution right.

## Acceptance criteria

- [ ] Gate results carry `report` (parsed JSON under 64 KiB, larger dropped), and `gate` payloads and
      `last_gate` carry `head` and `tree`. `last_green_loop` holds the last green, non-interrupted
      loop payload, and a later red loop run clears it.
- [ ] A close gate with `reuse_from` is skipped and a `gate.reused` event with all required keys is
      recorded only when R1-R7 hold. Each failing rule appends `gate.reuse_declined` naming that
      rule, and the gate runs.
- [ ] `close.force_full`, `NW_CLOSE_FULL=1`, a red sentinel and an overdue sentinel each force the
      full close run (a `gate.reuse_declined` with rule R1).
- [ ] `reuse_from` is validated (it names a loop gate and sits on a close gate), and the `close` and
      `sentinel` project sections are validated with their defaults; unknown keys in them fail.
- [ ] Both new events are in `schemas.EVENT_KEYS` with their required keys.
- [ ] The sentinel runs the close gates on a detached base worktree when due by reuse count or age,
      never while a campaign waits for a slot. Red sets `force_full_close` and escalates with
      suspects. Green clears the counter and the flag. `nw queue sentinel run|clear` work.
- [ ] The dogfood `.nw/project.yaml` sets `reuse_from`. `docs/product/capabilities/agentic.md`
      documents `reuse_from`, `close.force_full` and the sentinel.

## Test plan

- **Automated: reuse** (`tests/test_agentic_close_reuse.py`, close fixtures as in
  `test_agentic_close.py` / `test_agentic_gate_paths.py`): a clean rebase onto unrelated main commits
  gives `gate.reused` with every key, and the pytest gate does not run. Then one test per rule for
  the decline: a reuse flag that is off or forced, or an overdue sentinel (R1), no or stale green
  (R2), a fix commit after green or a conflict resolution in a branch file (R3), a main delta with
  `pyproject.toml` or `.nw/project.yaml` (R4), a grown selection (R5), main changing a module a
  branch test imports, or a test reading a branch data file (R6), old or large (R7).
- **Automated: recording** (`tests/test_agentic_gates.py`): `NW_GATE_REPORT` is set per gate, a
  report under the cap is attached and one over it is dropped, `head`/`tree` appear on the payload
  and `last_gate`, `last_green_loop` is set by a green loop run and cleared by a later red one.
- **Automated: schema and config**: a schema test for both events, and `reuse_from`, `close` and
  `sentinel` validation in `tests/test_agentic_project.py`.
- **Automated: sentinel** (`tests/test_agentic_sentinel.py`): due and not-due by count and age; the
  overdue case; no scheduling while a slot waiter exists; green resets; red sets `force_full_close`,
  escalates and makes the next close decline with R1; the CLI `run`/`clear`; a deleted
  `gate_reuse.json` is rebuilt as zero and a single reuse makes the sentinel due.
- **Manual verification:** (a) Run one dogfood code campaign through the queue and record close-gate
  wall time, and whether a `gate.reused` or `gate.reuse_declined` event appeared and with which rule.
  (b) `nw queue sentinel run --project Meta-Tools` on main: green, with the state file reset.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus
  `test_agentic_close*`, `test_agentic_gate*`, `test_agentic_queue*`, `test_agentic_project`,
  `test_agentic_dogfood_config`; `python3 tools/spec_lint.py` exits 0; the full suite runs at the
  queue's close gate (this change edits `.nw/project.yaml`, so R4 declines reuse for itself).

## Risks

- **Dynamic coupling the static graph misses** (a plugin registry, `importlib` by computed name, env
  or config read through a helper, test ordering or shared global state). A reused close can merge a
  regression. *Mitigation:* R6 fails closed on any shared static dependency. The sentinel runs the
  full suite at most 3 reuses or 24 h later and names the suspect merges; an overdue sentinel turns
  reuse off. A red sentinel turns reuse off project-wide until main is green.
- **Flaky tests** let a reused green hide a flake that the full run would have hit. This is
  accepted: SPEC-0293 already tracks flakes, and the sentinel retries through the normal ladder.
- **State loss** (`gate_reuse.json` deleted) loses the counter. It is rebuilt as zero with
  `last_green_at` unknown, which makes the sentinel due as soon as one reuse happens (it fails toward
  running).

## Clock Log

CLOCK-IN: n/a
CLOCK-OUT: n/a

## Rollout / migration

1. Ship the recording (`report`, `head`, `tree`, `last_green_loop`), the `close` and `sentinel`
   config sections and the sentinel (default on, idle until the first reuse). Reuse is **off** by
   default: no `reuse_from` means no reuse.
2. In the same change or right after, turn on `reuse_from` in the dogfood `.nw/project.yaml`.
   Campaigns already in flight pick it up at their next close, because main's config is read. Their
   first close declines with R2, since earlier loop runs have no report.
3. Watch `gate.reused` / `gate.reuse_declined` counts and sentinel results for a week. Tune
   `after_reuses` and `reuse_max_delta` from evidence and record the result in *As shipped*.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual steps performed and recorded.
- [ ] No regressions (queue close gate full suite).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated.

## Open questions

None.
