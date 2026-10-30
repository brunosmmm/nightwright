---
id: SPEC-0290
title: "Automatic spec splitting: a split review turns into child specs written by a split campaign"
status: done
owner: user
created: 2026-10-30
updated: 2026-10-30
milestone: "M9: agentic workflow"
kind: feature
source_idea: IDEA-065
parent: SPEC-0169
depends_on: [SPEC-0208, SPEC-0215, SPEC-0279]
tags: [agentic, queue, specs, review]
---

## Context

Promoted from idea `IDEA-065`. Part 1 of 2. Queueing the children this spec writes is
[SPEC-0294](./0294-queue-a-split-campaign-s-child-specs-on-merge.md).

Today a `split` spec review is a dead end:

- `src/wt/agentic/spec_review.py:check_admissible` refuses a spec whose latest review is
  `split` ("split the spec and queue the parts"). `wt queue add` stops there, and the
  conductor (skill row "One spec review round") has to split by hand.
- The reviewer's verdict already carries the plan: `suggested_splits: [{title, scope}]`
  (`personas.SPEC_REVIEWER_SCHEMA`).
- The only automated reaction to a split is in revise mode:
  `src/wt/agentic/spec_write.py:write_rules` narrows the spec to its **first** part and lists
  the rest under Non-goals. The other parts are lost unless someone re-files them.
- The pieces a real split needs already exist. The spec-number allocator
  (`spec_numbers.allocate`, SPEC-0208) reserves numbers across worktrees. The spec writer step
  (`queue._prepare_spec_write` / `_do_write_spec` / `_apply_spec_write`, SPEC-0215) writes,
  validates, regenerates and commits one spec file in a campaign worktree. SPEC-0279's
  `revise_pending` admission shows how `wt queue add` can admit a spec that the writer
  changes first.

## Goals / Non-goals

**Goals**
- `wt queue add SPEC-X`, when the latest review of an internal spec is `split` for its
  current text with 2–5 `suggested_splits`, admits a **split campaign** instead of refusing.
- The split campaign writes one child spec per suggested split. It runs one spec-writer run
  per part, each part file gets its number from the SPEC-0208 allocator, and the wiring is
  fixed:
  - `status: accepted`, `kind: feature`;
  - `supersedes: [SPEC-X]`;
  - `parent` copied from SPEC-X;
  - part 1 keeps SPEC-X's `depends_on`; part k > 1 has `depends_on: [part k-1]`.
- After the last part, SPEC-X is marked `superseded` with `superseded_by: <part 1>`. The
  ledger is regenerated and the campaign goes straight to CLOSING. It has no implementer, no
  reviewers and no follow-ups. The children merge to base like any campaign branch.
- A split campaign is ready as soon as the original is still `accepted` and the review sha
  still matches. It does not wait for the original's `depends_on`: writing spec files needs none
  of them built.
- A spec that `depends_on` the original is not made ready by the supersession. It waits until
  every spec that supersedes the original (all parts) is `done` or `superseded`.

**Non-goals**
- Queueing the children after the merge (SPEC-0294).
- The in-loop spec review of idea campaigns (SPEC-0214, not built). Its split case keeps
  SPEC-0215's narrowing rule until it can route to this split mode. That is a follow-up.
- Outbound (`PROJ-NNNN`) specs. They are still refused, because their target repo's numbering
  is not ours.
- Converting SPEC-X into an epic. Superseding is the repo's one mechanism for replacing a past
  decision (AGENTS.md), and it keeps the ledger a faithful timeline.

## Decision

Treat a split as a campaign of `kind: "split"`, which reuses the spec-writer step in a new
`split` mode, once per part. The flow is:

1. QUEUED → OPENING → SPEC_WRITING.
2. SPEC_WRITING repeats once per part.
3. A deterministic finalize step (no persona) marks the original superseded.
4. CLOSING → MERGED.

Close treats a split campaign like a merge request (SPEC-0233): no spec-change guard and no
spec DoD on the original. Instead it runs a **split check**: every part file exists and lints,
and the original is `superseded` by part 1. The regular gates (spec-lint, pytest) still run
in the re-gate.

Dependents of the original follow the supersession (Design, "backlog readiness"): a
`superseded` dependency counts as met only once the specs that supersede it are built. This
closes the hole where marking SPEC-X `superseded` would release every spec waiting on SPEC-X
before any part exists in code.

A chain (`part k depends on part k-1`) is the dependency default. The reviewer lists parts in
build order, and a chain never lets a later part start on a missing foundation. It costs some
parallelism, and a human can relax a child's `depends_on` before it is queued.

