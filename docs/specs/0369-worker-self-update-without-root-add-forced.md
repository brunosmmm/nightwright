---
id: SPEC-0369
title: "Worker self-update without root: forced-command `nw worker update` and `nw workers update NAME`"
status: proposed
owner: user
created: 2026-11-02
kind: feature
source_idea: IDEA-714
tags: [agentic, remote, workers, ops]
depends_on: [SPEC-0336, SPEC-0340, SPEC-0354]
---

## Context

Today the only way to put new nightwright code on a worker is `nw workers bootstrap`
([SPEC-0354](./0354-nw-workers-bootstrap.md)). Its install step runs `uv tool install --force
<wheel>` over the **human's** ssh session, and for a worker user the human cannot log in as
it goes through `--admin ADMIN` and `sudo -n -i -u DESTUSER` (`step_install` and `worker_argv`
in `src/nightwright/agentic/workers_bootstrap.py`). So every refresh needs a privileged account
and passwordless sudo, although the install itself only writes into the worker user's own home
(`uv tool dir`, `uv tool dir --bin`).

SPEC-0367 (campaign IDEA-675, on branch `nw/IDEA-675-20261101-f96a`, not yet on main) adds a
content-hash **build id** (`src/nightwright/buildid.py`, `build_id` in `nw worker info --json`),
makes bootstrap reinstall a stale worker, adds `--reinstall`, and makes `nw workers check` warn
`worker stale`. Its reinstall still goes through the sudo path above. This spec is meant to land
**after** SPEC-0367, reuses its build id, and replaces its sudo reinstall as the routine way to
refresh a worker. SPEC-0367's frontmatter is not referenced here (it is not on this base, so
the spec linter would reject the reference); the dependency is stated in this paragraph and in
Rollout.

The worker's coordinator-facing key is already bound to a forced command
(`command="<bin>/nw worker ssh-command",restrict <pubkey>`, SPEC-0336/0354) that execs only an
allow-list of `nw worker` verbs (`check_ssh_command` and `SSH_SUBCOMMANDS` in
`src/nightwright/agentic/worker.py`). That key already lets the coordinator make the worker run
repository gate commands as the worker user, so letting it also replace the worker's own
nightwright install as that same user adds no new privilege. What is missing is a verb that
accepts a wheel and installs it, and a coordinator command that drives it.

Two environment constraints from the human's fleet shape the design:

- The worker user's login shell may be **fish** (or anything non-POSIX). sshd runs the forced
  command and the commands of an unrestricted session through that shell. Bootstrap already
  works around it by feeding POSIX scripts to `sh -s` (`on_worker`). The new path must not
  depend on shell syntax at all.
- Replacing the install replaces the venv the running `nw` lives in, and a worker may be
  running gate jobs that use that same install.

## Goals / Non-goals

**Goals**
- A forced-command verb `nw worker update [--force]` that receives a wheel on stdin, checks it,
  and installs it with `uv tool install --reinstall` as the unprivileged worker user into its
  own home, with no sudo and no second ssh session.
- `nw workers update NAME [--force] [--dry-run]` on the coordinator: builds the wheel from the
  coordinator's own checkout, pushes it with the worker's registered dedicated key, and
  verifies the worker now reports the wheel's build id.
- Refuses while jobs are live on the worker unless `--force`; the refusal changes nothing.
- Shell-agnostic: works when the worker user's login shell is fish.
- Root/sudo (`--admin`) is needed only for first-time bootstrap (creating the user, installing
  the first nightwright, adding the key line).

**Non-goals**
- No change to first-time bootstrap: user creation, the first install and the key line still go
  through `nw workers bootstrap`, including its `--admin` path and its `--reinstall` flag, which
  remain the repair path for a worker whose install is too broken to run `nw` at all.
- No automatic or scheduled updates: nothing updates a worker unless a human or steward runs
  `nw workers update`. `nw workers check` only warns (SPEC-0367).
