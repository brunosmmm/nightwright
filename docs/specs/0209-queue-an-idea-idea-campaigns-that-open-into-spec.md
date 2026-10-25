---
id: SPEC-0209
title: "Queue an idea: idea campaigns that open into SPEC_WRITING"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, queue, specs]
source_idea: IDEA-063
depends_on: [SPEC-0200, SPEC-0208]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), from
`IDEA-063`. The user decided on 2026-10-25 that unwritten specs are part of the queue.
Inserting an IDEA runs **one campaign end to end** on one branch with one merge:
1. spec writing and revision (SPEC-0215);
2. review and in-loop acceptance (SPEC-0214);
3. the existing implement → gate → review → close loop.

This spec is the entry point and the plumbing. `wt queue add IDEA-NNN` creates an idea
campaign; it opens a worktree and enters SPEC_WRITING; the rest of wt tolerates a campaign
that has no spec yet; and abandoning it unlinks the idea. Today `wt queue add` takes only an
accepted, reviewed spec (`queue.plan_add`, SPEC-0176 / SPEC-0188).

## Goals / Non-goals

**Goals**
- `wt queue add IDEA-NNN` queues an idea as a campaign of kind `idea`, which opens into
  SPEC_WRITING.
- Every queue view and step handles an idea campaign that has no spec yet.
- The idea is linked while its campaign lives (`:CAMPAIGN:`), and unlinked on abandon.

**Non-goals**
- The spec writer itself (SPEC-0215), review and acceptance (SPEC-0214), and display
  (SPEC-0211 / SPEC-0217).
- Epic ideas: SPEC-0215's writer reports them as `blocked`.
- Outbound portable specs (IDEA-038).

## Decision

A campaign kind `idea`, with state.spec empty until a spec is written:
QUEUED → OPENING → **SPEC_WRITING**. Until SPEC-0215 lands, SPEC_WRITING parks
`spec_writer_unavailable` (resume_to SPEC_WRITING), so this spec ships safely on its own.

## Design

- **States:**
  - `SPEC_WRITING` joins `STATES`, `ACTIVE_STATES` (it holds a slot) and `RESUMABLE_STATES`;
  - `render.STATE_STYLES["SPEC_WRITING"] = "cyan"`;
  - `PARK_REASONS` gains `spec_writer_unavailable`.
