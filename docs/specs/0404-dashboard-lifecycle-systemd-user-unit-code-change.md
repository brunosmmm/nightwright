---
id: SPEC-0404
title: "Dashboard server lifecycle: code-change exit, exit 3 without a tailnet address, server.json record"
status: in-progress
owner: user
created: 2026-11-03
updated: 2026-11-04
kind: feature
source_idea: IDEA-877
parent: SPEC-0400
depends_on: [SPEC-0401, SPEC-0414]
tags: [agentic, dashboard, remote, lifecycle]
---

## Context

Promoted from idea `IDEA-877`. Child 4 of epic
[SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md) (remote web dashboard
over Tailscale). The epic's Decision 7 chose a supervised, self-restarting server (a systemd
user unit) over the conductor follower, because the dashboard must keep showing "the conductor
is down". The original scope of this idea (the process contract, the unit installer, and the
status surfaces) was reviewed as too large for one campaign (16 acceptance criteria across three
subsystems) and is **split in three**. **This spec is the first split: the server-process
lifecycle contract.** Any supervisor, systemd or a human's shell loop, relies on it, and the two
other splits build on it.

What this spec delivers: the server (a) exits 0 when nightwright's code changes so that its
supervisor starts it on the new code, (b) exits 3 when this host's tailnet address is missing at
start so that the supervisor retries, (c) shuts down cleanly on SIGTERM/SIGINT, and (d) publishes
a `server.json` runtime record that any other process can read to find the live server without
talking to it.

**Dependency on epic child 1 (SPEC-0414).** The read-only server of epic child 1
(`nw serve --dashboard`) is SPEC-0414, which is merged and named in this spec's `depends_on`.
Implementation builds on that server. The exact names this spec attaches to, and which child 1 must
therefore provide (they are listed in the epic's module layout and Decisions 1-2), are:

- `agentic/dashboard/server.py`: `serve(cfg, bind, …)`, whose return value is the process exit
  code, and the stdlib `ThreadingHTTPServer` it owns (so `httpd.shutdown()` exists);
- `agentic/dashboard/auth.py`: `own_tailnet_ips()` (the host's own `tailscale ip` addresses;
  empty or raising when tailscale is missing or logged out) and `check_bind(...)` (exit 2 on a
  refused bind, before the socket opens);
- `agentic/dashboard/cli_dashboard.py` and `cli_worker.serve_cmd`'s `--dashboard` wiring, where the
  two hidden options below are added;
- the `agentic.dashboard` config block and its fallback-to-defaults parsing.

What exists today, checked against the code on 2026-11-03:

- **Code-change detection (SPEC-0194, SPEC-0231).** `agentic/codewatch.py` has
  `code_checkout()` and `Watcher(root, check_s)`. `Watcher.poll(now_monotonic)` returns a
  change dict when `git rev-parse HEAD` moved and `src/nightwright`, `pyproject.toml` or
  `skills` changed (or `git diff` failed, `diff_failed: true`, SPEC-0262), and `None` for
  docs/spec/test-only commits. Followers (`events.follow`) print one `code_changed` line and
  return, so their owner re-arms them. The runner re-execs. A server has no Monitor owner to
  re-arm it; a supervisor must be that owner.
- **Liveness pattern.** Process records store `pid` plus the `/proc/<pid>/stat` start ticks
  (`herdr_autowatch.start_ticks` / `alive`, `conductor_cost.placement_live`,
  `runproc.start_ticks`). `herdr_autowatch.state_path` / `read_state` is the precedent for a
  small JSON runtime record under `<data_dir>/agentic/`.

## Goals / Non-goals

**Goals**
- G1. The server restarts itself onto new code: it exits 0 when nightwright's code changes
  (`codewatch`, SPEC-0231 semantics), with a `code_changed` line on stderr (the journal).
- G2. The server exits 3 when this host's tailnet address is missing at start (tailscale
  missing, logged out or not running), so a supervisor retries until tailscale is back. Exit 2
  stays reserved for a bind the gate refuses.
- G3. A `server.json` runtime record (pid + start ticks, url, bind, started_at, code sha,
  code_watch, connected clients) lets any other process find the live server, with a liveness
  reader that treats a stale file as "not running".
- G4. SIGTERM and SIGINT shut the server down cleanly: remove `server.json`, exit 0.

