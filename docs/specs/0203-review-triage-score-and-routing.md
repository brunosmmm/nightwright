---
id: SPEC-0203
title: "Review triage: deterministic score and routing of reviewer verdicts"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, review, triage, queue]
source_idea: IDEA-031
depends_on: [SPEC-0201]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-031`).

[SPEC-0201](./0201-reviewer-fan-out.md) runs several reviewers in parallel and combines their
verdicts with an interim rule: any `fix` or `must_fix` ⇒ fix. That is safe but blunt: a
reviewer's `fix` verdict over a handful of `minor`/`nit` observations on a sound change burns an
implementer loop (and a fresh budget). This spec replaces the interim combiner with a
**deterministic scorer and router**. Two optional layers build on its interface and are separate
specs: [SPEC-0206](./0206-review-triager-persona.md) (an LLM that may waive low-severity
findings) and [SPEC-0207](./0207-automatic-review-followups.md) (filing non-blocking findings as
follow-up ideas).

Split out of SPEC-0201/0203 on 2026-10-25 after spec review (docs/specs/reviews/SPEC-0203.json:
the original SPEC-0203 held the scorer, the triager persona and the filer). Earlier revision
notes are in that review file.

## Goals / Non-goals

**Goals**
- G1. Pure `triage_reviews`: a reviewer verdict blocks only when backed by a `must_fix` or
  `major` finding (or is an unexplained `fix`); everything else is judged by a weighted score
  against a per-profile threshold.
- G2. Routing: `pass` ⇒ `CLOSING`; `fix` ⇒ `IMPLEMENTING` with one merged, de-duplicated
  `fix_brief` (`loops += 1`).
- G3. Expose the score and the non-blocking (`advisory`) findings in `last_review`, the ledger
  and the queue CLI, with an interface (`waived` input, `score_only`, `waivable_weight`) that
  SPEC-0206 and SPEC-0207 consume.

**Non-goals**
- Any LLM in the decision, waivers (SPEC-0206), and filing or carrying findings over (SPEC-0207).
  `waived` is always empty in this spec's queue code; `triage_reviews` accepts it so the pure
  function is complete.
- Changing the reviewers or the fan-out (SPEC-0201/0205/0202), or the merge itself.

## Decision

Replace SPEC-0201's `combine_reviews` with a pure scorer `triage_reviews`; `_apply_reviews`
(SPEC-0201) calls it instead and `combine_reviews` is deleted (its parametrized tests are
replaced by `triage_reviews` tests; the SPEC-0201 e2e scripts keep their outcomes because their `fix` verdicts carry a
`must_fix`/`major` finding or no finding at all, and the others pass). Only deterministic code decides.

**What governs: verdict or score?** Both, in a fixed order. (1) Any unwaived `must_fix` ⇒ `fix`.
(2) A reviewer `fix` verdict blocks (⇒ `fix`) when it is backed by at least one `major` finding
of its own, or is unexplained (it has no findings: then it counts as one synthetic, unwaivable
`major`, id `<persona>#fix`, titled from the first 80 characters of its `fix_brief`, or `<persona>
returned fix without findings` when that is empty). (3) Otherwise the score decides.
Consequently a `fix` verdict whose findings are all `minor`/`nit` does **not** block by itself: its
findings only lower the score. That is the one real relaxation versus SPEC-0201's rule and is
exactly the case the Context describes. A `pass` verdict that carries a real `major` is *not*
blocked by that alone: reviewers set the verdict and a `pass` is respected, so the `major` costs
15 points and is reported as advisory (an LLM saying `pass` over a material defect is the
reviewer's call; the `must_fix` severity and an unmet AC are the hard stops, and this
is tested). A `major` backs a `fix` deliberately: a reviewer that names a concrete material defect
must be heard.

## Design

### Deterministic combiner

`triage_reviews(verdicts, profile, waived=()) -> {outcome, score, score_only, blocking, advisory,
waivable_weight}` — pure, in `queue.py`. `verdicts` is the ordered `{persona: verdict_dict}` of
valid verdicts, rebuilt on every call from `state.last_review.verdicts[persona]`, which SPEC-0201
persists with each reviewer's full `findings` list and `fix_brief` (so later ticks, a retry pass
and SPEC-0206's triager step do not need the ledger; the `finding` events keep only an audit copy).
Reviewers' own `score` values (kept in `verdicts[*].score`) are **not** inputs to triage.
Steps, in this order:

1. **Normalize**, in these sub-steps, in order:
   - 1a. Each real finding gets id `<persona>#<index>` (index in the verdict's list). A `pass`
     verdict containing a `must_fix` is treated as `fix`.
   - 1b. A `fix` verdict with **no findings** adds the synthetic `major` above (id
     `<persona>#fix`).
   - 1c. Every conformance `ac_assessment` entry with `met: false` is an *unmet AC*, a hard
     stop (step 5) whatever the verdict says. Its blocking representative is the reviewer's own
     finding that *covers* it with severity `major`/`must_fix` when there is one; otherwise a
     synthetic, unwaivable `major` (id `<persona>#ac<i>`, titled with the AC text) is added as a
     finding of that reviewer. A finding *covers* an entry when its `ac` is non-empty and, after
     `norm` (lower-case, whitespace collapsed), equal to the entry's `ac` or one contains the
     other with length ≥ 8 (tolerating a shortened or reworded criterion); a finding with a
     missing or empty `ac` covers nothing. A near-miss matching neither way therefore adds a
     synthetic `major` next to the reviewer's real finding (accepted: the cost of a wrong guess
     is a duplicate bullet; the cost of a missed unmet AC is a bad merge). The synthetic is a
     finding of its reviewer, so it keeps that reviewer's own `fix` verdict alive (step 3) and the
     reviewer's `fix_brief` enters the merged brief. 1b and 1c are independent: a `fix` verdict
     with no findings and an unmet AC gets both synthetics (30 points).
   - 1d. Findings (also two findings of the **same** reviewer; findings with an empty `files`
     list are never merged) identical by `(sorted files, norm_finding_title(title))`, where
     `norm_finding_title` is a module function in `queue.py` (lower-case, whitespace collapsed)
     that SPEC-0207's follow-up key reuses, across reviewers are **de-duplicated before
     scoring**: one merged finding kept, with the highest severity among the copies, listing all
     reporting personas — a duplicate issue is counted once (intended; two reviewers agreeing is
     not twice the defect). Synthetic findings are not merged. Its **canonical id** is that of
     the copy with the highest severity, ties broken by the earliest persona in profile order. A
     merged finding *belongs to every persona that reported it*: it counts toward each reporter's
     verdict in step 3. Waivers (step 2, SPEC-0206) reference canonical ids only; waiving a
     merged finding removes it for all reporters.
