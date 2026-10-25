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
SPEC-0201 specifies an interim rule: any `fix` or `must_fix` ⇒ fix. That is safe but blunt: a
reviewer's `fix` verdict over a handful of `minor`/`nit` observations on a sound change burns an implementer loop (and a fresh budget),
and the observations that were not worth blocking on are lost. The plan calls for a **review
triage**: decide fix versus forward, and make sure what is forwarded is not forgotten. This
spec replaces the interim combiner with a deterministic scorer, adds an optional small LLM
triager for the ambiguous cases, and files the non-blocking findings as follow-up ideas.

Revised 2026-10-25 after spec review (docs/specs/reviews/SPEC-0203.json).

## Goals / Non-goals

**Goals**
- G1. Deterministic combiner: a reviewer verdict blocks only when backed by a `must_fix` or
  `major` finding (or is an unexplained `fix`); everything else is judged by a weighted score
  against a per-profile threshold.
- G2. Optional `review_triager` persona, spawned only when waiving low-severity findings could
  change a score-only `fix` into a `pass`; it may waive `minor`/`nit` findings (never
  `major`/`must_fix`) with a reason.
- G3. Outcome `pass` ⇒ `CLOSING`; `fix` ⇒ `IMPLEMENTING` with one merged `fix_brief`
  (`loops += 1`).
- G4. File advisory and waived findings as follow-up ideas (idempotent, crash-safe), record them as
  `followup` ledger events, and list them where the merge is reported.

**Non-goals**
- Changing the reviewers or the fan-out (SPEC-0201/0202) or the merge itself.
- Auto-implementing follow-ups; they are ordinary ideas for the human or a later campaign.
- Letting an LLM override a `must_fix`, waive a `major`, or flip a verdict that a `major` backs.
- Splitting: considered and declined (scorer, triager and filer share the `last_review`
  `advisory`/`waived` interface and one e2e matrix). If the implementer budget runs out, the
  natural cuts are scorer+routing first, then triager, then filer.
- Filing findings of a campaign that never reaches `CLOSING` (parked, abandoned): its advisory
  findings stay in the ledger only. Accepted limitation.

## Decision

Once SPEC-0201 lands (this spec depends on it), replace its interim `combine_reviews` with a pure
scorer `triage_reviews`; `_apply_reviews` (SPEC-0201) calls it instead and `combine_reviews` is
deleted (its parametrized tests are replaced by `triage_reviews` tests; the SPEC-0201 e2e scripts
keep their outcomes because they use `must_fix` or all-pass). Add the `review_triager` persona behind
a pure `needs_triager` predicate, and a deterministic, idempotent follow-up filer run at the start
of the close step. Only the deterministic part is authoritative.

**What governs: verdict or score?** Both, in a fixed order. (1) Any unwaived `must_fix` ⇒ `fix`.
(2) A reviewer `fix` verdict blocks (⇒ `fix`) when it is backed by at least one `major` finding
of its own, or is unexplained (it has no findings: then it counts as one synthetic, unwaivable
`major`, id `<persona>#fix`, titled from its `fix_brief`). (3) Otherwise the score decides.
Consequently a `fix` verdict whose findings are all `minor`/`nit` does **not** block by itself: its
findings only lower the score. That is the one real relaxation versus SPEC-0201's rule and is
exactly the case the Context describes (a verdict of `fix` on a sound change over nit-picks). A
`pass` verdict that carries a real `major` is *not* blocked by that alone: reviewers set the verdict
and a `pass` is respected, so the `major` costs 15 points, is reported as advisory and is filed
as a follow-up (an LLM saying `pass` over a material defect is the reviewer's call; the `must_fix`
severity and the synthetic AC `major` are the hard stops). A
`major` backs a `fix` deliberately: a reviewer that names a concrete material defect must be
heard; waivers cannot touch it.

## Design

### Deterministic combiner

