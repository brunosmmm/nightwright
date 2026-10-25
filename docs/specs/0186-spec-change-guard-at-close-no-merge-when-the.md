---
id: SPEC-0186
title: "Spec-change guard at close: no merge when the implementer altered the spec"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, close, safety, review]
source_idea: IDEA-051
depends_on: [SPEC-0175, SPEC-0176]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-051`).

The close step (SPEC-0175) audits the spec's Definition of Done mechanically: `status: done`,
every Acceptance criterion box ticked, a real Test plan. The implementer persona edits that same
spec file inside its worktree — it is told to set `in-progress`/`done`, tick true ACs and add a
test record. Nothing stops it from also **rewording, weakening or deleting** an acceptance
criterion, or rewriting the Decision, so that the audit passes on a spec that no longer says
what was accepted. The queue already records `spec.sha256_at_open` when it opens a campaign
(SPEC-0176), but nothing ever compares it. Found on 2026-10-25 while the user asked how
conformance is checked; with wt's own merge policy being merge-to-main, this is a direct path
for an unreviewed change of intent into the live tool.

## Goals / Non-goals

**Goals**
- At open, keep the spec text as accepted (`<campaign dir>/spec_at_open.md`), alongside the
  existing hash.
- At close, before anything else, compare the worktree's spec with that copy. Allowed changes:
  - checkbox ticks (`- [ ]` → `- [x]`) anywhere;
  - the frontmatter `status:` and `updated:` values;
  - `### Test record …` and `### As shipped …` subsections (added or edited);
  - the `## Clock Log` section.
  Everything else — other frontmatter keys, Context, Goals, Decision, Design, Alternatives,
  Acceptance criteria text, Test plan, Rollout, Definition of done text — is **drift**.
- Drift routes the campaign to `TRIAGING` with a synthetic failed gate `spec-changed` whose tail
  is a unified diff of the disallowed changes, plus a `finding(must_fix)`. It never merges.
- A pure, separately tested function `spec_drift(old_text, new_text) -> list[str]` (hunks).

**Non-goals**
- Judging whether the implementation meets the ACs (SPEC-0187, conformance review).
- Blocking legitimate design learning: an implementer that discovers the design must change
  records it under `### As shipped`, or stops with `blocked`; changing the governing text is a
  human/steward decision via a superseding spec (AGENTS.md).
- Campaigns opened before this spec (no `spec_at_open.md`): the guard is skipped with an
  `info` finding.

## Decision

Add `spec_drift` to `src/wt/agentic/close.py` (normalize both texts, then diff), make
`queue._do_open` save `spec_at_open.md` in the campaign directory, and run the guard as the
first step of `close_campaign`, returning `TRIAGING` with a `spec-changed` synthetic gate on
drift — the same routing the existing `spec-dod` failure uses, so the queue needs no new state.

## Design

- **Normalization** (`_normalize_spec(text) -> str`):
  1. split frontmatter (`specmeta.split_frontmatter`); drop `status` and `updated`; re-serialize
     the rest with `yaml.safe_dump(sort_keys=True)` so key order and quoting don't matter;
  2. in the body, remove every `### Test record…` / `### As shipped…` subsection (its heading
     through the line before the next heading of level ≤ 3) and the whole `## Clock Log`
     section (through the next `## ` heading);
  3. rewrite `- [x]` / `- [X]` to `- [ ]`;
  4. strip trailing whitespace per line and collapse runs of blank lines.
- `spec_drift(old, new) -> list[str]`: `difflib.unified_diff` of the two normalized texts
  (`n=1`), grouped into hunks; empty list ⇔ no drift.
- **Open:** `queue._do_open` writes the worktree spec text to `store.campaign_dir(cfg, id) /
  "spec_at_open.md"` right after computing `sha256_at_open`.
- **Close** (`close_campaign`, new step 0, before the DoD check): read `spec_at_open.md`; if
  missing ⇒ append `finding(info, "spec-changed guard skipped: no open-time copy")` and
  continue; else `hunks = spec_drift(old, new)`; non-empty ⇒ return
  `CloseOutcome("TRIAGING", synthetic_gate={"name": "spec-changed", "required": True,
  "ok": False, "exit_code": None, "timed_out": False, "duration_s": 0.0, "log": "",
  "tail": <diff, ≤ 120 lines>}, events=[finding(must_fix, gate "spec-changed", title "spec
  text changed outside allowed sections", detail <diff>, files [spec rel_path])])`.
