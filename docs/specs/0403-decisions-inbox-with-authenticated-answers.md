---
id: SPEC-0403
title: "Decisions inbox payload: full escalation entries, effects and host fixes in nw.dashboard.v1"
status: proposed
owner: user
created: 2026-11-03
updated: 2026-11-04
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0400
source_idea: IDEA-875
tags: [agentic, dashboard, remote, escalation, conductor]
depends_on: [SPEC-0232, SPEC-0235, SPEC-0401, SPEC-0414]
---

## Context

Child 2 of the epic [SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md),
promoted from `IDEA-875`. The first draft of this spec covered the whole decisions inbox: the
payload, the phone view and the authenticated write path. Review found that too large for one
campaign: 15 acceptance criteria over two subsystems. It is split into two specs, in order:

1. **This spec: the read-only inbox payload.** Every open escalation, with everything the
   human needs to decide, and every human-owned park with the fix to run, inside
   `nw.dashboard.v1`, and shown by `nw dashboard snapshot`.
2. **Authenticated answers from the dashboard** (a later sibling spec that depends on this one
   and on the dashboard server spec): `GET /api/session`, `POST /api/escalations/<id>/plan`
   and `/answer` through `plan_answer`/`apply_answer` with `by="human"`, the plan token,
   CSRF, conflict mapping (404/409/400 and a 500 path), `audit.py` with tailnet login and
   device, the answer controls and confirm sheet, and the twin-equality and live-follower e2e.

This spec depends on [SPEC-0401](./0401-read-only-dashboard-server-and-nw-dashboard-v1.md)
(the first slice of epic child 1, `done`). It uses only what SPEC-0401 defines and ships:

- `agentic/dashboard/snapshot.py`: `build(cfg, now=None)`, which calls
  `queue.dashboard_payload` once per build and fills `inbox: {escalations, parks}`; the
  `_safe` / `_dp_section` error isolation (a section that raises becomes
  `{"error": "<Type>: <message>"}`); `compute_rev`; and `SnapshotCache`, whose `watermark`
  includes `escalations.signature(cfg)` and the campaign signature (`state.json`,
  `ledger.jsonl`), so the inbox is rebuilt when a record or a campaign changes.
- `agentic/cli_dashboard.py`: `nw dashboard snapshot [--json]`, whose text form prints one line
  per open escalation and per human-owned park, or `nothing needs you`.
- Today `inbox.escalations` holds only summary fields (`id, created, by, kind, question` (first
  line), `campaign, spec_id, project`), and `inbox.parks` holds the `needs_you` rows whose park
  reason is `main_dirty` or `main_not_on_base`. SPEC-0401 says that child 2 extends both.
- Its tests `tests/test_dashboard_snapshot.py`, `test_dashboard_cli.py`,
  `test_dashboard_readonly.py` and `test_dashboard_imports.py`.

The SPEC-0401 sibling specs for the dashboard server (`nw serve --dashboard`, auth, routes)
and the dashboard page (static UI, `?selftest=1`, headless Chromium) have not been written
yet. This spec therefore uses no server, route, page or browser interface.

What already exists in `agentic/escalations.py` (checked against the code on 2026-11-04):

- `list_all(cfg, "open")` lists open records. `signature(cfg)` is the cheap stat signature
  (moved there by SPEC-0401).
- `answerable_findings(cfg, state, esc_id=None)` returns the waivable findings (`blocking` +
  `advisory` of the stored review, minus what `human_overrides` already settled). With
  `esc_id` it ignores that escalation's own half-written overrides (SPEC-0286).
- The unmet ACs are the second element of the private `_review_findings(cfg, st)`: the
  distinct `ac` texts of the `met: false` `ac_assessment` entries. They have no public
  accessor.
- `plan_answer` accepts `choose` only for a record with options. It accepts `waive`/`attest`
  only for a record with a campaign, and checks them against `answerable_findings` and the
  unmet ACs. It refuses a campaign that is not PARKED `human_decision`, or that only has a
  `park_requested`.
