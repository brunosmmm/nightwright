---
id: SPEC-0369
title: "Worker self-update without root: forced-command verb `nw worker update` (worker side)"
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
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

SPEC-0367 (campaign IDEA-675, on branch `nw/IDEA-675-20261101-f96a`, not yet on main) adds the
content-hash **build id**, makes bootstrap reinstall a stale worker, adds `--reinstall`, and makes
`nw workers check` warn `worker stale`. **This spec has a hard dependency on SPEC-0367 and must
not be admitted to the queue before SPEC-0367 is on main.** It is not listed in `depends_on`
only because `tools/spec_lint.py` rejects references to specs that are not on the base; when
SPEC-0367 is on main, the first edit of this spec adds `SPEC-0367` to `depends_on`. This spec
uses exactly these names from SPEC-0367's `src/nightwright/buildid.py` (as on that branch):

- `buildid.compute_wheel(wheel: Path) -> str`: `b1:<64 hex>` over the `nightwright/` entries of a
  built wheel (not `*.dist-info`);
- `buildid.local() -> str`: the same id over the installed `nightwright` package tree;
- `build_id` in the `nw worker info --json` output (SPEC-0367 test `test_worker_info_reports_a_build_id`).

The property this spec relies on is that `compute_wheel(wheel) == local()` after that wheel is
installed; SPEC-0367 proves it in `tests/test_buildid.py::test_compute_wheel_equals_compute_of_the_installed_tree`
and `test_a_wheel_built_with_uv_matches_the_working_tree`. If those names differ on main, the
implementer takes the merged names and updates this paragraph.

The worker's coordinator-facing key is already bound to a forced command
(`command="<bin>/nw worker ssh-command",restrict <pubkey>`, SPEC-0336/0354) that execs only an
allow-list of `nw worker` verbs (`check_ssh_command` and `SSH_SUBCOMMANDS` in
`src/nightwright/agentic/worker.py`). That key already lets the coordinator make the worker run
repository gate commands as the worker user, so letting it also replace the worker's own
nightwright install as that same user adds no new privilege. What is missing is a verb that
accepts a wheel and installs it.

**Scope of this spec.** The whole idea (IDEA-714) is two separable surfaces: the **worker verb**
(this spec) and the **coordinator command** `nw workers update NAME` with wheel-build extraction,
hint text and docs (a follow-up spec, see Non-goals). The worker verb is fully testable on its
own by feeding it the wire format below on stdin; nothing here needs the coordinator command.

Two environment constraints from the human's fleet shape the design:

- The worker user's login shell may be **fish** (or anything non-POSIX). sshd runs the forced
  command through that shell. The verb takes only plain words, so it must not depend on shell
  syntax, and the worker only starts processes from argv lists.
- Replacing the install replaces the venv the running `nw` lives in, and a worker may be running
  gate jobs, or receive new ones, that use that same install.

## Goals / Non-goals

**Goals**
- A forced-command verb `nw worker update [--force]` that receives a wheel on stdin, checks it,
  and installs it with `uv tool install --reinstall` as the unprivileged worker user into its
  own home, with no sudo and no second ssh session.
- Refuses while jobs are live unless `--force`; the refusal changes nothing.
- Never lets a job start against a half-replaced install: claims that arrive during or after an
  update from an older process are refused as retryable.
- A dropped ssh connection cannot abort the install half-way.
- Shell-agnostic: works when the worker user's login shell is fish.

**Non-goals**
- **The coordinator command is a follow-up spec** (to be written with `depends_on` this spec),
  covering: `nw workers update NAME [--force] [--dry-run]`; extracting `build_wheel` from
  `workers_bootstrap.step_install`; the identity requirement; pushing the wheel over the
  registry record's `ssh_argv` with shell-safe words; interpreting the result line (busy exit 3,
  failed exit 1 with the recovery hint, the old-worker `not allowed` hint); the follow-up
  `workers.check` build-id verification; treating the `worker updating` claim refusal like a busy
  worker; updating SPEC-0367's stale-worker hint text and `docs/WORKFLOW.md`; and the end-to-end
  manual run with the coordinator. This spec only fixes the wire format that follow-up consumes.
