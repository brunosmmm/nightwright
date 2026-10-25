---
id: SPEC-0203
title: "Review triage: combiner, waivers and automatic follow-up ideas"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, review, triage, queue]
source_idea: IDEA-031
depends_on: [SPEC-0201, SPEC-0202]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-031`).

With [SPEC-0201](./0201-test-reviewer-persona.md) and
[SPEC-0202](./0202-adversarial-reviewer-persona.md) three reviewers return verdicts in parallel.
SPEC-0201 shipped an interim rule: any `fix` or `must_fix` ⇒ fix. That is safe but blunt: one
reviewer's `major` nit-pick on a sound change burns an implementer loop (and a fresh budget),
and the observations that were not worth blocking on are lost. The plan calls for a **review
triage**: decide fix versus forward, and make sure what is forwarded is not forgotten. This
spec replaces the interim combiner with a deterministic scorer, adds an optional small LLM
triager for the ambiguous cases, and files the non-blocking findings as follow-up ideas.

## Goals / Non-goals

**Goals**
- Deterministic combiner: any `must_fix` ⇒ fix; otherwise a score from weighted findings
  compared with a per-profile threshold.
- Optional `review_triager` persona, spawned only when reviewers disagree or the score is
  borderline, allowed to waive `minor`/`nit` findings (never `major`/`must_fix`) with a reason.
- Outcome `pass` ⇒ `CLOSING`; `fix` ⇒ `IMPLEMENTING` with one merged `fix_brief` (`loops += 1`).
- Automatically file waived and non-blocking findings above `nit` as follow-up ideas, record
  them as `followup` ledger events, and list them on the merge.

**Non-goals**
- Changing the reviewers or the fan-out (SPEC-0201/0202) or the close step.
- Auto-implementing follow-ups; they are ordinary ideas for the human or a later campaign.
- Letting an LLM override a `must_fix` or waive a `major`.

## Decision

Replace the body of `combine_reviews` with a pure scorer (`triage_reviews`), add the
`review_triager` persona behind a pure `needs_triager` predicate, and add a deterministic,
idempotent follow-up filer run at the `REVIEWING` exit. Only the deterministic part is
authoritative; the persona can only waive low-severity findings.

## Design

### Deterministic combiner

`triage_reviews(verdicts, profile, waived=()) -> {outcome, score, blocking, advisory,
borderline, disagreement}` — pure, in `queue.py`:

- Normalize all findings across reviewers (a `pass` verdict containing a `must_fix` counts as
  `fix`; a reviewer `fix` with no finding at or above `major` counts as one synthetic
  `major` titled from its `fix_brief`). Findings listed in `waived` are removed first, never a
  `must_fix` or `major`.
- Any `must_fix` ⇒ `outcome=fix` (no threshold, no triager).
- `score = max(0, 100 − Σ weight[severity])` over the remaining findings, default weights
  `{must_fix: 100, major: 15, minor: 4, nit: 1}`. `outcome = pass` iff `score >=
  profile.review.pass_score` (defaults: `balanced` 80, `thorough` 90, `frugal` 70) **and** no
  reviewer's own `fix` verdict survives (a `fix` reviewer is reconciled by its findings: if all
  of its findings were waived, its `fix` no longer counts).
- `blocking` = findings of a fixing outcome that drive it; `advisory` = findings kept but not
  blocking; `borderline` = `abs(score − pass_score) <= profile.review.borderline_margin` (default
  5); `disagreement` = reviewers' final verdicts differ.
- Profile keys under `review:`: `pass_score`, `borderline_margin`, `weights` (partial delta merges),
  `triager: true|false` (default true in `balanced`/`thorough`, false in `frugal`),
  `followups: true|false` (default true), `followups_nits: true|false` (default false). Validated by `resolve_profile`.

### `review_triager` persona (optional)

- `needs_triager(result, profile)`: `triager` enabled, no `must_fix`, and (`disagreement` or
  `borderline`) and at least one waivable (`minor`/`nit`) finding exists. Otherwise
  the deterministic outcome stands with no extra run.
- Persona `review_triager.md`, read-only, sonnet, effort medium, `max_budget_usd` 0.5 (frugal:
  haiku, 0.25; not run in frugal by default). Context: spec text, all reviewer verdicts,
  the deterministic result, and the list of waivable findings with stable ids
  (`<persona>#<index>`).
