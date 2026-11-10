---
id: SPEC-0538
title: "Add nw workers config and the forced nw worker config-set verb"
status: done
owner: user
created: 2026-11-10
updated: 2026-11-10
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
key. `paths.config_dir()` is `~/.config/nightwright` (or `$NW_CONFIG_DIR` /
`$XDG_CONFIG_HOME/nightwright`). A new worker home has neither that directory nor
`config.yaml`, and `load_config` does not create the directory. `uv tool install` leaves
`config.yaml` alone.

`workers.check` stores the probed `nw worker info --json` with `workers._cache_info`.
`workers.lease_free` and `workers.runs_slurm` read that cache (`lease:none` and
`executor:slurm`), not the host file. A write that leaves the cache on the previous
executor makes the coordinator treat a host that was just opted into Slurm as `local`.

This spec is the write path: a closed forced verb, `nw workers config`, the registry field
that remembers the desired value, a cache refresh after a successful push, and a human
`executor:` line on `nw workers check`. Reapplying that field from bootstrap and update is a
follow-on spec (Non-goals).

## Goals / Non-goals

**Goals**

- `nw workers config HOST get [KEY]` and `nw workers config HOST set KEY VALUE` read and
  write a closed set of keys on a registered SSH worker, over the same SSH path as
  `nw workers check` (including `ensure_record_ssh_dir`). The first key is
  `agentic.workers.executor`, values `local` and `slurm`.
- The forced command allows `nw worker config-set` with the literal grammar below, and
  refuses every other shape of that verb.
- The worker record stores the desired `executor`. `set` writes that key on the host,
  including when the host file or config_dir is missing. A record with no `executor` stays
  valid.
- A managed write changes only that key. Sibling keys (the box's `agentic.slurm.*` from
  SPEC-0510) stay. A write of the value already on disk does not rewrite the file.
- A successful push stores a fresh `nw worker info --json` on the record, so the cached
  `info.executor` is the value just written. Human `nw workers check HOST` prints that
  executor.

**Non-goals**

- Bootstrap and update reapplying the recorded executor. That follow-on depends on this
  verb. Out of scope here: `nw workers bootstrap --executor`; a bootstrap `config` step
  after `register` (`BIN_RE.fullmatch`, `on_worker`, persisting `executor` from register
  only after a successful register check); `nw workers update` and `workers_update.perform`
  pushing the recorded allowlisted executor after a matching build-id check and then
  refreshing cached info; leaving the host file alone and opening no second SSH when no
  executor is recorded; dry-run text for those commands; the WORKFLOW bootstrap **config**
  bullet. A failed push on that follow-on does not replace `info`. This spec does not add
  a bootstrap step and does not push from update.
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
worker with that verb over the same SSH argv as `nw workers check`, after
`ensure_record_ssh_dir`. The desired executor is stored on the worker record as `executor`.
`set` records it, then pushes. A successful push (exit 0 and `ok` true) then runs
`nw worker info --json` on that same SSH argv and stores it with `workers._cache_info`.
When the record has no `executor`, nothing in this spec writes the host file. Bootstrap
and update stay as they are.

The host file remains what `executors.get` reads. This spec gives the coordinator a way to
write that file and to refresh the cache from the file just written.

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
Exit 3 (the file rules below) prints the same JSON line with `ok` false and a short `error`,
and writes nothing.

File rules for set:

- If `config_dir` is absent, create it and any missing parents at mode 0700, then take the
  lock. Each directory this step creates is chmod'd to 0700, so the resulting `config_dir`
  mode is 0700 (`st_mode & 0o777 == 0o700`) under a non-zero umask. Parents that already
  existed are left alone. If `config_dir` exists and `lstat` is not a directory, a symlink
  included, exit 3 and create nothing (no directory, no `config.yaml`, no lock, no backup).
  Get never creates the directory or the file.
