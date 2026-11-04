---
id: SPEC-0401
title: "Read-only dashboard server and nw.dashboard.v1 snapshot (nw serve --dashboard)"
status: proposed
owner: user
created: 2026-11-03
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0400
source_idea: IDEA-874
tags: [agentic, dashboard, remote, web]
depends_on: [SPEC-0222, SPEC-0232, SPEC-0334]
---

## Context

This is child 1 of the epic [SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md),
promoted from `IDEA-874`. The human asked for "a live web-based dashboard of active items so
that I can consult this while I am away from home", reachable over Tailscale only. This child
ships the read-only half: a server, its auth gates, the `nw.dashboard.v1` snapshot and a
phone-first page that polls it. Children 2 to 5 add answers, SSE and the run tail, the systemd
unit, and push. Each of them depends only on what this spec ships.

What exists today, checked against the code on 2026-11-03:

- `nw serve` (`src/nightwright/agentic/cli_worker.py:213`, `serve_cmd`) has one mode,
  `--jobs` (`required=True`, help text "the only mode so far"), with `--bind` defaulting to
  `127.0.0.1:8790`. Its server is `pull_jobs.make_server`
  (`src/nightwright/agentic/pull_jobs.py:973`): a stdlib `ThreadingHTTPServer` subclass with a
  per-request socket timeout (`HANDLER_TIMEOUT_S`). Its `check_bind`
  (`pull_jobs.py:915`) accepts loopback and the whole Tailscale range without TLS, and anything
  with TLS. That gate is too loose for the dashboard: any tailnet IP, and wildcards with TLS.
- `queue.dashboard_payload` (`src/nightwright/agentic/queue.py:6811`,
  `nw.queue.dashboard.v1`) already carries what `nw queue watch` draws: `lease` (with
  `draining`), `runner_code`, `slots`, `gate_pools`, `active`, `needs_you`,
  `conductor_handling`, `waiting`, `backlog`, `delivered` and `totals.spend_today_usd`. It
  reports ages (`updated_age_s`, `state_age_s`, `park_age_s`, `heartbeat_age_s`,
  `finished_age_s`), not timestamps.
- `events.reader_holder` / `events.followers` (`src/nightwright/agentic/events.py:983`,
  `:1005`) give the conductor follower pid and lag and the steward followers. They read only:
  `reader_holder` takes and immediately releases a non-blocking `flock` on an existing lock
  file and never creates one.
- `cli_conduct.conduct_status` calls `events.sync`, which writes, and reads
  `read_since(cfg, 0)` over the whole event log. The dashboard must use neither.
  `cli_conduct._herdr_watch_status` is a safe read.
- `escalations.list_all(cfg, "open")` (`src/nightwright/agentic/escalations.py:78`) lists open
  escalations. The cheap `escalations_signature(cfg)` (stat calls only) lives in the TUI
  (`src/nightwright/tui/agentic_model.py:1024`).
- `tests/test_herdr.py:506` (`test_queue_runner_runproc_never_import_herdr`) is the
  import-graph precedent for invariant 2.
- There is no web framework and no `nw dashboard` command group.

## Goals / Non-goals

**Goals**
- G1. `nw serve --dashboard` serves the dashboard on this host's own tailnet IP (default) or on
  loopback, and nowhere else.
- G2. Every data request is authenticated: tailnet identity via `tailscale whois` on a tailnet
  bind, a per-host bearer token on loopback. Every check fails closed.
- G3. One `nw.dashboard.v1` snapshot, built only from the existing payload builders, computed
  at most once per `refresh_s` and only when a cheap watermark moved (or `max_stale_s`
  passed), shared by every client, served with an ETag.
- G4. A phone-first page shows health, active campaigns with cost vs cap, counts, needs-you,
  recent events and today's spend, and refreshes itself every 15 s with a conditional GET.
- G5. `nw dashboard snapshot [--json]` prints the same snapshot from the CLI, without a server.
- G6. The read-only invariant and the no-import invariant of the epic are pinned by tests.

**Non-goals**
- Answering escalations, the plan/answer endpoints, CSRF tokens and the full inbox entry
  (options, findings, unmet ACs, effects): child 2. This spec ships only the summary fields
  of each open escalation.
- SSE, the background snapshotter thread, `max_clients`, the campaign drilldown and the run
  tail: child 3.
- The systemd unit, code-change restart, `nw dashboard status` and the `conduct-status`
  `dashboard` key: child 4.
