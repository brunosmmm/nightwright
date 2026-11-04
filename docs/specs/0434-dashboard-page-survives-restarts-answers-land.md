---
id: SPEC-0434
title: "Dashboard page: answers land at once, the page survives server restarts, the data stamp is truthful"
status: done
owner: user
created: 2026-11-04
updated: 2026-11-04
milestone: "M9: agentic workflow"
source_idea: IDEA-980
kind: feature
parent: SPEC-0400
depends_on: [SPEC-0402, SPEC-0428, SPEC-0429, SPEC-0432]
tags: [agentic, dashboard, csrf, sse, bugfix]
---

## Context

Promoted from idea `IDEA-980`. Child of epic
[SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md).

The human reported on 2026-11-04: "interface is buggy and things don't change after I acked".
At 16:20:12 they answered ESC-0029 from the page. The server side was correct: the audit log
shows plan 200 and answer 200, the `escalation_answered` event was written, and the conductor
resumed the campaign 14 s later. `/api/snapshot` and `/api/stream` were current. The fault was in
the page. The dashboard service (`nw-dashboard.service`) restarted several times while the page
stayed open: it exits on code changes, and once a reinstall restarted it at 16:37.

Reproduced in a real headless Chromium (chrome-headless-shell 1208), against a real
`nw serve --dashboard --bind 127.0.0.1:<port>` on a scratch data dir, with five escalations
opened by the real `nw queue escalate --yes`:

1. **The answered escalation comes back as answerable.** After a 200 answer the page drops the
   escalation's UI state and polls once. The server cache has not re-checked its watermark yet,
   so the poll gets a 304. The escalation is drawn again with fresh option buttons until the next
   snapshot rev arrives: 1 s with `refresh_s: 2`, up to `refresh_s` (5 s by default) plus the
   hub tick in production. On a page whose stream is down this is up to one 15 s poll. Visible
   evidence: 150 ms after Confirm, `#needs` still lists `ESC-0001` with buttons next to the
   "ESC-0001 answered" flash.
2. **CSRF does not survive a restart.** `auth.Csrf()` makes a random secret per process. After a
   SIGTERM plus a relaunch on the same port, the page reconnected (`live` again in 3 s), but its
   next plan was refused 403 `bad_csrf`: "This page is stale: reload it, then answer again". The
   page could not answer at all until a manual reload. Plan tokens are signed with the same
   secret, so an open confirm panel also died with the restart.
3. **The indicator lies while the server is unreachable or wedged.** With the server down the
   chip read `polling` (it was not polling anything that answered). With the server SIGSTOPped
   it read `live` for 45 s while no byte arrived, and the page showed no "data as of" time at
   all, only "checked N s ago".
4. **A hung request freezes the page for good** (found reading the code; reproduced under the
   fake DOM). The snapshot `fetch` and the stream's opening `fetch` have no timeout. `poll()`
   returns the in-flight promise while one exists, and the visible-again path opens the stream
   only after that promise settles. A phone that sleeps mid-request (half-open TCP) therefore
   never polls or streams again. The stream watchdog is armed only after the response headers,
   so a stream open that hangs stays `reconnecting` forever too.
5. **A recovered server is noticed late.** After a failed stream the retry backoff doubles to
   30 s. A poll that succeeds in between (the server is back) does not reopen the stream.

The phone layout at 390×844 and 360×740 was clean: no overflow, no control under 44 px. The
drilldown and both ways back (the back link and `history.back()`) worked. Neither is changed
here.

## Goals / Non-goals

**Goals**
- A 200 answer removes the escalation from the inbox at once, and the next snapshot already
  reflects it.
- A page served by one server process can still plan and answer after the service restarts into
  a new process, without a reload.
- When a write is refused, the page says why, and never fails silently.
- The page never wedges on a hung request. It reconnects soon after the server returns.
- The header shows "data as of HH:MM:SS" and an indicator that is truthful (`live`, `polling`,
  `reconnecting`, `offline`, `stalled`). It warns when nothing has been confirmed for over 60 s.

