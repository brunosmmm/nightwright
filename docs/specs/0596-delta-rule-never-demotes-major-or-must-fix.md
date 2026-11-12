---
id: SPEC-0596
title: Delta rule never demotes a major or must_fix finding (basis_outside_delta marker)
status: accepted
owner: user
created: 2026-11-12
parent: SPEC-0552
depends_on: [SPEC-0554]
tags: [agentic, review, safety]
---

## Context

[SPEC-0554](./0554-marshal-review-packet-and-delta-reviews.md) added delta-mode reviews from
loop 2: a new finding must carry a valid basis (`delta_regression` citing a file in the delta,
or `missed_ac` naming a packet AC id); any other new finding is demoted to advisory
(`packet.enforce` marks it `demoted: true`, `queue.triage_reviews` lists it under
`basis_demoted` and never scores it). The rule's purpose was to stop `nit` / `minor` churn on
code the loop did not change.

The regression-set replays of SPEC-0554 AC7 and its follow-ups show the rule demoting the
real SPEC-0529 MAC reflection oracle (a signed 401 body is a valid request MAC for
`GET /v1/status`), a security bug:

- **AC7 replay** (SPEC-0554 As shipped): the test reviewer's MAC-reflection finding was
  demoted to advisory.
- **All-medium replay:** the reviewers rated it `minor` and it was demoted. That is a severity
  problem, not this rule's, and it is out of scope here.
- **Hybrid replay, 2026-11-12** (adversarial reviewer on Sonnet at effort `high`): it raised
  "Signed 401 body is a valid request MAC for GET /v1/status (MAC reflection)" as **`major`**
  with no basis, and `enforce` still demoted it (`demoted=True`, `advisory=True`). This is the
  case this spec fixes.

Keeping majors blocking costs little: the noise the delta rule was built to cut is `nit` and
`minor` findings on unchanged code.

Decided by the steward on 2026-11-12, inside the lean epic the human approved
([SPEC-0552](./0552-leaner-agentic-campaign-loop.md)), as a safety fix from regression
evidence. This spec **amends** SPEC-0554's demotion rule; SPEC-0554 is not superseded (its
packet, delta prompts, prior-finding tracking and export stay as shipped).

## Goals / Non-goals

**Goals**
- A new `major` or `must_fix` finding with an invalid basis is never demoted in a delta-mode
  review; it is scored and can block like any other finding.
- It stays visible as "outside the delta": the stored finding, the triage result and the
  export say so.
- `minor`, `nit`, `info` (and unknown-severity) findings with an invalid basis are demoted as
  before.

**Non-goals**
- Reviewer severity calibration (the all-medium replay's `minor`).
- SPEC-0468's adversarial-major relief (an adversarial-only major does not block once
  conformance passed) and the pass-score rule: unchanged.
- A new flag. The rule applies wherever `agentic.lean.packet` is pinned.

## Decision