- TLS, `tailscale cert`, service worker, push: child 5. The dashboard refuses TLS flags until
  then.
- Changing `nw serve --jobs` behaviour. Only its option wiring changes.

## Decision

1. **`nw serve` gets a second, mutually exclusive mode.** `--jobs` loses `required=True`.
   `--dashboard` is added. Exactly one of the two must be given, otherwise usage error, exit
   2. `--bind` defaults to `None` and resolves per mode: `127.0.0.1:8790` for `--jobs`
   (unchanged) and `agentic.dashboard.bind` (default `tailscale:8791`) for `--dashboard`.
   `--tls-cert`, `--tls-key` and `--client-ca` stay `--jobs`-only. With `--dashboard` they
   are refused (exit 2), but only **after** the bind gate has run, so a bad bind with TLS
   flags reports the bind rule.
2. **A strict bind gate of its own** (`dashboard/auth.check_bind`), not `pull_jobs.check_bind`.
   It accepts loopback, or an address listed by `tailscale ip` (v4 and v6) for this host.
   `tailscale` and `tailscale:PORT` resolve to the first address of `tailscale ip -4`.
   Wildcards, LAN addresses, `localhost` names other than the loopback literals, other tailnet
   IPs and non-IP hosts are refused with exit 2 and a message that names the rule. There is
   no flag or config key that bypasses it. If `tailscale` is missing, logged out or prints no
   address, `--bind tailscale` exits 1 with "tailscale has no address for this host (run
   `sudo tailscale up`)".
3. **Two auth modes, chosen from the bound address only.**
   - Loopback bind → **token mode**. `<data_dir>/agentic/dashboard/local.token` (32 random
     bytes, url-safe, mode 0600, created with `O_EXCL` on first start) must arrive as
     `Authorization: Bearer <token>`, compared with `hmac.compare_digest`. A missing or wrong
     token gets 401. The static shell (`/`, `/static/*`) holds no data and is served without
     auth. At start the server prints the loopback URL with `#token=<token>` in the
     fragment, which the page moves to `localStorage` and strips from the address bar.
   - Tailnet bind → **whois mode**. Every request, static ones included, runs
     `tailscale whois --json <peer-ip>` (3 s timeout). The request is allowed only when
     `UserProfile.LoginName` is in the allowed set and the node is not tagged (`Node.Tags`
     non-empty, or no `UserProfile`). The allowed set is `agentic.dashboard.allowed_logins`,
     or, when that list is empty, the host's own login (`tailscale status --json` →
     `Self.UserID` → `User[<id>].LoginName`), resolved once at start and re-resolved with the
     whois cache. Allowed results are cached per peer IP for 60 s. Failures (non-zero exit,
     timeout, unparsable JSON, missing fields, own-login lookup failing) are 403 and cached
     for 5 s so a failing peer cannot make the server fork per request. Nothing is cached
     across a server restart.
4. **Host and Origin checks on every request**, before auth. `Host` must be one of: the bound
   `addr:port` (IPv6 in brackets), the host's MagicDNS name (`Self.DNSName` without the
   trailing dot) with the port, and on loopback `localhost:port`. `Origin`, when present,
   must be `http://` plus one of those. A mismatch is 403. This blocks DNS rebinding.
5. **Refusals are audited** in `<data_dir>/agentic/dashboard/audit.jsonl`
   (`nw.dashboard.audit.v1`, `action: "refused"`, `result: 401|403`, with the reason, the
   peer IP, the whois login and node name when known, and the user agent). Identical
   refusals (same peer IP and reason) are written at most once per 10 s, which bounds the
   file under a retry storm. Child 2 adds `plan` and `answer` records to the same file and
   module.
6. **The snapshot is computed lazily and shared** (`dashboard/snapshot.SnapshotCache`). It
   has no thread in this child. On a request it stats the watermark at most once per
   `refresh_s` (5 s). It rebuilds only when the watermark moved or the cached snapshot is
   older than `max_stale_s` (30 s). One lock makes concurrent requests wait for a single
   build. With no requests the server does no queue reads at all. Child 3 puts a snapshotter
   thread in front of the same cache.
7. **The snapshot reuses the existing builders.** It never calls `events.sync`,
   `events.follow`, `read_since(cfg, 0)`, `conduct_status`, `store.write_state`, `mailbox`
   writes or any queue mutation. If a builder turns out to write, the fix is a read-only path
   in that builder, not a fork in the dashboard.
