---
id: SPEC-0400
title: "Remote web dashboard and decisions inbox over Tailscale (nw serve --dashboard)"
status: accepted
owner: user
created: 2026-11-03
updated: 2026-11-03
milestone: "M9: agentic workflow"
source_idea: IDEA-873
kind: epic
tags: [agentic, dashboard, remote, escalation, conductor, web]
depends_on: [SPEC-0222, SPEC-0232, SPEC-0235, SPEC-0250, SPEC-0334, SPEC-0362]
---

## Context

Promoted from idea `IDEA-873`. The human asked on 2026-11-03:

> "A live web-based dashboard of active items so that I can consult this while I am away from
> home. I should also be able to see an 'inbox' for my decisions and I should be able to select
> decisions for when the conductor escalates things. It should send messages to the conductor to
> unblock it."

The human has already decided how it is reached: **Tailscale only**. Nothing is exposed on the
public internet. `tailscale` 1.102 is installed on the host but is logged out.

Everything a phone needs to show already exists on the host, but only in terminals. This was
checked against the code on 2026-11-03:

- **Escalations (SPEC-0232/0235).** `agentic/escalations.py` stores `nw.escalation.v1`
  records (`ESC-NNNN`: question, options, recommendation, facts, campaign, park). The answer
  path is `plan_answer(cfg, id, choose=|text=|waive=,attest=,reason=, note=, by=)`, a pure
  validation, followed by `apply_answer(cfg, plan)`. `apply_answer` does the following:
  - takes `item_lock` and then the escalations lock;
  - re-checks that the record is `open` and that the campaign is PARKED `human_decision`;
  - writes the ledger `human_decision` and `note`, the `human_overrides` and the record;
  - then appends one `escalation_answered` event to `events.jsonl`.

  That event is how an answer reaches the conductor. The follower `nw queue events --follow
  --for conductor` (`events.follow`) polls every `poll_s` (2 s). `quiet.py` classifies
  `escalation_answered` as `wake`, so the event becomes a line on the conductor's Monitor
  stream. The `nw-conduct` table row `escalation_answered` then applies it: resume, abandon,
  prioritize or raise the cap. The answer command never moves the campaign itself. The CLI
  (`nw escalations answer`), the TUI escalation inbox (SPEC-0250, `tui/mailbox_tab.py`) and
  the steward relay all call this same pair with `by="human"` (the steward uses
  `steward:<P>`). `answerable_findings(cfg, state, esc_id)` lists the waivable finding ids.
  Escalations of kind `worker_lost` (SPEC-0342) are applied by the queue itself
  (`workers.apply_answered_escalations`).
- **Parks waiting for the human (SPEC-0222).** `queue.park_owner` maps `main_dirty`,
  `main_not_on_base` and `human_decision` to `human`. `dashboard_payload` splits parks into
  `needs_you` and `conductor_handling`. Only `human_decision` parks carry an escalation. The
  other two need the human's own checkout on the host.
- **Cap raises (IDEA-793).** The conductor resumes a first real `cost_cap` park itself and
  escalates the second one. An escalated cap raise is therefore an ordinary escalation that
  the human answers as `human`. IDEA-793's remaining gap is the steward-relayed "raise the cap"
  said in a steward session, and it stays open.
- **Live data.** The following payloads already exist:
  - `queue.dashboard_payload` (`nw.queue.dashboard.v1`, what `nw queue watch` draws): lease,
    slots, gate pools, active campaigns with activity, loops and `cost_usd` / `cap_usd`,
    `needs_you`, `conductor_handling`, waiting, backlog, delivered, and
    `totals.spend_today_usd`;
  - `queue.queue_payload` (`nw queue list --json`);
  - `cli_conduct.conduct_status`, which gives the conductor pid, stewards and `herdr_watch`.
    It calls `events.sync`, which writes, so the dashboard must not call it;
  - `events.read_since`;
  - `tail.read_lines` + `tail.Renderer` over `runs/<run>/stream.jsonl` (SPEC-0362, `nw
    campaign tail`);
  - `escalations.list_all` and the TUI's cheap `escalations_signature`.
