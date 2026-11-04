---
id: SPEC-0404
title: "Dashboard lifecycle: systemd user unit, code-change restart and nw dashboard status"
status: proposed
owner: user
created: 2026-11-03
kind: feature
source_idea: IDEA-877
parent: SPEC-0400
tags: [agentic, dashboard, remote, systemd, lifecycle]
---

## Context

Promoted from idea `IDEA-877`. Child 4 of epic
[SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md) (remote web dashboard
over Tailscale). The epic's Decision 7 chose a **systemd user unit** to supervise
`nw serve --dashboard`, not the conductor follower: the dashboard must show "the conductor is
down", so it cannot die with the conductor. This spec delivers that lifecycle. It depends on
the epic's child 1 (the read-only server, `nw serve --dashboard`, `auth.check_bind`,
`own_tailnet_ips`, whois, `agentic/dashboard/` package), which this spec builds on and does
not redefine.

What exists today, checked against the code on 2026-11-03:

- **Code-change detection (SPEC-0194, SPEC-0231).** `agentic/codewatch.py` has
  `code_checkout()` and `Watcher(root, check_s)`. `Watcher.poll(now_monotonic)` returns a
  change dict when `git rev-parse HEAD` moved and `src/nightwright`, `pyproject.toml` or
  `skills` changed (or `git diff` failed, `diff_failed: true`), and `None` for docs/spec/test-only
  commits. Followers (`events.follow`) print one `code_changed` line and return, so their
  owner re-arms them. The runner re-execs. A server has no Monitor owner to re-arm it; systemd
  must be that owner.
- **Liveness pattern.** Process records store `pid` plus the `/proc/<pid>/stat` start ticks
  (`herdr_autowatch.start_ticks` / `alive`, `conductor_cost.placement_live`,
  `runproc.start_ticks`). `herdr_autowatch.state_path` / `read_state` is the precedent for a
  small JSON runtime record under `<data_dir>/agentic/`.
- **Status precedent.** `cli_conduct.conduct_status` returns the `nw.conduct.status.v1`
  payload and already carries `herdr_watch` via `_herdr_watch_status(cfg)`, which swallows
  every exception and returns `None` (`cli_conduct.py:68`). `conduct-status` is rendered in
  `cli_conduct.py` around line 161.
- **Service precedent.** `docs/WORKFLOW.md` (the pull-worker section) tells the human to hand-write
  `~/.config/systemd/user/nw-worker.service`. No nightwright code writes or manages a unit.
- **Install shape.** `nw` is installed by `uv tool install` as `~/.local/bin/nw`
  (`pyproject.toml` `[project.scripts] nw = "nightwright.cli:cli"`). A systemd unit does not
  inherit the login shell's `PATH`, so `ExecStart` needs an absolute path.
- **Server today (SPEC-0400 child 1).** `nw serve --dashboard` exits 2 on a refused bind and
  otherwise serves until killed. Nothing restarts it, and nothing tells the human whether it
  is up.

## Goals / Non-goals

**Goals**
- G1. `nw dashboard unit --install|--uninstall|--print` manages
  `~/.config/systemd/user/nw-dashboard.service` (`Restart=always`, `RestartSec=10`, `ExecStart`
  pinned to the installed `nw`), so the server runs unattended and survives logout and
  reboot (with linger).
- G2. The server restarts itself onto new code: it exits 0 when nightwright's code changes
  (`codewatch`, SPEC-0231 semantics), and systemd starts it again.
- G3. The server exits non-zero when this host's tailnet address is missing at start (tailscale
  logged out or not running), so systemd retries until tailscale is back.
- G4. A `server.json` runtime record (pid + start ticks, url, bind, started_at, connected
  clients, code sha) lets any other process find the live server without talking to it.
- G5. `nw dashboard status [--json]` reports: unit state, pid, url, tailscale state, a whois
  self-check, connected clients.
- G6. `conduct-status` gains a `dashboard` key, like `herdr_watch`.
- G7. `agentic.dashboard` config keys and the human's Tailscale setup steps are documented in
  `docs/WORKFLOW.md` ("Remote dashboard" section).

**Non-goals**
- Supervising the server from the conductor follower or the queue runner (rejected in the
  epic, Decision 7). Nothing in the queue starts, stops or restarts the dashboard.