8. **Ages stay ages; `rev` ignores them.** `dashboard_payload` reports `*_age_s`. Rather than
   fork it into timestamps, the snapshot keeps them and carries `generated_at`. The page
   shows `age_s + (now − generated_at)`, which stays correct while the client holds a cached
   snapshot. `rev` is the sha256 (first 16 hex chars) of the canonical JSON
   (`sort_keys=True`) of the snapshot with `generated_at`, `rev` and every key ending in
   `_age_s` removed. A rebuild that only moved ages therefore keeps the same `rev`, and
   clients get 304. `rev` is the strong ETag.
9. **The page is static package data** under `src/nightwright/agentic/dashboard/static/`
   (`index.html`, `app.js`, `app.css`), loaded with `importlib.resources`. There is no build
   step, no CDN, no inline script. The CSP is
   `default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'`.
   It polls `GET /api/snapshot` with `If-None-Match` every 15 s and immediately on
   `visibilitychange` to visible.
10. **`nw dashboard snapshot [--json]`** builds the snapshot in-process with the same builder
    and prints it (`--json`) or a short text summary (health line, counts, spend today, open
    escalations). It needs no server, no tailscale and no token.

## Design

### Modules

New package `src/nightwright/agentic/dashboard/`:

- `__init__.py`: `settings(cfg)` reads `agentic.dashboard` with defaults
  `{bind: "tailscale:8791", allowed_logins: [], refresh_s: 5, max_stale_s: 30}`. Malformed
  values fall back to the default per key, like `escalations.settings`. Keys of later children
  (`max_clients`, `tail_poll_s`, `push`) are accepted and ignored here. Also `dashboard_dir(cfg)`
  → `<data_dir>/agentic/dashboard/`, created on first use with mode 0700.
- `auth.py`:
  - `run_tailscale(*args, timeout=3.0)`: subprocess wrapper that resolves `tailscale` on `PATH`
    (tests put a fake first), returns parsed stdout or raises `TailscaleError`.
  - `own_tailnet_ips() -> list[str]` from `tailscale ip`; `resolve_bind(spec) -> (host, port)`
    handling `tailscale` / `tailscale:PORT` / `ADDR:PORT` / `[v6]:PORT`.
  - `check_bind(host, own_ips) -> str | None`: `None` for loopback literals (`127.0.0.0/8`,
    `::1`) or a member of `own_ips`, else the refusal reason. The reasons name the rule, for
    example `0.0.0.0 is a wildcard; the dashboard binds only loopback or this host's own
    tailnet address (100.101.1.2)`.
  - `local_token(cfg, create=False)`.
  - `class Gate`: `mode` (`"token"`/`"whois"`), `allowed_hosts`, `authorize(peer_ip, headers)
    -> Verdict(ok, status, reason, login, node, node_ip)`, the whois cache (allowed results
    60 s, failures 5 s) and `check_host_origin(headers)`.
- `audit.py`: `append(cfg, record)` writes one JSON line under a file lock (`O_APPEND`),
  adding `ts`. The `refused` throttle is a dict keyed `(peer_ip, reason)` in the server.
- `snapshot.py`:
  - `watermark(cfg) -> tuple`: `(size, mtime_ns)` of `events.events_path(cfg)`,
    `escalations.signature(cfg)` and the `mtime_ns` of `lease.lease_path(cfg)`. Missing files
    count as `None`.
  - `build(cfg, now=None) -> dict`: one `nw.dashboard.v1` document (schema below).
  - `RECENT_TYPES`: `merged`, `handed_off`, `abandoned`, `run_failed`, `parked`, `resumed`,
    `escalation`, `escalation_answered`, `escalation_withdrawn`, `runner_dead`, `lease_stale`,
    `cost_warning`, `worker_failed`. A test asserts this is a subset of `events.EVENT_TYPES`.
  - `recent_events(cfg, n=20)`: reads at most the last 256 KiB of `events.jsonl` (seek, drop
    the first partial line), parses lines, keeps `RECENT_TYPES` and returns the last `n`.
  - `class SnapshotCache(cfg, clock=time.monotonic)`: `get() -> dict`, plus a `builds` counter
    and a `watermark_checks` counter for the tests.
