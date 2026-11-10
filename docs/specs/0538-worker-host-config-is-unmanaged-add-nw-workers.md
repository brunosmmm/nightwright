---
id: SPEC-0538
title: "Manage worker-host config with nw workers config; bootstrap and update write agentic.workers.executor"
status: proposed
owner: user
created: 2026-11-10
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, workers, slurm, ops]
depends_on: [SPEC-0354, SPEC-0385, SPEC-0503]
source_idea: IDEA-1164
---

## Context

[SPEC-0503](./0503-worker-executor-seam.md) chooses how a worker host starts jobs from that
host's own `agentic.workers.executor` (`executors.configured` in
`src/nightwright/agentic/executors.py`). Absent or blank means `local`. The coordinator does
not pick an executor at submit time, and a job keeps the executor recorded in its `job.json`.
[SPEC-0510](./0510-slurm-executor-on-the-current-box.md) opts a host into Slurm by setting
that key to `slurm`. Nothing in nightwright writes it. On the current box the value was put
into the `nw` user's `config.yaml` by hand.

The forced command (`check_ssh_command` in `src/nightwright/agentic/worker.py`) refuses every
`nw worker` verb it does not list, so the coordinator cannot edit that file over the worker
key. `nw workers bootstrap` ([SPEC-0354](./0354-nw-workers-bootstrap.md), steps in
`workers_bootstrap.STEPS`) installs nightwright, the forced-command line, and the registry
record, and never touches the worker user's config. `nw workers update`
([SPEC-0385](./0385-nw-workers-update-coordinator.md)) replaces the tool install and never
touches it either. A clean reinstall (a new home, or a `config.yaml` that is gone) therefore
comes back as `local` until someone edits the box again. `uv tool install` itself leaves
`config.yaml` alone; the gap is that the desired value lives only in that file.

## Goals / Non-goals

**Goals**

- `nw workers config HOST get [KEY]` and `nw workers config HOST set KEY VALUE` read and
  write a closed set of keys on a registered SSH worker. The first key is
  `agentic.workers.executor`, values `local` and `slurm`.
- The forced command allows `nw worker config-set` with the literal grammar below, and
  refuses every other shape of that verb.
- The worker record stores the desired `executor`. Bootstrap and update write it onto the
  host, including when the host file is missing, so a later reinstall restores it.
- A managed write changes only that key. Sibling keys (the box's `agentic.slurm.*` from
  SPEC-0510) stay. A write of the value already on disk does not rewrite the file.

**Non-goals**

- Managing `agentic.slurm.*` or any key other than `executor`. A free-form YAML editor over
  the forced key is out of scope.
- An unset operation that deletes the key. `set executor local` writes the explicit string
  `local`.
- Pull workers (`mode: pull`). The coordinator has no SSH session to them.
- Choosing the executor in the job header, draining or migrating jobs that already started,
  and SPEC-0516's `nw workers bootstrap --slurm` (installing Slurm itself).
- Pruning `config.yaml.bak.*` backups.

## Decision

Add one forced verb, `nw worker config-set`, whose arguments are a closed grammar for get
and set. The coordinator command `nw workers config HOST get|set` talks to a registered
worker with that verb over the same SSH argv as `nw workers check`. The desired executor is
stored on the worker record as `executor`. `set` records it, then pushes. Bootstrap grows a
`config` step after `register`, and update pushes once the new build has been checked, so
both reapply the recorded value onto the host file. When the record has no `executor`,
bootstrap and update leave the host file alone, so a hand edit on a worker that has not been
recorded yet is not overwritten with `local`.

The host file remains what `executors.get` reads. This spec only gives the coordinator a way
to write that file.

## Design

**Allowlist.** One table in a new module `src/nightwright/agentic/worker_host_config.py`:

| Key | YAML location | Values |
|---|---|---|
| `executor` | `agentic.workers.executor` | `local`, `slurm` |

The values are this constant, not `executors.known()`. A test that registers a fake executor
must not make that name settable over SSH. Both constant values are names the production
registry already contains.

**Forced grammar.** `config-set` joins `SSH_SUBCOMMANDS`. `check_ssh_command` accepts only:

- `nw worker config-set get`
- `nw worker config-set get executor`
- `nw worker config-set set executor local`
- `nw worker config-set set executor slurm`

`nw` may be an absolute path ending in `/nw`, as for the other verbs. Any other token
(`config-set` alone, a third value word, `SLURM`, a shell metacharacter, a different key)
raises `ValueError` and `ssh_command` exits 2 with `refused:` on stderr, having run nothing.
The worker command checks the same allowlist again, so a local invocation on the box is held
to the same set. A rejected key or value exits 2, writes nothing, and prints one JSON line
`{"schema":"nw.worker.config_set.result.v1","ok":false,"error":"..."}`.

**Worker command** `nw worker config-set` (in `cli_worker.py`, implementation in
`worker_host_config.py`). Stdout is one JSON line on success,
`schema` `nw.worker.config_set.result.v1`:

- get one key: `{"ok":true,"op":"get","key":"executor","value":"slurm"|null,"effective":"local"|"slurm"|<raw>}`
- get with no key: `{"ok":true,"op":"get","keys":{"executor":{"value":null,"effective":"local"}}}`
- set: `{"ok":true,"op":"set","key":"executor","value":"slurm","effective":"slurm","unchanged":false}`

`value` is the string in the file, or `null` when the file or the key is absent. `effective`
is `executors.configured(cfg)` (absent or blank becomes `local`; any other non-empty string
is returned as written, so a hand-edited `bogus` is visible and is not rewritten by get).
The file is `<config_dir>/config.yaml` of the process that runs the command
(`paths.config_dir` as the worker user). The coordinator's environment is not forwarded.

File rules for set:

- Take an exclusive flock on `config.yaml.lock` in that directory for the read-modify-write.
- `lstat` the path. A symlink, or a path that is not a regular file and not absent, exits 3
  and leaves the inode in place.
- `yaml.safe_load`. A document that is not a mapping (and not empty) exits 3 with the bytes
  unchanged. An absent or empty file is `{}`.
- When the nested value is already the requested string, return `unchanged: true` and do not
  open the file for write (bytes and mtime stay, comments stay).
- Otherwise set only `agentic.workers.executor` to that string, leave every other key, dump
  with `yaml.safe_dump(..., sort_keys=False)`, write a temp file in the same directory at
  mode `0600`, `os.replace` it over `config.yaml`, and `fsync` before the replace. When the
  file already existed, copy its previous bytes to `config.yaml.bak.YYYYMMDDHHMMSS` (append
  `-<pid>` if that name exists) at mode `0600` first. A failure before `os.replace` deletes
  the temp and leaves the original. The replaced file is mode `0600`.
- PyYAML drops comments on a real change. Keys other than `agentic.workers.executor` round-trip
  to the same parsed values, including sibling keys under `agentic.workers` and `agentic.slurm`.

Get never creates or modifies the file.

**Coordinator CLI** (`cli_workers.py`), a group so the words match the idea:

```
nw workers config HOST get [KEY]
nw workers config HOST set KEY VALUE [--json]
```

`get` also takes `--json`. `HOST` is the registry name (`box`), the same token as
`nw workers check`. The SSH argv is `normalize(cfg, rec)["ssh_argv"] + [ssh, nw, "worker",
"config-set", ...]`, timeout `workers.CHECK_TIMEOUT_S` (30 s). Parse the last stdout line
whose JSON `schema` is `nw.worker.config_set.result.v1` (a login banner may precede it).

- Unknown name: exit 1, no SSH, registry unchanged (same `no worker named` path as the other
  commands).
- Pull worker: exit 1, `no ssh to a pull worker`, registry unchanged.
- Key or value outside the allowlist: click usage error (exit 2), no SSH, registry unchanged.
- `get` with no key requests every allowlisted key. Human lines are
  `HOST executor live=<value|"(unset)"> effective=<effective> desired=<record|"(unset)">`.
  `--json` prints one object `{"name","op":"get","desired","keys":{...}}` using the worker's
  `value` and `effective`. SSH failure: exit 1, the human line still shows `desired` when the
  record has one, and `error` is the timeout or the last stderr line.
