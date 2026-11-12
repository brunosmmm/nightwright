---
id: SPEC-0555
title: Findings collator (marshal) and automatic follow-up ideas for out-of-scope findings
status: accepted
owner: user
created: 2026-11-11
parent: SPEC-0552
depends_on: [SPEC-0554]
tags: [agentic, review, followups]
source_idea: IDEA-1297
---

## Context

Phase 3 of [SPEC-0552](./0552-leaner-agentic-campaign-loop.md). After the reviewers,
`queue.triage_reviews` (SPEC-0203) de-duplicates findings by code, scores them and, on a
`fix`, `queue.review_brief` hands **all** surviving findings to the implementer. The
optional `review_triager` (SPEC-0206) only waives `minor`/`nit` findings to lift a score.
Follow-up ideas for advisory and waived findings are filed once, at close, as one grouped
idea per campaign (`followups.py`, SPEC-0207/0239). Nothing decides whether a finding is in
the spec's scope, so fix loops chase findings on code the campaign never touched, and the
steward or human is asked to rule on scope (waivers via `human_overrides_of`, escalations).
Merged campaigns since Nov 5 use a median of 1.5 and a mean of 3.05 fix loops.

## Goals / Non-goals

**Goals**
- One collated, de-duplicated finding list per loop, each finding tied to an AC (or to the
  spec's Decision/Design, or to a changed hunk) and marked in or out of scope.
- Only in-scope findings reach the implementer.
- Out-of-scope findings become follow-up ideas automatically, linked to the campaign,
  during the loop — no steward or human ruling needed.

**Non-goals**
- Severity judgement: the collator carries the reviewer's severity unchanged.
- Replacing `review_triager` (it still waives minor/nit score-only cases) or the scoring
  rules of `triage_reviews`.

## Decision

Add a marshal-tier `findings_collator` persona that runs after the reviewers of a loop. It
reads the reviewers' verdicts and the loop packet (SPEC-0554) and outputs, per finding: a
canonical id, the ids it merged, its tie (`AC<n>`, `decision`, `design`, or `diff:<file>`),
`scope: in|out` with a one-line reason, and the reviewer's severity verbatim. Code applies
hard guards, then scores only in-scope findings, sends only those to the implementer, and
files out-of-scope ones as follow-up ideas at once.

## Design

- **Persona:** `persona_defs/findings_collator.yaml` (role system, read-only, tier marshal,
  template `personas/findings_collator.md`, output schema `findings_collator` in
  `personas.py`). Input via `extra`: the loop's verdicts and `packet-loop-N.json`.
- **Output file:** `<campaign_dir>/findings-loop-N.json` (`nw.collated_findings.v1`):
  `[{id, merged: [ids], persona: [..], severity, tie, scope, reason, title, detail, files}]`.
- **Scope rule (given to the collator, enforced by code):** in scope if the finding is tied
  to an AC, to the spec's Decision/Design, or is a defect in lines this campaign changed
  (diff since base). Out of scope if it concerns code the campaign did not change and no AC
  or Decision asks for it, or asks for behaviour the spec does not ask for.
- **Code guards** (in a new `queue._apply_collation`, called from `queue._route_reviews`
  before `decide_reviews`):
  - a finding whose cited files intersect the campaign diff can never be `out`;
  - a `must_fix` or an unmet-AC synthetic finding can never be `out`;
  - every input finding must appear exactly once (as a canonical id or in a `merged` list);
    a dropped finding is restored as `in` with reason `collator_dropped`;
  - severities are compared with the inputs; a changed severity is reverted.
- **Routing:** `triage_reviews` runs on the in-scope set only; `review_brief` and
  `personas.render_previous_verdicts` / `{{pending_brief}}` carry only in-scope findings,
  each with its AC tie.
- **Follow-ups:** out-of-scope findings are filed by the queue process (persona runs cannot
  write org files, SPEC-0272) through `followups.py` into the campaign's grouped follow-up
  idea (the `nwfug:<campaign>` marker, idempotent by finding key, the same channel and
  linkage `nw idea capture`/close-time follow-ups use), at collation time rather than at
  close; the idea's line names the campaign, loop and AC tie. The close-time path skips keys
  already filed.
- **Failure:** a failed or invalid collator run falls back to today's routing (all
  findings in scope) for that loop, with a `collator_failed` event; never a park.
