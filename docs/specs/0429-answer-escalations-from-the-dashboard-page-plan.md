---
id: SPEC-0429
title: "Answer escalations from the dashboard page: plan, confirm, answer through plan_answer/apply_answer, 409 and audit"
status: done
owner: user
created: 2026-11-04
updated: 2026-11-04
milestone: "M9: agentic workflow"
source_idea: IDEA-964
kind: feature
parent: SPEC-0400
depends_on: [SPEC-0403, SPEC-0425, SPEC-0428]
tags: [agentic, dashboard, escalation, conductor, web]
---

## Context

Promoted from idea `IDEA-964`. Child of epic [SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md)
(its child 2, "Decisions inbox with authenticated answers"). [SPEC-0403](./0403-decisions-inbox-with-authenticated-answers.md)
shipped only the inbox **payload** (open escalations with options, effects, findings, unmet ACs,
host fixes); the page shows it read-only and the server has no write route.

The human decided on 2026-11-04: each open escalation in the inbox gets its options; choosing one
shows what it will do, then confirm, then a POST. The server applies the answer through exactly
the same code path as `nw escalations answer` with `by: human`; every answer is audited (Tailscale
login and device, or `local`); choose, free text and the override flows are supported; an
escalation already answered, withdrawn or no longer applicable gets `409` with the reason and
nothing is written; SPEC-0425's auto-applied answers are respected (a human answer reverses an
auto answer once, as in the CLI).

The CLI path, checked in the code (`agentic/cli_escalations.py` `escalations_answer`):
`ESC.plan_answer(cfg, id, choose=, text=, waive=, attest=, reason=, note=, by=)` (pure
validation; `AnswerFormError` for a wrong combination, `ValueError` otherwise), then
`ESC.apply_answer(cfg, plan)` (locks, re-checks, ledger `human_decision` + `note`,
`human_overrides`, the record, one `escalation_answered` event; may return `event_error`). A record
`answered` with `answer.auto` is reversible once by `human` (`ESC._reversible`).

## Goals / Non-goals

**Goals**
- G1. Answer an open escalation from the page with choose, text or override (waive / attest +
  reason), plus an optional note, after a confirm step that shows the plan and its effect.
- G2. The written record, ledger events, overrides and `escalation_answered` event equal what
  `nw escalations answer … --yes` writes (timestamps aside).
- G3. Conflicts are clean: `409` with the current status, who and when; nothing written.
- G4. Every plan, answer and refused answer is audited with the caller's identity.
- G5. A recent auto-applied answer can be reversed once from the page.

**Non-goals**
- Withdrawing escalations, resuming/parking/abandoning campaigns, mailbox messages (epic
  non-goals). `--auto` answers (the conductor's).
- Acting on `main_dirty` / `main_not_on_base` parks (they stay read-only with their host fix).

## Decision

1. **Two POST routes**, behind SPEC-0428's checks (Host, identity, JSON content type,
   same-origin, CSRF):
   - `POST /api/escalations/<id>/plan` with body `{form, choose?, text?, waive?, attest?,
     reason?, note?}` → `200 {plan, effect, plan_token, expires_at}`.
   - `POST /api/escalations/<id>/answer` with the same body plus `plan_token` → `200 {record,
     summary, event_error?}`.
   `form` is `choose` | `text` | `override`; a field that does not belong to the form is `400`.
   `<id>` must match `ESC-\d{4,}`. Bodies over 16 KiB are `413`.