- `set` writes `executor` onto the raw registry record through `workers.update` first, then
  pushes. A registry write failure pushes nothing and exits 1. A push failure (timeout,
  non-zero exit, `ok: false`, unparseable stdout) exits 1 with
  `HOST executor desired=<value>; host not updated: <reason>` and leaves the record at the
  new value so the next `set` or `update` retries. Success prints
  `HOST executor <value> (written|unchanged)`. When the file was actually rewritten, also
  print `HOST: jobs already running keep the executor they started with`. `--json` on
  success is `{"name","op":"set","key","value","effective","desired","unchanged","pushed":true}`.
  On a failed push it is the same shape with `"pushed":false` and `"error"`, and the process
  exits 1.

The coordinator's own `config.yaml` is not written. Only `<agentic_dir>/workers.json` gains
the field.

**Registry.** `workers._check_record` accepts an absent `executor`. When the field is
present it must be one of the allowlist values; anything else is a `ValueError` from
`read_strict`, the same way a bad `identity` rejects the file. `normalize` passes the field
through. No other record field changes meaning.

**Bootstrap** (`workers_bootstrap.py`). `prepare` gains a keyword-only `executor=None` after
`reinstall`, so existing positional callers stay valid. The flag `--executor` on
`nw workers bootstrap` is that allowlist. Omitted, `prepare` copies `executor` off the
existing record when the record has one. The flag overrides a recorded value.
`_record_diff` does not compare `executor` (ssh, slots, identity, and `nw` still conflict
as they do today).

`STEPS` becomes `("admin", "install", "verify", "key", "register", "config")`. The `config`
step runs as the worker user over bootstrap's existing `on_worker` session (the human's SSH,
including `--admin`'s `sudo -n -i -u USER`), not through the forced key, because bootstrap
already has that session and the key step may have been `--install-key` off. The script is
one line, `{bin}/nw worker config-set set executor {value}`, with `bin` matching `BIN_RE`
and `value` taken from the allowlist only. The step parses the result line the same way.
Non-zero, `ok: false`, or no result line is `StepFailed` and bootstrap exits 1. The step
status is `done` with a detail `written` or `unchanged`.

When `plan.executor` is `None`, the step does not call SSH and its status is
`skipped (no executor recorded)`. `dry_plan` lists no command in that case and uses the same
status. When an executor is set, `dry_plan` lists that one script line and the status is
`would run`.

`step_register` stores `executor` on a new record when `plan.executor` is set, and on an
existing record updates the field when it differs, still reporting
`skipped (already registered)` for the add itself. A failed `config` step therefore leaves
the recorded intent in place (register already ran) and leaves the host file as the worker
command left it. A later bootstrap reaches `config` again because install/verify/key/register
skip when they are already done. A failed earlier step still marks `config` `not run`.

**Update.** After `nw workers update` has a result state `updated` or `unchanged` and the
post-update `check` has matched the wheel's build id, if the record's `executor` is in the
allowlist, push `config-set set executor <value>` with the same argv and timeout as the CLI.
Do this in the click command and in `workers_update.perform` (the queue's `--when-idle`
path). A push failure exits 1 / `Outcome(kind="failed")` whose detail says the build was
updated and `config-set` did not write the executor. `busy`, `failed`, and a missing result
line (the old-worker `not allowed` path) do not push. A record with no `executor` does not
push and does not open a second SSH. `--dry-run` prints the config-set argv when the record
has an executor, sends nothing, and when it has none prints
`executor: not recorded; host config untouched`.

**Docs and help.** The `nw workers` and `nw worker` command help, and the `ssh-command`
docstring, list the new verb. `docs/WORKFLOW.md` gains the four `config-set` forms in the
forced-command list, a sentence on the executor paragraph that
`nw workers config HOST set executor slurm` records and writes the key and that bootstrap
and update reapply the record, and a **config** bullet after **register** in the bootstrap
step list. The existing `command="<bin>/nw worker ssh-command",restrict` example stays.

## Alternatives considered

- **Keep editing `config.yaml` on the box.** That is the bug: the forced key cannot do it,
  and a new home does not get the key back.
- **Send the executor in the job header.** SPEC-0503 fixes the choice on the host file and
  on the job that already started. A header would split that rule.
