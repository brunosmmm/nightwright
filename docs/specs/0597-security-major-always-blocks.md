---
id: SPEC-0597
title: A security major from any reviewer always blocks
status: done
owner: user
created: 2026-11-12
updated: 2026-11-12
parent: SPEC-0552
depends_on: [SPEC-0468, SPEC-0555, SPEC-0596]
tags: [agentic, review, safety]
---

## Context

[SPEC-0596](./0596-delta-rule-never-demotes-major-or-must-fix.md) stopped the SPEC-0554 delta
rule from demoting a `major`. Its replay (As shipped, "Finding the replay surfaced") showed the
SPEC-0529 MAC reflection oracle would still pass the campaign. The adversarial reviewer at
effort `high` raised "Signed 401 doubles as a valid request MAC (MAC reflection)" as **major**,
category `security`, but:

- its own verdict was `pass` with score 85 against the pass score 80, so no `fix` verdict
  backed the major and the score cleared the bar; and
- with conformance passing, [SPEC-0468](./0468-stop-queue-thrash.md)'s relief turns an
  adversarial-only major into an advisory follow-up.

**Human decision, 2026-11-12:** any `major` finding in a security category, from any reviewer,
always blocks. Neither the reviewer's `pass` verdict, nor a score at or above the pass score,
nor SPEC-0468's adversarial-major relief lets it through; the collator
([SPEC-0555](./0555-findings-collator-and-auto-follow-up-ideas.md)) cannot scope it out; the
delta rule (SPEC-0596) keeps it. SPEC-0468's relief stays for non-security adversarial majors;
`must_fix` behaves as today.

This spec **amends** SPEC-0468's relief (and adds a guard to SPEC-0555's chain); neither is
superseded.

## Goals / Non-goals

**Goals**
- A `major` (or `must_fix`) finding in a security category forces the review outcome to `fix`,
  from any reviewer, whatever that reviewer's verdict and the campaign score.
- No later step can remove it: not the 0468 relief, a collator `out` scope (live or stored),
  nor a delta-rule demotion (live or stored). Only an explicit human waiver
  (`nw campaign waive`, SPEC-0235) does.
- The conformance and test reviewers can mark a finding as security.
- It applies to every campaign, lean flags on or off.

**Non-goals**
- Reviewer severity calibration (a security issue rated `minor` stays a `minor`).
- Any change to SPEC-0468's relief for non-security majors, to `must_fix`, or to the score.
- A flag. This is a safety rule, not a lean experiment.

## Decision

`profiles.SECURITY_CATEGORIES = (security, injection, path_traversal, secrets, subprocess)`;
`profiles.security_blocks(f)` is true for a `major` / `must_fix` finding whose `category`
(case and surrounding spaces ignored) is one of them. `queue.triage_reviews` marks such a
finding `security_block: true` and:

1. never treats it as delta-demoted (`demoted`) or scoped out (`scope: out`), even when the
   stored verdict says so;
2. keeps the mark through the 1d identical-finding merge;
3. never gives it SPEC-0468's relief;
4. makes the outcome a hard `fix` (`hard = must_fix or fixing or unmet AC or security`), so
   neither the reviewer's `pass` nor the score can pass it. It is listed in `blocking`, and the
   result lists its ids under `security_block` (only when non-empty).

The collator guard chain gains `security` (after `basis_outside_delta`), and `collator.inputs`
keeps a demoted security major.

**Category decisions.**
- `data_loss` is **not** a security category. The human decision names security; data loss is an
  integrity defect. The adversarial prompt already makes a real data loss a `must_fix`, which
  always blocks, so a data-loss `major` is the lesser case. Adding it would widen the rule past
  the decision and bring back some of the churn SPEC-0468's relief removed. It can be added
  later if evidence shows data-loss majors slipping through.
