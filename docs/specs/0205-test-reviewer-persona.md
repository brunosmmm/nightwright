---
id: SPEC-0205
title: "Test reviewer persona: do the tests cover the spec?"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, review, personas]
source_idea: IDEA-029
depends_on: [SPEC-0201]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-029`).

[SPEC-0187](./0187-conformance-review-reviewing-state-with-a-read.md) put one independent
reviewer between `GATING` and `CLOSING`: does the change do what the spec says? It does not ask
whether the **tests** would catch a regression. A suite can be green while an acceptance
criterion has no test, while the only test is a happy path, or while a test asserts nothing. The
conformance reviewer flags tautological tests it happens to see; it is not tasked with mapping
every AC to tests nor with probing edge and failure cases. This spec adds that second lens as the
read-only `test_reviewer` persona, the second member of the fan-out that
[SPEC-0201](./0201-reviewer-fan-out.md) provides (the `reviewers` list, parallel runs, the
interim combiner, retries; none of which is restated here).

Split out of SPEC-0201/0203 on 2026-10-25 after spec review.

## Goals / Non-goals

**Goals**
- G1. Persona `test_reviewer` judging whether the tests added or changed cover each AC and the
  meaningful edge and failure cases of the change — not merely that they pass.
- G2. A deterministic `tests_touched` input (changed and removed test names) so the reviewer can
  apply the "deleted or weakened test" rule.
- G3. Enabled by default in `balanced` and `thorough` via the `reviewers` list; opt-in for
  `frugal`.

**Non-goals**
- The fan-out mechanics, cost-cap summing, combination and retry rules (SPEC-0201).
- Running or writing tests: the reviewer reads, it does not execute the suite or edit files.
- Mutation testing; changing the gates or the close step.

## Decision

Add the persona like the conformance reviewer: template, schema, `PERSONAS` entry, profile
settings, entry in `REVIEWER_PERSONAS` (SPEC-0201) and in the default `reviewers` of `balanced`
and `thorough`. Its verdict has the conformance reviewer's shape with `coverage_assessment`
instead of `ac_assessment`. Choices that were open: the reviewer may read any file but must not
run the suite (the gates already do); `tests_touched` is computed by code, not by the model, so
a removed test cannot go unnoticed; parametrized tests are not expanded (one name).

## Design

- File `src/wt/agentic/personas/test_reviewer.md`, registered in `PERSONAS` with `read_only=True`,
  schema `TEST_REVIEWER_SCHEMA` (in `personas.py`, beside `CONFORMANCE_REVIEWER_SCHEMA`; `schemas.py` holds only the ledger schemas and is unchanged); `permission_mode: dontAsk`, `--restricted`,
  read-only tools. `"test_reviewer"` is appended to `REVIEWER_PERSONAS` (`personas.py`) so
  `build_context` gives it the review context (diff, gate summary, previous verdicts).
- **Context plumbing:** `build_request` passes `review=persona in REVIEWER_PERSONAS` (SPEC-0201) to
  `build_context`; it also passes a separate new keyword `with_tests=(persona == "test_reviewer")`, and only then does
  `build_context` call `tests_touched`. `tests_touched` is added to `PLACEHOLDERS`
  (`personas.py`), pre-rendered by `build_context` into a string with `json.dumps(entries, indent=2, sort_keys=True)`, or `(none)` for an empty list or a `GitError` (so `_as_text` passes it through), and the existing `test_files`
  placeholder stays as it is.
- **Inputs (context)** — `spec_text`, `diff` (≤ 1500 lines, `git diff <base>...HEAD`),
  `tests_touched` (below), `gate_results` summary, `previous_verdicts`, `pending_brief`. The
  reviewer may read any file in the worktree (existing tests, fixtures); it must not run the suite
  or edit anything. `build_context` calls `tests_touched` only for this persona.
- **`tests_touched` extraction** (new `personas.tests_touched(worktree, base) -> list[dict]`,
  deterministic, no LLM). A file is a *test file* when it ends in `.py` and its path is under
  `tests/` or matches `test_*.py` / `*_test.py` (JSON fixtures, `conftest.py` helpers and other
  non-test files are skipped: an edit to a fixture marks no test `changed`, accepted, because the
  reviewer reads the diff). Two git calls: `git -c core.quotepath=off diff -M --name-status -z <base>...HEAD` for paths
  and statuses (`R087` etc. normalised to `R`; `T` is read as `M`), and **one** full `git diff -M
  --unified=0 <base>...HEAD` whose output is split per file on the `diff --git` headers (a
  per-path diff would hide the old path and report a renamed file as wholly new), giving hunk
  ranges per new path from the `+++ b/<path>` lines (run with `-c core.quotepath=off`; a path git
  still quotes is treated as having no hunk info, so every test of that file is `changed`). `git.GitError` degrades to an empty list, as `diff` does:
  - for each added/modified/renamed test file (new path), parse its `git show HEAD:<path>`
    version (not the worktree, which may be dirty and would disagree with the diff) with `ast`; a
    test function is any `def`/`async def` named `test_*` at module level or inside a class
    (reported as `Class::name`, nested classes dotted `Outer.Inner::name`; parametrization is not expanded — one name). A test's *span* runs from the smallest line among its decorators and the `def` to
    `end_lineno` (so editing an `@skip` or `parametrize` decorator counts). A test is `changed`
    when its span intersects the new-side range of a `--unified=0` hunk; a deletion-only
    hunk (new-side count 0 at line `s`) touches a test whose span satisfies
    `start − 1 ≤ s ≤ end`, so a removed assertion marks its test `changed` (a deletion just
    before or just after a test may also mark it `changed`: a harmless false positive, since the
    reviewer reads the diff). If `ast.parse`
    fails on the new version, fall back to the regex `^\s*(?:async\s+)?def\s+(test_\w+)` and treat every match
    inside a hunk range as changed; the same regex yields the names of an unparsable base version.
  - `removed`: test names parsed from the base version (`git show $(git merge-base <base> HEAD):<oldpath>`, matching the three-dot diff even if `base` is a moved branch tip) that are
    absent from the new version (or all of them for a deleted file). An entry exists when either the
    old or the new path is a test file: a rename from a test path to a non-test path lists all
    old names as `removed`; the reverse lists the new file's tests as `changed`. Renamed tests therefore
    appear as one removed and one changed name, which the reviewer reconciles from the diff.
  - Result entry: `{path, status: A|M|R|D, changed: [names], removed: [names]}`, sorted by path,
    names sorted, each list capped at 200 (a trailing `"… N more"` entry when capped). The
    prompt renders the list as sorted JSON so it is deterministic.
- **Method (in the template):** for **every** AC list the test(s) that would fail if the AC were
  broken — name, file, and what is asserted; an AC with no such test is `must_fix`; an AC whose
  only test would still pass with the behavior removed (tautology, over-mocking, asserting on the
  mock, no assertion) is `major`. Then enumerate the change's edge and failure cases (empty
  input, boundaries, error paths, permission/idempotency, concurrency where the diff has any) and
  flag untested material ones as `major` (behavior-bearing) or `minor` (cosmetic). Tests
  unrelated to the spec and flakiness risks (sleep, wall-clock, network) are `minor`.
  Regression: a `removed` test or a visibly weakened assertion without justification in the spec
  is `must_fix`.
- **Verdict schema** `TEST_REVIEWER_SCHEMA`: `{verdict: pass|fix, score: 0..100, findings:
  [{severity: must_fix|major|minor|nit, ac, title, detail, files}], coverage_assessment: [{ac,
  covered: bool, tests: [str]}], fix_brief}`; parity with `CONFORMANCE_REVIEWER_SCHEMA`: the required top-level
  keys are exactly `verdict`, `score`, `findings`, `coverage_assessment`, `fix_brief`; each
  finding requires `severity` and `title`; each `coverage_assessment` entry requires `ac` and
  `covered`.
  `ac` is the AC text or id the finding concerns, or the code path (`file:function`) for an edge
  case outside any AC. `verdict: pass` with a `must_fix` finding is read as `fix` (as SPEC-0187).
  Findings become `finding` ledger events carrying `ac`, like the conformance reviewer's (the
  `findings` loop in `_record_run` already handles the shape);
  `coverage_assessment` is kept in `run.json` and in `last_review.verdicts[test_reviewer]` only.
- **Profiles (`profiles.py`):** `personas.test_reviewer` — `balanced`/`thorough`: model `opus`,
  effort `high`, `max_budget_usd` 1.0; `frugal`: model `sonnet`, effort `medium`, 0.5 (present so
  a user can opt in via `reviewers`; not listed by default). `read_only: true`,
  `permission_mode: dontAsk`, same timeout as the conformance reviewer. Default `reviewers`
  for `balanced` and `thorough` gains `test_reviewer`; `frugal` stays `[conformance_reviewer]`.
  **Order ownership:** the canonical order is the tuple `REVIEWER_ORDER =
  ("conformance_reviewer", "test_reviewer", "adversarial_reviewer")` in `profiles.py`, defined
  jointly with SPEC-0202 (whichever ships first creates it; the other only adds its name); each
  of SPEC-0205/SPEC-0202 inserts its persona at its canonical position, so the result does not depend on which ships first (`[conformance_reviewer,
  test_reviewer]` without SPEC-0202). A user delta replaces the list wholesale (`deep_merge`).
- **Cost impact:** at defaults `balanced` adds 1.0 per review pass (conformance 1.5 + test 1.0 =
  2.5 against the 10.0 `campaign_cap_usd`, beside the 4.0 implementer budget). The SPEC-0201
  cap check compares *spent* money plus pending budgets: the first review fits (implementer
  actual spend ≤ 4.0, 4.0 + 2.5 < 10.0). After one fix loop spent money can reach about
  4.0 + 2.5 + 4.0, so a second review parks `cost_cap` once spent money exceeds `10.0 − 2.5 = 7.5` (it was
  `10.0 − 1.5 = 8.5` with one reviewer);
  `campaign_cap_usd` defaults are deliberately not changed, and users who run many loops are
  expected to raise the cap. Opting `test_reviewer` into `frugal` (frugal conformance 0.6 +
  0.5 = 1.1 per pass against the 3.0 cap) is tight and is the user's choice.
- **schemas.py / parking:** no new ledger event types and no new park reasons. `finding` events
  from this persona set the optional `ac` key like SPEC-0187's. `review_escalated`,
  `cost_cap`, `loop_exhausted` apply as in SPEC-0201.
- **Docs:** `docs/product/capabilities/agentic.md` "Reviews" lists the test reviewer.

## Alternatives considered

- **Let the test reviewer run the suite or mutate code** — rejected; the gates already run the
  suite and mutation testing is costly and out of scope.
- **Let the model find the changed tests from the diff** — a removed test is easy to miss in a
  1500-line diff; a deterministic list is cheap and makes the regression rule enforceable.
- **Fold the AC-to-test mapping into the conformance reviewer** — that reviewer is anchored on
  behavior; two contexts judging different things keep each sharper.

## Acceptance criteria

- [x] AC1 (G1). `test_reviewer` template (containing the regression rule: removed or weakened
      test ⇒ `must_fix`, and the severity mapping), `TEST_REVIEWER_SCHEMA` (exact required keys
      as above),
      `PERSONAS` entry and `REVIEWER_PERSONAS` membership exist; `build_request` is read-only and
      its prompt is deterministic (two builds equal) and contains the `tests_touched` entries
      (changed and removed names).
- [x] AC2 (G3). `resolve_profile` gives `personas.test_reviewer` the model/effort/budget above
      for every builtin profile and `reviewers` containing `test_reviewer` immediately after
      `conformance_reviewer` (asserted as `reviewers.index("test_reviewer") == reviewers.index("conformance_reviewer") + 1`) for `balanced` and `thorough` (exactly `["conformance_reviewer",
      "test_reviewer"]` when SPEC-0202 has not shipped) and `["conformance_reviewer"]` for
      `frugal`; a user delta can add it
      to `frugal`.
- [x] AC3 (G2). `tests_touched` returns, for a fixture repo, the changed test names (incl.
      `Class::name`), the removed test names (deleted function, deleted file, renamed file; an
      R100 rename lists nothing as changed), a test whose only edit is a deleted assertion as `changed`, normalises `R087` to `R`, caps
      long lists, and falls back to the regex on a syntax error.
- [x] AC4 (G1). Its findings become `finding` events with `persona: test_reviewer` and `ac`;
      a `verdict: pass` with a `must_fix` finding routes as `fix`; `ledger_problems` is empty for them and `coverage_assessment` is stored in
      `last_review.verdicts`.
- [x] AC5 (G1, G3). End to end with FakeRunner scripts on the default balanced reviewers:
      (a) conformance pass + test_reviewer fix then pass ⇒ `MERGED` with two implementer runs and
      the test findings in the ledger; (b) both pass ⇒ `MERGED`; (c) test_reviewer malformed
      twice ⇒ `PARKED(review_escalated)` naming it; (d) the cost-cap sum includes its budget (a
      cap that fits conformance alone but not both parks `cost_cap` before any spawn).
- [x] AC6. Existing queue/e2e tests that assumed the default `balanced` has one reviewer set
      `reviewers: [conformance_reviewer]` in their profile delta; `docs/product/capabilities/
      agentic.md` lists the reviewer (grep assertion in `tests/test_agentic_profiles.py`); full `uv run pytest` green; `python3 tools/spec_lint.py`
      exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_personas.py` — AC1/AC3: template, schema, request
  read-only, deterministic prompt, `tests_touched` fixtures in a git repo under `tmp_path`
  (added/modified/deleted/renamed files, `Class::name`, deleted assertion, syntax error, cap);
  `tests/test_agentic_profiles.py` — AC2 exact settings and default lists, frugal opt-in;
  `tests/test_agentic_logs.py` — AC4 ledger validity; `tests/test_agentic_e2e.py` — AC5 paths
  a-d, with the test_reviewer scripted to finish first and last.
  The cap marker is pinned as the literal string `"… N more"` in the AC3 test.