- Records of kind `worker_lost` (SPEC-0342) are applied by the queue itself
  (`workers.apply_answered_escalations`), not by the conductor.

`queue.dashboard_payload` puts parks whose owner is `human` (`main_dirty`, `main_not_on_base`,
`human_decision`) in `needs_you`. Each row carries `campaign`, `spec_id`, `spec_title`,
`project`, `state` and a `park` dict (`reason`, `owner`, `detail`, `parked_at`, …). The
`nw-conduct` row `escalation_answered` (`skills/nw-conduct/SKILL.md`) says what the conductor
does per answer kind:
- `choose`: it does what the option says.
- `override`: it resumes `--to REVIEWING`, and the fresh review triages with the overrides.
- `text`: it acts on the text, or escalates again.

## Goals / Non-goals

**Goals**
- G1. Each `inbox.escalations` entry in `nw.dashboard.v1` carries everything the human needs
  to decide: the full question, the options, the recommendation, the facts, the campaign and
  park context, the waivable findings, the unmet ACs, the answer forms that apply, whether
  the record can be answered now, and a fixed text per form saying what happens next.
- G2. `inbox.parks` lists every human-owned park that has no open escalation, read-only, with
  the exact fix to run on the host. This includes a `human_decision` park whose answer is
  given but not yet applied by the conductor.
- G3. The payload is built read-only from the existing builders. The data for the waivable
  findings and unmet ACs is the data `plan_answer` validates against, so what the inbox offers
  is what an answer may use.
- G4. `nw dashboard snapshot` shows the richer inbox on the host or over SSH today, before any
  server exists.

**Non-goals**
- **The write path** (the sibling spec in Context, item 2): `GET /api/session`, the
  `/plan` and `/answer` routes, the plan token, CSRF, body limits, the 404/409/400 mapping by
  re-reading, the 500 response for a non-`ValueError` from `apply_answer` (with `result: 500`
  in the audit enum and a test that makes `store.append_event` raise `OSError`), `audit.py`,
  and the twin-equality and live-follower e2e. That spec must define each of these.
- **The inbox view on the phone page** (cards, answer controls, the confirm sheet, park cards
  with a copyable fix, and the 390×844 / 360×740 self-test). The read-only cards go in the
  dashboard page sibling spec of SPEC-0401, or in a later spec that depends on it. The answer
  controls and their 44 px check go in the write-path spec. A page needs a server and a page
  shell, and neither exists yet. Putting either one here would make this spec depend on specs
  that have not been written.
- Live push over SSE (child 3) and phone notifications (child 5).
- Withdrawing escalations, sending mailbox messages, writing events, or resuming, parking or
  abandoning campaigns (epic non-goals).
- Acting on `main_dirty` / `main_not_on_base` parks. They are shown with the host fix only.
- Updating the epic's Breakdown line for child 2. This campaign edits only this file. The
  epic's next revision records the split.

## Decision

1. **A new module `agentic/dashboard/inbox.py` builds `inbox`.** It exposes
   `build_inbox(cfg, needs_you_rows) -> {"escalations": [...], "parks": [...]}`.
   `snapshot.build` calls it in place of its `_escalations` helper and its `parks` lambda,
   and passes the `needs_you` rows of the `dashboard_payload` it already computed, so there
   is no second `dashboard_payload` call. If `dashboard_payload` failed, `parks` is its
   error, as today, and `escalations` still builds.
2. **New public `escalations.unmet_acs(cfg, state, esc_id=None) -> list[str]`.** It is a thin
   wrapper over `_review_findings(...)[1]`, with the same `esc_id` handling as
   `answerable_findings`. It exists so that the inbox, the CLI and the TUI read one
   implementation (epic invariant 5). `escalations` still imports nothing from
   `agentic.dashboard`.
3. **The fields are additive and the SPEC-0401 summary fields are kept.** `question` keeps
   SPEC-0401's meaning (the first line), and the full text is the new `question_text`, so
   `nw dashboard snapshot` and SPEC-0401's tests keep their meaning. Every other field is new.