- `server.py`:
  - `make_server(cfg, bind=None, *, auth_mode=None)` returns a `DashboardServer`
    (`ThreadingHTTPServer`, `daemon_threads=True`, per-request socket timeout
    `pull_jobs.HANDLER_TIMEOUT_S`).
  - `auth_mode` is a Python-only keyword for tests: it forces `"whois"` on a loopback socket
    so the whois path can be driven over a real socket. No CLI option or config key reaches
    it, and a test greps `cli_worker.py` / `cli_dashboard.py` to keep it that way.
  - The handler serves `GET /`, `GET /static/<name>` (names from a fixed allow-list),
    `GET /api/snapshot` and `GET /api/health` (`{"ok": true}`, auth-gated, for child 4's
    self-check). Any other path is 404 and any other method 405. Request bodies are not read.
  - Every response carries `Cache-Control: no-store` (API) or an ETag (static), the CSP,
    `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.
  - After binding, the server writes `<dashboard_dir>/server.json`
    (`nw.dashboard.server.v1`: `pid`, `host`, `port`, `url`, `auth`, `started_at`) atomically
    and removes it on clean exit. Child 4 extends this record.
- `src/nightwright/agentic/cli_dashboard.py`: the `nw dashboard` group with `snapshot
  [--json]`, registered on the root CLI next to the other agentic groups. Child 4 adds
  `status` and `unit`.
- `cli_worker.serve_cmd`: the mode switch from Decision 1. `--dashboard` calls
  `dashboard.server.make_server`, prints `nw serve: dashboard on http://<host>:<port>` (and the
  `#token=` URL in token mode) to stderr, and serves until SIGINT/SIGTERM.
- `escalations.signature(cfg)`: `escalations_signature` moves from `tui/agentic_model.py` into
  `escalations.py` unchanged. The TUI calls it from there, which keeps one implementation.

### `nw.dashboard.v1`

```
{schema: "nw.dashboard.v1", generated_at, rev,
 health: {runner: {lease, runner_code, draining},          # dp.lease, dp.runner_code, lease.draining
          conductor: {pid, lag, cursor_age_s},             # reader_holder / followers row "conductor"
          stewards: [{address, pid, lag}],                 # followers kind == "mailbox", held
          herdr_watch},                                    # cli_conduct._herdr_watch_status
 totals,                                                   # dp.totals (spend_today_usd as-is)
 slots, gate_pools,                                        # dp
 active, needs_you, conductor_handling, waiting,           # dp, unchanged rows
 backlog: {count, top: [≤5 dp.backlog rows]},
 delivered,                                                # dp
 recent_events: [≤20 {id, ts, type, campaign, spec_id, project, by, summary}],
 inbox: {escalations: [{id, created, by, kind, question, campaign, spec_id, project}],
         parks: [needs_you rows whose park reason is main_dirty or main_not_on_base]}}
```

`dp` is `queue.dashboard_payload(cfg, now=now)`. `summary` is the event's one-line text form
as `nw queue events --text` prints it. If a text helper is not importable without side
effects, `summary` is `type` plus the campaign label. Child 2 extends each `inbox.escalations`
entry with options, recommendation, facts, findings, unmet ACs and effects, and the parks with
their host fix. Every builder call is wrapped so that one failing section becomes
`{"error": "<message>"}` in that section rather than a 500 for the whole snapshot.

### `GET /api/snapshot`

The flow is: Host/Origin check → `Gate.authorize` → `SnapshotCache.get()`. Then:
- `If-None-Match` equal to `"<rev>"` → 304 with no body;
- otherwise 200 with `application/json; charset=utf-8` and `ETag: "<rev>"`.

### The page

`index.html` is a single column built for 360–430 px wide screens. Tap targets are at least
44 px, and it uses system fonts with light and dark themes from `prefers-color-scheme`. Its
sections, top to bottom:

1. Header: overall status dot (red if the runner lease is stale or dead, or the conductor pid
   is missing; amber if draining or anything needs you; green otherwise), "updated Ns ago"
   and spend today.
2. **Needs you**: open escalations (id, campaign, question) and the human-owned parks, or
   "nothing waiting".
3. **Health**: runner (holder, heartbeat age, draining, code stale), conductor (pid, lag),
   stewards, herdr watch.
4. **Active**: one card per campaign with label, state, loop `n/max`, persona/activity and a
   cost-vs-cap bar (`cost_usd` / `cap_usd`, text when `cap_usd` is null).
5. **Counts**: active, queued, parked, waiting, backlog (ready/blocked), conductor-handling.
6. **Recent**: the last 20 events with type and age.