## Design

- `src/wt/agentic/spec_review.py:check_admissible`: in the `verdict == "split"` branch, when
  `rsha == sha`, the spec is internal (`not SP._is_outbound_spec_id`) and
  `2 <= len(suggested_splits) <= 5`, return `{**latest, "admission": "split_pending",
  "current_sha": sha}`. Every other split case keeps the current refusal. The message names
  the reason (outbound, stale review, or a part count out of range).
- `src/wt/agentic/queue.py:plan_add`: `review == "split_pending"` adds to the plan
  `split = {"review_sha", "parts": [{"title", "scope"}]}`. The profile comes from
  `select_profile(review=None)`, the default profile. The review's size describes the unsplit
  implementation, not the writing work.
- `queue.apply_add`: with `plan["split"]`:
  - It creates the campaign with `kind: "split"`, `spec.id = spec_id` and
    `state["split"] = {"original": spec_id, "original_rel": spec_rel_path, "review_sha",
    "parts": [{"title", "scope", "number": None, "rel_path": None, "spec_id": None}]}`.
    `original_rel` is the plan's `spec.rel_path`, captured once at admission. Finalize and
    `split_check` read the original's path only from `state.split.original_rel`, never from
    `state.spec`, which other code paths may rewrite.
  - It appends `admitted_split` `{review_sha, parts}`. Register the event in
    `schemas.EVENT_KEYS` as `("review_sha", "parts")`.
- `queue.backlog_readiness` (backlog readiness), two changes:
  - For `kind == "split"` it checks only that the spec file at `state.split.original_rel` is
    still `accepted` and that the spec's current sha still equals `state.split.review_sha`
    (otherwise `["spec changed since the split review"]`). It skips the `depends_on` loop, so a
    split of a spec whose dependency is not built is still ready.
  - For every other campaign, the `depends_on` loop uses a new helper
    `dependency_met(specs_dir, dep_id, dep_fm, seen)` instead of `dep_status in
    DEPENDENCY_SATISFIED`. `done` is met. `superseded` is met only when every replacement is
    met, recursively (`seen` guards cycles). The replacements are the `superseded_by` target
    plus every spec in `specs_dir` whose `supersedes` lists `dep_id`. When no replacement can be
    found or read, a `superseded` dependency stays met, as today. Any other status is unmet.
    The blocker text names the pending replacement, for example `waiting on SPEC-X (superseded;
    waiting on SPEC-A, SPEC-B)`. `_dangling` (`queue.py:4798`) is unchanged: a superseded
    dependency with unbuilt replacements is not dangling, because the parts are queued or can be
    queued.
- `queue.decide` step decision for SPEC_WRITING (the branch that returns `write_spec`). The
  `kind == "split"` check comes first, **before** the park
  `spec.id and not pending_brief` → `spec_review_unavailable` (`queue.py:185-187`). A split
  campaign sets `spec.id`, so the order is what keeps it from being parked:
  - If a part has no `spec_id`, return `write_spec` (cost-cap check as today).
  - Otherwise return the new `Step("finalize_split")`.
  - The `open` apply sends `kind == "split"` to SPEC_WRITING.
- `queue._prepare_spec_write`, for `kind == "split"`: the split branch comes first, **before**
  the `admission == "revise_pending"` and `elif spec.get("id")` → `revise` branches
  (`queue.py:1186-1189`), so a split campaign is never put into revise or amend mode. The mode
  is `split`, applied to the first part that has no `spec_id`.
  - The number comes from `SN.allocate(cfg, by="queue", campaign=cid, specs_dir=worktree /
    "docs/specs")`.
  - The path is `docs/specs/NNNN-<slugify(part title)>.md`.
  - The prompt values get `spec_text` = the original spec's text.
  - `write_rules("split", …)` is extended to state:
    - the exact front matter (`id`, `status: accepted`, `kind: feature`, `owner: user`,
      `created`, `title`, `supersedes`, `parent`, `depends_on` per the Decision, and
      `source_idea` copied from the original when it has one);
    - "part k of n; your scope: <scope>; the other parts (titles + scopes) are Non-goals";
    - "carry over only this part's Context, Design, acceptance criteria and Test plan
      items".
- `src/wt/agentic/spec_write.py`:
  - `snapshot(..., mode="split")` behaves like `write`.
  - `_content_problems` dispatches mode `split` to a new `_split_problems(fm, expected)`. It
    requires the exact `id`, `status`, `kind`, `supersedes`, `parent` and `depends_on` values
    passed in `spec_write["expected_fm"]`, which `_prepare_spec_write` records.