- No change to first-time bootstrap: user creation, the first install and the key line still go
  through `nw workers bootstrap`, including its `--admin` path and `--reinstall`, which remain
  the repair path for a worker whose install is too broken to run `nw` at all. Until the
  coordinator spec lands, SPEC-0367's sudo reinstall stays the routine refresh.
- No automatic or scheduled updates, no fleet-wide mode, no rollback to the previous install.
  A failed update is reported with the stage that failed; recovery is `nw workers bootstrap
  --reinstall`.
- No change to the `nw.job.v1` / `nw.job.frame.v1` protocols beyond one extra `retryable` field
  on the claim-stage error (see Design, step 4).
- Not an authentication mechanism for code: the sha256 in the header protects against a
  truncated or corrupted upload, not against a malicious coordinator (which the worker key
  already trusts; see Design, "Trust").

## Decision

Add the verb `nw worker update [--force]` to the forced-command allow-list. Its stdin is one
JSON header line followed by the wheel bytes. The worker validates and stages the wheel, refuses
if any job is live (unless `--force`), marks the worker as updating, then runs `uv tool install
--reinstall <wheel>` as itself in an isolated process whose output goes to a file, and verifies
the result in a fresh process. `jobs_lock` is held only for the live check and the marker write,
never across the install; job claims that see the marker, or come from a process older than the
install, are refused with a retryable `worker updating` error instead of running on mixed code.

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
(live jobs without `--force`, or another update holds the lock). Nothing from `uv` is ever
written to stdout (see step 6), so stdout stays one parseable line.

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
4. **lock, live check, marker.** Take `worker/update.lock` (flock, non-blocking; held means
   `busy` with `error: "another update is running"`, stage `lock`). Then, holding `jobs_lock(cfg)`
   **only for this step**: `_live_jobs(cfg)` gives the job ids whose `job.lock` another process
   holds (synchronous `gate-job` runs and detached `submit` jobs in `accepted` and `running`; a
   `lost` job has no lock holder and is not live). If the set is non-empty and not `force`, answer
   `busy` with `live_jobs` and exit 5; no file other than the part file was touched. Otherwise
   write the marker `worker/updating` (JSON `{pid, install_started_at, build_id_before}`, atomic
   write) and release `jobs_lock`. The marker is removed in the `finally` of the update.
   `jobs_lock` is **not** held while `uv` runs, so claims and `gc` are never stalled for the
   length of an install; they see the marker instead:
   - A claim (`gate-job` in `worker.py`, `submit` in `worker_job.py`) that has acquired
     `jobs_lock` refuses, before creating anything, with `stage: "claim"`, `error: "worker
     updating"` and `retryable: true` when the marker exists **and** `worker/update.lock` is
     held by another process (a marker left by a killed update is ignored and removed).
   - The same claim also refuses with the same error when `install_started_at` in
     `worker/update.json` (written at the start of every install attempt, step 6) is later than
     the time this process was started (`worker.PROCESS_STARTED`, set at import). That is a
     claimant that was already running as the old interpreter, was waiting for `jobs_lock` or
     the client's upload, and would otherwise run (and lazily import) against the replaced
     venv.
   `gc` and `reap_lost` are not refused: they only read and remove job dirs, and their lazy
   imports resolve against whatever is on disk, as they already do across a bootstrap.
5. **unchanged.** If `buildid.local()` (the installed code) equals `header.build_id`, answer
   `unchanged` (exit 0) without installing, regardless of `--force`.