- Take an exclusive flock on `config.yaml.lock` in that directory for the read-modify-write.
- `lstat` the `config.yaml` path. A symlink, or a path that is not a regular file and not
  absent, exits 3 and leaves the inode in place.
- `yaml.safe_load`. A document that is not a mapping (and not empty) exits 3 with the bytes
  unchanged. An absent or empty file is `{}`.
- `yaml.safe_load` returning None (empty or comments only) is `{}`. A `yaml.YAMLError` exits
  3 with the bytes unchanged. If `agentic` or `agentic.workers` is present and is not a
  mapping, set exits 3 with the bytes unchanged; a missing node is created. Only the final
  `executor` key may replace a non-string.
- When the nested value is already the requested string, return `unchanged: true` and do not
  open the file for write (bytes and mtime stay, comments stay).
- Otherwise set only `agentic.workers.executor` to that string, leave every other key, dump
  with `yaml.safe_dump(..., sort_keys=False)`, write a temp file in the same directory at
  mode `0600`, `os.replace` it over `config.yaml`, and `fsync` before the replace. When the
  file already existed, copy its previous bytes to `config.yaml.bak.YYYYMMDDHHMMSS` (append
  `-<pid>` if that name exists) at mode `0600` first. A failure before `os.replace` deletes
  the temp and leaves the original. The replaced file is mode `0600`. An exit 3 leaves no
  backup and no temp.
- PyYAML drops comments on a real change. Keys other than `agentic.workers.executor` round-trip
  to the same parsed values, including sibling keys under `agentic.workers` and `agentic.slurm`.
  A comments-only file therefore gains `agentic.workers.executor` and keeps no other keys.

Get never creates or modifies the file, the directory, or the lock.

**Coordinator CLI** (`cli_workers.py`), a group so the words match the idea:

```
nw workers config HOST get [KEY]
nw workers config HOST set KEY VALUE [--json]
```

`get` also takes `--json`. `HOST` is the registry name (`box`), the same token as
`nw workers check`. Before any SSH, call `workers.ensure_record_ssh_dir(cfg, normalized)`
the way `workers.check` does. `OSError` exits 1 with no SSH (`ssh socket directory unusable:
...`). The SSH argv is `normalize(cfg, rec)["ssh_argv"] + [ssh, nw, "worker", "config-set",
...]`, timeout `workers.CHECK_TIMEOUT_S` (30 s). Parse the last stdout line whose JSON
`schema` is `nw.worker.config_set.result.v1` (a login banner may precede it). This path
does not call `workers.check`.

- Unknown name: exit 1, no SSH, registry unchanged (same `no worker named` path as the other
  commands).
- Pull worker: exit 1, `no ssh to a pull worker`, registry unchanged.
- Key or value outside the allowlist: click usage error (exit 2), no SSH, registry unchanged.
- `get` with no key requests every allowlisted key. Human lines are
  `HOST executor live=<value|"(unset)"> effective=<effective> desired=<record|"(unset)">`.
  `--json` prints one object `{"name","op":"get","desired","keys":{...}}` using the worker's
  `value` and `effective`. SSH failure, including `ensure_record_ssh_dir` raising: exit 1,
  the human line still shows `desired` when the record has one, and `error` is the timeout,
  the socket-directory error, or the last stderr line. Get does not call `_cache_info`.
- `set` writes `executor` onto the raw registry record through `workers.update` first, then
  pushes. A registry write failure pushes nothing and exits 1. A push failure (ensure
  failure, timeout, non-zero exit, `ok: false`, unparseable stdout) exits 1 with
  `HOST executor desired=<value>; host not updated: <reason>` and leaves the record at the
  new value so the next `set` retries. The host file is unchanged. A failed push does not
  replace `info`.