2. **Waive.** Remove findings whose id is in `waived`, only if their severity is `minor` or `nit`
   (a `must_fix`/`major`/synthetic id in `waived` is ignored). Waiving happens before step 3,
   and synthetic findings are never waivable (the `<persona>#fix` synthetic only exists for a verdict
   with no real findings, so it cannot coexist with a waivable one; an AC synthetic may coexist
   with waivable `minor`/`nit` findings and stays).
3. **Block by verdict.** A reviewer's `fix` verdict *survives* iff it is unexplained (synthetic
   `major`) or at least one of its surviving findings is `major` or `must_fix`.
4. **Score.** `score = max(0, 100 − Σ weight[severity])` over all surviving findings, default
   weights `{must_fix: 100, major: 15, minor: 4, nit: 1}`.
5. **Outcome.** `fix` iff any surviving `must_fix`, or any surviving `fix` verdict (step 3), or
   any unmet AC (step 1c: it blocks whatever the reviewer's verdict and the score say, like a
   `must_fix`), or `score < profile.review.pass_score` (defaults: `balanced` 80, `thorough` 90,
   `frugal` 70); else `pass`. `score_only` = outcome is `fix` and none of the first three causes
   (a surviving `must_fix`, a surviving `fix` verdict, an unmet AC) holds.
   `waivable_weight` = Σ weights of surviving `minor`/`nit` findings.
6. `blocking` = if outcome `fix` for a non-score reason: the surviving `must_fix` and `major`
   findings (including synthetic ones); if `score_only`: all surviving findings, highest weight first; `[]` for `pass`.
   `advisory` = surviving findings not in `blocking`.

Worked examples (balanced, `pass_score` 80):
- conformance `fix` with two `minor`, test reviewer `pass` with one `nit`: no verdict survives
  (no `major`), score `100−4−4−1 = 91` ⇒ `pass`; three advisory findings.
- reviewer `fix` with one `major` and one `minor`: the verdict survives ⇒ `fix`,
  `blocking = [the major]`, score `81` is irrelevant.
- reviewer `fix` with no findings ⇒ synthetic `major` ⇒ `fix`.
- adversarial `pass` with four `minor`, test `pass` with three `minor`: no verdict survives,
  score `100−28 = 72` ⇒ `fix`, `score_only`, `waivable_weight 28`. SPEC-0206's triager may waive three
  `minor` (12) ⇒ score `84` ⇒ `pass`.
- any `must_fix` ⇒ `fix` whatever the score.
- conformance `pass` with one real `major`: no verdict survives, score `85` ⇒ `pass`, the `major`
  is advisory (SPEC-0207 files it) and a `note` event records the override.