- **HTTP server precedent (SPEC-0334).** `nw serve --jobs` (`agentic/pull_jobs.py`) is a
  stdlib `http.server.ThreadingHTTPServer` with per-request socket timeouts. Its
  `check_bind` allows loopback and the Tailscale ranges (`100.64.0.0/10`,
  `fd7a:115c:a1e0::/48`) without TLS, and a wildcard only with TLS. Its `--jobs` flag says
  "the only mode so far".
- **Dependencies.** `pyproject.toml` has click, rich, pyyaml, orgparse and textual. There is
  no web framework and no crypto library.
- **Related prior work.**
  - IDEA-008 (comfort-assistant, shipped): a web UI that rescanned a whole table on every 5 s
    poll. Lesson: compute a snapshot once, server-side, only when something changed, and
    share it between clients.
  - IDEA-059: desktop notifications, parked by the human.
  - IDEA-087 → SPEC-0232/0235: escalations.
  - IDEA-085 / SPEC-0249/0250: the TUI mailbox and escalation inbox.
  - IDEA-096 → SPEC-0222: "Needs you".
  - SPEC-0360..0366: herdr display. Its notifications only reach the host's terminal.

This is an epic because it splits into five shippable pieces with different risks: a server
plus read-only views; an authenticated write path; streaming; supervision; and phone push.

## Goals / Non-goals

**Goals**
- G1. From the phone, over the tailnet only, the human sees the following, current to within
  a few seconds:
  - runner, conductor, steward and herdr-watch health;
  - active campaigns with state, loop, persona/activity and cost vs cap;
  - queued, parked and waiting counts;
  - recent merges and failures;
  - today's spend;
  - a per-campaign drilldown with a live run tail.
- G2. A **decisions inbox** lists every open escalation with its question, options,
  recommendation, facts, campaign context, and what each answer will do. It also lists
  human-owned parks that have no escalation.
- G3. The human answers an escalation from the phone with choose, text, or override
  (waive/attest + reason). The answer goes through `escalations.plan_answer` /
  `apply_answer` with `by="human"`, so it writes exactly what `nw escalations answer --yes`
  writes and wakes the conductor through the same `escalation_answered` event.
- G4. Answering is authenticated (tailnet identity), CSRF-protected, confirmed (plan, then
  apply) and audited (who, from which device). An escalation already answered or withdrawn
  elsewhere is refused cleanly.
- G5. The server refuses to start on any address that is neither loopback nor this host's
  own tailnet address. It never makes the queue noticeably slower.
- G6. The server runs unattended (survives logout, reboot and upgrades), and `nw dashboard
  status` reports it.
- G7. The phone is told when a decision is waiting (last child, opt-in).

**Non-goals**
- Any public-internet exposure: no Funnel, no port forwarding, no third-party relay for the
  dashboard itself.
- A parallel channel to the conductor. The web UI sends no mailbox messages, writes no
  events of its own and does not resume, park or abandon campaigns. Free messages to the
  conductor outside an open escalation stay on the host (TUI mailbox, IDEA-085). Revisit only
  if the human asks.
- Withdrawing escalations from the web (the conductor and the CLI keep that).
- Acting on `main_dirty` / `main_not_on_base` parks from the phone: they need the host
  checkout. The inbox shows them with the fix to run.
- Multi-user or role-based access. One human, their own devices.
- Closing IDEA-793's steward-relay rule (see Context).
- Desktop notifications (IDEA-059 stays its own idea).

## Decision

1. **Server: a second mode of `nw serve`.** `nw serve --dashboard` is a separate process
   from `nw serve --jobs`, on its own port (default 8791). The two modes are mutually
   exclusive per process because their trust models differ: the job API takes bearer tokens
   and may bind a wildcard with TLS, while the dashboard never binds a wildcard. The server is
   built on the stdlib `ThreadingHTTPServer` with the SPEC-0334 handler hardening (per-request
   socket timeout, bounded bodies). The UI is static HTML/CSS/vanilla JS shipped as package
   data. There is no build step, and no CDN or other external fetch, so the phone loads
   everything from the host. **No new runtime dependency** for children 1-4. The only new
   dependency in the epic is `cryptography`, in child 5 (justified there).
