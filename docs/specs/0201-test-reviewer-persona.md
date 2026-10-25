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

Revised 2026-10-25 after spec review (docs/specs/reviews/SPEC-0201.json).

## Goals / Non-goals

**Goals**
- G1. Read-only persona `test_reviewer` judging whether the tests added or changed cover each AC
  and the meaningful edge and failure cases of the change — not merely that they pass.
- G2. `REVIEWING` runs all reviewers configured for the campaign **in parallel**, each a fresh run
  with its own budget, run directory and ledger entries.
- G3. A per-profile `reviewers` list; the cost cap covers the budgets of the reviewers about to
  run, checked before any spawn.
- G4. A minimal interim combination rule so the queue works before SPEC-0203 lands.
- G5. Per-persona malformed-verdict handling that never loses a valid sibling verdict.

**Non-goals**
- The adversarial reviewer (SPEC-0202), score weighting, waivers and follow-up filing (SPEC-0203).
- Running or writing tests: the reviewer reads, it does not execute the suite or edit files.
- Changing the gates or the close step.
- Splitting this spec: considered and declined. AC1-AC6 (fan-out) and AC7-AC8 (persona,
  `tests_touched`) are testable independently, but a persona without the `reviewers` list is
  never run, and a fan-out with one member is unexercised; they ship as one campaign, with the
  ACs grouped so progress is checkable per group.

## Decision

Make `REVIEWING` a fan-out step. The profile lists reviewer personas; the queue spawns one run
per reviewer concurrently, waits for all, and combines the verdicts with one pure function.
`test_reviewer` is the second member. The conformance reviewer's contract is unchanged.

Reasoning for the choices that were open: (1) the malformed-retry counter gets a **new** key and
the existing int stays, so in-flight campaigns and the existing assertions
(`tests/test_agentic_queue.py:171,188,195,214`, `tests/test_agentic_e2e.py:247,424`) need no
migration; (2) the combiner waits until every reviewer has a valid verdict (or is out of retries)
before routing, because routing on a partial set would send an implementer off with a brief
that a still-pending reviewer may contradict; (3) the cost cap stays pessimistic (as
`_cap_blocks`, `queue.py:79-91`) and the default `campaign_cap_usd` is left alone, since it is
compared with *actual* spend, not with the sum of budgets (see Cost cap).

## Design

### Shared: multi-reviewer REVIEWING (referenced by SPEC-0202 and SPEC-0203)

- **Profile key** `reviewers: [persona, …]` — persona names, each of which must exist in
  `PERSONAS` (`personas.py:133`) and in `profile["personas"]` and have `read_only: true`.
  **This spec ships** `BUILTIN_PROFILES` (`profiles.py`) with `reviewers: [conformance_reviewer,
  test_reviewer]` for `balanced` and `thorough`, and `[conformance_reviewer]` for `frugal`.
  SPEC-0202 later appends `adversarial_reviewer` to the `balanced`/`thorough` lists; it is not
  listed here because `resolve_profile` rejects personas absent from `PERSONAS`.
  `resolve_profile` validates the list (unknown, duplicate, missing-settings or non-read-only
  persona ⇒ `ValueError` naming it and the profile); `deep_merge` replaces lists wholesale, so a
  user delta `reviewers: [conformance_reviewer]` narrows it. An absent or empty key (including a
  hand-built profile dict, as in `tests/test_agentic_queue.py:20`) falls back to
  `[conformance_reviewer]` inside `plan_step`, which is SPEC-0187 behavior.
- **Plan:** `Step` (`queue.py:68`) gains `personas: tuple[str, …] = ()`. `plan_step` for
  `REVIEWING` returns `Step("review", persona=<first pending persona>, personas=<pending
  personas in profile order>)`. `persona` stays populated (the first pending one) so the cap-park
  step, the existing parametrized plan test (`tests/test_agentic_queue.py:36`) and `_apply`'s
  `res.step.persona` keep working. *Pending* = reviewers in the list that have no valid
  verdict for the current loop in `state.last_review.verdicts` (every reviewer on a first pass; only
  the missing ones on a retry pass). If *pending* is empty, `plan_step` returns the
  full reviewer list (a review is never skipped; SPEC-0203 adds its one exception, for its
  triager step).
