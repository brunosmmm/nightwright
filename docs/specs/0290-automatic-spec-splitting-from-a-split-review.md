---
id: SPEC-0290
title: "Automatic spec splitting: a split review turns into child specs written by a split campaign"
status: accepted
owner: user
created: 2026-10-30
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
    `state["split"] = {"original": spec_id, "review_sha", "parts": [{"title", "scope",
    "number": None, "rel_path": None, "spec_id": None}]}`.
  - It appends `admitted_split` `{review_sha, parts}`. Register the event in
    `schemas.EVENT_KEYS` as `("review_sha", "parts")`.
- `queue` step decision for SPEC_WRITING (the `decide` branch that returns `write_spec`): for
  `kind == "split"`:
  - If a part has no `spec_id`, return `write_spec`.
  - Otherwise return the new `Step("finalize_split")`.
  - The `open` apply sends `kind == "split"` to SPEC_WRITING.
- `queue._prepare_spec_write`, for `kind == "split"`: the mode is `split`, applied to the first
  part that has no `spec_id`.
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
    front matter: `status: superseded`, `superseded_by`, `updated: today`.
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
    matter has `status: superseded` and `superseded_by == parts[0].spec_id`. A failure is a
    `must_fix` finding, and the campaign parks `spec_write_failed` (there is no triager for a
    split).
- `queue._do_prepare_close`: skip `FU.file_followups` for `kind == "split"`, as it does for
  merge.
- `wt queue watch` / `wt campaign show`: the kind column shows `split`, and the campaign
  detail lists the parts with their ids.

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

- [ ] `check_admissible` returns `admission: "split_pending"` for an internal spec whose latest
      review is `split` for its current sha with 2–5 suggested splits. Outbound specs, stale
      reviews and part counts outside 2–5 are still refused, with a message naming the reason.
- [ ] `wt queue add` of such a spec creates a `kind: "split"` campaign with `state.split.parts`
      and an `admitted_split` ledger event. Opening it leads to SPEC_WRITING.
- [ ] Each part is written by its own spec-writer run in `split` mode, with a number from the
      SPEC-0208 allocator. Its front matter is validated to the exact wiring: `accepted`,
      `supersedes: [original]`, the original's `parent`, and the dependency chain. A violation
      gets the existing retry and then a `spec_write_failed` park.
- [ ] After the last part, the finalize step marks the original `superseded` with
      `superseded_by: <part 1>`, regenerates the ledger, commits, emits `split_finalized` and
      moves to CLOSING. It is idempotent on retry.
- [ ] Close skips the spec guard, the spec DoD, reviewers and follow-ups for a split campaign,
      and runs the split check instead. A passing campaign merges the children and the
      superseded original to base.

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
  - `test_split_part_wrong_wiring_retried_then_parked`: the fake writer omits `supersedes`.
    It retries once, then parks `spec_write_failed`.
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
- `tests/test_agentic_spec_writer.py`: `_split_problems` reports each wrong key. `write_rules
  ("split", …)` names the scope, the other parts and the exact front matter.
- **Sabotage check:**
  - Make `_split_problems` accept any `depends_on`.
    `test_split_part_wrong_wiring_retried_then_parked` (the variant with a wrong chain) must
    fail.
  - Skip the finalize step. `test_split_check_original_not_superseded_fails` and
    `test_finalize_split_supersedes_original` must fail.
- **Regression guard:** full `uv run pytest`. Approve, revise and revise-pending admission
  tests are unchanged.

## Rollout / migration

New admission path. Specs with a stale split review are still refused as before. Campaigns
of other kinds are unaffected.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