2. **Auth.**
   - **Bind gate.** `--bind tailscale` (the default) resolves this host's own address with
     `tailscale ip -4`. An explicit address must be loopback or one of the host's own
     `tailscale ip` addresses. Wildcards, LAN addresses and other tailnet IPs are refused with
     exit 2, and TLS options do not lift the refusal.
   - **Identity gate on tailnet binds: `tailscale whois --json <peer-ip>`.** The peer IP is
     assigned by Tailscale and cannot be spoofed inside the WireGuard tunnel. The returned
     `UserProfile.LoginName` must be in `agentic.dashboard.allowed_logins`, which by default
     holds only the host's own login (`tailscale status --json` → `Self.UserID` → `User`).
     Tagged nodes with no user are refused. Results are cached per peer IP for 60 s. Any whois
     failure denies the request (403), so the gate fails closed.
   - **Loopback binds** (tests, local use) require the per-host token in
     `<data_dir>/agentic/dashboard/local.token` (mode 0600, created on first start), sent as
     `Authorization: Bearer`.
   - **Rejected:** `tailscale serve` identity headers. They need a loopback bind behind a
     proxy, where any local process can forge the headers.
   - **Device identity** is whois' `Node.ComputedName` / `Node.Name` and the node IP. It is
     written to the audit log, so no separate per-device token is needed on the tailnet.
   - **Write protection.**
     - CSRF: writes are `POST` with `Content-Type: application/json` and an `X-NW-CSRF`
       header. The header's value is an HMAC of a per-process secret and the caller's node
       id, embedded in the page. The custom header plus the JSON body force a CORS preflight,
       which the server never grants.
     - `Origin`, when present, and `Host` must match the bound address or the host's
       MagicDNS name. This blocks DNS rebinding.
     - Confirm step: `POST …/plan` returns the dry-run plan with a short-lived plan token,
       and `POST …/answer` must echo that token.
     - Audit: every plan, answer and refusal is a line in
       `<data_dir>/agentic/dashboard/audit.jsonl`.
3. **An answer reaches the conductor by the existing path only.** The answer endpoint calls
   `escalations.plan_answer(..., by="human")` and then `escalations.apply_answer(plan)`, the
   same calls `nw escalations answer --yes` and the TUI make. The record, the ledger
   `human_decision` and `note` events, `human_overrides` and the single `escalation_answered`
   event are therefore byte-for-byte the CLI's (timestamps aside). The conductor follower
   delivers the event within its 2 s poll, and the conductor applies it per its table. The
   dashboard adds nothing to the conductor's stream.
4. **Optimistic concurrency** reuses what `apply_answer` already enforces. The answer request
   carries the escalation id and the plan token. The token binds the escalation id, the
   answer form and the record's `created` timestamp. If the record is no longer `open` (it was
   answered by the CLI, another device, the steward or the TUI, or withdrawn by the
   conductor), or its campaign is no longer PARKED `human_decision`, `plan_answer` or
   `apply_answer` raises. The server returns **409** with the record's current status, who
   changed it and when. Nothing is written in that case, and the UI refreshes the inbox.
5. **Live updates: one shared snapshot pushed over SSE, with conditional polling as
   fallback.** The rejected alternative is per-client polling that recomputes the payload
   each time (the IDEA-008 failure).
   - A single snapshotter thread runs only while at least one client is connected. Every
     `refresh_s` (5 s) it stats a cheap watermark: the size and mtime of `events.jsonl`, the
     escalations signature, and the mtime of the lease file.
   - It recomputes `nw.dashboard.v1` only when the watermark moved or `max_stale_s` (30 s)
     passed. A pushed snapshot carries timestamps rather than ages, and the page derives
     ages, so a 30 s recompute still shows correct ages.
   - Each new snapshot is hashed and sent to every client only when the hash changed. Clients
     receive `text/event-stream` with a 15 s heartbeat comment.
   - `GET /api/snapshot` with `If-None-Match` is the fallback for browsers or networks that
     drop SSE (mobile Safari in the background), polled every 15 s. The page reconnects on
     `visibilitychange`.
   - Clients are capped (`max_clients`, 8 → 503). With no client, the server does no queue
     work at all.
6. **Notifications: Web Push, opt-in, content-free, as the last child.** Herdr notifications
   are rejected: they only reach a terminal on the host, which the human is away from. The
   existing `agentic.escalations.notify` command hook remains for anyone who wants another
   local channel.
   - Web Push needs a secure context. Child 5 therefore adds HTTPS through `tailscale cert`
     for the host's MagicDNS name (still bound only to the tailnet IP) and a service worker.
     On iOS the page must also be added to the home screen (iOS 16.4+).
   - Pushes travel through Apple's or Google's push service. To keep the human's "nothing on
     the public internet" intent, the payload carries no question, campaign or spec text, only
     `nightwright: N decision(s) waiting`. Tapping the notification opens the inbox over the
     tailnet.
   - Push is off until the human taps "Enable notifications" on a device, which is the
     human's explicit opt-in.