2. **The CLI's functions, nothing else.** Both routes call `ESC.plan_answer(cfg, id, …,
   by="human")`; `answer` then calls `ESC.apply_answer(cfg, plan)` and nothing else writes.
   `apply_answer`'s `event_error` is returned, as the CLI warns about it.
3. **Confirm step = plan token.** `plan_token = <exp>.<mac>`, `mac = HMAC(secret, "plan" |
   identity_key | id | created | status | answered_at | canonical JSON of the answer request |
   exp)`, valid 10 minutes, with SPEC-0428's per-process secret. `answer` refuses a token that
   does not match the body or identity (`400 bad_plan_token`) and an expired one (`409
   plan_expired`); a record that changed since the plan (status or `answered_at`) fails the MAC
   and is reported as a conflict (`409`) after the conflict check of Decision 4.
4. **Conflict classification before any write** (both routes): a missing record is `404`. A
   record that is not `open` and not reversible by `human` is `409 {status, by, at, error}` (`by`
   / `at` from `answered_by` / `answered_at`, or `withdrawn.by` / `withdrawn.at`). A campaign-bound
   record whose campaign is not PARKED `human_decision` (and not a reversal) is `409 {status:
   "not_applicable", campaign_state, error}`, including a pending park. A `ValueError` that
   `apply_answer` raises (a race between plan and write) is also `409`, with the record re-read.
   Other `plan_answer` errors (`AnswerFormError`, a bad option, unknown finding ids, an empty
   text) are `400 {error}` with the message verbatim. In every non-200 case nothing is written
   except the audit line.
5. **Audit.** `audit.jsonl` (`nw.dashboard.audit.v1`) gains `action: plan | answer` lines with
   `esc_id`, `form`, `summary`, `result` (`ok` | `409` | `400` | `403` | `404` | `500`), `detail`
   and the identity (`login`, `node`, `node_ip`; `local` on loopback). The escalation record keeps
   `answered_by: "human"`, exactly as from the CLI.
6. **Auto-applied answers.** The inbox gains `auto_applied`: records `answered` with
   `answer.auto` and answered within the last 24 h, each with the same fields as an open entry
   plus `auto_answer` (the auto answer's summary) and `reversible: true`. The page offers the same
   forms on them, labelled "reverse auto answer"; the plan's `reverses` names the answer being
   reversed. The server accepts a reversal of any reversible record (the CLI's rule), the 24 h
   window only bounds what the inbox lists.
7. **The page.** Each open (or auto-applied) escalation shows its question, facts,
   recommendation, one button per option (at least 44 px tall), a text answer box and, when
   `override` is offered, checkboxes for the waivable findings and unmet ACs plus a reason box,
   and an optional note. Any choice calls `plan` and shows a confirm panel with the summary, the
   effect text (from the inbox `effects`, as returned by the server), what it reverses and the
   override's reach; "Confirm" calls `answer`, "Cancel" discards. A `409` shows "already
   answered by X at Y" (or the reason) and refreshes; a `403 bad_csrf` says "reload the page".
   After a success the page refreshes the snapshot at once.

## Design

- `dashboard/answers.py` (new): `parse_request(body) -> (kwargs, canonical)`; `classify(cfg, id)
  -> None | (status_code, body)`; `plan(cfg, id, body, identity, secret, now)`; `answer(cfg, id,
  body, identity, secret, now)`; `PlanToken.mint/check`. All writes go through
  `ESC.apply_answer` only.
- `dashboard/server.py`: the two POST routes; the audit call; `Cache-Control: no-store` on POST
  answers.
- `dashboard/audit.py`: `record()` takes `action`, `esc_id`, `form`, `summary`, `result`.
- `dashboard/inbox.py`: `auto_applied` section (Decision 6).
- `static/app.js` + `app.css`: the inbox controls and confirm panel (Decision 7).

## Alternatives considered

- **Re-implementing the answer write in the dashboard.** Rejected: the epic and the human require
  the CLI's path, byte-for-byte.
- **One POST with a `confirm: true` flag.** Rejected: the plan token makes the confirm step
  verifiable server-side and binds it to the exact record state that was shown.
- **Listing every auto-applied answer forever.** It would clutter the inbox with old answers;
  24 h covers "I was away and the conductor auto-applied", and the CLI keeps the general case.

## Acceptance criteria

- [x] `plan` then `answer` with `choose` from HTTP produces the same escalation record, ledger
      `human_decision` and `note` events, `human_overrides` and `escalation_answered` event data
      as `nw escalations answer ID --choose N --yes` on a copy of the same data dir (timestamps
      aside); the same holds for `text` and for an override (waive + attest + reason) and for a
      note.
- [x] A second answer, an answer to a withdrawn record, and one whose campaign is no longer
      PARKED `human_decision` get `409` with status, by and at (or the reason); the data dir is
      byte-identical apart from `audit.jsonl`.
- [x] `answer` without a plan token, with a token for another body, identity or record, or with
      an expired token is refused (400 / 400 / 409) and writes nothing.
- [x] A form error (`choose` 9 of 2 options, an unknown finding, override without reason, a
      foreign field) is `400` with the `plan_answer` message and writes nothing.
- [x] Every plan, answer and refused answer appends one audit line with action, esc_id, form,
      result and identity (`local` on loopback; login, node, node_ip on the tailnet).
- [x] An auto-applied answer (SPEC-0425) is listed under `inbox.auto_applied` and can be reversed
      once from HTTP; the record gets `answer_history` and the event carries `reverses_auto`,
      as from the CLI; a second reversal is `409`.
- [x] The page renders option buttons, the text box and the override controls for an open
      escalation, shows the confirm panel with the server's summary and effect, posts the answer
      with the CSRF header only after Confirm, and shows the 409 reason.

## Test plan

- **Automated tests**
  - `tests/test_dashboard_answers.py` (new): in-process server on real sockets over a seeded data
    dir with real escalations made by `ESC.plan_escalate`/`apply_escalate` on a PARKED
    `human_decision` campaign: twin checks against `nw escalations answer` run through the real
    CLI (`CliRunner`) on a copy of the data dir for choose, text (+ note), override; conflicts
    (second answer, withdrawn, campaign resumed) → 409 and a byte-identical data dir apart from
    the audit file; plan-token refusal matrix; form errors → 400; the audit lines; auto-applied
    reversal (an escalation auto-answered through `plan_answer(auto=True)` with a due timeout)
    and its second reversal → 409; the `auto_applied` inbox section.
  - `tests/js/page_logic.test.js` / `tests/js/answer.test.js` (new): the page's inbox controls
    against the fake DOM: option click → plan POST with `X-NW-CSRF` and JSON; confirm panel text;
    Confirm → answer POST with the plan token; Cancel posts nothing; 409 message.
- **Manual verification (real processes, recorded in As shipped):** a real `nw serve
  --dashboard` on a free loopback port against a scratch data dir holding escalations created
  with the real CLI (`nw queue escalate …`): plan + answer over curl/python; the record and the
  `escalation_answered` event diffed against a twin data dir answered with `nw escalations
  answer --yes`; a second answer → 409; the page driven in the headless Chromium shell when it
  exists on the host.
- **Regression guard:** affected tests, the read-only guard (`test_dashboard_http_readonly.py`:
  GET routes still write nothing), the import-graph guard, `python3 tools/spec_lint.py`.

## As shipped

Shipped as designed (`dashboard/answers.py`, the two POST routes in `server.py`, `inbox.auto_applied`,
the inbox controls in `app.js`). Details the Design left open:

- The plan response's `plan` is the CLI plan's public view (`id, campaign, by, question, answer,
  summary, reverses, reverses_summary, override{waive[id,severity,title], attest, reason,
  implicitly_attested, reach}`); `effect` is `inbox.effects_of(kind, [form], reverses)`, which
  prefixes "Reverses the auto-applied answer: …" for a reversal.
- A token for another body, identity or record and a garbage token are `400 bad_plan_token`; an
  expired one is `409 plan_expired`. A missing token is 400. A refused write (CSRF, origin,
  content type) is audited as `action: refused` with the `esc_id` and `result`.
- Campaign links in the inbox and the Active list are buttons that set `#/c/<cid>` (SPEC-0430),
  because the page's guard forbids writing `href` from data.