- **AC6 check:** the files expected to change are `tests/test_agentic_queue.py` and
  `tests/test_agentic_e2e.py` helpers (profile delta), plus `docs/product/capabilities/agentic.md`;
  `tests/test_agentic_profiles.py` gets a docs grep for `test_reviewer`.
- **Manual verification:** the next real campaign with the balanced profile records both
  reviewers' verdicts and the actual spend per review pass here (to judge the cap impact).
- **Regression guard:** full `uv run pytest`.

### Test record (2026-10-25)

- `uv run pytest -q` (full suite) green; new tests in `tests/test_agentic_personas.py`
  (template, schema, request, deterministic prompt, `tests_touched` fixtures: nested classes,
  decorator edit, deleted assertion, deleted/added/renamed incl. R100 and R087, rename across the
  test boundary, syntax-error regex fallback, `"… 50 more"` cap, GitError ⇒ `[]`),
  `tests/test_agentic_profiles.py` (settings, default lists, `REVIEWER_ORDER`, frugal opt-in,
  docs grep), `tests/test_agentic_logs.py` (AC4: ledger valid, `pass`+`must_fix` routes as `fix`,
  `coverage_assessment` in `last_review` and `run.json`), `tests/test_agentic_e2e.py` (AC5 a-d,
  with the test reviewer finishing first and last).
- `python3 tools/spec_lint.py --write-ledger` then `python3 tools/spec_lint.py` exit 0.
- Manual verification (both reviewers' verdicts and actual spend per review pass on a real
  balanced campaign) is deferred to the next real campaign, as the plan words it.

### As shipped

- `REVIEWER_ORDER` was created here in `profiles.py` (SPEC-0202 had not shipped); the builtin
  `reviewers` lists remain literal lists, and a test pins them to that order.
- AC6: only the `tests/test_agentic_e2e.py` `make_cfg` helper needed the
  `reviewers: [conformance_reviewer]` default (the other test files build on it);
  `tests/test_agentic_queue.py` uses profile dicts without a `reviewers` key, which already
  fall back to the conformance reviewer, so it is unchanged.
- `queue._store_verdicts` keeps `coverage_assessment` beside `ac_assessment` in
  `last_review.verdicts`; the regex fallback marks a test `changed` only when a hunk touches its
  `def` line, as specified.

## Rollout / migration

Existing `balanced`/`thorough` campaigns gain the second reviewer on their next `REVIEWING`
(about $1.0 more per loop); users who want the old cost set `reviewers` in their profile delta.
Restart the running loop to load the new code.

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