7. **Lifecycle: a systemd user unit, not the conductor follower.**
   - The dashboard's job is to show what is wrong while the human is away, including "the
     conductor is down". A server supervised by the conductor follower (the SPEC-0365
     pattern) would die exactly when it is needed. SPEC-0291's "no persistent supervisor"
     rule was about the queue runner and does not cover a read-mostly viewer.
   - `nw dashboard unit --install` writes `~/.config/systemd/user/nw-dashboard.service`
     (`Restart=always`, `RestartSec=10`).
   - The server exits 0 when nightwright's code changes (the `codewatch` mechanism of
     SPEC-0231), so systemd restarts it on the new code.
   - The server also exits non-zero when the tailnet address is missing at start, and systemd
     retries.
   - `nw dashboard status [--json]` reports the unit, the pid, the URL, the tailscale state, a
     whois self-check and connected clients. `conduct-status` gains a `dashboard` key, like
     `herdr_watch`.

## Architecture / cross-cutting design

**Module layout** (new package `src/nightwright/agentic/dashboard/`):
- `server.py`: `make_server(cfg, bind, …)`, the handler and routing, and SSE fan-out.
- `auth.py`: `check_bind`, `own_tailnet_ips`, whois with a cache and fail-closed behaviour,
  the local token, CSRF, and the Host/Origin checks.
- `snapshot.py`: builds `nw.dashboard.v1`, and holds the watermark and the shared
  snapshotter.
- `inbox.py`: the inbox payload, plus plan/answer through `escalations`.
- `audit.py`: `audit.jsonl`.
- `static/`: `index.html`, `app.js`, `app.css` (and, in child 5, `sw.js` + `manifest.json`).
- `cli_dashboard.py`: `nw dashboard snapshot|status|unit` and `nw serve --dashboard` wiring
  in `cli_worker.serve_cmd`.

**Invariants every child respects:**
1. **Read-only except one write.**
   - The server never calls `events.sync`, `events.follow`, `store.write_state`,
     `mailbox.send` or any `queue` mutation. The only queue-side write is
     `escalations.apply_answer`.
   - The server's own files live under `<data_dir>/agentic/dashboard/`: `local.token`,
     `audit.jsonl`, `server.json`, and from child 5 `push/`.
   - A test pins this: serving snapshots and tails leaves every other file under the data dir
     byte-identical.
2. **The queue never imports the dashboard.** The loop, the runner, `runproc`, `events` and
   `escalations` do not import `agentic.dashboard` (an import-graph test, as for herdr in
   SPEC-0360). If the dashboard crashes or hangs, no tick, run or follower is affected.
3. **Bind gate.** `auth.check_bind` accepts only loopback or the host's own tailnet IPs. It
   is called before the socket opens, and nothing (no flag, no config) bypasses it.
4. **Every API request is authenticated:** whois on a tailnet bind, the local token on
   loopback. Static assets on a tailnet bind are also whois-gated. On loopback the static
   shell holds no data and is served without auth, while the data API still requires the
   token.
5. **Payloads reuse, never fork, the existing builders.** The following are used as-is:
   - `queue.dashboard_payload` for active, `needs_you`, `conductor_handling`, waiting,
     backlog, delivered and totals. `spend_today_usd` is the same number `nw queue watch`
     shows;
   - `queue_payload["lease"]` and `runner_code` for runner health;
   - `events.reader_holder` / `events.followers` (read-only) for conductor and stewards;
   - `cli_conduct._herdr_watch_status`;
   - `escalations.list_all("open")`, `answerable_findings`, `summarize`;
   - `tail.read_lines` + `tail.Renderer` (plain text) for the run tail.

   A dashboard view that needs a field these do not have adds it to the builder, so the CLI
   gains it too.
6. **Recent events** are read from the tail of `events.jsonl` (the last 256 KiB, then
   filtered), never with `read_since(cfg, 0)` over the whole file.