**Non-goals** (each is a sibling split of IDEA-877, to be captured as its own spec; none is
delivered here)
- **`nw dashboard unit --install|--uninstall|--print`** and the generated
  `~/.config/systemd/user/nw-dashboard.service` (`Restart=always`, `RestartSec=10`,
  `RestartPreventExitStatus=2`, `ExecStart` pinned to the installed `nw`), plus the `docs/WORKFLOW.md`
  "Remote dashboard" section (every `agentic.dashboard` key and the human's Tailscale setup
  steps). Split 2; depends on this spec for the exit-code contract (0 restart, 2 permanent, 3
  retry).
- **`nw dashboard status [--json]`** (unit state, pid, url, tailscale state, whois self-check,
  `ok`/`problems` and exit codes), the `conduct-status` `dashboard` key and its render line, and
  the `nw-orient` skill mention. Split 3; depends on this spec for `server.json` and
  `record_live`.
- Any change to what the server serves, its auth or the snapshot (epic child 1), and the inbox,
  SSE and push (epic children 2, 3, 5).
- Supervising the server from the conductor follower or the queue runner (rejected in the epic,
  Decision 7). Nothing in the queue starts, stops or restarts the dashboard.
- Zero-downtime restarts. A code-change restart drops open connections; the page's reconnect
  covers it.
- Implementing the SSE client count itself (epic child 3). This spec only defines the hook it
  plugs into (Decision 6).

## Decision

This spec builds on SPEC-0401's server, snapshot cache and auth gates; implementation must not start before SPEC-0401 is done.

1. **Module `agentic/dashboard/lifecycle.py`** holds the pure logic: `server_json_path(cfg)`,
   `write_server_record`, `read_server_record`, `remove_server_record`, `record_live(rec)`,
   `CodeWatchThread`, and the config helper for `code_check_s`. `server.py` gets hooks only.
2. **Code-change exit.** The server runs a `codewatch.Watcher(code_checkout(), check_s)` on its
   own thread (default every 10 s, `agentic.dashboard.code_check_s`). On a change it writes one
   `code_changed` line to stderr, shuts the HTTP server down cleanly, removes `server.json` and
   exits **0**. Exit 0, not a failure code, is deliberate: a code change is not a failure, and a
   supervisor configured to restart on any exit (the next split's `Restart=always`) starts the
   server on the new code. Docs-only, spec-only and test-only commits do not make it exit,
   because `Watcher` returns `None` for them. A skill change restarts it too (the follower paths
   include `skills`; harmless and consistent). A `git diff` failure counts as a change, so the
   server restarts rather than running stale code.
3. **No checkout, no watcher.** When the package is not imported from a git checkout
   (`code_checkout()` is `None`), the watcher is not started and `server.json` says
   `code_watch: false`, so a status reader can show the server will not self-restart.
   `agentic.dashboard.code_check_s: 0` and a hidden `--no-code-watch` flag also turn it off. A
   hidden `--code-root PATH` option points it at another checkout (tests).
4. **Missing tailnet address exits 3.** On start, when the chosen bind needs a tailnet address
   (`--bind tailscale`, the default, or an explicit non-loopback address) and
   `auth.own_tailnet_ips()` yields none (the `tailscale` binary is missing, not logged in, or
   `tailscale ip` returns nothing), the server exits **3** with a message naming the cause. It
   binds no socket and writes no `server.json`. A bind that child 1's gate refuses while the
   host does have tailnet addresses (wildcard, LAN, foreign tailnet IP) keeps exit **2**, so the
   two are distinguishable in a supervisor's log. A loopback bind needs no tailscale and never
   exits 3. Exit 2 means a configuration error that retrying cannot fix; exit 3 means "retry
   later". This spec defines the codes; how a supervisor reacts is the next split's concern.
5. **Signals.** SIGTERM (what `systemctl stop` sends) and SIGINT shut the HTTP server down
   cleanly, remove `server.json` and exit 0. SIGKILL leaves a stale record, which `record_live`
   reads as not running.
6. **`server.json`** at `<data_dir>/agentic/dashboard/server.json` is the runtime record, written
   atomically (temp file in the same directory + rename) after the socket is bound and removed on
   clean shutdown: `{schema: "nw.dashboard.server.v1", pid, ticks, url, host, port, bind, scheme,
   started_at, code_sha, code_watch, clients, updated_at}`.
   - **Liveness**: `record_live(rec)` is true when `pid` is alive **and** its `/proc` start ticks
     equal `ticks` (reusing the `herdr_autowatch.alive` approach), so a stale file from a crash
     or a reused pid reads as "not running". A missing or unparsable file reads as no record.
   - **`clients`** is the number of connected live-update clients, obtained through a
     `client_count()` callable on the server object that `serve()` polls. **Until epic child 3's
     SSE stream exists, `client_count()` returns 0** and `clients` is 0; child 3 wires its real
     count into the same hook with no change to this spec's record code. This keeps the spec
     independent of child 3 (the epic lets children 2-4 run in parallel after child 1), and the
     clients test drives the hook directly.
   - `clients` and `updated_at` are refreshed at most every 5 s and **only when `clients`
     changed**: an idle server rewrites nothing. Producing the number does no queue read.
7. **Config** (child 1 defines `agentic.dashboard`; this spec adds one key): `code_check_s`
   (default 10; 0 disables the watcher; a malformed value, such as non-numeric or negative, falls
   back to 10, like `escalations.settings`). No other key.

## Design

**Files**
- `src/nightwright/agentic/dashboard/lifecycle.py` (new): the functions in Decision 1.
  `CodeWatchThread(stop_event, on_change, check_s, root=AUTO)` wraps `codewatch.Watcher`, polls
  with `time.monotonic()` once per second (calling `Watcher.poll`, which self-throttles to
  `check_s`) and calls `on_change(change)` exactly once, then ends.
- `src/nightwright/agentic/dashboard/server.py` (child 1): hooks only. `serve()`:
  1. resolves the bind; maps the missing-tailnet case to exit 3 and a refused bind to exit 2
     before opening the socket;
  2. opens the socket, then calls `lifecycle.write_server_record`;
  3. starts `CodeWatchThread` unless disabled (Decision 3), whose `on_change` logs the
     `code_changed` line, sets the exit code to 0 and calls `httpd.shutdown()` from a
     non-server thread;
  4. runs a small record-refresh tick that polls `client_count()`;
  5. installs SIGTERM/SIGINT handlers that call `httpd.shutdown()`;
  6. in a `finally`, removes `server.json` (only if it still names this pid) and returns the exit
     code.
- `src/nightwright/agentic/dashboard/cli_dashboard.py` / `cli_worker.serve_cmd` (child 1): add the
  hidden `--code-root PATH` and `--no-code-watch` options to `nw serve --dashboard`.

**Invariants.** Only the dashboard server writes `server.json`, under its own
`<data_dir>/agentic/dashboard/` directory, so the epic's read-only invariant (nothing written
outside `agentic/dashboard/`) holds. The epic's import-graph invariant holds: `lifecycle.py`
imports no queue module except `codewatch`, `store` (paths) and config helpers; no queue, runner,
`runproc`, `events` or `escalations` module imports `agentic.dashboard`. The later status splits
read `server.json` through `lifecycle.read_server_record` / `record_live`, which are the stable
contract.