- **`wt queue add IDEA-NNN [--priority N] [--profile P] [--yes] [--json]`.** The argument
  matches `^IDEA-\d+$` and dispatches to `plan_add_idea` / `apply_add_idea`; spec ids keep
  the existing path. The plan and `--json` (`wt.queue.add.v1`) carry `kind: "idea"`,
  `idea_id`, `project`, `profile`, `priority`, `campaign`, `branch`, `worktree`.
  - **Refusals (ValueError)**, checked in this order, so the first match gives its message:
    - an unknown idea;
    - an idea with a `:SPEC:` (message: "queue that spec instead: wt queue add SPEC-…").
      This runs before the closed check, so a SPECCED idea with `:SPEC:` gets this
      actionable message;
    - a closed idea (`idea_is_open` false);
    - an open campaign for the same idea: any campaign whose `state.kind == "idea"`, whose
      `state.idea.id` equals the argument, and whose state is not terminal (MERGED,
      HANDED_OFF, ABANDONED). Found by scanning `store.list_campaigns(cfg)`;
    - a project whose `.wt/project.yaml` doesn't load. The project is the idea's
      `:PROJECT:`, default `Meta-Tools`.
  - **Add lock:** a new queue-wide `store.add_lock(cfg)`, an exclusive `flock` on
    `<data_dir>/agentic/add.lock` built like `store.item_lock` (process RLock + `flock`).
    Both `apply_add` (spec campaigns, `queue.py:1996`) and `apply_add_idea` hold it around
    the duplicate re-check and `store.create_campaign`, so two concurrent adds of the same
    idea (or spec) create exactly one campaign; the second raises the duplicate refusal.
  - **On apply:**
    - campaign id `store.new_campaign_id(cfg, "IDEA-NNN")`, and `state.kind = "idea"`;
    - `state.spec_id = None` (top level, as the event envelope reads it);
    - `state.idea` = `explore.idea_show_payload` (id, heading, summary, questions, log, project,
      kind), plus `epic` from the idea task's `:EPIC:` (`task.properties.get("EPIC")`), plus
      `state_before` (the idea's org state);
    - `state.spec = {"id": None, "rel_path": None, "internal": True}`;
    - the idea gets `:CAMPAIGN: <campaign id>` through `org_write.set_property` (SPEC-0190
      lock).
- **"No spec yet"** is one predicate, `queue.has_no_spec(st)`: `st.get("kind") == "idea"` and
  `not (st.get("spec") or {}).get("id")`. It keys on `kind` first, because spec campaigns
  created before this spec have no `spec.id` field (they carry `spec_id` and
  `spec.rel_path` only), and must keep their old behaviour. Every rule below that says
  "no spec yet" uses it.
- **"Open idea"** is one predicate, `queue.idea_is_open(task)`: state `IDEA` or `INCUBATE`,
  or `SPECCED` without `:SPEC:`. Add refusals and readiness both use it.
- **Readiness:** `queue.backlog_readiness` checks `has_no_spec(st)` **first**, before it reads
  the spec file (today it reads `state.spec.rel_path` at `queue.py:484`, which would answer
  "spec file missing"). For such a campaign it returns `(True, [])` while
  `idea_is_open` holds for the idea, else `(False, ["idea IDEA-NNN is <state>"])`. An idea
  that no longer exists in the org gives `(False, ["idea IDEA-NNN not found"])`.
- **Open:** `_do_open` skips the spec hash and the `spec_at_open.md` copy when
  `has_no_spec(st)`. The open-result apply (`kind == "open"` in the result apply, today
  `finish("IMPLEMENTING", "opened")` at `queue.py:930`) picks `SPEC_WRITING` instead when
  `has_no_spec(st)`. Where that transition goes through `_enter_slot_or_wait` and no slot
  is free, the campaign goes to WAITING with `resume_to SPEC_WRITING`.
- **`plan_step`:** SPEC_WRITING → park `spec_writer_unavailable` (resume_to SPEC_WRITING).
  SPEC-0215 replaces this with the write step.
- **Specless tolerance:** each of these uses `has_no_spec(st)` (show the idea id and heading
  instead of a spec) and does not crash:
  - `queue_payload` / `wt queue list` / `wt queue backlog`;
  - `dashboard_payload` / `wt queue watch --once`;
  - `wt campaign show` / `log`;
  - `wt queue conduct-status`;
  - the events projection (`events.sync`);
  - the watchdog checks;
  - `wt spec reconcile` (it ignores campaigns without a spec).
- **Merge:** nothing extra here. Once a spec exists (SPEC-0215), the implementer sets it
  `done` as for any campaign, and the existing `wt spec reconcile` advances the idea.
  `:CAMPAIGN:` stays as a pointer to the history.
- **Abandon:**
  - `apply_abandon` accepts WAITING as `plan_abandon` already does; today `apply_abandon`
    raises for WAITING (`apply_abandon`, `queue.py:2130`). Both now accept PARKED, QUEUED or WAITING.
  - For a `kind: idea` campaign it also:
    - clears the idea's `:CAMPAIGN:`;
    - clears `:SPEC:` when it equals this campaign's `state.spec.id` (a different, hand-set
      one is kept);
    - restores the idea's org state to `state.idea.state_before` (the field `apply_add_idea`
      records; the `idea_show_payload` snapshot's own `state` is not used), **only** when
      the idea's current state is one the campaign could have left it in: still
      `state_before`, or `SPECCED` with `:SPEC:` equal to this campaign's `state.spec.id`.
      A state set by hand meanwhile (for example DROPPED) is kept, and the abandon result
      notes "idea state kept: <state>";
    - releases the campaign's reservations: `spec_numbers.list_reservations(cfg,
      specs_dir=<repo.root>/docs/specs)` (the project's base checkout, so the reservation
      key matches the one the writer used), filtered on `record["campaign"] == cid`, then
      `spec_numbers.release(cfg, record["number"], specs_dir=…)` for each. None found is a
      no-op; a ValueError from `release` (pruned concurrently) is ignored. Pruning would
      also catch it later, because the campaign is ABANDONED.
    All org writes go through `org_write`.

## Alternatives considered

- **Merge the spec to main before implementation** — rejected by the user: an extra merge per
  idea during closes (`base_moving`), and two campaigns to track per idea.
- **Link the idea only after merge** — the idea would show nothing while its campaign runs;
  rolling back on abandon is cheaper.

## Acceptance criteria

- [ ] `wt queue add IDEA-NNN --priority 2 --profile lean --yes --json`:
  - creates a QUEUED campaign of kind `idea` with the snapshot (including `epic` from
    `:EPIC:` and `state_before`) and the given priority and profile;
  - sets the idea's `:CAMPAIGN:`;
  - the dry run writes nothing.
- [ ] Each refusal errors with its message: unknown idea, closed idea, idea with `:SPEC:`,
      duplicate campaign (an open `kind: idea` campaign for the same `idea.id`; an abandoned
      one does not block), unloadable project. A SPECCED idea with `:SPEC:` gets the
      "queue that spec instead" message, not the closed one.
- [ ] Two concurrent `apply_add_idea` calls for the same idea (threads, started together
      after both plans are made) create exactly one campaign; the other raises the
      duplicate refusal. `apply_add` holds the same `add.lock`.
- [ ] A pre-existing spec campaign state without `spec.id` (only `spec_id` and
      `spec.rel_path`) is not treated as "no spec yet": its readiness still reads the spec
      file, and its open still hashes and copies the spec.
- [ ] The backlog shows the campaign ready, and not ready once the idea is closed. Readiness
      for an idea campaign never reads a spec file.
- [ ] After admission and open, the campaign is in SPEC_WRITING with no spec hash. The next
      tick parks it `spec_writer_unavailable` (resume_to SPEC_WRITING). With no free slot
      at open, it waits with `resume_to SPEC_WRITING`.
- [ ] With an idea campaign in SPEC_WRITING and in that park, every command and function
      under *Specless tolerance* runs without error, and the listing shows the idea id.
- [ ] Abandoning:
  - a parked idea campaign restores the idea's state and clears `:CAMPAIGN:`;
  - a `:SPEC:` equal to the campaign's `state.spec.id` is cleared; a hand-set `:SPEC:`
    naming another spec is kept;
  - a reservation recorded for the campaign is released (gone from `list_reservations`);
    one recorded for another campaign is kept;
  - an idea set to DROPPED by hand while the campaign was parked stays DROPPED;
  - a WAITING campaign can now be abandoned (`apply_abandon`);
  - `wt queue add IDEA-NNN` works again afterwards.
- [ ] `render.STATE_STYLES` has `SPEC_WRITING`.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_idea_campaign.py`, using the `test_agentic_e2e`
  harness and a helper that captures an idea in the harness's org. One test per AC, and each
  sub-bullet is its own assertion. The abandon tests set `:SPEC:` to the campaign's
  `state.spec.id` (and, separately, to another id) and reserve a number with
  `spec_numbers.allocate(..., campaign=cid)` (and one for another campaign) before
  abandoning. The legacy-state test writes a spec campaign state without `spec.id`. The
  WAITING abandon uses its own fixture, `waiting_idea_campaign` (an opened idea campaign
  moved to WAITING via `_enter_slot_or_wait` with zero free slots). The concurrency test
  runs two threads on `apply_add_idea` with a barrier. The specless-tolerance test drives CliRunner over every
  listed command, plus `events.sync` and the watchdog, on the parked fixture.
- **Regression guard:** full `uv run pytest`. Spec campaigns are unaffected, and existing
  abandon tests still pass with WAITING now allowed.

## Rollout / migration

Ships through the agentic queue. Until SPEC-0215 lands, idea campaigns park
`spec_writer_unavailable`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`). (No manual steps.)
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