7. **Schema** `nw.dashboard.v1`:

   ```
   {schema, generated_at, rev,
    health: {runner: {lease, runner_code, draining}, conductor: {pid, lag},
             stewards: [...], herdr_watch: {...}},
    totals,
    active, needs_you, conductor_handling, waiting, backlog: {count, top: [≤5]}, delivered,
    recent_events: [≤20 of merged|run_failed|park|escalation|escalation_answered|…],
    inbox: {escalations: [...], parks: [...]}}
   ```

   `rev` is the snapshot hash and is also the ETag.

   Inbox escalation entry:

   ```
   {id, created, by, kind, question, options[{n,label}], recommendation, facts,
    campaign, spec_id, spec_title, project, park: {escalated_from, resume_to},
    findings: answerable_findings(...), unmet_acs, effects: {choose|text|override: "…"}}
   ```

   `effects` is a fixed text per answer form that states what the conductor does with it,
   taken from the `nw-conduct` `escalation_answered` row. For example, override: "the
   conductor resumes the campaign to REVIEWING; reviewers are told not to re-raise the waived
   findings".
8. **HTTP API**:
   - `GET /` (the shell)
   - `GET /api/snapshot` (ETag)
   - `GET /api/stream` (SSE `snapshot` events)
   - `GET /api/campaigns/<cid>`: drilldown with the ledger tail, run list and `decisions.read`
     for the campaign
   - `GET /api/campaigns/<cid>/tail?run=&offset=` (SSE of rendered lines)
   - `POST /api/escalations/<id>/plan` → `{plan, plan_token}`. The body is `{form:
     choose|text|override, choose?, text?, waive?, attest?, reason?, note?}`.
   - `POST /api/escalations/<id>/answer` with the same body + `plan_token` → `200 {record}` |
     `409 {status, by, at}` | `400 {error}` (a `plan_answer` `ValueError` / `AnswerFormError`
     message, verbatim)
   - child 5: `POST /api/push/subscribe`, `/unsubscribe`
9. **Audit record** (`nw.dashboard.audit.v1`):

   ```
   {ts, action: plan|answer|refused|subscribe, esc_id, form, summary,
    login, node, node_ip, user_agent, result: ok|409|400|403, detail}
   ```

   The escalation record itself keeps `answered_by: "human"`, as from the CLI. The audit
   line adds the device.
10. **Config** `agentic.dashboard`:

    ```
    {bind: "tailscale:8791", allowed_logins: [], refresh_s: 5, max_stale_s: 30,
     max_clients: 8, tail_poll_s: 1.0, push: {enabled: false}}
    ```

    `allowed_logins: []` means the host's own login. Malformed values fall back to the
    defaults, as `escalations.settings` does.

## Breakdown / sub-specs