- A successful push (exit 0 and `ok` true), including `unchanged: true`, then runs
  `nw worker info --json` on that same SSH argv and stores it with `workers._cache_info`,
  so `info.executor`, `executor:<name>`, and `lease:none` match the file just written.
  Those fields are whatever `nw worker info --json` reports for that file
  (`executor:<name>` when the name is known to the worker; `lease:none` when that
  executor's `lease_cancels` is false, which is Slurm). The info argv is
  `ssh_argv + [ssh, nw, "worker", "info", "--json"]`, timeout `workers.CHECK_TIMEOUT_S`,
  parsed as `workers._check` parses info (`json.loads` of stdout, a mapping). Exit 0
  requires that store, and the stored `info.executor` equals the value just written. When
  the info probe fails, or its `executor` differs from the value just written, the command
  exits 1, does not call `_cache_info`, leaves the previous `info` object unchanged, and
  reports `HOST executor <value> (written|unchanged); info cache not refreshed: <reason>`.
  The host file stays as the worker command left it.
- Success (file pushed and info stored) prints `HOST executor <value> (written|unchanged)`.
  When the file was actually rewritten, also print
  `HOST: jobs already running keep the executor they started with`. `--json` on success is
  `{"name","op":"set","key","value","effective","desired","unchanged","pushed":true}`.
  On a failed push it is the same shape with `"pushed":false` and `"error"`, and the process
  exits 1. On an info-probe failure after a successful push it is that shape with
  `"pushed":true` and `"error"`, and the process exits 1.

The coordinator's own `config.yaml` is not written. Only `<agentic_dir>/workers.json` gains
the `executor` field and, after a successful push, a replaced `info` object.

**Registry.** `workers._check_record` accepts an absent `executor`. When the field is
present it must be one of the allowlist values; anything else is a `ValueError` from
`read_strict`, the same way a bad `identity` rejects the file. `normalize` passes the field
through. No other record field changes meaning. The desired value lives on `executor`. The
cached probe lives on `info` and is replaced only by `_cache_info` after a successful push.
Records that have no `executor` stay valid, and this spec's get/set are the only commands
that write the field.

**Human check.** Human `nw workers check HOST` prints a line `executor: <info.executor>`
from the probed info, and `executor_error: <text>` when that field is present. The line is
one of the detail lines under the `ok` / `FAILED` line (same indent as the capacity line).
When `info` is not a mapping, neither line is printed. `--json` is unchanged: the check
payload stays `{name, ok, errors, warnings, info}` (and `slurm_check` when `--slurm` ran).
`info.executor` is already inside that `info` object.

**Docs and help.** The `nw workers` and `nw worker` command help, and the `ssh-command`
docstring, list the new verb and the four forms. `docs/WORKFLOW.md` gains the four
`config-set` forms in the forced-command list, and a sentence that
`nw workers config HOST set executor slurm` records the value and writes the key. The
existing `command="<bin>/nw worker ssh-command",restrict` example stays. This spec does not
add a bootstrap step bullet.

## Alternatives considered

- **Keep editing `config.yaml` on the box.** That is the bug: the forced key cannot do it,
  and a missing `config_dir` cannot be written until something creates it.
- **Send the executor in the job header.** SPEC-0503 fixes the choice on the host file and
  on the job that already started. A header would split that rule.
- **Accept arbitrary YAML over `config-set`.** The forced command would be a remote editor
  for a file that can hold paths and credentials. The allowlist is one enum.
- **Refresh the cache by calling `workers.check` before the write.** Check stores the
  pre-write probe. `lease_free` and `runs_slurm` would then see `local` on a host that
  just became `slurm`. The info probe runs after a successful push, and it is not `check`.
- **Fold bootstrap and update into this spec.** The follow-on in Non-goals depends on this
  verb. Landing the grammar, the file rules, the registry field, and the cache refresh on
  `set` first keeps that follow-on from changing install and update in the same change.
- **Overload `nw workers set --executor`.** `set` changes slots and kinds and does not open
  SSH. The idea names `nw workers config`.
- **Write `local` whenever the record has no executor.** A record that has not been
  recorded yet must stay valid, and this spec must not invent a host-file write for it.

## Acceptance criteria

- [x] `nw workers config HOST get [executor]` and `nw workers config HOST set executor (local|slurm)`
      exist, with `HOST` before the verb. `--json` on either prints one JSON object and no
      other stdout.
- [x] An unknown name and a pull worker exit 1, open no SSH session, and leave `workers.json`
      bytes unchanged. A key or value outside the allowlist exits 2 and also opens no SSH
      and leaves the registry unchanged.
- [x] `check_ssh_command` accepts only the four `config-set` forms listed in Design, and
      `ssh_command` execs those. Every other `config-set` shape exits 2 with `refused:` and
      does not exec.
- [x] The worker command refuses a bad key or value with exit 2, `ok: false`, and the
      config file's bytes unchanged. Get of a missing file returns `value: null` and
      `effective: "local"` and does not create the file or the directory.
- [x] Set of a different value writes only `agentic.workers.executor`, preserves the parsed
      values of every other key (including `agentic.slurm.conf` and other `agentic.workers`
      keys), replaces atomically, and leaves the file mode `0600`. A symlink `config.yaml`
      and a non-mapping document exit 3 with the original inode's bytes unchanged.
- [x] Set against a temp home with no `config_dir` creates that directory mode 0700 and
      `config.yaml` mode 0600. A symlink `config_dir` exits 3 and creates nothing. Get of a
      missing directory does not create it.
- [x] A file whose `agentic` or `agentic.workers` is a scalar or a list exits 3 and the bytes
      are identical. A comments-only file gains `agentic.workers.executor` and keeps no other
      keys. Invalid YAML exits 3 with the bytes identical. A non-string `executor` value is
      replaced by the requested string; a missing `agentic` or `agentic.workers` node is
      created.
- [x] Set of the value already stored returns `unchanged: true` and leaves the file's bytes
      and mtime unchanged. A real change writes one `config.yaml.bak.*` sibling at mode
      `0600`; an unchanged set writes no backup. Exit 3 writes no backup.
- [x] `set` stores `executor` on the worker record before the SSH push. A failed push exits 1,
      keeps that record value, leaves the host file unchanged, and leaves the previous `info`
      object unchanged. A successful push leaves the record and the host file on that value.
      The coordinator's `config.yaml` is unchanged.
- [x] After a successful set, the stored `info.executor` equals the value just written. A
      push of `slurm` whose info probe reports `executor: slurm` and features `executor:slurm`
      and `lease:none` stores that object. A failed push leaves the previous `info` object
      unchanged. An info probe that fails after a successful file write exits 1, leaves the
      previous `info` object unchanged, and leaves the host file written.
- [x] Get and set call `ensure_record_ssh_dir` before SSH. An `OSError` from it exits 1 and
      opens no SSH. For set, the record already holds the new `executor` and `info` is
      unchanged.
- [x] Get reports the live file value, `effective`, and the record's desired value. A live
      read failure exits 1 and still reports the desired value. Get does not replace `info`.
- [x] `read_strict` accepts a record with no `executor` and a record with `executor: slurm`,
      and raises `ValueError` on `executor: bogus`.
- [x] Jobs already running are not signalled or rewritten. `executors.get` still reads the
      host file. `nw worker info` keeps reporting `executor` and `executor:<name>` from that
      file, and human `nw workers check HOST` prints `executor: slurm` after a successful
      set to `slurm`. `--json` on `nw workers check` is unchanged.
- [x] `docs/WORKFLOW.md` and the `nw workers` / `nw worker` / `ssh-command` help text describe
      the new command and the four forced forms.
- [x] `python3 tools/test_tiers.py --check` exits 0.

### Test record (2026-11-10)

- `uv run pytest -q tests/test_worker_host_config.py tests/test_worker_ssh_command.py` (72 passed)
- `uv run pytest -q tests/test_workers_bootstrap.py tests/test_workers_update_cmd.py` (67 passed)
- `python3 tools/test_tiers.py --check` (exit 0)
- Manual box steps: not run in this loop; after deploy per Rollout.
- Full suite: left to campaign gating.

### Test record (2026-11-11, loop 1)

- `uv run pytest -q tests/test_worker_host_config.py tests/test_worker_ssh_command.py` (99 passed)
- `python3 tools/test_tiers.py --check` (exit 0)
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` (exit 0)
- Manual box steps: not run; after deploy per Rollout.
- Full suite: left to campaign gating.

### Test record (2026-11-10, loop 3)

- `uv run pytest -q tests/test_worker_host_config.py tests/test_worker_ssh_command.py` (120 passed)
- `python3 tools/test_tiers.py --check` (exit 0)
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` (exit 0)
- Manual box steps: not run; after deploy per Rollout.
- Full suite: left to campaign gating.

### Test record (2026-11-11, loop 2)

- `uv run pytest -q tests/test_worker_host_config.py tests/test_worker_ssh_command.py` (81 passed)
- `python3 tools/test_tiers.py --check` (exit 0)
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` (exit 0)
- Manual box steps: not run; after deploy per Rollout.
- Full suite: left to campaign gating.

### Test record (2026-11-10, loop 2 review)

- `uv run pytest -q tests/test_worker_host_config.py tests/test_worker_ssh_command.py` (115 passed)
- `python3 tools/test_tiers.py --check` (exit 0)
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` (exit 0)
- Manual box steps: not run; after deploy per Rollout.
- Full suite: left to campaign gating.

### Test record (2026-11-10, loop 0)

- `uv run pytest -q tests/test_worker_host_config.py tests/test_worker_ssh_command.py` (84 passed)
- `python3 tools/test_tiers.py --check` (exit 0)
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` (exit 0)
- Manual box steps: not run; after deploy per Rollout.
- Full suite: left to campaign gating.

### Test record (2026-11-10, loop 0 review)

- `uv run pytest -q tests/test_worker_host_config.py tests/test_worker_ssh_command.py` (128 passed)
- `python3 tools/test_tiers.py --check` (exit 0)
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` (exit 0)
- Manual box steps: not run; after deploy per Rollout.
- Full suite: left to campaign gating.

### As shipped (2026-11-10)

Review waived (steward): non-UTF-8 config.yaml mapping, `FileExistsError` mkdir race,
`O_NOFOLLOW` on `config.yaml.lock`, and further test-plan gaps filed as follow-up ideas;
not shipped in this change.

## Test plan

- **Automated tests:** new module `tests/test_worker_host_config.py` with exactly one
  module-level speed-tier marker, `pytestmark = pytest.mark.fast`. It asserts:
  - `config-set` is in `SSH_SUBCOMMANDS`, and `check_ssh_command` maps the four allowed
    command strings onto `["config-set", ...]` and raises `ValueError` for `config-set`
    alone, an extra word, `executor SLURM`, a different key, and a metacharacter form
    (`config-set set executor local; id`).
  - Direct `apply` against a temp `config_dir`: missing file get returns `value: null` and
    `effective: "local"` and creates neither the directory nor the file; set on a temp home
    whose `config_dir` is absent creates that directory mode 0700 and `config.yaml` mode
    0600 with the key; a symlink `config_dir` exits 3 and creates nothing; get of a missing
    directory does not create it.
  - A pre-existing `agentic.slurm.conf` and `agentic.workers.key` still parse to the same
    values after set; set of the same value leaves bytes and mtime and writes no backup; a
    real change writes a `0600` backup; a symlink `config.yaml` and a top-level list
    document exit 3 with bytes unchanged; a bad value writes nothing.
  - A file whose `agentic` or `agentic.workers` is a scalar or a list exits 3 and the bytes
    are identical; a comments-only file gains `agentic.workers.executor` and keeps no other
    keys; invalid YAML exits 3 with the bytes identical; an `executor` value that is a list
    is replaced by the string.
  - CliRunner `nw workers config` with a stub SSH: get JSON; set writes the registry field
    then the argv ending in `worker config-set set executor slurm`, then an argv ending in
    `worker info --json`; the info stub's object (`executor: slurm`, features including
    `executor:slurm` and `lease:none`) is the record's stored `info`. A record whose cached
    info says `executor: local`, after that successful push, has `info.executor` `slurm`
    and the feature `executor:slurm`.
  - A failing config-set stub leaves the registry `executor` set, the stubbed host file
    untouched, the previous `info` object unchanged, exit code 1, and does not run the info
    argv. A config-set stub that succeeds and an info stub that fails leaves the host file
    written, the previous `info` object unchanged, and exit code 1.
  - `ensure_record_ssh_dir` is called before SSH. Monkeypatching it to raise `OSError`
    exits 1 with no SSH; for set, the registry holds the new value and `info` is unchanged.
  - Pull and unknown name exit 1 with the registry unchanged; `set executor bogus` exits 2
    with no SSH. The coordinator `config.yaml` sentinel in that temp config dir is unchanged.
    Get does not replace a seeded `info` object.
  - Human `nw workers check box`, with SSH stubbed to return info JSON whose `executor` is
    `slurm`, has stdout that contains the line `executor: slurm`. `--json` still parses as
    the existing check list and includes that `info` object. A probe with no `info` mapping
    prints no `executor:` line.
  - `read_strict` accepts a record with no `executor` and `executor: slurm`, and raises
    `ValueError` on `executor: bogus`.
  Extend `tests/test_worker_ssh_command.py` (its existing `pytestmark`) with the four
  allowed commands in the exec parametrization and the refused shapes next to the other
  grammar refusals. Do not add a second tier marker.
  `python3 tools/test_tiers.py --check` exits 0 (`tools/test_tiers.py` is in the tree).
