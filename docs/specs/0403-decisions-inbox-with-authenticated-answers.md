---
id: SPEC-0403
title: "Decisions inbox with authenticated answers through escalations.apply_answer"
status: proposed
owner: user
created: 2026-11-03
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0400
source_idea: IDEA-875
tags: [agentic, dashboard, remote, escalation, conductor, web]
depends_on: [SPEC-0232, SPEC-0235, SPEC-0401]
---

## Context

Child 2 of the epic [SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md),
promoted from `IDEA-875`. It is the child that lets the human unblock the conductor from the
phone. It builds on child 1 of the same epic,
[SPEC-0401](./0401-read-only-dashboard-server-and-nw-dashboard-v1.md) (the read-only
`nw serve --dashboard` server, its bind gate, whois and local-token auth, Host/Origin checks,
the snapshot cache and the `nw.dashboard.v1` snapshot). SPEC-0401 is declared in `depends_on`:
implementation of this spec must not start until SPEC-0401 is `done`.

What already exists (checked against the code on 2026-11-03):

- `agentic/escalations.py`:
  - `plan_answer(cfg, esc_id, *, choose=, text=, waive=, attest=, reason=, note=, by=)` is
    pure validation. It raises `AnswerFormError` (a `ValueError` subclass) for a wrong
    combination of forms, and `ValueError` for everything else: the record is not `open`
    ("ESC-… is answered since …"), the campaign is not PARKED `human_decision` or is still
    pending (`park_requested`), the choice is not an option, the text is empty, or a waive or
    attest does not match the stored review. `read` raises `ValueError("no escalation …")`.
  - `apply_answer(cfg, plan)` takes `item_lock` and then the escalations lock, re-checks
    `open` and the park, writes the ledger `human_decision` and `note` events and the
    `human_overrides`, closes the record (`answered_by`, `answered_at`), and then appends one
    `escalation_answered` event. A failed append comes back as `event_error` on the returned
    record and is not persisted.
  - `answerable_findings(cfg, state, esc_id)` returns the waivable findings. The unmet ACs are
    computed by the private `_review_findings(...)[1]` and have no public accessor.
  - `summarize(answer)` is the pinned one-line answer summary. `list_all(cfg, "open")` lists
    open records. `withdraw` closes a record as `withdrawn`.
  - Records of kind `worker_lost` (SPEC-0342) are applied by the queue itself
    (`workers.apply_answered_escalations`), not by the conductor.
- `queue.dashboard_payload` puts human-owned parks (`park_owner`: `main_dirty`,
  `main_not_on_base`, `human_decision`) in `needs_you`.
- The `nw-conduct` table row `escalation_answered` says what the conductor does for each
  answer kind. For `choose` it does what the option says. For `override` it resumes to
  `REVIEWING` and the fresh review triages with the overrides. For `text` it acts on the text
  or escalates again.
- The CLI `nw escalations answer`, the TUI inbox (SPEC-0250) and the steward relay all call
  `plan_answer` and then `apply_answer`.

## Goals / Non-goals

**Goals**
- G1. The dashboard's snapshot carries a **decisions inbox**. It lists every open escalation
  with its question, options, recommendation, facts, campaign and park context, the waivable
  findings, the unmet ACs, the answer forms that apply, and a fixed text per form saying what
  happens next. It also lists human-owned parks that have no open escalation, read-only, with
  the fix to run on the host.
- G2. The human answers from the phone with choose, text or override (waive and/or attest plus
  a reason), each with an optional note. The answer passes through a confirm step (plan, then
  answer) and is applied through `escalations.plan_answer` / `apply_answer` with
  `by="human"`. The resulting writes equal those of `nw escalations answer … --yes`.
