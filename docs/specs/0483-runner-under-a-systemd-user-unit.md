---
id: SPEC-0483
title: The queue runner runs under a systemd user unit and resolves its own credentials
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, queue, runner, systemd, credentials, resilience]
supersedes: [SPEC-0291]
depends_on: [SPEC-0482]
---

## Context

After the 2026-11-08 21:19 crash (SPEC-0482) nothing restarted the runner for about 10 hours.
The watchdog emitted `runner_dead` once and then `conductor_ping` events with `"runner": null`;
the conductor never acted. SPEC-0291 made `nw queue run --detach` / `nw queue stop` the way to
start and stop the runner and rejected a systemd user unit, following SPEC-0169's non-goal "a
persistent supervisor (systemd)". **The human has reversed that decision**: the runner must be
supervised by a systemd user unit. This spec supersedes SPEC-0291 and records that SPEC-0169's
systemd non-goal no longer holds.

The 21:11 relaunch also lost `CURSOR_API_KEY`: nightwright only ever had the key through process
inheritance. A long-lived runner had it and kept it across re-execs; a session stopped it during
a drain and started a fresh `nw queue run --detach` from a shell without it. The human keeps
tokens in `pass` (gpg) and loads them into fish with a function running `pass show <entry>` per
var (`~/.config/projects/api-tokens.json`: `CURSOR_API_KEY -> cursor/sdk`).

## Goals / Non-goals

**Goals**
- A crashed runner is restarted without a human; a crash loop stops and escalates.
- Every runner start, whoever triggers it, gets its credentials, and none is ever written, logged
  or emitted.
- When the unit is installed, no launch path spawns a runner outside it.