- `queue._apply_spec_write`, for mode `split`:
  - It records `number`, `rel_path` and `spec_id` on the part.
  - It returns `finish("SPEC_WRITING", "split_part_written")`, emitting
    `spec_written` with `mode: "split"` as today.
  - Problems and retries are unchanged (`SPEC_WRITE_RETRY_MAX`, then
    `spec_write_failed`).
- New worker step `_do_finalize_split` (main thread is not needed; it touches only the
  worktree):
  - New `SW.supersede(worktree, original_rel, superseded_by)` rewrites only the original's
    front matter: `status: superseded`, `superseded_by`, `updated: today`. The caller passes
    `original_rel = state.split.original_rel` and `superseded_by = parts[0].spec_id`.
  - It then runs `SW.regenerate` (the ledger) and commits
    `wt(<cid>): split SPEC-X into A, B[, …]`.
  - Its apply emits `split_finalized` `{original, parts}` (register it in `EVENT_KEYS`) and
    finishes `CLOSING`, reason `split_written`. A failure parks `spec_write_failed` with
    `resume_to: SPEC_WRITING`, and the next step retries the finalize, which is idempotent.
- `src/wt/agentic/close.py`:
  - New `_is_spec_less(state)` returns `kind in ("merge", "split")`. `prepare_close` uses it
    where it now skips `spec_guard` and `spec_dod` for merge requests.
  - For `kind == "split"`, it instead runs `split_check(worktree, state)`: each part file
    exists and passes `verify_spec_file` lint, ignoring status, and the original's front
    matter (found at `state.split.original_rel`) has `status: superseded` and
    `superseded_by == parts[0].spec_id`. A failure is a `must_fix` finding, and the campaign
    parks `spec_write_failed` with `resume_to: "SPEC_WRITING"` (there is no triager for a
    split).
  - Resuming from that park goes back to SPEC_WRITING, and the step decision picks the work:
    a part without a `spec_id` is written, and when every part has one the finalize step runs
    again. Finalize is idempotent and re-supersedes the original if it is not yet superseded,
    so a failed check on the original self-heals on resume.
  - A human repairs a missing or bad part by clearing that part's `spec_id`, `number` and
    `rel_path` in `state.split.parts` (and deleting its file if one exists in the worktree),
    then resuming. The part is rewritten with a new number and finalize runs again.
- `queue._do_prepare_close`: skip `FU.file_followups` for `kind == "split"`, as it does for
  merge.
- `wt queue watch` / `wt campaign show`: the kind column shows `split`, and the campaign
  detail lists the parts with their ids (`part k: SPEC-NNNN <title>`, or `part k: (pending)
  <title>` before it is written).

## Alternatives considered

- **One writer run that writes all parts.** Rejected. `validate_spec_change` relies on "exactly
  one file changed", and one bad part would roll back all of them. Per-part runs retry
  independently.
- **Convert the original into an epic.** Rejected. It rewrites an accepted spec's kind.
  Superseding is the documented mechanism, and the children inherit the original's parent.
- **No dependencies between parts (let the human wire them).** Rejected. Unwired children
  would start in parallel on a foundation that does not exist yet. The chain is a safe default.
- **Keep narrowing to part 1 (today).** Rejected. It loses the other parts.

## Acceptance criteria

- [x] `check_admissible` returns `admission: "split_pending"` for an internal spec whose latest
      review is `split` for its current sha with 2–5 suggested splits. Outbound specs, stale
      reviews and part counts outside 2–5 are still refused, with a message naming the reason.
- [x] `wt queue add` of such a spec creates a `kind: "split"` campaign with `state.split.parts`
      and an `admitted_split` ledger event. Opening it leads to SPEC_WRITING.
- [x] Each part is written by its own spec-writer run in `split` mode, with a number from the
      SPEC-0208 allocator. Its front matter is validated to the exact wiring: `accepted`,
      `supersedes: [original]`, the original's `parent`, and the dependency chain. A violation
      gets the existing retry and then a `spec_write_failed` park.
- [x] After the last part, the finalize step marks the original `superseded` with
      `superseded_by: <part 1>`, regenerates the ledger, commits, emits `split_finalized` and
      moves to CLOSING. It is idempotent on retry.
- [x] Close skips the spec guard, the spec DoD, reviewers and follow-ups for a split campaign,
      and runs the split check instead. A passing campaign merges the children and the
      superseded original to base.
- [x] A failed split check parks `spec_write_failed` with `resume_to: SPEC_WRITING`. Resuming
      after a part's `spec_id` was cleared rewrites that part, runs finalize again and returns to
      CLOSING.