4. **`forms` and `answerable` mirror `plan_answer`'s rules,** so the inbox never offers an
   answer that `plan_answer` would refuse for a structural reason. The rules are written once
   in `inbox.py`, and a test checks them against `plan_answer` on the seeded records.
5. **The effects and host fixes are fixed texts.** `EFFECTS` (per answer form) and `HOST_FIX`
   (per park reason) are module constants, so they are reviewable and testable. A drift guard
   ties `EFFECTS` to the `nw-conduct` `escalation_answered` row.
6. **Parks widen from "two reasons" to "no open escalation".** `inbox.parks` becomes every
   `needs_you` row with no open escalation for its campaign. For `main_dirty` /
   `main_not_on_base` this is what SPEC-0401 lists today. The addition is a `human_decision`
   park whose latest escalation is already answered or withdrawn. It is still waiting on
   someone, and the inbox says on whom. SPEC-0401 named this extension ("the parks with their
   host fix").
7. **Per-entry error isolation.** When one entry fails to build, for example because a
   campaign's `state.json` is unreadable, that entry carries the SPEC-0401 summary fields plus
   `error: "<Type>: <message>"` and `answerable: false`, and the rest of the inbox still
   builds.

## Design

### Escalation entries

`build_inbox` reads `list_all(cfg, "open")` and sorts it by `created` ascending (ties broken
by `id`). For each record it reads the campaign state once (`store.read_state`) when the record
has a campaign, and it reads the spec title from the campaign state or the `needs_you` row.

```
{id, created, by, kind, question, question_text, options: [{n, label}], recommendation,
 facts: [str], campaign, spec_id, spec_title, project,
 park: {reason, escalated_from, resume_to, parked_at, detail} | null,
 answerable: bool, pending: bool, why_not: str | null,
 forms: ["choose"?, "text", "override"?],
 findings: [{id, personas, severity, title, detail, files, synthetic}], unmet_acs: [str],
 effects: {choose?: str, text: str, override?: str}}
```

- `options` are the record's options as stored (`n` 1-based, `label`), and `[]` when there are
  none. `facts` is a list of strings: a stored string becomes one item, and a stored list is
  kept as it is.
- `park` comes from the campaign state's `park`. It is null for a campaign-less record, and
  null when the campaign is not PARKED.
- `pending` is true when the campaign has a `park_requested` and is not PARKED yet.
- `answerable` is true exactly when the record has no campaign, or its campaign is PARKED with
  park reason `human_decision`. Otherwise `why_not` says why, with one of three fixed texts:
  - `"not parked yet"`, when `pending` is true;
  - `"campaign is <STATE>"`, when the campaign is in any other state;
  - `"campaign state unreadable"`, when the state could not be read.
- `findings` is `answerable_findings(cfg, state, esc_id)`, projected onto `id, personas,
  severity, title, detail, files, synthetic` (the keys of `queue.triage_reviews`' finding
  records), with `title` and `detail` (null for synthetic findings) each cut to 300
  characters (an ellipsis marks a cut). The `id`s are kept exactly, because `--waive` matches on them.
- `unmet_acs` is `escalations.unmet_acs(cfg, state, esc_id)`, as full texts, because
  `--attest` matches on them.
- `findings` and `unmet_acs` are both `[]` for a campaign-less record.
- `forms`:
  - `choose` appears only when `options` is non-empty;
  - `text` always appears;
  - `override` appears only when the record has a campaign and at least one finding or unmet
    AC.
- `effects` is `EFFECTS` filtered to `forms`:
  - `choose`: "The conductor does what the chosen option says (resume, abandon, prioritize or
    raise the cap) and tells you in one line."
  - `text`: "The conductor acts on your text by its decision table; if the text is not
    actionable, it asks again with a new question."
  - `override`: "The conductor resumes the campaign to REVIEWING; reviewers are told not to
    re-raise the waived findings or the attested ACs."
  - For `kind == "worker_lost"`, every form's effect is `EFFECTS_WORKER_LOST`: "The queue
    applies this answer itself (SPEC-0342); the conductor is not involved."