In `packet.enforce`, an invalid basis demotes a new finding only when its severity is not
`major` or `must_fix`. A `major` / `must_fix` with an invalid basis keeps no `demoted` mark and
gets `basis_outside_delta: true` instead; it is not counted in `demoted`. Downstream it is an
ordinary finding (scored, backs a `fix` verdict, open and non-advisory in the next packet,
collated under SPEC-0555's normal guards), and the marker is carried to the triage result,
`review_outcome` and the campaign export.

## Design

- `agentic/packet.py`: `BLOCKING_SEVERITIES = ("major", "must_fix")`; `enforce` branches on
  it when `_basis_ok` fails. Its return shape `(findings, demoted)` is unchanged; `demoted`
  counts only real demotions.
- `queue.triage_reviews`: a stored finding with `basis_outside_delta` is scored as usual; its
  record carries `basis_outside_delta: true` into `blocking` / `advisory` (key present only when
  set), and the result gains `basis_outside_delta: [ids]` (present only when non-empty, like
  `basis_demoted`). A human waiver removes it like any finding. Flag-off results are unchanged
  (no stored finding can carry the marker without `enforce`).
- `review_outcome` gains `outside_delta` (count of the result's `basis_outside_delta`) next to
  `demoted`, only with the packet flag pinned.
- `campaign_export.CAMPAIGN_COLUMNS` appends `outside_delta_findings` (int; the last
  `review_outcome.outside_delta` of each loop, summed, the way `demoted_findings` is). Appended
  at the end, so v1 consumers keep their column positions.
- Collator (SPEC-0555): `collator.inputs` already skips only `demoted` findings, so a kept
  major is an input; it now also passes `basis_outside_delta` through to the collator. The
  guards are unchanged and stay consistent: a `must_fix` is never scoped out, nor is a finding
  tied to an AC / the Decision / the Design, citing a changed file or on an unmet AC. A kept
  `major` with none of those ties may still be scoped out by the collator; that is the
  collator's job (it is then filed as a follow-up), not a delta-rule demotion.
- `packet.prior_findings`: unchanged; since the finding has no `demoted` mark it is carried
  into the next loop's packet as `open`, non-advisory, so the reviewer must account for it.
- The review triager sees only waivable `minor` / `nit` findings (SPEC-0206), so the marker
  matters to it through `last_review.advisory` and the triage result, not its prompt.
- `tools/replay_review_packet.py`: prints `basis_outside_delta` per finding and match (and
  carries the `--reviewer-effort` override from the `replay/medium` branch).

**In-flight campaigns.** A campaign with `lean.packet` pinned picks up the new rule at its
next delta review (enforcement runs when verdicts are stored). Findings already stored as
`demoted` stay demoted; nothing is rewritten. This is intended.

## Alternatives considered

- **Drop the delta rule.** Loses the churn reduction on `nit` / `minor` findings that SPEC-0554
  was built for.
- **Keep demoting but file majors as follow-ups.** The finding still never blocks the campaign
  that introduced the bug; the oracle would ship.
- **A new flag.** The fix is a safety correction to a flag that is already on for Meta-Tools;
  gating it would leave the default campaigns exposed.

## Acceptance criteria

- [ ] AC1: In delta mode, a new `major` or `must_fix` finding with an invalid basis is not
      demoted: it carries `basis_outside_delta: true`, no `demoted` mark, and is not counted
      in `enforce`'s `demoted`.
- [ ] AC2: Such a finding is scored and blocks like any other: with a `fix` verdict it is in
      `blocking`, it backs the verdict, and the result lists it under `basis_outside_delta`;
      the next loop's packet carries it as `open` and non-advisory.
- [ ] AC3: A new `minor`, `nit`, `info` (or unknown-severity) finding with an invalid basis is
      still demoted and counted (`demoted`, `basis_demoted`).
- [ ] AC4: `review_outcome` carries `outside_delta` with the flag pinned, and the campaign
      export's `outside_delta_findings` and `demoted_findings` count correctly.
- [ ] AC5: With the packet flag off nothing changes: no `outside_delta` on `review_outcome`,
      `outside_delta_findings` is 0, the triage result shape is unchanged.
- [ ] AC6: The hybrid replay case 1 (SPEC-0529, delta loop 2, adversarial reviewer at effort
      `high`) on this code shows the MAC reflection finding no longer demoted.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_packet.py::test_enforce_basis_rule` (extended): must_fix / major with invalid
    basis kept and marked, minor / nit / info / no-severity demoted, `demoted == 4`, never both
    marks (AC1, AC3).
  - `tests/test_agentic_review_triage.py`: `test_blocking_severity_outside_delta_stays_blocking_and_marked`
    (major and must_fix; blocking, fixing, `basis_outside_delta` / `basis_demoted` lists,
    advisory record unmarked), `test_outside_delta_major_merged_and_human_waived`,
    `test_missed_ac_with_unknown_ac_is_demoted` (now a minor) (AC2, AC3).
  - `tests/test_agentic_packet_queue.py::test_outside_delta_major_stays_blocking` (FakeRunner,
    three loops: the loop-2 conformance major on untouched code forces loop 3, `review_outcome`
    `(fix,0,0),(fix,0,1),(pass,0,0)`, open non-advisory in packet 3, export `(0, 1)`);
    `test_two_loops_packet_per_loop_and_delta_review` (the demoted finding is now a minor;
    `outside_delta` 0); `test_flag_off_no_marshal_no_packet` (no `outside_delta`, export 0)
    (AC2, AC4, AC5).
  - `tests/test_agentic_campaign_export.py`: column list / types / count pinned with the
    appended `outside_delta_findings` (AC4).
- **Manual verification:** `tools/replay_review_packet.py` hybrid case 1 (SPEC-0529-20261110-f7b4,
  `--loop 2 --upto-seq 567 --gate-seq 566 --entry-run 75 --prior-runs
  072-conformance_reviewer,073-test_reviewer,074-adversarial_reviewer`, head `79aff687`, prev
  `ee44a190`, base `4bd6f0eb`, `--personas adversarial_reviewer --reviewer-model
  claude-sonnet-5-5 --reviewer-effort high`), read-only on copies; one attempt (AC6).
- **Regression guard:** affected tests (`tools/affected_tests.py`) and one close gate on the box.

## Rollout / migration

No migration. Live at merge for every campaign with `lean.packet` pinned (Meta-Tools default);
in-flight campaigns get it at their next delta review.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; replay recorded.
- [ ] No regressions (close gate).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated.

## As shipped

Implemented on branch `lean/no-demote-major` (not merged). Code as in Design.

**Replay (AC6), 2026-11-12.** Hybrid case 1 through this branch's
`tools/replay_review_packet.py`, scratch copies on `/home` (two earlier launches with the
scratch under `/tmp` stopped on the user's tmpfs quota before writing anything). Marshal
`claude-haiku-5-5` $0.0061 (23 s); adversarial reviewer `claude-sonnet-5-5` effort `high`
$0.1752 (15.7 s); total **$0.181**. The reviewer raised "Signed 401 doubles as a valid request
MAC for GET /v1/status (MAC reflection)" as **`major`**, no basis: `enforce` now returns
`demoted: 0`, the finding has `basis_outside_delta: true` and no `demoted` mark (the old rule
demoted it). Fed through `triage_reviews` (campaign profile `cursor`, pass score 80, major 15)
it is scored (score 85, listed under `basis_outside_delta`) instead of dropped (old rule: 100,
`basis_demoted`).

**Finding the replay surfaced (not changed here).** In that replay the campaign would still
pass: the adversarial reviewer returned verdict `pass` with the major, so no `fix` verdict
backs it and 85 >= 80; and with conformance passing, SPEC-0468's relief moves an
adversarial-only major to advisory anyway. The delta rule is no longer what hides the oracle;
whether those two rules should yield for security majors is a separate decision for the
human.

**Tests and gate.** See Test plan; results recorded at close.