- [x] A split campaign is never parked `spec_review_unavailable` and never prepared in
      `revise` or `amend` mode: its SPEC_WRITING step returns `write_spec` or `finalize_split`.
- [x] `backlog_readiness` for a split campaign ignores the original's `depends_on` and requires
      only that the original is `accepted` and the review sha is unchanged. A split of a spec
      whose dependency is not done is ready.
- [x] A spec that `depends_on` a `superseded` original is not ready until every spec that
      supersedes the original is `done` or `superseded`. With no readable replacement, a
      `superseded` dependency is met as before.
- [x] `state.split.original_rel` is recorded at admission, and finalize and the split check use
      it instead of `state.spec`.
- [x] `wt queue watch` and `wt campaign show` show kind `split`, and `campaign show` lists each
      part with its id (or `(pending)`).

## Test plan

- `tests/test_agentic_spec_review.py`:
  - `test_split_admitted_as_split_pending`;
  - `test_split_outbound_refused`;
  - `test_split_stale_sha_refused`;
  - `test_split_one_part_refused` and `test_split_six_parts_refused`.
- `tests/test_agentic_queue.py`, using the fake runner (`agentic.runner: fake`) whose
  scripted spec writer writes the requested file from a fixture:
  - `test_add_split_creates_split_campaign`: `kind`, `state.split.parts`, the
    `admitted_split` event and the default profile.
  - `test_split_campaign_writes_each_part`: there are 3 parts. There are three `write_spec`
    runs, the three files exist with consecutive allocated numbers, and part 2 has
    `depends_on: [part 1]`.
  - `test_split_part_wrong_wiring_retried_then_parked`, parametrized over the wrong key
    (`supersedes` missing, `depends_on` wrong, `parent` wrong, `status` not `accepted`): the
    fake writer writes a part with that one key wrong. It retries once, then parks
    `spec_write_failed`.
  - `test_split_step_never_parks_or_revises`: a `kind: "split"` campaign in SPEC_WRITING with
    `spec.id` set and no `pending_brief` gets `write_spec` (not `spec_review_unavailable`) while
    a part is unwritten and `finalize_split` after the last. `_prepare_spec_write` returns mode
    `split`, also when `admission` is `revise_pending`.
  - `test_split_readiness_ignores_depends_on`: the original has `depends_on` on a spec that is
    not done. `backlog_readiness` of the split campaign is ready. It is not ready when the
    original is no longer `accepted` or when its sha differs from `review_sha`.
  - `test_dependent_of_split_original_waits_for_parts`: SPEC-D `depends_on` SPEC-X. After
    finalize (X `superseded`, parts `accepted`) SPEC-D is not ready and the blocker names the
    parts. With one part still `accepted` it stays blocked. With every part `done` it is ready.
  - `test_superseded_dependency_without_replacement_is_met`: a `superseded` dependency that no
    spec lists in `supersedes` and with no readable `superseded_by` stays met (old behaviour).
  - `test_split_check_failure_resumes_to_spec_writing`: a failed split check parks
    `spec_write_failed` with `resume_to: SPEC_WRITING`. After clearing a part's `spec_id`,
    resuming rewrites that part, finalizes again and reaches CLOSING.
  - `test_split_original_rel_recorded`: `state.split.original_rel` equals the plan's spec path.
    Rewriting `state.spec.rel_path` before finalize does not change which file is superseded.
  - `test_finalize_split_supersedes_original`: the original has `status: superseded` and
    `superseded_by` = part 1, the ledger is regenerated, the commit exists, `split_finalized`
    is emitted and the state is CLOSING.
  - `test_finalize_split_idempotent`: running the finalize twice gives one superseded
    front-matter change and no error.
  - `test_split_close_skips_review_and_followups`: the campaign reaches MERGED with no
    reviewer run and no `followup` events. `python3 tools/spec_lint.py` passes on base after
    the merge.
- `tests/test_agentic_close.py`:
  - `test_split_check_missing_part_fails`;
  - `test_split_check_original_not_superseded_fails`;
  - `test_split_skips_spec_guard`.
- `tests/test_agentic_queue_cli.py` and `tests/test_agentic_campaign_cli.py`: a `kind: "split"`
  campaign with two written parts and one pending part. `wt queue watch --once` shows kind
  `split`. `wt campaign show <id>` lists `part 1: SPEC-NNNN <title>` and `part 3: (pending)
  <title>`.
- `tests/test_agentic_spec_writer.py`: `_split_problems` reports each wrong key. `write_rules
  ("split", …)` names the scope, the other parts and the exact front matter.