`triage_reviews(verdicts, profile, waived=()) -> {outcome, score, score_only, blocking, advisory,
waivable_weight}` — pure, in `queue.py`. `verdicts` is the ordered `{persona: verdict_dict}` of
valid verdicts, rebuilt on every call from `state.last_review.verdicts[persona]`, which SPEC-0201
persists with each reviewer's full `findings` list and `fix_brief` (so later ticks, a retry pass
and the triager step do not need the ledger; the `finding` events keep only an audit copy).
Reviewers' own `score` values (kept in `verdicts[*].score`) are **not** inputs to triage.
Steps, in this order:

1. **Normalize.** Each real finding gets id `<persona>#<index>` (index in the verdict's list). A
   `pass` verdict containing a `must_fix` is treated as `fix`. A `fix` verdict with no findings
   adds the synthetic `major` above. A conformance `ac_assessment` entry with `met: false`
   for which the reviewer reported no finding of severity `major`/`must_fix` carrying that `ac`
   adds a synthetic, unwaivable `major` (id `<persona>#ac<i>`), so the relaxation below never
   hides an unmet acceptance criterion. Findings identical by `(sorted files, norm(title))`, where `norm` lower-cases and collapses
   whitespace and is the **same function** the follow-up key uses,
   across reviewers are **de-duplicated before scoring**: one merged finding kept, with the
   highest severity among the copies, listing all reporting personas — a duplicate issue is
   counted once (intended; two reviewers agreeing is not twice the defect). Its **canonical id**
   is that of the copy with the highest severity, ties broken by the earliest persona in profile
   order. A merged finding *belongs to every persona that reported it*: it counts toward each
   reporter's verdict in step 3. Waivers (step 2) and the triager reference canonical ids only;
   waiving a merged finding removes it for all reporters.
2. **Waive.** Remove findings whose id is in `waived`, only if their severity is `minor` or `nit`
   (a `must_fix`/`major`/synthetic id in `waived` is ignored). Waiving happens before step 3,
   and the synthetic `major` is never present in the waivable set, so a verdict with real
   `minor`/`nit` findings never carries a synthetic one.
3. **Block by verdict.** A reviewer's `fix` verdict *survives* iff it is unexplained (synthetic
   `major`) or at least one of its surviving findings is `major` or `must_fix`.
4. **Score.** `score = max(0, 100 − Σ weight[severity])` over all surviving findings, default
   weights `{must_fix: 100, major: 15, minor: 4, nit: 1}`.
5. **Outcome.** `fix` iff any surviving `must_fix`, or any surviving `fix` verdict (step 3), or
   any synthetic AC `major` (step 1: it blocks whatever the reviewer's verdict said, like a
   `must_fix`), or `score < profile.review.pass_score` (defaults: `balanced` 80, `thorough` 90,
   `frugal` 70); else `pass`. `score_only` = outcome is `fix` and none of the first three causes
   holds.
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
  score `100−28 = 72` ⇒ `fix`, `score_only`, `waivable_weight 28`. The triager waives three
  `minor` (12) ⇒ score `84` ⇒ `pass`.
- any `must_fix` ⇒ `fix` whatever the score.
- conformance `pass` with one real `major`: no verdict survives, score `85` ⇒ `pass`, the `major`
  is advisory (and filed).