- [ ] Read-only dashboard server and nw.dashboard.v1 snapshot (project: Meta-Tools) — `nw serve --dashboard` (stdlib server, own port 8791, mutually exclusive with `--jobs`), `agentic/dashboard/{server,auth,snapshot}.py`, bind gate (loopback or own `tailscale ip` only, `--bind tailscale` default, refuse wildcard/LAN/foreign tailnet even with TLS flags), whois identity gate with `allowed_logins` (default own login, fail closed, 60 s cache) and loopback `local.token`, Host/Origin checks, `nw.dashboard.v1` built from `dashboard_payload`/`queue_payload`/followers/herdr status/escalations (no `events.sync`), shared watermark-driven snapshot cache + `GET /api/snapshot` with ETag, phone-first static page (health, active with cost vs cap, counts, needs-you, recent events, spend today; 15 s conditional poll), `nw dashboard snapshot --json`, read-only and no-import invariants pinned by tests
- [ ] Decisions inbox with authenticated answers through escalations.apply_answer (project: Meta-Tools) — inbox payload (open escalations with question/options/recommendation/facts/campaign/park, `answerable_findings`, unmet ACs, per-form `effects`; human-owned parks without an escalation shown read-only with the host fix), `POST /plan` and `/answer` calling `plan_answer`/`apply_answer` with `by="human"` (choose, text, override waive/attest + reason, optional note), plan-token confirm step, CSRF header HMAC, 409 on answered/withdrawn/not-parked, `audit.jsonl` with tailnet login + device, real e2e: an answer from HTTP reaches a live `nw queue events --follow --for conductor` and equals the CLI's writes (depends_on child 1)
- [ ] Live updates over SSE and per-campaign drilldown with run tail (project: Meta-Tools) — `GET /api/stream` SSE fan-out of the shared snapshot (only on rev change, 15 s heartbeat, `max_clients`, no work with zero clients, reconnect on visibilitychange, polling fallback kept), campaign drilldown (ledger tail, runs, conductor decisions, cost vs cap), `GET /api/campaigns/<cid>/tail` SSE streaming `tail.Renderer` plain lines from `stream.jsonl` offsets until the run ends, load guard test vs IDEA-008 (depends_on child 1)
- [ ] Dashboard lifecycle: systemd user unit, code-change restart and nw dashboard status (project: Meta-Tools) — `nw dashboard unit --install|--uninstall|--print` (user unit, `Restart=always`, ExecStart pinned to the installed `nw`), server exits 0 on code change (SPEC-0231 codewatch) and non-zero when the tailnet address is missing, `server.json` runtime record (pid + start ticks, url, clients), `nw dashboard status [--json]` (unit, pid, url, tailscale state, whois self-check), `conduct-status` `dashboard` key, `agentic.dashboard` config keys documented in `docs/WORKFLOW.md` with the human's Tailscale setup steps (depends_on child 1)
- [ ] Phone notifications via Web Push over the tailnet (project: Meta-Tools) — opt-in HTTPS via `tailscale cert` for the MagicDNS name (still tailnet-IP bound), service worker + web app manifest (iOS home-screen install), VAPID keypair under `agentic/dashboard/push/`, subscribe/unsubscribe per device (audited), the server's own `--for dashboard` events cursor pushes a content-free "N decision(s) waiting" on `escalation` and on SPEC-0232 reminders, expired subscriptions pruned, new dependency `cryptography` (RFC 8291 aes128gcm + ECDH P-256 + ES256 VAPID JWT; not in stdlib) (depends_on child 2 and child 4)

**Delivery record (2026-11-04).** The breakdown above is the plan; the queue split it into these
specs (the linter's rollup in `docs/LEDGER.md` is the authority on status):

- child 1 → SPEC-0401 (snapshot), SPEC-0413 (page), SPEC-0414 (loopback server, superseded by
  SPEC-0428 for auth), SPEC-0416 (tailnet bind gate), SPEC-0417 (whois gate logic), SPEC-0418
  (page routes), SPEC-0428 (no loopback token: Host allowlist, same-origin, per-page CSRF, no
  CORS; whois gate wired into the server, tailnet binds served). Decision 2's "loopback binds
  require the local token" is replaced by SPEC-0428 (the human's 2026-11-04 decision).
- child 2 → SPEC-0403 (inbox payload) and SPEC-0429 (answering from the page: plan / confirm /
  answer through `plan_answer` / `apply_answer`, 409, audit, auto-answer reversal).
- child 3 → SPEC-0402 (live snapshot stream) and SPEC-0430 (campaign drilldown and live run tail).
- child 4 → SPEC-0404 (code-change exit, exit 3, `server.json`); the systemd user unit and `nw
  dashboard status|unit` are a separate, parallel build.
- child 5 (Web Push) is **not delivered** and has no spec yet: it needs the new `cryptography`
  dependency, HTTPS through `tailscale cert`, and the human's tailnet admin steps (MagicDNS and
  HTTPS certificates), none of which exist on this host (tailscale is logged out).
- The manual real-tailnet checks 1–4 of the Test plan wait for the human's `sudo tailscale up` /
  `tailscale set --operator=$USER` and a phone on the same tailnet.

Sequencing / dependencies: child 1 comes first. It is useful alone and is the human's first
ask ("consult this while away"). Children 2, 3 and 4 each depend only on child 1 and may run
in parallel. Child 2 is the priority because it unblocks the conductor. Child 5 comes last:
it needs the inbox to point at (child 2) and an always-on server (child 4), and it is the only
child that adds a dependency and needs extra tailnet admin settings.

## Acceptance criteria

- [ ] `nw serve --dashboard` serves the dashboard on this host's tailnet IP. It refuses to
      start (exit 2, message naming the rule) on `0.0.0.0`, `::`, a LAN address, a tailnet IP
      that is not this host's, or any of these with TLS flags.