- **Sabotage check:**
  - Make `_split_problems` accept any `depends_on`.
    `test_split_part_wrong_wiring_retried_then_parked[depends_on wrong]` must fail.
  - Skip the finalize step. `test_split_check_original_not_superseded_fails` and
    `test_finalize_split_supersedes_original` must fail.
  - Revert `dependency_met` to `status in DEPENDENCY_SATISFIED`.
    `test_dependent_of_split_original_waits_for_parts` must fail.
  - Remove the `kind == "split"` early return from `backlog_readiness`.
    `test_split_readiness_ignores_depends_on` must fail.
- **Regression guard:** full `uv run pytest`. Approve, revise and revise-pending admission
  tests are unchanged.

### Test record (2026-10-30)

Ran, all passing:

- `uv run pytest tests/test_agentic_split.py` (18 tests: admission, writing each part, the
  parametrized wrong-wiring cases, the step ordering, split readiness, dependents of a
  superseded original, finalize and its idempotence, close to MERGED, the failed-check resume);
- `uv run pytest tests/test_agentic_spec_review.py` (split admission, outbound, stale, 1 and 6
  parts);
- `uv run pytest tests/test_agentic_close.py -k split` (split check, broken chain, spec guard
  skipped);
- `uv run pytest tests/test_agentic_spec_writer.py` (`_split_problems`, `write_rules("split")`,
  snapshot), `tests/test_agentic_dashboard.py`, `tests/test_agentic_campaign_cli.py` (kind
  `split` in `queue watch`, the parts in `campaign show`);
- regression: `tests/test_agentic_queue.py`, `test_agentic_close.py`, `test_agentic_e2e.py`,
  `test_spec_numbers.py`, `test_agentic_idea_campaign.py`, `test_agentic_queue_merge.py`,
  `test_agentic_events.py`, `test_agentic_queue_cli.py`;
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py`.

Sabotage checks run by hand, each failing the named test: `_split_problems` ignoring
`depends_on` (the `depends_on wrong` case), `dependency_met` reverted to
`status in DEPENDENCY_SATISFIED` (both dependents-of-original tests), the split early return
removed from `backlog_readiness` (`test_split_readiness_ignores_depends_on`). Skipping the
finalize step leaves the original `accepted`, so `test_split_close_skips_review_and_followups`
(it must reach MERGED through the split check) fails; `test_finalize_split_supersedes_original`
asserts the superseded original directly.

The full `uv run pytest` run passed too (3660 passed, 3 skipped; `tests/test_spec_review_store.py`
ran separately after a message-wording fix: 89 passed together with `test_agentic_spec_review.py`).

### As shipped

- Tests for the queue behaviour live in a new `tests/test_agentic_split.py` (reusing the
  spec-writer harness) instead of `tests/test_agentic_queue.py`; the test names are the ones in
  the Test plan.
- `spec_numbers.allocate` is idempotent per `campaign`, so every part is reserved under
  `<campaign>#<k>` (one reservation per part). `spec_numbers._abandoned` strips the `#k`
  suffix so an abandoned split campaign still frees its reservations.
- The finalize step is a pool task like the gate (`Step("finalize_split")`), not a persona run;
  it is also deferred while the runner drains.
- `split_check` also verifies the chain (part k > 1 has `depends_on: [part k-1]`). A human who
  repairs a middle part clears that part and every later part (their `depends_on` names it),
  not only the one; the allocator hands out numbers again (a part that still holds its
  reservation keeps its number).
- A split campaign whose re-gate fails parks `gates_failed` (like a merge request); it has no
  triager. `_reconcile_closed` is skipped for it (no idea tracks the parts).
- In `wt queue watch` the active card gets a `kind split` row (the table views show `split`
  under the campaign id); `wt campaign show` prints `split of SPEC-X into N parts` and one
  `part k: <id or (pending)> <title>` line per part.
- `_split_problems` treats an absent `parent` as expected when the original has none. The
  writer prompt's `{{idea_text}}` is replaced by a one-line note and `{{spec_text}}` is the
  original spec's text.

## Rollout / migration

New admission path. Specs with a stale split review are still refused as before. Campaigns
of other kinds are unaffected, with one deliberate exception: `dependency_met` applies to every
spec, so a hand-superseded dependency now also waits for the specs that supersede it. A
dependent of a dependency superseded by hand, whose replacement is not yet `done`, becomes
blocked where it was ready before; that is the intended fix, not a regression. The CLI change
(kind `split`, parts listing) is additive.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