- G3. Writes are authenticated (child 1's gate), CSRF-protected, bound to the plan the human
  confirmed, and audited with tailnet login and device.
- G4. An escalation that was answered or withdrawn elsewhere, or whose campaign left the park,
  is refused with 409 and the current status, and nothing is written.

**Non-goals**
- Withdrawing escalations, sending mailbox messages, writing events of the dashboard's own,
  or resuming, parking or abandoning campaigns from the web (epic non-goals).
- Acting on `main_dirty` / `main_not_on_base` parks from the phone. They are shown with the
  host fix only.
- Live push of the inbox over SSE (child 3) and phone notifications (child 5). Until child 3,
  the inbox refreshes with child 1's 15 s conditional poll and right after each answer.
- Letting the client choose `by`. Every web answer is `by="human"`.

## Decision

1. **Inbox in the snapshot.** A new `agentic/dashboard/inbox.py` builds `inbox: {escalations,
   parks}` from `escalations.list_all(cfg, "open")`, the campaign states, `answerable_findings`,
   a new public `escalations.unmet_acs(cfg, state, esc_id=None)` (a thin wrapper over
   `_review_findings`, so the CLI and TUI can use it too, per epic invariant 5) and the
   `needs_you` rows of `dashboard_payload`. Child 1's snapshot builder fills the `inbox` key
   from it. Child 1's watermark already includes `escalations_signature`, so the inbox is only
   rebuilt when a record changes.
2. **Two write routes, both through the existing pair.** `POST
   /api/escalations/<id>/plan` calls `plan_answer(..., by="human")` and returns the plan plus
   a signed **plan token**. `POST /api/escalations/<id>/answer` checks the token, calls
   `plan_answer` again with the same body (so it revalidates against the current state), then
   calls `apply_answer`. The server writes nothing else outside `agentic/dashboard/`.
3. **Plan token: stateless, HMAC-signed, short-lived.** The token binds the escalation id, the
   record's `created`, a SHA-256 of the canonical answer body, the caller's identity (tailnet
   node id, or `local` on loopback) and an expiry 300 s ahead. It is signed with a per-process
   secret. An answer whose body, caller or expiry does not match is refused with 400 ("re-plan").
   A `created` that no longer matches the record is refused with 409.
4. **CSRF: custom header carrying an HMAC.** Every `POST` must have `Content-Type:
   application/json` and `X-NW-CSRF` equal to `HMAC-SHA256(csrf_secret, caller identity)`.
   The page gets the value from a new authenticated `GET /api/session` (`{login, node,
   csrf}`). It is not templated into the HTML because the shell is static package data and,
   on loopback, unauthenticated. A cross-origin page cannot read `/api/session` (no CORS is
   ever granted), and the custom header forces a preflight that the server never answers. So
   this gives the same protection as embedding the value. Child 1's Host/Origin checks apply
   to every POST too.
5. **Conflict mapping by re-reading, not by parsing messages.** When `plan_answer` or
   `apply_answer` raises a `ValueError`, the server re-reads the record and its campaign
   state. If the record is not `open`, or its campaign is not PARKED `human_decision` (pending
   included), the response is **409** with the current status. Otherwise it is **400** with the
   exception message verbatim. An unknown id is **404**.
6. **Audit.** Every plan, answer and refusal on the write routes, and every 401/403 on any API
   route, appends one `nw.dashboard.audit.v1` line to `<data_dir>/agentic/dashboard/audit.jsonl`
   with the tailnet login, node name and node IP. If child 1 has already shipped
   `audit.py`, this spec extends it. Otherwise this spec adds it.
7. **Phone UI.** The static page gains an inbox view with one card per escalation, answer
   controls for the forms that apply, a confirm sheet that shows the plan summary and the
   effect, and clear messages for 409 and 400.

## Design

### Inbox payload (`inbox.py`)

`build_inbox(cfg, needs_you_rows) -> {"escalations": [...], "parks": [...]}`. It is read-only:
it calls no `events.sync`, takes no locks and writes nothing.

Escalation entries come from `list_all(cfg, "open")`, oldest `created` first:

```
{id, created, by, kind, question, options[{n,label}], recommendation, facts,
 campaign, spec_id, spec_title, project,
 park: {reason, escalated_from, resume_to, parked_at} | null,
 answerable: bool, pending: bool,
 forms: ["choose"?, "text", "override"?],
 findings: [...], unmet_acs: [...],
 effects: {choose?: "...", text: "...", override?: "..."}}
```

- `park` and `spec_title` come from the campaign state and the spec index. They are null for a
  campaign-less record.
- `pending` is true when the campaign has a `park_requested` with reason `human_decision` but
  is not parked yet. `answerable` is false when `pending` is true or the campaign is in any
  other state that is not PARKED `human_decision`. The UI shows non-answerable cards without
  controls and with the reason ("not parked yet").
- `findings` is `answerable_findings(cfg, state, esc_id)`, with each finding's text fields cut
  to 300 characters. `unmet_acs` is `escalations.unmet_acs(cfg, state, esc_id)`, as full AC
  texts, because `--attest` matches on them. Both are `[]` for a campaign-less record.
- `forms`: `choose` only when the record has options. `text` always. `override` only when the
  record has a campaign and at least one finding or unmet AC.
- `effects` is a module constant `EFFECTS`, filtered to `forms`:
  - `choose`: "The conductor does what the chosen option says (resume, abandon, prioritize or
    raise the cap) and tells you in one line."
  - `text`: "The conductor acts on your text by its decision table; if the text is not
    actionable, it asks again with a new question."
  - `override`: "The conductor resumes the campaign to REVIEWING; reviewers are told not to
    re-raise the waived findings or the attested ACs."
  - For `kind == "worker_lost"`, every form's effect is "The queue applies this answer itself
    (SPEC-0342); the conductor is not involved."