- Verdict schema `REVIEW_TRIAGER_SCHEMA`: `{waive: [{id, reason}], note}`. The code accepts only
  ids that name an existing `minor`/`nit` finding (others are ignored and logged as a `note`
  event); a waiver without a non-empty reason is ignored. Then `triage_reviews` re-runs with
  `waived` set; the triager is spawned at most once per loop. A malformed or failed triager
  run never blocks: the deterministic result stands (it only adds a `note` event). Its cost
  is charged and counted in the cap check like any persona (cap check covers reviewers' sum
  plus, conservatively, the triager's budget only when it would run, checked again before that
  spawn: over cap ⇒ skip the triager, deterministic result stands).
- Each waiver is recorded as a `note` event (`text`: `waived <id>: <reason>`)
  and `state.last_review.waived = [{id, persona, title, reason}]`.

### Routing and state

- `REVIEWING` after all reviewers (and the optional triager): `pass` ⇒ `CLOSING`
  (`review_pass`); `fix` ⇒ `loops += 1` → `IMPLEMENTING` (`review_fix`) with one
  `pending_brief` `{source: review, fix_brief, findings: blocking}` where `fix_brief` merges the
  reviewers' briefs (persona-labelled) plus the blocking finding titles, de-duplicated by
  `(files, title)`; `loop_max` ⇒ `PARKED(loop_exhausted)`; reviewer malformed twice ⇒
  `PARKED(review_escalated)` (SPEC-0201). No new park reasons.
- `state.last_review` gains `score`, `outcome`, `waived`, `advisory`, `triager_run`.
- The score appears in the `transition` event's reason detail and in `wt queue list` / `show`.

### Follow-up filing

- At the exit to `CLOSING` (outcome `pass`) — and also when an outcome `fix` later ends in
  a pass — file every **advisory** finding of severity `major` or `minor`, and every **waived**
  `minor` finding (reason `waived`), that has not been filed yet. `nit` findings, advisory or
  waived, are filed only when `profile.review.followups_nits` is true (default false).
  Findings already addressed by a later implementer loop are not filed: only the findings of
  the **final** passing review are considered, so a fixed finding never becomes an idea.
- Filing: `wt idea capture --kind improvement --project <campaign project> [--epic <campaign's
  parent epic, when the spec has one>] "<summary>"`, called through the in-process API
  (`org_write`) in the main repo's data dir, never inside the worktree. Summary text:
  `[<spec id>] <persona>/<category-or-ac>: <title> — <first 200 chars of detail> (files: …)`.
  After capture the new idea's heading is **verified** by reading it back (`wt idea show` logic):
  heading must contain the finding title; mismatch ⇒ the idea is left, a `note` event records
  the mismatch, the finding stays unfiled-recorded.
- Idempotent: a `followup` event is recorded per finding with a stable key `sha1(spec_id,
  persona, severity, title, files)`; re-running the exit (resume, retry) skips keys already in
  the ledger. A capture error never blocks the merge: it becomes a `note` event and the finding
  is listed as `unfiled`.
- **Ledger:** new event type `followup` registered in `schemas.py` `EVENT_KEYS`:
  `("persona", "severity", "title", "key", "idea_id", "reason")` where `reason` is `advisory`
  or `waived`; `idea_id` is null when filing failed or is disabled.
- **On the merge:** the close step's merge summary (and `wt queue show`) lists the follow-ups
  (`IDEA-NNN severity title`) and the waivers with their reasons; `wt campaign` summary includes
  the same.