- The gate triager sees `spec-changed` in `last_gate.results` and briefs the implementer to
  restore the accepted text (or to report `blocked` if the design truly must change).

## Alternatives considered

- **Compare the whole file by hash** — rejected; the implementer must tick boxes, set status
  and add a test record, so a hash always differs.
- **Forbid the implementer from editing the spec at all** — rejected; AGENTS.md makes the
  implementer responsible for ticking ACs and recording the test run.
- **Park instead of triage** — rejected for wave 1; restoring the accepted text is usually a
  mechanical fix the next implementer run can do, and `loop_max` bounds it.

## Acceptance criteria

- [x] `spec_drift` returns no hunks for: ticking boxes, changing `status`/`updated`, adding or
      editing `### Test record` / `### As shipped` subsections, editing `## Clock Log`.
- [x] `spec_drift` returns hunks for: rewording or deleting an AC, adding an AC, editing
      Decision / Design / Test plan text, changing another frontmatter key (e.g. `depends_on`).
- [x] `_do_open` writes `spec_at_open.md` with the worktree spec text.
- [x] `close_campaign` with drift returns `TRIAGING` with the `spec-changed` synthetic gate and a
      `must_fix` finding, before any rebase or merge; with no copy it adds an `info` finding and
      proceeds.
- [x] End to end with the fake runner: an implementer run that deletes an AC and marks the spec
      done never reaches `MERGED`; the ledger shows the `spec-changed` finding; main is unchanged.
- [x] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_spec_drift.py` — parametrized allowed / forbidden
  cases over a fixture spec; `close_campaign` drift and no-copy paths on a tmp repo (reusing the
  SPEC-0175 fixtures); an e2e through the queue with `FakeRunner` (implementer deletes an AC and
  sets `done`; triager escalates) asserting the campaign ends `PARKED`, never `MERGED`, and main's
  HEAD is unchanged; `_do_open` writes the copy.
- **Manual verification:** none beyond the suite (the next real campaign exercises the guard).
- **Regression guard:** full `uv run pytest`.

### Test record (2026-10-25)

- `tests/test_agentic_spec_drift.py`: 17 tests — 6 allowed-edit cases (status, tick, updated,
  Test record, Clock Log, all combined) report no drift; 8 governing-edit cases (delete / reword
  / add an AC, Decision, Design, Test plan, a frontmatter key, allowed edits plus a deleted AC)
  report hunks; `spec_guard` without a copy ⇒ `info` finding and proceed; with drift ⇒
  `spec-changed` synthetic gate + `must_fix` finding, and no events for allowed-only edits;
  end to end with the fake runner, an implementer that deletes the only AC and marks the spec
  done goes GATING → CLOSING → TRIAGING → PARKED, never MERGED, main HEAD unchanged,
  `spec_at_open.md` holds the accepted text, the synthetic gate is in `last_gate`.
- Mutation check: with `spec_drift` short-circuited to no hunks, the guard and e2e tests fail.
- `tests/test_agentic_close.py` fixture now carries the open-time copy (as every new campaign
  does); all 19 close tests and the queue / e2e suites pass unchanged otherwise.
- Full `uv run pytest`: 1524 passed, 1 skipped, 0 failed (after the ledger regeneration).

### As shipped

- `_normalize_spec` re-serializes frontmatter minus `status`/`updated` with sorted keys, drops
  `### Test record…` / `### As shipped…` subsections and the `## Clock Log` section, and
  un-ticks checkboxes; `spec_drift` diffs the normalized texts (`n=1`) into `@@` hunks.
- `spec_guard(cfg, state)` runs as step 0 of `close_campaign`; the drift diff is capped at 120
  lines in the gate tail and finding detail.

## Clock Log

Internal work: clock on IDEA-051 with `wt idea clock-in` / `clock-out`.

## Rollout / migration

Campaigns opened after the merge get the guard; in-flight older campaigns skip it with an info
finding. The running loop must be restarted to load the new code.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