- **Export:** campaigns gain `out_of_scope_findings` and `collator_fallbacks`; findings gain
  `scope` and `tie`.

Flag: `state.lean.collator` (requires `packet`; pinned at open, SPEC-0553).

## Alternatives considered

- **Extend `review_triager` to scope findings** — the triager is a waiver of last resort
  that runs only on score-only fixes; scoping is needed on every fix loop and is a different
  contract (no waiving, no severity changes).
- **Code-only scoping by file overlap** — misses findings that are in scope through an AC on
  unchanged code (a missing test, a missing behaviour); the marshal ties findings to ACs,
  code keeps the hard guards.
- **One idea per out-of-scope finding via `nw idea capture`** — floods the org; the grouped
  idea per campaign is the existing, idempotent pattern.

## Acceptance criteria

- [ ] With `lean.collator` on, every review loop writes `findings-loop-N.json`, each input
      finding appears exactly once, and every collated finding carries a tie and a scope.
- [ ] The collator runs at tier marshal and never changes a severity (code reverts any
      change).
- [ ] A finding citing a changed file, a `must_fix`, or an unmet-AC finding is never out of
      scope, whatever the collator says.
- [ ] Only in-scope findings are scored and reach the implementer's prompt.
- [ ] Out-of-scope findings are in the campaign's grouped follow-up idea after the loop,
      linked to the campaign, without being filed twice at close.
- [ ] A collator failure falls back to today's routing for that loop and records
      `collator_failed`; with the flag off, routing is identical to today.
- [ ] The regression-set replay keeps the SPEC-0529 reflection oracle and the SPEC-0476
      trailing-newline hang in scope (both reach the implementer).

## Test plan

- **Automated tests:**
  - `tests/test_agentic_collator.py` (new): guards (diff overlap, must_fix, unmet AC, dropped
    finding restored, severity reverted), routing of in-scope only, fallback on failure.
  - `tests/test_ideas_group_followups.py` (extend): collation-time filing into the grouped
    idea, idempotent across loops and at close.
  - `tests/test_agentic_review_triage.py` (extend): scoring on the in-scope set.
  - FakeRunner queue test: a loop with one in-scope and one out-of-scope finding — the
    implementer brief has only the first, the follow-up idea has the second.
- **Regression set:** feed the recorded verdicts of `SPEC-0529-20261110-f7b4` (loop with
  seq 552/580/593) and `SPEC-0476-20261109-ca46` (seq 29/217/238) to the collator; both
  catches must come out `in`.
- **Measurement:** ≥10 Meta-Tools campaigns with `tiers + packet + collator` vs
  `tiers + packet`: median fix loops (`loops`) per merged campaign, and scope rulings per
  campaign = human/steward waivers (`waived_count` minus triager waivers) plus escalations
  and steward mailbox threads about scope.

## Rollout / migration

Flag off by default; requires `packet`. On for Meta-Tools via `agentic.project_lean`,
measured, then default-on. Existing grouped follow-up ideas are reused.

## Risks

- **A real bug scoped out.** Guards make in-diff, must_fix and unmet-AC findings
  un-scopeable; the rest still lands in a follow-up idea, not lost.
- **Marshal mis-merges distinct findings.** `merged` lists are kept; the implementer brief
  shows the merged titles; the export keeps every original finding.
- **Follow-up noise.** One grouped idea per campaign, capped by `followups_max`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; measurement recorded.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated.

## Open questions

- Should out-of-scope `major` findings also notify the project steward (mailbox), or is the
  follow-up idea enough? (Default here: idea only.)
