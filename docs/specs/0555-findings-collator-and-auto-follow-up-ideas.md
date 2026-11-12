---
id: SPEC-0555
title: Findings collator (marshal) and automatic follow-up ideas for out-of-scope findings
status: in-progress
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

### Design as built (where it differs from the above)

- **A REVIEWING sub-step, like the triager.** `_route_reviews` (flag pinned, every reviewer
  valid) calls `_start_collation`: the verdict set (`{persona: run id}`, the collation's
  `runs` signature) gets `last_review.collation = {status: pending}` and the campaign stays
  REVIEWING (`review_collate`); `plan_step` then returns `Step("review",
  persona="findings_collator")` before the triager check. Its harvest (`_apply_collator`)
  applies the verdict and routes the review in the same apply. A new verdict set (a retried
  reviewer, the SPEC-0553 deep close pass, a re-entered review) has a new signature, so it is
  collated again; `_begin_review` drops `collation` with the verdicts. No queue state, no
  retries, no park on any path. A loop with no finding writes `findings-loop-N.json` with
  `status: skipped` and runs nothing.
- **The collator never writes text.** Its output is `{collated: [{id, merged, tie, scope,
  reason, severity}]}` (key `collated`, so `_record_run` emits no `finding` events for it).
  `collator.collate` copies title, detail and files from the canonical input finding and
  takes `severity` = the highest input severity of the record's members (a collator severity
  that differs is reverted and kept as `severity_reverted`). The file also keeps every input
  finding verbatim under `inputs`.
- **Routing keeps the triage scorer.** Merges do not change scoring: each stored finding is
  marked `scope` / `tie` from its record (`collator.scoped`, a copy; the stored verdicts are
  unchanged) and `triage_reviews` lists `scope: out` findings under `out_of_scope` (key present
  only when non-empty) and never scores them; a reviewer whose `fix` rested only on
  out-of-scope findings no longer backs a fix, and no "fix without findings" synthetic is
  added. Synthetic unmet-AC findings are built by the triage from `ac_assessment`, so they are
  never scoped out by construction. The triager's input and waivers use the scoped verdicts.
- **Guards as built.** `out` becomes `in` (`guard`) when the tie is an AC id of the spec,
  `decision`, `design` or `diff:<changed file>` (the scope rule: a tied finding is in scope),
  when a member cites a changed file (diff = the packet stat plus `git diff --name-only
  --no-renames base...HEAD`; cited paths are normalized: worktree prefix, `./` and `:line`
  suffixes dropped), a member is `must_fix`, or a member's reviewer `ac` covers an unmet AC
  (`queue._covers`). A malformed tie (unknown AC id, `diff:` of an unchanged file, free text)
  becomes `none`. Ids the collator invents or repeats are ignored (`unknown`); a verdict that
  names none of a non-empty input (seen in the real call: Haiku once emitted its structured
  output twice, the second time `collated: []`) raises and falls back.
- **Implementer prompt.** Once a collation scoped anything out, `review_brief` builds the fix
  brief from the in-scope blocking findings only (verbatim, each `[tie]`-tagged) instead of the
  reviewers' free-text `fix_brief`s, which may describe out-of-scope work; must-fix items carry
  `tie` (rendered as the item tag when there is no `ac`). The implementer's
  `{{previous_verdicts}}` then leaves out the review group's `run.end` events. Nothing scoped
  out: today's brief plus the tie tags.
- **Follow-ups.** `followups.file_out_of_scope` runs `_file` (the close-time filer) on the
  out-of-scope entries: one entry per member finding (verbatim title / files, so its key is the
  close-time key), origin `out_of_scope`, detail `out of scope (<campaign> loop N, tie T):
  <reason> | <reviewer detail>`. Same group, cap (`followups_max`), `followups: false` switch,
  idempotency (ledger `followup` keys, own group lines, other open groups); it never raises.
  The close skips those keys through its existing ledger check.