### Park entries

A park entry is a `needs_you` row whose `campaign` has no open escalation. The row is kept as
`dashboard_payload` built it, so every SPEC-0401 field stays. Two keys are added:

- `inbox_state`:
  - `awaiting_conductor` when the park reason is `human_decision` and the campaign's latest
    escalation (by `created`) is `answered`;
  - `needs_decision` when the park reason is `human_decision` and the latest escalation is
    `withdrawn` or missing. The conductor is expected to re-escalate or resume.
  - `needs_host` otherwise.
- `fix`: the `HOST_FIX` text for the reason, with `<cid>` and `<root>` filled in. `<root>` is
  the campaign state's `repo.root`. When it is missing, `<root>` becomes `the project repo`.
  - `main_dirty`: "On the host: move the uncommitted changes in `<root>` off main (commit
    them on a branch), then `nw queue resume <cid>`."
  - `main_not_on_base`: "On the host: move any work off the current branch of `<root>`,
    switch it back to its base branch, then `nw queue resume <cid>`."
  - `human_decision` / `awaiting_conductor`: "Answered; the conductor applies it on its next
    wake (`nw conduct-status` on the host if it does not)."
  - `human_decision` / `needs_decision`: "No open question; the conductor re-escalates or
    resumes it (`nw campaign log <cid> --tail 20` on the host)."
  - Any other reason: "On the host: `nw campaign log <cid> --tail 20`."

The latest escalation per campaign comes from one `list_all(cfg)` call per build, grouped by
campaign. It is not read once per row.

### `escalations.unmet_acs`

```python
def unmet_acs(cfg, state: dict, esc_id=None) -> list[str]:
    if esc_id is not None:
        state = _without_overrides_of(state, esc_id)
    return _review_findings(cfg, state)[1]
```

The order is that of `_review_findings`: the first occurrence across the stored verdicts.

### `nw dashboard snapshot` text form

The text form stays one line per inbox item, and the lines carry the new facts:
- An escalation prints `ESC-NNNN <campaign|-> <question> [<forms joined by />]`. A
  non-answerable entry prints `(<why_not>)` in place of the forms, and an entry with `error`
  prints `(error: <message>)`.
- A park prints `<cid> <reason> <inbox_state>: <fix>`.

`--json` is unchanged apart from the richer `inbox`. The command still exits 0 when a section
or entry has an error.

### Module boundaries and invariants

- New: `agentic/dashboard/inbox.py` (`build_inbox`, `EFFECTS`, `EFFECTS_WORKER_LOST`,
  `HOST_FIX`, `FINDING_TEXT_MAX = 300`).
- Changed: `snapshot.build` (calls `build_inbox`; drops `_escalations` and the `parks` lambda),
  `cli_dashboard.py` (the text form), and `escalations.py` (gains only `unmet_acs`).
- `build_inbox` is read-only. It calls no `events.sync`, takes no lock and writes nothing.
  SPEC-0401's read-only file-hash test keeps covering `build`, and so now covers the inbox.
- SPEC-0401's import-graph test (`tests/test_dashboard_imports.py`) keeps pinning that
  `escalations` and the queue modules do not import `agentic.dashboard`. `inbox.py` imports
  `escalations`, `store` and nothing from the server side.
- Cost: one `list_all(cfg)`, plus one state read and one `_review_findings` per open
  escalation with a campaign, per build. Builds happen only when SPEC-0401's watermark moved or
  `max_stale_s` passed.

## Alternatives considered

- **Keep the whole inbox (payload, page cards, write path) in one spec.** Lost: 15 criteria
  over two subsystems. The payload is useful alone through `nw dashboard snapshot`, and the
  write path builds on it.
- **Put the read-only inbox cards in this spec.** Lost: the page shell, its `?selftest=1` and
  the headless-Chromium harness belong to SPEC-0401's dashboard page sibling spec, which is
  not written yet. A card test here would depend on an interface that no spec defines.
