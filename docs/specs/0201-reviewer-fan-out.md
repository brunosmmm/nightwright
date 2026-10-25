---
id: SPEC-0201
title: "Reviewer fan-out: run several reviewer personas in parallel in REVIEWING"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, review, queue]
source_idea: IDEA-029
depends_on: [SPEC-0187]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-029`).

[SPEC-0187](./0187-conformance-review-reviewing-state-with-a-read.md) put one independent
reviewer between `GATING` and `CLOSING`. Further independent lenses (tests, adversarial) need
`REVIEWING` to run **several** reviewers at once, wait for all of them and merge their verdicts
into one routing decision. This spec adds only that infrastructure; it ships with a single member
(`conformance_reviewer`) and changes no review behaviour: with one reviewer the brief, routing and events are byte-for-byte
SPEC-0187's, and only `last_review` gains keys.
[SPEC-0205](./0205-test-reviewer-persona.md) adds the test reviewer,
[SPEC-0202](./0202-adversarial-reviewer-persona.md) the adversarial one, and
[SPEC-0203](./0203-review-triage-score-and-routing.md) replaces the interim
combiner defined here.

Split out of the original SPEC-0201 and SPEC-0203 on 2026-10-25 after spec review
(docs/specs/reviews/SPEC-0201.json: the original SPEC-0201 also carried the test reviewer persona,
now SPEC-0205). `source_idea: IDEA-029` stays because the idea is only delivered once SPEC-0205
lands: `wt spec reconcile --apply` must not close IDEA-029 on this spec alone.

## Goals / Non-goals

**Goals**
- G1. A per-profile `reviewers` list; `REVIEWING` runs all configured reviewers **in parallel**,
  each a fresh run with its own budget, run directory and ledger entries.
- G2. The cost cap covers the budgets of the reviewers about to run, checked before any spawn.
- G3. A minimal interim combination rule so the queue works before SPEC-0203 lands.
- G4. Per-persona malformed-verdict / failure handling (one retry each) that never loses a valid
  sibling verdict, and `review_escalated` parking.
- G5. The persisted `last_review` shape and the merged `fix` brief for multiple reviewers.

**Non-goals**
- Any new persona (tests: SPEC-0205; adversarial: SPEC-0202), score weighting and waivers
  (SPEC-0203), the triager (SPEC-0206), follow-up filing (SPEC-0207).
- Changing the gates or the close step.

## Decision

Make `REVIEWING` a fan-out step. The profile lists reviewer personas; the queue spawns one run
per reviewer concurrently, waits for all, and combines the verdicts with one pure function. After
this spec the only registered reviewer is `conformance_reviewer` and every builtin profile has
`reviewers: [conformance_reviewer]`; later specs append personas to `PERSONAS`,
`REVIEWER_PERSONAS` and the default lists.

Reasoning for the choices that were open: (1) the malformed-retry counter gets a **new** key and
the existing int stays, so in-flight campaigns and the existing assertions
(`tests/test_agentic_queue.py:171,188,195,214`, `tests/test_agentic_e2e.py:247,424`) need no
migration; (2) the combiner waits until every reviewer has a valid verdict (or is out of retries)
before routing, because routing on a partial set would send an implementer off with a brief
that a still-pending reviewer may contradict; (3) the cost cap stays pessimistic (as
`_cap_blocks`) and the default `campaign_cap_usd` is left alone, since it is
compared with *actual* spend, not with the sum of budgets; (4) with one registered reviewer,
multi-reviewer behaviour is exercised in tests by a fixture that registers a test-only second
reviewer (a copy of the conformance `PERSONAS` entry under another name, added to
`REVIEWER_PERSONAS` and to the profile's `personas`), so this spec does not depend on
SPEC-0205/0202.

## Design

### Multi-reviewer REVIEWING (referenced by SPEC-0205, SPEC-0202, SPEC-0203 and SPEC-0206)

- **Profile key** `reviewers: [persona, …]` — persona names, each of which must exist in
  `PERSONAS` and in `profile["personas"]` and have `read_only: true`.
  **This spec ships** `BUILTIN_PROFILES` (`profiles.py`) with `reviewers: [conformance_reviewer]`
  for `balanced`, `thorough` and `frugal`. SPEC-0205 and SPEC-0202 append `test_reviewer` and
  `adversarial_reviewer` to the `balanced`/`thorough` lists; they are not listed here because
  `resolve_profile` rejects personas absent from `PERSONAS`.
  One helper `reviewers_of(profile) -> list[str]` in `profiles.py` returns the effective list
  (absent or empty ⇒ `["conformance_reviewer"]`) and is the only reader used by `plan_step`,
  the tick expansion, `_apply_reviews`, the `_prepare_run` filter and the cost check;
  `resolve_profile` itself returns `reviewers: []` as configured. `resolve_profile` validates the list (unknown, duplicate, missing-settings or non-read-only
  persona ⇒ `ValueError` naming it and the profile); `deep_merge` replaces lists wholesale, so a
  user delta `reviewers: [conformance_reviewer]` narrows it. An absent or empty key (including a
  hand-built profile dict, as in `tests/test_agentic_queue.py:20`) falls back to
  `[conformance_reviewer]` through `reviewers_of`, which is SPEC-0187 behavior.
- **Plan:** `Step` gains `personas: tuple[str, …] = ()`. `plan_step` for
  `REVIEWING` returns `Step("review", persona=<first pending persona>, personas=<pending
  personas in profile order>)`. `persona` stays populated (the first pending one) so the cap-park
  step, the existing parametrized plan test (`tests/test_agentic_queue.py:36`) and `_apply`'s
  `res.step.persona` keep working. *Pending* = reviewers in the list that have no valid
  verdict for the current loop in `state.last_review.verdicts` (every reviewer on a first pass; only
  the missing ones on a retry pass). If *pending* is empty, `plan_step` returns the
  full reviewer list (a review is never skipped; SPEC-0206 adds its one exception, for its
  triager step).
- **Execution:** the tick expands one `review` step into one `_prepare_run` + `_do_persona`
  task per pending persona (`_prepare_run`, the tick's pool), each producing its own
  `build_request`, run directory (`runs/NNN-<persona>/`), `run.start`/`run.end`, `cost` and
  `finding` events (`persona` = that reviewer). The thread pool is sized
  `max(slots, number of agent tasks in the tick)` (the count of all `pending_agent` entries, gate and persona tasks, after expanding every
  review step) so every
  campaign's reviewers run concurrently even when `slots` is smaller (a fan-out may therefore
  briefly exceed `slots`; accepted, the alternative is a half-run review; the number of active
  campaigns is already bounded by admission). When all of a campaign's futures finish, their
  `StepResult`s are sorted into profile-list order, wrapped in one aggregate
  `StepResult(reviews=[…])` and passed to `_apply` **once** (new `_apply_reviews`, replacing
  `_apply_review`). Events are therefore appended in profile-list order regardless
  of which reviewer finished first. Run ids are allocated in profile-list order before any
  task starts.
- **Independence:** `previous_verdicts` of a reviewer's request are built by `_prepare_run`
  from the ledger (`run.end` events, tail 2, as today) but, **for reviewer personas only**,
  filtered (read the whole `run.end` list, filter, **then** take the last 2) to events whose run number (the `NNN` prefix of `run_id`) is ≤
  `last_review.entry_run`, a new int written on every entry into REVIEWING (step 4 below) and
  equal to `counters.runs` at that moment. Runs started in the current review pass-through, in
  the first pass or in a retry pass (whose numbers exceed `entry_run`), are therefore never
  shown to any reviewer, whereas the implementer's and earlier loops' `run.end` events survive the filter and fill the
  tail of 2 exactly as before. Non-reviewer personas are unchanged.
- **Failure paths in the tick** (several independently testable rules):
  1. *One reviewer configured:* every failure path below is unchanged from SPEC-0187 and wins
     over rules 3-5: any `res.error` or raising worker parks `open_failed` / `config_invalid`
     through the existing path (campaign-wide error result, no synthetic `run.end`, no retry),
     and an interrupted pass leaves state unchanged (no verdict stored); a test pins each.
  2. *Precedence with two or more reviewers*, when one pass mixes outcomes: **interrupted**
     first (rule 5), then **`config_error`** (rule 6), then **missing** (rules 3-4, the normal
     retry logic).
  3. `_prepare_run` is arranged so that everything that can raise (`build_request`, directory
     creation) happens before its first side effect (the `run.start` append). A `ValueError`/
     `OSError` raised there therefore leaves no trace: that persona gets a per-persona error
     result (no `run.start`, no cost), siblings still run, and the persona is *missing*
     (consuming its retry like a hard failure) with a `note` event naming the error. A failure
     of the final `write_state` inside `_prepare_run` is a rule 4 case.
  4. *Two or more reviewers:* a worker future raising (or any exception after `run.start` was written) yields a
     per-persona result with a synthetic failed `RunResult` (`is_error=True`,
     `verdict_ok=False`, `cost_usd=None`, `result_tail=repr(exc)`), so `_record_run` writes the
     matching `run.end` and `cost` (budget charged pessimistically) and no `run.start` is
     orphaned; the persona is *missing*; the exception is also appended to `report.errors` (the
     tick's handler today builds one campaign-wide `implement` error result; for a `review` step
     it builds one per persona).
  5. *Two or more reviewers:* when `ctx.stop` is set or a run is `interrupted`, the aggregate is recorded (events, costs,
     valid verdicts stored in `last_review.verdicts` with the current `loop`) but no decision is
     made and no retry counter changes, as the single-reviewer interrupted case (state
     unchanged, `interrupted` reason); the next tick re-spawns only the personas still without
     a valid verdict (a persona never prepared because `ctx.stop` was set between two
     `_prepare_run` calls is simply still pending). If every reviewer was valid when the
     interruption hit, the next tick re-runs them all (accepted: rare, and a review is never
     skipped).
  6. A `config_error` result of any reviewer parks `config_invalid` as today, after the
     siblings' events and costs are recorded.
  7. Aggregate bookkeeping: `state["last_verdict"]` is that of the last reviewer in profile
     order; the report row's `run_id` is that run's id and its `cost_usd` the sum over the pass.
     A bare single-persona `review` `StepResult` passed to `_apply` (as the existing test helper
     does, `tests/test_agentic_queue.py:241-245`) is wrapped by `_apply` into a one-element
     `reviews`, so `_apply_reviews` is the only code path; a state without
     `last_review.entry_run` (hand-built or old) disables the `previous_verdicts` filter above
     (behaviour as today).
- **Cost cap:** `_cap_blocks` generalises to `_cap_blocks(state, profile, personas)`: the needed
  amount is the sum of `persona_settings(profile, p)["max_budget_usd"]` over the **pending**
  personas, compared with `cost.cap_usd − cost.total_usd` (the state key is `total_usd`). If it does not fit: `PARKED(cost_cap)`, no reviewer spawned (never a partial
  fan-out; a retry pass checks only its pending reviewers). The park detail names the personas and
  the sum. A `cost_cap` park on a retry pass followed by a resume re-runs every reviewer (the resume
  is an entry into REVIEWING and clears the verdicts, step 4): accepted, it is rare and keeps the
  rule uniform. Each reviewer's cost is charged on its own `cost` event with that persona, by
  `_record_run`, as now. Default cap impact: none (one reviewer, as SPEC-0187); `campaign_cap_usd`
  defaults are not changed. Later reviewer specs state their own budget impact.
- **Interim combiner** `combine_reviews(verdicts) -> {verdict, must_fix, findings, fix_brief}`
  (pure, in `queue.py`; SPEC-0203 replaces it and states the call-site change): `verdicts`
  is an ordered `{persona: verdict_dict}` of valid verdicts. The result is `fix` if any reviewer
  returned `fix` or any finding has severity `must_fix`, else `pass`. A *fixing* reviewer is one
  with verdict `fix` or any `must_fix` finding. With two or more reviewers `fix_brief` is each
  fixing reviewer's `fix_brief` prefixed `[<persona>] `, the entries joined by one blank line,
  followed (after a blank line) by one line per `must_fix` finding in profile order then list
  order, `must_fix: <title>`; e.g. `[A] do X\n\n[B] do Y\n\nmust_fix: tests missing`. With one
  reviewer `fix_brief` is that reviewer's text verbatim. `must_fix` is the list of must-fix
  findings (`{ac, title, detail, files}`) with `persona` added when two or more reviewers are
  configured; `findings` is the count of all findings.
- **Decision order in `_apply_reviews`** (the answer to "valid `fix` plus malformed sibling"):
  1. Merge this pass's per-persona outcomes into `last_review.verdicts` (valid ⇒ stored with
     `loop`; malformed ⇒ left absent).
  2. Let *missing* be the reviewers without a valid verdict **for the current loop** (an entry
     counts only when `entry.loop == counters.loops` (defensive: verdicts are cleared on entry, so
     a stale entry arises only from a hand-built state, which is how the test constructs it); an older loop's entry for the same persona
     does not, and is overwritten on success). A reviewer that failed hard (runner error, timeout,
     budget kill, `rr.verdict_ok` false) is *missing* exactly like a malformed one: same retry
     rule, and its sibling `StepResult`s are still applied (events, costs and valid verdicts are
     recorded in step 1 before anything is decided; the ledger is never half-applied). If **any**
     missing persona has already used its single retry
     (retries used by `persona` ≥ `REVIEW_RETRY_MAX`) ⇒
     `PARKED(review_escalated)` naming exactly those personas (even if a sibling said `fix`).
     Otherwise (every missing persona still has retry budget) increment the counter for each
     missing persona and the legacy int `review_retries` by one, and stay in `REVIEWING`
     (`review_retry`). Mixed case: A missing for the second time and B for the first ⇒ park naming
     A only; B is never spawned a third time. A hard failure's charged cost (its budget when
     the runner reported none) is already in `cost.total_usd` when the retry's cap check runs. **No routing happens while any verdict is missing:** a valid `fix` is
     held, not sent to the implementer, so one merged brief is produced per loop.
     *Retries used by `persona`* = `counters.review_retries_by_persona[persona]` when present,
     else, **only when exactly one reviewer is configured**, the legacy int `review_retries`,
     else 0. So an in-flight single-reviewer campaign whose int is already 1 escalates on the
     next malformed verdict exactly as before, the `decide_review` wrapper (which passes its
     single reviewer) reads the int, and the existing `:171` style tests need no change; with two
     or more reviewers a leftover int is ignored (the resume re-runs the review anyway).
  3. When no reviewer is missing: `combine_reviews`; `pass` ⇒ `CLOSING`
     (`review_pass`); `fix` ⇒ `loops += 1`; beyond `loop_max` ⇒ `PARKED(loop_exhausted)`
     (detail carries the merged brief, as `decide_review`); else
     `IMPLEMENTING` (`review_fix`).
  4. **Entering `REVIEWING` always starts a fresh review.** The single choke point is
     `_transition` (`queue.py`): when `to == "REVIEWING"` and the previous state is not
     `REVIEWING`, it clears `last_review.verdicts`, the per-review fields later specs add
     (SPEC-0206's `triage`, `triager_run`, `waived`), `counters.review_retries` and `review_retries_by_persona`, and sets
     `last_review.entry_run = counters.runs`, before writing the state. Every way into
     `REVIEWING` goes through it: `gates_green` (which also happens at an unchanged
     `counters.loops` after CLOSING→TRIAGING→`retry_gate`→GATING), a human resume (directly or
     via `_enter_slot_or_wait`, including the release of a campaign parked in `WAITING` with
     `resume_to: REVIEWING`, `slot_free`), and `config_recovered`. Self-transitions inside
     `REVIEWING` (`review_retry`) do not clear. Every other rebuild of `last_review`
     (`_apply_reviews` on a retry pass) carries `entry_run` forward. So a `pass` on older code
     is never reused.
  5. On any exit from `REVIEWING` by transition (`CLOSING` or `IMPLEMENTING`) both
     `review_retries` and `review_retries_by_persona` are reset (to 0 and removed), i.e. the
     allowance is **one retry per persona per review pass-through**, regardless of whether the
     pass-through ended in `pass` or `fix`. Resuming a `PARKED(review_escalated|cost_cap)`
     campaign into `REVIEWING` is such an entry, so the resumed review re-runs every reviewer (matches the "resume re-runs the review"
     rule in `_resume_target`).
  `decide_reviews(state, profile, valid, failed)` (pure; `valid` = ordered `{persona: verdict}`
  of every reviewer with a valid current-loop verdict after step 1, `failed` = the set of
  personas that produced no usable verdict in this pass) implements steps 2-3 and returns a
  `Step` extended with `missing` (personas to retry, in profile order), `escalated` (personas
  out of retries) and, when routing, the `combine_reviews` result; `_apply_reviews`, not
  `decide_reviews`, mutates the counters from `missing`;
  `decide_review(state, profile, verdict, ok)` stays as a thin one-reviewer wrapper with its
  current signature and behaviour (the existing direct tests, `tests/test_agentic_queue.py`
  `decide_review` cases, keep passing), and `_cap_blocks(state, profile, personas)` accepts a
  persona name or a list so the implementer/triage callers are unchanged.
- **Counters:** `counters.review_retries` stays an int (`_counters` seeding,
  unchanged), meaning "retry passes used in this review". The per-persona map is
  `counters.review_retries_by_persona` and is **not seeded** by `_counters`; it appears only
  after the first malformed verdict, so no in-flight campaign needs migration and the e2e
  equality assertion on the counters dict (`tests/test_agentic_e2e.py:247`) still holds.
  The reads/writes at `tests/test_agentic_queue.py:171,188,195,214` and
  `tests/test_agentic_e2e.py:424` keep working because the int is still maintained.
- **State:** `state.last_review` becomes
  `{loop, entry_run, run, runs, verdicts, verdict, score, findings, must_fix}`:
  - `verdicts: {persona: {verdict, score, fix_brief, findings: [<the reviewer's full finding
    dicts as returned: severity, title, detail, files, plus ac/category when present>],
    ac_assessment (conformance reviewer only, as returned), run,
    loop}}`, one entry per reviewer, persisted in state so later ticks (a retry pass,
    SPEC-0206's triager step) can recombine without re-reading the ledger; **overwritten**
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
  is updated to add the new keys `runs` and `verdicts` (`entry_run` is written only on entry to
  REVIEWING, so it is absent in that hand-built state).
- **Counters on `fix`:** `_apply_reviews` keeps the resets of the single-reviewer code
  (in `_apply_review`): `loops += 1`, `gate_retries = 0`, `triage_retries = 0`, and preserves
  `pending_brief.human_notes` when building the new brief.
- **Brief:** with **more than one** reviewer configured: `pending_brief.source` = `review`,
  `pending_brief.personas` lists the contributing (fixing) personas (*fixing* = verdict `fix`
  or any `must_fix` finding), `fix_brief` is the merged
  persona-labelled brief of the combiner, each `must_fix` entry also has `persona`, and
  `human_notes` as before; the implementer prompt's `From: <source>` line prints `review`. With
  **exactly one** reviewer the brief is SPEC-0187's, unchanged: `source` = that persona,
  `fix_brief` the reviewer's own text (no prefix, no appended `must_fix:` lines), `must_fix`
  entries without `persona`, no `personas` key. So the existing assertions at
  `tests/test_agentic_queue.py:198` and `tests/test_agentic_e2e.py:399` stay valid; the only
  existing assertion this spec permits to change is the whole-`last_review` equality at `:201`.
- **Existing tests keep passing** because the default (and the hand-built profile fallback) is
  `[conformance_reviewer]`; the new tests exercise two reviewers through the test-only second
  reviewer fixture (Decision, point 4).
- **`REVIEWER_PERSONAS`** (`personas.py`): module constant `("conformance_reviewer",)`;
  `build_context`'s `review=` flag (currently `persona ==
  "conformance_reviewer"`) becomes `persona in REVIEWER_PERSONAS`, so each later reviewer gets the
  review context (diff, gate summary, previous verdicts) by appending its name. A name is valid in
  `reviewers` iff it is in `PERSONAS`, `read_only`, in `REVIEWER_PERSONAS` and in
  `profile["personas"]`.

### schemas.py, parking and profiles

- `schemas.py`: no change. No new ledger event types; `run.start`/`run.end`/`finding`/`cost`
  already carry `persona`.
- No new park reasons: `review_escalated` (a reviewer missing after its retry; detail names the
  personas), `cost_cap` (sum of pending budgets), `loop_exhausted` (fix at `loop_max`) are reused.
- `profiles.py`: profile schema gains `reviewers` (validated in `resolve_profile`); the three
  builtin profiles set it to `[conformance_reviewer]`. A profile without it behaves as SPEC-0187.
- `docs/product/capabilities/agentic.md`: describes the `reviewers` key and the new
  `last_review` shape.

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

## Acceptance criteria

- [ ] AC1 (G1). `resolve_profile` returns exactly `reviewers == ["conformance_reviewer"]` for
      `balanced`, `thorough` and `frugal`; rejects an unknown, duplicate, non-read-only or
      non-`REVIEWER_PERSONAS` persona, or one missing from `profile["personas"]` (no settings), with a
      `ValueError` naming it and the profile; a user delta replaces the list; absent/empty key ⇒
      conformance only.
- [ ] AC2 (G1, G2). `plan_step` for `REVIEWING` returns a `review` step with `personas` = the
      pending reviewers in profile order and `persona` = the first (full list when pending is
      empty); the cap check sums the pending reviewers' `max_budget_usd` against
      `cap_usd − total_usd`, parks `cost_cap` (detail names the personas and sum) without
      spawning any when over, and a retry pass sums only the still-pending reviewers.
- [ ] AC3a (G1). Reviewers run concurrently as independent runs (own run dir, budget, `run.*`,
      `cost` and `finding` events); run ids are allocated in profile-list order before any task
      starts; the pool is sized `max(slots, pending_agent entries)` (two reviewers with
      `slots: 1` are both in flight at once, shown with a barrier in the FakeRunner); events are
      applied in profile-list order even when the runs finish in reverse order.
- [ ] AC3b (G1, G4). No reviewer's request contains a run of the current review pass-through
      (`previous_verdicts` is filtered by `last_review.entry_run`, filter-then-tail, and earlier
      loops' reviewer runs still appear); each rule of "Failure paths in the tick" (single
      reviewer unchanged; prepare failure leaves no trace; worker exception writes a matching
      `run.end`/`cost`; interrupted keeps state; `config_error` parks; precedence) has a test.
- [ ] AC4 (G3, G5). `combine_reviews`: any `fix` verdict or `must_fix` finding ⇒ `fix` with a
      merged, persona-labelled `fix_brief`; all `pass` ⇒ `pass`; `state.last_review` has the
      shape above (per-persona `verdicts` with full findings, `runs`, `run` = last reviewer's
      run, SPEC-0187 keys) and with two or more reviewers `pending_brief.source == "review"` listing the fixing personas, whereas a single reviewer's brief is SPEC-0187's unchanged.
- [ ] AC5 (G4). Retry and park: a missing reviewer (malformed or hard failure) is retried once
      while valid siblings are kept and **not** re-spawned; a valid `fix` plus a missing sibling
      does not route until the sibling is resolved; missing after its retry ⇒
      `PARKED(review_escalated)` naming only the personas out of retries (the mixed A/B case
      names A only), even if a sibling said `fix`; sibling events and costs are recorded for a
      hard failure.
- [ ] AC6 (G4). Counters and freshness: `review_retries` stays an int and
      `review_retries_by_persona` is absent until used; both reset on exit from REVIEWING by
      `pass` and by `fix`; every entry into REVIEWING through `_transition` (from `gates_green` at an unchanged loop
      after CLOSING→TRIAGING→GATING, a human resume, a resume released from `WAITING`, and
      `config_recovered`) clears `verdicts` so the review re-runs; a
      verdict whose `loop` differs from `counters.loops` is not valid; an old-shape
      `last_review` (no `verdicts`) makes the review re-run.
- [ ] AC7 (G1-G5). End to end with FakeRunner and the two-reviewer fixture: (a) A pass + B fix
      then both pass ⇒ `MERGED` with two implementer runs; (b) both pass ⇒ `MERGED`; (c) B
      malformed twice ⇒ `PARKED(review_escalated)`; (d) fix until `loop_max` ⇒
      `PARKED(loop_exhausted)`; (e) A `fix` + B malformed once then valid pass ⇒ one implementer
      brief containing `[A]`, A not re-spawned.
- [ ] AC8. Regression and docs: with the default `[conformance_reviewer]` existing queue, close
      and e2e tests pass, the only existing assertion changed being
      `tests/test_agentic_queue.py:201` (new `last_review` keys), and a single-reviewer brief
      identical to SPEC-0187's (`:198` and e2e `:399` unchanged);
      `docs/product/capabilities/agentic.md` describes the `reviewers` key and the new
      `last_review` shape; full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_profiles.py`: AC1 — default lists per profile (exact equality),
    unknown/duplicate/non-read-only/non-`REVIEWER_PERSONAS` rejection, delta replace, absent key.
  - `tests/test_agentic_queue.py` (with the `second_reviewer` fixture): AC2 — `plan_step` review
    step (full list, retry subset, fallback without `reviewers` key), cap sum parametrized (fits
    / over by 0.01 / retry subset fits where the full list would not); AC3 — a `_apply_reviews`
    test with results supplied in reverse order asserting ledger order follows the profile list,
    and pool sizing (two reviewers, `slots: 1`, barrier); AC4 — `combine_reviews` parametrized and
    the `last_review` shape; AC5 — `decide_reviews` cases: all valid; one malformed first time
    (retry, sibling kept); malformed twice (park names persona); valid `fix` + malformed sibling
    (no transition); the mixed A/B retry case; `_prepare_run` failure for the second reviewer,
    worker exception (synthetic failed `RunResult`, matching `run.end`/`cost`), interrupted run, and the single-reviewer error case (still `open_failed`/`config_invalid`) (AC3b failure paths); `reviewers_of` with absent and empty keys, and `decide_review` wrapper parity; a timed-out reviewer treated as missing with
    sibling costs recorded; AC6 — counters reset after `pass` and after `fix`; no
    `review_retries_by_persona` key until used; resume into REVIEWING (direct, via WAITING release, and `config_recovered`) clears verdicts and sets `entry_run`; a retry pass keeps `entry_run`; `decide_reviews` returns `missing`/`escalated` without mutating counters; re-entry
    from `gates_green` at the same loop clears verdicts and `plan_step` with empty pending returns
    the full list; a single-reviewer state with only the legacy int `review_retries` = 1 escalates on the next malformed verdict while two reviewers ignore the int; stale older-loop entry not counted; old-shape `last_review` re-runs;
    `pending_brief.source == "review"` (two reviewers) and the unchanged single-reviewer brief; `previous_verdicts` filtering by `entry_run` (a retry
    pass does not see the kept sibling's run; an earlier loop's reviewer run is shown).
  - `tests/test_agentic_personas.py`: `REVIEWER_PERSONAS` gives the review context to a listed
    persona and not to the others.
  - `tests/test_agentic_e2e.py`: AC7 paths a-e with the fixture reviewer scripted in the
    FakeRunner JSON with varying delays (second reviewer finishing first and last); assert run
    dirs and event order are stable across repeated runs and the retried case does not spawn the
    sibling again.
- **Manual verification:** none until SPEC-0205/0202 add a second real reviewer; then their
  campaigns record both verdicts here.
- **Regression guard:** full `uv run pytest`.

## Rollout / migration

Profiles without `reviewers` keep the conformance-only review unchanged. In-flight campaigns in `REVIEWING` resume (resume re-runs the review); an in-flight state
with the old `last_review` shape (no `verdicts`) is treated as "no valid verdicts" and the review
re-runs. Counters need no migration (see Counters). The running loop must be restarted to load
the new code.

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