**Non-goals**
- The drilldown timeline rendering (owned by a parallel change, `feat/campaign-story`).
- Layout changes. The phone check passed.

## Decision

1. **A persisted secret.** `agentic/dashboard/secret` holds 32 random bytes, mode 0600, in the
   0700 dashboard dir. `make_server` loads it, or creates it with `O_CREAT|O_EXCL`. A file that
   is missing, the wrong size, or readable by group or others is replaced atomically. CSRF values
   and plan tokens therefore stay valid across restarts.

   The SPEC-0428 guarantees are unchanged:
   - a value is still an HMAC over a random nonce and the caller's identity;
   - a cross-origin page still cannot read it (no CORS, Host/Origin allowlist);
   - writes still need JSON, same-origin, and the header.

   Only someone who can read the user's data dir can read the secret, and that person can already
   answer through the CLI. To rotate it, delete the file and restart the server.
2. **The page refreshes its CSRF on `bad_csrf` as a fallback.** This covers a rotated secret. The
   page reads `<meta name="nw-csrf">` from a same-origin `GET /`, then retries once. A plan is
   retried as is. An answer is never re-sent blind: the page re-plans and shows the confirm panel
   again with "The dashboard restarted: confirm again". The human confirms twice, which keeps the
   SPEC-0429 two-step. A `bad_plan_token` (400) is handled the same way.
3. **Optimistic removal plus an immediate refetch.** On a 200 answer the page hides that
   escalation id until a snapshot without it arrives, for at most 60 s; after that the server's
   view wins. The server expires its snapshot cache's watermark check after every successful
   answer (`SnapshotCache.expire()`), so the page's immediate poll gets the new rev with a 200,
   not a 304.
4. **Robust liveness.**
   - Every snapshot, plan, and answer request has a timeout (`AbortController`): 10 s for reads,
     20 s for writes.
   - The stream watchdog is armed before the opening `fetch`.
   - A successful poll while the stream is down reopens the stream at once.
   - The browser `online` event does the same.
   - The page polls once each time the stream opens, as a re-fetch.
5. **A truthful header.**
   - `#updated` shows "data as of HH:MM:SS · checked N s ago". The time is the server clock at the
     last confirmation: a 200, a 304, or any stream byte, heartbeat included.
   - The chip reads `offline` while requests fail, and `stalled` when the stream is open but
     silent for over 30 s (two heartbeats).
   - The stale banner appears after 60 s without a confirmation (was 45 s).
6. **Clear write errors.** Every refused or failed plan or answer shows its reason on the
   escalation:
   - a timeout or network error on an answer says the outcome is unknown and refetches;
   - a non-JSON 5xx shows the status;
   - a busy escalation shows "working…".

   Each flash is stamped with its time.

## Design

- `auth.load_secret(dir) -> bytes` and `SECRET_NAME = "secret"`. `server.make_server` builds
  `A.Csrf(A.load_secret(dashboard_dir(cfg)))` when no `csrf` is passed. A bare `DashboardServer`
  keeps a per-instance random secret, so tests are unaffected. The tailnet listener shares the
  `csrf` object, as before.
- `snapshot.SnapshotCache.expire()` forces the next `get()` to check the watermark.
  `server._post_answer` calls it after an `answer` returns 200.
- `static/app.js`:
  - `req(path, opts, ms)` wraps `env.fetch` with an abort timeout.
  - `st.csrf` holds a refreshed value; `refreshCsrf()` fetches `/` and parses the meta tag.
  - `gone[id] = expiry` hides answered ids; `renderNeeds` filters `escalations` and
    `auto_applied` by it, and the signature includes it.
  - `liveLabel()` gains `offline` and `stalled`.
  - `render()` writes the `#updated` age node with the stamp.
  - `request()` calls `kickStream()` after a successful response while the stream is down.

  The drilldown timeline code is not touched.
