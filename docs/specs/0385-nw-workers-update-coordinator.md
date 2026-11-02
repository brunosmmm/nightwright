---
id: SPEC-0385
title: "`nw workers update NAME`: coordinator side of the worker self-update"
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
kind: feature
tags: [agentic, remote, workers, ops]
depends_on: [SPEC-0369, SPEC-0354]
---

## Context

[SPEC-0369](./0369-worker-self-update-without-root-add-forced.md) shipped the worker verb
`nw worker update [--force]` and fixed its wire format, but left the coordinator command to a
follow-up. Without it a worker whose key is forced-command restricted (the real `box`) cannot be
upgraded: `sh -s`, scp and sftp are refused, and `nw workers bootstrap` needs them.

## Goals / Non-goals

**Goals**
- `nw workers update NAME [--force] [--dry-run]`: build a wheel of this checkout, stream it to the
  worker's `nw worker update` exactly per SPEC-0369's wire format over the registered ssh
  connection (record `ssh_argv`, `identity`, `nw`), report the answer, then run the `check`.

**Non-goals**
- `--all` (skipped: each worker's busy/force answer deserves its own decision).
- sudo, key changes, registry mutation, draining. A busy worker is reported, not forced.

## Decision

New module `workers_update.py` (wheel build reused from `workers_bootstrap.build_wheel`, which
`step_install` now also calls; build id from `buildid.compute_wheel`). The command sends one
header line plus the wheel bytes on stdin of `<ssh_argv> DEST NW worker update [--force]`.
`--force` is passed only when typed. Exit: 0 updated/unchanged (after a check whose build id
equals the wheel's), 5 busy, 1 failed (worker `failed`, no result line, check mismatch).

## Design

- `--dry-run` builds the wheel (its size is part of the plan), prints the command and sends nothing.
- busy: `worker has live jobs; drain it (nw workers drain NAME) or retry; --force installs without
  killing jobs`, plus the live job ids; failed: stage and error, with the `uv.log` hint for
  install/verify; no result line: the last stderr line, with an "older worker" hint on `not allowed`.
- After `updated`/`unchanged`, `workers.check` runs and its `info.build_id` must equal the sent
  wheel's build id.

## Alternatives considered

- **Duplicate the wheel build in the new module** -- rejected; one `build_wheel`.
- **Drain automatically** -- rejected: changes the queue's placement, not what the human asked for.

## Acceptance criteria

- [x] `nw workers update NAME` streams the SPEC-0369 header and wheel; `--force` only when typed.
- [x] busy -> exit 5 with the drain message; failed -> exit 1 with the uv.log hint; old worker
      without the verb -> exit 1 with a hint.
- [x] After success the check confirms the build id (mismatch -> exit 1).
- [x] `--dry-run` sends nothing and prints the wheel size.
- [x] Listed in the workers group help and docs/product/reference/index.md.
- [x] Real worker: an update that really installs was run on `box` and its check shows the new build.

## Test plan

- **Automated:** `tests/test_workers_update_cmd.py` (stub ssh checks framing/sha; updated,
  unchanged, `--force`, busy, failed, not allowed, check mismatch, dry-run, unknown worker);
  `tests/test_workers_bootstrap.py` and `tests/test_workers_registry.py` as regression for the
  shared `build_wheel` and the group.
- **Manual:** `nw workers update box --dry-run`, `nw workers check box`, `nw workers update box`,
  then `nw workers check box` (no stale warning) and `nw gate run --project Meta-Tools --cmd true`.

## As shipped

- Shipped as designed. `workers_bootstrap.build_wheel(out)` was extracted from `step_install`
  (behavior unchanged); `workers_update.py` holds the header/framing/result parsing; the click
  command is in `cli_workers.py`. SPEC-0369 was not edited (it is done; its follow-up pointer is
  left as is). Shell completion is click-generated, so no list needed editing.
- Verified with fakes (`tests/test_workers_update_cmd.py` plus the bootstrap/registry files, 124
  passed): framing and sha, updated/unchanged/busy/failed/not-allowed/mismatch, `--force`, dry-run.
- Verified for real on `box` (2026-11-02): `--dry-run` (wheel 830368 bytes, build b1:132bd656a),
  `nw workers check box` (stale warning b1:15ce1fa2d), and `nw workers update box` without
  `--force` -> **busy, exit 5** with the drain message and the two live job ids (box held 2/6).
- Success path verified for real on `box` (closed 2026-11-02): worker_updated events #3407 (b1:1719dcb0b) and #3492 (b1:deaa4a63b; list shows update_last updated 2026-11-02T17:34:22-05:00), one worker_update_failed #3428 (ENOSPC on the box disk at receive, since cleared). After the latest update `nw workers check box` -> ok and `check box --auth` -> ok; gate jobs (e.g. GATEJOB-0041) then passed on `box`.
- Incident: the first test draft passed `obj=cfg`, which the cli group ignores, so seven runs
  sent a bogus 150-byte test wheel to the real `box` (one with `--force`, while idle). The worker
  rejected each at `install` (uv: no .dist-info), the box stayed on b1:15ce1fa2d and `check box`
  was ok afterwards. Fixed by monkeypatching `nightwright.cli.load_config` and asserting the
  registry path is under tmp_path.


## Definition of done

- [x] Acceptance criteria all met (real success-path update verified on `box`, see As shipped).
- [x] Test plan automated part executed; the real busy path was run on `box`.
- [x] No regressions in the touched files (bootstrap, registry); the queue gate runs the rest.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated and `python3 tools/spec_lint.py` exits 0.
