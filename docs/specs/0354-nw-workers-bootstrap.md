---
id: SPEC-0354
title: "nw workers bootstrap"
status: done
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

`source_idea: IDEA-559` is kept only for lineage: this spec was split out of SPEC-0343, whose
work was filed under IDEA-559. This spec does not implement IDEA-559's summary (worker-owned
async jobs). Clock time still goes to IDEA-559.

Today the coordinator reaches a worker with `workers.default_ssh_argv` (no `-i`, so it uses
the default identity). The registry record has no field for picking a key, and `nw workers
add` never stores `ssh_argv` (`src/nightwright/agentic/workers.py`,
`src/nightwright/agentic/cli_workers.py`). If the forced-command line is installed for the
human's own login key, it never takes effect. sshd uses the first `authorized_keys` line that
matches, and the human's unrestricted line is already there. So the worker needs its own key,
and the coordinator must be made to use it.

## Goals / Non-goals

**Goals**
- `nw workers bootstrap NAME --ssh USER@HOST` installs nightwright on a worker, installs the
  forced-command key line for a **dedicated worker key**, and registers the worker so the
  coordinator connects with that key.
- Idempotent: steps already done are skipped; a per-step status table says what happened.
- `--dry-run` prints every step and command without running anything.
- Optional `--admin USER@HOST` creates the dedicated worker user with sudo when missing.