- No fleet-wide `--all`; one named worker per call.
- No rollback to the previous install. A failed update is reported with the stage that failed
  (see Design, "Failure"); recovery is `nw workers bootstrap --reinstall`.
- No change to the `nw.job.v1` / `nw.job.frame.v1` protocols or to what a healthy worker does
  with jobs.
- Not an authentication mechanism for code: the sha256 in the header protects against a
  truncated or corrupted upload, not against a malicious coordinator (which the worker key
  already trusts; see Design, "Trust").

## Decision

Add the verb `nw worker update [--force]` to the forced-command allow-list. Its stdin is one
JSON header line followed by the wheel bytes. The worker validates and stages the wheel,
refuses if any job is live (unless `--force`), then runs `uv tool install --reinstall <wheel>`
as itself and verifies the result in a fresh process. The coordinator command `nw workers
update NAME` builds the wheel, streams it through the existing registry `ssh_argv` (so over the
dedicated worker key and the forced command), and then confirms with `nw workers check`'s
probe that the worker reports the wheel's build id. Nothing on this path uses `sudo`, `sh -s`
or any shell syntax: the remote command is plain words, and the worker only starts processes
from argv lists.

## Design

### Wire format (`nw.worker.update.v1`)

Coordinator -> worker, on stdin: one line of compact JSON, `\n`, then exactly `size` bytes.

```
{"schema": "nw.worker.update.v1", "wheel": "nightwright-0.1.0-py3-none-any.whl",
 "size": 1234567, "sha256": "<64 hex>", "build_id": "b1:<64 hex>"}
```

Worker -> coordinator, on stdout: exactly one JSON line, the result:

```
{"schema": "nw.worker.update.result.v1", "state": "updated|unchanged|busy|failed",
 "stage": null|"header"|"receive"|"validate"|"lock"|"install"|"verify",
 "error": null|"...", "live_jobs": ["<job id>", ...],
 "build_id_before": "b1:..."|null, "build_id_after": "b1:..."|null, "version": "0.1.0"|null,
 "forced": false}
```

Exit status: 0 `updated` or `unchanged`; 3 `failed` (the existing `EXIT_ERROR`); 5 `busy`
(live jobs without `--force`, or another update holds the lock). Anything printed by `uv` goes to
stderr, never stdout, so stdout stays one parseable line.

### Worker side (new module `src/nightwright/agentic/worker_update.py`)

`check_ssh_command` gains `update`: `args == []` or `args == ["--force"]`, nothing else.
`SSH_SUBCOMMANDS` gains `"update"`, and `worker.info` adds `"update"` to `features`. `cli_worker.py`
gets the `nw worker update [--force]` command and its docstring entry; the `ssh-command`
docstring lists the verb.

`update(cfg, force, stdin, stdout) -> int`, in order:

1. **header.** Read one line, at most 64 KiB. Require `schema`, a `wheel` name matching the
   PEP 427 shape `^nightwright-[0-9A-Za-z.!+_]+-py3-none-any\.whl$` (no path separators), an
   integer `size` in `1..MAX_WHEEL_BYTES` (64 MiB), a 64-hex `sha256`, and a `build_id`
   matching `^b1:[0-9a-f]{64}$`. Otherwise `failed`, stage `header`, nothing written.
2. **receive.** Stream exactly `size` bytes into `worker/update/<wheel>.part` (mode 0600, the
   directory created 0700 and emptied of stale `.part` files first), hashing as it goes. EOF
   before `size` bytes, or a sha256 mismatch, is `failed`, stage `receive`; the part file is
   removed. Bytes after `size` are ignored, not read.
3. **validate.** The staged file must be a zip that passes `zipfile.ZipFile.testzip()`, contain
   `nightwright/__init__.py`, and have `buildid.compute_wheel(path) == header.build_id`.
   Otherwise `failed`, stage `validate`. This runs **before** anything touches the installed
   copy, so a bad upload can never break a working install.