- **Execution:** the tick expands one `review` step into one `_prepare_run` + `_do_persona`
  task per pending persona (`queue.py:523`, `queue.py:885-897`), each producing its own
  `build_request`, run directory (`runs/NNN-<persona>/`), `run.start`/`run.end`, `cost` and
  `finding` events (`persona` = that reviewer). The thread pool is sized
  `max(slots, number of agent tasks in the tick)` (after expanding every review step) so every
  campaign's reviewers run concurrently even when `slots` is smaller (a fan-out may therefore
  briefly exceed `slots`; accepted, the alternative is a half-run review; the number of active
  campaigns is already bounded by admission). When all of a campaign's futures finish, their
  `StepResult`s are sorted into profile-list order, wrapped in one aggregate
  `StepResult(reviews=[…])` and passed to `_apply` **once** (new `_apply_reviews`, replacing
  `_apply_review`, `queue.py:744`). Events are therefore appended in profile-list order regardless
  of which reviewer finished first. Run ids are allocated in profile-list order before any
  task starts.
- **Independence:** a reviewer's `build_request` receives `previous_verdicts` only from
  earlier loops (entries of `last_review.verdicts` with `loop < counters.loops`); the current
  loop's verdicts, including those kept across a retry pass, are never passed to any reviewer.
- **Cost cap:** `_cap_blocks` generalises to `_cap_blocks(state, profile, personas)`: the needed
  amount is the sum of `persona_settings(profile, p)["max_budget_usd"]` over the **pending**
  personas, compared with `cost.cap_usd − cost.total_usd` (the state key is `total_usd`,
  `queue.py:87`). If it does not fit: `PARKED(cost_cap)`, no reviewer spawned (never a partial
  fan-out; a retry pass checks only its pending reviewers). The park detail names the personas and
  the sum. Each reviewer's cost is charged on its own `cost` event with that persona, by
  `_record_run`, as now. Default cap impact: at defaults `balanced` adds `test_reviewer` budget
  1.0 per review pass (conformance 1.5 + test 1.0 = 2.5 of the 10.0 cap on top of the 4.0
  implementer budget); the pessimistic check compares *spent* money, not budgets, so the first
  review fits (implementer actual spend ≤ 4.0, 4.0 + 2.5 < 10.0) and a second loop needs
  spent + 4.0 ≤ 10.0 for the implementer as it does today. `campaign_cap_usd` defaults are not
  changed. `frugal` (cap 3.0, one reviewer 0.6) is unchanged.
- **Interim combiner** `combine_reviews(verdicts) -> {verdict, must_fix, findings, fix_brief}`
  (pure, in `queue.py`; SPEC-0203 replaces it, see that spec for the call-site change): `verdicts`
  is an ordered `{persona: verdict_dict}` of valid verdicts. Result is `fix` if any reviewer
  returned `fix` or any finding has severity `must_fix`, else `pass`. `fix_brief` is each fixing
  reviewer's `fix_brief`, prefixed `[<persona>] `, joined by blank lines, followed by one line
  per `must_fix` finding title (`must_fix: <title>`); `must_fix` is the list of must-fix findings
  with `persona` added; `findings` is the count of all findings.