- **Replace `question` with the full text.** Lost: it changes the meaning of a field that
  SPEC-0401 shipped and its CLI uses. `question_text` is additive.
- **Compute unmet ACs in `inbox.py` from the stored verdicts.** Lost: it would fork
  `_review_findings`, and the inbox could then offer ACs that `--attest` rejects. A public
  wrapper gives one implementation.
- **Leave `human_decision` parks out of `inbox.parks`, as SPEC-0401 does.** Lost: an answered
  park that the conductor has not applied is exactly what the human wants to see while away.
  Showing it as `awaiting_conductor` tells them the decision is not lost.
- **Fail the whole inbox when one record is bad.** Lost: one unreadable campaign would hide
  every other decision. Per-entry errors follow SPEC-0401's per-section rule.

## Acceptance criteria

- [ ] `build(cfg)["inbox"]["escalations"]` has one entry per open escalation, ordered by
      `created` (then `id`). Each entry holds every field listed under Design. `question` is
      the first line and `question_text` the full text. `findings` equals
      `answerable_findings(cfg, state, id)` projected and cut to 300 characters with ids
      intact. `unmet_acs` equals `escalations.unmet_acs(cfg, state, id)`.
- [ ] `forms` follows the Design rules: no `choose` without options, and no `override` for a
      campaign-less record or one with no findings and no unmet ACs. `effects` has exactly
      the keys in `forms`, with the `EFFECTS` texts. A `worker_lost` record's effects are all
      `EFFECTS_WORKER_LOST`.
