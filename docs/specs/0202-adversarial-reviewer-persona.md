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
test ([SPEC-0201](./0201-test-reviewer-persona.md)) reviewers both judge the change against the
spec. Neither is asked to attack the code itself: logic errors the spec does not mention,
injection or path traversal, unsafe subprocess or shell use, leaked secrets, races, swallowed
errors, or operations that can lose data. This repo shells out to `git`, `claude` and gate
commands and writes user data under the data dir, so such defects matter. This spec adds the
`adversarial_reviewer`, the third member of the multi-reviewer `REVIEWING` fan-out defined in
SPEC-0201 (see its "Shared: multi-reviewer REVIEWING" section, which this spec relies on and does
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
  `permission_mode: dontAsk`, `--restricted`. It is told to assume the code is wrong until shown
  otherwise and to **try to break it by reading**: for each changed hunk, ask what input,
  ordering, timing or environment makes it misbehave. It must cite file and line for each
  finding and state a concrete trigger ("when `name` contains `../`…"); a finding without a
  trigger is at most `minor`. It does not re-judge spec conformance or test coverage (other
  reviewers do) and never edits files.
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
- **Verdict schema** `ADVERSARIAL_REVIEWER_SCHEMA`: `{verdict: pass|fix, score: 0..100,
  findings: [{severity: must_fix|major|minor|nit, category, title, detail, trigger, files,
  lines}], fix_brief}`; `score` = confidence the change is safe to merge. `pass` with a
  `must_fix` is `fix`.
- **Ledger:** `finding` events from this persona carry `category` (and keep `gate: null`,
  `ac: null`); `_events_for_result` copies `category` through for any reviewer finding that has it.
  No new event types, no new park reasons (`review_escalated` on malformed twice, as SPEC-0201).
- **Profiles:** `personas.adversarial_reviewer` — `balanced`: opus, effort high,
  `max_budget_usd` 1.5; `thorough`: opus, high, 2.5; `frugal`: sonnet, medium, 0.6 (present so a
  user can opt in via `reviewers`, but not listed by default). Default `reviewers` for
  `balanced`/`thorough` gain `adversarial_reviewer`; `frugal` stays conformance-only.
- **Docs:** capability page "Reviews" lists the three reviewers and their categories.

## Alternatives considered

- **Fold the security checklist into the conformance reviewer** — rejected: that reviewer is
  anchored on the spec and would skim past defects outside it.
- **Run a static analyzer instead** — complementary, belongs in gates; it cannot judge
  logic or intent.
- **Free-form categories** — rejected: a closed enum lets triage (SPEC-0203) and the follow-up
  filing group and route findings.

## Acceptance criteria

- [ ] `adversarial_reviewer` template, `ADVERSARIAL_REVIEWER_SCHEMA` (category enum, trigger),
      and `PERSONAS` entry exist; `build_request` is read-only, uses the profile model and
      budget, and its prompt is deterministic.
- [ ] Profiles define its settings as above; `balanced` and `thorough` default `reviewers`
      include it, `frugal` does not; a user delta can add it to `frugal`.
- [ ] Its findings become `finding` ledger events with `persona: adversarial_reviewer`
      and `category`; `schemas.py` accepts them (required keys unchanged) and
      `ledger_problems` is empty for them.
- [ ] With the SPEC-0201 fan-out and FakeRunner: an adversarial `must_fix` (e.g. category
      `path_traversal`) with the other reviewers passing ⇒ `IMPLEMENTING` with a brief labelled
      `[adversarial_reviewer]`; then all pass ⇒ `MERGED`; malformed twice ⇒
      `PARKED(review_escalated)` naming the persona.
- [ ] The cost-cap sum includes its budget (a cap that fits two reviewers but not three parks
      `cost_cap` before any spawn).
- [ ] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_personas.py` (template, schema enum, request
  read-only/model/budget per profile, deterministic prompt); `tests/test_agentic_profiles.py`
  (default lists, frugal opt-in); `tests/test_agentic_queue.py` (event mapping with
  `category`, cap sum with three reviewers); `tests/test_agentic_logs.py` (ledger validity of
  a reviewer `finding` with `category`); `tests/test_agentic_e2e.py` (the scripted paths above).
- **Manual verification:** the next real campaign records this reviewer's verdict and cost here.
- **Regression guard:** full `uv run pytest`.

## Rollout / migration

Existing `balanced`/`thorough` campaigns gain a third reviewer on their next `REVIEWING`
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
