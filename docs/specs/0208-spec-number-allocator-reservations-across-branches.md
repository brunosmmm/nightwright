---
id: SPEC-0208
title: "Spec-number allocator: reservations that see every branch and worktree"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [specs, agentic, concurrency]
source_idea: IDEA-052
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md),
promoted from bug `IDEA-052`. It is the first of four specs that put spec writing into the
queue (SPEC-0208 → 0209 → 0210 → 0211, the user's 2026-10-25 decision: "unwritten specs are
part of the queue").

`specs.next_spec_number(specs_dir)` (`src/wt/specs.py`) returns max(`NNNN-*.md` in one
directory) + 1, so it sees only the local checkout. On 2026-10-25 it would have handed out
SPEC-0186 and SPEC-0190 while other branches already held them. Since then the steward and
the conductor have reserved every number by hand over the mailbox, and used placeholder
files to make `wt spec new` skip ahead. Headless spec writers (SPEC-0209) running in parallel
campaign worktrees cannot do that; they would collide on every number.

## Goals / Non-goals

**Goals**
- One allocator hands out the next free number for a spec directory. It considers the
  working tree, every local branch, and outstanding reservations, under a lock, so two
  concurrent callers never get the same number.
- Reservations are visible and releasable from the CLI. `wt spec new` uses the allocator.

**Non-goals**
- Outbound portable ids (`<PROJ>-NNNN`; they have their own outbox numbering).
- Renumbering existing specs (IDEA-056).
- Remote branches (wt works on local branches and worktrees).

## Decision

Add `src/wt/spec_numbers.py`. It keeps a reservation store at
`<data_dir>/spec-reservations.json` under an flock, and computes the next number by scanning
the working tree, `git ls-tree` of every local branch, and live reservations.
`scaffold_from_idea` and the new CLI commands call it.

## Design

- **Key:** a spec directory is identified by `(repo_root, rel_dir, prefix)`. The defaults are
  the repo containing `cfg["specs_dir"]`, `docs/specs` and `SPEC`. The store keys entries by
  `f"{repo_root}::{rel_dir}::{prefix}"`.
- **Used numbers:** the union of
  1. `NNNN` from `^(\d{4})-.*\.md$` names in `<repo_root>/<rel_dir>` (the checkout);
  2. for each `git for-each-ref --format=%(refname) refs/heads/`, the names from
     `git ls-tree --name-only <ref> -- <rel_dir>/` with the same pattern (campaign branches,
     steward branches, `main`);
  3. live reservations for the key.
  `TEMPLATE*.md` and `SCHEMA.md` never match. Git errors (not a repo) are treated as no
  branches.
- **Reservation record:** `{number, id: "SPEC-NNNN", key, by, for, campaign, at}`.
- **`allocate(cfg, *, by, note=None, campaign=None, key=None) -> dict`:**
  - Under `flock(<data_dir>/spec-reservations.lock)`, it returns an existing live reservation
    with the same `campaign` (idempotent for a campaign that asks twice).
  - Otherwise it records `max(used) + 1` (or 1) and returns it.
  - It writes the store atomically.
- **Pruning:** inside every `allocate` and `list_reservations` call, a reservation is
  released when either
  - its number now exists in the checkout or on the base branch (`main`), meaning it landed;
    or
  - its `campaign` is ABANDONED (`store.read_state`).
  Unknown campaigns are kept.
- **`release(cfg, number, key=None)`** deletes one reservation; an unknown number raises
  ValueError.
- **CLI:**
  - `wt spec reserve [--for TEXT] [--json]` prints `SPEC-NNNN` (or
    `{schema: wt.spec.reservation.v1, …}`); `by` is `human`, or `$WT_MAILBOX_ADDR` when set.
  - `wt spec reservations [--json]` is a table of live reservations (id, for, by, campaign,
    age) or `{schema: wt.spec.reservations.v1, reservations: [...]}`.
  - `wt spec release NNNN|SPEC-NNNN` (dry-run by default, `--yes` to apply, as wt does
    elsewhere).
- **`wt spec new` / `scaffold_from_idea`:** the spec number comes from `allocate(by="spec new",
  note=<idea id>)` instead of `next_spec_number`. `next_spec_number` stays as a pure helper
  for tests and lint.

## Alternatives considered

- **Only scan branches, no reservations** — two writers that have not committed yet still
  collide.
- **Reservations only, no scan** — misses numbers taken by hand on branches (today's
  practice).
- **A counter that never reuses** — fine, but a scan is needed anyway to start from the real
  maximum; released numbers are rare.

## Acceptance criteria

- [ ] In a scratch repo with `0001` on `main`, `0002` committed only on branch `b1` and `0003`
      only in an uncommitted working-tree file, `allocate` returns 4; a second call (another
      `by`) returns 5.
- [ ] Two processes calling `allocate` at the same time get different numbers (8 concurrent
      calls → 8 distinct numbers).
- [ ] Calling `allocate` twice with the same `campaign` returns the same number.
- [ ] A reservation is pruned once its file exists on `main`, or once its campaign is
      ABANDONED; `release` removes one, and an unknown number errors.
- [ ] `wt spec reserve` / `wt spec reservations` / `wt spec release` work in human and
      `--json` forms; `release` is dry-run without `--yes`.
- [ ] `wt spec new --from-idea` in a repo whose branch `b1` holds the next local number picks
      the number after it.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_spec_numbers.py`, on a scratch git repo with a temp
  `data_dir`, one test per AC. The concurrency test uses `multiprocessing` (fork) with 8
  workers. The pruning test commits the reserved file to `main`, and marks a fake campaign
  ABANDONED via `store`. The CLI tests use CliRunner. The `spec new` test uses the
  `test_assoc` cfg helpers with a git-initialised specs repo.
- **Regression guard:** full `uv run pytest` (existing `spec new` tests keep passing; they
  have no branches).

## Rollout / migration

Ships through the agentic queue. The store is created on first use. The mailbox hand
reservations stop once this lands: the conductor and steward use `wt spec reserve`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