- [ ] A pending escalation (the campaign has `park_requested` and is not PARKED) has
      `answerable: false, pending: true, why_not: "not parked yet"`. A record whose campaign
      is RUNNING has `answerable: false` and `why_not: "campaign is RUNNING"`. A record whose
      campaign is PARKED `human_decision` has `answerable: true`. On the seeded fixture, every
      `answerable: true` entry with each of its `forms` passes `plan_answer` (valid arguments
      built from the entry's own options, findings and unmet ACs), and `plan_answer` refuses
      every `answerable: false` entry.
- [ ] `inbox.parks` holds exactly the `needs_you` rows with no open escalation for their
      campaign, each with every SPEC-0401 row field plus `inbox_state` and `fix`, both per
      the Design. A `main_dirty` park's `fix` names its repo root and `nw queue resume <cid>`.
      A `human_decision` park whose latest escalation is answered has `inbox_state:
      awaiting_conductor`. One whose latest escalation is withdrawn has `needs_decision`.
- [ ] `escalations.unmet_acs` exists, returns the `met: false` AC texts in first-occurrence
      order, and ignores `esc_id`'s own overrides. Each text it returns is accepted by
      `plan_answer(..., attest=[text], reason=…)`.
- [ ] An open escalation whose campaign state is unreadable yields an entry with the summary
      fields, `error`, and `answerable: false`, and every other entry still builds. When
      `dashboard_payload` raises, `parks` is its error and `escalations` still builds.
- [ ] `build_inbox` and `build` leave every file under the data dir byte-identical (SPEC-0401's
      read-only test, run with the seeded inbox fixture). `escalations` still does not import
      `agentic.dashboard` (SPEC-0401's import-graph test).
- [ ] `nw dashboard snapshot` (real subprocess) prints each escalation with its forms or its
      `why_not`, and each park with its `inbox_state` and `fix`. `--json` carries the full
      inbox, and the command exits 0 when an entry has an error.
- [ ] A drift guard test fails if `EFFECTS["override"]` stops mentioning `REVIEWING`, or if the
      `escalation_answered` row of `skills/nw-conduct/SKILL.md` stops containing
      `--to REVIEWING`, `choose` and `text`.
- [ ] `python3 tools/spec_lint.py` exits 0, and the affected tests pass, including SPEC-0401's
      snapshot, cache, CLI, read-only and import tests, changed only where Decisions 3 and 6
      change their expected output.

## Test plan

- **Automated tests:**
  - `tests/test_dashboard_inbox.py` (new, unit, seeded `tmp_path` data dir). It seeds the
    following:
    - a PARKED `human_decision` campaign with `ESC-0001`, which has options, a recommendation,
      facts as a list, a multi-line question, and a stored review with two blocking findings
      (one with a 400-character detail) and one unmet AC;
    - a pending `ESC-0002` (park requested only);
    - `ESC-0003` on a RUNNING campaign;
    - a campaign-less `ESC-0004` without options;
    - a `worker_lost` record;
    - a `main_dirty` park with `repo.root` set, and a `main_not_on_base` park without it;
    - a `human_decision` park whose escalation is answered, and one whose escalation is
      withdrawn;
    - `ESC-0005`, whose campaign's `state.json` is corrupt.

    It asserts:
    - every entry field and the ordering;
    - `question` vs `question_text`;
    - the finding projection and the 300-character cut, with ids intact;
    - the `forms` and `effects` rules, and the worker_lost wording;
    - the `answerable`, `pending` and `why_not` triples;
    - the `plan_answer` cross-check of the third acceptance criterion (it calls only
      `plan_answer`, which is pure);
    - park `inbox_state` and `fix`, including the `the project repo` fallback;
    - the per-entry error for `ESC-0005`, and the `dashboard_payload`-raises case;
    - that the data dir is byte-identical after `build_inbox`.

    The drift guard against `skills/nw-conduct/SKILL.md` is in this file
    (`test_effects_match_nw_conduct_row`).
  - `tests/test_agentic_escalations.py` (extended):
    - `unmet_acs` returns the `met: false` texts in order, and ignores the given escalation's
      own half-written overrides (the SPEC-0286 case);
    - every returned text is accepted by `plan_answer(attest=[…], reason=…)`.
  - `tests/test_dashboard_snapshot.py` (updated):
    - `inbox.escalations` entries carry the new keys, and `question` is still the first line;
    - the parks assertion is updated only where its fixture has a `human_decision` park with
      no open escalation;
    - `rev` changes when an escalation's options change.
  - `tests/test_dashboard_cli.py` (extended, real `uv run nw dashboard snapshot` subprocess on
    the seeded dir): the text form has the forms or `why_not` per escalation and the `fix` per
    park, and `--json` round-trips the inbox. The command exits 0 with the corrupt-state
    entry present.
  - `tests/test_dashboard_readonly.py` and `tests/test_dashboard_imports.py` (SPEC-0401): run
    unchanged against the new code, with the readonly test's fixture extended by an open
    escalation that has a stored review.
- **Manual verification:** on the host, with at least one real open escalation, run `uv run
  nw dashboard snapshot` and `uv run nw dashboard snapshot --json | jq .inbox`. Check that the
  forms, findings, unmet ACs and park fixes match `nw escalations show <id>` and `nw queue
  watch`, and record the result in this spec when it is done.
- **Regression guard:** run `python3 tools/affected_tests.py --run uv run pytest -q`. It
  covers `tests/test_agentic_escalations*.py`, the TUI escalation inbox tests (SPEC-0250,
  `test_tui_escalations_*`), `tests/test_agentic_dashboard*.py` (`dashboard_payload` /
  `nw queue watch`) and SPEC-0401's dashboard tests, all of which must pass.

## Rollout / migration

1. SPEC-0401 is `done`, so this spec can land as soon as it is accepted. It does not need the
   dashboard server or page specs.
2. No data migration. The escalation, event and state formats are unchanged. `nw.dashboard.v1`
   gains fields, and `inbox.parks` gains the `human_decision` rows with no open escalation.
   Nothing consumes the document outside nightwright yet.
3. The write-path sibling spec follows. It depends on this spec and on the dashboard server
   spec. The epic's Breakdown line for child 2 is updated in the epic's next revision.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed: the automated tests above pass, and the manual `nw dashboard
      snapshot` check is recorded here.
- [ ] No regressions (affected tests at the queue's close gate).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated, and `python3 tools/spec_lint.py` exits 0.

## Open questions

_None._ The 300-character finding cut and the fixed texts are module constants that can be
tuned from use.