- **Manual verification:** on the coordinator, against the registered box (a build that
  already contains `config-set`), `nw workers config box get`, then
  `nw workers config box set executor slurm` (exit 0), then `get` again (`live=slurm`,
  `desired=slurm`). As the `nw` user on the box, `config.yaml` parses with
  `agentic.workers.executor: slurm` and the same `agentic.slurm` values as before the set.
  A second `set executor slurm` reports `unchanged`. `nw workers check box` stdout contains
  the line `executor: slurm`. `nw workers update box --dry-run` and
  `nw workers bootstrap box --ssh <the recorded dest> --dry-run` do not print a
  `config-set` line and do not change the box file.
- **Regression guard:** the affected-test run of the modules above.
  `tests/test_workers_bootstrap.py` and `tests/test_workers_update_cmd.py` stay green
  without a new step or a second SSH: this spec does not change `STEPS` or update's push.
  Their existing tier markers stay the only markers. The forced-command refusals already
  in `tests/test_worker_ssh_command.py` still refuse. `executors.configured` for a missing
  key is still `local`. Existing `nw workers check` tests that assert substrings of the
  human output stay green; `--json` check payloads gain no new top-level key.

## Rollout / migration

1. Ship the code. Records without `executor` stay valid. Bootstrap and update still leave
   the host file alone.
2. Update the box to a build that contains `config-set` with the existing
   `nw workers update` or bootstrap. Those commands do not push `executor`.
3. `nw workers config box set executor slurm`. That records the opt-in and rewrites the
   key when the file differs, creating `config_dir` when it is missing. The cached `info`
   then reports `slurm`.
4. `nw workers config box get` shows `live=slurm` and `desired=slurm`. Human
   `nw workers check box` stdout contains the line `executor: slurm`.

A later spec re-applies the recorded value from bootstrap and update, which is what makes a
clean reinstall restore the key without another `set`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; the automated tests listed above pass; the manual steps were run
      and their results noted in this spec.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`) and
      `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