- `static/app.css`: `.is-offline` and `.is-stalled` chip colours.

## Alternatives considered

- **Client-only CSRF refresh with a per-process secret.** Every restart would cost the human an
  extra confirm and would lose an open plan. A code-change restart happens often here (the
  editable install restarts on every merge), so this lost. It is kept as the fallback.
- **Sending the CSRF value in a cookie (double submit).** This changes SPEC-0428's model, and it
  still breaks on restart unless the secret persists.
- **Retrying an answer automatically on `bad_csrf`.** This would apply a decision without a
  confirm made under the current credentials. Rejected; the page re-plans instead.

## Acceptance criteria

- [x] After a 200 answer the escalation is gone from `#needs` before any new snapshot arrives,
      and the immediate poll returns the new rev (200, not 304).
- [x] A CSRF value and a plan token minted by server process A are accepted by process B after a
      restart on the same data dir. The secret file is mode 0600, and a group- or world-readable
      or malformed file is replaced.
- [x] On 403 `bad_csrf` the page fetches a fresh CSRF value. A plan is retried once. An answer is
      re-planned and shown for confirmation again, never re-sent automatically.
- [x] Every failed plan or answer (403, 409, 400, 5xx, network, or timeout) leaves a visible
      reason on the escalation.
- [x] A hung snapshot request is aborted after 10 s, and the page keeps polling. A hung stream
      open fails over to polling and retries.
- [x] A successful poll while the stream is down reopens the stream at once.
- [x] `#updated` shows "data as of HH:MM:SS". The chip reads `offline` while requests fail, and
      `stalled` when the stream is silent over 30 s. The stale banner appears after 60 s.
- [x] In real headless Chromium: answer, then restart, then answer again, all without a reload.
      The phone selftest at 390×844 still passes.

## Test plan

- **Automated tests:**
  - `tests/js/answer.test.js`:
    - optimistic removal on a 200 answer before a new rev arrives;
    - a 304 after the answer does not bring the escalation back;
    - `bad_csrf` on a plan refreshes the value from `/` and retries once with the new header;
    - `bad_csrf` on an answer re-plans and shows the confirm panel again, with only one answer
      POST;
    - network and timeout errors show a reason.
  - `tests/js/live.test.js`:
    - a hung snapshot fetch is aborted and polling continues;
    - a hung stream open fails over;
    - a poll success while the stream is down reopens the stream;
    - the `offline` and `stalled` labels;
    - the "data as of" stamp and the 60 s stale banner.
  - `tests/test_dashboard_csrf.py`:
    - the secret file's mode, persistence, and replacement;
    - a page and plan token from server A are accepted by a fresh server B on the same data dir,
      over real sockets.
  - `tests/test_dashboard_answers.py`: the snapshot after a web answer is fresh at once (the
    cache expiry).
- **Manual verification:** run the scratch-data-dir repro driver (Context) in real
  chrome-headless-shell through steps (a) to (f), plus `tests/js/browser_drive.js` for the phone
  selftest. Record the results in As shipped.
- **Regression guard:** the affected-tests selection (`tools/affected_tests.py`), all `tests/js`
  node suites, and `spec_lint`.

## As shipped

Shipped as designed (Decisions 1 to 6), with these additions found during the work:

- **Hub phase fix (`stream.Hub._run`).** Each snapshotter tick calls `cache.expire()` before
  `get()`. Before the fix, the cache's own check clock could run a hair behind the hub tick, so
  the check was skipped and a queue change reached the stream after up to 2 × `refresh_s`. In
  the browser repro with the default `refresh_s: 5`, the page took 9.5 s to show an answer made
  through the CLI; after the fix it took 4.7 s, including the CLI's own start-up.