**Non-goals**
- Installing or starting the unit on any machine (the human runs `nw queue install`).
- A `pi` credential resolver beyond the same `agentic.credentials` map (it works for any var).
- Changing the conductor launch (`nw queue conductor-launch` starts the conductor session, not a
  runner; the conductor's own `nw queue run --detach` / `stop` / `restart` delegate).

## Decision

1. **`nw queue install | uninstall`** (mirrors `nw dashboard install`, SPEC-0427) writes
   `~/.config/systemd/user/nw-runner.service` and `nw-runner-failed.service`, then
   `daemon-reload` + `enable --now` (a running unit is restarted onto the new text).
   `nw-runner.service`:
   - `ExecStart=<uv tool nw> queue run` in the foreground; `Type=simple`; `Restart=always`,
     `RestartSec=10`; `StartLimitIntervalSec=900`, `StartLimitBurst=5` (5 starts in 15 min is a
     crash loop); `OnFailure=nw-runner-failed.service`; journal logging; `WantedBy=default.target`.
   - `KillMode=process`, `TimeoutStopSec=120`: a stop or restart signals the runner only.
     Persona runs live in their own sessions and are adopted by the next runner
     (SPEC-0225/0229); the default `control-group` would kill them with it.
   - `Environment=NW_RUNNER_UNIT=nw-runner.service` (this process is the unit's), `PATH` (the
     installing shell's minus virtualenv / `.venv` dirs, plus `/usr/local/bin:/usr/bin:/bin`),
     `GNUPGHOME` / `PASSWORD_STORE_DIR` only when the installing shell sets them, the `NW_*_DIR`
     overrides as the dashboard unit does. **No credential and no `EnvironmentFile`.**
   - `nw-runner-failed.service` (`Type=oneshot`) runs the hidden `nw queue unit-failed`, which
     files one queue-wide escalation (`kind: runner_unit_failed`, the unit's `Result`,
     `NRestarts`, last exit status and the journalctl command) while none is open. With
     `Restart=always` the unit only enters `failed` (and fires `OnFailure=`) when the start limit
     is hit.
2. **Delegation while the unit file exists** (`runner_unit.installed()`; a file check, no
   systemctl):
   - `detach_runner` (`nw queue run --detach`, the start phase of `nw queue restart`) runs
     `systemctl --user start nw-runner.service` and waits for the lease (30 s); its `argv`,
     `env` and `cwd` are ignored (restart records `env_used: unit`).
   - `stop_runner` (`nw queue stop`, the stop phase of `nw queue restart`) keeps every refusal
     and the close guard, then runs `systemctl --user stop --no-block nw-runner.service` instead
     of SIGTERM when the unit is active, and waits for the lease as before. A systemctl stop is
     deliberate: systemd does not restart it.
   - A foreground `nw queue run` without `NW_RUNNER_UNIT` refuses while the unit is active, with
     a message naming the unit, `nw queue stop|restart`, `systemctl --user restart
     nw-runner.service` and the journalctl command.
   - The code-change re-exec (SPEC-0194/0229) is unchanged: it is an `exec` of the unit's own
     pid, which systemd keeps supervising with the unit's environment; no runner is spawned.
3. **`agentic.credentials`** (`config.yaml`): `VAR: pass:<entry>` or `VAR: env`. The runner
   calls `credentials.ensure(cfg)` at start and at the top of every tick. A var already set wins
   and is never re-read. A missing `pass:` var is resolved with `pass show <entry>`: stdin
   `/dev/null`, no TTY (new session), `GPG_TTY` removed, `PASSWORD_STORE_GPG_OPTS` gains
   `--batch --pinentry-mode error` so gpg fails instead of asking for a passphrase, a 5 s
   timeout; the first stdout line goes into `os.environ`, so persona children inherit it.
   A failure (locked agent, missing entry, no `pass`, timeout) never raises: the var stays
   unset, SPEC-0482's cursor preflight holds the steps that need it, one escalation
   (`kind: credential_unresolved`) names the var, the source and the fix ("unlock gpg: run
   keys-setup or `pass show cursor/sdk` once"; the escalation's notifier covers notify-send),
   and the next tick tries again. A later success journals `credential_resolved` and withdraws
   the escalation; no restart is needed. Records carry the var, the entry and a status, never a
   value. `nw doctor`'s `agentic.cursor` check reports `resolves` / `locked` / `missing`.

## Design

- `src/nightwright/agentic/runner_unit.py` (new): `render_unit`, `render_failed_unit`,
  `unit_env`, `unit_path` (honours `NW_SYSTEMD_USER_DIR`, for tests), `installed`, `active`,
  `foreground_refusal`, `start`, `stop`, `escalate_failed`, `install_commands`,
  `uninstall_commands`, `systemctl`.
- `src/nightwright/agentic/credentials.py` (new): `settings`, `fetch`, `status`, `ensure`,
  `fix_text`.
- `queue.detach_runner` / `queue.stop_runner` delegate (`systemctl_run` injectable);
  `restart._start` marks `env_used: unit`; `queue.tick` / `queue.run` call `CR.ensure`.
- `cli_queue`: `queue install`, `queue uninstall`, hidden `queue unit-failed`, the foreground
  refusal in `queue run`.
- `config.py`: `agentic.credentials: {}` default, documented. `doctor.check_cursor_agent`.
- `tests/conftest.py`: an autouse fixture points `NW_SYSTEMD_USER_DIR` at an empty temp dir and
  removes `NW_RUNNER_UNIT`, so a host with the unit installed never delegates in tests.
- SPEC-0291 is `superseded` by this spec. SPEC-0169 gets a dated note under its non-goal.

## Alternatives considered

- **The watchdog (events follower) restarts the runner.** It only runs while a conductor
  session follows the stream, and a relaunch from it inherits the follower's environment, the
  21:11 failure mode. Rejected by the human in favour of systemd.
- **`EnvironmentFile=` with an `agent.env` file, or persisting the key from a runner that has
  it.** Puts a plaintext copy of the token on disk next to the human's encrypted store. Rejected
  by the human; credentials come from `pass`.
- **`KillMode=mixed`.** Its final SIGKILL reaches the persona runs in the cgroup.
- **Exit and let systemd restart on a code change** instead of the in-place exec. Opens a
  `runner_dead` window of `RestartSec` on every merge and drops the lease handover; the exec
  already stays under the unit.

## Acceptance criteria

- [ ] The rendered unit has `ExecStart=<nw> queue run`, `Restart=always`, the start limit,
      `OnFailure=nw-runner-failed.service`, `KillMode=process`, a PATH with `/usr/bin`, and no
      `EnvironmentFile` or credential; `GNUPGHOME` / `PASSWORD_STORE_DIR` appear only when set.
- [ ] `systemd-analyze --user verify` accepts both rendered units (real binary, temp dir).
- [ ] With the unit file installed, `detach_runner`, `stop_runner` and restart's start phase go
      through `systemctl --user` and never spawn or signal a process themselves; a foreground
      `nw queue run` refuses with a message naming `nw-runner.service` while it is active, and
      runs when `NW_RUNNER_UNIT` is set.
- [ ] `nw queue unit-failed` files one `runner_unit_failed` escalation and no second while it
      is open.
- [ ] `credentials.ensure` resolves a `pass:` var through a fake `pass` (no TTY, `GPG_TTY` unset,
      `--pinentry-mode error`), reports `locked` on failure and timeout without raising, files one
      escalation, retries on the next call, and withdraws the escalation once it resolves; the
      cursor gate holds while unresolved and admits after; the process env wins.
- [ ] No credential value appears in the runner journal, the event stream or an escalation.
- [ ] `nw doctor`'s `agentic.cursor` reports a resolving / locked credential without its value.

## Test plan

- **Automated tests** (`tests/test_runner_unit.py`, `tests/test_credentials.py`):
  unit rendering and `unit_env`; `systemd-analyze --user verify` (skipped without the binary);
  `detach_runner` / `stop_runner` / `restart._start` with a fake `systemctl_run` that takes and
  releases the lease, `popen` and `kill` failing the test if called; the CLI refusal and its
  bypass; `escalate_failed` dedupe; `install --dry-run` output; a fake `pass` on PATH for
  resolved / locked / timeout / env-wins / held-then-resumed; value absent from `runner.jsonl`,
  `events.jsonl` and the escalations dir; the doctor check.
- **Manual verification:** `systemd-analyze --user verify` on the rendered units; `pass --version`
  only (no `pass show` on the real store, which could raise a pinentry). The unit is not
  installed here: the human installs it after landing.
- **Regression guard:** affected tests plus the touched files; SPEC-0291's and SPEC-0375's
  tests keep passing with the unit absent (the conftest fixture).

## Rollout / migration

1. Land. 2. The human adds `agentic.credentials: {CURSOR_API_KEY: pass:cursor/sdk}` to
`config.yaml`, stops the hand-started runner (`nw queue stop --yes`) and runs
`nw queue install`. 3. `systemctl --user status nw-runner.service`; `nw doctor`.
`nw queue uninstall` reverts to SPEC-0291's `--detach` behaviour.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass; manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