- **Packet interaction (SPEC-0554).** `packet.remember_verdicts` keeps the scoped-out findings
  in `packet_prev.out_of_scope`; the next loop's packet marks them `waived` ("out of scope:
  filed as a follow-up idea"), so a delta-mode reviewer does not carry them as open priors.
  Demoted (advisory) findings are not collated.
- **Settings.** Builtin profiles do not list the collator (their sha must not change for
  flag-off campaigns); `profiles.persona_settings` falls back to `collator.SETTINGS`
  (haiku / medium, $0.40, read-only, `dontAsk`), a profile may set it. With `lean.tiers` the
  model is the marshal tier's (`claude-haiku-5-5`, effort `medium`).
- **Events / export.** New ledger events `findings_collated` (`loop`, `status`, `findings`,
  `out`, `scopes: [{id, key, persona, scope, tie}]`, `stats`) and `collator_failed` (`loop`,
  `error`, `run_id`); `review_outcome` gains `out_of_scope` (flag pinned only). Export:
  `campaigns.out_of_scope_findings` (distinct keys scoped out by each loop's last collation),
  `campaigns.collator_fallbacks` (`collator_failed` count), `findings.scope` / `findings.tie`
  (from the first collation after the finding that lists its key; null otherwise).
- **Open question default kept:** out-of-scope `major` findings go to the idea only, no
  steward mail.

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

- [x] With `lean.collator` on, every review loop writes `findings-loop-N.json`, each input
      finding appears exactly once, and every collated finding carries a tie and a scope.
- [x] The collator runs at tier marshal and never changes a severity (code reverts any
      change).
- [x] A finding citing a changed file, a `must_fix`, or an unmet-AC finding is never out of
      scope, whatever the collator says.
- [x] Only in-scope findings are scored and reach the implementer's prompt.
- [x] Out-of-scope findings are in the campaign's grouped follow-up idea after the loop,
      linked to the campaign, without being filed twice at close.
- [x] A collator failure falls back to today's routing for that loop and records
      `collator_failed`; with the flag off, routing is identical to today.
- [x] The regression-set replay keeps the SPEC-0529 reflection oracle and the SPEC-0476
      trailing-newline hang in scope (both reach the implementer). *(See As shipped.)*

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

## As shipped

Shipped behind `agentic.lean.collator` (default **off**, already in `config.DEFAULT_CONFIG`
from SPEC-0553; needs `packet`, pinned per campaign at creation). Flag off, or a campaign
pinned before this change (its `state.lean` has no `collator` key, whatever the live config
says): no collator run, no `findings-loop-N.json`, no `last_review.collation`, no
`findings_collated` / `collator_failed` events, no `out_of_scope` on `review_outcome`, today's
fix brief (reviewers' free text), nothing filed mid-loop; `triage_reviews` returns the same keys
(asserted with the flag absent, `{packet}` and `{tiers, packet}` pinned and the live config
flipped on after the open, and with the collation helpers patched to raise).

**Modules.** `agentic/collator.py` (flag, signature / pending, `inputs`, `input_doc` /
`collator_values`, `SCHEMA`, the guards `collate`, `scope_map` / `annotate` / `scoped`,
`out_findings`, `document` / `write`, `followup_entries`), `persona_defs/findings_collator.yaml`
+ `personas/findings_collator.md` (`FINDINGS_COLLATOR_SCHEMA`, placeholder `collator_input`),
`profiles` (`DEFAULTED_PERSONAS`, `collator.SETTINGS`), queue (`plan_step` sub-step,
`_start_collation`, `_prepare_collator`, `_apply_collator`, `_collation_done` /
`_collation_failed`, `_apply_collation` / `_scoped_verdicts`, `triage_reviews` `out_of_scope`
and ties, `review_brief`, `_previous_runs`, `_begin_review`), `followups.file_out_of_scope`,
`packet` (`packet_prev.out_of_scope` -> `waived`), `schemas` (two events), export columns, docs
(capabilities, README), `tools/replay_collator.py` (the regression-set replay).

**Tests.** New `tests/test_agentic_collator.py` (pure guards: diff overlap incl. `./` and
`:line` paths, must_fix, unmet AC, AC / decision / design / diff ties, malformed ties, dropped
restored, every input once, severity reverted, verbatim text, unknown / duplicate ids, empty
verdict raises, demoted inputs skipped, annotation and flag-off identity, follow-up entries;
FakeRunner: one in-scope + one out-of-scope finding (brief has only the first, the grouped idea
has the second after loop 1 and before the close, filed once, the loop-2 packet waives it,
export columns), a scoped-out changed-file `must_fix` forced back in, collator failure
(malformed run, raising guard) -> today's routing + `collator_failed` + no park, no findings ->
no run, flag off / pinned-before-the-flag). Extended `tests/test_ideas_group_followups.py`
(collation-time filing into the group, idempotent across loops and at close, `followups:
false`, filer failure), `tests/test_agentic_review_triage.py` (scoring on the in-scope set,
brief with ties and no out-of-scope text, no new keys without scopes); registry / CLI / tiers /
personas / export fixtures for the new builtin persona and columns. Affected tests pass except
three that fail identically on the base commit 62937eb6 (`test_tui_campaign_records::
test_records_table_order_labels_and_no_placeholder`, two in `test_worker_job_reconcile`).

**Real calls (`claude -p --model claude-haiku-5-5 --effort medium`, tier marshal, read-only).**
`tools/replay_collator.py` on scratch copies (campaign dirs copied to a scratch data dir,
worktrees scratch clones at the reviewed head; no live campaign dir written), the reviewers'
recorded verdicts of each loop as input, the packet built by code (marshal not run):

| Campaign / loop (runs) | Findings | Cost | Tokens (cache create / read / out) | Result |
|---|---|---|---|---|
| SPEC-0529 loop at `ee44a190` (072-074, seq 552) | 19 | $0.0076 | 21,163 / 36,809 / 6,062 | 19 records, 0 out; "Signed 401 is a MAC oracle" `in`, tie AC9 |
| SPEC-0529 loop at `79aff687` (076-078, seq 580/593) | 22 -> 18 records | $0.0059 | 17,800 / 9,914 / 4,458 | 0 out; both MAC-oracle findings (`must_fix`) and the test gap (`major`) `in`, AC9 |
| SPEC-0476 loop 1 at `bc9371ed` (003-005, seq 29) | 17 -> 15 | $0.0049 | 15,215 / 9,914 / 3,476 | 0 out; the trailing-fragment `must_fix` `in`, AC4 |
| SPEC-0476 loop at `aaea30bf` (016-018, seq 217/238) | 17 -> 13 | $0.0087 | 27,700 / 43,527 / 5,368 | 0 out; "hangs on a no-newline fragment" (`must_fix`) and "no trailing newline hangs the worker" (`major`) `in`, AC4 |

No guard had to fire and no severity was changed in these four runs. A first SPEC-0529 run
($0.0079) exposed a failure mode: Haiku emitted its structured output twice, the second time
`collated: []`, which the guards turned into 19 restored findings. Since then an empty list for
a non-empty input is a failed run (`collator_failed`, today's routing) and the template asks for
the object once with every finding; the rerun above is after that change. Total real-call spend
$0.035.

**Post-merge.** The measurement (≥ 10 Meta-Tools campaigns with `tiers + packet + collator` vs
`tiers + packet`: median `loops` per merged campaign and scope rulings per campaign) needs live
campaigns with the flag on (`agentic.project_lean.Meta-Tools.collator: true`) and is recorded
here after merge. The regression set above contained no finding the collator scoped out, so the
follow-up path has been exercised by the FakeRunner tests only.

## Open questions

- Should out-of-scope `major` findings also notify the project steward (mailbox), or is the
  follow-up idea enough? (Default here: idea only.)