- **The plan token refusal (400) now carries `reason: "bad_plan_token"`.** This is additive, and
  lets the page tell it apart from other 400s.
- **An expired confirmation (409 `plan_expired`) is re-planned once**, like a bad token, with
  "The confirmation expired: check it and confirm again".
- **The answer timeout message** says the outcome is unknown ("may or may not have been
  applied") and refetches.
- **The drilldown's campaign `fetch` is bounded by the same 10 s timeout.** The timeline
  rendering is untouched.
- **A successful poll reopens a down stream at most once per 2 s**, and the backoff is not reset
  by it. A server that refuses streams but answers polls is therefore retried at most once per
  poll.
- **The secret lives at `<data_dir>/agentic/dashboard/secret`.** `tests/test_dashboard_server_assets.py`
  and `tests/test_dashboard_http_readonly.py` allow it in the dashboard dir (SPEC-0400
  invariant 1: that dir is the only one the dashboard writes).

**Tests:**
- `tests/js/answer.test.js`: six new tests, 13 in all.
- `tests/js/live.test.js`: five new or split tests, 20 in all.
- `tests/js/page_logic.test.js`: one new test, plus the header and stale assertions updated; 15
  in all.
- `tests/test_dashboard_restart.py` (new):
  - the secret file's mode and reuse;
  - a malformed or exposed secret is replaced;
  - `make_server` shares the persisted secret;
  - the snapshot right after a 200 answer is a 200 without the escalation, even with an
    hour-long cache;
  - real `nw serve --dashboard` process A serves the page and a plan token, then after SIGTERM
    and a relaunch on the same port, process B accepts A's CSRF value and plan token and a
    fresh plan and answer;
  - a forged value is still `bad_csrf`.

  Each new guard was checked to fail with its fix reverted.

**Manual verification (2026-11-04).** Real chrome-headless-shell 1208, at a 390×844 phone
viewport. Scratch `NW_DATA_DIR` / `NW_CONFIG_DIR`, five escalations opened by the real
`nw queue escalate --yes`, and a real `nw serve --dashboard --bind 127.0.0.1:<port>` with the
default `refresh_s`.

| Step | Before | After |
|---|---|---|
| (a) answer | the escalation was re-drawn with buttons until the next rev (1 s at `refresh_s: 2`) | gone 0.15 s after Confirm; the flash is time-stamped |
| (b) SIGTERM and relaunch on the same port | chip `polling` while down; live again in 3 s; the next plan got 403 `bad_csrf` "reload" | chip `offline` while down; live again in 3 s; plan and answer 200 with no reload |
| (b2) relaunch with the secret file deleted (rotated) | n/a | the page refreshed its CSRF from `/`, retried the plan, answered 200 |
| (c) answer through the CLI | the page followed | it followed within one `refresh_s` (above) |
| (e) drilldown, back link, `history.back()` | worked | worked |
| (f) server SIGSTOPped for 70 s | `live` for 45 s with no byte, then `reconnecting` / "stale" | `live` up to 30 s, then `stalled`; at 45 s the watchdog moves it to `reconnecting`, then `offline` once the poll and the open time out; `live` again 0.6 s after SIGCONT |

Throughout, the header reads "data as of HH:MM:SS · checked N s ago". `tests/js/browser_drive.js`
passed the selftest at 390×844 and 360×740 (no overflow, no control under 44 px). Its tail step
needs a campaign with runs, which the scratch world does not have.

**Not reproduced:** the exact 16:20 session. The audit and server state point to failure modes
1 and 2 together (an answer re-shown as answerable, then answers refused after the restarts), and
both are fixed. Pages still open on the old `app.js` hold a value minted under the old
per-process secret, so they need one reload.

## Rollout / migration

None. The first start writes the secret file. Pages that are already open hold a value minted
under the old per-process secret. The new fallback (Decision 2) covers them once they load the
new `app.js`. Until then, a page still running the old `app.js` needs one reload.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