- **Accept arbitrary YAML over `config-set`.** The forced command would be a remote editor
  for a file that can hold paths and credentials. The allowlist is one enum.
- **Put the value only in the `nw worker update` header.** A config change would wait for a
  wheel install, and bootstrap would still need a second path. Update calls the same verb
  after a successful check.
- **Overload `nw workers set --executor`.** `set` changes slots and kinds and does not open
  SSH. The idea names `nw workers config`.
- **Write `local` whenever the record has no executor.** The next update of the current box
  would replace a hand-edited `slurm` before anyone had recorded it.

## Acceptance criteria

- [ ] `nw workers config HOST get [executor]` and `nw workers config HOST set executor (local|slurm)`
      exist, with `HOST` before the verb. `--json` on either prints one JSON object and no
      other stdout.
- [ ] An unknown name and a pull worker exit 1, open no SSH session, and leave `workers.json`
      bytes unchanged. A key or value outside the allowlist exits 2 and also opens no SSH
      and leaves the registry unchanged.
- [ ] `check_ssh_command` accepts only the four `config-set` forms listed in Design, and
      `ssh_command` execs those. Every other `config-set` shape exits 2 with `refused:` and
      does not exec.
- [ ] The worker command refuses a bad key or value with exit 2, `ok: false`, and the
      config file's bytes unchanged. Get of a missing file returns `value: null` and
      `effective: "local"` and does not create the file.
- [ ] Set of a different value writes only `agentic.workers.executor`, preserves the parsed
      values of every other key (including `agentic.slurm.conf` and other `agentic.workers`
      keys), replaces atomically, and leaves the file mode `0600`. A symlink `config.yaml`
      and a non-mapping document exit 3 with the original inode's bytes unchanged.
- [ ] Set of the value already stored returns `unchanged: true` and leaves the file's bytes
      and mtime unchanged. A real change writes one `config.yaml.bak.*` sibling at mode
      `0600`; an unchanged set writes no backup.
- [ ] `set` stores `executor` on the worker record before the SSH push. A failed push exits 1,
      keeps that record value, and leaves the host file unchanged. A successful push leaves
      the record and the host file on that value. The coordinator's `config.yaml` is unchanged.
- [ ] Get reports the live file value, `effective`, and the record's desired value. A live
      read failure exits 1 and still reports the desired value.
- [ ] `nw workers bootstrap --executor local|slurm` records that value and the `config` step
      (after `register`) runs `{bin}/nw worker config-set set executor <value>` as the worker
      user. With no flag and no recorded executor, the step is
      `skipped (no executor recorded)` and runs no remote command. `--executor` does not
      cause a settings conflict. A failed earlier step leaves `config` as `not run`. A
      failed `config` step exits 1 and leaves the executor already stored by `register`.
- [ ] `dry-run` of bootstrap and of update prints the `config-set` command and sends nothing
      when an executor is recorded, and prints that none is recorded otherwise.
- [ ] `nw workers update` and `workers_update.perform`, after `updated` or `unchanged` and a
      matching build-id check, push the recorded executor. A push failure is exit 1 /
      `kind: "failed"` and the detail says the build was updated and the executor was not
      written. No recorded executor, or a `busy` / `failed` / verb-missing update, pushes
      nothing.
- [ ] Jobs already running are not signalled or rewritten. `executors.get` still reads the
      host file. `nw worker info` keeps reporting `executor` and `executor:<name>` from that
      file, so `nw workers check HOST` shows `slurm` after a successful set to `slurm`.
- [ ] `docs/WORKFLOW.md` and the `nw workers` / `nw worker` / `ssh-command` help text describe
      the new command, the four forced forms, and the bootstrap `config` step.
- [ ] `python3 tools/test_tiers.py --check` exits 0.

## Test plan