- **Decision order in `_apply_reviews`** (the answer to "valid `fix` plus malformed sibling"):
  1. Merge this pass's per-persona outcomes into `last_review.verdicts` (valid ⇒ stored with
     `loop`; malformed ⇒ left absent).
  2. Let *missing* be the reviewers without a valid verdict **for the current loop** (an entry
     counts only when `entry.loop == counters.loops`; an older loop's entry for the same persona
     does not, and is overwritten on success). A reviewer that failed hard (runner error, timeout,
     budget kill, `rr.verdict_ok` false) is *missing* exactly like a malformed one: same retry
     rule, and its sibling `StepResult`s are still applied (events, costs and valid verdicts are
     recorded in step 1 before anything is decided; the ledger is never half-applied). If **any**
     missing persona has already used its single retry
     (`counters.review_retries_by_persona[persona] ≥ REVIEW_RETRY_MAX`, `queue.py:59`) ⇒
     `PARKED(review_escalated)` naming exactly those personas (even if a sibling said `fix`).
     Otherwise (every missing persona still has retry budget) increment the counter for each
     missing persona and the legacy int `review_retries` by one, and stay in `REVIEWING`
     (`review_retry`). Mixed case: A missing for the second time and B for the first ⇒ park naming
     A only; B is never spawned a third time. A hard failure's charged cost (its budget when
     the runner reported none) is already in `cost.total_usd` when the retry's cap check runs. **No routing happens while any verdict is missing:** a valid `fix` is
     held, not sent to the implementer, so one merged brief is produced per loop.
  3. When no reviewer is missing: `combine_reviews`; `pass` ⇒ `CLOSING`
     (`review_pass`); `fix` ⇒ `loops += 1`; beyond `loop_max` ⇒ `PARKED(loop_exhausted)`
     (detail carries the merged brief, as `decide_review`, `queue.py:166-192`); else
     `IMPLEMENTING` (`review_fix`).
  4. **Entering `REVIEWING` always starts a fresh review:** every transition into it (from
     `GATING` on `gates_green`, which also happens at an unchanged `counters.loops` after
     CLOSING→TRIAGING→`retry_gate`→GATING, `queue.py:711-736`; and a resume, below) clears
     `last_review.verdicts` and both retry counters, so a `pass` on older code is never reused.
  5. On any exit from `REVIEWING` by transition (`CLOSING` or `IMPLEMENTING`) both
     `review_retries` and `review_retries_by_persona` are reset (to 0 and removed), i.e. the
     allowance is **one retry per persona per review pass-through**, regardless of whether the
     pass-through ended in `pass` or `fix`. Resuming a `PARKED(review_escalated|cost_cap)`
     campaign into `REVIEWING` is such an entry, so the resumed review re-runs every reviewer (matches the "resume re-runs the review"
     rule in `_resume_target`, `queue.py:117`).
  `decide_review` becomes `decide_reviews(state, profile, verdicts_by_persona, malformed)`
  (pure) implementing steps 2-3.
- **Counters:** `counters.review_retries` stays an int (`_counters` seeding, `queue.py:296`,
  unchanged), meaning "retry passes used in this review". The per-persona map is
  `counters.review_retries_by_persona` and is **not seeded** by `_counters`; it appears only
  after the first malformed verdict, so no in-flight campaign needs migration and the e2e
  equality assertion on the counters dict (`tests/test_agentic_e2e.py:247`) still holds.
  The reads/writes at `tests/test_agentic_queue.py:171,188,195,214` and
  `tests/test_agentic_e2e.py:424` keep working because the int is still maintained.
- **State:** `state.last_review` becomes
  `{loop, run, runs, verdicts, verdict, score, findings, must_fix}`:
  - `verdicts: {persona: {verdict, score, fix_brief, findings: [<the reviewer's full finding
    dicts as returned: severity, title, detail, files, plus ac/category when present>],
    ac_assessment (conformance reviewer only, as returned), run,
    loop}}`, one entry per reviewer, persisted in state so later ticks (a retry pass,
    SPEC-0203's triager step) can recombine without re-reading the ledger; **overwritten**
    when a persona is retried (so a failed-then-retried persona holds only its successful run).
    Per-reviewer `score` is informational: it feeds the interim top-level `score` below and
    nothing else, and SPEC-0203 stops using it;
  - `run` = the run id of the **last reviewer in profile-list order** (kept so SPEC-0187
    readers get a single string); `runs` = `{persona: run_id}`;
  - `verdict`, `score`, `findings` (count), `must_fix` (count) are the combined values;
    `score` = the minimum `score` among reviewers that report one (null if none).
  The top-level `verdict`, `score`, `findings`, `must_fix` are written only when step 3 runs,
  so during a retry pass they are absent or stale (from the previous loop); readers use
  `verdicts[*].loop`.
  The only consumers of `last_review` are `queue.py` itself and the prose in
  `docs/product/capabilities/agentic.md` (grep confirmed no CLI renderer); that page is updated
  with the new shape. With `reviewers = [conformance_reviewer]` the old keys keep the SPEC-0187
  values, and the one existing assertion on the whole dict (`tests/test_agentic_queue.py:201`)
  may be updated to add the new keys `runs` and `verdicts`.
- **Counters on `fix`:** `_apply_reviews` keeps the resets of the single-reviewer code
  (`queue.py:763-777`): `loops += 1`, `gate_retries = 0`, `triage_retries = 0`, and preserves
  `pending_brief.human_notes` when building the new brief.
- **Brief:** `pending_brief.source` = `review` (also with a single reviewer; the implementer
  prompt's `From:` line changes from `conformance_reviewer` to `review` — the one visible
  difference from SPEC-0187 for conformance-only profiles; everything else about such a profile
  is unchanged); `pending_brief.personas` lists the contributing
  (fixing) personas; `fix_brief`, `must_fix` (each entry now also has `persona`) and
  `human_notes` as before. The implementer persona text already says "fix what the brief says";
  the prompt's `From: <source>` line prints `review`. The assertion at
  `tests/test_agentic_queue.py:198` and `tests/test_agentic_e2e.py:399` (`From:
  conformance_reviewer`) are updated by the implementer to `review` and
  `[conformance_reviewer]` respectively — these are the only existing assertions this spec
  permits to change, apart from `:201` above.
- **Existing tests keep passing** because the e2e FakeRunner helper (`tests/test_agentic_e2e.py`)
  and queue tests set `reviewers: [conformance_reviewer]` in their profile delta; the new
  tests exercise the default two-reviewer lists.

### Persona `test_reviewer`

- File `src/wt/agentic/personas/test_reviewer.md`, registered in `PERSONAS` with `read_only=True`,
  schema `TEST_REVIEWER_SCHEMA` (`schemas.py`); `permission_mode: dontAsk`, `--restricted`,
  read-only tools. `build_context`'s `review=` flag (`personas.py:311`, currently
  `persona == "conformance_reviewer"`) becomes `persona in REVIEWER_PERSONAS`, a module constant
  `("conformance_reviewer", "test_reviewer", "adversarial_reviewer")` so every reviewer gets
  the review context (diff, gate summary, previous verdicts).