`app.js` uses `fetch` with `If-None-Match` and renders with `textContent` only (no
`innerHTML` of data). On 401 it shows "token missing" and on 403 "not allowed on this device".
If a fetch fails it shows a stale banner with the age of the last good snapshot.
`?selftest=1` runs, after the first successful render, an in-page check: no horizontal
overflow (`scrollWidth <= innerWidth`), the Needs-you and Active sections visible, every
button or link at least 44 px tall, and the snapshot loaded. It writes
`<pre id="selftest">{"ok": …, "failures": [...]}</pre>` into the DOM.

## Alternatives considered

- **Reuse `pull_jobs.check_bind`.** It accepts any address in `100.64.0.0/10` and wildcards
  with TLS, which the epic forbids. A separate, stricter gate keeps the job API unchanged.
- **One `nw serve` process serving both modes.** Rejected by the epic: the trust models
  differ (bearer tokens and optional wildcard with TLS vs. tailnet identity and never a
  wildcard).
- **Converting ages to timestamps in the snapshot.** `now − age` jitters by up to a second
  between builds, which would change `rev` on every rebuild. Excluding `*_age_s` from the hash
  and deriving display ages from `generated_at` gives a stable ETag without forking the
  builder.
- **A snapshotter thread now.** Polling at 15 s does not need one, and a lazy cache already
  guarantees zero work with zero clients. Child 3 adds the thread when SSE needs a push
  source.
- **Building from `conduct_status`.** It calls `events.sync` (writes) and reads the whole
  event log. Rejected by invariant 1.
- **`tailscale serve` identity headers.** Rejected by the epic: any local process can forge
  them behind a loopback proxy.

## Acceptance criteria

- [ ] `nw serve` with neither `--jobs` nor `--dashboard`, or with both, exits 2 with a usage
      message. `nw serve --jobs` keeps its default bind `127.0.0.1:8790` and its existing
      behaviour (its existing tests pass unchanged).
- [ ] `nw serve --dashboard --bind X` exits 2 with a message naming the "loopback or this
      host's own tailnet address" rule for X in {`0.0.0.0:0`, `[::]:0`, `192.168.1.10:0`,
      `100.64.0.9:0` while `tailscale ip` reports a different own address, `example.com:0`}.
      The same holds with `--tls-cert/--tls-key` added. With a valid bind plus TLS flags it
      exits 2 saying TLS is not supported for the dashboard yet.
- [ ] `nw serve --dashboard` (default `--bind tailscale`) binds the first `tailscale ip -4`
      address on port 8791. With `tailscale` absent or printing nothing it exits 1 with the
      "tailscale has no address" message.
- [ ] On a loopback bind, `GET /api/snapshot` without a token or with a wrong token returns
      401. With the token from `local.token` (mode 0600) it returns 200. `GET /` returns
      200 without a token.
- [ ] In whois mode, a request whose whois login is not allowed, whose node is tagged, or for
      which `tailscale whois` exits non-zero or times out returns 403 and writes one
      `refused` line to `audit.jsonl`. This applies to static paths as well. An allowed login
      returns 200. With `allowed_logins: []` the allowed login is the host's own
      (`Self.UserID`). A second request within 60 s from the same allowed peer does not run
      `tailscale whois` again.
- [ ] A request with a `Host` that is not the bound address, the MagicDNS name or (on
      loopback) `localhost`, or with a foreign `Origin`, returns 403.
- [ ] `GET /api/snapshot` returns a document with every top-level key of the `nw.dashboard.v1`
      schema above. Its `totals.spend_today_usd` equals `queue.dashboard_payload`'s for the
      same data. Its `inbox.escalations` lists exactly the open escalations, and its
      `inbox.parks` lists exactly the `main_dirty` / `main_not_on_base` parks.
- [ ] A repeat request with `If-None-Match: "<rev>"` returns 304 while nothing changed, even
      after a rebuild forced by `max_stale_s`. After a ledger or event change and
      `refresh_s`, it returns 200 with a new `rev`.
- [ ] `SnapshotCache` performs 0 builds and 0 watermark checks with no requests. With many
      concurrent requests within one `refresh_s` and an unchanged watermark it performs at most
      one build. It rebuilds when the watermark moves (after `refresh_s`) or when `max_stale_s`
      passes.
- [ ] `recent_events` reads at most the last 256 KiB of `events.jsonl` and returns at most 20
      events of `RECENT_TYPES`, which is a subset of `events.EVENT_TYPES`.
