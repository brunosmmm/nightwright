---
id: SPEC-0506
title: "Files a remote gate is allowed to write (the regenerated ledger) come back to the worktree; the run is not repeated locally"
status: in-progress
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

- [ ] A remote run whose only change is `docs/LEDGER.md` (declared) is accepted remotely; the
      worktree's `docs/LEDGER.md` equals the worker's content; no local gate run starts.
- [ ] A remote run that also changed an undeclared path falls back locally with reason `tree`.
- [ ] A worktree whose tree moved during the run falls back locally (no write).
- [ ] Oversize or hash-mismatched content falls back locally.
- [ ] nightwright's `spec-lint` gate declares `writes: ["docs/LEDGER.md"]`.

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

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual step performed and noted.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Risks

- Writing into a worktree an agent is editing: guarded by the tree-equality check, which fails
  closed (fallback) on any change.

## Open questions

- None.