Park entries are the `needs_you` rows that have no open escalation for their campaign:

```
{campaign, spec_id, spec_title, project, reason, parked_at, detail, state, fix}
```

- `state` is `awaiting_conductor` when the reason is `human_decision` and the campaign's
  latest escalation is `answered`, meaning the answer is already given and the conductor has
  not applied it yet. Otherwise it is `needs_host`.
- `fix` comes from a constant `HOST_FIX` keyed by reason, with the campaign id and the repo
  root (campaign state `repo.root`) filled in:
  - `main_dirty`: "On the host: move the uncommitted changes in `<root>` off main (commit them
    on a branch), then `nw queue resume <cid>`."
  - `main_not_on_base`: "On the host: move any work off the current branch of `<root>`,
    switch it back to its base branch, then `nw queue resume <cid>`."
  - `human_decision` while awaiting the conductor: "Answered; the conductor applies it on its
    next wake (`nw conduct-status` on the host if it does not)."
  - Any other reason: "On the host: `nw campaign log <cid> --tail 20`."

### Routes (added to child 1's `server.py` router)

| Route | Auth | Result |
|---|---|---|
| `GET /api/session` | child 1 gate | `{login, node, csrf}` (`login`/`node` are `"local"` on loopback) |
| `POST /api/escalations/<id>/plan` | gate + CSRF + JSON | `200 {plan, plan_token, effect}` |
| `POST /api/escalations/<id>/answer` | gate + CSRF + JSON | `200 {record}` · `409 {...}` · `400 {error}` · `404 {error}` |

Request body for both POST routes. The answer route also carries `plan_token`.

```
{form: "choose"|"text"|"override", choose?: int|str, text?: str,
 waive?: [str], attest?: [str], reason?: str, note?: str}
```

- Unknown keys (including `by`) → 400. `form` must agree with the fields present, and the
  server maps the body onto exactly the matching `plan_answer` keywords. For example,
  `form: "text"` passes only `text=` and `note=`, so a mixed body reaches `plan_answer` as a
  mixed call and gets its `AnswerFormError` verbatim.
- Body limit 16 KiB → 413. `Content-Type` other than `application/json` → 415. Invalid JSON
  → 400.
- Order of checks: child 1 gate (401/403) → Host/Origin (403) → method and content type →
  `X-NW-CSRF` (403 `{error: "csrf"}`) → body parse → handler.
- `plan` in the plan response is the `plan_answer` dict minus the internal `override` detail,
  and it keeps `summary`. `effect` is the `EFFECTS` text for the form.
