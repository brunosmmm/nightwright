---
id: SPEC-0202
title: "Adversarial reviewer persona: security, logic and bug hunting"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, review, personas, security]
source_idea: IDEA-030
depends_on: [SPEC-0201]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-030`).

The conformance ([SPEC-0187](./0187-conformance-review-reviewing-state-with-a-read.md)) and
test ([SPEC-0205](./0205-test-reviewer-persona.md)) reviewers both judge the change against the
spec. Neither is asked to attack the code itself: logic errors the spec does not mention,
injection or path traversal, unsafe subprocess or shell use, leaked secrets, races, swallowed
errors, or operations that can lose data. This repo shells out to `git`, `claude` and gate
commands and writes user data under the data dir, so such defects matter. This spec adds the
`adversarial_reviewer`, a further member of the multi-reviewer `REVIEWING` fan-out defined in
SPEC-0201 (see its "Multi-reviewer REVIEWING" section, which this spec relies on and does
not restate).

## Goals / Non-goals

**Goals**
- Read-only persona `adversarial_reviewer` that hunts for defects in the diff, with a
  `category` on every finding.
- Join the default `reviewers` list of `balanced` and `thorough`.

**Non-goals**
- Fan-out, cost-cap summing, combination rule, retry accounting: all in SPEC-0201.
- Score weighting, waivers, follow-up filing: SPEC-0203.
- Dynamic analysis, running exploits, fuzzing, dependency CVE scanning.

## Decision

Add the persona exactly as the other reviewers: template, schema, `PERSONAS` entry, profile
settings, and add it to the default `reviewers` lists. Its verdict has the same shape as the
conformance reviewer's plus a per-finding `category`.

## Design

- **Persona** `src/wt/agentic/personas/adversarial_reviewer.md`, `read_only=True`,
  `permission_mode: dontAsk`, `--restricted`, and is appended to `REVIEWER_PERSONAS`
  (SPEC-0201) so it gets the review context. It is told to assume the code is wrong until shown
  otherwise and to **try to break it by reading**: for each changed hunk, ask what input,
  ordering, timing or environment makes it misbehave. It must cite file and line for each
  finding and state a concrete trigger ("when `name` contains `../`…"); a finding without a
  trigger is at most `minor`. It does not re-judge spec conformance or test coverage (other
  reviewers do) and never edits files.
- **Context plumbing:** a new context key `changed_files`: `build_context` (with `review=True`)
  sets it to `git.changed_files(worktree, base)` sorted lexicographically by full path, keeping
  the first 300 and appending `… N more` where N = total − 300, as a **pre-joined string** with
  one path per line (like `diff`, so `_as_text` passes it through; the template says so),
  `(none)` when empty or on `git.GitError`/`OSError`; it is added to `PLACEHOLDERS`
  (`personas.py`). It is computed inside the existing `try` that computes `diff` and `test_files`
  (a failure there leaves all three unset; accepted, as today), for every reviewer persona (the
  conformance template ignores it). `test_files` stays unchanged.
- **Inputs:** `spec_text` (for intent only), `diff` (≤ 1500 lines), `changed_files` (paths;
  the reviewer reads full files for context, including callers of changed functions),
  `gate_results`, `previous_verdicts`, `pending_brief`.
- **Categories** (closed set, validated by the schema enum): `bug`, `logic`, `security`,
  `injection`, `path_traversal`, `subprocess`, `secrets`, `concurrency`, `error_handling`,
  `data_loss`, `resource_leak`, `other`. Checklist in the template: shell strings built from
  input, `shell=True`, unvalidated paths joined to a root, tempfile/permission handling,
  secrets or tokens logged or committed, unlocked read-modify-write of shared files, time-of-check
  vs time-of-use, exceptions swallowed or turned into success, non-atomic writes, destructive
  git/file operations without guard, unbounded loops/reads.
- **Severity guide:** exploitable security flaw, data loss or a certain crash on the main path
  ⇒ `must_fix`; plausible defect with a concrete trigger on a realistic path ⇒ `major`;
  hardening / unlikely trigger ⇒ `minor`; style ⇒ `nit`.
- **Verdict schema** `ADVERSARIAL_REVIEWER_SCHEMA` (in `personas.py`, beside
  `CONFORMANCE_REVIEWER_SCHEMA`): `{verdict: pass|fix, score: 0..100, findings: [{severity:
  must_fix|major|minor|nit, category, title, detail, trigger, files, lines}], fix_brief}`;
  `score` = confidence the change is safe to merge.
  - Required: top level `verdict`, `score`, `findings`, `fix_brief` (as the conformance schema);
    per finding `severity`, `category` and `title`.
  - `category` is a closed enum; a missing or unknown one makes the verdict malformed, which
    uses the single retry (deliberate: categories stay groupable).
  - `trigger` is an optional string; `lines` an optional array of `"<file>:<line>"` strings.
  - "A finding without a trigger is at most `minor`" is a **prompt rule only**, not enforced in
    code (accepted: a trigger-less false positive can cost implementer loops up to `loop_max`;
    the template states the rule and a test asserts the template contains it).
  - `pass` with a `must_fix` is `fix` (as SPEC-0187).
- **Ledger and brief, as separately testable rules:**
  1. `finding` events from this persona carry `category` (and keep `gate: null`, `ac: null`).
     `_record_run` (`queue.py`) builds reviewer `finding` events from a fixed key set (`ac`,
     `title`, `detail`, `files`); this spec adds `category`, copied when the finding has one.
  2. One helper `finding_detail(f)` (`queue.py`) returns `f.detail` with the trigger folded in:
     `"<detail>\ntrigger: <trigger>"` when both exist, `"trigger: <trigger>"` when only the
     trigger exists, `detail` otherwise. It is used for the event `detail` (rule 1) and for the
     `detail` of every `pending_brief.must_fix` entry (built where SPEC-0201/0203 build the
     brief), so the ledger and the brief cannot disagree.
  3. `pending_brief.must_fix` entries also carry `category` when present, and
     `render_pending_brief` (`personas.py`) prints the bracket as `[gate|ac|category]` (first
     one present) — e.g. `- MUST FIX [path_traversal]: <title> (<files>) — <detail>` — so the
     implementer reads the category and the trigger in the `From: review` brief.
  4. `lines` and the separate `trigger` key stay only in `last_review.verdicts[
     adversarial_reviewer].findings` (SPEC-0201 persists the full dicts) and in `run.json`.
  5. `schemas.py` is unchanged (`finding` requires no `category`); no new event types and no new
     park reasons (`review_escalated` on malformed twice, as SPEC-0201).
- **Profiles:** `personas.adversarial_reviewer` — `balanced`: opus, effort high,
  `max_budget_usd` 1.5; `thorough`: opus, high, 2.5; `frugal`: sonnet, medium, 0.6 (present so a
  user can opt in via `reviewers`, but not listed by default). Default `reviewers` for
  `balanced`/`thorough` gain `adversarial_reviewer` at its canonical position, last (canonical
  order `conformance_reviewer`, `test_reviewer`, `adversarial_reviewer`, defined jointly with
  SPEC-0205 as the tuple `REVIEWER_ORDER` in `profiles.py`; there is no dependency between the
  two specs, either may ship first),
  so the list is `[conformance_reviewer, adversarial_reviewer]` before SPEC-0205 ships and
  `[conformance_reviewer, test_reviewer, adversarial_reviewer]` after; `frugal` stays
  conformance-only. The persona is appended to `REVIEWER_PERSONAS`.
- **Cost impact:** `balanced` review passes sum 1.5 + 1.5 = 3.0 (4.0 with SPEC-0205's 1.0)
  beside the 4.0 implementer budget and the 10.0 `campaign_cap_usd`; `thorough` 1.5 + 2.5 (+ 1.0)
  against 30.0. The SPEC-0201 cap check uses spent money plus pending budgets, so the first
  review always fits and a second loop parks `cost_cap` earlier than with one reviewer;
  `campaign_cap_usd` defaults are deliberately not changed.
- **Docs:** capability page "Reviews" lists the shipped reviewers and this reviewer's categories.

## Alternatives considered

- **Fold the security checklist into the conformance reviewer** — rejected: that reviewer is
  anchored on the spec and would skim past defects outside it.
- **Run a static analyzer instead** — complementary, belongs in gates; it cannot judge
  logic or intent.
- **Free-form categories** — rejected: a closed enum lets triage (SPEC-0203) and the follow-up
  filing group and route findings.

## Acceptance criteria

- [ ] AC1. `adversarial_reviewer` template (containing the checklist, the severity guide and the
      trigger rule), `ADVERSARIAL_REVIEWER_SCHEMA` (exact required keys, category enum,
      optional `trigger`/`lines`) and `PERSONAS`/`REVIEWER_PERSONAS` entries exist;
      `build_request` is read-only, uses the profile model and budget, and its prompt is
      deterministic (two builds equal).
- [ ] AC2. A verdict whose finding lacks `category` or has an unknown one is malformed
      (`verdict_ok` false) and goes through the SPEC-0201 retry.
- [ ] AC3. Profiles define its settings as above; `balanced` and `thorough` default `reviewers`
      include it last (membership and relative order against `REVIEWER_ORDER`, not exact
      equality), `frugal` does not; a user delta adding it to `frugal` passes `resolve_profile`.
- [ ] AC4. `changed_files` is a context placeholder: sorted, first 300 kept, `… N more` with N =
      total − 300, `(none)` when empty or on `GitError`.
- [ ] AC5. `finding` events carry `persona: adversarial_reviewer` and `category`, the trigger
      folded into `detail` by `finding_detail` (all three cases); `ledger_problems` is empty for
      them; the rendered `From: review` brief for an adversarial `must_fix` contains the
      category bracket, the files and the trigger text.
- [ ] AC6. With the SPEC-0201 fan-out and FakeRunner: an adversarial `must_fix` (e.g. category
      `path_traversal`) with the other reviewers passing ⇒ `IMPLEMENTING` with a brief labelled
      `[adversarial_reviewer]`; the same as `verdict: pass` carrying that `must_fix` also routes
      `fix`; then all pass ⇒ `MERGED`; malformed twice ⇒ `PARKED(review_escalated)` naming the
      persona.
- [ ] AC7. The cost-cap sum includes its budget (a cap that fits the other configured reviewers
      but not them plus this one parks `cost_cap` before any spawn).
- [ ] AC8. `docs/product/capabilities/agentic.md` lists the shipped reviewers and this
      reviewer's categories (grep assertion); full `uv run pytest` green;
      `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_personas.py` — AC1 (template incl. the trigger rule,
      schema enum and required keys, request read-only/model/budget per profile, deterministic
      prompt), AC2 (`verdict_ok` false without category), AC4 (`test_changed_files_*`),
      `render_pending_brief` bracket (AC5); `tests/test_agentic_profiles.py` — AC3 (list
      order by `REVIEWER_ORDER`, frugal opt-in) and the AC8 docs grep;
      `tests/test_agentic_queue.py` — AC5 (`finding_detail` three cases, event mapping with
      `category`, brief entries) and AC7 (cap sum with this reviewer added);
      `tests/test_agentic_logs.py` — AC5 ledger validity of a reviewer `finding` with
      `category`; `tests/test_agentic_e2e.py` — AC2 retry and AC6 paths.
- **Manual verification:** the next real campaign records this reviewer's verdict and cost here.
- **Regression guard:** full `uv run pytest`.

## Rollout / migration

Existing `balanced`/`thorough` campaigns gain an additional reviewer on their next `REVIEWING`
(about $1.5 more per loop); users who want the old cost set `reviewers` in their profile delta.
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
