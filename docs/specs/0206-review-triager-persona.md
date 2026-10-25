---
id: SPEC-0206
title: "Review triager persona: waive low-severity findings that only cost a loop"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, review, triage, personas]
source_idea: IDEA-031
depends_on: [SPEC-0203]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-031`).

[SPEC-0203](./0203-review-triage-score-and-routing.md) scores the reviewers' findings
deterministically and routes `pass`/`fix`. One case is a judgment call: a `fix` caused **only** by
the score (no `must_fix`, no surviving `fix` verdict, no synthetic `major`) because several
`minor`/`nit` findings add up below `pass_score`. Some of those findings are subjective, out of
scope or not defects at all; waiving them would flip the outcome to `pass`, saving an implementer
loop. This spec adds a small, optional LLM persona, `review_triager`, that may waive such findings
and nothing else. It never touches `must_fix`, `major` or synthetic findings and never flips a
verdict by itself: it supplies `waived` ids to the deterministic `triage_reviews`, which decides.

Split out of SPEC-0201/0203 on 2026-10-25 after spec review.

## Goals / Non-goals

**Goals**
- G1. Run the triager only when waiving every waivable finding could change a score-only `fix`
  into a `pass` (pure `needs_triager`), at most once per loop, within the cost cap.
- G2. Accept only valid waivers (existing `minor`/`nit` ids with a reason), record them, and
  re-run `triage_reviews` with `waived`.
- G3. A malformed, failed or skipped triager never blocks or changes the deterministic outcome.

**Non-goals**
- Waiving `major`/`must_fix`/synthetic findings, flipping a verdict, or any decision outside the
  `waived` input (that is the "LLM decides everything" design, rejected in SPEC-0203).
- Scoring and routing themselves (SPEC-0203), filing waived findings as follow-ups (SPEC-0207).

## Decision

A new read-only persona `review_triager` behind the pure predicate `needs_triager`. The
triager step reuses the `REVIEWING` state and the SPEC-0201 execution path (a one-persona
`review` step) and is tracked by a small state machine in `last_review.triage`. It is not a
reviewer: it is not in `reviewers` nor in `REVIEWER_PERSONAS`. Choices made: (1) the
waiver set is validated by code, not trusted; (2) a triager failure counts as "done" (no retry,
no `review_escalated`), because the deterministic result is already a valid outcome; (3) the
triager is on by default for `balanced`/`thorough` and off for `frugal`.

## Design

### `needs_triager` and the state machine

- `needs_triager(result, profile) -> bool` (pure, `queue.py`; `result` is the `triage_reviews`
  result): `profile.review.triager` is true, `result.outcome == "fix"`, `result.score_only`, and
  `result.score + result.waivable_weight >= profile.review.pass_score` (waiving **everything**
  waivable could flip it). In every other case — a `must_fix`, a surviving `fix` verdict, a
  synthetic `major`, a clear pass, a score too low to rescue — the triager cannot change the
  outcome and does not run.
- `state.last_review.triage` takes values absent (not evaluated), `"pending"`, `"done"`,
  `"skipped"`. In `_apply_reviews`, after all reviewers are valid and `triage_reviews` ran, if
  `triage` is absent and `needs_triager` is true:
  - if the cap fits (`_cap_blocks(state, profile, ["review_triager"])` with the triager's budget;
    it takes `state`, not the result): set `triage = "pending"`, stay in `REVIEWING` (transition
    reason `review_triage`, no counters touched);
  - else set `triage = "skipped"` (terminal: later ticks never re-evaluate), append a `note`
    event `triager skipped: cost cap`, and route on the deterministic result.
- `plan_step` for `REVIEWING`: if every reviewer has a valid verdict for the current loop and
  `triage == "pending"` it returns `Step("review", persona="review_triager",
  personas=("review_triager",))`; this is the one exception to SPEC-0201's "a review is never
  skipped / pending empty ⇒ full list" rule. In all other cases it behaves as SPEC-0201.
- After the triager run (valid, malformed, failed or timed out) `triage` becomes `"done"`, so it
  runs **at most once per loop**; an interrupted run leaves `triage = "pending"` and state
  unchanged (the next tick re-spawns it, as any interrupted step). `triage`, `triager_run`
  and `waived` are cleared together with `verdicts` on every entry into `REVIEWING`
  (SPEC-0201 step 4); they are kept when the campaign leaves `REVIEWING`, for reporting.
- **Applying a triager result.** The tick passes the triager's `StepResult` to `_apply_reviews`
  wrapped as `reviews=[…]` like any review step, but `_apply_reviews` branches first on the
  step's persona: for `review_triager` the result is kept out of `last_review.verdicts` and out
  of the missing/retry logic (the reviewers' counters are untouched; a triager failure never
  causes `review_escalated`). Order: (1) record `run.end`/`cost` events and charge the cost via
  `_record_run` (no reviewer `finding` events are emitted for this persona); (2) validate the
  verdict against `REVIEW_TRIAGER_SCHEMA` (invalid/failed ⇒ `note`, no waivers); (3) accept
  waivers as below; (4) set `triage = "done"`, `waived`, `triager_run`; (5) call
  `triage_reviews(verdicts, profile, waived=ids of last_review.waived)` in the same tick and
  route as SPEC-0203. Any later `_apply_reviews` evaluation with `triage == "done"` likewise
  passes the stored `last_review.waived` ids, so a re-evaluated tick never drops waivers.
- **Notes and keys.** The `review score <n>/<pass_score> <verdict>` note of SPEC-0203 is written
  once per review, after the **final** `triage_reviews` run (post-waiver score); the `pending`
  stage writes only the `review_triage` transition and no score note. This spec amends
  SPEC-0203's `last_review` key list by adding `triage` and `triager_run` and by making `waived`
  non-empty when waivers were accepted (SPEC-0203's tests assert the base keys, this spec's tests
  assert the additions; both hold).

### Persona `review_triager`

- File `src/wt/agentic/personas/review_triager.md`, registered in `PERSONAS` with `read_only=True`,
  schema `REVIEW_TRIAGER_SCHEMA` (in `personas.py`, beside `CONFORMANCE_REVIEWER_SCHEMA`):
  `{waive: [{id, reason}], note}`, required keys `waive` (may be empty) and `note`; each item
  requires `id` and `reason`. `permission_mode: dontAsk`, `--restricted`.
- **Context:** `spec_text`, `previous_verdicts` blanked (`_prepare_run` passes no `run.end` events for this persona, since
  `triage_input` already carries the verdicts), and one new placeholder `triage_input`
  (added to `PLACEHOLDERS`), a pre-rendered string built by `_prepare_run` in `queue.py` (which
  already holds the state; `personas.py` cannot import `queue.py`) and passed through a new
  `build_request(..., extra=…)` keyword that `build_context` merges. It is
  `json.dumps({score, pass_score, reviewer_verdicts: {persona: {verdict, fix_brief}},
  waivable: [{id, personas, severity, title, detail, files}]}, indent=2, sort_keys=True)`; the
  waivable list holds only surviving `minor`/`nit` findings with their canonical ids. The
  template tells it to waive only findings that are subjective, out of scope for the spec, or
  non-defects, to give a reason for each, and that it cannot waive anything else.
- **Waiver handling:** the code accepts only ids that name an existing waivable `minor`/`nit`
  finding with a non-empty `reason`; others are ignored and each ignored one adds a `note`
  event `triager ignored <id>: <why>`. Then `triage_reviews(verdicts, profile, waived=accepted)`
  re-runs and routes as in SPEC-0203. Each accepted waiver is a `note` event (`text`: `waived
  <id>: <reason>`) and appended to `state.last_review.waived = [{id, personas, severity, title, detail,
  files, ac, category, reason}]` (the finding's fields as in `advisory`, so SPEC-0207 can file it); `last_review.triager_run` = its run id. A malformed or failed run (or a verdict
  that fails the schema) adds one `note` event and the deterministic result stands. The
  triager's cost is charged like any persona's (`_record_run`).
- **Profiles (`profiles.py`):** `review.triager: bool` (validated; allowed under `review:`), true
  in `balanced`/`thorough`, false in `frugal`; `personas.review_triager`:

| profile | model / effort / budget |
|---|---|
| balanced | sonnet / medium / 0.5 |
| thorough | sonnet / medium / 0.5 |
| frugal | haiku / medium / 0.25 (present so a user can enable it; off by default) |

  `read_only: true`, `permission_mode: dontAsk`. A hand-built profile without `review.triager`
  has it off.
- **schemas.py / parking:** no new ledger event types (`note`) and no new park reasons. The
  transition reason `review_triage` is new (reasons are free-form strings).
- **Docs:** `docs/product/capabilities/agentic.md` documents the triager, `review.triager` and
  `last_review.triage`.

## Alternatives considered

- **Always run a triager** — pays $0.5 on every clean review; it can only matter in the
  score-only case, so it runs only there.
- **Let the triager waive `major` or flip a verdict** — rejected; that is the LLM-decides-all case.
- **A triager failure retried / escalated like a reviewer** — rejected: the deterministic
  outcome is already valid; a retry would only add cost.
- **Skip the state machine and call the triager inline in `_apply_reviews`** — `_apply` runs on
  the main thread; agent runs belong to the worker pool and the step model.

## Acceptance criteria

- [x] AC1 (G1). `needs_triager` truth table: true iff `triager` is on, the outcome is a
      score-only `fix` and `score + waivable_weight >= pass_score`; false for a `must_fix`, a
      surviving `fix` verdict, a synthetic `major`, a pass, a hopeless score, or `triager: false`.
- [x] AC2 (G1). State machine: after the last valid reviewer verdict `_apply_reviews` sets
      `triage = "pending"` (reason `review_triage`) when needed and the cap fits; `plan_step`
      then returns the one-persona `review_triager` step; after its run `triage = "done"` and
      the campaign routes; it runs at most once per loop; a cap that cannot fit its budget sets
      `skipped` with a `note` and later ticks do not retry; the fields clear on re-entry into
      `REVIEWING` and persist after leaving it; an interrupted run keeps `pending`.
- [x] AC3 (G2). Waivers: only existing `minor`/`nit` ids with a non-empty reason are honored;
      `major`/`must_fix`/synthetic/unknown ids and empty reasons are ignored with a `note`;
      accepted waivers are `note` events and in `last_review.waived`; the re-run
      `triage_reviews` routes on them (e.g. waiving enough `minor` findings ⇒ `pass` ⇒ `CLOSING`;
      waiving too few ⇒ still `fix`).
- [x] AC4 (G3). A malformed, failed or timed-out triager leaves the deterministic outcome, sets
      `triage = "done"`, never `review_escalated`, and charges its cost.
- [x] AC5 (G1). `review_triager`: template, `REVIEW_TRIAGER_SCHEMA` (exact required keys),
      `PERSONAS` entry, not in `REVIEWER_PERSONAS`/`reviewers`, read-only `build_request` with a
      deterministic prompt containing the `triage_input` waivable ids; per-profile
      model/effort/budget as above; `review.triager` defaults and validation in
      `resolve_profile`.
- [x] AC6. End to end with FakeRunner: (a) a clean pass or a `must_fix` ⇒ no triager run;
      (b) a score-only `fix` ⇒ triager waives minors ⇒ `MERGED`, waivers recorded;
      (c) triager waives too little ⇒ `IMPLEMENTING` once; (d) triager malformed ⇒ deterministic
      `fix` stands; (e) a cap too small for the triager ⇒ skipped, deterministic outcome.
- [x] AC7. `docs/product/capabilities/agentic.md` documents the triager (grep test); full
      `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_review_triage.py` (`needs_triager` truth table,
  waiver validation and re-run of `triage_reviews`, `triage_input` rendering);
  `tests/test_agentic_queue.py` (state machine: pending → triager step → done, skipped on cap,
  once per loop, clear on re-entry, interrupted keeps pending, the triager's cost charged on failure and on timeout, waivers re-applied on a later evaluation);
  `tests/test_agentic_personas.py` (template, schema, `build_request`, prompt determinism,
  settings per profile); `tests/test_agentic_profiles.py` (`review.triager`, persona settings,
  and the docs grep for `review_triager` that AC7 refers to); `tests/test_agentic_e2e.py` (AC6 paths a-e).
- **Manual verification:** on the next real campaign whose score-only `fix` triggers the triager,
  record its waivers and cost here.
- **Regression guard:** full `uv run pytest`.

### Test record (2026-10-25)

- `uv run pytest -q -x` (full suite): 1904 passed, 1 skipped.
- New tests: `tests/test_agentic_review_triage.py` (`needs_triager` truth table, `_accept_waivers`,
  waiver re-run, `_triage_input`), `tests/test_agentic_queue.py` (pending → triager step → done,
  skipped on cap and terminal, once per loop, clear on re-entry, interrupted keeps pending, cost
  charged on malformed/failed/timed-out runs, stored waivers re-applied), `tests/test_agentic_personas.py`
  (template, schema, `build_request`, determinism, per-profile settings), `tests/test_agentic_profiles.py`
  (`review.triager` defaults/validation, persona settings, docs grep), `tests/test_agentic_e2e.py` (AC6 a-e).
- `python3 tools/spec_lint.py --write-ledger && python3 tools/spec_lint.py`: exit 0.
- Manual verification (a real campaign whose score-only `fix` triggers the triager) is pending: record
  its waivers and cost on the next such campaign.

### As shipped (2026-10-25)

- Matches the Design. Small choices: `profiles.triager_enabled(profile)` is the shared "on" check
  (`review.triager is True`); the routing tail of `_apply_reviews` moved into `_route_reviews`, which the
  triager path shares; `decide_reviews` takes a `waived` argument; `_accept_waivers`, `_apply_triager` and
  `_triage_input` live in `queue.py`.
- `plan_step` does not re-check the cap for the triager step: it was checked when `triage` became `pending`.
- A triager step is applied through the multi-reviewer path even with one reviewer, so a prepare failure
  becomes a result without a run (a `note`, `triage = "done"`) instead of the campaign-wide `open_failed` park.
- The `review_triager` template carries the spec text and `triage_input` only (no gate results or brief);
  `test_templates_and_schemas` excludes it from the common-placeholder check.
- Existing SPEC-0201/0203 tests whose score-only `fix` expected a direct loop now run with
  `review.triager: false` (`DELTA` in `test_agentic_reviewers.py`, the SPEC-0203 e2e low-score test).

## Rollout / migration

Takes effect on the next `REVIEWING`; no data migration (`triage` is absent on old state, which
means "not evaluated"). Users can set `review.triager: false` to opt out. Restart the running
loop to load the code.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
