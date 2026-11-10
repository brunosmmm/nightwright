---
id: SPEC-0506
title: "Files a remote gate is allowed to write (the regenerated ledger) come back to the worktree; the run is not repeated locally"
status: done
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
parent: SPEC-0501
tags: [agentic, gates, workers, ledger, performance]
depends_on: [SPEC-0338, SPEC-0359, SPEC-0368]
source_idea: IDEA-1082
---

## Context

nightwright's first gate is `spec-lint`: `python3 tools/spec_lint.py --write-ledger >/dev/null
&& python3 tools/spec_lint.py` (`.nw/project.yaml`). After a rebase the ledger is often stale, so
the worker's checkout gains a modified `docs/LEDGER.md`. The worker reports it in `done.changed`
and the coordinator's `_check_changed` (`remote_gates.py:541`) raises
`RemoteFailure("tree", ..., cooldown=False)`: **the whole gate run is repeated locally** on the
laptop. IDEA-1082: in the SPEC-0476 campaign on 2026-11-09 this sent the full `pytest-affected`
to the coordinator, which timed out after 2946 s. The research for SPEC-0501 counts 205 of 715
`pytest-affected` runs as full-suite-sized, the same order of cost as box overload.

The coordinator-side write is only needed for files a gate deliberately regenerates; a
formatter that rewrites source is a different case and must keep re-running locally.

## Goals / Non-goals

**Goals**
- A remote run whose only writes are declared, small, regenerated files is accepted as the run
  of record; those files are written into the coordinator's worktree.
- Undeclared writes keep today's local re-run (with reason `tree`, SPEC-0505).

**Non-goals**
- Committing on the agent's behalf (the implementer/close flow commits as today).
- Standalone gate jobs (SPEC-0381 already keeps `changed` without a re-run).

## Decision

A gate entry may declare `writes: [<glob>, ...]`. The worker returns the content of changed
files that match the declared globs of gates in the job (each <= 1 MiB, total <= 4 MiB,
zlib+base64 like gate logs, `remote_gates._inflate`). The coordinator applies them to the
worktree only if the worktree's tree is still the tree under test (SPEC-0359 `head`/`tree`);
then the remote run stands. Any changed path outside the globs, an oversize file, or a moved
worktree keeps today's `tree` fallback.

## Design

- `project.py`: allowed gate key `writes` (list of globs, matched with the same matcher as
  `paths`). nightwright's `spec-lint` gate gets `writes: ["docs/LEDGER.md"]`, and the yaml
  comment describing the local re-run is updated.
- Worker (`worker.py`, where `done` is built with `changed=tree_changes(tree)`, `:1178`): new
  field `written: {path: {"z": <b64 zlib>, "sha256": <hex>}}` for changed paths matching the
  union of the job's gates' `writes`; `changed` is unchanged (the full list).
- Coordinator (`remote_gates._check_changed` and `_Job` at `:1248`): `undeclared = changed -
  written.keys()`. If `undeclared` is empty and every `written` entry inflates and hashes
  correctly: when `git -C worktree rev-parse HEAD^{tree}` equals the run's `tree`, write the files
  (atomic replace), append a `note` "applied N file(s) regenerated on <worker>: docs/LEDGER.md",
  and return the run. Otherwise raise the existing `tree` failure.
- The header announces the capability (`payload.writes_ok: true`); an old worker that does not
  send `written` simply leads to today's fallback.

## Alternatives considered

- **Run `--write-ledger` on the coordinator before dispatch.** Makes the ledger correct but
  hard-codes one project's tool in the dispatcher; `writes` is generic (any project's
  regenerated artifacts).
- **Commit the regenerated ledger on the worker and fetch it.** The worker's checkout is
  throwaway and the coordinator never fetches from it; a content payload is simpler.

## Acceptance criteria

- [x] A remote run whose only change is `docs/LEDGER.md` (declared) is accepted remotely; the
      worktree's `docs/LEDGER.md` equals the worker's content; no local gate run starts.
- [x] A remote run that also changed an undeclared path falls back locally with reason `tree`.
- [x] A worktree whose tree moved during the run falls back locally (no write).
- [x] Oversize or hash-mismatched content falls back locally.
- [x] nightwright's `spec-lint` gate declares `writes: ["docs/LEDGER.md"]`.

## Test plan

- **Automated tests:** `tests/test_remote_gates_writes.py` with the existing fake worker:
  declared-only change applied; undeclared change -> `tree`; moved tree -> `tree`; oversize ->
  `tree`; bad hash -> `tree`; old worker (no `written`) -> `tree`. Config validation test for
  `writes`.
- **Manual verification (real binary):** on a throwaway branch with a stale ledger block
  (edit one spec's `status` without `--write-ledger`), run the campaign gate against the box;
  the ledger note "applied 1 file(s) regenerated on box" appears, the worktree's ledger is fresh,
  and the gate's `placement.where` is `remote`.
- **Regression guard:** affected tests of `remote_gates.py`, `worker.py`, `project.py`.

## Rollout / migration

Deploy the worker first (`nw workers update`), then the coordinator; either order is safe
because absent fields fall back. Supersedes nothing (refines SPEC-0338's `tree` rule).

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; manual step performed and noted.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Risks

- Writing into a worktree an agent is editing: guarded by the tree-equality check, which fails
  closed (fallback) on any change.

## Open questions

- None.

## As shipped

- `project.py`: gate key `writes` (non-empty list of globs, same matcher as `paths`);
  `.nw/project.yaml` `spec-lint` declares `writes: ["docs/LEDGER.md"]` (comment updated).
- Worker (`worker.py`): `written_files()` adds `written: {path: {z, sha256}}` (gzip+base64, the
  same encoding as gate logs) to `done` for changed paths matching a gate's `writes`, only when
  the header payload has `writes_ok`; nothing is returned (so the run falls back) if a file is
  missing, over 1 MiB, or the total is over 4 MiB. `changed` is unchanged.
- Coordinator (`remote_gates.py`): `_header` sets `payload.writes_ok` when any gate declares
  `writes`; `_writes_acceptor` is passed to `_check_changed` from both the synchronous
  `_dispatch` and the async `_Job._done`. It verifies, on the coordinator's own gate globs (the
  worker is not trusted): every changed path is declared and present in `written`, relative and
  outside `.git`, inflated within the caps, sha256 equal, and `tree_under_test(worktree)[1]`
  equals the run's tree (the commit under test's tree, so a dirty worktree also works; the spec
  said `HEAD^{tree}`). Then files are replaced atomically and a `note` "applied N file(s)
  regenerated on <worker>: <paths>" is appended. Anything else raises today's `tree` failure,
  whose message now ends with the reason in parentheses. Standalone gate jobs are unchanged.
- Tests: `tests/test_remote_gates_writes.py` (declared applied, undeclared, no `writes`, moved
  tree, oversize, bad hash, old worker, unsafe paths, async worker, header flag, worker helper,
  config validation, nightwright's own gate).
- Verification: the new and touched test files pass locally (`test_remote_gates_writes.py`,
  `test_remote_gates.py`); the loop-tier gate on the box (GATEJOB-1464, commit 260e12a7) passed.
  Manual step, as far as it can run before merge: the loopback tests drive the real
  `nw worker ssh-command` / `gate-job` process of this checkout through a real regenerating gate
  (declared file applied, note written, `placement.where` remote). A run against the real box
  with a stale ledger was not possible: `nw gate run` is a standalone job (SPEC-0381, keeps
  `changed`, never applies `written`), and the box's installed `nw` is the main checkout's, so it
  has no `written` support until this merges and `nw workers update` runs; until then it simply
  falls back to today's `tree` re-run (the designed old-worker path). Repeat the stale-ledger
  campaign gate after `nw workers update`.
