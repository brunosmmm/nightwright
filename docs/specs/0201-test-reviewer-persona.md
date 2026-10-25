---
id: SPEC-0201
title: "Test reviewer persona: do the tests cover the spec, plus multi-reviewer REVIEWING"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, review, personas, queue]
source_idea: IDEA-029
depends_on: [SPEC-0187]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-029`).

[SPEC-0187](./0187-conformance-review-reviewing-state-with-a-read.md) put one independent
reviewer between `GATING` and `CLOSING`: does the change do what the spec says? It does not
ask whether the **tests** would catch a regression. A suite can be green while an acceptance
criterion has no test, while the only test is a happy path, or while a test asserts nothing. The
conformance reviewer flags tautological tests it happens to see; it is not tasked with
mapping every AC to tests nor with probing edge and failure cases. This spec adds that second
lens as the `test_reviewer` persona, and also introduces the shared mechanism by which
`REVIEWING` runs **several** reviewers ([SPEC-0202](./0202-adversarial-reviewer-persona.md)
adds a third; [SPEC-0203](./0203-review-triage-combiner-waivers-and-automatic.md) combines
their verdicts).

## Goals / Non-goals

**Goals**
- Read-only persona `test_reviewer` judging whether the tests added or changed cover each AC
  and the meaningful edge and failure cases of the change — not merely that they pass.
- `REVIEWING` runs all reviewers configured for the campaign **in parallel**, each a fresh run
  with its own budget, run directory and ledger entries.
- A per-profile `reviewers` list; the cost cap covers the sum of reviewer budgets before spawn.
- A minimal interim combination rule so the queue works before SPEC-0203 lands.

**Non-goals**
- The adversarial reviewer (SPEC-0202), score weighting, waivers and follow-up filing (SPEC-0203).
- Running or writing tests: the reviewer reads, it does not execute the suite or edit files.
- Changing the gates or the close step.

## Decision

Make `REVIEWING` a fan-out step. The profile lists reviewer personas; the queue spawns one run
per reviewer concurrently, waits for all, and combines the verdicts with one pure function.
`test_reviewer` is the second member. The conformance reviewer's contract is unchanged.

## Design

### Shared: multi-reviewer REVIEWING (referenced by SPEC-0202 and SPEC-0203)

- **Profile key** `reviewers: [persona, …]` (a list of persona names, each of which must exist in
  `PERSONAS` and `profile["personas"]` and be `read_only`). Defaults in
  `BUILTIN_PROFILES`: `balanced` and `thorough` — `[conformance_reviewer, test_reviewer,
  adversarial_reviewer]`; `frugal` — `[conformance_reviewer]`. Until SPEC-0202 ships the
  defaults omit `adversarial_reviewer`. `resolve_profile` validates the list (unknown or
  non-read-only persona ⇒ `ValueError` naming it); `deep_merge` replaces lists wholesale, so a
  user delta `reviewers: [conformance_reviewer]` narrows it. An absent or empty key falls
  back to `[conformance_reviewer]` (SPEC-0187 behavior).
- **Plan:** `plan_step` for `REVIEWING` returns a `review` step with `personas: [...]`
  (the resolved list, in order). The step is executed in the same thread pool used for agent
  steps: one `_do_persona` task per reviewer, each producing its own `build_request`, run
  directory (`runs/NNN-<persona>/`), `run.start`/`run.end` events and `finding` events
  (`persona` = that reviewer). Reviewers never see one another's verdicts (independence);
  they get `previous_verdicts` from earlier loops only. The ordering of events in the ledger is
  deterministic: results are applied in profile-list order after all tasks finish.
- **Cost cap:** before spawning, the pessimistic check sums `max_budget_usd` of every reviewer
  in the list against `campaign_cap_usd - cost.total`. If the sum does not fit the review does
  not start: `PARKED(cost_cap)` as for any persona (never a partial fan-out). The cost recorded
  per reviewer is charged separately (`cost` events carry the reviewer's persona).
- **Interim combiner** `combine_reviews(verdicts) -> {verdict, must_fix, findings, fix_brief}`
  (pure, in `queue.py`; SPEC-0203 replaces its body): `fix` if any reviewer returned `fix` or any
  finding has severity `must_fix`; otherwise `pass`. The merged `fix_brief` is each fixing
  reviewer's `fix_brief`, prefixed `[<persona>]`, joined by blank lines, followed by the
  must-fix finding titles.
- **Malformed verdicts:** per reviewer, `counters.review_retries[persona]` (one retry per loop
  in total per persona, as SPEC-0187). A persona that stays malformed after its retry
  ⇒ `PARKED(review_escalated)` (existing reason; the detail names the persona). A retry re-runs
  **only** the malformed reviewers; valid verdicts from the same pass are kept in
  `state.last_review.verdicts[persona]` and reused.
- **State:** `state.last_review` becomes `{run, loop, verdicts: {persona: {verdict, score,
  findings, run}}, verdict, must_fix, findings}`; the SPEC-0187 keys (`verdict`, `score`,
  `findings`, `must_fix`) stay populated from the combination (`score` = the minimum score of
  the reviewers that report one) so existing consumers keep working.
- **Brief:** `pending_brief.source` = `review` (was `conformance_reviewer`); the brief lists
  the contributing personas. The implementer persona text already says "fix what the brief
  says"; no template change besides the source label.

### Persona `test_reviewer`

- File `src/wt/agentic/personas/test_reviewer.md`, registered in `PERSONAS` with `read_only=True`,
  schema `TEST_REVIEWER_SCHEMA`; `permission_mode: dontAsk`, `--restricted`, read-only tools.
- **Inputs (context)** — `spec_text`, `diff` (≤ 1500 lines, `git diff <base>...HEAD`), `test_files`
  (paths under `tests/` touched, and for each the added/changed test function names), `gate_results`
  summary, `previous_verdicts`, `pending_brief`. The reviewer may read any file in the worktree
  (existing tests, fixtures) to judge coverage; it must not run the suite or edit anything.
- **Method (in the template):** for **every** AC list the test(s) that would fail if the AC were
  broken — name, file, and what is asserted; an AC with no such test is `must_fix`; an AC whose
  only test would still pass with the behavior removed (tautology, over-mocking, asserting on
  the mock, no assertion) is `major`. Then enumerate the change's edge and failure cases
  (empty input, boundaries, error paths, permission/idempotency, concurrency where the diff has
  any) and flag untested material ones as `major` (behavior-bearing) or `minor` (cosmetic).
  Tests unrelated to the spec, flakiness risks (sleep, wall-clock, network) are `minor`.
  Regression: a deleted or weakened existing test without justification in the spec is `must_fix`.
- **Verdict schema** `TEST_REVIEWER_SCHEMA`: `{verdict: pass|fix, score: 0..100, findings:
  [{severity: must_fix|major|minor|nit, ac, title, detail, files}], coverage_assessment: [{ac,
  covered: bool, tests: [str]}], fix_brief}`. `ac` is the AC text or id the finding concerns,
  or the code path (`file:function`) for an edge case outside any AC. `verdict: pass` with a
  `must_fix` finding is read as `fix` (as SPEC-0187). Findings become `finding` ledger events
  carrying `ac`, like the conformance reviewer's (the existing `findings` branch in
  `_events_for_result` already handles the shape; `coverage_assessment` is kept in `run.json`
  only).
- **Profiles:** `personas.test_reviewer` — `balanced`/`thorough`: model `opus`, effort `high`,
  `max_budget_usd` 1.0; `frugal`: model `sonnet`, effort `medium`, 0.5. `read_only: true`,
  `permission_mode: dontAsk`, same timeout as the conformance reviewer.

### schemas.py and parking

- No new ledger event types. `run.start`/`run.end`/`finding`/`cost` already carry `persona`.
  `finding` events from this persona set the optional `ac` key like SPEC-0187's.
- No new park reasons: `review_escalated` (malformed twice, names the persona), `cost_cap`
  (sum of budgets), `loop_exhausted` (fix at `loop_max`) are reused.
- Profile schema gains `reviewers`; a profile without it behaves as SPEC-0187.

## Alternatives considered

- **Run reviewers sequentially, stopping at the first `fix`** — cheaper on failure, but slower
  on success, and the implementer then gets one reviewer's complaints per loop, burning loops.
  Parallel runs give the implementer one merged brief per loop.
- **One combined "super reviewer"** — one context asked to judge conformance, tests and
  security tends to dilute each; independent fresh contexts are the plan's explicit design.
- **Let the test reviewer run the suite or mutate code** — rejected; the gates already run
  the suite and mutation testing is out of scope and costly.

## Acceptance criteria

- [ ] `resolve_profile` returns `reviewers` per profile (defaults above) and rejects an unknown
      or non-read-only persona; a user delta replaces the list; absent key ⇒ conformance only.
- [ ] `plan_step` for `REVIEWING` returns a review step listing the resolved reviewers; the
      cost-cap check sums their budgets and parks `cost_cap` without spawning any when over.
- [ ] Reviewers run concurrently as independent runs (own run dir, budget, `run.*`, `cost` and
      `finding` events); events are applied in profile-list order; reviewers never receive
      each other's current-loop verdicts.
- [ ] `combine_reviews`: any `fix` verdict or `must_fix` finding ⇒ `fix` with a merged,
      persona-labelled `fix_brief`; all `pass` ⇒ `pass`; `state.last_review` has per-persona
      `verdicts` and the SPEC-0187 keys.
- [ ] A malformed verdict is retried once for that persona only; twice ⇒
      `PARKED(review_escalated)` naming the persona; valid sibling verdicts are not re-run.
- [ ] `test_reviewer`: template, `TEST_REVIEWER_SCHEMA`, `PERSONAS` entry, per-profile
      model/effort/budget as above; `build_request` is read-only and its prompt is
      deterministic and includes the touched test functions.
- [ ] End to end with FakeRunner scripts: (a) conformance pass + test_reviewer fix then pass ⇒
      `MERGED` with two implementer runs and the test findings in the ledger; (b) both pass ⇒
      `MERGED`; (c) test_reviewer malformed twice ⇒ `PARKED(review_escalated)`; (d) fix until
      `loop_max` ⇒ `PARKED(loop_exhausted)`.
- [ ] Existing queue, close and e2e tests still pass (default profiles in tests resolve
      to the reviewers they script, or tests set `reviewers: [conformance_reviewer]`).
- [ ] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_profiles.py` (reviewers defaults, validation, delta
  replace); `tests/test_agentic_queue.py` (plan_step review step and pessimistic sum cap,
  `combine_reviews` parametrized, per-persona retry accounting, state shape);
  `tests/test_agentic_personas.py` (template, schema, request: read-only, model/budget per
  profile, deterministic prompt, test function listing); `tests/test_agentic_e2e.py`
  (paths a-d above, each reviewer scripted in the FakeRunner JSON script; assert run dirs and
  event order are stable across repeated runs).
- **Manual verification:** the next real campaign with the balanced profile records both
  reviewers' verdicts and costs here.
- **Regression guard:** full `uv run pytest`.

## Rollout / migration

Profiles without `reviewers` keep the conformance-only behavior. In-flight campaigns in
`REVIEWING` resume with the new list (resume re-runs the review). The running loop must be
restarted to load the new code.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
