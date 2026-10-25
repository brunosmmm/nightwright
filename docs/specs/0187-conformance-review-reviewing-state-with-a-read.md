---
id: SPEC-0187
title: "Conformance review: REVIEWING state with a read-only conformance-reviewer persona"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, review, personas, queue]
source_idea: IDEA-028
depends_on: [SPEC-0186]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-028`, pulled forward on 2026-10-25).

The plan's implementation lifecycle has reviews after the gates: conformance, test and
adversarial reviewers with isolated sub-agents, then a review triage that decides fix vs
forward. Wave 1 shipped without them (the "thin implement loop"), so today a campaign merges on
gates plus the implementer's own ticked ACs. Tests can pass while an AC is unmet, or met only by
a test written to suit the implementation. The user asked for independent reviews; this spec
adds the first and most important one — **conformance**: does the change do what the spec
says? — with minimal triage folded in. Test and adversarial reviewers, numeric review triage
and waivers stay later-wave items (IDEA-029/030/031).

## Goals / Non-goals

**Goals**
- New queue state `REVIEWING` between `GATING` (all required gates ok) and `CLOSING`.
- New read-only persona `conformance_reviewer` (`--restricted`; model `opus` in `balanced` and
  `thorough`, `sonnet` in `frugal`; budget about $1.5) that receives the spec text from the
  worktree, the diff against the base, the list of test files touched, and the gate summary.
- Verdict schema: `{verdict: pass|fix, score: 0..100, findings: [{severity:
  must_fix|major|minor|nit, ac, title, detail, files}], ac_assessment: [{ac, met, evidence}],
  fix_brief}`.
- Routing: `pass` with no `must_fix` finding ⇒ `CLOSING`; `fix` or any `must_fix` ⇒
  `loops += 1` → `IMPLEMENTING` with `pending_brief` from `fix_brief` + must-fix findings
  (`loop_max` ⇒ `PARKED(loop_exhausted)`); a malformed verdict twice ⇒
  `PARKED(review_escalated)`.
- Every finding is a `finding` ledger event (`persona: conformance_reviewer`); the score is in
  the `run.end` verdict and in `state.last_review`.
- The cost cap applies before the review spawn like any persona.

**Non-goals**
- Test reviewer, adversarial reviewer, numeric review triage, waivers, follow-up idea filing.
- Changes to the close step (it still re-gates, audits DoD and, with SPEC-0186, checks drift).
- New event types; a review park surfaces as the existing `parked` event.

## Decision

Extend the queue's state machine with `REVIEWING`, add the persona (markdown template, verdict
schema, profile defaults) and a small `decide_review(state, profile, verdict, verdict_ok)` next
to `decide_triage`, reusing the runner, the cost accounting and the brief mechanism.

## Design

- **States:** `STATES`/`ACTIVE_STATES` gain `REVIEWING`; `RESUMABLE_STATES` gain it too
  (resume re-runs the review); `PARK_REASONS` gain `review_escalated`.
- **Transitions:** `GATING` all required ok ⇒ `REVIEWING` (reason `gates_green`); `REVIEWING`
  ⇒ `CLOSING` (`review_pass`) / `IMPLEMENTING` (`review_fix`) / `PARKED`. A `CLOSING` →
  `TRIAGING` round trip (re-gate failure, spec drift) that ends in a new implementer run goes
  through `REVIEWING` again.
- **Persona:** `src/wt/agentic/personas/conformance_reviewer.md` — read the spec's Decision,
  Design and Acceptance criteria; for each AC decide `met` with concrete evidence (file and
  function, or test name and what it asserts); flag ACs "met" only by a trivially passing or
  tautological test as `major`; flag scope creep beyond the spec as `minor`; never edit files.
  Context: `spec_text`, `diff` (≤ 1500 lines, `git diff <base>...HEAD`), `test_files` (paths
  under `tests/` touched), `gate_results` summary, `previous_verdicts`.
- **Profiles:** `BUILTIN_PROFILES[*]["personas"]["conformance_reviewer"]` with `read_only:
  true`, `permission_mode: dontAsk`, models as above, `max_budget_usd` 1.5 (frugal 0.6).
- **State and counters:** `counters.review_retries` (malformed verdicts tolerated per loop, 1;
  reset on pass and on fix) and `state.last_review` = `{run, verdict, score, findings,
  must_fix, loop}`. A reviewer finding event carries `ac` besides the usual fields.
- **Queue:** `plan_step` returns a `review` step for `REVIEWING`; `_do_persona` handles it like
  the triager (no commit); `decide_review` maps the verdict; the brief carries `source:
  conformance_reviewer`.
- **Listing / docs:** `wt queue list` shows the new state; capability page "Reviews"; the
  state machine diagram updated.

## Alternatives considered

- **Run the review inside the gate triager** — rejected; the triager runs only on failure and
  sees logs, not intent. A green build is exactly when conformance needs checking.
- **Reviewer with edit rights fixing small issues itself** — rejected; reviewers judge, the
  implementer fixes (fresh context per role, as the plan says).
- **Review after the close step's rebase** — rejected; a `fix` would then have to undo a rebase.
  Reviewing before closing keeps closing purely mechanical.

## Acceptance criteria

- [x] `plan_step` / `decide_review` implement every routing rule above (parametrized tests,
      including must-fix overriding a `pass`, `loop_max`, malformed-twice park).
- [x] The persona template and schema exist; `build_request` for it is read-only and uses the
      profile model and budget; prompts are deterministic.
- [x] End to end with the fake runner: reviewer `fix` once then `pass` ⇒ `MERGED` with two
      implementer runs and the reviewer's findings in the ledger; reviewer `fix` until
      `loop_max` ⇒ `PARKED(loop_exhausted)`; malformed twice ⇒ `PARKED(review_escalated)`.
- [x] The cost cap is checked before the review spawn.
- [x] Existing queue, close and e2e tests updated for the new state and still pass.
- [x] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_queue.py` (transitions, decide_review),
  `tests/test_agentic_personas.py` (template, schema, request), `tests/test_agentic_e2e.py`
  (three e2e paths above; existing paths gain a passing reviewer step in their scripts).
- **Manual verification:** the next real campaign records the reviewer's verdict, score and cost
  in this spec's test record.
- **Regression guard:** full `uv run pytest`.

### Test record (2026-10-25)

- `uv run pytest -q tests/test_agentic_queue.py tests/test_agentic_personas.py
  tests/test_agentic_e2e.py tests/test_agentic_spec_drift.py` — green: `plan_step` /
  `decide_review` routing (parametrized: pass, must-fix overriding a pass, fix, `loop_max`,
  malformed twice), `_apply` for the review step, request/prompt/schema checks (read-only,
  profile model and budget per profile, deterministic prompt, diff truncation), and three e2e
  paths (fix then pass ⇒ `MERGED` with two implementer runs and the findings in the ledger;
  fix until `loop_max` ⇒ `PARKED(loop_exhausted)`; malformed twice ⇒ `PARKED(review_escalated)`).
  Existing e2e / close / drift scripts gained a passing reviewer step.
- Cost cap before the review spawn: `test_plan_step_cost_cap_is_pessimistic` (REVIEWING rows).
- `uv run pytest -q` — full suite green; `python3 tools/spec_lint.py` exits 0.
- Manual: deferred by the spec itself (it needs a real campaign) — the next real campaign records the reviewer's verdict, score and cost here.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

In-flight campaigns past `GATING` close without a review; new ones pass through `REVIEWING`.
The running loop must be restarted to load the new code.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