- The conformance and test reviewer schemas had no `category`. Both gain an **optional**
  `category` whose enum is exactly `SECURITY_CATEGORIES`. Their prompts gain one bullet: a
  security finding sets `category` to one of these, any other finding leaves it out, and a
  security `major` / `must_fix` always blocks. A narrow enum asks for one decision ("is this
  security?") without making those reviewers classify everything.
- **Everywhere, not flag-gated.** The rule applies with or without the lean flags, so the way to
  mark a finding must exist in both modes too. The schemas and prompts change for every
  campaign. A conformance or test finding with no category behaves exactly as before.
- Declared (SPEC-0371) reviewers with custom categories are covered when they use one of these
  names.

## Design

- `agentic/profiles.py`: `SECURITY_CATEGORIES`, `SECURITY_BLOCKING_SEVERITIES`,
  `is_security_category`, `security_blocks`.
- `agentic/queue.py::triage_reviews`: steps 1 to 4 above; record and result keys
  `security_block` (present only when set, so results without a security finding have the
  same shape as before). `review_brief`: when a one-reviewer `fix` rests on a security block
  and that reviewer's free-text brief is empty (its verdict was `pass`), the brief is the
  findings list.
- `agentic/collator.py`: `inputs` keeps a `demoted` finding when `security_blocks`; `collate`
  guard `security`.
- `agentic/personas.py`: optional `category` (security enum) on `CONFORMANCE_REVIEWER_SCHEMA`
  and `TEST_REVIEWER_SCHEMA`; `personas/conformance_reviewer.md` and `test_reviewer.md`: the
  instruction bullet and `"category"` in the findings shape.
- `packet.enforce`: unchanged (SPEC-0596 already keeps every `major` / `must_fix`).
- `tools/replay_review_packet.py`: `--triage` prints the campaign-level triage and route of
  the replayed verdicts (`decide_reviews` with the campaign profile). `--assume-pass` adds the
  reviewers that were not replayed as a clean `pass`. `without_categories` gives the same
  verdicts' outcome with the categories dropped, which is the pre-0597 route.

**In-flight campaigns** pick the rule up at their next triage (`_route_reviews`, the review
triager input, escalations): `triage_reviews` reads the stored verdicts. So a stored collation
`out`, or a pre-0596 `demoted` mark, on a security major is ignored from then on. This is
intended. Nothing stored is rewritten.

## Alternatives considered

- **Only drop the 0468 relief for security majors.** The replay case would still pass, because
  the reviewer's own verdict was `pass` and the score was 85, above the pass score 80.
- **Make security majors `must_fix` (rewrite severity).** That changes the stored finding and
  the score weights (100 vs 15), and the collator's severity-revert guard would fight it.
  A separate mark keeps the reviewer's severity.
- **Keyword-match titles for conformance/test findings.** Fragile and invisible to the
  reviewer; an explicit optional category is cheap and auditable.
- **Gate the schema change behind a lean flag.** Then the rule would only half-apply on default
  campaigns, which contradicts the decision that it applies everywhere.

## Acceptance criteria

- [x] AC1: An adversarial `major` with a security category, under that reviewer's `pass`
      verdict and score 85 (pass score 80), with conformance passed, makes `triage_reviews`
      return `fix` with the finding in `blocking` and listed under `security_block`.
      `decide_reviews` routes it to `implement`.
- [x] AC2: A non-security adversarial `major` (including `data_loss`) in the same shape still
      gets SPEC-0468's relief: outcome `pass`, finding advisory.
- [x] AC3: The collator cannot scope out a security major (guard `security`). A stored
      `scope: out` or `demoted` mark on one is ignored by the triage, and `collator.inputs`
      keeps a demoted one.
- [x] AC4: A conformance or test reviewer `major` with a security `category` blocks under a
      `pass` verdict. Their schemas carry the optional security `category` and their prompts
      ask for it.
- [x] AC5: With the lean flags off, and with them on, a FakeRunner campaign whose adversarial
      reviewer passes with a security major goes back to implementing (outcomes `fix`, `pass`),
      and the finding reaches the implementer. With a non-security category it merges after one
      loop as before.
- [x] AC6: Results without a security finding are unchanged (no `security_block` key); a human
      waiver still removes a security major; a security `minor` does not force `fix`.
- [x] AC7: Replay case 1 (SPEC-0529, delta loop 2, adversarial reviewer at effort `high`)
      on this branch gives the campaign-level route `fix` (`implement`), where the same
      verdicts without categories give `pass`.

## Test plan

- **Automated tests:** `tests/test_security_blocks.py`:
  - `test_adversarial_security_major_with_pass_verdict_routes_to_fix` (each category; AC1),
    `test_decide_reviews_sends_the_replay_shape_back_to_implementing` (AC1, AC2);
  - `test_non_security_adversarial_major_keeps_the_0468_relief` (`bug`, `logic`, `data_loss`,
    `concurrency`, none; AC2);
  - `test_collator_cannot_scope_out_a_security_major`,
    `test_collator_inputs_keep_a_demoted_security_major`,
    `test_stored_scope_out_or_delta_demotion_cannot_hide_a_security_major` (AC3);
  - `test_conformance_or_test_reviewer_security_major_blocks`,
    `test_security_mark_survives_a_merge_with_an_unmarked_finding`,
    `test_conformance_and_test_schemas_carry_an_optional_security_category`,
    `test_reviewer_templates_ask_for_the_security_category` (AC4);
  - `test_queue_security_major_under_pass_verdict_forces_a_fix_loop` and
    `test_queue_non_security_major_still_passes_under_0468`, each with `lean` off and on
    (packet + collator, the collator scoping the finding `out`) (AC5);
  - `test_a_human_waiver_still_removes_a_security_major`,
    `test_minor_security_finding_does_not_force_fix_and_must_fix_still_does`,
    `test_single_reviewer_brief_names_the_security_finding` (AC6). Existing triage, 0468
    (`tests/test_thrash_brakes.py`), collator, packet and persona tests pass unchanged (AC6).
- **Manual verification:** `tools/replay_review_packet.py` case 1 (SPEC-0529-20261110-f7b4,
  `--loop 2 --upto-seq 567 --gate-seq 566 --entry-run 75 --prior-runs
  072-conformance_reviewer,073-test_reviewer,074-adversarial_reviewer`, head `79aff687`, prev
  `ee44a190`, base `4bd6f0eb`, `--personas adversarial_reviewer --reviewer-model
  claude-sonnet-5-5 --reviewer-effort high --triage --assume-pass
  conformance_reviewer,test_reviewer`), scratch copies on `/home`, one attempt (AC7).
- **Regression guard:** affected tests (`tools/affected_tests.py`), `spec_lint`, one close gate
  on the box.

## Rollout / migration

No migration and no flag: the rule is live at merge for every campaign. In-flight campaigns get
it at their next triage.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; replay recorded.
- [x] No regressions (close gate).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated.

## As shipped

Shipped 2026-11-12 on branch `lean/security-blocks` (based on the SPEC-0596 head `5b2c94a0`).
The code is as described in Decision and Design. Category decisions: `data_loss` is out; the
conformance and test reviewers get an optional `category` with the security enum, on every
campaign.

**Replay (AC7), 2026-11-12.** Case 1 through this branch's `tools/replay_review_packet.py`
(`--triage --assume-pass conformance_reviewer,test_reviewer`), scratch copies under
`~/.cache/nw-replay/0597-case1`, one attempt. Costs: marshal `claude-haiku-5-5` $0.0101
(27.7 s); adversarial reviewer `claude-sonnet-5-5` effort `high` $0.2197 (16.3 s); total
**$0.230**. The reviewer again raised the oracle as **`major`**, category `security`
("Signed 401 can be replayed as a valid request MAC (token-only holder reads /v1/status)"),
with verdict **`pass`** (score 70). The delta rule kept it (`basis_outside_delta`, `demoted`
0). Campaign-level triage (profile pass score 80, major weight 15) gave score **85**, outcome
**`fix`**, route **`implement` / `review_fix`**, with the finding in `blocking` and under
`security_block`. Dropping the categories from the same verdicts gives **`pass`**, the
pre-0597 route. That is the 0596 replay's outcome: verdict `pass`, 85 >= 80, and 0468 relief
with conformance passing.

**Tests and gate.** `tests/test_security_blocks.py` has 29 tests; with the triage, 0468, collator
and packet-queue tests, 165 pass locally. Persona, registry, packet and profile tests: 468
passed. `spec_lint` exits 0. Close gate **GATEJOB-2783** (commit `c09c999d`) passed:
spec-lint, docs-tests 249 passed, pytest 8426 passed, none failing.

Status stays `accepted` as the brief set it. Moving it to `done` is left to the merge.
