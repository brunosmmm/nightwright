---
id: SPEC-0208
title: "Spec-number allocator: reservations that see every branch and worktree"
status: done
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
SPEC-0186 and SPEC-0190 while other branches held them. Sessions then reserved numbers by
hand over the mailbox and used placeholder files to skip ahead. Headless spec writers
(SPEC-0209) running in parallel campaign worktrees cannot do that.

Revised 2026-10-25 after `wt spec review` (revise, 8 findings):
- the store key is the repository's shared git dir, not a worktree root;
- every worktree's working tree is scanned;
- pruning happens only once a number is committed on a branch;
- the scan, CLI and lock details are pinned;
- the idea-link (`:SPEC:`) and renumber parts of IDEA-052 are explicitly deferred.

Round 2 (revise, 5 findings) pins:
- how a spec directory maps to a key (including non-git directories);
- the store's schema and corruption handling;
- the scope of `reservations` and `release`;
- when pruning is observed;
- the failure cleanup of `spec new`.

## Goals / Non-goals

**Goals**
- One allocator hands out the next free number for a repository's spec directory. It
  considers every worktree's working tree, every local branch and outstanding reservations,
  under a cross-process lock, so concurrent callers in any worktree of the same repository
  never get the same number.
- Reservations are visible and releasable from the CLI. `wt spec new` uses the allocator.