- **Inputs (context)** — `spec_text`, `diff` (≤ 1500 lines, `git diff <base>...HEAD`), `tests_touched`
  (below), `gate_results` summary, `previous_verdicts`, `pending_brief`. The reviewer may read any
  file in the worktree (existing tests, fixtures); it must not run the suite or edit anything.
- **`tests_touched` extraction** (new `personas.tests_touched(worktree, base) -> list[dict]`,
  deterministic, no LLM). A file is a *test file* when its path is under `tests/` or matches
  `test_*.py` / `*_test.py`. Two git calls: `git diff -M --name-status <base>...HEAD` for paths and statuses
  (`R087` etc. normalised to `R`), then `git diff -M --unified=0 <base>...HEAD -- <path>` per test
  file for hunk ranges:
  - for each added/modified/renamed test file (new path), parse the HEAD version with `ast`;
    a test function is any `def`/`async def` named `test_*` at module level or inside a class
    (reported as `Class::name`; parametrization is not expanded — one name). A test is
    `changed` when its line span intersects the new-side range of a `--unified=0` hunk; a
    deletion-only hunk (new-side count 0 at line `s`) touches a test whose span satisfies
    `start − 1 ≤ s ≤ end`, so a removed assertion marks its test `changed`.
    If `ast.parse` fails, fall back to the regex `^\s*(?:async\s+)?def\s+(test_\w+)` and treat
    every match inside a hunk range as changed.
  - `removed`: test names parsed from the base version (`git show <base>:<oldpath>`) that are
    absent from the new version (or all of them for a deleted file). Renamed tests therefore
    appear as one removed and one changed name, which the reviewer reconciles from the diff.
  - Result entry: `{path, status: A|M|R|D, changed: [names], removed: [names]}`, sorted by
    path, names sorted, each list capped at 200 (a trailing `"… N more"` entry when capped).
  Removed tests are what lets the reviewer apply the "deleted or weakened" rule.
- **Method (in the template):** for **every** AC list the test(s) that would fail if the AC were
  broken — name, file, and what is asserted; an AC with no such test is `must_fix`; an AC whose
  only test would still pass with the behavior removed (tautology, over-mocking, asserting on
  the mock, no assertion) is `major`. Then enumerate the change's edge and failure cases
  (empty input, boundaries, error paths, permission/idempotency, concurrency where the diff has
  any) and flag untested material ones as `major` (behavior-bearing) or `minor` (cosmetic).
  Tests unrelated to the spec and flakiness risks (sleep, wall-clock, network) are `minor`.
  Regression: a `removed` test or a visibly weakened assertion without justification in the spec
  is `must_fix`.