- [ ] Serving the page and snapshots, with rebuilds forced, leaves every file under the data
      dir outside `agentic/dashboard/` byte-identical. The only files the server creates are
      `local.token`, `audit.jsonl` and `server.json` under `agentic/dashboard/`.
- [ ] No module in `agentic/queue.py`, the runner, `runproc`, `events`, `escalations`, `lease`,
      `store` or `mailbox` imports `nightwright.agentic.dashboard` (directly or transitively).
- [ ] The page at 390×844 and 360×740 in headless Chromium with `?selftest=1` reports
      `ok: true`: no horizontal overflow, Needs-you and Active visible, tap targets ≥ 44 px,
      snapshot loaded. The page makes no request to any origin other than its own (no
      external URL in `static/`).
- [ ] `nw dashboard snapshot --json` prints a valid `nw.dashboard.v1` document, needs no
      running server, and writes nothing outside `agentic/dashboard/`. Without `--json` it
      prints the text summary.
- [ ] `escalations.signature` exists, and the TUI escalation inbox uses it with unchanged
      behaviour.
- [ ] `python3 tools/spec_lint.py` exits 0 and the affected tests pass.

## Test plan

**Automated tests.** Real processes and sockets wherever the epic requires them. Waits use
`tests/waits.py` (`NW_TEST_WAIT_S`), never fixed sleeps.

- `tests/fixtures/fake_tailscale.py` plus a `tests/fixtures/bin/tailscale` shim placed first on
  `PATH`. It answers `ip`, `ip -4`, `status --json` and `whois --json <ip>` from a JSON fixture
  named by `NW_FAKE_TAILSCALE`, which can set: own IPs, own login, per-IP whois result, tagged,
  exit code, sleep (for the timeout case), and a call log file (to count whois calls).
- `tests/fixtures/dashboard_data.py`: seeds a temp `NW_DATA_DIR` with one active campaign
  (with cost and cap), a PARKED `human_decision` campaign with open `ESC-0001`, a `main_dirty`
  park, a delivered (merged) campaign, a lease file and an `events.jsonl` larger than 256 KiB
  whose tail holds the recent events.
- `tests/test_dashboard_auth.py` (unit):
  - `check_bind` accept/refuse table (loopback v4/v6, own v4/v6, wildcard v4/v6, LAN, foreign
    100.x, hostname) and the rule text;
  - `resolve_bind` forms;
  - `Gate.authorize` in whois mode against the fake (allowed, unknown login, tagged, no
    UserProfile, exit 1, timeout, bad JSON) with the 60 s / 5 s cache under an injected clock;
  - the own-login default from `status --json`;
  - `check_host_origin` table;
  - token compare;
  - `local.token` created 0600 and reused.
- `tests/test_dashboard_snapshot.py` (unit):
  - `build` on the fixture has every schema key;
  - `totals.spend_today_usd` equals `dashboard_payload`'s;
  - the inbox escalations and parks sets;
  - `recent_events` bound (a patched `open` records the seek offset ≥ size − 256 KiB) and
    `RECENT_TYPES ⊆ EVENT_TYPES`;
  - `rev` unchanged when only `*_age_s` / `generated_at` differ and changed when a cost
    changes;
  - one failing builder section yields `{"error": …}` and the rest still builds;
  - `SnapshotCache` with an injected clock: 0 builds and 0 checks with no `get`; 20 threads
    × 10 `get()` within `refresh_s` → 1 build; a watermark move before `refresh_s` → still
    cached; after `refresh_s` → rebuild; `max_stale_s` → rebuild with the same `rev`.
- `tests/test_dashboard_e2e.py` (real subprocess, real HTTP via `urllib`):
  - Start `uv run nw serve --dashboard --bind 127.0.0.1:0` with the seeded `NW_DATA_DIR` and
    read the port from `server.json`;
  - no token → 401, token → 200 with schema, `If-None-Match` → 304;
  - append a ledger event plus an `events.jsonl` line, wait past `refresh_s` (config set to
    0.2 s), get 200 with a new `rev`;
  - `GET /` and `/static/app.js` without a token → 200 with the CSP header;
  - wrong `Host` → 403, foreign `Origin` → 403, `POST` → 405, unknown path → 404;
  - SIGTERM → exit 0 and `server.json` removed.
