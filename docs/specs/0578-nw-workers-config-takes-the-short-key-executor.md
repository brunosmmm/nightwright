---
id: SPEC-0578
title: nw workers config accepts the dotted path agentic.workers.executor and lists allowed keys in --help
status: proposed
owner: user
created: 2026-11-12
kind: feature
source_idea: IDEA-1185
tags: [workers, cli]
depends_on: [SPEC-0538]
---

## Context

[SPEC-0538](./0538-worker-host-config-is-unmanaged-add-nw-workers.md) added
`nw workers config NAME get|set` to read and write an allowlisted key of a worker host's
`config.yaml` over SSH. The allowlist (`ALLOWED` in
`src/nightwright/agentic/worker_host_config.py`) is keyed by a short name: `executor`. Every
other place that names this setting uses the full YAML path `agentic.workers.executor`: the
worker's own `config.yaml`, the executor error text (`src/nightwright/agentic/executors.py`
"unknown executor ... (agentic.workers.executor)"), and specs 0501, 0503, 0510 and 0538 itself.

So an operator who copies the path from any of those and runs
`nw workers config box set agentic.workers.executor slurm` gets a usage error
(`key 'agentic.workers.executor' is not allowed`) that does not say what is allowed. The
command's `--help` is a one-line docstring and lists no keys either, so the only way to learn
the accepted key is to read SPEC-0538.

## Goals / Non-goals

**Goals**
- `nw workers config NAME get|set` accepts the full dotted path of an allowlisted key as well as
  its short name, with identical behavior.
- A refused key names the accepted spellings, and `nw workers config --help` lists the allowed
  keys (both spellings) and their values.

**Non-goals**
- No new allowlisted key and no new value: the allowlist stays `executor` = `local`|`slurm`.
- No change on the worker host: the forced SSH grammar of `nw worker config-set`
  (`check_ssh_command` in `src/nightwright/agentic/worker.py`) and the host-side validation stay
  short-key only, so SPEC-0538's security surface is unchanged.
- No partial paths (`workers.executor`), no case folding, no globbing: only the two exact
  spellings are accepted.
- No change to the registry record field (`executor`) or to the output field names.

## Decision

Normalize the key on the coordinator, before anything else happens: a small table maps each
accepted spelling (short name and full dotted path) to the canonical short key. The command
validates the canonicalized key, then sends the canonical short key to the host and records
it in output, so everything downstream of the CLI is exactly as in SPEC-0538. Unknown keys fail
with a usage error that lists the accepted spellings, and the command help lists them too.

## Design

**Table.** In `src/nightwright/agentic/worker_host_config.py` add, next to `ALLOWED`:

```python
KEY_PATHS: dict[str, str] = {"executor": "agentic.workers.executor"}   # canonical -> YAML path

def canonical_key(key: str) -> str:
    """Return the canonical short key for `key` (short name or full dotted path), else raise
    ValueError listing the accepted spellings."""
```

`canonical_key` matches exactly (no strip, no case folding): `"executor"` and
`"agentic.workers.executor"` both return `"executor"`; anything else raises
`ValueError("key 'x' is not allowed (allowed: executor, agentic.workers.executor)")`. The
accepted spellings are derived from `KEY_PATHS`, so a future key added to `ALLOWED` and
`KEY_PATHS` is listed automatically. `validate_key` calls `canonical_key` and keeps its
existing "is not allowed" wording; `validate_value` is unchanged apart from validating the
canonical key. A test asserts `KEY_PATHS` and `ALLOWED` have the same keys.

**CLI.** In `workers_config` (`src/nightwright/agentic/cli_workers.py`), `get` and `set`
replace `key` with `canonical_key(key)` right after argument-count checks and before the
validation, the SSH-dir work and the registry update. `ValueError` becomes `click.UsageError`
as today. From there the SSH tail is `["get", "executor"]` or `["set", "executor", VALUE]`, the
JSON `key` field and `keys` map use `executor`, and text lines say `executor`, whichever
spelling the operator typed. The refused-set path stays free of side effects (no registry
write, no SSH), as in SPEC-0538.

**Help.** The `workers_config` docstring (the `--help` text) states the grammar and an
allowed-keys block generated from `ALLOWED`/`KEY_PATHS`, for example:

```
Allowed keys (the short name or the full path):
  executor  (agentic.workers.executor)   local | slurm
```

