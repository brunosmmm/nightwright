---
id: SPEC-0427
title: "Dashboard service: nw dashboard install, uninstall and status over a systemd user unit"
status: accepted
owner: user
created: 2026-11-04
updated: 2026-11-04
milestone: "M9: agentic workflow"
kind: feature
source_idea: IDEA-962
parent: SPEC-0400
tags: [agentic, dashboard, remote, lifecycle, systemd]
depends_on: [SPEC-0404, SPEC-0414, SPEC-0416]
---

## Context

Promoted from idea `IDEA-962`. Child of epic
[SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md). The epic's Decision 7
chose a systemd user unit to supervise the dashboard server. Its child 4 ("Dashboard
lifecycle") listed three pieces: the unit installer, the code-change restart and
`nw dashboard status`.
[SPEC-0404](./0404-dashboard-lifecycle-systemd-user-unit-code-change.md) took that child's idea
(`IDEA-877`) and split it in three. It shipped only the first split, the server-process
contract:

- `lifecycle.py` with `server.json` and `record_live`;
- the code-change exit 0;
- exit 3 when there is no tailnet address;
- the hidden `--code-root` / `--no-code-watch` flags.

Its Non-goals named the two other splits ("each is a sibling split of IDEA-877, to be
captured as its own spec"), but nobody captured them. This spec covers **both** of those
splits. The human asked for it directly (2026-11-04): `nw serve --dashboard` must run as a
`systemctl --user` service.

What exists, checked against the code on 2026-11-04:

- `nw serve --dashboard` (`cli_worker._serve_dashboard`, SPEC-0414/0416/0404) binds
  `127.0.0.1:8791` by default. Its exit codes:
  - 0 on SIGTERM/SIGINT or a code change;
  - 2 on a refused bind or a TLS flag (`click.UsageError`);
  - 3 on `auth.TailnetMissing`;
  - 1 on an `OSError` while binding.

  `auth.resolve_bind` still refuses every non-loopback address with exit 2 until the whois
  identity gate is wired into the server (a parallel slice). A tailnet bind today therefore
  exits 3 when Tailscale is down and 2 when it is up.
- `server.json` (`lifecycle.read_server_record`, `record_live`) carries `pid`, `url`, `bind`,
  `code_sha` and `code_watch`. It does not say which checkout the code watch follows.
- `nw dashboard` has only `snapshot` (SPEC-0401).
- `nw queue conduct-status` (`cli_conduct.conduct_status`) shows `herdr_watch` (SPEC-0365), the
  way this spec wants the dashboard shown. `nw doctor` (SPEC-0349) runs one `check_*` per line,
  with an injectable `run_cmd`.
- The human's `nw` is an editable `uv tool` install at
  `~/.local/share/uv/tools/nightwright/bin/nw`. In a worktree, `uv run nw` puts that worktree's
  `.venv/bin` first on PATH. "nw on PATH" is therefore the wrong binary to pin when an agent
  installs from its worktree.
- A systemd **user** manager cannot order against **system** units. `After=tailscaled.service`
  in a user unit names a user-level unit, and on this host that unit is `not-found`.

## Goals / Non-goals

**Goals**
- G1. `nw dashboard install [--bind loopback|tailscale] [--port N] [--dry-run]` writes
  `~/.config/systemd/user/nw-dashboard.service`, then runs `daemon-reload` and `enable --now`.
  `--dry-run` prints the unit and the commands.
- G2. The unit restarts the server onto new code after its exit 0 and stops on exit 2. It never
  hot-loops on exit 3. When Tailscale comes up later, the server binds by itself within
  seconds.
- G3. `nw dashboard uninstall` stops, disables and removes the unit, then runs
  `daemon-reload`.
- G4. `nw dashboard status [--json]` reports:
  - whether the unit is installed, enabled and active;
  - the pid and the URL;
  - whether the server runs the checkout's current code;
  - the log command and linger.

  When linger is off it prints `loginctl enable-linger <user>`.
- G5. `nw queue conduct-status` gains a dashboard line and `nw doctor` a dashboard check. Both
  warn when the unit is installed but not active.

**Non-goals**
- Running `loginctl enable-linger`. It changes the human's system config, so it is only
  printed.
- Installing the human's real unit. The human runs `nw dashboard install`.
- The work of the parallel dashboard-server slices: the whois identity gate, lifting the
  tailnet bind refusal, the token-on-loopback change, CSRF, SSE and the inbox. This spec does
  not touch `server.py`, `auth.py`, the page, `inbox.py` or `audit.py`.
- The `tailscale state` and `whois self-check` fields that the epic's Decision 7 listed for
  status. They need the identity gate, which is not in the server yet. Status reports the bind,
  and the unit state shows the exit-3 wait.
- A system-level (root) unit, or any other supervisor.

## Decision

1. **Command names.** The commands are `nw dashboard install | uninstall | status`, as the
   human asked. They replace the epic's `nw dashboard unit --install|--uninstall|--print`
   spelling; `install --dry-run` does what `--print` would have done.
2. **Restart policy: `Restart=always`, `RestartSec=5`.**
   - `Restart=always` is chosen over `on-failure` on purpose. The code-change exit is exit
     **0** (SPEC-0404), and `on-failure` would not restart it.
   - `systemctl --user stop` still stops the unit: systemd never auto-restarts a unit that is
     being stopped.
   - Five seconds keeps a code-change restart short (about 6 s with Python start-up).
3. **Exit 2 stops the unit: `RestartPreventExitStatus=2`.** Retrying cannot fix a refused bind
   or a bad flag.
4. **Exit 3 cannot hot-loop, because the server waits in-process (`--tailnet-wait`).**
   - New option: `nw serve --dashboard --tailnet-wait SECONDS`. While `resolve_bind` raises
     `TailnetMissing`, the server retries every 5 s and exits 3 only after `SECONDS`.
   - The default is 0, so a server run by hand keeps SPEC-0404's immediate exit 3.
   - A tailscale unit passes `--tailnet-wait 60`. During an outage that gives one exit 3 and
     one restart about every 65 s. The server binds within 5 s of Tailscale coming up.

   Rejected options:
   - `RestartPreventExitStatus=3`: after a boot race with `tailscaled`, the dashboard stays
     down until a human restarts it.
   - Systemd's own backoff (`RestartSteps`/`RestartMaxDelaySec`): it counts every restart, and
     only a manual start resets the counter. The many code-change restarts would push the
     delay to its maximum.
   - A bare `StartLimit*`: an exit 3 every 5 s trips the limit within a minute, and the unit
     then stays failed.
5. **Crash-loop bound: `StartLimitIntervalSec=300`, `StartLimitBurst=20`** (in `[Unit]`).
   - A process that exits 1 at once (for example a port in use) stops after 20 starts, in
     about 2 minutes.
   - The exit-3 cadence (at most 6 starts per 300 s) never reaches the limit.
   - Code changes never reach it either: each one needs a new commit under `src/nightwright`,
     `pyproject.toml` or `skills`.
6. **Tailscale ordering.** `After=tailscaled.service` is added only when such a unit exists, in
   user or system scope. The unit never uses `Requires`, `Wants` or `BindsTo`. A user manager
   cannot order against a system unit, so with the usual system `tailscaled` this line has no
   effect, and the docs say so. The real guard is Decision 4.
7. **Which `nw` the unit runs.** The first that exists:
   1. `--nw-bin PATH`;
   2. the `uv tool` install: `nightwright/bin/nw` under `$UV_TOOL_DIR`, else under
      `$XDG_DATA_HOME/uv/tools`, else under `~/.local/share/uv/tools`;
   3. `nw` on PATH, with symlinks resolved;
   4. the `nw` next to the running interpreter.

   The uv tool path comes before PATH, so that an install from a worktree's `uv run` does not
   pin that worktree's `.venv`.
8. **Environment.**
   - `Environment=NW_CONFIG_DIR=…` and `Environment=NW_DATA_DIR=…` are written only when the
     installing shell resolves them to a directory other than the default
     (`~/.config/nightwright`, `~/.local/share/nightwright`). The check uses
     `paths.config_dir()` / `data_dir()`, which also honour `XDG_*_HOME`.
   - `PYTHONUNBUFFERED=1`.
   - Stdout and stderr go to the journal, with `SyslogIdentifier=<unit name>`.
   - `WantedBy=default.target`.
9. **A re-install restarts the unit.** When the unit is already active, `install` adds
   `systemctl --user restart`. `enable --now` alone would leave the old process running on the
   old unit text.
10. **`server.json` gains `code_root`.**
    - `_serve_dashboard` writes it right after the record, when the code watch is on.
    - It names the checkout the code watch follows. `status` then compares `code_sha` with that
      checkout's HEAD, not with the checkout the status process runs from.
    - The rewrite is a second atomic write. `server.py` is not touched.
11. **Status.** `lifecycle.status(cfg, …)` builds `nw.dashboard.status.v1`:

    ```
    {unit: {name, path, installed, reachable, enabled, active, state, sub_state,
            main_pid, n_restarts, result, exit_status},
     running,
     server: {pid, url, bind, started_at, code_watch, code_sha, clients, code_root} | null,
     code: {watched, running_sha, head_sha, current, root},
     linger, linger_fix, logs, ok, problems, warnings}
    ```

    It reads `systemctl --user show -p …` and `loginctl show-user <user> -p Linger`.
    - `problems`: the unit is installed but not active; it is active without a live
      `server.json`; the server runs code other than the checkout's HEAD; systemctl cannot be
      reached.
    - `warnings`: the service is not installed; the unit is not enabled; linger is off.
    - `nw dashboard status` exits 1 when there are problems, else 0.
12. **Integration.** `lifecycle.brief(cfg)` returns None, and runs no subprocess, when there is
    no unit file and no live server.
    - `conduct_status` adds the `dashboard` key (lazy import; `None` on any error). Its text
      form prints a `dashboard:` line, yellow when not ok.
    - `nw doctor` adds the `agentic.dashboard` check:
      - skip when the service is not installed;
      - ok when a server is running by hand;
      - warn when the unit is installed but not active. The fix is
        `systemctl --user restart …` plus the journal command;
      - warn on any other problem;
      - warn when linger is off, with the fix `loginctl enable-linger <user>`;
      - ok when the unit is active.
13. **Test hooks.** `install`, `uninstall` and `status` take a hidden `--unit-name`. `install`
    also takes a hidden `--code-root`, which it passes to the server. A real unit can thus be
    installed under a scratch name without touching `nw-dashboard.service`.

## Design

**Files**
- `src/nightwright/agentic/dashboard/lifecycle.py`:
  - unit text and paths: `UNIT_NAME`, `RESTART_SEC`, `TAILNET_WAIT_S`, `START_LIMIT_*`,
    `unit_name`, `unit_dir`, `unit_path`, `logs_command`, `resolve_nw_bin`, `env_overrides`,
    `unit_bind`, `exec_args`, `render_unit`;
  - commands: `install_commands`, `uninstall_commands`, `shell_line`;
  - probes: `tailscaled_unit_exists`, `systemctl_show`, `linger_enabled`, `linger_fix`;
  - documents: `status`, `brief`, `brief_line`.
  - Every subprocess goes through `run=None`, which means `subprocess.run` looked up at each
    call, so tests can inject or patch it.
  - Unit words are quoted with `_q`: `%` becomes `%%`, `$` becomes `$$`, and a word with
    whitespace or quotes is double-quoted.
- `src/nightwright/agentic/cli_dashboard.py`: the `install`, `uninstall` and `status` commands
  and `_print_status`. After `enable --now`, `install` polls the status for up to 15 s and
  prints it. It stops polling once `server.json` is live or the unit is no longer active.
- `src/nightwright/agentic/cli_worker.py`:
  - `--tailnet-wait` and `_resolve_dashboard_bind`: one `retrying for up to Ns` line, then a
    poll every 5 s. SIGINT during the wait exits 0.
  - the `code_root` rewrite of `server.json`.
- `src/nightwright/agentic/cli_conduct.py`: `_dashboard_status` and the render line.
- `src/nightwright/doctor.py`: `check_dashboard(cfg, run_cmd=)`, run after `agentic.workers`.
- `docs/WORKFLOW.md` gets a "Remote dashboard" section: the commands, how the unit reacts to
  each exit code, what happens when Tailscale comes up later, the config keys and the Tailscale
  steps.
- `skills/nw-orient/SKILL.md` gets one line on `nw dashboard status`.

**The unit** (loopback, default dirs):

```
[Unit]
Description=nightwright remote dashboard (nw serve --dashboard)
StartLimitIntervalSec=300
StartLimitBurst=20

[Service]
Type=simple
ExecStart=/home/<u>/.local/share/uv/tools/nightwright/bin/nw serve --dashboard --bind 127.0.0.1:8791
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5
RestartPreventExitStatus=2
StandardOutput=journal
StandardError=journal
SyslogIdentifier=nw-dashboard

[Install]
WantedBy=default.target
```

With `--bind tailscale` the bind becomes `tailscale:PORT` and `--tailnet-wait 60` is appended.
`After=tailscaled.service` is added when that unit exists.

**Invariants**
- No queue-side module imports `agentic.dashboard`. `cli_conduct` and `doctor` import it lazily
  inside a function, and `tests/test_dashboard_imports.py` still passes.
- The status path is read-only.
- `install` writes only the unit file, and `uninstall` removes only that file.
- Nothing here runs `loginctl enable-linger`.

## Alternatives considered

- **`Restart=on-failure` with a non-zero code-change exit.** SPEC-0404 already rejected it:
  every code change would show as a failure in the journal.
- **`RestartPreventExitStatus=3`.** It does not recover after Tailscale comes up (Decision 4).
- **`RestartSteps` / `RestartMaxDelaySec` backoff.** The counter keeps growing across
  code-change restarts (Decision 4).
- **A companion `.timer` that restarts a failed unit.** It is a second unit to install and
  explain. It would also restart a unit the human stopped on purpose.
- **An `ExecStopPost` sleep on exit 3.** It leaves the unit `deactivating` for a minute, which
  status would misreport, and it puts shell code in the unit file.
- **`Requires=`/`Wants=tailscaled.service`.** The human ruled out a hard requirement, and a user
  manager cannot see the system unit anyway.

## Acceptance criteria

- [ ] `nw dashboard install` writes `~/.config/systemd/user/nw-dashboard.service` (honouring
      `XDG_CONFIG_HOME`). It then runs `systemctl --user daemon-reload` and `enable --now`, and
      adds `restart` when the unit was already active. `--dry-run` prints the unit path, the
      unit text and the commands, and changes nothing.
- [ ] The unit text is as decided:
  - [ ] `ExecStart` is the resolved `nw` (`--nw-bin`, else the uv tool install, else PATH
        resolved, else the interpreter's sibling), followed by `serve --dashboard --bind
        127.0.0.1:PORT` (default 8791) or by `--bind tailscale:PORT --tailnet-wait 60`.
  - [ ] It carries `Environment=NW_CONFIG_DIR/NW_DATA_DIR` only for non-default directories,
        and logs to the journal.
  - [ ] It has `Restart=always`, `RestartSec=5` and `RestartPreventExitStatus=2`, plus
        `StartLimitIntervalSec=300` and `StartLimitBurst=20` in `[Unit]`.
  - [ ] It has `After=tailscaled.service` only for a tailscale bind and only when that unit
        exists, and never `Requires` or `Wants`.
- [ ] Under real `systemctl --user`:
  - [ ] a code-change exit 0 makes systemd restart the server: a new pid, `NRestarts` up by
        one, and the page served again;
  - [ ] a tailscale unit with no tailnet address exits 3 about once a minute, not in a hot
        loop, and never reaches `start-limit-hit`.
- [ ] `nw serve --dashboard --tailnet-wait S` retries a missing tailnet address at least every
      5 s for up to S seconds, then exits 3. It stops waiting as soon as an address appears.
      `--tailnet-wait 0`, the default, keeps SPEC-0404's immediate exit 3.
- [ ] `nw dashboard uninstall` runs `disable --now` and `reset-failed`, removes the unit file,
      runs `daemon-reload`, and reports each step. On a host without the unit it says the unit
      is not installed and exits 0.
- [ ] `nw dashboard status [--json]` reports:
  - [ ] whether the unit is installed, enabled and active, its main pid and `NRestarts`;
  - [ ] the server's pid, URL and bind from a live `server.json`;
  - [ ] whether the running `code_sha` equals the HEAD of the watched checkout
        (`server.json` `code_root`);
  - [ ] the `journalctl --user -u <unit> -f` command and linger;
  - [ ] with linger off, `loginctl enable-linger <user>`, which it prints and never runs;
  - [ ] exit 1 on a problem (installed but not active, active without a live server, stale
        code), else 0.
- [ ] `nw queue conduct-status` has a `dashboard` key and a `dashboard:` line (installed,
      active, URL, problems). `nw doctor` has an `agentic.dashboard` check that warns when the
      unit is installed but not active. With no unit file and no live server, neither runs a
      subprocess.
- [ ] No queue-side module imports the dashboard package (the import-graph tests pass).
      Affected tests pass and `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated** (`tests/test_dashboard_service.py`):
  - Unit text:
    - the loopback unit: the restart keys, the StartLimit keys in `[Unit]`, no After or
      Requires, no tailnet wait, no env for default dirs;
    - the exit-3 cadence stays under the start limit, and a crash loop does not;
    - the tailscale unit: `--tailnet-wait 60`, and a soft `After=` only when the unit exists;
    - quoting of paths with spaces, `%` and `$`;
    - `env_overrides` for default, NW_* and XDG_* dirs;
    - the `resolve_nw_bin` order, and `unit_bind` / `unit_name` validation.
  - `status`, with an injected fake `run`:
    - active, live and on current code gives ok;
    - after the checkout moves on, the code is stale;
    - failed, linger off, active without `server.json`, and an unreachable user manager each
      give the matching problem or warning;
    - with no unit, `brief`, doctor and conduct-status run no subprocess;
    - a server run by hand is shown.
  - The doctor `agentic.dashboard` check (ok; warn for linger off; warn for not active) and
    `conduct_status()["dashboard"]`.
  - Real `nw` subprocesses, with a fake `systemctl` / `loginctl` on PATH:
    - `install --dry-run` writes nothing;
    - `install` writes the unit and runs `daemon-reload` and `enable --now`;
    - `status`, text and `--json`, exits 1 and prints the linger command, which is never
      executed;
    - a re-install while active restarts the unit;
    - `uninstall` runs its steps in order, and a second `uninstall` says not installed.
  - Real `nw serve --dashboard` subprocesses, with a fake `tailscale`:
    - `--tailnet-wait 2` exits 3 after at least 2 s, with one `retrying` line;
    - the wait ends within one poll once the fake starts reporting an address.
- **Manual** (real systemd in this user session; recorded under As shipped):
  1. Prepare scratch `NW_CONFIG_DIR`/`NW_DATA_DIR` (with `code_check_s: 1`), a scratch git code
     root, a free loopback port, and the unit name `nw-dashboard-test`.
  2. Run `install`, then `systemctl --user is-active`, then an HTTP GET of `/`.
  3. Commit under the scratch code root. Expect a new pid, `NRestarts` up by one, and the page
     served again. Then run `nw dashboard status`.
  4. Separately, with Tailscale logged out, install a `--bind tailscale` unit named
     `nw-dashboard-test3`. Sample `NRestarts` for about 3 minutes and expect about one exit 3
     per 65 s.
  5. Run `uninstall` for both units. Expect `LoadState=not-found` and no file left behind.

  Do not install the real `nw-dashboard.service`, and do not enable linger.
- **Regression guard.** Run `tools/affected_tests.py`, plus `tests/test_doctor.py`,
  `tests/test_dashboard_lifecycle.py`, `tests/test_dashboard_imports.py`,
  `tests/test_herdr_autowatch.py` and the completion tests, then `spec_lint`.

## Rollout / migration

1. Land. Nothing changes on a host without the unit: the doctor check skips, and
   conduct-status prints `dashboard: not installed`.
2. The human runs `nw dashboard install` once (loopback). Optionally, they also run
   `loginctl enable-linger $USER` so that the unit survives logout. `--bind tailscale` is for
   later, once the server accepts a tailnet bind.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass; manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None._