**Restart-loop behaviour (information for the supervisor split).** Exit 3 occurs before bind
and before any record is written, so during a tailscale outage there is no `server.json` and
`record_live` reports "not running". A supervisor's retry interval bounds the loop.

## Alternatives considered

- **Non-zero exit for a code change (supervisor `Restart=on-failure`).** Works, but makes every
  code change a "failure" in the supervisor's log and blurs real crashes. Exit 0 plus
  restart-always keeps failures meaningful.
- **`systemctl --user restart` from inside the server on code change.** Needs the user bus from
  a process that may be run by hand; self-exit works under any supervisor and by hand.
- **systemd path unit or `ExecReload` on code change.** More moving parts; the process already
  has `codewatch`, the mechanism followers use.
- **A single exit code for "bind refused" and "tailnet address missing".** Rejected: the first
  cannot be fixed by retrying, the second can, and a supervisor needs to tell them apart.
- **Counting clients here via our own connection tracking.** Rejected: it would duplicate child
  3's SSE bookkeeping. A hook with a default of 0 keeps this spec independent.

## Acceptance criteria

- [x] When nightwright's code changes (HEAD moves with a `src/nightwright`, `pyproject.toml` or
      `skills` change in the watched checkout), the running server writes a `code_changed` line
      to stderr, shuts down cleanly, removes `server.json` and exits **0**. A docs-only commit
      does not make it exit.
- [x] When the package is not imported from a git checkout, no watcher runs and `server.json` has
      `code_watch: false`; `agentic.dashboard.code_check_s: 0` and `--no-code-watch` also
      disable it, and `--code-root PATH` retargets it.
- [ ] When the bind needs a tailnet address and `own_tailnet_ips()` yields none, the server exits
      **3** with a message naming the cause, binds no socket and writes no `server.json`. A
      refused bind (for example `0.0.0.0`) keeps exit **2**, and a loopback bind never exits 3.
- [x] After a successful bind, `server.json` exists at `<data_dir>/agentic/dashboard/server.json`
      with `schema`, `pid`, `ticks`, `url`, `host`, `port`, `bind`, `scheme`, `started_at`,
      `code_sha`, `code_watch`, `clients` and `updated_at`, written atomically (no partial file
      is ever visible). `record_live` is true for the real running server and false for a
      record whose pid is dead or whose pid is alive with different start ticks.