- [ ] Over the tailnet, a request from a node whose login is not allowed, from a tagged node,
      or made while `tailscale whois` fails gets 403 and an audit line. On loopback, a
      request without the local token gets 401.
- [ ] The phone shows the following and updates without a reload within `refresh_s` +
      delivery of a queue change:
      - health: the runner lease and heartbeat, the conductor follower, stewards, the herdr
        watch;
      - active campaigns with state, loop, activity and cost vs cap;
      - queued, parked and waiting counts;
      - recent merges and failures;
      - spend today, equal to `nw queue watch`'s.
- [ ] The inbox lists every open escalation with question, options, recommendation, facts,
      campaign and park, the waivable findings and unmet ACs, and the effect of each answer
      form. It also lists human-owned parks without an escalation, read-only, with their host
      fix.
- [ ] Answering from the phone (choose, text, or override) after the confirm step produces the
      same record, ledger events, overrides and single `escalation_answered` event as `nw
      escalations answer … --by human --yes`. The conductor follower delivers that event.
      Nothing else is written outside `agentic/dashboard/` (only the audit line inside it).
- [ ] A second answer, an answer to a withdrawn record, or one whose campaign left the park
      gets 409 with the current status, and nothing is written. A write without the CSRF
      header, with a foreign `Origin` or with a wrong `Host` is refused.
- [ ] The per-campaign drilldown streams a running persona's tail live, and it ends when the
      run ends.
- [ ] With zero connected clients the server does no queue reads. With N clients the
      snapshot is computed at most once per `refresh_s` and only when the watermark moved
      (or `max_stale_s` passed).
- [ ] The systemd user unit keeps the server up across logout, reboot (with linger) and
      nightwright upgrades. `nw dashboard status` reports it.
- [ ] With push enabled on a device, an escalation produces one content-free notification on
      that device. Tapping it opens the inbox over the tailnet.
- [ ] No queue, runner, `runproc`, `events` or `escalations` module imports
      `agentic.dashboard`.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

Integration level. Each child carries its own unit tests. Fakes alone have shipped broken
integrations in this repo, so every item below runs **real processes over real sockets**
unless it is marked manual.

- **Real HTTP against a running server** (`tests/test_dashboard_e2e.py`):
  - Start `nw serve --dashboard --bind 127.0.0.1:0` as a subprocess with `NW_DATA_DIR`
    pointing at a seeded test data dir: an active campaign, a parked `human_decision`
    campaign with an open `ESC-0001` that has options, findings and an unmet AC, and a
    `main_dirty` park. Read the port from `server.json`.
  - Use `urllib` for real requests:
    - `/api/snapshot` matches the schema, and its `totals.spend_today_usd` equals
      `dashboard_payload`'s;
    - `If-None-Match` returns 304;
    - `/api/stream` delivers a `snapshot` event, and a second one after a ledger change;
    - `/api/campaigns/<cid>/tail` streams lines appended to a fixture `stream.jsonl` and ends
      when the run is marked finished.
  - Waits use `tests/waits.py`.
- **Real end-to-end answer reaching a conductor follower:**
  - In the same data dir, start a real `nw queue events --follow --for conductor --all
    --no-code-watch` subprocess. POST plan, then answer (`--choose 2`) with the local token
    and CSRF header. Within `NW_TEST_WAIT_S` the follower's stdout carries one
    `escalation_answered` line for `ESC-0001` with `by: human`.
  - Twin check: a copy of the seeded data dir answered with `nw escalations answer ESC-0001
    --choose 2 --yes`. The escalation record, the ledger `human_decision` and `note` events,
    `human_overrides` and the event `data` are equal to the web-answered ones, modulo
    timestamps. The same twin check runs for a text answer and for an override (waive +
    attest + reason).
  - Conflicts:
    - answer again → 409;
    - withdraw `ESC-0002` with the CLI, then answer it from HTTP → 409;
    - in both cases the data dir is unchanged apart from the audit line.
