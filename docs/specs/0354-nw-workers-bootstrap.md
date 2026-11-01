---
id: SPEC-0354
title: "nw workers bootstrap"
status: accepted
owner: user
created: 2026-11-01
updated: 2026-11-01
source_idea: IDEA-559
parent: SPEC-0310
tags: [agentic, remote, workers, ops]
depends_on: [SPEC-0337]
---

## Context

Adding a worker today means installing nightwright on it by hand and writing the
forced-command `authorized_keys` line yourself. The bootstrap half of
[SPEC-0343](./0343-worker-bootstrap-and-drain.md) never needed the job records of
[SPEC-0341](./0341-coordinator-re-attach-job-records-and-adoption.md): it needs only the
worker commands of [SPEC-0336](./0336-gate-worker-job-runner-and-forced-command.md) and
`nw workers add|check` of [SPEC-0337](./0337-worker-registry-and-multi-pool-gate-slots.md),
both on main. The human wants it now, ahead of the remote-job chain, so it is split out
(SPEC-0343 becomes drain-only).

The common real case: the human already has a dedicated unprivileged user and nightwright on
the worker, and wants only the key step automated. The command therefore must skip what is
already done.

## Goals / Non-goals

**Goals**
- `nw workers bootstrap NAME --ssh USER@HOST` installs nightwright on a worker, installs the
  forced-command key line, and registers the worker.
- Idempotent: steps already done are skipped; a per-step status table says what happened.
- `--dry-run` prints every step and command without running anything.
- Optional `--admin USER@HOST` creates the dedicated worker user with sudo when missing.

**Non-goals**
- Persona prerequisites (`claude` login): SPEC-0344's `check --auth`.
- Automatic worker upgrades (re-running bootstrap with a newer checkout does reinstall, since
  the version differs; no scheduled upgrades).
- Draining (SPEC-0343).

## Decision

Bootstrap installs a wheel of the coordinator's own checkout, so both sides run the same
code. Everything runs over the human's own ssh session (their key, not the forced command).
Each step first checks whether it is needed.

## Design

`nw workers bootstrap NAME --ssh DEST [--slots N] [--install-key] [--admin USER@HOST]
[--dry-run]`. `DEST` is the worker account (e.g. `nw@box`), never root.

Steps, in order, each reported in a table (`done`, `skipped (already ...)`, `would run`,
`failed`, `not requested`):

0. **admin (only with `--admin`).** Over `ssh ADMIN`, `sudo -n id -u DESTUSER` decides whether
   the user exists; if not, `sudo -n useradd -m DESTUSER` (no sudo/wheel group, no password).
   If `sudo -n` fails because a password is needed, stop with `sudo on ADMIN needs a
   password; configure NOPASSWD for useradd or create the user yourself` and change nothing.
   Then proceed as `DEST`. Without `--admin` the user must already exist.
1. **install.** `ssh DEST nw worker info` reports the installed version. If it equals the
   coordinator's version, skip. Otherwise `uv build --wheel`, `scp` the wheel, then
   `ssh DEST uv tool install --force <wheel>`.
2. **verify.** `ssh DEST nw worker info` must succeed and report that version; else fail.
3. **key.** The line is built from `agentic.workers.key` (default `~/.ssh/id_ed25519.pub`) in
   the SPEC-0336 forced-command form with `restrict`. Without `--install-key` it is printed
   and the table shows `not requested`. With `--install-key`:
   - Refuse (before any remote write) if the account in `--ssh` is `root`, or
     `ssh DEST id -u` returns 0.
   - Verify the line to install is exactly `restrict,command="<forced command>" <pubkey>`;
     refuse any other shape (extra options, missing `restrict`, multiple lines).
   - Over the same ssh session: `mkdir -p ~/.ssh && chmod 700 ~/.ssh`, append the line only
     if not already present (`grep -qxF`), then `chmod 600 ~/.ssh/authorized_keys`. Present
     already -> `skipped`.
4. **register.** With `--install-key`: `nw workers add NAME --ssh DEST [--slots N]` with
   check, which now can succeed. Without: `nw workers add ... --no-check` and print `run nw
   workers check NAME after installing the key line`. If NAME is already registered with the
   same settings, skip.

The exit status is nonzero if any step failed. `--dry-run` runs only read-only local logic
(builds the plan), prints each step with its exact commands, makes no ssh/scp/uv/registry
call, and exits 0.

## Alternatives considered

- **Install from a package index.** The coordinator and worker could differ in version.
- **Always use sudo to manage the account.** Most users already have the worker user, so
  `--admin` is opt-in.
- **`ssh-copy-id`.** It cannot write a forced-command line.

## Acceptance criteria

- [ ] A fresh run builds, copies and installs the wheel, verifies `nw worker info`, and
      registers the worker; a status table lists every step.
- [ ] Re-running with nw already at the same version and the key line present skips install
      and key; nothing is duplicated in `authorized_keys`.
- [ ] `--install-key` creates `~/.ssh` (700), appends the exact `restrict` forced-command
      line once, sets `authorized_keys` to 600, and then runs `add` with check.
- [ ] Without `--install-key` the line is printed, the worker is added with `--no-check`,
      and the next step is printed.
- [ ] `--install-key` refuses a root account (by name or uid 0) and any line that is not the
      exact forced-command form with `restrict`, without touching the worker.
- [ ] `--admin` creates the worker user (`useradd -m`, no sudo group) when missing, skips it
      when present, and fails clearly when sudo needs a password.
- [ ] `--dry-run` prints every step and command and runs none (no ssh, scp, build, or
      registry write).

## Test plan

- **Automated tests** (`tests/test_workers_bootstrap.py`, offline), using the loopback fake
  `ssh`/`scp` of `tests/test_workers_registry.py` (SPEC-0336/0337/0338 style: the stand-in
  drops the destination and runs the command locally against a temp `HOME`), with a fake
  `uv`:
  - fresh run: sequence of calls and status table;
  - second run: install and key skipped, `authorized_keys` has one line, modes 700/600;
  - version mismatch triggers reinstall;
  - without `--install-key`: `--no-check` path and printed hint;
  - root account and malformed line are refused with no remote write;
  - `--admin`: user missing -> `useradd -m` (no group flags); present -> skipped; fake sudo
    demanding a password -> clear failure, nothing else run;
  - `--dry-run`: recording fakes see zero calls; output lists every command.
- **Manual verification:** `nw workers bootstrap box --ssh nw@box --install-key --dry-run`,
  then for real against a machine with an existing `nw` user, then `nw workers check box`.
- **Regression guard:** affected tests while iterating; SPEC-0337's registry tests run
  unchanged.

## Clock Log

Use `nw idea clock-in IDEA-559` / `nw idea clock-out IDEA-559`.

## Rollout / migration

New command only; no registry schema change.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided: version equality (not a hash) decides "already installed"; sudo is
non-interactive only (`sudo -n`).