### Profile changes

`BUILTIN_PROFILES[*]["review"] = {pass_score, borderline_margin, weights, triager, followups}`
as above; `personas.review_triager` added to each profile (settings above).

## Alternatives considered

- **Let an LLM decide everything** — non-reproducible and can talk itself out of real defects;
  keeping must-fix and major non-waivable bounds the damage of a bad judgment.
- **Always run a triager** — pays $0.5 on every clean review; unnecessary when reviewers
  agree and the score is far from the threshold.
- **Do not file follow-ups; only list them** — lists in a merge log are not seen again; ideas
  are the repo's durable backlog.
- **File every finding including nits** — noise; nits are opt-in.

## Acceptance criteria

- [ ] `triage_reviews` is pure and parametrized-tested: must_fix ⇒ fix regardless of score;
      weights and `pass_score` per profile; a `fix` reviewer whose findings were all waived no
      longer forces `fix`; waivers never remove `major`/`must_fix`; borderline and disagreement
      flags correct.
- [ ] `resolve_profile` provides and validates `review:` defaults and `personas.review_triager`;
      user deltas merge.
- [ ] `needs_triager` is false when reviewers agree and the score is clear, or a `must_fix`
      exists, or `triager` is off, or the cap cannot fit it; true otherwise. At most one
      triager run per loop.
- [ ] Triager waivers: only existing `minor`/`nit` ids with a reason are honored; others are
      ignored with a `note`; a malformed or failed triager run leaves the deterministic outcome.
- [ ] Routing: `pass` ⇒ `CLOSING`; `fix` ⇒ `IMPLEMENTING` with one merged, de-duplicated
      brief and `loops += 1`; `loop_max` ⇒ `PARKED(loop_exhausted)`.
- [ ] `followup` is registered in `schemas.py` and valid under `ledger_problems`; follow-ups
      are filed via the in-process capture as `--kind improvement` with the campaign's project
      (and parent epic when it has one), heading-verified, one `followup` event per
      finding, idempotent on resume, never for findings fixed in a later loop, never for
      `nit` by default; a capture failure does not block the merge.
- [ ] The merge summary and `wt queue show` list follow-ups and waivers.
- [ ] End to end with FakeRunner (all three reviewers and the triager scripted): (a) clean
      pass, no triager run, no follow-ups; (b) minor/major advisory findings with a passing
      score ⇒ `MERGED` and follow-up ideas filed in a temp data dir with the right project/
      epic/kind; (c) disagreement ⇒ triager runs, waives a minor ⇒ `MERGED`, waiver recorded;
      (d) must_fix ⇒ `IMPLEMENTING` once, then pass; fixed findings are not filed; (e) idempotent
      resume of the exit files each idea once.
- [ ] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_review_triage.py` (new: `triage_reviews`,
  `needs_triager`, waiver validation, brief merge/dedup, follow-up key stability);
  `tests/test_agentic_profiles.py` (`review:` defaults/validation); `tests/test_agentic_schemas.py`
  (`followup` accepted, missing key rejected); `tests/test_agentic_queue.py` (routing, cost
  caps incl. triager skip); `tests/test_agentic_e2e.py` (paths a-e; ideas read back from the temp
  org data with project, epic, `improvement` kind and verified heading).
- **Manual verification:** on the next real multi-reviewer campaign confirm the score,
  any waiver, and filed follow-up ideas (`wt idea list`) are recorded here.
- **Regression guard:** full `uv run pytest`; SPEC-0201/0202 e2e scripts keep passing under
  the new combiner (their expected outcomes are unchanged because they use `must_fix` or all-pass).

## Rollout / migration

Takes effect on the next `REVIEWING`; no data migration. Existing ledgers without `followup`
events are valid. Users can set `review.followups: false` or `review.triager: false` to opt out.
Restart the running loop to load the code.

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