- `tests/test_dashboard_bind_cli.py` (real subprocess):
  - each refused bind from the AC exits 2 with the rule text, also with `--tls-cert/--tls-key`
    pointing at dummy files;
  - with the fake reporting own IP `127.0.0.1` the default `--bind tailscale:0` starts, and
    with the fake printing nothing it exits 1;
  - neither or both of `--jobs` / `--dashboard` → exit 2;
  - `--dashboard` with a valid bind plus TLS flags → exit 2.
- `tests/test_dashboard_whois_e2e.py`: an in-process `make_server(cfg, "127.0.0.1:0",
  auth_mode="whois")` in a thread, with real `urllib` requests and the fake `tailscale` on
  `PATH`. Cases:
  - unknown login, tagged node, whois exit 1 and whois timeout → 403 plus one `refused` audit
    line each;
  - allowed login → 200;
  - a second request within 60 s → the fake's call log shows one whois call;
  - 10 identical refusals within 10 s → one audit line;
  - static paths are gated too.

  A grep assertion checks that `auth_mode` appears in neither `cli_worker.py` nor
  `cli_dashboard.py`.
- `tests/test_dashboard_readonly.py`:
  - hash every file under the seeded data dir except `agentic/dashboard/`;
  - run the real server with `refresh_s: 0.1`, `max_stale_s: 0.2` and make 100 mixed requests
    (snapshot, conditional snapshot, static) over at least 20 s so that rebuilds happen;
  - run `nw dashboard snapshot --json`;
  - re-hash: identical. `agentic/dashboard/` holds only `local.token`, `audit.jsonl`,
    `server.json`.
- `tests/test_dashboard_imports.py`: in the style of `test_queue_runner_runproc_never_import_herdr`,
  import each listed queue-side module in a fresh interpreter and assert
  `nightwright.agentic.dashboard` is not in `sys.modules`. Also assert the same statically with
  `ast` over those modules' sources.
- `tests/test_dashboard_page.py`: headless Chromium (`NW_TEST_CHROMIUM`, else `chromium` /
  `google-chrome` on `PATH`, else the Playwright cache's headless shell; skipped only when
  none exists).
  - Run `--headless --window-size=390,844 --dump-dom` against
    `http://127.0.0.1:<port>/?selftest=1#token=<token>` on the e2e server, parse `#selftest`
    and assert `ok: true`. Repeat at 360×740.
  - A static check asserts no `http://` / `https://` / `//` external URL in `static/*` and no
    inline `<script>` body.
- `tests/test_dashboard_cli.py`: `nw dashboard snapshot --json` validates against the schema
  keys; the text form contains the spend and counts lines.
- The existing `nw serve --jobs` tests (SPEC-0334) and the TUI escalation inbox tests
  (SPEC-0250) run unchanged as regression guards.

**Manual verification (on this host, recorded in this spec's As shipped).**
1. `uv run nw serve --dashboard --bind 127.0.0.1:8791`, then open the printed `#token=` URL in
   a desktop browser with a mobile viewport. The page renders and refreshes every 15 s.
2. Once the human has run `sudo tailscale up` and `sudo tailscale set --operator=$USER`:
   - `uv run nw serve --dashboard` binds the tailnet IP;
   - the phone on cellular with the Tailscale app connected loads it;
   - `audit.jsonl` stays empty for the phone;
   - with the Tailscale app disconnected the page is unreachable.

   Until the human logs in, this step is recorded as pending, and the server fails closed
   (exit 1 on `--bind tailscale`).

**Regression guard.** Affected tests via `python3 tools/affected_tests.py --run uv run pytest
-q`. The queue's close gate runs the affected set.

## Rollout / migration

1. Land the package, the `serve` mode switch, `escalations.signature` (TUI delegates) and
   the tests in one change. There is no data migration: `agentic/dashboard/` is created on
   first start.
2. Until child 4, the human starts `nw serve --dashboard` by hand. Until child 2, answers stay
   in the CLI and TUI.
3. The `nw-orient` skill and the WORKFLOW "Remote dashboard" section come with child 4. This
   child's `--help` texts describe the mode and the bind rule.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; the new tests and the affected tests pass; manual step 1 performed,
      step 2 performed or recorded as pending on the human's Tailscale login.
- [ ] No regressions (`nw serve --jobs` and TUI escalation tests green).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

_None. The one external unknown, whether `tailscale whois` runs without root on this host,
is covered by `tailscale set --operator=$USER` in the epic's rollout. Until then the gate
fails closed._
