---
id: SPEC-0214
title: "Spec review loop inside the campaign, with in-loop acceptance"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, queue, specs, review]
source_idea: IDEA-063
depends_on: [SPEC-0200, SPEC-0210, SPEC-0215]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), from
`IDEA-063`, and the last step of "one campaign end to end" (SPEC-0209 → 0215 → **0214**).
After SPEC-0215, an idea campaign writes its spec and parks `spec_review_unavailable`. This
spec adds the review:
- the SPEC-0188 spec reviewer reviews the spec in the worktree;
- `revise` or `split` sends it back to SPEC_WRITING (SPEC-0215's revise mode), up to a round
  limit;
- `approve` accepts it inside the loop, and the campaign continues to IMPLEMENTING on the same
  branch.
It uses SPEC-0210's `review_context`, `review_payload` and store.

There are two spec states, no human-accept path, and three park reasons for the spec
phase.

## Goals / Non-goals

**Goals**
- An idea campaign goes SPEC_WRITING → SPEC_REVIEW → (SPEC_WRITING → SPEC_REVIEW)* →
  IMPLEMENTING without a human.
- The spec phase's own failure modes use three park reasons:
  - `spec_write_failed` (SPEC-0215);
  - `spec_review_exhausted` (real exhaustion, plus the dirty, reviewer-failed and
    accept-failed cases, told apart by detail and `resume_to`);
  - `cost_cap`.
  Generic parks (`human`, `config_invalid`) still apply. The two legacy reasons
  (`spec_writer_unavailable`, `spec_review_unavailable`) are no longer produced by the
  normal flow.

**Non-goals**
- Automatic splitting (IDEA-065). A split is a revise with a narrowing rule.
- A human approval step before acceptance. Humans step in through parks and `--note`.
- Display (SPEC-0211, SPEC-0217).

## Decision

- Add state `SPEC_REVIEW`, with step `Step("spec_review", persona="spec_reviewer")`.
- Acceptance is a deterministic step at the end of a successful review, applied by `_apply`.
- A successful SPEC_WRITING commit (SPEC-0215) now transitions to SPEC_REVIEW instead of
  parking `spec_review_unavailable`.

## Design

- **State and reasons:**
  - `SPEC_REVIEW` joins `STATES`, `ACTIVE_STATES` and `RESUMABLE_STATES`, with
    `render.STATE_STYLES` set to magenta. `_resume_target` therefore maps a human park in
    SPEC_REVIEW (or SPEC_WRITING) to that same state;
  - `PARK_REASONS` gains `spec_review_exhausted`;
  - `spec_review_unavailable` stays defined. The normal flow no longer produces it, because
    every route into revise mode sets `pending_brief`. A resume of that park (legacy, or
    SPEC-0215's empty-brief guard) goes to SPEC_WRITING when a `--note` is given, so the
    writer addresses the note before the review. Without a note, `plan_resume` sends it to
    SPEC_REVIEW instead of its stored `resume_to`. An explicit `--to` still wins. The
    SPEC_REVIEW path never evaluates the
    empty-brief guard;
  - `SPEC_REVIEW_RETRY_MAX = 1`;
  - `_apply.finish`'s copied-key whitelist gains `spec_review`;
  - builtin profiles gain `spec_review_max: 3`, the maximum number of reviews per campaign
    since the last `--reset-loops`.
- **`plan_step` for SPEC_REVIEW:** the cost cap first. `_cap_blocks(state, profile,
  "spec_reviewer")`, using the profile's `spec_reviewer.max_budget_usd`, parks `cost_cap`
  with resume_to SPEC_REVIEW (resume with `--raise-cap`). Otherwise
  `Step("spec_review", persona="spec_reviewer")`. SPEC-0215 does the same for SPEC_WRITING
  with `spec_writer`.
- **Worker exceptions:** SPEC-0215 changes `tick`'s pool exception handler to keep the
  submitted step, so an exception in the `spec_review` worker reaches this spec's
  `spec_review` row of the error table (park `spec_review_exhausted`, resume_to
  SPEC_REVIEW) instead of being recorded as an implementer failure.
- **Request:** `build_request` for `spec_reviewer` uses `spec_review.review_context(<worktree
  spec>, spec_id, idea_text=<state.idea as JSON>)`, read-only with `cwd = add_dirs =
  (worktree,)`. The system prompt is a new `SPEC_REVIEW_CAMPAIGN_INVARIANT` ("You are the
  spec-review step of campaign {{campaign}} on {{spec_id}}, in its worktree. Read only.
  Finish with the JSON verdict."). `build_request` gains an `invariant=` keyword that
  overrides `p.invariant`, and the in-campaign call passes `SPEC_REVIEW_CAMPAIGN_INVARIANT`.
  The `spec_reviewer` persona keeps the standalone `SPEC_REVIEW_INVARIANT` for `wt spec
  review`.
- **Worker:**
  0. If the spec file in HEAD already has `status: accepted` (an earlier accept committed,
     then failed in steps 4–6), no review runs. The step returns `accepted`, and `_apply`
     runs accept step 0 (the human-change check), then steps 4–6. No run is spent and no
     round is counted.
  1. It checks for no tracked changes (`gitops.is_clean(worktree, untracked=False)`). Tracked
     changes make the step return `dirty`, and no run starts.
  2. Otherwise it records S, the spec file's sha, and H, the worktree HEAD, then runs the
     reviewer. If HEAD moved, the sha changed, or any tracked file changed during the
     read-only run, the worker runs `git reset --hard H`. That restores HEAD and every
     tracked file, and is safe because step 1 guaranteed a clean start; untracked files are
     untouched. The run counts as failed with reason "reviewer modified the worktree", so the
     retry starts clean.
  3. It returns S and the run result.
- **`_apply`** has its own `kind == "spec_review"` branch. It records the run (cost,
  interrupt) as the persona branch does, then:
  - **Dirty:** park `spec_review_exhausted`, detail "commit or discard the worktree changes,
    then resume", resume_to SPEC_REVIEW.
  - **A failed run** (runner error, timeout, non-conforming verdict, `_check_verdict` failure,
    or a sha change):
    - `counters.spec_review_retries += 1`;
    - retry while `spec_review_retries <= SPEC_REVIEW_RETRY_MAX`, else park
      `spec_review_exhausted`, detail "reviewer failed: <reason>", resume_to SPEC_REVIEW.
  - **A conforming verdict:**
    1. `save_review` the payload for S;
    2. append ledger `spec_review {verdict, spec_sha256: S, round, counts: {must_fix, major,
       minor, nit}, run_id}`, added to `schemas.EVENT_KEYS` with those five keys. `round` is
       1 + the number of earlier `spec_review` events, so it keeps growing across
       `--reset-loops`;
    3. `state.spec_review = {round, verdict, sha: S}`, `counters.spec_rounds += 1`, and
       `spec_review_retries = 0`.
  - **Routing after a conforming verdict** (checked after the `spec_rounds` increment):
    - **approve → accept** (on the main thread, under the campaign lock). It is deterministic
      and quick: a status edit, the regenerate commands (each with SPEC-0191's command
      timeout; for wt, about a second) and one commit. A human park requested meanwhile is
      honored by `finish` after the accept, so the campaign parks already accepted, with
      resume_to IMPLEMENTING. Steps 5–7 are idempotent, so a failure after the commit is
      retried by worker step 0:
      0. re-read the on-disk state under the lock. If it is no longer SPEC_REVIEW (a human
         changed it), skip the accept and let `finish` record `superseded_by_human`;
      1. `specs.set_spec_status(<file>, "accepted")`; only the `status:` and `updated:` lines
         change;
      2. run SPEC-0215's generated-files step (the project's `merge.regenerate` commands;
         for wt, `docs/LEDGER.md`);
      3. `git add <file> <changed generated paths>`, then commit `wt(<cid>): accept SPEC-NNNN
         (round R)`. If regeneration or the commit fails, restore those paths (`git restore
         --source=HEAD --staged --worktree -- <file> <generated paths>`) and park
         `spec_review_exhausted`, detail "accept failed: <error>", resume_to SPEC_REVIEW;
      4. copy the file to `campaigns/<cid>/spec_at_open.md` and set `spec.sha256_at_open`
         (the SPEC-0186 close guard's baseline);
      5. append ledger `spec_accepted {spec_id, round}`, added to `EVENT_KEYS`, and set
         `pending_brief = None`;
      6. `_transition` to IMPLEMENTING, reason `spec_accepted`. SPEC_REVIEW and IMPLEMENTING
         are both active states, so the campaign keeps its slot and never waits.

      *Note:* steps 4–6 run on the main thread inside `_apply`, so the worker error table
      never sees their failures. The accept branch therefore wraps them in its own
      `try/except`. Any exception parks `spec_review_exhausted`, detail "accept failed after
      commit: <error>", resume_to SPEC_REVIEW. On resume, worker step 0 finds the committed
      accept and finishes steps 4–6 without a review.
    - **revise or split**, while `spec_rounds < spec_review_max`: set `pending_brief =
      {source: "spec_reviewer", fix_brief: <summary>, must_fix: <must_fix and major
      findings>, suggested_splits: <for a split>, human_notes: <kept>}`, then go to
      SPEC_WRITING (SPEC-0215's revise mode). SPEC-0215 clears `pending_brief` after a
      successful revise.
    - **Otherwise (exhausted):** set `pending_brief` from the last findings exactly as for
      revise, then park `spec_review_exhausted`, detail "N reviews without approval; last
      verdict <v>", resume_to SPEC_WRITING. A resume with no note therefore still gives the
      writer findings to address.
- **Resume** (any spec-phase park): a plain resume resets both retry counters, refreshes
  `state.idea`, and adds `--note` to `pending_brief`. `--reset-loops` also resets
  `spec_rounds`.
  - The dirty, reviewer-failed and accept-failed parks resume to SPEC_REVIEW. They consume no
    round, because a round is counted only for a conforming verdict.
  - A real exhaustion resumes to SPEC_WRITING, so the writer always revises against the
    stored findings first, with or without `--reset-loops`. Without it, the next review is
    one past the limit, so a non-approve verdict parks again: exactly one more review. With
    it, the full `spec_review_max` rounds are available again.
- **Generic errors:** `_apply`'s error table (`queue.py` ≈651) maps `spec_review` to reason
  `spec_review_exhausted` and resume_to SPEC_REVIEW, with detail "spec_review step failed:
  <error>". This sits next to SPEC-0215's `write_spec` → `spec_write_failed` / SPEC_WRITING
  entry. Neither falls back to the `open_failed`/IMPLEMENTING default.

## Alternatives considered

- **A human accept step** — the earlier design. It added `spec_ready`, `--no-auto-accept` and
  stale-text guards, all of which kept diverging in review. A human can still stop a
  campaign with a park request before acceptance.
- **A separate SPEC_REVISION state** — SPEC-0215's revise mode does the same with one state.
- **A separate park reason per reviewer failure cause** — the detail and `resume_to` already
  tell the causes apart, so one reason keeps the conduct table small.

## Acceptance criteria

- [x] **Approve path** (FakeRunner: writer, then reviewer approve):
  - SPEC_WRITING → SPEC_REVIEW → IMPLEMENTING;
  - the spec is `accepted`, and the accept commit contains only the spec file and the
    regenerated `docs/LEDGER.md` (an untracked setup file stays uncommitted);
  - `python3 tools/spec_lint.py` exits 0 in the worktree after the accept;
  - `spec_at_open.md` equals the accepted text;
  - the store holds the review for S;
  - the ledger has `spec_review` (round 1, with `run_id`) and `spec_accepted`;
  - the run continues to MERGED with no human step, with the spec and the feature on main.
- [x] **Revise path** (writer → reviewer revise → writer → reviewer approve):
  - two `spec_review` events, rounds 1 and 2;
  - the second writer prompt contains the round-1 findings;
  - accepted after round 2.
  A `split` verdict behaves the same, and the writer prompt contains the narrowing rule.
- [x] **Exhaustion:** with `spec_review_max: 2` and reviewer → revise twice, the campaign
      parks `spec_review_exhausted` (resume_to SPEC_WRITING), with the round-2 findings in
      `pending_brief`.
  - A resume with no note runs the writer with those findings, then exactly one more review.
  - `wt queue resume --note …` puts the note in the writer prompt.
  - `--reset-loops` resets `spec_rounds`, and the next `spec_review` event's round is 3.
- [x] **Reviewer failure** (each case is its own test):
  - a runner error retries once, then parks `spec_review_exhausted` ("reviewer failed"),
    resume_to SPEC_REVIEW;
  - a reviewer that edits the spec or another tracked file, or commits, has the worktree
    reset to H, is
    retried with a clean worktree, and then parks "reviewer failed: reviewer modified the
    worktree", not "commit or discard";
  - a tracked uncommitted edit parks it before any run ("commit or discard");
  - an untracked setup file alone doesn't block;
  - resuming any of these consumes no round.
- [x] **Accept failure:** an injected commit failure leaves the worktree clean (the file,
      `docs/LEDGER.md` and the index restored) and parks `spec_review_exhausted` ("accept
      failed"). Resuming re-reviews, then accepts. A failure injected after the accept commit
      (in the `spec_at_open.md` copy) parks. The resume runs no reviewer and no writer, and
      reaches IMPLEMENTING.
- [x] **Cost cap:** with `cap_usd` below `total_usd + spec_reviewer budget`, a campaign
      entering SPEC_REVIEW parks `cost_cap` (resume_to SPEC_REVIEW) without a run. `wt
      queue resume --raise-cap` runs the review.
- [x] **Slots:** with `slots=1` and one other campaign QUEUED, the approved campaign goes
      straight to IMPLEMENTING (it keeps its slot).
- [x] **Human change:** when the on-disk state is no longer SPEC_REVIEW at accept time, no
      accept commit is made.
- [x] **Generic errors:** an OSError raised in the `write_spec` worker parks
      `spec_write_failed` (resume_to SPEC_WRITING). One raised in the `spec_review` worker
      parks `spec_review_exhausted` (resume_to SPEC_REVIEW).
- [x] **Legacy park:** resuming a `spec_review_unavailable` park without a note goes to
      SPEC_REVIEW with no writer run. With a note, it goes to SPEC_WRITING, and the note
      reaches the writer prompt.
- [x] **Recording:**
  - `spec_review` and `spec_accepted` validate against `schemas.EVENT_KEYS`;
  - `state.spec_review` survives `_apply`;
  - `counters.spec_rounds` matches the number of reviews since the last `--reset-loops`;
  - the reviewer run's cost is added to `state.cost.total_usd`;
  - every builtin profile has `spec_review_max`;
  - the in-campaign request's system prompt contains the campaign invariant.
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_spec_review_loop.py`, using the e2e harness, the
  SPEC-0215 FakeRunner helpers (including its ledger fixture repo) and `spec_reviewer`
  entries. At least one test per AC, plus separate tests for: the worktree-editing reviewer,
  the dirty worktree, split narrowing, exhaustion then a resume with no note, exhaustion
  then `--reset-loops`, and the failure after the accept commit. Failures are injected by monkeypatching the reviewer
  runner and the accept's commit call (`gitops.git(..., "commit", ...)`; `commit_all` is
  not used, because it adds everything). The spec-editing reviewer is a FakeRunner that writes to
  the file. Slots are patched to 1 for the slot case, and `cap_usd` is set in state for the cap
  case.
- **Manual verification:** none beyond the automated end-to-end run.
- **Regression guard:** full `uv run pytest`. SPEC-0215's tests that expect
  `spec_review_unavailable` after a write change to expect SPEC_REVIEW. Its AC "Resuming
  `spec_review_unavailable` without a note re-parks it" changes to expect SPEC_REVIEW.

### Test record (2026-10-25)

- `uv run pytest tests/test_agentic_spec_review_loop.py tests/test_agentic_spec_writer.py`:
  92 passed (the new SPEC-0214 tests plus SPEC-0215's, updated to expect SPEC_REVIEW after a write).
- Full `uv run pytest -q -x`: 2404 passed, 1 skipped. `python3 tools/spec_lint.py` exits 0.
- After the review-loop fixes: `uv run pytest tests/test_agentic_spec_review_loop.py`: 34 passed;
  full `uv run pytest -q -x`: 2411 passed, 1 skipped; `python3 tools/spec_lint.py` exits 0.
- Manual verification: none beyond the automated end-to-end run, as planned.
- After the rebase onto YAML profiles (SPEC-0237) and the SPEC-0230 single-round override:
  `uv run pytest tests/test_agentic_spec_review_loop.py tests/test_agentic_spec_writer.py`: 96
  passed; full `uv run pytest -q -x`: 2604 passed, 3 skipped; `python3 tools/spec_lint.py` exits 0.
- Loop-3 fix: `uv run pytest tests/test_agentic_spec_review_loop.py tests/test_agentic_spec_writer.py`:
  102 passed (accept-failure resumes assert one review; new human-edit re-review test).
- Loop-4 fixes: `uv run pytest tests/test_agentic_spec_review_loop.py tests/test_agentic_spec_writer.py`:
  107 passed; full `uv run pytest -q -x`: 2860 passed, 3 skipped; `python3 tools/spec_lint.py` exits 0.
- Loop-2 fixes: `uv run pytest tests/test_agentic_spec_review_loop.py`: 36 passed (new: plain
  resume of the unchanged park, after-revision accept failures before and after the commit,
  stray-file cleanup, dirty-over-accepted precheck order).

### As shipped (2026-10-25)

- The worker steps 0 and 1 (already-accepted HEAD, tracked changes) run in a main-thread
  `_spec_review_precheck` just before `run.start`, not in the worker, so a dirty or already
  accepted campaign spends no run id. Behavior is as specified: `dirty` and `accepted` results
  go straight to `_apply`.
- `build_prompt` also takes `invariant=`, because `build_request` renders through it.
- Resuming any of `spec_write_failed`, `spec_review_exhausted`, `spec_review_unavailable`
  refreshes the idea snapshot and resets both retry counters (`SPEC_PHASE_PARKS`).
- `_do_spec_review` catches any exception from the runner, still runs the reset-to-H check, and
  returns it as `error`, so a crashing runner leaves a clean worktree and `_apply` closes the
  run (`run.end`, cost) before parking `spec_review_exhausted` ("spec_review step failed: …").
- Review-round tests added: an approve one past the limit accepts, the regenerate-failure and
  worktree-modifying-reviewer parks, timeout and interrupted reviews, a runner that raises
  after editing the spec.
- `docs/product/loop/operate.md` lists the new park reason and marks `spec_review_unavailable` legacy.
- **SPEC-0230 overrides applied (binding amendment): one spec review per campaign.**
  `spec_review_max` is not a profile key (the builtin profiles are YAML since SPEC-0237 and
  carry none) and `counters.spec_rounds` is gone. A `revise` or `split` always goes to the
  writer's revise mode. Back in SPEC_REVIEW, the precheck's step 0′ finds the campaign's
  prior revise/split `spec_review` event and runs no reviewer: a changed spec sha is accepted
  (commit `accept <SPEC> (round 1) (after revision)`, `pending_brief =
  revision_brief(<stored review>)`), an unchanged sha parks `spec_review_exhausted` ("the
  revision did not change the spec", resume_to SPEC_WRITING). A resume after an accept failure
  also goes to accept, never to a second review. `--reset-loops` has no spec-round effect.
  This replaces the AC items "Revise path" (now writer → revise → writer → accept, one
  `spec_review` event), "Exhaustion" (now the unchanged-sha park) and the `spec_rounds` /
  "every builtin profile has `spec_review_max`" recording items (now: no builtin profile has
  it). Tests: `test_revise_then_accept_after_revision_without_a_second_review`,
  `test_an_unchanged_revision_parks_and_runs_no_second_review`,
  `test_resume_with_a_note_revises_then_accepts`, `test_no_builtin_profile_has_spec_review_max`.

- **Loop-2 review fixes.** The unchanged-revision park now sets `pending_brief` from the stored
  review of the prior revise / split (falling back to a plain "revise the spec" brief), so a
  resume with no note runs the writer instead of going around `spec_review_unavailable` →
  SPEC_REVIEW → unchanged. The precheck runs the tracked-clean check before the `accepted`
  short-circuit. A failed `_commit_accept` also rolls back through `SW.rollback` (undeclared
  changed paths restored, untracked files created since the snapshot removed). An accept failure
  after a revision resumes to the accept again, never to a second review (tests added for both
  failure points).

- **Loop-3 review fix: an approve whose accept failed resumes to the accept.** `_spec_review_precheck`
  now looks up the campaign's last `spec_review` event (`_last_review_event`; `_prior_revision` is
  that event when its verdict is revise / split). When it is `approve` and its `spec_sha256`
  equals the current spec sha, the precheck returns `approved` and `_apply_spec_review` calls
  `_accept_spec(committed=False, rnd=<event round>, revision=None)`: no reviewer runs and no
  second `spec_review` event is written. When the sha differs, a human edited the approved spec
  after the failure; the choice is deterministic: the precheck returns None and the spec is
  re-reviewed (that is the only case where a second review of an approve happens; its round is
  the next one). The Exhaustion / accept-failure ACs' "Resuming re-reviews, then accepts" is thereby
  replaced by "Resuming accepts without a review" (test
  `test_commit_failure_restores_the_worktree_then_a_resume_accepts_without_a_second_review`; the
  regeneration-failure test asserts one review too; the human-edit case is
  `test_a_human_edit_of_the_approved_spec_re_reviews_after_an_accept_failure`).

- **Loop-4 review fixes.** (1) The precheck's `accepted` short-circuit finishes a committed accept
  only when this campaign has a `spec_review` event and no `spec_accepted` event. Otherwise
  (resume `--to SPEC_REVIEW` after acceptance, or on a plain spec campaign) the precheck returns
  `already_accepted`: no run, no re-baseline of `spec_at_open.md` / `sha256_at_open`, no second
  `spec_accepted`, `pending_brief` untouched; the campaign parks `spec_review_exhausted`
  ("the spec is already accepted; there is nothing to review"), resume_to IMPLEMENTING. Tests:
  `test_resume_to_spec_review_after_acceptance_keeps_the_close_guard_baseline`,
  `test_resume_to_spec_review_on_a_plain_spec_campaign_runs_nothing`. (2) Before the post-review
  `reset --hard H`, `_save_worktree_changes` keeps a moved HEAD and the tracked uncommitted diff
  (`git stash create`, not the shared stash stack) under `refs/wt/spec-review-saved/<run>/{head,wip}`;
  a `note` ledger event names them and the park detail ends "(worktree changes saved as …)" after
  "reviewer modified the worktree". Test: `test_a_reset_after_the_review_keeps_a_human_edit_and_commit_in_refs`.
  (3) Tests added: an explicit `--to` beats the legacy-park redirect; a `park_requested` during the
  review is honored after the accept (parks accepted, resume_to IMPLEMENTING). The optional
  `--note` carry-over on SPEC_REVIEW-bound resumes was not done.

## Rollout / migration

Ships through the agentic queue after SPEC-0215 and SPEC-0210. Once it lands, ideas can be
queued end to end with `wt queue add IDEA-NNN`. Campaigns parked `spec_review_unavailable`
resume into SPEC_REVIEW.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
