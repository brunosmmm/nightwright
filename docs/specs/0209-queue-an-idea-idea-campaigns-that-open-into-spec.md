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
- Every queue view and step handles a campaign whose `state.spec.id` is None.
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
  - **Refusals (ValueError):**
    - an unknown idea, or a closed one (anything other than `IDEA`, `INCUBATE`, or `SPECCED`
      without `:SPEC:`);
    - an idea with a `:SPEC:` (message: "queue that spec instead: wt queue add SPEC-…");
    - an open campaign for the same idea;
    - a project whose `.wt/project.yaml` doesn't load. The project is the idea's
      `:PROJECT:`, default `Meta-Tools`.
  - **On apply:**
    - campaign id `store.new_campaign_id(cfg, "IDEA-NNN")`, and `state.kind = "idea"`;
    - `state.spec_id = None` (top level, as the event envelope reads it);
    - `state.idea` = `explore.idea_show_payload` (heading, summary, questions, log, project,
      kind), plus `epic` from the idea task's `:EPIC:` (`task.properties.get("EPIC")`), plus
      `state_before` (the idea's org state);
    - `state.spec = {"id": None, "rel_path": None, "internal": True}`;
    - the idea gets `:CAMPAIGN: <campaign id>` through `org_write.set_property` (SPEC-0190
      lock).
- **Readiness:** `queue.backlog_readiness` returns `(True, [])` for `kind == "idea"` while the
  idea is open, else `(False, ["idea IDEA-NNN is <state>"])`.
- **Open:** `_do_open` skips the spec hash and the `spec_at_open.md` copy while
  `state.spec.id` is None. An idea campaign's open goes to SPEC_WRITING.
- **`plan_step`:** SPEC_WRITING → park `spec_writer_unavailable` (resume_to SPEC_WRITING).
  SPEC-0215 replaces this with the write step.
- **Specless tolerance:** each of these treats a missing `state.spec.id` / `spec_id` as "no
  spec yet" (show the idea id and heading instead) and does not crash:
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
    raises for WAITING (`queue.py` ≈1556). Both now accept PARKED, QUEUED or WAITING.
  - For a `kind: idea` campaign it also:
    - clears the idea's `:CAMPAIGN:`;
    - clears `:SPEC:` when it equals this campaign's `state.spec.id` (a different, hand-set
      one is kept);
    - restores the idea's org state to `state.idea.state_before`;
    - releases the campaign's reservation with `spec_numbers.release` when one exists
      (pruning would also catch it).
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
      duplicate campaign, unloadable project.
- [ ] The backlog shows the campaign ready, and not ready once the idea is closed.
- [ ] After admission and open, the campaign is in SPEC_WRITING with no spec hash. The next
      tick parks it `spec_writer_unavailable` (resume_to SPEC_WRITING).
- [ ] With an idea campaign in SPEC_WRITING and in that park, every command and function
      under *Specless tolerance* runs without error, and the listing shows the idea id.
- [ ] Abandoning:
  - a parked idea campaign restores the idea's state and clears `:CAMPAIGN:`. With a
    hand-set `:SPEC:` naming another spec, that `:SPEC:` is kept;
  - a WAITING campaign can now be abandoned (`apply_abandon`);
  - `wt queue add IDEA-NNN` works again afterwards.
- [ ] `render.STATE_STYLES` has `SPEC_WRITING`.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_idea_campaign.py`, using the `test_agentic_e2e`
  harness and a helper that captures an idea in the harness's org. One test per AC, and each
  sub-bullet is its own assertion. The specless-tolerance test drives CliRunner over every
  listed command, plus `events.sync` and the watchdog, on the parked fixture.
- **Regression guard:** full `uv run pytest`. Spec campaigns are unaffected, and existing
  abandon tests still pass with WAITING now allowed.

## Rollout / migration

Ships through the agentic queue. Until SPEC-0215 lands, idea campaigns park
`spec_writer_unavailable`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