The module usage text at the top of `cli_workers.py` (`nw workers config NAME get [KEY]` lines)
gains the same note.

**Docs.** SPEC-0538 is not rewritten. This spec is the record of the dotted-path alias; the
docs that name the command (`docs/README.md` or the nw-orient skill, wherever
`nw workers config` is described) get one line saying both spellings work.

## Alternatives considered

- **Accept the dotted path on the worker side too** — widens the forced SSH grammar that
  SPEC-0538 froze for security; the coordinator already knows both spellings, so no host change
  is needed.
- **Accept any suffix of the path (`workers.executor`)** — ambiguous once a second key lands
  and invites typos to succeed silently; two exact spellings are enough.
- **Rename the key to the full path and drop the short name** — breaks existing scripts and
  SPEC-0538's documented grammar.
- **Only improve the error message** — still makes the path from the worker's own
  `config.yaml` fail; the operator should not have to translate it.

## Acceptance criteria

- [ ] `nw workers config NAME set agentic.workers.executor slurm` behaves exactly like
      `set executor slurm`: same registry write, same SSH tail (`config-set set executor slurm`),
      same output and exit code.
- [ ] `nw workers config NAME get agentic.workers.executor` behaves exactly like
      `get executor`, including with `--json`.
- [ ] Output (text and `--json` `key` / `keys`) uses the canonical short key `executor`
      whichever spelling was typed.
- [ ] Any other key (`workers.executor`, `AGENTIC.WORKERS.EXECUTOR`, ` executor`, `slots`,
      `agentic.workers`) exits 2 with a usage error that contains `is not allowed` and lists
      both accepted spellings; no registry write and no SSH happen.
- [ ] A bad value with the dotted key is refused as with the short key.
- [ ] `nw workers config --help` lists `executor`, `agentic.workers.executor`, and the values
      `local` and `slurm`.
- [ ] The key tables are single-sourced: `KEY_PATHS` and `ALLOWED` have identical keys, and the
      error and help text are derived from them.
- [ ] The worker-side grammar is unchanged: `nw worker config-set set agentic.workers.executor
      local` is still refused by `check_ssh_command` and by `nw worker config-set`.

## Test plan

- **Automated tests:** extend `tests/test_worker_host_config.py` (already carries its one
  module marker `pytestmark = pytest.mark.fast`; no second tier added) with, reusing the
  `coord_world` fixture and `CliRunner`:
  - `test_workers_config_set_dotted_path`: `set agentic.workers.executor slurm --json` records
    `executor: slurm`, sends the SSH tail `set executor slurm`, JSON `key == "executor"`; output
    equals the short-key run.
  - `test_workers_config_get_dotted_path`: `get agentic.workers.executor` and with `--json`
    match `get executor`.
  - `test_workers_config_rejects_other_spellings`: parametrized over `workers.executor`,
    `AGENTIC.WORKERS.EXECUTOR`, ` executor`, `slots`, `agentic.workers`, for `get` and `set`:
    exit 2, message contains `is not allowed` and both spellings, registry unchanged, no SSH
    call.
  - `test_workers_config_dotted_key_bad_value`: dotted key with value `docker` is refused.
  - `test_workers_config_help_lists_keys`: `--help` output contains `executor`,
    `agentic.workers.executor`, `local`, `slurm`.
  - `test_key_tables_agree`: `set(KEY_PATHS) == set(ALLOWED)` and `canonical_key` round-trips
    each path and short name.
  - `test_worker_side_grammar_stays_short`: `check_ssh_command` and
    `nw worker config-set set agentic.workers.executor local` refuse the dotted path.
  Run `python3 tools/test_tiers.py --check` (it exists) and require exit 0, since no new test
  module is added.
- **Manual verification:** `uv run nw workers config --help` shows the allowed-keys block;
  `uv run nw workers config <name> get agentic.workers.executor` against a real worker prints
  the same `executor live=... effective=...` line as `get executor` (one real call, per the
  real-binary rule).
- **Regression guard:** run the affected tests
  (`python3 tools/affected_tests.py --run uv run pytest -q`) plus
  `tests/test_worker_host_config.py` and `tests/test_worker_ssh_command.py`; all existing
  SPEC-0538 tests pass unchanged. `python3 tools/spec_lint.py` exits 0.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass (`python3 tools/affected_tests.py --run uv run
      pytest -q`); `python3 tools/test_tiers.py --check` exits 0; manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

None.