- **Verdict schema** `TEST_REVIEWER_SCHEMA`: `{verdict: pass|fix, score: 0..100, findings:
  [{severity: must_fix|major|minor|nit, ac, title, detail, files}], coverage_assessment: [{ac,
  covered: bool, tests: [str]}], fix_brief}`. `ac` is the AC text or id the finding concerns,
  or the code path (`file:function`) for an edge case outside any AC. `verdict: pass` with a
  `must_fix` finding is read as `fix` (as SPEC-0187). Findings become `finding` ledger events
  carrying `ac`, like the conformance reviewer's (the `findings` branch in
  `_events_for_result`, `queue.py:373`, already handles the shape; `coverage_assessment` is kept in
  `run.json` only).
- **Profiles:** `personas.test_reviewer` — `balanced`/`thorough`: model `opus`, effort `high`,
  `max_budget_usd` 1.0; `frugal`: model `sonnet`, effort `medium`, 0.5 (present so a user can opt
  in via `reviewers`; not listed by default). `read_only: true`, `permission_mode: dontAsk`,
  same timeout as the conformance reviewer.

### schemas.py and parking

- No new ledger event types. `run.start`/`run.end`/`finding`/`cost` already carry `persona`.
  `finding` events from this persona set the optional `ac` key like SPEC-0187's.
- No new park reasons: `review_escalated` (malformed after retry; detail names the personas),
  `cost_cap` (sum of pending budgets), `loop_exhausted` (fix at `loop_max`) are reused.
- Profile schema gains `reviewers`; a profile without it behaves as SPEC-0187.

## Alternatives considered

- **Run reviewers sequentially, stopping at the first `fix`** — cheaper on failure, but slower
  on success, and the implementer then gets one reviewer's complaints per loop, burning loops.
  Parallel runs give the implementer one merged brief per loop.
- **Route a valid `fix` immediately while a sibling is being retried** — rejected: the retried
  sibling would produce a second brief and a second implementer loop for one review round.
- **Make `review_retries` a dict** — rejected: breaks in-flight state and five existing
  assertions for no gain over a separate key.
- **One combined "super reviewer"** — one context asked to judge conformance, tests and
  security tends to dilute each; independent fresh contexts are the plan's explicit design.
- **Let the test reviewer run the suite or mutate code** — rejected; the gates already run
  the suite and mutation testing is out of scope and costly.

## Acceptance criteria

- [ ] AC1 (G3, G4). `resolve_profile` returns exactly `reviewers == ["conformance_reviewer",
      "test_reviewer"]` for `balanced` and `thorough` and `["conformance_reviewer"]` for `frugal`;
      rejects an unknown, duplicate or non-read-only persona with a `ValueError` naming it; a user
      delta replaces the list; absent/empty key ⇒ conformance only.
- [ ] AC2 (G2, G3). `plan_step` for `REVIEWING` returns a `review` step with `personas` = the
      pending reviewers in profile order and `persona` = the first (full list when pending is
      empty); the cap check sums the pending reviewers' `max_budget_usd` against
      `cap_usd − total_usd`, parks `cost_cap` (detail names the personas and sum) without
      spawning any when over, and a retry pass sums only the still-pending reviewers.
- [ ] AC3 (G2). Reviewers run concurrently as independent runs (own run dir, budget, `run.*`,
      `cost` and `finding` events); run ids are allocated in profile-list order before any task
      starts; the pool is sized `max(slots, tasks in the tick)` (two reviewers with `slots: 1`
      are both in flight at once, shown with a barrier in the FakeRunner); events are applied in
      profile-list order even when the runs finish in reverse order; no reviewer's request
      contains a current-loop verdict.
- [ ] AC4 (G4). `combine_reviews`: any `fix` verdict or `must_fix` finding ⇒ `fix` with a merged,
      persona-labelled `fix_brief`; all `pass` ⇒ `pass`; `state.last_review` has the shape
      above (per-persona `verdicts` with full findings, `runs`, `run` = last reviewer's run,
      SPEC-0187 keys) and `pending_brief.source == "review"` listing the fixing personas.
- [ ] AC5 (G5). Retry and park: a missing reviewer (malformed or hard failure) is retried once
      while valid siblings are kept and **not** re-spawned; a valid `fix` plus a missing sibling
      does not route until the sibling is resolved; missing after its retry ⇒
      `PARKED(review_escalated)` naming only the personas out of retries (the mixed A/B case
      names A only), even if a sibling said `fix`; sibling events and costs are recorded for a
      hard failure.