4. **lock and live check.** Take `worker/update.lock` (flock, non-blocking; held means `busy`
   with `error: "another update is running"`, stage `lock`), then `jobs_lock(cfg)`, and keep
   both until the install and verify finish. Under them, `_live_jobs(cfg)` gives the job ids
   whose `job.lock` another process holds: that covers synchronous `gate-job` runs and detached
   `submit` jobs in states `accepted` and `running`. A `lost` job has no lock holder and is not
   live. If the set is non-empty and not `force`, answer `busy` with `live_jobs` and exit 5; no
   file other than the part file was touched. Holding `jobs_lock` means a job that arrives
   during the update waits on its claim until the update ends instead of starting against a
   half-replaced install.
5. **unchanged.** If `buildid.local()` (the installed code) equals `header.build_id`, answer
   `unchanged` (exit 0) without installing, regardless of `--force`.
6. **install.** Run `[uv, "tool", "install", "--reinstall", str(wheel)]` with `subprocess.run`
   (argv list, no shell), stdout redirected to stderr, a 900 s timeout, and `VIRTUAL_ENV` and
   `PYTHONPATH` removed from its environment. `uv` is resolved from `PATH`, then next to the
   running `nw` (the forced command's environment has a minimal `PATH`), then
   `~/.local/bin/uv`, `/usr/local/bin/uv`, `/usr/bin/uv`; none found is `failed`, stage
   `install`, `uv not found for this user`. `HOME`, `UV_TOOL_DIR` and `UV_TOOL_BIN_DIR` are
   inherited, so the install lands wherever the worker user's own `uv tool dir` points, which is
   where the forced command's `<bin>/nw` already is. `SIGHUP`, `SIGINT`, `SIGTERM` and `SIGPIPE`
   are ignored from step 4 to the end, so a dropped ssh connection cannot interrupt `uv`
   half-way; the result line is written best-effort, and the result is also saved to
   `worker/update.json` (atomic write) for a later look.
7. **verify, in a fresh process.** The running process is the old install, whose venv was just
   replaced, so after step 6 it only runs `[own_nw, "worker", "info", "--json"]` (a new
   interpreter on the new code) and parses its output; every module it needs is imported at
   module top level. `build_id` must equal `header.build_id`. Otherwise `failed`, stage
   `verify`, `build_id_after` set to what the worker reported.
8. **cleanup.** The staged wheel and part file are removed in a `finally`, on every path.

`--force` only overrides the live-job refusal. It never kills a job: a job that was running
when the install was replaced may fail, and the coordinator's lost-job retry (SPEC-0342)
handles it as it handles any worker-side loss.

### Coordinator side (new module `src/nightwright/agentic/workers_update.py`, command in `cli_workers.py`)

`nw workers update NAME [--force] [--dry-run]`:

1. Look up NAME in the registry; unknown name is exit 2 as for other `nw workers` commands.
   The record must have an `identity` (the dedicated key, SPEC-0354); otherwise stop with
   `worker NAME has no dedicated identity; run nw workers bootstrap NAME ... first`, so the
   update never rides an unrestricted login key.
2. Build the wheel with the shared helper `workers_bootstrap.build_wheel(out_dir)` (the
   `uv build --wheel` code of `step_install`, extracted; bootstrap behaviour unchanged), compute
   `buildid.compute_wheel`, sha256 and size.
3. Push: `record["ssh_argv"] + [record["ssh"], record["nw"], "worker", "update"
   (+ "--force")]` with the header line and the wheel bytes on stdin, through `subprocess.Popen`
   with a 900 s timeout. These are the same ControlMaster-multiplexed argv and key the gate path
   uses. The remote command words match `^[A-Za-z0-9._/-]+( [A-Za-z0-9._/-]+)*$`: no quoting,
   `~`, `$`, `;`, `&&` or redirection, so fish, csh and sh parse them identically. The coordinator
   never sends anything through `sh -s`.
4. Interpret the result line:
   - `updated` or `unchanged`: continue to verification.
   - `busy`: print `worker NAME has live jobs (<ids>); wait, nw workers drain NAME, or rerun
     with --force` (or `another update is running`), exit 3; nothing changed.
   - `failed`: print `stage` and `error`, and, for stages `install` and `verify`, `the
     install may be broken; recover with nw workers bootstrap NAME --ssh <ssh> --reinstall`.
     Exit 1.
   - ssh exit 2 whose stderr contains `refused:` and `not allowed`, or an unreadable result
     line: if the stderr names `update` as not allowed, print `the worker's nightwright
     predates nw worker update; bootstrap it once with nw workers bootstrap NAME --ssh <ssh>
     --reinstall`; exit 1. For any other ssh failure after the upload began, say the install
     may have completed and still run the verification below.
5. **Verify build id.** Run `workers.check` (SPEC-0337/0341) on the record. It must be `ok`
   and its `info.build_id` must equal the pushed wheel's id; `nw workers update` then prints
   `NAME: updated <old12> -> <new12>` (or `already <new12>`). A mismatch, or a check failure,
   exits 1 with `worker reports build <got>, expected <want>`. This second, independent probe
   is what makes a worker whose result line was lost (dropped connection) still verifiable.

`--dry-run` builds nothing and connects to nothing: it prints the registry record's ssh
destination, the exact remote command line, the header shape and whether `--force` was given,
and exits 0 (nonzero only for step 1's local problems).

### Hint text

SPEC-0367's `worker stale` warning from `nw workers check` and its rejected-job message name
`nw workers bootstrap NAME --ssh <ssh> --reinstall`. On landing, those two hints name `nw
workers update NAME` first, with the bootstrap command as the fallback for a worker that
predates the verb.

### Trust

The worker key already lets its holder run arbitrary repository gate commands as the worker
user, so the update verb grants nothing beyond that. The forced command still bounds it: the
grammar accepts only `update` and `update --force`, the wheel name is validated, the staged
path is inside the worker's own data dir, and the installer is always `uv tool install
--reinstall` on that file. The sha256 and build-id checks detect corruption, not a hostile
coordinator.

## Alternatives considered

- **Keep the sudo reinstall (SPEC-0367) as the routine refresh.** It needs an admin account
  and NOPASSWD sudo for something that only touches the worker user's home; it is kept only for
  first-time bootstrap and repair.
- **The coordinator runs `uv tool install` over an unrestricted ssh session as the worker
  user.** That needs a second, unrestricted key for the worker account, which is exactly what
  the dedicated forced-command key (SPEC-0354) exists to avoid.
- **The worker pulls the wheel from a package index or a URL.** The coordinator and worker
  could then differ in code, and the worker needs outbound access to the coordinator. Pushing
  the coordinator's own wheel over the existing channel guarantees both run the same build.
- **`scp` the wheel plus a second verb to install it.** Two round trips and a staged file the
  worker has to trust the name of; the single stdin stream keeps the transfer, checks and
  install under one lock.
- **Run a POSIX script on the worker (`sh -s`) as bootstrap does.** Works with fish only
  because the shell is told to run `sh`; it still needs a login that can run arbitrary
  commands, which the forced key cannot. Plain argv words and a Python verb avoid the shell.
- **Kill live jobs on `--force`.** Surprising and lossy; the coordinator already retries jobs
  lost on a worker (SPEC-0342), so `--force` just stops protecting them.

## Acceptance criteria

- [ ] `nw worker ssh-command` accepts `nw worker update` and `nw worker update --force` from
      `SSH_ORIGINAL_COMMAND` and refuses (exit 2, nothing run) `update` with any other
      argument, `update --force --force`, `update ;id`, and any unknown verb. `nw worker info
      --json` lists `update` in `features`.
- [ ] A worker given a valid header and wheel with a different build id installs it with `uv
      tool install --reinstall <wheel>` as the invoking user (no `sudo` anywhere on the path),
      then reports `state: updated`, `build_id_after` equal to the header's `build_id`, exit 0;
      the staged wheel and part file are gone afterwards.
- [ ] When the installed build id already equals the header's, the result is `unchanged`, exit
      0, and `uv` is not run (with or without `--force`).
- [ ] A header with a bad schema, an unsafe or non-wheel `wheel` name, a `size` of 0 or above
      64 MiB, a malformed `sha256` or a malformed `build_id` fails with stage `header` and
      writes nothing under `worker/update/`.
- [ ] A truncated upload, a sha256 mismatch, a wheel whose zip is corrupt, one without
      `nightwright/__init__.py`, or one whose computed build id differs from the header's
      fails (stage `receive` or `validate`), exits 3, and leaves the installed code untouched
      (`uv` not run).
- [ ] With a live job (a held `job.lock`, either a synchronous gate job or a detached submitted
      job in `accepted` or `running`) and no `--force`, the result is `busy` with the job ids in
      `live_jobs`, exit 5, and the installed code is untouched. A `lost` job is not live.
      With `--force` the install proceeds and no job process is signalled.
- [ ] Two concurrent `update` calls: the second gets `busy` (`another update is running`)
      without touching anything. A job claim started during an update waits for `jobs_lock` and
      proceeds after it ends.
- [ ] A `uv` failure or timeout gives `failed`, stage `install`, with `uv`'s last stderr line in
      `error`; an install after which a fresh `nw worker info --json` reports another build id
      (or does not run) gives `failed`, stage `verify`. The verify step runs in a new process,
      not in the replaced one. `worker/update.json` holds the last result.
- [ ] A dropped connection (stdout closed, SIGHUP) after the install began does not interrupt
      `uv`; the install finishes and `nw workers update` still verifies it through its
      follow-up `workers.check`.
- [ ] `nw workers update NAME` on a registered worker with an `identity` builds the wheel,
      sends it through the record's `ssh_argv` with remote command `<nw> worker update`, and on
      success prints `NAME: updated <old12> -> <new12>` after `workers.check` is `ok` and its
      `info.build_id` equals the wheel's id; exit 0. A mismatch exits 1 with `worker reports
      build <got>, expected <want>`.
- [ ] `nw workers update NAME` on a worker with live jobs and no `--force` prints the live job
      ids, exits 3 and changes nothing; with `--force` it updates. On a worker whose
      nightwright predates the verb it exits 1 with the one-time bootstrap hint. On a record
      with no `identity` it exits nonzero before building anything.
- [ ] `nw workers update NAME --dry-run` prints the remote command and header shape, builds and
      connects to nothing, and exits 0.
- [ ] The remote command words the coordinator sends match `^[A-Za-z0-9._/-]+( [A-Za-z0-9._/-]+)*$`,
      and a real forced command run under a `fish` login shell (when `fish` is installed)
      accepts the update stream and installs.
- [ ] `nw workers update` never invokes `sudo` or `--admin`; `nw workers bootstrap` behaves as
      before (existing bootstrap tests pass) and its install step uses the shared
      `build_wheel` helper.
- [ ] SPEC-0367's stale-worker hints name `nw workers update NAME` first and the bootstrap
      `--reinstall` command as the fallback, and `docs/WORKFLOW.md` documents `nw workers
      update`, the live-job refusal and `--force`, and that root is needed only for the first
      bootstrap.

## Test plan

- **Automated tests**, all with temp `HOME`/data dirs and no network unless noted:
  - `tests/test_worker_ssh_command.py` (extend): the grammar cases of the first criterion,
    including injection attempts and `update` in `info` features (`tests/test_worker.py`).
  - `tests/test_worker_update.py` (new), calling `worker_update.update` with in-memory
    streams and a fake `uv` executable earlier on `PATH` that records its argv and its uid and
    installs a stub into a temp `UV_TOOL_DIR`: valid install (`updated`, argv is exactly
    `tool install --reinstall <wheel>`, wheel and part file removed, `update.json` written);
    `unchanged`; every header-rejection case; truncated body, bad sha, corrupt zip, missing
    `__init__.py`, build-id mismatch, each leaving the fake `uv` uncalled; live job via a held
    `job.lock` (a real flock held by a helper process) and a detached-job fixture from
    `tests/test_worker_jobs.py`, with and without `--force`, plus a `lost` job treated as not
    live; the update lock contention case; `jobs_lock` blocking a concurrent claim; fake `uv`
    failing, timing out (patched timeout) and installing the wrong build (verify failure);
    SIGHUP/closed stdout during install not interrupting the fake `uv`; verify runs through a
    separate process (the fake `nw` records its pid, which differs from the test's).
  - `tests/test_workers_update.py` (new): the coordinator command with a fake `ssh` earlier on
    `PATH` that stands in for the worker by running `nw worker update` in-process: success with
    build-id check, `busy` -> exit 3, `failed` messages and the recovery hint, old-worker
    (`not allowed`) hint, missing `identity`, lost result line recovered by the follow-up check,
    build-id mismatch exit 1, `--dry-run` making no subprocess call, the remote-command-words
    regex, no `sudo`/`--admin` in any recorded argv.
  - `tests/test_workers_bootstrap.py` (existing, must still pass) with one added test that
    `step_install` calls the shared `build_wheel`.
  - `tests/test_worker_update_real_uv.py` (new, skipped unless `uv` is installed): builds a
    real wheel of this checkout with `uv build --wheel`, runs `worker_update.update` with
    `HOME`, `UV_TOOL_DIR` and `UV_TOOL_BIN_DIR` in a temp dir and a real `uv tool install
    --reinstall`, then runs the installed `nw worker info --json` and asserts its `build_id`
    equals the wheel's. Needs the package index (or a warm uv cache); skipped, not failed, when
    `uv` is missing.
  - `tests/test_worker_update_fish.py` (new, skipped unless `fish` is installed): runs the
    real forced command (`nw worker ssh-command` with `SSH_ORIGINAL_COMMAND="nw worker update"`)
    from `fish -c` with a stub wheel stream and the fake `uv`, asserting the result line.
- **Manual verification** (against the real box, per the real-binary rule, since fakes do not
  prove the integration): on a worker bootstrapped with the dedicated key and a login shell of
  fish, from the coordinator with a changed checkout:
  1. `nw workers update BOX --dry-run` shows the command; exit 0.
  2. With a gate job running on BOX, `nw workers update BOX` exits 3 naming the job; nothing
     changed (`nw workers check BOX` shows the old build id).
  3. After the job ends, `nw workers update BOX` prints `BOX: updated <old12> -> <new12>`;
     `nw workers check BOX` shows no `worker stale` warning; no `sudo` appears in the worker's
     auth log for the update.
  4. Run it again: `already <new12>`. Run one remote gate on BOX: it completes remotely.
  5. `nw workers update BOX --force` during a running job: the job is not signalled; note
     whether it completes or is retried.
  Record the outcome in the spec's shipped notes.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus the
  files above, `tests/test_workers_bootstrap.py`, `tests/test_worker_jobs.py`,
  `tests/test_worker_ssh_command.py` and `tests/test_workers_registry.py`; the full suite runs
  at the queue's close gate. `python3 tools/spec_lint.py` exits 0.

## Rollout / migration

1. Land SPEC-0367 first (build id, `--reinstall`, `worker stale` warning). This spec's
   verification compares build ids, so it cannot ship without them.
2. Land this spec. Workers still running a pre-`update` nightwright refuse the verb (exit 2,
   `not allowed`); each needs one `nw workers bootstrap NAME --ssh ... --reinstall` (with
   `--admin` if that is how it was set up). After that, every later refresh is `nw workers
   update NAME`.
3. Verify with the manual steps above on one real worker before using it on the rest.
4. No registry or schema change; `nw.worker.info.v1` gains only the `update` feature entry.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected automated tests pass and the manual steps against the real
      box were performed and noted in the spec.
- [ ] No regressions (full suite at the queue's close gate).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated with `python3 tools/spec_lint.py --write-ledger`, and
      `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