- The 200 answer `record` is what `apply_answer` returned, including `event_error` when the
  wake-up append failed. The UI shows that as a warning ("answer stored; the conductor was
  not woken — check the host").
- The 409 body is `{error, status, by, at, campaign_state}`:
  - for `answered`: `by` = `answered_by`, `at` = `answered_at`;
  - for `withdrawn`: `withdrawn.by` and `withdrawn.at`;
  - for not-parked: `status: "open"`, the campaign's `state`, and `by`/`at` null.

### Plan token and CSRF (`auth.py`)

- Two secrets are made at server start with `secrets.token_bytes(32)` and kept in memory
  only: `plan_secret` and `csrf_secret`. A restart invalidates every token. The page then
  gets a 403 `csrf` or a 400 `re-plan`, refetches `/api/session` once, and asks the human to
  confirm again.
- `caller_id`: on a tailnet bind, whois `Node.ID` (falling back to the node IP); on loopback,
  `"local"`.
- `csrf_for(caller_id) = hex(HMAC-SHA256(csrf_secret, caller_id))`, compared with
  `hmac.compare_digest`.
- `canonical_body`: the request body without `plan_token`, with the strings in `note`,
  `text` and `reason` whitespace-trimmed, `waive`/`attest` kept in the order given, serialised with `json.dumps(...,
  sort_keys=True, separators=(",", ":"))`.
- `plan_token = b64url(payload) + "." + b64url(HMAC-SHA256(plan_secret, payload))`, where
  `payload = json {v:1, esc, created, body: sha256(canonical_body), caller, exp: now+300}`.
- `check_plan_token(token, esc_id, body, caller_id, record)` raises `PlanTokenError` (→ 400
  `re-plan: <why>`) for a bad signature, an expired token, or a different esc, body or caller.
  It raises `PlanConflict` (→ 409) when `created` differs from the record's.

### Answer flow (`inbox.py`)

```
plan(cfg, esc_id, body)   -> plan_answer(cfg, esc_id, **kwargs(body), by="human")
answer(cfg, esc_id, body) -> check token → plan_answer(...) again → apply_answer(cfg, plan)
on ValueError             -> classify(cfg, esc_id): 404 | 409 | 400
```

- `classify` re-reads the record (`read` → `no escalation` = 404). It returns 409 when the
  record is not `open` or its campaign is not PARKED `human_decision`. Otherwise it returns
  400 with `str(e)`.
- `plan_answer` is pure and `apply_answer` checks before it writes, so a 409 or 400 leaves
  the data dir unchanged apart from the audit line. The tests pin this.
- The handler holds no lock of its own. `apply_answer`'s `item_lock` and escalations lock
  serialise two concurrent web answers, and also a web answer racing the CLI: the loser gets
  409.

### Audit (`audit.py`)

`append(cfg, rec)` writes one JSON line with mode 0600 into `audit.jsonl`, opened with
`O_APPEND`, under a module `threading.Lock`. The line format:

```
{schema: "nw.dashboard.audit.v1", ts, action: plan|answer|refused, esc_id, form, summary,
 login, node, node_ip, user_agent, result: ok|400|401|403|404|409|413|415, detail}
```

- `user_agent` is cut to 200 characters and `detail` to 500.
- `summary` is `plan["summary"]` when a plan exists. For a 409, `detail` holds the current
  status/by/at.
- An audit write failure never blocks or undoes an answer. It goes to stderr and the
  response carries `audit_error`.
- `login`/`node`/`node_ip` come from child 1's whois result: `UserProfile.LoginName`,
  `Node.ComputedName` or `Node.Name`, and the peer IP. On loopback they are `"local"`,
  `"local"` and `127.0.0.1`/`::1`.

### Phone UI (`static/app.js`, `app.css`)

- An "Inbox (N)" tab in the header. N counts answerable escalations.
- One card per escalation: the question, the recommendation highlighted, facts as a list,
  the campaign/spec/project line, the park (`escalated_from` → `resume_to`), and then the
  controls for the forms in `forms`:
  - choose: one button per option, with the recommended option marked;
  - text: a textarea;
  - override: checkboxes for findings (id + text) and unmet ACs, plus a required reason
    field.

  Each form also has an optional note field. Every control is at least 44 px tall.
- "Review" posts to `…/plan`. The confirm sheet shows `plan.summary` and `effect` with
  Confirm/Cancel. Confirm posts to `…/answer` with the token.
- On 200, the card shows "Sent — the conductor will apply it" (or the `event_error` warning)
  and the page refetches `/api/snapshot`. On 409, it shows "Already <status> by <by> at
  <at>" and refetches. On 400, it shows the error verbatim and keeps the input.
- Park cards are read-only and show the `fix` in a copyable monospace block.
- `?selftest=1` (child 1) additionally checks that, when the seeded inbox is non-empty, at
  least one answer control and the Review button are visible and at least 44 px tall.

### Module boundaries

- New: `agentic/dashboard/inbox.py`. Extended or new: `agentic/dashboard/audit.py`,
  `auth.py` (tokens, CSRF), `server.py` (routes), `snapshot.py` (fills `inbox`), `static/*`.
- `escalations.py` gains only `unmet_acs`. It does not import the dashboard, and child 1's
  import-graph test keeps covering this.
- The dashboard's only queue-side write is `escalations.apply_answer`. Child 1's read-only
  file-hash test is extended to cover the plan route.

## Alternatives considered

- **Server-side plan store (dict of pending plans).** Lost: it needs expiry sweeping and
  memory bounds, and the signed token gives the same binding with no state. A per-process
  secret already makes a restart drop every plan.
- **Applying the stored plan dict at answer time without re-planning.** Lost: the state can
  change between the confirm sheet and the tap. Calling `plan_answer` again revalidates
  against the current review, and `apply_answer` re-checks inside its locks anyway.
- **Mapping 409 by matching `ValueError` text ("is answered since").** Lost: it is fragile
  against wording changes. Re-reading the record and the campaign state is authoritative.
- **Embedding the CSRF value in a templated `index.html`.** Lost: the shell is static package
  data and is unauthenticated on loopback. An authenticated JSON endpoint that cross-origin
  pages cannot read is equivalent.
- **Accepting `by` from the client, or a per-device `by` such as `human:phone`.** Lost: the
  record must equal the CLI's (`answered_by: "human"`). The device goes in the audit line.
- **Withdraw and resume buttons.** Lost: epic non-goal. The conductor stays the only actor on
  campaigns.

## Acceptance criteria

- [ ] `nw.dashboard.v1` carries `inbox.escalations` with one entry per open escalation (oldest
      first) holding every field listed under Design, including `findings` equal to
      `answerable_findings(cfg, state, id)`, `unmet_acs` equal to `escalations.unmet_acs(...)`,
      `forms`, and `effects` for exactly those forms.
- [ ] A pending escalation (campaign has `park_requested` and is not parked) appears with
      `answerable: false, pending: true`. A campaign-less record has no `override` form. A
      `worker_lost` record's effects name the queue, not the conductor.
- [ ] `inbox.parks` lists every `needs_you` row without an open escalation, with its `fix`
      from `HOST_FIX`. A `human_decision` park whose latest escalation is answered shows
      `state: awaiting_conductor`.
- [ ] `escalations.unmet_acs` exists, and `escalations` does not import `agentic.dashboard`.
- [ ] `POST …/plan` returns `{plan, plan_token, effect}` and writes nothing outside
      `agentic/dashboard/`. The only write is one audit line.
- [ ] `POST …/answer` with a valid token, for each of choose, text and override (with and
      without a note), produces a record, ledger `human_decision` and `note` events,
      `human_overrides` and one `escalation_answered` event equal, modulo timestamps, to those
      of `nw escalations answer <id> … --by human --yes` on a twin data dir.
- [ ] An answer posted over HTTP to a real server reaches a real `nw queue events --follow
      --for conductor` subprocess as one `escalation_answered` line with `by: human` within
      `NW_TEST_WAIT_S`.
- [ ] An answer to an already-answered record, to a withdrawn record, to a record whose
      campaign is no longer PARKED `human_decision`, or with a token whose `created` no longer
      matches, gets 409 `{error, status, by, at, campaign_state}`. The data dir outside
      `agentic/dashboard/` is byte-identical before and after.
- [ ] A plan token that is tampered with, expired, issued for another escalation, another
      body or another caller gets 400 `re-plan…`, and nothing is written.
- [ ] A `plan_answer` validation error (bad option, empty text, override without reason,
      unknown waive id, mixed forms) gets 400 with the exception message verbatim. An unknown
      escalation id gets 404. A body key `by` or any other unknown key gets 400.
- [ ] A POST without `X-NW-CSRF`, with a wrong value, or with a value issued for another
      caller gets 403. A foreign `Origin` or wrong `Host` gets 403. A non-JSON content type
      gets 415, and a body over 16 KiB gets 413. None of these call `plan_answer`.
- [ ] Every plan, answer and refusal on the write routes, and every 401/403 on any API route,
      appends one `nw.dashboard.audit.v1` line with action, result, login, node, node_ip and
      user agent. On a tailnet-identity request, `login` and `node` are whois'
      `UserProfile.LoginName` and `Node.ComputedName`.
- [ ] The escalation record keeps `answered_by: "human"` for web answers.
- [ ] At 390×844 and 360×740, the page self-test passes with a seeded inbox: no horizontal
      overflow, and the answer controls and Review button are visible and at least 44 px tall.
- [ ] `python3 tools/spec_lint.py` exits 0, and the affected tests pass.

## Test plan

- **Automated tests:**
  - `tests/test_dashboard_inbox.py` (unit, seeded `tmp_path` data dir). It seeds:
    - a PARKED `human_decision` campaign with `ESC-0001` that has options, a recommendation,
      facts, a stored review with two blocking findings and one unmet AC;
    - a pending `ESC-0002` (park requested only);
    - a campaign-less `ESC-0003`;
    - a `worker_lost` record;
    - a `main_dirty` park;
    - a `human_decision` park whose escalation is answered.

    It asserts every entry field, the ordering, `forms`, `effects` filtering and the
    worker_lost wording, the `answerable`/`pending` flags, park `fix` and `state`, and that
    `build_inbox` leaves the data dir byte-identical. A drift guard asserts that `EFFECTS["override"]`
    mentions `REVIEWING` and that the `escalation_answered` row of `skills/nw-conduct/SKILL.md`
    still contains `--to REVIEWING`.
  - `tests/test_escalations.py` (extended): `unmet_acs` returns the `met: false` AC texts and
    agrees with what `--attest` accepts.
  - `tests/test_dashboard_answer.py` (in-process `make_server` on `127.0.0.1:0`, real sockets
    via `urllib`, local token):
    - plan, then answer, for choose, text and override, with and without a note;
    - `/api/session` returns the CSRF value for `local`;
    - every 400, 403, 404, 409, 413 and 415 case in the acceptance criteria, each with a hash
      of the data dir (excluding `agentic/dashboard/`) taken before and after;
    - token tamper, expiry (an injected clock), and a mismatched body, caller or esc;
    - two concurrent answers on threads: one 200 and one 409;
    - an `event_error` surfaced when `events.append` is made to fail;
    - one audit line per request, with the right action and result;
    - an audit-write failure still returns 200 with `audit_error`.
  - `tests/test_dashboard_auth.py` (extended from child 1): the fake `tailscale` executable on
    `PATH` plus child 1's injected-peer hook. A write from an allowed login records its
    `login`/`node`/`node_ip` in the audit line. A CSRF value minted for node A is refused for
    node B. Unknown login, tagged node and whois failure on a POST each give 403 plus an audit
    line.
  - `tests/test_dashboard_e2e.py` (real subprocesses, waits via `tests/waits.py`):
    - Start `nw serve --dashboard --bind 127.0.0.1:0` with `NW_DATA_DIR` set to the seeded
      dir, and read the port from `server.json`.
    - Start `nw queue events --follow --for conductor --all --no-code-watch`.
    - GET `/api/session`, POST plan, then answer `ESC-0001` with `choose: 2`. The follower's
      stdout carries exactly one `escalation_answered` line for `ESC-0001` with `by: human`.
    - Twin check: copy the seeded dir before answering, and run `nw escalations answer
      ESC-0001 --choose 2 --by human --yes` on the copy. Compare the record, the ledger
      `human_decision` and `note` events, `human_overrides` and the event `data` with
      timestamps (`ts`, `answered_at`, `at`) removed. Repeat with fresh copies for text and
      for override (`--waive F1 --attest "<unmet AC>" --reason …`).
    - Conflicts:
      - answer `ESC-0001` again → 409;
      - `nw escalations withdraw` a fourth open record with the CLI, then answer it over HTTP
        → 409.

      In both cases the data dir outside `agentic/dashboard/` is unchanged.
  - `tests/test_dashboard_mobile.py` (child 1's headless-Chromium self-test, extended): seed
    the inbox, and check at 390×844 and 360×740 that the answer controls and Review button are
    at least 44 px tall and visible, with no overflow.
- **Manual verification** (human, real tailnet; recorded in the epic's manual checks 3 and 4):
  answer a real escalation from the phone over cellular. The conductor wakes and applies it,
  `nw escalations show <id>` says `answered_by: human`, and the `audit.jsonl` line names the
  phone's node. Then answer an escalation in the CLI first and try it from the phone: a clean
  "Already answered by human at …" message.
- **Regression guard:** the existing `tests/test_escalations*.py`, the TUI inbox tests
  (SPEC-0250) and child 1's read-only, import-graph and load-guard tests keep passing, run
  through `python3 tools/affected_tests.py --run uv run pytest -q`.

## Rollout / migration

1. Land after child 1 is `done`. Children 3 and 4 do not block this spec.
2. No data migration. `audit.jsonl` is created on the first audited request. The escalation
   and event formats are unchanged.
3. The human needs no new step beyond the epic's Tailscale setup. Until then, the loopback bind
   with the local token works for local use and tests.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed: the automated tests above pass, and the manual phone check is
      recorded in the epic.
- [ ] No regressions (affected tests at the queue's close gate).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated, and `python3 tools/spec_lint.py` exits 0.

## Open questions

_None._ Every design detail is decided above. The plan TTL (300 s) and body limit (16 KiB) are
module constants that can be tuned from use.