**Non-goals**
- Persona prerequisites (`claude` login): SPEC-0344's `check --auth`.
- Installing `uv` on the worker (it is a prerequisite; bootstrap fails clearly without it).
- Generating the worker key pair (bootstrap prints the `ssh-keygen` command when it is
  missing; it does not write to the human's `~/.ssh`).
- Automatic worker upgrades (re-running bootstrap with a newer checkout does reinstall, since
  the version differs; no scheduled upgrades).
- Draining (SPEC-0343).

## Decision

Bootstrap installs a wheel built from the coordinator's own checkout, so both sides run the
same released version (`__version__`). A wheel install has no git checkout, so `git_sha` is
not available on the worker to compare. Setup steps run over the human's own ssh session.
That session uses the human's login key, and with `--admin` it goes through the admin
account. The coordinator then reaches the worker only with a separate, dedicated worker key,
which is bound to the forced command. That key is stored on the worker's registry record as
a new optional `identity` field. Each step first checks whether it is needed.

## Design

`nw workers bootstrap NAME --ssh DEST [--slots N] [--install-key] [--admin USER@HOST]
[--dry-run]`. `DEST` is the worker account (e.g. `nw@box`), never root; `DESTUSER` is its
user part.

**Worker key.** The new config key `agentic.workers.key` holds the path to the worker
**public** key (default `~/.ssh/nw_worker_ed25519.pub`). The private key is the same path
without `.pub`. The worker key is a dedicated key: it must differ from the key the human's
ssh session uses. Bootstrap checks both files exist and are readable before any remote step.
If either is missing, the run stops with `worker key <path> not found; create it with
ssh-keygen -t ed25519 -N '' -f <private path>`, before any remote step. `<pubkey>` below is
that file's single line (`<type> <base64 blob> [comment]`); more than one line is refused.

**Remote runner.** "Run `X` on the worker" means:
- without `--admin`: `ssh -o BatchMode=yes -o ControlPath=none DEST X`;
- with `--admin`: `ssh -o BatchMode=yes -o ControlPath=none ADMIN sudo -n -i -u DESTUSER X`.
  This applies to every step, whether or not step 0 just created the user. A user created
  this way holds only the forced-command line, so the human's own key cannot reach it
  directly.

`ControlPath=none` keeps bootstrap's sessions, which are unrestricted, out of the
coordinator's ControlMaster directory (`workers.ssh_dir`). A later coordinator connection
therefore cannot reuse one of them.

**Local pre-checks (before any remote call, including step 0):**
- Worker key files present and readable (above).
- Registry conflict: if NAME is already registered with a different `ssh`, `slots`, `nw` or
  `identity`, step 4 is `failed` with `worker NAME is registered with different settings;
  nw workers rm NAME first`. Every other step shows `not run`, nothing remote runs, and the
  exit is nonzero. A `nw` that differs only because `<bin>` is not yet known is not a
  conflict here; step 4 checks it again after step 1.

Steps, in order, each reported in a table (`done`, `skipped (already ...)`, `would run`,
`failed`, `not requested`, `not run`). A failed step stops the run, and every later step
shows `not run`.

0. **admin (only with `--admin`).** Over `ssh ADMIN`, first run `sudo -n true`. A nonzero
   exit stops the run with `sudo on ADMIN needs a password; configure NOPASSWD for useradd or
   create the user yourself`, and nothing is changed. Then `id -u DESTUSER` (no sudo) decides
   whether the user exists: present -> `skipped (already exists)`; missing -> `sudo -n
   useradd -m DESTUSER` (no `-G`, no sudo/wheel group, no password) -> `done`.
   Without `--admin`, the user must already exist and accept the human's ssh login as `DEST`.
1. **install.** On the worker, `command -v uv` must succeed; otherwise the step fails with
   `uv not found for DESTUSER on the worker; install uv system-wide`. Next, `uv tool dir
   --bin` gives `<bin>`. It must be an absolute path matching `^/[A-Za-z0-9._/-]+$`;
   anything else fails the step with `unexpected uv tool bin dir <value>`. Then `<bin>/nw
   worker info --json` reports the installed version. If it equals the coordinator's
   `__version__`, the step is `skipped (already <version>)`. Otherwise (missing or different
   version), bootstrap runs locally `uv build --wheel`, copies the wheel, and runs `uv tool
   install --force <remote wheel>` on the worker. Without `--admin` the wheel goes `scp` to
   `DEST:` a `mktemp -d` dir. With `--admin` it goes `scp` to `ADMIN:` a `mktemp -d` dir,
   chmod 755 for the dir and 644 for the wheel, removed afterwards.
2. **verify.** `<bin>/nw worker info --json` on the worker must succeed and report the
   coordinator's version. Otherwise the step is `failed` (`worker reports <v>, expected
   <v>` or the ssh error), later steps are `not run`, and the exit is nonzero.
3. **key.** The forced command is `<bin>/nw worker ssh-command`. The line is exactly
   `command="<bin>/nw worker ssh-command",restrict <pubkey>`, which is the SPEC-0336 form.
   Without `--install-key` the line is printed and the step shows `not requested`.
   With `--install-key`, these checks run before any remote write:
   - Refuse if the account in `--ssh` is `root`, or `id -u` on the worker returns 0.
   - Refuse unless the built line is exactly that form: no other options, `restrict`
     present, a single line, and `<bin>` validated as in step 1.
   - Read the remote `~/.ssh/authorized_keys` (missing = empty). If the worker public key's
     base64 blob appears on any line that is not exactly the built line, refuse with `key
     already authorized without the forced command; use a dedicated worker key`. This covers
     the human's login key used as the worker key, and an older forced line with another
     path.

   Then, on the worker: `mkdir -p ~/.ssh && chmod 700 ~/.ssh`. The line is appended only if
   it is not already present (`grep -qxF`), then `chmod 600 ~/.ssh/authorized_keys`. If the
   line is already present, the step is `skipped`. With `--admin` these commands run as
   DESTUSER through sudo, so the files belong to DESTUSER.
4. **register.** The record is `nw workers add NAME --ssh DEST --nw <bin>/nw
   --identity <private key path> [--slots N]`:
   - With `--install-key`, it runs with check. The check now goes through the dedicated key
     and so through the forced command.
   - Without `--install-key`, it runs with `--no-check` and prints `run nw workers check NAME
     after installing the key line`.
   - If NAME is already registered with identical `ssh`, `slots`, `nw` and `identity`, the
     step is `skipped (already registered)`. If it differs (including a `nw` that only became
     known after step 1), the step is `failed` with the conflict message above.

**Registry: dedicated key selection.** `nw workers add` gains `--identity PATH` (an absolute
path to a private key, not starting with `-`). It is stored as the optional record field
`identity`. `workers.normalize` appends `-i <identity> -o IdentitiesOnly=yes` to the
`ssh_argv` it builds when `identity` is set. Every coordinator connection to that worker
(check, gate jobs) then offers only the dedicated key. `_check_record` validates the field
when present. Records without it behave exactly as today, so the schema id stays
`nw.workers.v1`. `nw workers list --json` shows `identity`.

The exit status is nonzero if any step failed. `--dry-run` runs only the local pre-checks
and plan building. It prints every step with its exact commands, makes no
ssh/scp/uv/registry call, and exits 0 (nonzero only when a local pre-check fails). Under
`--dry-run`, `<bin>` is printed as the placeholder `<uv tool bin dir>`, so the forced
command reads `<uv tool bin dir>/nw worker ssh-command` and step 4 shows `--nw <uv tool bin
dir>/nw`.

## Alternatives considered

- **Install from a package index.** The coordinator and worker could differ in version.
- **Always use sudo to manage the account.** Most users already have the worker user, so
  `--admin` is opt-in.
- **`ssh-copy-id`.** It cannot write a forced-command line.
- **Reuse the human's login key with the forced line placed first.** That would lock the
  human out of the account, since sshd uses the first matching line. Appending it after the
  existing line has no effect. A dedicated key is the only way the restriction holds.
- **`--admin` copies the human's login key into the new user.** That gives an unrestricted
  login to an account that should only have the forced key. Running setup through `ADMIN`
  with `sudo -u` avoids it.
- **Compare `git_sha` as well as version.** A wheel install has no checkout, so the worker
  reports no `git_sha`. The check would never apply.
- **Store a full `ssh_argv` in the record.** That would freeze the coordinator's
  ControlMaster path into the registry. A small `identity` field composes with the defaults
  instead.

## Acceptance criteria

- [x] A fresh run resolves `<bin>` with `uv tool dir --bin`, builds, copies and installs the
      wheel, verifies `<bin>/nw worker info`, and registers the worker with `nw = <bin>/nw`
      and `identity = <worker private key>`. A status table lists every step.
- [x] Re-running with nw already at the same version, the key line present and the same
      registration skips install, key and register. Nothing is duplicated in
      `authorized_keys`.
- [x] A worker reporting a different version is reinstalled. If `nw worker info` still fails
      or reports another version afterwards, verify is `failed`, later steps are not run,
      and the exit status is nonzero.
- [x] `--install-key` creates `~/.ssh` (700), appends the exact line
      `command="<bin>/nw worker ssh-command",restrict <pubkey>` once, sets `authorized_keys`
      to 600, and then runs `add` with check.
- [x] `--install-key` refuses when the worker public key is already authorized by a line
      that is not the exact forced-command line. The registered worker connects with the
      dedicated key: its normalized `ssh_argv` contains `-i <identity> -o IdentitiesOnly=yes`.
- [x] A record without `identity` normalizes to the same `ssh_argv` as before this spec.
      `nw workers add --identity` rejects a relative path or one starting with `-`.
- [x] Without `--install-key` the line is printed, the worker is added with `--no-check`,
      and the next step is printed.
- [x] `--install-key` refuses a root account (by name or uid 0) and any line that is not the
      exact `command="...",restrict <pubkey>` form, without touching the worker.
- [x] A remote bin dir that is not an absolute path matching `^/[A-Za-z0-9._/-]+$` fails
      install with no key written.
- [x] A missing worker key file stops the run before any remote call, printing the
      `ssh-keygen` command.
- [x] `--admin` probes `sudo -n true` first. If sudo needs a password, it fails clearly and
      runs nothing else. Otherwise it creates the worker user (`useradd -m`, no group flags)
      when `id -u` says it is missing and skips it when present. A freshly created user then
      completes install, verify, key (files owned by that user, 700/600) and register, with
      every worker-side command run through `ADMIN` as `sudo -n -i -u DESTUSER`.
- [x] NAME already registered with a different `ssh`, `slots`, `nw` or `identity` fails
      register with `worker NAME is registered with different settings; nw workers rm NAME
      first`, and no remote command runs when the conflict is visible locally.
- [x] `--dry-run` prints every step and command (with the `<uv tool bin dir>` placeholder)
      and runs none (no ssh, scp, build, or registry write).
- [x] Bootstrap's own ssh/scp calls pass `-o ControlPath=none`.
- [x] `agentic.workers.key` is documented with the other `agentic.workers` settings.

## Test plan

- **Automated tests** (`tests/test_workers_bootstrap.py`, offline) use the loopback fake
  `ssh`/`scp` of `tests/test_workers_registry.py` (SPEC-0336/0337/0338 style). The stand-in
  drops the destination and runs the command locally against a temp `HOME`, and records
  each argv. There is also a fake `uv` (for `build`, `tool dir --bin` printing a temp
  absolute dir, and `tool install` writing a fake `nw` with a set version) and a fake `sudo`.
  A generated temp worker key pair is set through `agentic.workers.key`. Cases:
  - `test_fresh_run`: call sequence, status table, registered record has
    `nw=<bin>/nw` and `identity`;
  - `test_rerun_skips`: install, key and register skipped; `authorized_keys` has one line;
    modes 700/600;
  - `test_version_mismatch_reinstalls`: older fake `nw` is reinstalled;
  - `test_verify_fails_after_install`: fake install leaves the wrong version, so verify is
    `failed`, key/register are `not run`, and the exit is nonzero;
  - `test_key_line_form`: the appended line equals
    `command="<bin>/nw worker ssh-command",restrict <pubkey>`; a pre-existing identical line
    -> `skipped`;
  - `test_key_already_authorized_unrestricted`: the blob on a plain line, or on a forced
    line with another path, gives the refusal message, `authorized_keys` is byte-for-byte
    unchanged, and nothing is registered;
  - `test_bad_bin_dir`: fake `uv tool dir --bin` prints a relative path or one with a space,
    so install fails and no remote write happens;
  - `test_missing_worker_key`: zero ssh calls; message names the `ssh-keygen` command;
  - `test_no_install_key`: `--no-check` path and printed hint;
  - `test_refuse_root`: `root@x` and fake `id -u` = 0 are refused with no remote write;
  - `test_registry_conflict`: pre-registered NAME with different slots -> failed message,
    zero remote calls;
  - `test_admin_password_needed`: fake `sudo -n true` fails, so there is a clear failure and
    no other call;
  - `test_admin_existing_user`: `id -u` succeeds, so the step is skipped;
  - `test_admin_fresh_user_end_to_end`: `id -u` fails, `useradd -m DESTUSER` is called with
    no `-G`, then install/verify/key/register all run through `ADMIN sudo -n -i -u DESTUSER`
    (asserted from recorded argv) and the run ends `done`;
  - `test_dry_run`: recording fakes see zero calls; output lists every command with the
    `<uv tool bin dir>` placeholder;
  - `test_controlpath_none`: every recorded bootstrap ssh/scp argv contains
    `ControlPath=none`.
- `tests/test_workers_registry.py` additions:
  - `test_identity_in_ssh_argv`: `normalize` appends `-i <id> -o IdentitiesOnly=yes`;
  - `test_no_identity_unchanged`: argv equals `default_ssh_argv`;
  - `test_add_identity_validation`: relative path or leading `-` rejected.
- **Manual verification:** first, `ssh-keygen -t ed25519 -N '' -f
  ~/.ssh/nw_worker_ed25519`. Then run `nw workers bootstrap box --ssh nw@box --install-key
  --dry-run`. Then run it for real against a machine with an existing `nw` user. Then run
  `nw workers check box`. Finally, `ssh -i ~/.ssh/nw_worker_ed25519 -o IdentitiesOnly=yes
  nw@box id` must be rejected by the forced command, while the human's own `ssh nw@box id`
  still works.
- **Regression guard:** affected tests while iterating. SPEC-0337's existing registry tests
  run unchanged (a record without `identity` behaves as before).

### Test record (2026-11-01)

- `uv run pytest -q -n 0 tests/test_workers_bootstrap.py`: 19 passed. All 16 named cases are
  there (`test_key_already_authorized_unrestricted`, `test_bad_bin_dir` and
  `test_missing_worker_key` are parametrized: plain line and other-path forced line; relative
  and spaced bin dir; missing `.pub` and missing private key).
- `uv run pytest -q -n 0 tests/test_workers_registry.py tests/test_rich_output_guard.py`: 56
  passed, including `test_identity_in_ssh_argv`, `test_no_identity_unchanged`,
  `test_add_identity_validation` (relative, `-o...`, `~/...`, spaced; on `add` and on a
  hand-edited record) and `test_add_identity_is_stored_and_listed`. SPEC-0337's existing
  registry tests pass unchanged.
- `python3 tools/affected_tests.py --run uv run pytest -q -n 4`: 4877 passed, 3 skipped
  (the selection fell back to almost the whole suite).
- `python3 tools/spec_lint.py --write-ledger` then `python3 tools/spec_lint.py`: OK.
- Manual, partly done. A throwaway key pair was made with `ssh-keygen -t ed25519 -N '' -f
  $T/nw_worker_ed25519`, with a temp config setting `agentic.workers.key`. Then the real CLI
  ran `nw workers bootstrap box --ssh nw@box --install-key --dry-run`. It printed every
  step's exact command with the `<uv tool bin dir>` placeholder, all five steps showed
  `would run` / `not requested`, the exit was 0, and no data dir was created. `--ssh
  root@box --install-key --dry-run` was refused before any call. **Not performed:** the
  real run against a machine with an existing `nw` user, `nw workers check box`, and the
  check that `ssh -i ~/.ssh/nw_worker_ed25519 -o IdentitiesOnly=yes nw@box id` is refused
  while the human's own `ssh nw@box id` works. This environment has no worker host or `nw`
  user. Creating one needs sudo or pulling a container image, and a loopback sshd as the
  human's own user would write the human's real `~/.ssh/authorized_keys`. A human has to run
  this step before the spec is `done`.
- Full suite left to the gate's close run.

### Test record (2026-11-01, review round 1: `--admin` under a real `sudo -i`)

- The fake `sudo` now models `-i` the way sudo's `parse_args.c` does. It backslash-escapes
  every character except alphanumerics, `_`, `-` and `$`, joins the args, and runs the
  result with `sh -c`. The fake `ssh` logs its stdin (the worker script) and passes it on.
  Against the old `sh -c '<script>'` transport, this fake fails `test_admin_fresh_user_end_to_end`
  (`touch: cannot touch ''`) and `test_admin_existing_user`. That run was done by
  temporarily restoring the old transport, then reverted.
- `uv run pytest -q -n 0 tests/test_workers_bootstrap.py tests/test_workers_registry.py
  tests/test_rich_output_guard.py`: 81 passed. New and extended cases:
  - `test_key_already_authorized_unrestricted` gains an `--admin` axis (existing user; the
    refusal is given and `authorized_keys` is byte-for-byte unchanged).
  - `test_registry_conflict_ssh_identity` covers a different `ssh`, a different `identity`,
    and a record with no `identity`. Each fails with the conflict message and makes zero
    remote calls.
  - `test_check_fails_after_install`: the dedicated-key check fails, so register is
    `failed`, the exit is 1, and the registry stays empty.
  - `test_admin_fresh_user_end_to_end` uses a key comment holding `'`, `$HOME`, a backtick
    and `\`, and still ends all `done` with the exact line in `authorized_keys`.
- `python3 tools/spec_lint.py`: OK. Full suite left to the gate. The manual step on a real
  worker host is still not performed (same reason as above).

### Test record (2026-11-01, review round 2: real sshd)

- **Manual verification, performed** against a real OpenSSH 10.0 `sshd` on loopback, as the
  human's own account standing in for `nw` (no separate machine or `nw` user exists here, and
  sudo needs a password, so `--admin` was not run for real). The setup avoided the human's
  real `~/.ssh`: a temp host key; `AuthorizedKeysFile` pointing to a temp HOME, seeded with a
  throwaway "login" key line (unrestricted); `SetEnv HOME=<temp>` so the worker side's `~` and
  `uv tool dir --bin` (`<temp>/.local/bin`) are in the temp HOME; and `ssh`/`scp` wrappers on
  PATH adding `-F <temp ssh_config>`. That config held `Host box` (port, temp known_hosts) and
  `IdentityFile <login key>`, which is the ssh_config fallback case from the review. A fresh
  `ssh-keygen -t ed25519 -N ''` worker key was set through `agentic.workers.key`. Results:
  - `nw workers bootstrap box --ssh bruno@box --install-key --dry-run`: every command was
    printed, and the exit was 0.
  - Real run: install `done` (a wheel of this checkout, real `uv build`/`scp`/`uv tool
    install`), verify `done`, key `done`, register `done` ("checked and registered"), exit 0.
    `authorized_keys` held the login line plus exactly
    `command="<temp>/.local/bin/nw worker ssh-command",restrict ssh-ed25519 ... nw-worker`,
    with modes 700/600. `nw workers list --json` shows `identity`.
  - `nw workers check box`: `ok`.
  - `ssh -i <worker key> -o IdentitiesOnly=yes bruno@box id`: `refused: only nw worker
    <...> is allowed`, exit 2. The login key's `ssh bruno@box id` still worked.
  - Re-run: install `skipped (already 0.1.0)`, verify `done`, key `skipped (already
    present)`, register `skipped (already registered)`, exit 0, and `authorized_keys` still
    had 2 lines.
  - With the forced line removed, the dedicated key is not authorized, so ssh falls back to
    the config's login key. `nw workers check box` now **fails** (`ran an unrestricted
    command ... not bound to the forced command`). Before this round's probe it would have
    passed. This run also found that the unrestricted master opened by that check survived,
    so the next check still failed after the line was restored. The check now closes such a
    master with `-O exit`, and with the line restored the check was `ok` again.
  - A no-identity record `plain` for the same `bruno@box` opened its own master
    (`<%C>`), next to `box`'s (`<%C>-<tag>`), and `nw workers check box` stayed `ok`, so it
    did not ride the unrestricted master.
  - The first real attempt failed at `scp` (exit 255) because the throwaway `sshd_config`
    had no `Subsystem sftp`, which OpenSSH 9+ `scp` needs. That was a test-harness gap, not
    bootstrap. After adding it, the run above went through. sshd was then stopped and the
    temp dir removed.
- New automated cases (`tests/test_workers_bootstrap.py`): `test_bad_worker_key` (a
  two-line `.pub`, a malformed line, a key path without `.pub`: exit 1, the message, zero
  calls); `test_default_key_path`; `test_uv_missing` (`uv not found ...`, later steps `not
  run`); `test_unreachable_is_not_uv_missing` (ssh 255 gives the ssh error);
  `test_admin_runas_refused_is_not_uv_missing`; `test_admin_useradd_fails`;
  `test_verify_info_fails_after_install` (`nw worker info` exits nonzero, or prints
  non-JSON); `test_key_line_form_refused_in_step` (a bad line built in step 3 is refused
  before any `authorized_keys` read or write); `test_cleanup_failure_is_a_warning` (after a
  good install, and after an `scp` failure whose cause is kept);
  `test_check_needs_the_forced_command`. The fake `ssh` now acts as sshd for the dedicated
  key: the first line holding the blob decides, and a `command="..."` line runs the forced
  command with `SSH_ORIGINAL_COMMAND`. The fake `nw worker ssh-command` allows only `info
  --json`. `tests/test_workers_registry.py`: `test_identity_controlpath_is_its_own`.
- `uv run pytest -q -n 4 tests/test_workers_bootstrap.py tests/test_workers_registry.py
  tests/test_rich_output_guard.py tests/test_worker*.py`: 334 passed.
  `python3 tools/spec_lint.py`: OK. The full suite is left to the gate's close run.

### Test record (2026-11-01, review round 3)

- New and extended cases. `tests/test_workers_registry.py`:
  `test_identity_appended_to_hand_set_ssh_argv` (a hand-set `ssh_argv` plus `identity` gets
  `-i <identity> -o IdentitiesOnly=yes` appended). `tests/test_workers_bootstrap.py`:
  - `test_refuse_root`: `root@x` is refused (exit 1, `must not be root`, zero calls, nothing
    registered) with `--install-key`, without it, and with `--dry-run`.
  - `test_uid_probe_unreachable_is_install`: an unreachable host under `--install-key` fails
    `install` with the ssh error, and `key` is `not run`.
  - `test_install_key_after_no_check_runs_the_check`: registered with `--no-check`, then an
    `--install-key` run appends the key, and register is `skipped (already registered)` only
    after the dedicated-key check (forced-command probe included) passed. A failing check
    there fails register. A rerun without `--install-key` prints the check hint.
  - `test_failed_check_closes_the_master`: the worker has no git and the dedicated key sits
    on an unrestricted line. The check reports both `git is missing` and `not bound to the
    forced command`, and closes the master with one `-O exit`. A passing check sends no
    `-O exit`.
- `uv run pytest -q -n 4 tests/test_workers_bootstrap.py tests/test_workers_registry.py
  tests/test_rich_output_guard.py tests/test_worker*.py`: 338 passed.
- `python3 tools/spec_lint.py`: OK. The full suite is left to the gate's close run.

### As shipped (2026-11-01)

- Code: `src/nightwright/agentic/workers_bootstrap.py` (the steps), `nw workers bootstrap` in
  `cli_workers.py`, and the `identity` field in `workers.py` (`IDENTITY_RE`, `identity_ok`,
  `_check_record`, `normalize`). `nw workers add --identity` was added; `list --json` gains
  `identity`. Docs are in `docs/WORKFLOW.md` (Remote gate worker -> Bootstrap) and
  `docs/product/capabilities/agentic.md`.
- With `--install-key`, the uid-0 probe (`id -u` on the worker) runs **before** install,
  not inside step 3. It is a read, and install already writes to the worker, so this keeps
  "refuses a root account without touching the worker" true. A refusal shows on the `key`
  row, with install, verify and register `not run`. A root account by name is refused in the
  local pre-checks, before any remote call. The other step-3 checks (line form,
  already-authorized blob) run in step 3, before its write.
- `register` writes the record itself, with the same fields and validation as `nw workers
  add` and `WK.check` when `--install-key`. It does not shell out to `nw workers add`.
  `--dry-run` prints the equivalent `nw workers add ... --identity ... [--no-check]` line.
- Every worker-side command is a one-line POSIX script fed on **stdin** to `sh -s`:
  `ssh DEST sh -s` without `--admin`, and `ssh ADMIN sudo -n -i -u DESTUSER sh -s` with it.
  The script is never on a command line, because `sudo -i` re-joins its command into one
  string for the target's login shell. It backslash-escapes every character except
  alphanumerics, `_`, `-` and `$`, so that shell would expand the script's `$f`/`$(...)` (and
  any `$` in the key comment) before the inner `sh` saw it. Scripts are single lines, so
  `sh -s` has read the whole script before running anything, and no command can consume
  it. `--dry-run` shows each one as `printf '%s\n' '<script>' | ssh ... sh -s`. Output is
  decoded with `errors="replace"`. The admin-side commands (`sudo -n true`, `id -u`, `sudo -n useradd -m`, the
  wheel's `mktemp -d`/`chmod`/`rm -rf`) run as ADMIN. The key script appends a newline first
  when `authorized_keys` does not end in one, and re-checks `grep -qxF` before appending.
- `--ssh` must be `USER@HOST`, with the user matching `[a-z_][a-z0-9_.-]{0,31}`.
  `agentic.workers.key` must name a `.pub` file with exactly one `<type> <base64> [comment]`
  line. The private-key path must match the registry's `IDENTITY_RE` (`/[A-Za-z0-9._/+-]+`).
- `normalize` appends `-i <identity> -o IdentitiesOnly=yes` to a hand-set `ssh_argv` as well
  as to the default one.
- The CLI prints each failed step's message in full after the table (`error: <step>:
  <message>`), because the table can truncate it.

### As shipped (2026-11-01, review round 2)

- The uv probe is `command -v uv >/dev/null || exit 42`. Only exit 42 means "uv not found".
  Any other nonzero exit (ssh 255 for an unknown host key or a failed auth, or sudo refusing
  `-u DESTUSER`) fails install with `could not run a command as DESTUSER on the worker
  [through ADMIN] (exit N): <stderr>`.
- `--admin` makes the wheel dir with `mktemp -d /tmp/nw-wheel.XXXXXXXX` as ADMIN. Plain
  `mktemp -d` would use ADMIN's TMPDIR, which pam_tmpdir makes 0700. Without `--admin` it is
  still `mktemp -d` as DESTUSER.
- Removing the remote wheel dir is best-effort. A failure there is a `warning: could not
  remove HOST:DIR (...)` note. It neither fails a finished install nor replaces the cause of
  an earlier failure.
- An `identity` record's default ControlPath is `<ssh_dir>/%C-<sha1(identity)[:8]>`, and
  `ssh_dir` picks a candidate short enough for the 9 extra bytes. `%C` hashes only user, host
  and port, so this is what keeps the record off a master opened with another key for the
  same `user@host`. A record without `identity` normalizes exactly as before. The AC's
  `-i <identity> -o IdentitiesOnly=yes` is still appended.
- `nw workers check` (`WK.check`) on an `identity` record also sends `echo
  nw-forced-command-probe` after `info` succeeds. The SPEC-0336 forced command must refuse
  it (nonzero exit, `refused:` on stderr). If it runs (exit 0), the check fails with
  `... is not bound to the forced command`, and the master that session opened is closed
  (`-O exit`) so later connections cannot ride it. The reason: `IdentitiesOnly` still lets
  ssh fall back to `IdentityFile` entries for the host in the human's ssh_config, so a working
  `info` does not prove that the dedicated key and its forced line authenticated. Bootstrap's
  register check gets this too.
- Version policy: as decided, "already installed" means `__version__` equality, and a wheel
  carries no git sha. A worker on an older commit with the same `version` is therefore not
  reinstalled. `docs/WORKFLOW.md` says so: bump the version for a change the worker must run,
  or run `uv tool install --force` on the worker.
### As shipped (2026-11-01, review round 3)

- A `root` user part in `--ssh` is refused in the local pre-checks whether or not
  `--install-key` is given, as the Design's "never root" says. Without `--install-key` the
  refusal shows on the `install` row.
- The pre-install uid probe (with `--install-key`) reports a failure to reach the worker
  (ssh, `sudo -u`) on the `install` row, with the same `could not run a command as DESTUSER
  ...` message as the uv probe. Only the uid-0 refusal is on the `key` row.
- Register with `--install-key` on an identical existing record still runs the check
  (`WK.check`, forced-command probe included). The worker may have been registered with
  `--no-check` earlier, so this is where the proof gets collected. A failed check fails
  register. On pass the step stays `skipped (already registered)`. Without `--install-key`, a
  skipped register still prints the `run nw workers check NAME` hint.
- `WK.check` on an `identity` record runs the forced-command probe whenever `info` gave JSON,
  even after another error such as missing git or a protocol mismatch. Any failed check on
  an `identity` record then closes the record's ControlMaster (`ssh -O exit`, best-effort),
  so a master opened by a fallback key never outlives the check.

## Clock Log

Use `nw idea clock-in IDEA-559` / `nw idea clock-out IDEA-559`.

## Rollout / migration

- New command `nw workers bootstrap`.
- New config key `agentic.workers.key` (path to the worker public key, default
  `~/.ssh/nw_worker_ed25519.pub`). It is documented with the other `agentic.workers`
  settings and validated as an existing readable file (with its private key next to it)
  when bootstrap runs.
- Registry: new optional record field `identity` and `nw workers add --identity`. This is
  backward compatible: existing records have no `identity` and behave as before, and the
  schema id stays `nw.workers.v1`. An older nightwright reading a record that has
  `identity` ignores it and connects with the default key. Such a worker would then fail
  its check, which is visible and not unsafe.
- Workers registered by hand before this spec keep working. To move one to a dedicated key,
  run `nw workers rm NAME` then `nw workers bootstrap NAME ... --install-key`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided: equality of the released version (`__version__`) decides "already
installed", since wheel installs carry no `git_sha`. Sudo is non-interactive only (`sudo
-n`, probed with `sudo -n true`). The worker key is dedicated, and bootstrap never generates
it.