- **Bind and auth refusal cases** (real subprocess and real sockets):
  - `--bind 0.0.0.0:0`, `[::]:0`, a LAN-looking address and `100.64.0.9:0` (with a fake
    `tailscale` on `PATH` reporting another own IP) each exit 2 with the rule, also with
    `--tls-cert/--tls-key`.
  - On loopback, no token → 401.
  - Tailnet identity is driven through a fake `tailscale` executable (`whois --json` returns
    a fixture). The server-side whois path is exercised by injecting a peer address, since
    real tailnet peers are impossible in CI. Cases:
    - unknown login → 403;
    - tagged node → 403;
    - whois exit 1 → 403;
    - allowed login → 200 with node name in the audit.
  - Missing `X-NW-CSRF`, wrong `Origin` or wrong `Host` → refused.
- **Read-only and load guards:**
  - After 2 minutes of snapshots, SSE and tails with no answer, a hash of every file under
    the data dir except `agentic/dashboard/` is unchanged.
  - A counter on the snapshot builder shows 0 builds with no clients, and at most one per
    `refresh_s` with 5 SSE clients and an unchanging watermark.
  - An import-graph test pins invariant 2.
- **Mobile viewport:**
  - Load the page in a headless Chromium (`NW_TEST_CHROMIUM`, else `chromium` /
    `google-chrome` on `PATH`, else the Playwright cache's headless shell; skipped only when
    none exists, and the child's DoD requires the run on this host) at 390×844 with
    `?selftest=1`. The page's self-test asserts there is no horizontal overflow, that the
    inbox and answer controls are visible and at least 44 px tall, and that SSE connected,
    and writes the result into the DOM read by `--dump-dom`.
  - Repeat at 360×740.
- **Lifecycle (child 4):** `nw dashboard unit --print` content test. A manual `systemctl
  --user` start/stop/restart-on-code-change check, recorded in the child spec.
- **Manual verification (human, real tailnet; recorded in this epic when done):**
  1. With Wi-Fi off on the phone (cellular only) and the Tailscale app connected, open
     `http://<host-tailnet-ip>:8791`. The dashboard loads and updates live.
  2. From a device on another login, or with the phone app disconnected, the dashboard is
     unreachable or gets 403.
  3. Answer a real escalation from the phone. The conductor wakes and applies it, and `nw
     escalations show` and `audit.jsonl` name the phone.
  4. Answer the same escalation in the CLI first, then from the phone → a clean "already
     answered" message.
  5. (Child 5) Add the page to the home screen, enable notifications, raise an escalation,
     and receive the content-free push.

## Rollout / sequencing

1. **Human steps (Tailscale, one time; nothing else in this epic needs the human):**
   - On the host: `sudo tailscale up` and log in. Then `sudo tailscale set
     --operator=$USER`, so that `tailscale whois/status/ip` work without root for the
     dashboard.
   - On the phone: install the Tailscale app and log in with the **same** account.
   - For the unit to run while logged out: `loginctl enable-linger $USER`. Then `nw
     dashboard unit --install` (child 4).
   - Child 5 only: in the Tailscale admin console, enable MagicDNS and HTTPS certificates.
     The host's MagicDNS name then appears in public Certificate Transparency logs. That is
     the one trade-off of push, and it is why push is opt-in. On iPhone, add the page to the
     home screen and tap "Enable notifications".
2. **Children 1 to 4 to 5**, as sequenced above. Partial delivery behaves as follows:
   - after child 1, the dashboard is read-only and the human still answers via CLI or TUI;
   - after child 2, the phone unblocks the conductor;
   - child 3 replaces the 15 s polling with push updates (polling remains the fallback);
   - until child 4, the human starts `nw serve --dashboard` by hand;
   - without child 5, the human relies on the existing `notify-send` and mail, plus looking
     at the dashboard.
3. No data migration. `agentic/dashboard/` is created on first start. The escalation and
   events formats are unchanged.
4. Docs: `docs/WORKFLOW.md` gains a "Remote dashboard" section (child 4). The `nw-orient`
   skill mentions `nw dashboard status`.

## Definition of done

- [ ] All child specs `done` or `superseded` (the linter gates this).
- [ ] Integration test plan executed; `uv run pytest` green.
- [ ] Human's real-tailnet manual checks 1-4 (and 5 once child 5 lands) recorded here.
- [ ] Acceptance criteria met; epic body reflects what shipped.
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

_None. Every design question is decided above. Whether `tailscale whois` works without root
on this host's tailscaled setup can only be checked after the human logs in. The Rollout
covers it with `tailscale set --operator=$USER`, and child 1 fails closed (403) until then._
