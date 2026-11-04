---
id: SPEC-0432
title: "Dashboard default bind: loopback always plus the tailnet address whenever one exists"
status: done
owner: user
created: 2026-11-04
updated: 2026-11-04
milestone: "M9: agentic workflow"
source_idea: IDEA-974
kind: feature
parent: SPEC-0400
depends_on: [SPEC-0416, SPEC-0417, SPEC-0427, SPEC-0428]
tags: [agentic, dashboard, tailscale, lifecycle, systemd]
---

## Context

Promoted from idea `IDEA-974`. Child of epic
[SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md).

Observed live on 2026-11-04. The human ran `nw dashboard install --bind tailscale` while Tailscale
was logged out on the host (`tailscaled` active, `tailscale status`: "Logged out"). The server
waited `--tailnet-wait 60`, exited 3, and systemd restarted it (`NRestarts` 14 and climbing).
`nw dashboard status` said "server not running (no live server.json) … problem: unit active but
no live server.json (starting, or waiting for a tailnet address)". Nothing was served at all,
not even on localhost. One server binds one address, so the human had to choose between loopback
and tailnet.

The human decided that one dashboard service serves loopback **always** and the host's tailnet
address **whenever one exists**, without anyone having to choose.

This spec changes parts of two `done` specs. Neither is replaced as a whole, so neither is marked
superseded; the parts changed are:

- [SPEC-0428](./0428-dashboard-loopback-without-a-token-host-allowlist.md) Decision 1 ("two auth
  modes, chosen by the bound address") and Decision 2 ("the default bind stays `127.0.0.1:8791`;
  remote use is `--bind tailscale`"). Auth modes are now chosen **per listening socket** of one
  process, and the default bind is the combined one. Decisions 3 to 7 (Host allowlist, no CORS,
  same-origin + CSRF, no token, audit identity) carry forward unchanged, per listener.
- [SPEC-0427](./0427-dashboard-service-nw-dashboard-install-uninstall.md): the `install --bind`
  default (`loopback` becomes `auto`), the status text for a server that waits for a tailnet
  address, and the role of exit 3 (now only in the explicit tailnet-only mode). The unit text,
  restart policy, uninstall and the rest of status are unchanged.
- [SPEC-0416](./0416-dashboard-tailnet-slice-spec-0400-child-follows.md) /
  [SPEC-0417](./0417-dashboard-tailnet-whois-identity-gate-and-tailnet.md): `resolve_bind` and
  `auth.Gate` are reused unchanged.

## Goals / Non-goals

**Goals**
- G1. With no `--bind`, `nw serve --dashboard` and the `nw dashboard install` unit serve
  `127.0.0.1:PORT` at once and also the host's own tailnet `IP:PORT` while one exists. Both
  listeners share one process, one snapshot cache, one SSE hub (and its slot pool), one CSRF
  secret and one audit log.
- G2. A logged-out or absent Tailscale never stops loopback serving and never causes an exit or a
  systemd restart. The tailnet listener appears and disappears live.
- G3. The auth mode cannot be mixed up: it is fixed by the listening socket, never by a request
  header.
- G4. `server.json`, `nw dashboard status` and the one-line brief show both listeners and say
  why the tailnet one is waiting, with the fix.

**Non-goals**
- HTTPS, IPv6 tailnet listeners (the first own tailnet address, IPv4 first, is bound, as
  `resolve_bind` does today), LAN binds.
- Changing the human's unit or Tailscale login: the human re-runs `nw dashboard install`.

## Decision

1. **The default bind is `auto[:PORT]`.** `nw serve --dashboard` with no `--bind`, or with
   `--bind auto` / `auto:PORT`, runs a loopback listener on `127.0.0.1:PORT` (default 8791) and
   a tailnet supervisor for the same port. `--bind 127.0.0.1:P` (or `localhost`, `[::1]`) is
   loopback only. `--bind tailscale[:P]` or an own tailnet IP is tailnet only and keeps the
   SPEC-0427 behavior: wait up to `--tailnet-wait`, then exit 3.
2. **Each listener is its own socket with a fixed mode.** The loopback listener is mode `local`
   (SPEC-0428 local rules: no login, Host allowlist `127.0.0.1` / `localhost` / `[::1]` with the
   port, CSRF on writes). The tailnet listener is mode `whois` (the whois `Gate` on every request,
   CSRF on writes, Host allowlist = the tailnet IP and the MagicDNS name and its first label, with
   the port). The mode is an attribute of the server object that owns the socket, passed
   explicitly; a `local`-mode server refuses to be constructed on a non-loopback address. A
   request on the tailnet socket with `Host: 127.0.0.1:P` is `403 bad_host`, and a request
   on the loopback socket with the tailnet Host is `403 bad_host`.
3. **The tailnet supervisor** checks `tailscale ip` every `agentic.dashboard.tailnet_recheck_s`
   seconds (default **15**, valid range (0, 300]; 15 s puts the tailnet listener up within 15 s
   of `tailscale up` at about four cheap subprocess calls a minute). When an own tailnet address
   appears it binds `IP:PORT` live; when the address disappears or changes it closes that
   listener (its socket, its open connections and its open streams) and keeps loopback serving,
   then rebinds to the new address if there is one. A failed bind (`EADDRINUSE`, ...) is a
   waiting reason, retried at the next check. Each state change is logged once:
   - `nw serve: tailnet: waiting for a Tailscale address (logged out)`
   - `nw serve: tailnet: listening on http://100.x.y.z:8791 (tailscale whois identity gate)`
   - `nw serve: tailnet: address gone, listener closed (loopback keeps serving)`
4. **The waiting reason** comes from `tailscale status --json` `BackendState` when `tailscale ip`
   has no address: `NeedsLogin` / `NoState` → `logged out`, `Stopped` → `stopped`, `Starting` →
   `starting`, a status that cannot be read → `tailscaled not running`, no binary →
   `tailscale not installed`. Each reason carries a fix hint: `sudo tailscale up` (logged out,
   stopped), `sudo systemctl start tailscaled` (not running), `install Tailscale` (missing).
5. **`server.json`** gains `listeners`, a list with one entry per listener:
   `{"name": "local"|"tailnet", "auth": "local"|"whois", "state": "listening"|"waiting",
   "url": str|null, "reason": str|null, "hint": str|null}`, rewritten when a listener changes
   state. The top-level `url` / `host` / `port` / `auth` stay the primary listener (loopback in
   `auto` mode) so older readers keep working. In tailnet-only mode the server writes a record
   with a single `waiting` listener and `url: null` while it waits, so status can say why
   instead of "no live server.json".
6. **`nw dashboard status`** prints one line per listener:
   `local    http://127.0.0.1:8791/ (no login)`,
   `tailnet  http://100.x.y.z:8791/ (whois)` or
   `tailnet  waiting: Tailscale logged out — run: sudo tailscale up`. A waiting tailnet listener
   in `auto` mode is not a problem (exit 0). A tailnet-only server that is waiting **is** a
   problem (nothing is served), and the problem names the fix and `nw dashboard install` for the
   default bind. "Unit active but no live server.json" now only means "starting".
7. **`nw dashboard install`** `--bind` choices are `auto` (default), `loopback`, `tailscale`. The
   default unit runs `nw serve --dashboard --bind auto:PORT` (no `--tailnet-wait`).
   `After=tailscaled.service` is added for `auto` and `tailscale` when such a unit exists
   (ordering only; harmless). Re-running install over an existing unit prints what changed
   (`bind tailscale:8791 (tailnet only) -> auto:8791 (loopback + tailnet)` and the changed lines),
   or `unit unchanged`.

## Design

- `dashboard/server.py`:
  - `DashboardServer(cfg, addr, cache, *, mode=None, gate=None, peer=None, hub=None, csrf=None)`:
    `hub` / `csrf` shared when given (a shared hub is not closed by `server_close`). The server
    tracks its open connections and `server_close` shuts them down and sets `closed`, which the
    snapshot and tail stream loops check, so closing the tailnet listener ends its streams.
    `mode="local"` on a non-loopback address raises `ValueError`.
  - `auto_port(spec) -> int | None`: the port of `auto` / `auto:PORT` (None for other specs).
  - `tailnet_recheck_s(cfg)`.
  - `TailnetSupervisor(cfg, port, *, cache, hub, csrf, recheck_s, on_change, log, own=None,
    reason=None)`: `poll()` runs one check (tests call it directly); `start()` runs it on a
    daemon thread every `recheck_s` until `stop()`; `listener()` is the current entry for
    `server.json`. `own` / `reason` are test seams (`own_tailnet_ips` / `tailnet_reason` by
    default) that the CLI never passes.
- `dashboard/auth.py`: `tailnet_reason(run=run_tailscale) -> (reason, hint)` (Decision 4). The
  supervisor's `tailscale` forms are `ip` and `status --json` (the gate already runs the latter).
- `cli_worker._serve_dashboard`: `auto` mode resolves the loopback bind through `resolve_bind`
  (SPEC-0416 seam rule), builds the loopback server with a shared hub + CSRF, writes
  `server.json` with both listeners, starts the supervisor, and on exit stops the supervisor,
  closes both listeners and then the hub. The tailnet-only path keeps its wait loop and writes the
  waiting record while it waits (removed again on exit 3).
- `dashboard/lifecycle.py`: `build_record(..., listeners=None)`; `RecordRefresher.update(**kw)`
  (thread-safe, writes at once, only while the record is still this process's); `unit_bind`
  learns `auto` (default) → `auto:PORT`; `status()` copies `listeners` into `server` and applies
  Decision 6; `brief` / `brief_line` show the tailnet state.
- `cli_dashboard`: `install --bind auto|loopback|tailscale` (default `auto`), the change report,
  `_print_status` per-listener lines.
- Docs: `docs/WORKFLOW.md` "Remote dashboard", `nw serve --help`, `nw dashboard install --help`,
  and the reference row for `serve`.

## Alternatives considered

- **One socket on `0.0.0.0` and decide the mode from the destination address or the Host
  header.** Rejected: a wildcard bind exposes the LAN, and a header-keyed mode is exactly the
  mix-up G3 forbids.
- **Keep exit 3 and let systemd retry.** That is the observed failure: nothing served while
  waiting, and a restart counter that climbs forever.
- **Two systemd units (one loopback, one tailnet).** Two processes, two hubs, two CSRF secrets,
  two code watchers and twice the status output; the human still has to install both.
- **React to Tailscale changes through `tailscale status --watch` / netlink.** More moving parts
  for a few seconds of latency on an event that happens rarely; a 15 s poll is enough.

## Acceptance criteria

- [x] With no `--bind` and no tailnet address, `nw serve --dashboard` serves `127.0.0.1:PORT` at
      once, logs `tailnet: waiting for a Tailscale address (<reason>)` once, and does not exit.
- [x] When an own tailnet address appears, the tailnet listener is bound within one re-check
      interval: an allowed whois login gets 200, an unknown login gets 403, loopback keeps
      working. When the address goes away the tailnet listener closes (connections refused) and
      loopback keeps serving.
- [x] The loopback socket never applies tailnet rules and the tailnet socket never applies
      loopback rules: `Host: 127.0.0.1:P` on the tailnet socket is 403 `bad_host`; the tailnet
      Host on the loopback socket is 403 `bad_host`; a `local`-mode server cannot be built on a
      tailnet address.
- [x] Both listeners share one snapshot cache, hub, CSRF secret and audit log; closing the
      tailnet listener does not close the shared hub.
- [x] `server.json` carries `listeners` with url, auth and state (listening / waiting + reason).
- [x] `nw dashboard status` prints a `local` and a `tailnet` line; a waiting tailnet in `auto`
      mode is not a problem; a waiting tailnet-only server is reported as waiting with the fix,
      not as "no live server.json".
- [x] `nw dashboard install` defaults to `--bind auto` (`ExecStart=… --bind auto:8791`, no
      `--tailnet-wait`); re-running it over a tailscale-only unit replaces it and prints what
      changed.
- [x] `--bind 127.0.0.1:P` is loopback only; `--bind tailscale` still waits then exits 3.
- [x] A real systemd test unit with the default bind keeps `NRestarts` at 0 while there is no
      tailnet address.
- [x] `docs/WORKFLOW.md`, `nw serve --help` and `nw dashboard install --help` describe the
      default.

## Test plan

- **Automated tests**
  - `tests/test_dashboard_dual_listen.py` (new): `auto_port`, `tailnet_recheck_s`,
    `tailnet_reason` against a fake run (each BackendState, a failing status, a missing binary);
    `TailnetSupervisor.poll()` with an injected `own` that flips none → address → none → other
    address, binding real sockets on `127.0.0.x` with the whois mode (fake gate + peer seam):
    waiting / listening / closed transitions, `on_change` calls, log lines once each, a failed bind
    is a waiting reason; mode separation over real sockets (Host `127.0.0.1` refused on the
    whois socket, the whois Host refused on the loopback socket, `local` refused on a tailnet
    address); shared hub survives the tailnet listener's close and an open stream on it ends;
    real `nw serve --dashboard` subprocess with a fake logged-out `tailscale` on PATH: loopback
    200, `server.json` listeners, no exit within a few seconds.
  - `tests/test_dashboard_service.py`: `unit_bind("auto")`, default unit text, the install change
    report, status per-listener lines and the problem rules, `brief_line`.
  - Existing dashboard server / tailnet / service / seams tests stay green.
- **Manual verification (real processes, recorded in As shipped):**
  1. Real `tailscale` logged out (or, if the host is logged in, a fake logged-out `tailscale` on
     `PATH`): loopback serves at once, status shows tailnet waiting, the process lives ≥ 90 s.
  2. `unshare -rn` netns with a fake `tailscale` whose `ip` output flips none → 100.x: the
     tailnet listener appears within one re-check; allowed login 200, unknown login 403, loopback
     200. Flip back: the tailnet listener closes, loopback stays up.
  3. Mode separation on real sockets in the netns: `Host: 127.0.0.1:P` to the tailnet socket is
     403; a loopback peer on the tailnet socket is 403 `not_tailnet_peer`.
  4. A real systemd test unit (`nw-dashboard-test-dual`, scratch dirs, free port) with the default
     bind and no tailnet address: `NRestarts` stays 0 over ≥ 90 s; then uninstall it.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q -n 2`, the touched
  test files, `python3 tools/spec_lint.py`.

## As shipped

Shipped as designed. Notes where the code settled details the Design left open:

- `make_server` gained `hub=` / `csrf=` (the SPEC-0416 seam test now lists them). The CLI passes
  `mode="local"` for the loopback half of `auto`; `DashboardServer` refuses `local` on any
  non-loopback address, so that argument cannot open a no-login tailnet socket. The CLI never
  passes the supervisor's seams (`own`, `reason`, `gate`, `peer`); a new AST test guards that.
- The tailnet listener gets a fresh `auth.Gate` (and so a fresh MagicDNS read) each time it
  binds. A changed address closes the old listener and binds the new one in the same check.
- In `auto` mode `server.json` starts with the tailnet entry in state `waiting`, reason
  `checking`, and the first check rewrites it within milliseconds. In tailnet-only mode the
  waiting record is written once the wait starts and removed on exit 3 or SIGTERM (a SIGTERM
  during the wait is now a clean exit 0).
- Each waiting check also runs `tailscale status --json` for the reason, so a waiting host
  makes about eight cheap `tailscale` calls a minute.
- `NeedsMachineAuth` → `waiting for admin approval` (hint: approve it in the admin console), in
  addition to the reasons in Decision 4.
- `nw dashboard status` prints `server   pid … · bind … · since … · clients N`, then one line per
  listener. With no record and an active unit it prints `server   starting (no server.json
  yet)`. The `exit 3` problem text for an inactive tailnet-only unit names `sudo tailscale up`
  and `nw dashboard install`.
- `tests/test_dashboard_tailnet_cli.py`'s "port 8791 untouched" check now compares against the
  port's state before the test. On this host the human's own dashboard holds `127.0.0.1:8791`,
  and that made 32 of those tests fail whatever the code did.

### Test record (2026-11-04)

- Automated: `tests/test_dashboard_dual_listen.py` (new, 19 tests in 15 functions) covers `auto_port`, the
  recheck setting, `tailnet_reason` (each state, a daemon that is down, a missing binary, never
  raises), `local` refused off loopback, the CLI seam guard, and the supervisor on real sockets
  (127.0.0.2 standing in for the tailnet address): waiting → listening → 403 for an unknown login
  → closed, a failed bind as a waiting reason, rebinding to a changed address, modes keyed on the
  socket, and a closed tailnet listener ending its stream while the shared hub keeps serving
  loopback. It also covers status / brief lines and problems, the install change report, a real
  `nw serve` with a fake logged-out `tailscale`, and a real tailnet-only wait record. The updated
  tests are `test_dashboard_service.py`, `test_dashboard_tailnet_cli.py` and
  `test_dashboard_tailnet_seams.py`; `test_rich_output_guard.py` allowlists the server's new
  `log()` stderr lines.
- `python3 tools/affected_tests.py --run uv run pytest -q -n 2`: 3989 passed, 7 skipped, 1
  failed (`test_rich_output_guard`, the unlisted `log()`; allowlisted and re-run green). The full
  suite is left to the close gate.
- **Real processes**, all with scratch `NW_DATA_DIR` / `NW_CONFIG_DIR` under
  `/home/bruno/work/.nw-tmp/dash-dual/`. By the time of the test the host's real Tailscale had
  been logged in by the human, so the logged-out case uses the fake `tailscale`
  (`tests/fixtures/fake_tailscale.py`) first on `PATH`:
  1. Logged out, `nw serve --dashboard --bind auto:58921`: loopback was 200 at once. Status
     showed `local    http://127.0.0.1:58921/ (no login)` and `tailnet  waiting: Tailscale logged
     out — run: sudo tailscale up`. The process was alive, with loopback 200, at 30, 60 and 95 s.
     `tailscale ip` was run every 15 s (7 checks) and the waiting line was logged once. SIGTERM
     gave exit 0.
  2. `unshare -rn` netns, `lo` with 100.101.0.5/.6, default bind (`auto:8791`, recheck 15 s),
     fake starts logged out. Loopback 200, the tailnet port refused connections, status said
     waiting. After flipping the fake to own IP 100.101.0.5, the tailnet listener was up **14 s**
     later and status showed `tailnet  http://100.101.0.5:8791/ (whois)`. From .5 (own login
     me@example.com), `/` and `/api/snapshot` returned 200, and so did the MagicDNS Host. From .6
     (eve@example.com), `/api/snapshot` and `/app.js` returned 403 `login_not_allowed`. Loopback
     stayed 200. After flipping back to logged out, the tailnet listener closed **14 s** later
     (connection refused) while loopback stayed 200 and status said waiting again. The process
     stayed up. The log had exactly four tailnet lines: waiting, listening, address gone,
     waiting.
  3. Mode separation in that netns: on the tailnet socket from the allowed .5, `Host:
     127.0.0.1:8791` and `Host: localhost:8791` were 403 `bad_host`. A loopback peer
     (127.0.0.1 → 100.101.0.5) was 403 `not_tailnet_peer`. On the loopback socket, `Host:
     100.101.0.5:8791` was 403 `bad_host`, and a request from source 100.101.0.5 with the
     loopback Host was 200 (local rules, no whois). The audit lines carried these reasons.
  4. Also, briefly, the **real logged-in Tailscale** (`auto:58922`, scratch data): status listed
     `tailnet  http://100.69.173.72:58922/ (whois)`. A request from the host's own tailnet
     address got 200 through real `tailscale whois`, and `Host: 127.0.0.1` on that socket got 403.
  5. Real systemd, unit `nw-dashboard-test-dual` on port 58923, with a drop-in that put the
     logged-out fake first on `PATH`. First `install --bind tailscale` reproduced the human's
     state: `problem: nothing is served: the tailnet-only bind tailscale:58923 is waiting:
     Tailscale logged out — run: sudo tailscale up; or run nw dashboard install …`. Re-running
     `install` with the default printed `replaced …`, `bind tailscale:58923 (tailnet only) ->
     auto:58923 (loopback + tailnet)` and the `- ExecStart=…` / `+ ExecStart=…` lines, then
     restarted the unit. `NRestarts` stayed **0** with the same `MainPID` and loopback 200 at
     every 20 s sample from 16:32:21 to 16:33:41, and the journal had one waiting line.
     `uninstall` and removing the drop-in left `LoadState=not-found`. The human's
     `nw-dashboard.service`, their Tailscale login and linger were not touched.

## Rollout / migration

1. Land. The human's running unit restarts onto the new code (code watch) but keeps its
   `--bind tailscale` ExecStart until reinstalled.
2. The human runs `nw dashboard install`; it replaces the tailnet-only unit with the default one
   and prints the change.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None._