- **Automated tests:** new module `tests/test_worker_host_config.py` with exactly one
  module-level speed-tier marker, `pytestmark = pytest.mark.fast`. It asserts:
  - `config-set` is in `SSH_SUBCOMMANDS`, and `check_ssh_command` maps the four allowed
    command strings onto `["config-set", ...]` and raises `ValueError` for `config-set`
    alone, an extra word, `executor SLURM`, a different key, and a metacharacter form
    (`config-set set executor local; id`).
  - Direct `apply` against a temp `config_dir`: missing file get; set creates mode `0600`
    and the key; a pre-existing `agentic.slurm.conf` and `agentic.workers.key` still parse
    to the same values; set of the same value leaves bytes and mtime and writes no backup;
    a real change writes a `0600` backup; symlink and a list document exit 3 with bytes
    unchanged; a bad value writes nothing.
  - CliRunner `nw workers config` with a stub SSH: get JSON; set writes the registry field
    then the argv ending in `worker config-set set executor slurm`; a failing stub leaves
    the registry set, the stubbed host file untouched, and exit code 1; pull and unknown
    name exit 1 with the registry unchanged; `set executor bogus` exits 2 with no SSH.
    The coordinator `config.yaml` sentinel in that temp config dir is unchanged.
  - `prepare` copies a recorded executor and lets `--executor` override it without a
    conflict; `step_register` stores it on a new record and updates it on an existing one;
    `step_config` with a fake `on_worker` sends the one-line script and returns `done`, and
    with `executor is None` does not call `on_worker`; `dry_plan` lists the script only when
    an executor is set; monkeypatching `step_register` to raise leaves `config` as
    `not run`.
  - `workers_update.perform` and the update command, with `send` and `check` stubbed: a
    record with `executor: slurm` and an `updated` result runs one config-set SSH and
    returns `updated`; that SSH failing returns `failed` and names `config-set`; a record
    without `executor` does not start that SSH; a `busy` result does not either.
  - `read_strict` accepts `executor: slurm` and raises `ValueError` on `executor: bogus`.
  Extend `tests/test_worker_ssh_command.py` (its existing `pytestmark`) with the four
  allowed commands in the exec parametrization and the refused shapes next to the other
  grammar refusals. Do not add a second tier marker.
  Update equality assertions on bootstrap status dicts in `tests/test_workers_bootstrap.py`
  (its existing marker) so a run with no recorded executor shows
  `config: skipped (no executor recorded)` when the step is reached and `config: not run`
  when an earlier step failed. That module's fake `nw` does not need a `config-set` handler
  for those runs, because the step must not call it.
  `tests/test_workers_update_cmd.py` (its existing marker) stays green: its records have no
  `executor`, so the stub SSH is still invoked once for the wheel.
  `python3 tools/test_tiers.py --check` exits 0 (`tools/test_tiers.py` is in the tree).
- **Manual verification:** on the coordinator, against the registered box,
  `nw workers config box get`, then `nw workers config box set executor slurm` (exit 0),
  then `get` again (`live=slurm`, `desired=slurm`). As the `nw` user on the box,
  `config.yaml` parses with `agentic.workers.executor: slurm` and the same `agentic.slurm`
  values as before the set. A second `set executor slurm` reports `unchanged`.
  `nw workers update box --dry-run` and
  `nw workers bootstrap box --ssh <the recorded dest> --dry-run` each print a
  `config-set set executor slurm` line and change nothing on the box.
  `nw workers check box` reports executor `slurm`.
- **Regression guard:** the affected-test run of the modules above. Bootstrap without an
  executor still installs, verifies, installs the key, and registers, with `config` skipped.
  Update of a record without `executor` is still one SSH. The forced-command refusals
  already in `tests/test_worker_ssh_command.py` still refuse. `executors.configured` for a
  missing key is still `local`.

## Rollout / migration

1. Ship the code. Records without `executor` make bootstrap and update leave the host file
   alone, so the box's hand-edited `slurm` survives the deploy.
2. Update the box to a build that contains `config-set` (`nw workers update box`, or a
   bootstrap that installs this checkout).
3. `nw workers config box set executor slurm`. That records the opt-in and rewrites the key
   only if the file differs. From then on, bootstrap (including a reinstall onto a new home)
   and update recreate the key when the file is missing.
4. `nw workers config box get` and `nw workers check box` both show `slurm`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; the automated tests listed above pass; the manual steps were run
      and their results noted in this spec.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`) and
      `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