**Non-goals**
- Outbound portable ids (`<PROJ>-NNNN`), which have their own outbox numbering.
- Remote branches; wt works on local branches and worktrees.
- **Deferred to IDEA-056** (stated here because IDEA-052's log asks for them):
  - `wt spec renumber`;
  - keeping an idea's `:SPEC:` link right after a rename.
  The allocator removes the cause of most renames; SPEC-0209 sets `:SPEC:` from the
  allocated id.

## Decision

Add `src/wt/spec_numbers.py` with `allocate`, `list_reservations` and `release`, backed by
`<data_dir>/spec-reservations.json` under an flock on `<data_dir>/spec-reservations.lock`. Add
the CLI commands `wt spec reserve`, `wt spec reservations` and `wt spec release`.
`specs.scaffold_from_idea` calls `allocate`.

## Design

- **Spec directory → key.** Every call takes a `specs_dir`, an absolute path. It defaults to
  `specs._specs_dir(cfg)`, the internal specs directory; campaigns pass
  `<worktree>/docs/specs`.
  - **When `specs_dir` is inside a git repository:**
    - `top = git -C <specs_dir> rev-parse --show-toplevel`;
    - `rel_dir = relpath(specs_dir, top)`;
    - `repo_id = realpath(git -C <specs_dir> rev-parse --path-format=absolute
      --git-common-dir)`, which is the same for the main checkout and every worktree;
    - key = `f"{repo_id}::{rel_dir}"`.
  - **When `specs_dir` doesn't exist yet**, the git commands run from its nearest existing
    ancestor, so a new repository keeps the same key once the directory is created.
  - **Otherwise** (not inside a git repository, e.g. test configs with a bare temp
    `specs_dir`): key = `realpath(specs_dir)`, and the only scan is that directory's listing.
    With no branch scan, such reservations are never pruned by commits; they last until
    `release`, which is fine for tests and scratch use.
  - A bare repository's `bare` worktree record is skipped naturally, since it has no
    `<rel_dir>` directory.
  - The id prefix is always `SPEC`.
- **Used numbers** are the union of the following. The basename pattern is the existing
  `specs._NUM_RE` (`^(\d{4})-`) applied to `*.md` basenames, and `TEMPLATE*.md` /
  `SCHEMA.md` never match.
  1. **Every worktree's working tree:** for each `worktree <path>` record in
     `git -C <top> worktree list --porcelain`, the `NNNN-*.md` files in `<path>/<rel_dir>`
     (committed or not). This includes detached-HEAD worktrees. Records marked `prunable`,
     or whose path no longer exists, are skipped.
  2. **Every local branch:** for each ref from `git -C <top> for-each-ref
     --format=%(refname) refs/heads/`, the basenames from `git -C <top> ls-tree
     --name-only <ref> -- <rel_dir>/`.
  3. **Live reservations** for the key.
  Any git command that fails contributes nothing.
- **Store:** `<data_dir>/spec-reservations.json` =
  `{"schema": "wt.spec.reservations.store.v1", "reservations": [record, …]}`, a flat list in
  which each record carries its `key`. A missing or empty file is an empty store. A file
  that is not valid JSON, or has the wrong schema, raises ValueError naming the file and the
  fix (inspect it, then delete it). It is never silently reset, which could hand out a
  number twice.
- **Reservation record:** `{number, id: "SPEC-NNNN", key, by, note, campaign, at}`. The CLI's
  `--for` sets `note`.
- **`allocate(cfg, *, by, note=None, campaign=None, specs_dir=None) -> dict`.**
  Under the lock:
  1. prune (below);
  2. if `campaign` is given and a live reservation for that key and campaign exists, return
     it (idempotent);
  3. otherwise reserve `max(used) + 1` (1 when empty), write the store atomically (temp file +
     `os.replace`) and return the record.
  The lock is a plain `fcntl.flock` on the lock file, which is cross-process (tests run 8
  processes). `store._process_lock` is not reused, because it guards campaign dirs.
- **Pruning** (inside `allocate` and `list_reservations`). A reservation is released when
  - its number appears in the scan of **local branches** (scan 2), meaning it was committed
    on some branch: the branch scan from then on keeps it used, and an uncommitted file in a
    worktree never prunes;
  - or its `campaign` reads as ABANDONED through `agentic.store.read_state(cfg, campaign)`.
    A ValueError from `read_state` (an unknown campaign) means keep it.
  `spec_numbers` imports `agentic.store` lazily, inside the pruning function, so plain
  `wt spec` use does not load the agentic package. A campaign whose number was committed and pruned and that calls
  `allocate` again gets a *new* number, which is intended: by then its spec file names its
  number.
- **Locking:** `allocate`, `list_reservations` (which prunes, and so may rewrite the store) and
  `release` each hold the same flock for their whole read-modify-write cycle.
- **`list_reservations(cfg, *, specs_dir=None, all_keys=False) -> list[dict]`** prunes, then
  returns the records for the key (or every record with `all_keys`), ordered by number.
- **`release(cfg, number, *, specs_dir=None)`** deletes the reservation for `number` (int or
  `SPEC-NNNN`) under the key. A number not reserved under that key raises ValueError.
- **Store location:** `cfg["data_dir"]` (never `paths.data_dir()`), so test configs with a
  temp `data_dir` stay isolated.
- **CLI** (in `cli.py`'s `spec` group):
  - `wt spec reserve [--for TEXT] [--json]` prints `SPEC-NNNN`, or
    `{"schema": "wt.spec.reservation.v1", **record}`. `by` is `$WT_MAILBOX_ADDR` when set,
    else `human`. The CLI never sets `campaign`; only the Python API (SPEC-0209) does.
  - `wt spec reservations [--all] [--json]` prints a table with columns id, note, by, campaign
    and age, for the internal specs directory's key (every key with `--all`). `--json`
    prints `{"schema": "wt.spec.reservations.v1", "reservations": [records]}`.
  - `wt spec release NNNN|SPEC-NNNN [--yes]` acts on the internal specs directory's key and is
    a dry run without `--yes`.
  - All three accept `--specs-dir PATH` to target another repository's spec directory.
- **`wt spec new` / `scaffold_from_idea`:** the number comes from `allocate(cfg, by="spec new",
  note=<idea id>)`. Everything after the allocation runs in `try/except`: the file write, the
  idea's `set_property`, the state change. Any exception releases the number and re-raises.
  An already-written file and `:SPEC:` property are not rolled back; numbering stays safe,
  because the worktree scan still sees the file. If `release` itself fails, the original
  exception is re-raised, and the stale reservation is pruned once the file is committed.
  `next_spec_number` stays as a pure helper.

## Alternatives considered

- **Only scan branches, no reservations** — two writers that have not committed yet still
  collide.
- **Reservations only, no scan** — misses numbers already taken on branches.
- **Key on the worktree root** — each worktree would have a private registry, which defeats
  the purpose (the first review's finding).

## Acceptance criteria

- [x] In a scratch repo, `allocate` returns 5 when all of these hold: `0001` on `main`; `0002`
      only on branch `b1`; `0003` uncommitted in the main checkout; `0004` uncommitted in a
      second worktree `w2`. A second call with `specs_dir=<w2>/docs/specs` returns 6, because
      the key is shared and the first call's reservation for 5 is live.
- [x] Eight processes calling `allocate` concurrently get eight distinct numbers.
- [x] Two calls with the same `campaign` return the same number.
- [x] An uncommitted file for a reserved number does not prune the reservation. After the
      file is committed on any branch, the next `allocate` or `list_reservations` call no
      longer lists it. Likewise after its campaign is marked ABANDONED. `release` removes one,
      and an unknown number errors.
- [x] A corrupt store file makes `allocate` raise, naming the file; a missing or empty one
      works.
- [x] `release` and `list_reservations` run concurrently with `allocate` lose no
      reservation: four processes, each looping `allocate` and `list_reservations`, plus one
      `release`, end with exactly the expected records.
- [x] Outside a git repository, `allocate` works from the directory listing alone.
      `TEMPLATE.md` and `SCHEMA.md` never count.
- [x] CLI: `reserve` (human and `--json`, `by` taken from `WT_MAILBOX_ADDR`); `reservations`
      (table columns, `--json` schema, `--all` across two keys); `release` (dry-run without
      `--yes`, applied with it); `--specs-dir` for a second repository.
- [x] `wt spec new --from-idea` in a repo whose branch `b1` holds the next local number picks
      the number after it. A failure at the file write, or at the idea's `set_property`, after
      allocation leaves no reservation behind.
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_spec_numbers.py`, on a scratch git repo with
  `git worktree add` for `w2`, a temp `cfg["data_dir"]`, and one test per AC.
  - The concurrency test uses `multiprocessing` with the fork context and 8 workers, then
    asserts distinct numbers.
  - Pruning commits the reserved file on a branch, and writes a fake ABANDONED campaign state
    through `store`.
  - The non-repo case uses a plain temp directory.
  - The CLI tests use CliRunner with `WT_MAILBOX_ADDR` set and unset.
  - `spec new` uses the `test_promote` cfg helpers, with the specs directory inside a git
    repo that has branch `b1`. The failure cases monkeypatch the file write and
    `org_write.set_property` in turn to raise after allocation.
  - The existing `spec new` tests keep their bare temp `specs_dir`, which takes the non-git
    path.
- **Regression guard:** full `uv run pytest`. Existing scaffold tests use temp `data_dir`s
  through their cfg helpers, so the store never touches the real data dir.

### Test record (2026-10-25)

- `uv run pytest tests/test_spec_numbers.py tests/test_promote.py` — 30 passed (one test per
  AC: scratch repo with `b1` + detached worktree `w2`, 8-process allocate, campaign
  idempotence, prune on commit / ABANDONED / release, corrupt/missing/empty store, 4 hammer
  processes + 1 release, non-git directory, CLI via CliRunner, `spec new` branch number and
  both failure points).
- Full `uv run pytest`: 1866 passed, 1 skipped, 1 failed (`test_rich_output_guard`, the new
  `spec reserve` / `reservations` stdout emitters were not on its allowlist); added them as
  machine paths and re-ran that file plus `test_spec_numbers.py` — 16 passed.
- `python3 tools/spec_lint.py --write-ledger` then `python3 tools/spec_lint.py` — exit 0.

### Test record (2026-10-25, review loop 1)

- Review follow-up: `tests/test_spec_numbers.py` now pins each scan source as the unique
  maximum (branch only, main-checkout working tree only, linked-worktree working tree only,
  reservation only), a prunable (deleted) worktree, per-key `release`, malformed release
  arguments (API and CLI), `by == "human"` when `WT_MAILBOX_ADDR` is unset, exact table
  header columns, prune-on-commit on a branch checked out nowhere (via the store contents
  after `allocate`), and `--all` pruning ABANDONED campaigns of other keys.
- `uv run pytest tests/test_spec_numbers.py` — 18 passed; mutation checks (skipping the first
  worktree record, dropping the key filter in `release`) each fail a test.

### Test record (2026-10-25, review loop 2)

- Review follow-up in `tests/test_spec_numbers.py`: commit pruning is limited to the caller's
  key (repo2's reservation survives repo's commit of the same number); a corrupt or
  wrong-schema store file is left byte-identical after the raise; `wt spec release
  --specs-dir` (dry run and `--yes`); `spec new` re-raises the original error when `release`
  itself fails (the stale reservation stays).
- `uv run pytest tests/test_spec_numbers.py` — 21 passed; removing the `r["key"] == key`
  guard in `_prune` fails `test_commit_prunes_only_the_callers_key`.
- `python3 tools/spec_lint.py --write-ledger` then `python3 tools/spec_lint.py` — exit 0.

### As shipped

- `list_reservations(all_keys=True)` prunes by campaign state across all keys but by branch
  commits only for the caller's key (other keys have no spec directory to scan).
- `wt spec release` without `--yes` validates the reservation exists (after pruning) and
  prints what it would release.
- `wt spec reserve` and `reservations` also accept `--specs-dir` (the spec says all three).

## Rollout / migration

Ships through the agentic queue. When it merges, `IDEA-052` gets a log line saying the
renumber and `:SPEC:` repair parts moved to `IDEA-056`, so closing 052 leaves nothing
untracked. The store is created on first use. Once this lands, sessions
use `wt spec reserve` instead of mailbox reservations.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
