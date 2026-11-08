---
id: SPEC-0468
title: Stop the queue from thrashing a finished campaign
status: accepted
owner: user
created: 2026-11-08
updated: 2026-11-08
tags: [agentic]
depends_on: [SPEC-0203, SPEC-0207, SPEC-0345, SPEC-0420, SPEC-0462]
source_idea: IDEA-1033
---

## Context

Campaigns that are done from the spec's point of view keep spending implement and review
rounds, then park on a verdict that was never a verdict. Across the live Meta-Tools queue
on 2026-11-08: IDEA-1024 had 12 review rounds and 0 unanimous passes, with the adversarial
reviewer returning `fix` on every round; SPEC-0335 passed review at 100 twice and was
resumed into implementing until the score fell to 0; eight campaigns parked together
because Cursor returned `You're out of usage`, which the queue recorded as
`error=error` and treated as two unusable verdicts. A cursor campaign has no dollar
signal (`cost_usd` stays 0), so the cost cap never stops it. The worker that rejected
`NW_AFFECTED_FULL` was an old build; this checkout's allowlist already lists the key
(SPEC-0420).

## Goals / Non-goals

**Goals**

- A Cursor usage-limit failure pauses the host. It does not spend a review or triage retry
  and it does not open a red-base fix-forward.
- An adversarial `major` does not send the campaign back to implementing once conformance
  passed with no must-fix and no unmet acceptance criterion. It is filed as a follow-up.
- A `diverging_rework` park and a close whose only failure is `spec-dod` cannot resume
  into implementing.
- `loop_exhausted` auto-resumes once, only when the must-fix set shrank. The second
  exhaustion stays parked.
- A cursor campaign has a review-round budget that a resume cannot zero. The dollar cap
  stays off.

**Non-goals**

- No numeric dollar cap on a cursor campaign.
- Test-reviewer majors still block.
- An adversarial must-fix still blocks.
- The conductor's abandon path for `diverging_rework` stays available.

## Decision

Six brakes, all in the queue, none of them a new persona:

1. Copy a failed run's `error_message` into the result tail when `result` is empty. Treat
   `out of usage`, `usage limit`, and `feature_unavailable` on an error result as a host
   usage pause of 15 minutes (`usage-pause.json`). While it holds, the persona gate admits
   nothing and `redmain.step` does not open a fix-forward. A review or gate triage whose
   missing verdicts are all usage failures stays in place and does not increment its retry
   counter.
2. The live worker is updated to this checkout's build, which already allowlists
   `NW_AFFECTED_FULL`. No further code change.
3. In `triage_reviews`, when the conformance verdict is `pass`, nothing surviving is a
   must-fix, and no acceptance criterion is unmet, a major whose only reporter is
   `adversarial_reviewer` is removed from the score and from `fixing` and appended to
   `advisory`. The existing follow-up filer records it at close.
4. `diverging_rework` parks with `resume_to: SPEC_WRITING`. `plan_resume` rejects
   `IMPLEMENTING`, `GATING`, `REVIEWING`, and `DIVERGENCE_TRIAGE` for that reason and for
   `round_budget`. A close whose synthetic gate is `spec-dod` parks `spec_status` with
   `resume_to: CLOSING` and never enters `TRIAGING`. Any other resume target is rejected.
5. The must-fix titles of the first fix round in a loop window are the baseline. On
   `loop_exhausted`, if that set is strictly smaller and `loop_auto_resumes` is 0, the
   queue returns to `IMPLEMENTING` with the loop counter cleared and
   `loop_auto_resumes` set to 1. Otherwise it parks. A resume that resets loops clears
   the baseline and does not clear `loop_auto_resumes` or `review_rounds`.
6. When the campaign's runner is `cursor`, each review-driven implement round increments
   `review_rounds`. The cap is `2 * loop_max`. The next review fix at the cap parks
   `round_budget` (`resume_to: SPEC_WRITING`) instead of implementing. Gate-triage fixes
   do not count.

## Design