- A `fix` with one `minor` that a second reviewer reported as `major` (same files and title): one
  merged finding (`major`, canonical id the second reviewer's) belongs to both; the first
  reviewer's verdict survives through it (and is therefore not "unexplained"), outcome `fix`.
- conformance `pass` with `ac_assessment` `[{met: false}]` and only a `minor` finding on that
  AC: a synthetic AC `major` is added; score `100−15−4 = 81 ≥ 80`, yet the outcome is `fix`
  (the synthetic AC `major` blocks independent of score and verdict), `blocking = [synthetic]`.

Profile keys under `review:`: `pass_score` (0..100), `weights` (partial delta merges over the
default map; every value an int ≥ 0), `triager: true|false` (default true in
`balanced`/`thorough`, false in `frugal`), `followups: true|false` (default true),
`followups_nits: true|false` (default false). All validated by `resolve_profile`
(`ValueError` naming the key).

### `review_triager` persona (optional)

- `needs_triager(result, profile) -> bool` (pure, takes the `triage_reviews` result):
  `profile.review.triager` is true, `result.outcome == "fix"`, `result.score_only`, and
  `result.score + result.waivable_weight >= pass_score` (waiving **everything** waivable could
  flip it). In every other case — a `must_fix`, a surviving `fix` verdict, a clear pass, a score too
  low to rescue — the triager **cannot change the outcome** and is not run. The cost-cap check is a
  separate step in `_apply_reviews` (`_cap_blocks` with the triager's budget; it takes `state`,
  not the result; the triager's budget is added to the pending sum): over the cap ⇒ the triager is skipped and the deterministic result stands,
  with a `note` event.
- **Mechanics:** when `needs_triager` and the cap fits, `_apply_reviews` records
  `last_review.triage = "pending"` and stays in `REVIEWING` (reason `review_triage`); the next
  `plan_step` for `REVIEWING` sees every reviewer valid and `triage == "pending"` and returns
  `Step("review", persona="review_triager", personas=("review_triager",))`. After its run (or its
  failure) `triage` becomes `"done"`, so it runs **at most once per loop**; when `needs_triager` is true but the cap refuses it,
  `triage` is set to `"skipped"` (terminal, so later ticks never re-evaluate it); `triage` is cleared
  with `verdicts` when the loop's verdicts are replaced.
- Persona `review_triager.md`, read-only, sonnet, effort medium, `max_budget_usd` 0.5 (frugal:
  haiku, 0.25; not run in frugal by default since `triager: false`). Context: spec text, all
  reviewer verdicts, the deterministic result, and the waivable findings with their stable ids.
  It is told to waive only findings that are subjective, out of scope, or non-defects.
- Verdict schema `REVIEW_TRIAGER_SCHEMA`: `{waive: [{id, reason}], note}`. The code accepts only
  ids that name an existing `minor`/`nit` finding (others are ignored and logged as a `note`
  event); a waiver without a non-empty reason is ignored. Then `triage_reviews` re-runs with
  `waived` set and routes. A malformed or failed triager run never blocks: the deterministic
  result stands (it only adds a `note` event). Its cost is charged like any persona's.
- Each waiver is recorded as a `note` event (`text`: `waived <id>: <reason>`) and
  `state.last_review.waived = [{id, persona, title, severity, reason}]`.

### Routing and state

- `_apply_reviews` after all reviewers (and the optional triager): `pass` ⇒ `CLOSING`
  (`review_pass`); `fix` ⇒ `loops += 1` → `IMPLEMENTING` (`review_fix`) with one
  `pending_brief` `{source: review, personas, fix_brief, must_fix: blocking}` where `fix_brief`
  merges the fixing reviewers' briefs (persona-labelled) plus the blocking finding titles,
  de-duplicated by `(files, title)`; beyond `loop_max` ⇒ `PARKED(loop_exhausted)`; reviewer
  malformed after its retry ⇒ `PARKED(review_escalated)` (SPEC-0201). No new park reasons.
- `state.last_review` gains `outcome`, `waived`, `advisory` (list of
  `{id, personas, severity, title, detail, files}`), `triage`, `triager_run`, and **redefines**
  the top-level `score` as the weighted triage score (SPEC-0201 defined it as the minimum of the
  reviewers' own scores; readers of `last_review` see the new meaning; per-reviewer scores
  remain in `verdicts[*].score`).
- **Carry-over:** on every `fix` outcome the `advisory` findings and the waived ones are merged
  into `state.review_carry` (a list; de-duplicated by follow-up key, below), so advisory
  findings of intermediate loops are not lost although the implementer was never asked to fix
  them. `review_carry` is added to the key list that `_apply.finish` persists
  (`queue.py:602`). Blocking findings are never carried, and a carried key that becomes
  *blocking* in a later review is removed from the carry (the implementer is told to fix it).
  Blocking is evaluated **per key**: any blocking copy of a key (in any severity, from any
  reporter) removes its carry entry. A key that was carried and then neither reappears nor is
  blocking is still filed (below). A carried minor/major that the implementer happens to fix
  incidentally is still filed
  (accepted: the filer cannot tell, and a spurious follow-up is cheaper than a lost one).
- **Reporting the score:** each `_apply_reviews` transition is accompanied by a `note` event
  `review score <n>/<pass_score> <outcome>` (transition reason strings are unchanged), and
  `wt queue show` prints a `review:` line from `last_review` (score, outcome, waivers).
  `wt queue list --json` rows gain `review_score` (null when none).

### Follow-up filing

- **When:** at the start of the close step (`_do_close`, before `close_campaign`), i.e. only for a
  campaign whose review passed. A close step is re-run after a park/resume (`open_failed` with `resume_to: CLOSING`) or
  when the close returns the campaign to TRIAGING→…→CLOSING (`regate_failed`); each re-run
  repeats the filer, so a filing that failed *before a close that then failed* is retried. A
  capture error on a close that then succeeds is not retried: the finding is reported
  `unfiled`. Considered set = `state.review_carry ∪ last_review.advisory ∪
  last_review.waived`, de-duplicated by key; **file** those of severity `major` or `minor`;
  `nit` only when `profile.review.followups_nits`. `followups: false` files nothing. Only
  findings that were *blocking* are guaranteed absent from the set (see carry-over).
- **Where it runs:** `_do_close` (a serial worker-side step, `queue.py:559`) calls
  `file_followups(cfg, state)` first. It does not write the ledger itself: it returns the events
  (`followup`, `note`) in `StepResult.events_before`, which `_apply` appends on the main thread
  whether or not `close_campaign` then succeeds or raises, and a summary list
  (`[{idea_id, severity, title, reason}]`, `unfiled`, `waived`) on `StepResult.followups`;
  `_apply` stores it in `state.close.followups` (persisted: `close` is in the `finish` key list)
  and the merge summary / `wt queue show` render it. `_apply` sets `state["close"]["followups"]` **after** it assigns `state["close"] =
  oc.close` (which replaces the dict), or the list would be lost. A close that raises and is
  later re-run repeats the filer, which is idempotent.
- **Filing:** through the in-process API, `org_write.add_idea(cfg, text, project=<campaign
  project>, epic=<parent epic when the spec has one>, kind="improvement")`
  (`org_write.py:896`; the CLI `wt idea capture` calls the same function), in the main repo's
  data dir, never inside the worktree. Text: `wtfu:<key12> [<spec id>] <persona>/<slot>: <title> —
  <first 200 chars of detail> (files: …)` where `key12` is the first 12 hex chars of
  the key and `<slot>` is the finding's `ac` if present, else its `category` if present, else
  omitted together with its `/`. Before building the text, `title`, `detail` and file names are sanitized: newlines and
  control characters become spaces, `[`/`]` are dropped, `—` is written `-`, and the title is cut
  at 120 characters. The marker `wtfu:<key12>` leads the text (so no cap or wrapping can truncate it) and is
  alphanumeric (so sanitizing cannot alter it); a test files a finding with a 300-character
  title and 20 files and asserts the headline still starts with the marker.
- **Idempotency (crash-safe):** key = `sha1(spec_id|normalized title|sorted files)` where the title is lower-cased
  with whitespace collapsed — deliberately **without** persona or severity, so the same issue
  keeps its key when its severity or reporter set changes between loops. A carry/advisory/waived
  entry is `{key, persona (canonical reporter), severity (highest seen), title, detail, files,
  ac, category, reason: advisory|waived}`; on key collision the entry keeps the highest
  severity. For each finding to file:
  1. if a `followup` event with that key is in the ledger ⇒ skip;
  2. else look the marker up in the ideas file and its archive
     (`org_write.ideas_archive_path`); found ⇒ **adopt** it: write the `followup` event with the
     existing `idea_id`, do not capture again (this covers a crash between `add_idea` and the
     event);
  3. else capture; on success verify that the returned headline contains the marker; write the
     `followup` event with the new `idea_id` (a headline without the marker adds a `note`
     event but the event is still written, so a created idea is never captured twice);
  4. a capture error ⇒ `note` event, **no** `followup` event, so the next close attempt
     is retried only if the close step is re-run (see When). The merge is never blocked; after
     the close succeeds the finding is reported `unfiled`.
- **Ledger:** new event type `followup` registered in `schemas.py` `EVENT_KEYS`:
  `("persona", "severity", "title", "key", "idea_id", "reason")`, where `reason` is `advisory` or
  `waived`; `idea_id` is always set (events are only written for existing ideas).
- **Reporting:** the close outcome / merge summary and `wt queue show` list the follow-ups
  (`IDEA-NNN severity title`), the `unfiled` findings and the waivers with their reasons.

### Profile changes

`BUILTIN_PROFILES[*]["review"] = {pass_score, weights, triager, followups, followups_nits}`
as above; `personas.review_triager` added to each profile:

| profile | `pass_score` | `triager` | triager model / effort / budget |
|---|---|---|---|
| balanced | 80 | true | sonnet / medium / 0.5 |
| thorough | 90 | true | sonnet / medium / 0.5 |
| frugal | 70 | false | haiku / medium / 0.25 |

All three have `followups: true`, `followups_nits: false`, the default `weights`. `resolve_profile`
validates all five keys and the persona settings.

## Alternatives considered

- **Let an LLM decide everything** — non-reproducible and can talk itself out of real defects;
  keeping must-fix and major non-waivable bounds the damage of a bad judgment.
- **Always run a triager** — pays $0.5 on every clean review; the triager can only matter in the
  score-only case, so it runs only there.
- **Let the triager waive `major` or flip a verdict** — rejected; that is the LLM-decides-all case.
- **Do not file follow-ups; only list them** — lists in a merge log are not seen again; ideas
  are the repo's durable backlog.
- **File only the final review's findings** — rejected: advisory findings of earlier loops would
  be silently dropped (see carry-over).
- **File every finding including nits** — noise; nits are opt-in.

## Acceptance criteria

- [ ] AC1 (G1). `triage_reviews` is pure and parametrized-tested on the worked examples above:
      `must_fix` ⇒ `fix` regardless of score; a `fix` verdict with only `minor`/`nit` findings does
      not block (outcome by score); a `fix` verdict with a `major` blocks; a `fix` with no
      findings blocks via the synthetic `major`; weights and `pass_score` per profile; identical
      findings from two reviewers are scored once and a deduplicated `fix` verdict survives via the
      merged finding; a `met: false` AC with no `major`+ finding adds a synthetic `major` that forces `fix`
      even for a `pass` verdict at score 81 (balanced);
      waivers never remove `major`/`must_fix`/
      synthetic findings; `score_only` and `waivable_weight` correct.
- [ ] AC2 (G1, G2). `resolve_profile` provides and validates `review:` (`pass_score`, `weights`,
      `triager`, `followups`, `followups_nits`) and `personas.review_triager` for every builtin
      profile (exact defaults above); bad values raise `ValueError` naming the key; user deltas
      merge (partial `weights`).
- [ ] AC3 (G2). `needs_triager` is true iff `triager` is on, the outcome is a score-only `fix`
      and `score + waivable_weight >= pass_score`; false for a `must_fix`, a surviving `fix`
      verdict, a pass, a hopeless score, or `triager: false`. In the queue, a cap that cannot
      fit the triager's budget skips it with a `note`; at most one triager run per loop; a cap refusal sets `triage = "skipped"` and later ticks do not retry it.
- [ ] AC4 (G2). Triager waivers: only existing `minor`/`nit` ids with a non-empty reason are
      honored; others are ignored with a `note`; a malformed or failed triager run leaves the
      deterministic outcome; waivers are recorded as `note` events and in `last_review.waived`.
- [ ] AC5 (G3). Routing: `pass` ⇒ `CLOSING`; `fix` ⇒ `IMPLEMENTING` with one merged,
      de-duplicated brief and `loops += 1`; `loop_max` ⇒ `PARKED(loop_exhausted)`;
      `last_review` has `score`, `outcome`, `waived`, `advisory`, `triage`, `triager_run`;
      a `note` event `review score …` is written; `wt queue show` prints the `review:` line and
      `wt queue list --json` carries `review_score`.
- [ ] AC6 (G2). `review_triager`: template, `REVIEW_TRIAGER_SCHEMA`, `PERSONAS` entry,
      read-only `build_request` with deterministic prompt containing the waivable finding ids,
      per-profile model/effort/budget; `docs/product/capabilities/agentic.md` documents the
      `review:` block, the new `last_review` keys and the follow-up behavior.
- [ ] AC7 (G4). `followup` is registered in `schemas.py` and valid under `ledger_problems`
      (missing key rejected); the filer files `major`/`minor` advisory and waived findings
      (and `nit` only with `followups_nits`; nothing with `followups: false`) through
      `add_idea` with `kind="improvement"`, the campaign's project and its parent epic when it
      has one, embedding the `wtfu:` marker; advisory findings from earlier `fix` loops are
      filed once via carry-over; blocking findings are never filed.
- [ ] AC8 (G4). Idempotency: re-running the filer files each finding once, including after a
      simulated crash between `add_idea` and the event (the marker lookup adopts the idea) and
      after a capture error on the first attempt followed by a re-run of the filer (no
      duplicate); a carried finding whose key becomes blocking in a later loop (even at a
      higher severity) is not filed; a capture failure does not block the merge and the finding is reported
      `unfiled`; the filer's events are appended on the main thread even when
      `close_campaign` raises, and `review_carry` survives `_apply.finish`.
- [ ] AC9 (G4). The merge summary and `wt queue show` list follow-ups, unfiled findings and
      waivers with reasons.
- [ ] AC10. End to end with FakeRunner (all three reviewers and the triager scripted): (a) clean
      pass, no triager run, no follow-ups; (b) minor/major advisory findings with a passing
      score ⇒ `MERGED` and follow-up ideas filed in a temp data dir with the right project/
      epic/kind; (c) score-only `fix` ⇒ triager runs, waives minors ⇒ `MERGED`, waivers
      recorded; (d) `must_fix` ⇒ `IMPLEMENTING` once, then pass; the fixed finding is not
      filed while an advisory finding of the first review is; (e) the close step run twice
      (resume) files each idea once.
- [ ] AC11. Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_review_triage.py` (new: `triage_reviews` worked
  examples and dedup, `needs_triager` truth table, waiver validation, brief merge/dedup,
  follow-up key stability and text sanitizing, filer idempotency with a temp org data dir
  including the crash and error cases);
  `tests/test_agentic_profiles.py` (`review:` defaults/validation, AC2);
  `tests/test_agentic_personas.py` (`review_triager` template, schema, request, prompt
  determinism, settings per profile);
  `tests/test_agentic_close.py` (existing: merge summary lists follow-ups, unfiled findings and
  waivers; filer events recorded when `close_campaign` raises);
  `tests/test_agentic_logs.py` (existing ledger tests: add `followup` accepted / missing key
  rejected); `tests/test_agentic_queue.py` (routing, `last_review` shape, triage pending →
  triager step → done, cost-cap skip of the triager, carry-over, the `note` score event,
  `queue show` / `list --json`); `tests/test_agentic_e2e.py` (paths a-e; ideas read back from the
  temp org data with project, epic, `improvement` kind and marker in the headline).
- **Docs check:** a grep assertion in `tests/test_agentic_profiles.py` that
  `docs/product/capabilities/agentic.md` mentions `pass_score`, `followups` and `review_triager`.
- **Manual verification:** on the next real multi-reviewer campaign confirm the score,
  any waiver, and filed follow-up ideas (`wt idea list`) are recorded here.
- **Regression guard:** full `uv run pytest`.

## Rollout / migration

Takes effect on the next `REVIEWING`; no data migration. Existing ledgers without `followup`
events are valid; a `last_review` without the new keys is treated as "no verdicts" (the review
re-runs, SPEC-0201). Users can set `review.followups: false` or `review.triager: false` to opt
out. Restart the running loop to load the code.

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