- A `fix` with one `minor` that a second reviewer reported as `major` (same files and title): one
  merged finding (`major`, canonical id the second reviewer's) belongs to both; the first
  reviewer's verdict survives through it (and is therefore not "unexplained"), outcome `fix`.
- conformance `pass` with `ac_assessment` `[{met: false}]` and only a `minor` finding on that
  AC: a synthetic AC `major` is added; score `100−15−4 = 81 ≥ 80`, yet the outcome is `fix`
  (the unmet AC blocks independent of score and verdict), `blocking = [synthetic]`.
- conformance `pass` with `[{met: false}]` and a covering real `major` on that AC: no synthetic
  is added, the outcome is still `fix` (unmet AC), `blocking = [the major]` (so a stronger
  finding never weakens the outcome versus a weaker one).
- conformance `fix` with two `minor` findings and an unmet AC with no covering `major`: the
  synthetic keeps this `fix` verdict alive; its `fix_brief` and the synthetic are in the brief.

Profile keys under `review:` (this spec): `pass_score` (int 0..100, not a bool) and `weights`
(after the delta merge a map with **exactly** the keys `must_fix`, `major`, `minor`, `nit`, each
an int ≥ 0, not a bool; a partial user delta merges over the defaults). `resolve_profile`
validates them (`ValueError` naming the key) and **rejects any other key under `review:`**;
SPEC-0206 and SPEC-0207 each extend the allowed set with their own keys.

### Routing, state and reporting

- `_apply_reviews` (SPEC-0201), once every reviewer has a valid verdict, calls
  `triage_reviews(verdicts, profile)`: `pass` ⇒ `CLOSING` (`review_pass`); `fix` ⇒ `loops += 1` →
  `IMPLEMENTING` (`review_fix`); beyond `loop_max` ⇒ `PARKED(loop_exhausted)` (detail carries the
  brief); a reviewer missing after its retry ⇒ `PARKED(review_escalated)` (SPEC-0201). No new
  park reasons. Counter resets and `human_notes` preservation are as in SPEC-0201.
- **The brief** (`pending_brief`, replacing SPEC-0201's combiner output) with two or more
  reviewers is `{source: "review", personas, fix_brief, must_fix, human_notes}`:
  - `personas` = reviewers whose `fix` verdict survived (step 3) plus reporters of a blocking
    finding;
  - `fix_brief` = for each reviewer whose `fix` verdict survived, its own `fix_brief` prefixed
    `[<persona>] ` (blank-line separated), then, if `score_only`, the line `Review score <n> is
    below the pass score <m>; address the highest-weight findings first.`, then one bullet per
    `blocking` entry: `- [<severity>] <title> (<files>): <detail>`. Prose briefs are not
    de-duplicated (findings already were, in step 1d); a bullet exists once per merged finding;
  - `must_fix` = the `blocking` list in the existing entry shape `{ac, title, detail, files}`
    plus `id`, `severity` and `personas`. In a `score_only` outcome it lists all surviving
    findings (highest weight first), not only must-fix ones.
  With exactly one reviewer the brief keeps SPEC-0187's shape (`source` = that persona, its own
  `fix_brief` verbatim, `must_fix` entries with exactly the keys `{ac, title, detail, files}`, no
  `id`/`severity`/`personas`, one per blocking finding) except that a `score_only` or
  synthetic-`major` outcome with an empty reviewer brief gets the same score line and bullets.
- `state.last_review` after this spec is `{loop, entry_run, run, runs, verdicts, verdict, score,
  findings, must_fix, waived, advisory}` where: `verdict` = the triage outcome (`pass|fix`; no
  separate `outcome` key); `score` = the weighted triage score (**redefined**: SPEC-0201 had the
  minimum of the reviewers' own scores; per-reviewer scores remain in `verdicts[*].score`);
  `findings` = count of surviving findings; `must_fix` = count of surviving `must_fix` ones;
  `waived` = `[]` here; `advisory` = list of `{id, personas, severity, title, detail, files, ac,
  category}` for the current review only (replaced on each review; stored for SPEC-0207).
- **Reporting:** whenever `triage_reviews` ran (every `pass`, `fix` and `loop_exhausted` park; not
  for `review_escalated` or a retry pass, where no verdict set is complete) `_apply_reviews`
  appends a `note` event with the exact text `review score <n>/<pass_score> <verdict>`, e.g.
  `review score 72/80 fix` (transition reasons are unchanged), and, only when the outcome is `pass`, one `note` `pass over major: <id> <title>` for each
  real `major` that ended up `advisory` although its reviewer's own verdict was `pass`.
  `wt campaign show` (`cli_campaign.py`) prints a `review:` line from `last_review` (score,
  verdict, advisory count and how many of those are `major`). `queue_payload` rows (`wt queue
  list --json`, `wt.queue.v1`) gain an additive `review_score` key (null when none; no schema
  version bump).

### schemas.py, parking and profiles

- `schemas.py`: no change (the `note` event type exists). No new park reasons.
- `profiles.py`: `BUILTIN_PROFILES[*]["review"] = {pass_score, weights}`:

| profile | `pass_score` |
|---|---|
| balanced | 80 |
| thorough | 90 |
| frugal | 70 |

  all with the default `weights`. A hand-built profile dict without `review` uses the `balanced`
  values.
- `docs/product/capabilities/agentic.md` documents the `review:` block and the new `last_review`
  keys.

## Alternatives considered

- **Let an LLM decide everything** — non-reproducible and can talk itself out of real defects;
  the deterministic core bounds the damage of any later LLM layer.
- **Keep the interim rule (any `fix` ⇒ fix)** — wastes loops on nit-picks.
- **Use the reviewers' own `score` values** — they are not calibrated across personas; severities
  are the common currency.

## Acceptance criteria

- [ ] AC1 (G1). `triage_reviews` is pure and parametrized-tested on the worked examples above:
      `must_fix` ⇒ `fix` regardless of score; a `fix` verdict with only `minor`/`nit` findings does
      not block (outcome by score); a `fix` verdict with a `major` blocks; a `fix` with no
      findings blocks via the synthetic `major`; a `pass` verdict with a real `major` does not
      block (score 85 ⇒ `pass`, the `major` advisory); weights and `pass_score` per profile;
      identical findings from two reviewers are scored once and a deduplicated `fix` verdict
      survives via the merged finding; a `met: false` AC forces `fix` even for a `pass` verdict at score 81 (balanced),
      with a synthetic `major` when no covering `major`+ finding exists and with that finding
      as the blocking representative when it does;
      the AC cover matching (exact, containment ≥ 8 chars, empty `ac`, near-miss adds the
      synthetic) and a `fix` with no findings plus an unmet AC gets both synthetics;
      `waived` ids never remove `major`/`must_fix`/synthetic findings; `score_only` and
      `waivable_weight` correct.
- [ ] AC2 (G1). `resolve_profile` provides and validates `review:` (`pass_score`, `weights`) for
      every builtin profile (exact defaults above); bad values raise `ValueError` naming the key;
      user deltas merge (partial `weights`); unknown `review:` keys, bool values, missing/extra
      weight keys and out-of-range `pass_score` raise.
- [ ] AC3 (G2). Routing: `pass` ⇒ `CLOSING`; `fix` ⇒ `IMPLEMENTING` with one merged
      brief of the pinned shape (labelled `fix_brief`, score line when `score_only`, bullets,
      `must_fix` entries with `id`/`severity`/`personas`; single reviewer keeps SPEC-0187's
      shape) and `loops += 1`; `loop_max` ⇒ `PARKED(loop_exhausted)`; a missing
      reviewer after retry ⇒ `PARKED(review_escalated)`; `combine_reviews` no longer exists.
- [ ] AC4 (G3). `last_review` has the keys listed above (SPEC-0206 adds `triage` and `triager_run`) with `score` weighted and
      `verdict` the outcome; a `note` event `review score 72/80 fix` is written when
      `triage_reviews` ran (including `loop_exhausted`) and not for `review_escalated`; a
      `pass over major` note is written only when the outcome is `pass`; `wt campaign show` prints the `review:` line
      and `wt queue list --json` carries `review_score`.
- [ ] AC5. End to end with FakeRunner: (a) all reviewers pass with `minor` findings ⇒ `MERGED`,
      advisory recorded; (b) a `fix` over only `minor` findings and a passing score ⇒ `MERGED`
      with no implementer loop; (c) `must_fix` ⇒ `IMPLEMENTING` once, then pass; (d) score below
      `pass_score` with no blocking cause ⇒ `IMPLEMENTING` with a brief listing the findings.
- [ ] AC6. `docs/product/capabilities/agentic.md` documents the block and the new `last_review` keys (grep test for `pass_score`, `advisory` and `entry_run`); full
      `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_review_triage.py` (new: `triage_reviews` worked
  examples, dedup and canonical ids, `waived` handling, brief merge/dedup);
  `tests/test_agentic_profiles.py` (`review:` defaults/validation, AC2; docs grep for
  `pass_score`); `tests/test_agentic_queue.py` (routing, `last_review` shape, the `note` score
  event, `campaign show` / `queue list --json`, the SPEC-0201 `combine_reviews` tests replaced);
  `tests/test_agentic_e2e.py` (paths a-d).
- **Manual verification:** on the next real multi-reviewer campaign confirm the score and
  outcome are recorded here.
- **Regression guard:** full `uv run pytest`.

## Rollout / migration

Takes effect on the next `REVIEWING`; no data migration. A `last_review` without the new keys is
treated as "no verdicts" (the review re-runs, SPEC-0201). Restart the running loop to load the
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