- The page skips re-rendering the inbox while its data is unchanged, so typing is never
  interrupted by an unrelated snapshot.

Real-process evidence (2026-11-04), all against a SCRATCH data dir under
`/home/bruno/work/.nw-tmp/dash-ui/` with escalations opened by the real CLI (`nw queue escalate
--yes`), never the real data dir:

- Plan then confirm over real HTTP for ESC-0001 (choose), ESC-0002 (text + note), ESC-0003
  (override: waive `conformance_reviewer#0`, attest `AC 4 output is sorted`, reason), ESC-0005
  (reversal of a `nw escalations answer --auto --by conductor` answer). A copy of the data dir
  taken before the answers was answered with `nw escalations answer … --yes` (same forms). With
  timestamps masked, the escalation records, the `escalation_answered` events (5 each, including
  the auto one), the ledger `human_decision` + `note` events of every campaign and the
  `human_overrides` are **identical** (scratch `twin_diff.py`: "ALL IDENTICAL").
- A second plan and a replayed answer for ESC-0001 → 409 `{status: answered, by: human, at}`;
  the withdrawn ESC-0004 → 409 `{status: withdrawn, by: conductor}`; a second reversal of
  ESC-0005 → 409; the data dir was byte-identical outside `agentic/dashboard/` after them.
- A real `nw queue events --follow --for conductor --all --no-code-watch` delivered the
  `escalation_answered` line (`by: human`) of a web answer within its poll.
- Headless Chromium (Playwright's `chrome-headless-shell` 145, `tests/js/browser_drive.js` over
  the DevTools protocol): the page listed the open escalation with its option buttons; clicking
  an option showed the confirm panel with the server's summary and effect; Confirm answered it
  ("ESC-0006 answered: chose 1 (ok) — the conductor applies it.") and the escalation left the inbox
  on the next snapshot.

Automated: `tests/test_dashboard_answers.py` (twin checks against the CLI for all four forms,
conflicts, plan-token and form-error matrices, write checks, audit, auto-applied listing) and
`tests/js/answer.test.js` (via `tests/test_dashboard_answer_page.py`).

## Rollout / migration

No data migration; the escalation format is unchanged. The page gains controls; an old cached
page keeps working read-only.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

_None._