6. **install.** First write `install_started_at` into `worker/update.json`. Then run
   `[uv, "tool", "install", "--reinstall", str(wheel)]` with `subprocess.Popen` (argv list, no
   shell), a 900 s timeout, and `VIRTUAL_ENV` and `PYTHONPATH` removed from its environment.
   `uv` is resolved from `PATH`, then next to the running `nw` (the forced command's environment
   has a minimal `PATH`), then `~/.local/bin/uv`, `/usr/local/bin/uv`, `/usr/bin/uv`; none found
   is `failed`, stage `install`, `uv not found for this user`. `HOME`, `UV_TOOL_DIR` and
   `UV_TOOL_BIN_DIR` are inherited, so the install lands wherever the worker user's own `uv tool
   dir` points, which is where the forced command's `<bin>/nw` already is.

   Isolation from the ssh session: `uv`'s stdout and stderr are captured to
   `worker/update/uv.log` (a file, never the ssh channel) and its stdin is `/dev/null`, so no fd
   connected to the ssh session is inherited. `uv` is started in its own session
   (`start_new_session=True`) with default signal dispositions restored (`restore_signals=True`
   plus a `preexec_fn` that sets `SIGHUP`, `SIGINT`, `SIGTERM` and `SIGPIPE` to `SIG_DFL`), because
   an ignored disposition survives `exec` and a write to a closed ssh fd would otherwise kill it
   half-way. The updater itself ignores `SIGHUP`, `SIGINT`, `SIGTERM` and `SIGPIPE` from the start
   of step 4 to the end, so a dropped connection cannot interrupt the wait either. After `uv`
   exits, the tail of `uv.log` is copied best-effort to the worker's stderr (errors ignored), and
   on a nonzero exit or timeout its last non-empty line becomes `error`. The result line is
   written best-effort to stdout and always saved to `worker/update.json` (atomic write, with
   `install_started_at`) for a later look.
7. **verify, in a fresh process.** The running process is the old install, whose venv was just
   replaced, so after step 6 it only runs `[own_nw, "worker", "info", "--json"]` (a new
   interpreter on the new code; `own_nw` is the absolute path of the `nw` the forced command
   started as, resolved before step 6) and parses its output; every module it needs is imported
   at module top level. `build_id` must equal `header.build_id`. Otherwise `failed`, stage
   `verify`, `build_id_after` set to what the worker reported.
8. **cleanup.** The staged wheel, the part file and the marker are removed in a `finally`, on
   every path; `uv.log` is kept until the next update empties the directory.

`--force` only overrides the live-job refusal. It never kills a job: a job that was running when
the install was replaced may fail, and the coordinator's lost-job retry (SPEC-0342) handles it as
it handles any worker-side loss.

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
- **Run `uv tool install` over an unrestricted ssh session as the worker user.** That needs a
  second, unrestricted key for the worker account, which is exactly what the dedicated
  forced-command key (SPEC-0354) exists to avoid.
- **The worker pulls the wheel from a package index or a URL.** The coordinator and worker could
  then differ in code, and the worker needs outbound access. Pushing a wheel over the existing
  channel guarantees the worker runs the build it was sent.
- **`scp` the wheel plus a second verb to install it.** Two round trips and a staged file the
  worker has to trust the name of; the single stdin stream keeps transfer, checks and install
  under one lock.
- **Run a POSIX script on the worker (`sh -s`) as bootstrap does.** It needs a login that can
  run arbitrary commands, which the forced key cannot. Plain argv words and a Python verb avoid
  the shell.
- **Hold `jobs_lock` through the install.** It blocks every claim, `gc` and `reap_lost` for up
  to 15 minutes, and a claimant released afterwards is still the old interpreter running against
  the new venv. The marker plus refuse-as-retryable avoids both.
- **Kill live jobs on `--force`.** Surprising and lossy; the coordinator already retries jobs
  lost on a worker (SPEC-0342), so `--force` just stops protecting them.
- **One spec for the verb and the coordinator command.** About 15 acceptance criteria over two
  independent surfaces; the verb is testable alone with the wire format fixed, so the
  coordinator command is a dependent follow-up.

## Acceptance criteria

- [x] `nw worker ssh-command` accepts `nw worker update` and `nw worker update --force` from
      `SSH_ORIGINAL_COMMAND` and refuses (exit 2, nothing run) `update` with any other
      argument, `update --force --force`, `update ;id`, and any unknown verb. `nw worker info
      --json` lists `update` in `features`.
- [x] A worker given a valid header and wheel with a different build id installs it with `uv
      tool install --reinstall <wheel>` as the invoking user (no `sudo` anywhere on the path),
      then reports `state: updated`, `build_id_after` equal to the header's `build_id`, exit 0;
      the staged wheel, part file and `worker/updating` marker are gone afterwards.
- [x] When the installed build id already equals the header's, the result is `unchanged`, exit
      0, and `uv` is not run (with or without `--force`).
- [x] A header with a bad schema, an unsafe or non-wheel `wheel` name, a `size` of 0 or above
      64 MiB, a malformed `sha256` or a malformed `build_id` fails with stage `header` and
      writes nothing under `worker/update/`.
- [x] A truncated upload, a sha256 mismatch, a wheel whose zip is corrupt, one without
      `nightwright/__init__.py`, or one whose computed build id differs from the header's
      fails (stage `receive` or `validate`), exits 3, and leaves the installed code untouched
      (`uv` not run).
- [x] With a live job (a held `job.lock`, either a synchronous gate job or a detached submitted
      job in `accepted` or `running`) and no `--force`, the result is `busy` with the job ids in
      `live_jobs`, exit 5, and the installed code is untouched. A `lost` job is not live.
      With `--force` the install proceeds and no job process is signalled.
- [x] Two concurrent `update` calls: the second gets `busy` (`another update is running`)
      without touching anything. `jobs_lock` is not held while `uv` runs (a `gc` or an
      unrelated lock take completes during a fake `uv` that is still running). A `gate-job` or
      `submit` claim started during an update, and one from a process started before an install
      that has since begun, is refused with `stage: claim`, `error: worker updating`,
      `retryable: true`, and never starts a job; a marker left by a killed update does not
      refuse claims.
- [x] A `uv` failure or timeout gives `failed`, stage `install`, with the last non-empty line of
      `uv.log` in `error`; an install after which a fresh `nw worker info --json` reports
      another build id (or does not run) gives `failed`, stage `verify`. The verify step runs in
      a new process, not in the replaced one. `worker/update.json` holds the last result.
- [x] A dropped connection after the install began (stdout closed, the ssh-side stderr closed
      mid-run, `SIGHUP` delivered) does not interrupt `uv`: a fake `uv` that keeps running and
      writing after the close still completes, the install finishes, and `worker/update.json`
      records `updated`. `uv` is started with default `SIGHUP`/`SIGPIPE` dispositions, in its
      own session, with no fd shared with the ssh session.
- [x] A real forced command run under a `fish` login shell (when `fish` is installed) accepts
      `nw worker update` with the update stream on stdin and installs.

## Test plan

- **Automated tests**, all with temp `HOME`/data dirs and no network unless noted:
  - `tests/test_worker_ssh_command.py` (extend): the grammar cases of the first criterion,
    including injection attempts; `update` in `info` features (`tests/test_worker.py`).
  - `tests/test_worker_update.py` (new), calling `worker_update.update` with in-memory
    streams and a fake `uv` executable earlier on `PATH` that records its argv, uid, signal
    dispositions and session id, and installs a stub into a temp `UV_TOOL_DIR`: valid install
    (`updated`, argv is exactly `tool install --reinstall <wheel>`, wheel, part file and marker
    removed, `update.json` written); `unchanged`; every header-rejection case; truncated body,
    bad sha, corrupt zip, missing `__init__.py`, build-id mismatch, each leaving the fake `uv`
    uncalled; live job via a held `job.lock` (a real flock held by a helper process) and a
    detached-job fixture from `tests/test_worker_jobs.py`, with and without `--force`, plus a
    `lost` job treated as not live; update-lock contention; **claim during update**: while the
    fake `uv` blocks on a pipe, a `gate-job` claim and a `submit` claim each get the retryable
    `worker updating` error (asserted on the error frame, not a delayed start), a `gc` call
    completes (so `jobs_lock` is free), and a stale marker with no lock holder does not refuse;
    a claimant whose `PROCESS_STARTED` predates `install_started_at` is refused; fake `uv`
    failing (its last stderr line lands in `error`), timing out (patched timeout) and installing
    the wrong build (verify failure); **drop test**: the ssh-side stderr and stdout are closed
    and `SIGHUP` is sent to the updater during the fake `uv` run, and the fake `uv` (which
    writes to its stderr after the close) still completes, with its recorded `SIGHUP`/`SIGPIPE`
    dispositions default and its sid different from the updater's; verify runs through a
    separate process (the fake `nw` records its pid, which differs from the test's).
  - `tests/test_worker_update_real_uv.py` (new, skipped unless `uv` is installed): builds a
    real wheel of this checkout with `uv build --wheel`, runs `worker_update.update` with
    `HOME`, `UV_TOOL_DIR` and `UV_TOOL_BIN_DIR` in a temp dir and a real `uv tool install
    --reinstall`, then runs the installed `nw worker info --json` and asserts its `build_id`
    equals `buildid.compute_wheel(wheel)`. Needs the package index (or a warm uv cache);
    skipped, not failed, when `uv` is missing.
  - `tests/test_worker_update_fish.py` (new, skipped unless `fish` is installed): runs the
    real forced command (`nw worker ssh-command` with `SSH_ORIGINAL_COMMAND="nw worker update"`)
    from `fish -c` with a stub wheel stream and the fake `uv`, asserting the result line.
- **Manual verification** (against the real box, per the real-binary rule, since fakes do not
  prove the integration). On a worker bootstrapped with the dedicated key and a login shell of
  fish, with a changed checkout whose wheel and header were produced by a throwaway script
  (`uv build --wheel`, then the header JSON line followed by the wheel bytes), piped over `ssh
  -i <worker key> BOX nw worker update`:
  1. With a gate job running on BOX, the stream returns `busy` naming the job, exit 5; `nw
     worker info --json` on BOX still shows the old build id.
  2. After the job ends, the stream returns `updated`; `nw worker info --json` shows the new
     build id; no `sudo` appears in the worker's auth log for the update; one remote gate on BOX
     completes.
  3. Sending it again returns `unchanged`.
  4. With a gate job running, `--force` installs without signalling the job; note whether the
     job completes or is retried. Also start a second claim during the install and note the
     `worker updating` refusal.
  Record the outcome in the spec's shipped notes.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus the files
  above, `tests/test_worker_jobs.py`, `tests/test_worker_ssh_command.py` and
  `tests/test_workers_bootstrap.py`; the full suite runs at the queue's close gate.
  `python3 tools/spec_lint.py` exits 0.

### Test record (2026-11-02)

- `uv run pytest -q tests/test_worker_update.py tests/test_worker_ssh_command.py` (new
  `test_worker_update.py`: 38 tests with a fake `uv`/`nw`; grammar cases incl. injections), then
  `python3 tools/affected_tests.py --include tests/test_worker_update{,_fish,_real_uv}.py
  --include tests/test_worker{,_jobs,_ssh_command}.py --include tests/test_workers_bootstrap.py
  --run uv run pytest -q -n 4`: 727 passed.
- `tests/test_worker_update_real_uv.py` ran against the real `uv` (`uv build --wheel` of this
  checkout, a real `uv tool install --reinstall` into a temp `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR`/`HOME`,
  then the installed `nw worker info --json` reported `compute_wheel(wheel)`): passed.
  `tests/test_worker_update_fish.py` ran the real forced command from `fish -c`: passed.
- The full suite was left to the queue's close gate.
- **Not run: the manual steps against the real box** (`nw@morais-Desktop`, the registry's only
  worker). Pushing an unreleased wheel to the human's production worker is an outward-facing
  change to their fleet, and that worker's installed nightwright predates the `update` verb
  (it would refuse `nw worker update`), so it first needs `nw workers bootstrap box --reinstall`.
  The real-`uv` and `fish` tests above are the only real-binary evidence so far.

### Test record, loop 2 (2026-11-02)

- Review fixes: `tests/test_worker_update.py` now 50 tests, plus `tests/test_worker_ssh_command.py`
  and the related worker files: new cases for the claim bound (`PROCESS_STARTED` between
  `install_started_at` and `install_finished_at`, and a missing finish time), a helper process
  holding `job.lock` under `--force` (still alive, never signalled), the second update leaving
  the first's staged wheel and no `.part`, `find_uv` (next to `nw`, `~/.local/bin`, PATH, none
  found), a closed stderr, the stage named by an unexpected error, and stale wheels removed
  before a receive. All passed. The full suite was left to gating; the manual real-box steps
  were still not run (see the first record).

### As shipped (2026-11-02)

- `worker_update.py` as designed, with these deviations: `worker/update.lock` is taken right
  after the header is accepted (before `receive`), not at step 4, so a second concurrent update
  answers `busy` before it can empty the staging dir of the first; after `validate` the part
  file is renamed to `update/<wheel>` because `uv` needs a `.whl` file name (that file is the
  "staged wheel" removed in cleanup). Only the process holding the lock cleans up files.
- `install_started_at` is epoch seconds (`time.time()`), compared with `worker.PROCESS_STARTED`.
  `worker/update.json` is written from step 6 on (not for header/receive/validate/busy/unchanged
  answers), so an `unchanged` answer never erases the install marker older claimants rely on.
- The claim refusal lives in `worker.update_refusal(cfg)` (called under `jobs_lock` by `gate_job`
  and `worker_job.submit`); `WorkerError` gained `retryable`, and the claim `error` frame gains
  `retryable: true`. `update()` takes an optional `nw_argv` (the `nw` used for the verify step)
  so tests can use a fake; the CLI passes none and gets `worker.own_nw()`.
- When the ssh stdout is closed, Python's exit-time flush can turn the process exit status into
  120; the result is still in `worker/update.json`.
- `SPEC-0367` is not yet in this spec's `depends_on`: the spec file is guarded during
  implementation, so that one-line frontmatter edit is left for a later spec edit.
- Loop 2 (review): `_run_install` also writes `install_finished_at` (after verify, before the
  marker is removed) into `worker/update.json`, and `worker.update_refusal` refuses a claimant
  whose `PROCESS_STARTED` is earlier than it (falling back to `install_started_at` while an
  install is running or was killed), so a process exec'd while `uv` replaced the venv cannot
  start a job after the marker is gone. An unexpected error reports the stage that was running;
  the stderr copy is best-effort everywhere; `_receive` also clears stale `nightwright-*.whl`.
- `tests/test_worker_jobs.py` now expects `features == ["async", "persona", "update"]`.
- Loop 3 (human decision): the real-box manual steps (Test plan, "Manual verification") and the
  matching Definition-of-done box ("Test plan executed ... manual steps against the real box")
  are deliberately **left unticked**. The human deferred that check for the merge; it is verified
  and recorded separately by the steward (needs `nw workers bootstrap box --reinstall` first
  because the box's install predates the `update` verb). No evidence exists yet, so no tick. Main
  (a257d16f) was merged into the branch and the ledger regenerated.
- Loop 4 (human waiver): human waived: see ledger. The unrun real-box manual steps (Test plan,
  "Manual verification") are waived by the human (campaign note #275, steward :160; evidence
  conductor:156), so the matching Definition-of-done box is ticked on that waiver, **not** because
  the steps were performed. No real-box evidence exists in this spec; the only real-binary
  evidence is the real-`uv` and `fish` tests in the Test record. Status set to `done` on that
  basis; the ledger was regenerated.

## Rollout / migration

1. **Admission:** SPEC-0367 must be on main first (build id, `compute_wheel`, `local()`, the
   `build_id` field in `worker info`); this spec's validation and verification compare build ids.
   When it is, add `SPEC-0367` to `depends_on` and re-check the names in Context.
2. Land this spec. Workers still running a pre-`update` nightwright refuse the verb (exit 2,
   `not allowed`); each needs one `nw workers bootstrap NAME --ssh ... --reinstall` (with
   `--admin` if that is how it was set up) to get the verb. Until the coordinator-side
   follow-up spec lands, the verb is driven by hand or by that spec's command.
3. Verify with the manual steps above on one real worker before using it on the rest.
4. No registry or schema change; `nw.worker.info.v1` gains only the `update` feature entry, and
   the claim-stage error frame gains an optional `retryable` field.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected automated tests pass and the manual steps against the real
      box were performed and noted in the spec.
- [x] No regressions (full suite at the queue's close gate).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated with `python3 tools/spec_lint.py --write-ledger`, and
      `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