- [ ] AC6 (G5). Counters and freshness: `review_retries` stays an int and
      `review_retries_by_persona` is absent until used; both reset on exit from REVIEWING by
      `pass` and by `fix`; every entry into REVIEWING (from `gates_green` at an unchanged loop
      after CLOSING→TRIAGING→GATING, and a resume) clears `verdicts` so the review re-runs; a
      verdict whose `loop` differs from `counters.loops` is not valid; an old-shape
      `last_review` (no `verdicts`) makes the review re-run.
- [ ] AC7 (G1). `test_reviewer`: template, `TEST_REVIEWER_SCHEMA`, `PERSONAS` entry, per-profile
      model/effort/budget as above; `build_request` is read-only and its prompt is
      deterministic and contains the `tests_touched` entries (changed and removed names).
- [ ] AC8 (G1). `tests_touched` returns, for a fixture repo, the changed test names (incl.
      `Class::name`), the removed test names (deleted function, deleted file, renamed file), a
      test whose only edit is a deleted assertion as `changed`, normalises `R087` to `R`, and
      falls back to the regex on a syntax error.
- [ ] AC9 (all). End to end with FakeRunner scripts: (a) conformance pass + test_reviewer fix then
      pass ⇒ `MERGED` with two implementer runs and the test findings in the ledger; (b) both
      pass ⇒ `MERGED`; (c) test_reviewer malformed twice ⇒ `PARKED(review_escalated)`;
      (d) fix until `loop_max` ⇒ `PARKED(loop_exhausted)`; (e) conformance `fix` + test_reviewer
      malformed once then valid pass ⇒ one implementer brief containing `[conformance_reviewer]`,
      conformance not re-spawned.
- [ ] AC10. Regression and docs: existing queue, close and e2e tests pass, the only existing
      assertions changed being `tests/test_agentic_queue.py:198,201` and
      `tests/test_agentic_e2e.py:399` (the e2e and queue helpers set `reviewers:
      [conformance_reviewer]`); `docs/product/capabilities/agentic.md` describes the `reviewers`
      key, the two reviewers and the new `last_review` shape; full `uv run pytest` green;
      `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_profiles.py`: AC1 — default lists per profile (exact equality),
    unknown/duplicate/non-read-only rejection, delta replace, absent key.
  - `tests/test_agentic_queue.py`: AC2 — `plan_step` review step (full list, retry subset,
    fallback without `reviewers` key), cap sum parametrized (fits / over by 0.01 / retry subset
    fits where the full list would not); AC3 — a `_apply_reviews` test with results supplied in
    reverse order asserting ledger order follows the profile list; AC4 — `combine_reviews`
    parametrized and the `last_review` shape; AC5 — `decide_reviews` cases: all valid; one
    malformed first time (retry, sibling kept); malformed twice (park names persona); valid `fix` +
    malformed sibling (no transition); counters reset after `pass` and after `fix`; no
    `review_retries_by_persona` key until used; resume into REVIEWING clears verdicts;
    `pending_brief.source == "review"`; the mixed A/B retry case; re-entry into REVIEWING from `gates_green` at the same loop clears
    the verdicts (CLOSING→TRIAGING→`retry_gate`→GATING→REVIEWING path) and `plan_step` with empty
    pending returns the full list; a timed-out reviewer treated
    as missing with sibling costs recorded; stale older-loop entry not counted; old-shape
    `last_review` re-runs; pool sizing (two reviewers, `slots: 1`, barrier).
  - `tests/test_agentic_personas.py`: AC7/AC8 — template, schema, request read-only,
    model/budget per profile, deterministic prompt (two builds equal), `previous_verdicts`
    of the second reviewer's request has no current-loop content, `tests_touched` fixtures
    (git repo in `tmp_path`).
  - `tests/test_agentic_e2e.py`: AC9 paths a-e, each reviewer scripted in the FakeRunner JSON
    script with varying delays (the test_reviewer finishing first and last); assert run dirs
    and event order are stable across repeated runs, and that the retried case does not spawn
    the sibling again.
- **Manual verification:** the next real campaign with the balanced profile records both
  reviewers' verdicts and costs here.
- **Regression guard:** full `uv run pytest`.

## Rollout / migration

Profiles without `reviewers` keep the conformance-only review (apart from the `From: review` label). In-flight campaigns in
`REVIEWING` resume with the new list (resume re-runs the review); an in-flight state with the
old `last_review` shape (no `verdicts`) is treated as "no valid verdicts" and the review re-runs.
Counters need no migration (see Counters). The running loop must be restarted to load the new
code.

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