- Running `sudo`, `tailscale up`, `tailscale set --operator` or `loginctl enable-linger` on the
  human's behalf. The command prints these as steps; it runs only user-level `systemctl --user`
  (the human's own session, an explicit `--install`).
- Non-systemd supervisors (launchd, supervisord, Docker). `--print` gives the human the unit
  text; anything else is theirs.
- Changing what the server serves, its auth, or the snapshot (child 1) and the inbox, SSE and
  push (children 2, 3, 5).
- Zero-downtime restarts. A code-change restart drops open SSE connections; the page's
  reconnect (child 1/3) covers it.

## Decision

1. **A new click group `nw dashboard`** in `agentic/dashboard/cli_dashboard.py` (child 1 already
   owns `snapshot`; this spec adds `unit` and `status`). Module `agentic/dashboard/lifecycle.py`
   holds the pure logic: unit text rendering, `server.json` read/write/liveness, the code
   watcher thread and the status builder. The CLI stays thin.
2. **The unit is a user unit** at `~/.config/systemd/user/nw-dashboard.service`
   (`$XDG_CONFIG_HOME` honoured), with `Restart=always`, `RestartSec=10`, and `ExecStart`
   pinned to the absolute path of the installed `nw`. `--install` writes it, runs
   `systemctl --user daemon-reload` and `systemctl --user enable --now nw-dashboard.service`,
   and prints the human-only follow-ups (linger, Tailscale). `--uninstall` stops, disables and
   removes it. `--print` writes the unit text to stdout and touches nothing.
3. **`Restart=always`, not `on-failure`**, because the code-change exit is **0**: systemd
   must restart a clean exit. A deliberate `systemctl --user stop` still stops it.
4. **Code-change exit.** The server runs a `codewatch.Watcher(code_checkout(), check_s)` on its
   own thread (default every 10 s, `agentic.dashboard.code_check_s`). On a change it writes one
   `code_changed` line to stderr (the journal), closes `server.json` state, shuts the HTTP
   server down cleanly, and exits 0. When the package is not imported from a git checkout
   (`code_checkout()` is None) the watcher is disabled and `server.json` says
   `code_watch: false`, so `status` shows it will not self-restart. `--no-code-watch` turns it
   off, and a hidden `--code-root PATH` points it at another checkout (tests).
5. **Missing tailnet address exits non-zero.** On start, when `--bind tailscale` (the default)
   cannot resolve an own tailnet IP (the `tailscale` binary is missing, not logged in, or
   `tailscale ip -4` returns nothing), the server exits **3** with a message naming the cause
   and writes no `server.json`. Exit 2 stays reserved for a bind the gate refuses (child 1),
   so the two cases are distinguishable in `systemctl status`. systemd retries exit 3 every
   `RestartSec`. A refused bind (exit 2) is a configuration error that retrying cannot fix,
   so the unit sets `RestartPreventExitStatus=2` and the unit stays failed until the human
   fixes the config.
6. **`server.json`** at `<data_dir>/agentic/dashboard/server.json` is the runtime record, written
   atomically (temp file + rename) after the socket is bound and removed on clean shutdown:
   `{schema: "nw.dashboard.server.v1", pid, ticks, url, host, port, bind, scheme, started_at,
   code_sha, code_watch, clients, updated_at}`. `clients` and `updated_at` are refreshed by
   the server at most every 5 s and only when `clients` changed (no write when idle), and
   there is no queue read to produce it. Liveness is `pid` alive **and** `/proc` start ticks
   equal (reusing the `herdr_autowatch.alive` approach), so a stale file from a crash or a
   reused pid reads as "not running".
7. **`nw dashboard status [--json]`** is read-only and never starts anything. It reports:
   - **unit**: `systemctl --user show nw-dashboard.service -p LoadState,ActiveState,SubState,
     UnitFileState,MainPID` parsed, or `not installed` / `systemd unavailable`;
   - **process**: pid, start time and uptime from `server.json`, and whether it is live;
   - **url**: from `server.json` when live, else the url the config would bind;
   - **tailscale**: `tailscale status --json` → `BackendState` (`Running`, `NeedsLogin`,
     `Stopped`, ...), the host's own tailnet IPs, and the login the dashboard would accept
     (`allowed_logins`, default the host's own login);
   - **whois self-check**: `tailscale whois --json <own tailnet ip>` and the same
     `allowed_logins` check child 1's gate applies. It shows `ok`, or the reason the gate
     would answer 403 (for example, "tailscale cannot run `whois` without root: run `sudo
     tailscale set --operator=$USER`"). This is the human's early signal that the operator
     step is missing;
   - **clients**: connected SSE clients from `server.json`.
   Exit code 0 when the server is live and the self-check is `ok`; 1 when anything is wrong
   (so it works in scripts); the `--json` payload has the same fields plus `ok: bool` and
   `problems: [str]`.
8. **`conduct-status` gains `dashboard`**: `lifecycle.status_summary(cfg)` returns
   `{live, pid, url, clients, unit}` or `None`, using only `server.json`, `/proc` and one
   cached `systemctl --user is-active`. It swallows every exception like
   `_herdr_watch_status`. It runs no `tailscale` command and no whois, so `conduct-status`
   stays cheap. `conduct-status` renders one line when the key is not `None`.
9. **Config** (child 1 defines `agentic.dashboard`; this spec adds): `code_check_s` (default
   10; 0 disables the watcher; malformed falls back to 10). No other key.
10. **Docs.** `docs/WORKFLOW.md` gains a "Remote dashboard" section: every `agentic.dashboard`
    key with its default, the human's one-time Tailscale steps (`sudo tailscale up`, `sudo
    tailscale set --operator=$USER`, the phone app on the same account,
    `loginctl enable-linger $USER`, `nw dashboard unit --install`, `nw dashboard status`), and
    the lifecycle behaviour (restart on code change, exit 3 while tailscale is down). The
    `nw-orient` skill mentions `nw dashboard status`.

## Design

**Files**
- `src/nightwright/agentic/dashboard/lifecycle.py` (new): `unit_path()`, `render_unit(nw_path)`,
  `install(...)`, `uninstall(...)`, `server_json_path(cfg)`, `write_server_record`,
  `read_server_record`, `remove_server_record`, `record_live(rec)`, `CodeWatchThread`,
  `unit_state(run=subprocess.run)`, `tailscale_state(run=...)`, `status(cfg, ...)`,
  `status_summary(cfg)`.
- `src/nightwright/agentic/dashboard/cli_dashboard.py` (child 1 creates it): adds
  `nw dashboard unit` and `nw dashboard status`.
- `src/nightwright/agentic/dashboard/server.py` (child 1): hooks only. `serve()` calls
  `lifecycle.write_server_record` after bind, starts `CodeWatchThread`, passes the SSE client
  count to the record writer, and maps outcomes to exit codes (0 code change or SIGTERM, 2
  refused bind, 3 missing tailnet address).
- `src/nightwright/agentic/cli_conduct.py`: `_dashboard_status(cfg)` next to
  `_herdr_watch_status`, the `"dashboard"` key in `conduct_status`, and a render line.
- `docs/WORKFLOW.md`, `skills/nw-orient/SKILL.md`: docs only.

**Unit text** (`render_unit`; `<NW>` is `shutil.which("nw")` resolved to an absolute path at
install time, or `--nw PATH`; refused when it is not found, because a unit with a relative
`ExecStart` fails at boot):

```ini
[Unit]
Description=nightwright remote dashboard (nw serve --dashboard)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=<NW> serve --dashboard
Restart=always
RestartSec=10
RestartPreventExitStatus=2
Environment=PYTHONUNBUFFERED=1
# NW_DATA_DIR / XDG_* are passed through when set at install time:
Environment=NW_DATA_DIR=<resolved data dir, only when the env var was set>

[Install]
WantedBy=default.target
```

Notes: `After=tailscaled.service` is advisory because the user manager may not see a system
unit; the exit-3 retry loop is the real guarantee. The unit adds no `--bind`: it runs
`--bind tailscale` from config (`agentic.dashboard.bind`), so editing config and restarting
the unit changes the bind without re-installing. `nw dashboard unit --print` shows the exact
text `--install` writes; `--install` is idempotent (rewrites only when the text differs, always
`daemon-reload`s after a change, prints "unchanged" otherwise). `--uninstall` on a missing unit
exits 0 with "not installed". Both `--install` and `--uninstall` fail with a clear message when
`systemctl --user` is unavailable (no `systemd`, or no user bus), and `--install` does not leave
a half-written unit (write to temp, rename).

**Code watch thread.** `CodeWatchThread(stop_event, on_change, check_s, root=AUTO)` wraps
`codewatch.Watcher`, polling with `time.monotonic()` every second and calling `on_change(change)`
once. `server.serve()` handles it by logging the `code_changed` line, calling
`httpd.shutdown()` from a non-server thread, and returning exit code 0. Docs-only, spec-only
and test-only commits do not restart it, because `Watcher` returns `None` for them (the
follower paths include `skills`; a skill change restarts the dashboard too, harmless and
consistent with followers). A `git diff` failure counts as a change (SPEC-0262), so it restarts
rather than running stale code.

**Restart-loop guard.** `Restart=always` plus a persistent failure would loop. `RestartSec=10`
bounds it, and systemd's default `StartLimitBurst`/`StartLimitIntervalSec` (5 in 10 s) cannot
trip at that interval, so a tailscale outage is a slow retry, not a failure state. The record
is written only after a successful bind, so `status` during the outage shows
`unit: activating (auto-restart)`, `process: not running`, and `tailscale: NeedsLogin` and
names the cause.

**Signals.** SIGTERM (what `systemctl stop` sends) and SIGINT shut the HTTP server down
cleanly, remove `server.json` and exit 0. SIGKILL leaves a stale record, which `record_live`
reads as not running.

**`server.json` writes.** Only the dashboard server writes it, under its own directory, so epic
invariant 1 (nothing written outside `agentic/dashboard/`) holds. The import-graph invariant
(epic invariant 2) holds as well: only `cli_conduct` imports `lifecycle`, lazily and inside
`try/except` like `herdr_autowatch`, and `lifecycle` imports no queue module except
`codewatch`, `store` (paths) and `config`/`escalations.settings`-style config helpers.
`conduct_status` reads `server.json` but never calls into the server.

**Failure isolation.** `_dashboard_status` returns `None` on any exception. `nw dashboard
status` shows each probe's failure as a `problems` entry and never raises. A missing
`tailscale` binary yields `tailscale: unavailable`.

## Alternatives considered

- **Supervise from the conductor follower (SPEC-0365 pattern).** Rejected by the epic: it would
  stop exactly when the conductor does, the case the dashboard exists to show.
- **`Restart=on-failure` with a non-zero exit for code change.** Works, but makes every code
  change a "failure" in `systemctl status`/journal and blurs real crashes. Exit 0 plus
  `Restart=always` keeps failures meaningful; `RestartPreventExitStatus=2` handles the one
  non-retryable case.
- **systemd path unit or `ExecReload` on code change.** More moving parts; the process already
  has `codewatch`, the mechanism followers use.
- **`systemctl --user` restart from inside the server on code change.** Needs the user bus
  from a process that may be run by hand; self-exit works under any supervisor and by hand.
- **`conduct-status` runs the whois self-check.** Rejected: it shells out to `tailscale` on a
  command the conductor runs often. `status` keeps the full check; `conduct-status` shows
  liveness only.
- **Print instructions only, never write the unit.** The human asked for no manual steps; the
  unit is small and `--print` keeps the manual path.

## Acceptance criteria

- [ ] `nw dashboard unit --print` writes the unit to stdout and creates no file; the text has
      `Restart=always`, `RestartSec=10`, `RestartPreventExitStatus=2`, an absolute-path
      `ExecStart=<nw> serve --dashboard`, and `WantedBy=default.target`. It exits non-zero when
      `nw` cannot be resolved to an absolute path.
- [ ] `nw dashboard unit --install` writes `~/.config/systemd/user/nw-dashboard.service`
      (honouring `$XDG_CONFIG_HOME`), runs `systemctl --user daemon-reload` and `enable --now`,
      is idempotent (a second run reports "unchanged" and rewrites nothing), and prints the
      human-only follow-ups (`loginctl enable-linger`, the Tailscale steps). It runs no `sudo`.
- [ ] `nw dashboard unit --uninstall` stops, disables and removes the unit, then
      `daemon-reload`s; on a missing unit it exits 0 with "not installed". Both flags fail
      with a clear message when `systemctl --user` is unavailable.
- [ ] `--install`, `--uninstall` and `--print` are mutually exclusive, and exactly one is
      required.
- [ ] When nightwright's code changes (HEAD moves with a `src/nightwright`, `pyproject.toml` or
      `skills` change), the running server writes a `code_changed` line to stderr, shuts down
      cleanly and exits **0**. A docs-only commit does not make it exit. With no git checkout
      the watcher is disabled and `server.json` has `code_watch: false`.
- [ ] When the host's tailnet address cannot be resolved at start (`--bind tailscale`), the
      server exits **3** with a message naming the cause, binds no socket and writes no
      `server.json`. A refused bind keeps exit **2**.
- [ ] After a successful bind, `server.json` exists at `<data_dir>/agentic/dashboard/server.json`
      with `schema`, `pid`, `ticks`, `url`, `host`, `port`, `bind`, `started_at`, `code_sha`,
      `code_watch` and `clients`, written atomically. SIGTERM removes it and exits 0. A
      `server.json` whose pid is dead, or alive with different start ticks, reads as not
      running.
- [ ] `clients` in `server.json` tracks the connected SSE client count, is rewritten only when
      the count changed (and at most every 5 s), and an idle server rewrites nothing.
- [ ] `nw dashboard status` prints unit state, pid and uptime, url, tailscale state, the
      whois self-check result and connected clients, and `--json` emits the same fields plus
      `ok` and `problems`. It exits 0 only when the server is live and the self-check is
      `ok`, and 1 otherwise. It starts nothing and writes nothing.
- [ ] With whois failing (for example `tailscale whois` exits 1), `status` reports the reason
      and names the `sudo tailscale set --operator=$USER` step.
- [ ] With `systemctl` or `tailscale` absent, `status` still prints, marks those probes
      `unavailable`, and does not raise.
- [ ] `nw queue conduct-status --json` has a `dashboard` key: `{live, pid, url, clients,
      unit}` when `server.json` exists, else `null`. It runs no `tailscale` command, and a
      probe failure yields `null`, never an error. The text form prints one dashboard line
      when the key is not null.
- [ ] `agentic.dashboard.code_check_s` is honoured (0 disables; malformed falls back to 10).
- [ ] `docs/WORKFLOW.md` has a "Remote dashboard" section listing every `agentic.dashboard`
      key with its default and the human's Tailscale setup steps; the `nw-orient` skill
      mentions `nw dashboard status`.
- [ ] No queue, runner, `runproc`, `events` or `escalations` module imports
      `agentic.dashboard` (the epic's import-graph test still passes; `cli_conduct` imports it
      lazily).
- [ ] `uv run pytest` affected tests pass; `python3 tools/spec_lint.py` exits 0.

## Test plan

Real processes and real sockets wherever the behaviour is process-level (fakes alone have
shipped broken integrations in this repo). `systemctl` and `tailscale` are fake executables on
`PATH` that record their argv, because a CI runner has neither a user bus nor a tailnet.

- **Automated tests** (`tests/test_dashboard_lifecycle.py`, waits via `tests/waits.py`):
  - `test_render_unit_contents`: required lines, absolute `ExecStart`, `NW_DATA_DIR` present
    only when set.
  - `test_unit_print_touches_nothing`: `--print` leaves `$HOME` and `$XDG_CONFIG_HOME` empty;
    refuses a relative or missing `nw`.
  - `test_unit_install_uninstall_idempotent`: with `HOME`/`XDG_CONFIG_HOME` in tmp and a fake
    `systemctl`, `--install` writes the file, the fake saw `daemon-reload` then
    `enable --now nw-dashboard.service`, a second `--install` says unchanged and writes
    nothing, `--uninstall` removes the file and the fake saw `stop`/`disable`/`daemon-reload`;
    `--uninstall` twice exits 0; no `sudo` argv anywhere; no `systemctl` on `PATH` gives a
    clear error and no file; the three flags are mutually exclusive.
  - `test_server_record_roundtrip_and_liveness`: write/read/remove; atomic (no partial file
    visible); a dead pid, and a live pid with wrong ticks, read as not running; the real
    current process with its real ticks reads live.
  - `test_server_exits_0_on_code_change` (real subprocess): a tmp git repo with a
    `src/nightwright` dir is the watched checkout, passed through the hidden
    `nw serve --dashboard --code-root PATH` option (default: `code_checkout()`). Start
    `nw serve --dashboard --bind 127.0.0.1:0 --code-root <tmp>` with `code_check_s: 1`, commit
    a change under `src/nightwright`, and assert the process exits 0 within `NW_TEST_WAIT_S`
    with a `code_changed` line on stderr and `server.json` removed. A second run commits only a
    `docs/` file and asserts the process is still running after several poll intervals, then
    exits 0 on SIGTERM with `server.json` removed.
  - `test_server_exits_3_without_tailnet_address` (real subprocess): fake `tailscale` on `PATH`
    returning exit 1 / empty `ip -4`; `nw serve --dashboard` exits 3, stderr names the cause,
    no `server.json`, no listening socket. A `0.0.0.0` bind still exits 2 (regression of child
    1's gate).
  - `test_no_code_checkout_disables_watcher`: `code_checkout()` patched to `None`;
    `server.json.code_watch` is false.
  - `test_clients_count_written_only_on_change`: with 0 then 2 then 0 SSE clients, the record's
    `clients` follows and `updated_at` does not move while the count is steady.
  - `test_status_report_and_exit_codes`: fake `systemctl show` output, fake `tailscale status
    --json` (`Running`, `NeedsLogin`) and `whois --json` (ok, exit 1, foreign login) against a
    live record produce the expected `ok`/`problems`; the operator hint appears on a
    permission-denied whois; exit 0 only when live and `ok`; missing binaries give
    `unavailable`, no traceback; `status` leaves the data dir byte-identical.
  - `test_conduct_status_dashboard_key`: key is `null` without a record, populated with a live
    one, `null` when the probe raises; the fake `tailscale` records zero invocations.
  - `test_dashboard_config_code_check_s`: 0, malformed and default.
  - `tests/test_dashboard_e2e.py` (child 1) gains a case: start the real server, `kill -TERM`,
    assert exit 0 and no `server.json`.
  - Import-graph test (child 1's) is extended to include `lifecycle.py` and `cli_conduct`'s lazy
    import; `completion` test passes for the new commands.
- **Docs check**: a test asserts every key in the `agentic.dashboard` defaults appears in the
  `docs/WORKFLOW.md` "Remote dashboard" section.
- **Manual verification** (this host; recorded in this spec before `done`):
  1. `nw dashboard unit --print`, then `nw dashboard unit --install`; `systemctl --user status
     nw-dashboard` shows active with tailscale logged in (or `activating (auto-restart)` with
     exit 3 and the cause in `journalctl --user -u nw-dashboard` when tailscale is logged out).
  2. `nw dashboard status` shows unit, pid, url, tailscale `Running`, whois `ok`, clients;
     `nw dashboard status --json | jq .ok` is `true`.
  3. Merge a change under `src/nightwright` into the checkout `nw` runs from; within
     `code_check_s` the journal shows `code_changed` and a new pid with a later start time.
  4. `systemctl --user restart nw-dashboard` and `systemctl --user stop nw-dashboard` leave the
     expected `status` output; stop removes `server.json`.
  5. With linger on, log out and in (or reboot): the unit is running without a manual start.
  6. `nw queue conduct-status` shows the dashboard line.
- **Regression guard**: affected tests (`python3 tools/affected_tests.py --run uv run pytest
  -q`) plus the files touched; `conduct-status` snapshot tests for the other keys are
  unchanged; the epic's read-only and import-graph tests pass; `python3 tools/spec_lint.py`.

## Rollout / migration

1. Land after epic child 1 (`nw serve --dashboard`). No data migration; `server.json` is
   created on first start and absent otherwise.
2. A human who already runs `nw serve --dashboard` by hand stops it, runs
   `nw dashboard unit --install`, and follows the printed steps (linger, Tailscale).
3. `conduct-status` consumers see a new `dashboard` key (`null` when the dashboard is not
   running); existing keys are unchanged.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected automated tests pass; manual steps 1-6 performed and noted
      here.
- [ ] No regressions (the epic's read-only and import-graph tests pass).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).
- [ ] `python3 tools/spec_lint.py` exits 0.

## Open questions

None. Whether the `systemctl --user` start/stop/restart-on-code-change checks pass can only be
confirmed on the host after the human has logged in to Tailscale; the manual verification
covers it, and the dashboard fails closed until then.