`persona_gate.usage_until` / `note_usage` sit beside the rate-limit backoff and do not
change it. `is_rate_limit` still excludes account limits. `Gate.admit` holds with
`usage_pause` first. `runner.parse_result` fills `result_text` from `error_message` when
the error result's `result` is empty, so `events._is_transient` can see the new markers.

`queue.triage_reviews` performs the demotion after waiver and before the score. Demoted
findings are advisory on both `pass` and `fix`, so a later close files them and the
implementer is not briefed to chase them.

`queue._route_reviews` applies the round budget before the one automatic loop resume, and
the automatic resume before the `loop_exhausted` park. The triage path applies the same
automatic resume, using the triager verdict's `must_fix` list, and does not increment
`review_rounds`.

## Alternatives considered

- **A dollar cap estimated from run duration.** Cursor still reports no price. A fake
  number would park campaigns for the wrong reason.
- **Demote every non-conformance major.** The test reviewer is the one that kept a real
  hole open on IDEA-1015. Only the adversarial major is demoted.
- **Auto-resume `loop_exhausted` whenever a fix was committed.** That is the resume the
  conductor has been doing by hand, and it is how SPEC-0335 went from 100 to 0.

## Acceptance criteria

- [ ] An error result with an empty `result` and `error_message` "You're out of usage"
      stores that sentence as the run's result text, and the text is transient.
- [ ] While `usage-pause.json` is in the future, the persona gate admits no campaign step
      and `redmain.step` returns `none` instead of `open_fixforward`.
- [ ] A review or gate triage whose failed runs are usage exhaustion does not increment
      `review_retries` or `triage_retries`.
- [ ] Conformance `pass`, no must-fix, no unmet AC, and an adversarial major yields
      outcome `pass` (or `fix` only because some other reviewer still has a major) and
      the adversarial major is advisory.
- [ ] An adversarial must-fix, or a conformance verdict that is not `pass`, still blocks.
- [ ] `diverging_rework` parks with `resume_to` `SPEC_WRITING`. Resuming it to
      `IMPLEMENTING` raises.
- [ ] A close outcome whose only synthetic gate is `spec-dod` parks `spec_status` with
      `resume_to` `CLOSING`. Resuming it anywhere else raises.
- [ ] The first `loop_exhausted` whose must-fix title set is smaller than the baseline
      returns to `IMPLEMENTING` and sets `loop_auto_resumes` to 1. A second exhaustion
      parks. A flat or larger set parks the first time.
- [ ] A cursor campaign at `review_rounds == 2 * loop_max` parks `round_budget` on the
      next review fix. `reset_loops` does not clear `review_rounds` or `loop_auto_resumes`.
- [ ] `docs/product/loop/operate.md` names `spec_status` and `round_budget`.

## Test plan

- **Automated tests:** `tests/test_thrash_brakes.py` covers the result tail, the usage
  pause, the adversarial demotion, the resume guards, the loop auto-resume predicate,
  and the cursor round cap. `tests/test_agentic_queue.py::test_apply_close_outcomes`
  expects `spec_status`. `tests/test_agentic_divergence.py::test_e2e_gate_repeat_rework_parks`
  expects `resume_to` `SPEC_WRITING`. `tests/test_docs_two_pillars.py` keeps the park
  table complete.
- **Manual verification:** `nw workers check box` reports the build that contains
  `NW_AFFECTED_FULL` in `ENV_ALLOWLIST`. After the runner reloads this code, a usage
  failure writes `usage-pause.json` and holds the persona gate instead of parking
  `review_escalated`.
- **Regression guard:** existing review tests still require a test-reviewer or
  conformance major to block. The rate-limit backoff tests are unchanged.

## Clock Log

CLOCK-IN: [2026-11-08 Sun 10:31] via `nw idea clock-in IDEA-1033`

## Rollout / migration

Deploy order: the worker update already ran (`nw workers update box`, build
`b1:4777e63f1`). The queue code takes effect when this commit is what the running
`nw` imports. Campaigns already parked `review_escalated` on the usage message stay
parked until a resume; the pause stops the next occurrence. `review_rounds` and
`loop_auto_resumes` start at 0 on old state.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

None.