- [x] SIGTERM and SIGINT make the server exit 0 and remove `server.json`; SIGKILL leaves a
      record that `record_live` reads as not running.
- [x] `clients` in `server.json` follows the server's `client_count()` hook (0 until epic child
      3 wires it), is rewritten only when the count changed and at most every 5 s, and an idle
      server rewrites nothing.
- [x] `agentic.dashboard.code_check_s` is honoured: `0` disables the watcher, a malformed value
      falls back to 10, and the default is 10.
- [x] Nothing is written outside `<data_dir>/agentic/dashboard/` by this lifecycle code, and no
      queue, runner, `runproc`, `events` or `escalations` module imports `agentic.dashboard`
      (the epic's import-graph test still passes and now covers `lifecycle.py`);
      `uv run pytest` affected tests pass and `python3 tools/spec_lint.py` exits 0.

## Test plan

Real processes and real sockets wherever the behaviour is process-level (fakes alone have
shipped broken integrations in this repo). A fake `tailscale` executable on `PATH` stands in for
the tailnet, because a CI runner has none. Waits use `tests/waits.py` (`NW_TEST_WAIT_S`), never a
fixed sleep before a positive assertion.

- **Automated tests** (`tests/test_dashboard_lifecycle.py`):
  - `test_server_exits_0_on_code_change` (real subprocess): a tmp git repo with a
    `src/nightwright` dir is the watched checkout, passed through the hidden `--code-root`.
    Start `nw serve --dashboard --bind 127.0.0.1:0 --code-root <tmp>` with `code_check_s: 1`,
    commit a change under `src/nightwright`, and assert the process exits 0 within
    `NW_TEST_WAIT_S`, stderr has a `code_changed` line and `server.json` is removed. A second run
    commits only a `docs/` file, asserts the process is still running after several poll
    intervals, then exits 0 on SIGTERM with `server.json` removed. Covers AC 1 and 5.
  - `test_no_code_checkout_disables_watcher`: `code_checkout()` patched to `None`, and separately
    `--no-code-watch` and `code_check_s: 0`; `server.json.code_watch` is false and a commit under
    `src/nightwright` does not stop the server. Covers AC 2.
  - `test_server_exits_3_without_tailnet_address` (real subprocess): fake `tailscale` on `PATH`
    exiting 1 / printing nothing for `ip`; `nw serve --dashboard` exits 3, stderr names the cause,
    no `server.json`, nothing listening. Also: a missing `tailscale` binary exits 3; a loopback
    bind with the same fake starts normally; `0.0.0.0` and `100.64.0.9:0` (fake reporting a
    different own IP) still exit 2 (regression of child 1's gate). Covers AC 3.
  - `test_server_record_roundtrip_and_liveness`: write/read/remove; atomic (a reader looping
    during repeated writes never sees a partial or empty file); a dead pid, and a live pid with
    wrong ticks, read as not running; the real current process with its real ticks reads live; a
    missing or garbage file reads as no record. Covers AC 4 and the SIGKILL half of AC 5 (a
    `kill -9`'d real subprocess leaves a record that reads as not running).
  - `test_clients_count_written_only_on_change`: drive `client_count()` through 0, 2, 0 and
    assert the record's `clients` follows, `updated_at` does not move while the count is steady,
    and rewrites are at most one per 5 s (injected clock). Covers AC 6.
  - `test_dashboard_config_code_check_s`: 0, malformed (string, negative) and default. Covers
    AC 7.
  - `test_lifecycle_import_graph_and_writes`: extends the epic's import-graph test to include
    `lifecycle.py` and asserts no queue module imports `agentic.dashboard`; a run of the server
    leaves every file under the data dir outside `agentic/dashboard/` byte-identical. Covers
    AC 8.
  - `tests/test_dashboard_e2e.py` (child 1) gains a case: start the real server, send `kill
    -TERM`, assert exit 0 and no `server.json`.
- **Manual verification** (this host; recorded in this spec before `done`; no systemd needed):
  1. Run `nw serve --dashboard` by hand with tailscale logged in; `cat
     <data_dir>/agentic/dashboard/server.json` shows the record, and `kill -TERM <pid>` removes
     it and exits 0.
  2. With tailscale logged out (or `tailscale` renamed on `PATH`), `nw serve --dashboard`
     exits 3 with the cause and writes no record; `echo $?` prints 3.
  3. Merge a change under `src/nightwright` into the checkout `nw` runs from; within
     `code_check_s` the server prints `code_changed` and exits 0.
- **Regression guard**: affected tests (`python3 tools/affected_tests.py --run uv run pytest
  -q`) plus the files touched; the epic's read-only and import-graph tests pass; `completion`
  test passes for the new hidden options; `python3 tools/spec_lint.py`.

### Test record (2026-11-04)

- `uv run pytest -q tests/test_dashboard_lifecycle.py`: 24 passed (real `nw serve --dashboard`
  subprocesses against a tmp git checkout through `--code-root`: exit 0 + `code_changed` +
  `server.json` removed on a `src/nightwright` commit; a `docs/` commit keeps it running and
  SIGTERM exits 0; `--no-code-watch`, `code_check_s: 0` and a non-checkout root give
  `code_watch: false`; record roundtrip, atomic writes under a looping reader, liveness (dead
  pid, wrong ticks, real process), a `kill -9`'d server reads not running; `RecordRefresher`
  with an injected clock; `code_check_s` parsing; the import graph and the writes-only-under-
  `agentic/dashboard/` check).
- `uv run pytest -q tests/test_dashboard_serve_cli.py tests/test_dashboard_server.py
  tests/test_dashboard_imports.py`: pass (SIGINT/SIGTERM e2e cases of SPEC-0414 cover AC 5's
  signal half).
- `python3 tools/spec_lint.py` exits 0 after `--write-ledger`. The full suite was left to gating.
- **Not done:** AC 3 (exit 3 without a tailnet address) and manual steps 1-3 (they need a
  tailscale login and the real checkout; step 2 needs the tailnet bind). See "As shipped".

### As shipped (2026-11-04)

- **Exit 3 is not implemented.** SPEC-0414 narrowed itself to the loopback server: the tree has no
  `own_tailnet_ips()`, no `--bind tailscale`, no fake-`tailscale` fixture, and `check_bind`
  refuses every non-loopback address with exit 2 (a loopback bind never needs a tailnet address).
  The "bind needs a tailnet address and none exists" case therefore cannot occur yet. It lands
  with the tailnet-slice sibling of SPEC-0414: that spec's `serve` path should raise a distinct
  `TailnetMissing` mapped to exit 3 before the socket opens and before `server.json`, and carry
  the test cases of this spec's `test_server_exits_3_without_tailnet_address`. AC 3 stays unticked.
- `lifecycle.py` holds the record and watcher logic as designed. `server.py` keeps
  `server_json_path` / `remove_server_json` as aliases of `lifecycle`'s and
  `write_server_json(cfg, server, *, code_watch, code_sha, bind)` builds the record through
  `lifecycle.build_record`. The record keeps SPEC-0414's `auth: "token"` field and renames
  `start_ticks` to `ticks`.
- `DashboardServer.client_count()` returns 0 (the hook); `RecordRefresher.tick()` is driven once
  per second by the serve loop in `cli_worker._serve_dashboard` (no separate refresh thread) and
  rewrites the record only when `clients` changed and 5 s passed since the last write.
- The watcher polls once per second (`Watcher.poll` throttles to `code_check_s`). On a change the
  `code_changed` line goes to stderr and the serve loop stops, which is a normal shutdown (exit 0).
- `agentic.dashboard.code_check_s` is read by `lifecycle.code_check_s(cfg)`;
  `dashboard.settings()` is unchanged.
- `tests/test_dashboard_lifecycle.py` starts the server with a PATH that keeps `git`, because
  `test_dashboard_cli.nw_env` drops every PATH directory holding a `tailscale` (here `/usr/bin`).

## Rollout / migration

1. Land after epic child 1 (`nw serve --dashboard`). No data migration; `server.json` is created
   on first start and absent otherwise. Nothing changes for a human who runs the server by hand,
   except that it now exits on a code change instead of running stale code.
2. The sibling splits (unit installer + docs; `nw dashboard status` + `conduct-status` key) are
   captured as separate specs that `depends_on` this one. Until they land, the human restarts
   the server by hand after the exit-0 or exit-3 and reads `server.json` directly.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected automated tests pass; manual steps 1-3 performed and noted
      here.
- [ ] No regressions (the epic's read-only and import-graph tests pass).
- [ ] `depends_on` names the epic child 1 spec (SPEC-0414), and `spec_lint` resolves it.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).
- [ ] `python3 tools/spec_lint.py` exits 0.

## Open questions

None. The former bookkeeping item (adding child 1's spec id, SPEC-0414, to `depends_on`) was a dependency
of the order of work, not a design question: it is recorded in Context and the Definition of
done.
