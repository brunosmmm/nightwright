---
id: SPEC-0569
title: "Spec-number collisions: a guard for numbers that bypass nw spec reserve, remote refs in the scan, and a re-reserve on collision"
status: proposed
owner: user
created: 2026-11-12
kind: feature
source_idea: IDEA-1074
tags: [specs, agentic, concurrency]
depends_on: [SPEC-0208]
---

## Context

[SPEC-0208](./0208-spec-number-allocator-reservations-across-branches.md) made
`nw spec reserve` (`spec_numbers.allocate`, `src/nightwright/spec_numbers.py`) hand out the
next free number after scanning every worktree, every local branch and the live reservations
under one flock. IDEA-1074 reports that this still does not prevent collisions: on 2026-11-09
SPEC-0490 and SPEC-0496 were each reserved for a campaign worktree branch and then also used
on `main` by another session; the campaign specs were renumbered to 0495 and 0498.

The allocator is only a fence for callers that ask it. Three gaps let a number be taken
behind its back:

1. **Hand numbering is the documented path.** `AGENTS.md` (step 2, "New work") says
   `cp docs/specs/TEMPLATE.md docs/specs/NNNN-slug.md`, and `docs/README.md` step 1 says
   "next free number". A session on `main` follows that with `ls docs/specs`, which sees
   neither a live reservation nor another branch's file. The reserved number (held by a
   campaign whose file is still uncommitted, or committed only on its own branch) is picked
   again, and because the two files have different slugs git merges them without conflict.
   Only `tools/spec_lint.py` check 11 (duplicate number prefix) notices, after the second
   file is already on `main`.
2. **Remote-tracking refs are not scanned.** `_branch_numbers` reads `refs/heads/` only, so a
   number that exists on `origin/main` (another machine or clone pushed it, not yet merged
   into the local `main`) is reported free.
3. **A taken number is only revisited when it is committed.** The queue's spec-write step
   calls `allocate` on every snapshot (`queue.py` ~2772), but `allocate` is idempotent on
   `campaign`: it returns the campaign's existing reservation unless `_prune` has dropped it,
   and `_prune` drops a reservation only when its number is *committed* on a branch. A
   number taken by an uncommitted file in another worktree, or by a differently named file
   that appears during the writer's run, leaves the reservation intact and nothing
   re-checks it before the spec is committed.

## Goals / Non-goals

**Goals**
- Any session or tool can ask "is the number in this spec file safe?" and get a deterministic
  answer that sees other worktrees, local and remote-tracking branches and live reservations.
- The documented way to start a spec uses the allocator; hand numbering from `ls` is no
  longer recommended anywhere in the contract docs.
- A campaign whose reserved number was taken elsewhere is moved to a fresh number before its
  spec is committed, instead of producing a duplicate.
- The reservation scan also covers remote-tracking refs.

**Non-goals**
- Blocking a commit with a git hook, or any change to the main checkout's git behavior.
- Changing the store format, the key scheme or `release` semantics of SPEC-0208.
- Renumbering existing specs (`nw spec renumber` stays deferred, IDEA-056).
- Outbound portable ids (`<PROJ>-NNNN`).
- Reading the reservation store from `tools/spec_lint.py`, which is stdlib/config-free; the
  new check lives in `nw`.

## Decision

Add one read-only check, `spec_numbers.collisions`, exposed as `nw spec check-numbers`, that
reports every spec file in a tree whose number is used by a *different* filename on another
worktree or ref, or is held by a live reservation of a campaign other than the tree's own.
Add a second read-only helper, `spec_numbers.number_taken`, for the moment before the
campaign's file exists. Run both from the queue's spec-write step, where a collision
re-reserves the campaign's number and retries on its own counter, without asking the writer
to change the text. Extend the allocator's branch scan with `refs/remotes/`. Change `AGENTS.md`
and `docs/README.md` so a new spec is numbered with `nw spec reserve` (or
`nw spec new --from-idea`) and checked with `nw spec check-numbers` before it is committed.

## Design

- **Scan (`spec_numbers.py`).**
  - `_branch_numbers(top, rel_dir)` also iterates `git for-each-ref --format=%(refname)
    refs/remotes/`, skipping symbolic `*/HEAD` refs. Both `allocate` and `_prune` use it, so
    a number on `origin/main` counts as used for allocation. `_prune` keeps its rule "drop a
    reservation once its number is committed on a branch"; remote refs count as branches here,
    which is safe because the number stays used through the scan.
  - A new helper `_number_files(top, rel_dir)` returns `{number: {filename: [location, …]}}`
    over every worktree working tree (location `worktree:<path>`) and every local and remote
    ref (location `<refname without refs/heads/ or refs/remotes/ prefix>`), using the same
    `_NUM_RE` basename match. `TEMPLATE*.md` and `SCHEMA.md` never match. A failing git
    command contributes nothing, as today.
- **`collisions(cfg, *, specs_dir=None, tree=None, campaign=None) -> list[dict]`.**
  - `tree` defaults to the repository top-level of `specs_dir` (the checkout the command runs
    in); the spec files checked are the `NNNN-*.md` files in `tree/<rel_dir>`.
  - `campaign` defaults to the base id of the tree's branch when that branch is `nw/<id>`
    (split-part suffix `#k` stripped), else `None`. The queue passes it explicitly.
  - A **filename collision** is a number in the tree whose filename set across
    `_number_files`, excluding the tree's own worktree location, contains a filename different
    from the tree's file. The same filename on other refs is normal (a merged or shared spec)
    and never reported.
  - A **reservation collision** is a number in the tree that has a live reservation (same key,
    after pruning) whose `campaign` is set and differs from `campaign`; both sides are
    compared with any `#k` split suffix stripped. Reservations with no
    campaign (made by `nw spec reserve`) never collide: their holder is the human or session
    that is about to create the file.
  - Each result is `{number, id, file, kind: "filename" | "reservation", other: [locations or
    the campaign id]}`; sorted by number. It does not write the store (it prunes in memory
    only), so it is safe to run anywhere.
- **CLI:** `nw spec check-numbers [--specs-dir PATH] [--json]` in the `spec` group of
  `cli.py`. Default output is one line per collision (`SPEC-0490: docs/specs/0490-a.md also
  exists as 0490-b.md on main` or `... is reserved by campaign <id>`), then the remedy
  `renumber one of them: nw spec reserve`. Exit 0 with `no spec-number collisions` when none,
  exit 1 otherwise. `--json` prints `{"schema": "nw.spec.collisions.v1", "collisions":
  [...]}`.
- **`number_taken(cfg, number, *, specs_dir=None, exclude_campaign=None) -> list[str]`.**
  Read-only; built on `_number_files`, so it needs no file in the checked tree. It returns the
  locations (`worktree:<path>` or ref names) where *any* filename carries `number`, ignoring
  the location of the tree resolved from `specs_dir` (the campaign's own worktree, so a
  leftover file of an earlier attempt does not count) and the campaign's own ref `nw/<id>`
  (base id, `#k` stripped, when `exclude_campaign` is given). It does not write the store and
  does not look at reservations. `[]` means the number is free of files elsewhere.
- **Queue (`agentic/spec_write.py`, `agentic/queue.py`).** Two call sites, one re-reserve
  helper.
  - *Re-reserve helper* (in `queue.py`, beside the snapshot code): `SN.release(cfg, number,
    specs_dir=…)`, catching `ValueError` ("is not reserved"); then `SN.allocate(cfg,
    by="queue", campaign=state["id"], specs_dir=…)`. The `ValueError` is expected, not an
    error: `release` and `allocate` both call `_prune` first, which already drops a
    reservation whose number is committed on any local or remote-tracking branch (for example
    committed on `main` or `origin/main`), so for that kind of collision `release` finds
    nothing and `allocate` alone already returns a fresh number. The release matters for the
    other kind, a number taken by an uncommitted file in another worktree or by a differently
    named file in the campaign's own run, where the reservation is still live and `allocate`
    would otherwise return it again. After the release, `allocate` sees the taken number as
    used (worktree and branch scan) and returns a higher one.
  - *Snapshot time* (`queue.py` ~2772, mode `write` only): after `SN.allocate`, call
    `SN.number_taken(cfg, rec["number"], specs_dir=…, exclude_campaign=state["id"])`. When it
    returns locations, run the re-reserve helper, repeat the check on the new number (at most
    `SPEC_NUMBER_RETRY_MAX` times in this loop, counted on the collision counter below) and
    build the snapshot (`number`, `path`) from the final reservation, so the first writer run
    already uses it and no writer run is spent. The filename-aware `collisions` rule cannot
    be used here, because the campaign's file does not exist yet.
  - *After the writer* (`queue.py` ~2956, in the write-result step, after
    `SW.validate_spec_change` and before regenerate/commit): the caller computes
    `SN.collisions(cfg, specs_dir=…, tree=worktree, campaign=state["id"])` and keeps the
    filename collision whose number is `sw["number"]`. `validate_spec_change` stays pure and
    gains no `cfg`. A collision is a **distinct result, not a text problem**: it is stored
    as `w["collision"] = {"number", "file", "other"}` next to `w["problems"]`, and its message
    (`SPEC-NNNN is already used by <file> on <ref>`) is never appended to `w["problems"]`. The
    rejected write is rolled back with the same `roll_back()` as a text problem.
  - *Handling* (`queue.py` ~4858, before the `w["problems"]` branch): when `w["collision"]`
    is set, the queue does not copy anything into `brief["write_problems"]` (so the writer
    brief never mentions the collision and any other text problems found on the same attempt
    are dropped, since the file is rewritten under a new number and validated afresh), runs
    the re-reserve helper, increments the **own** counter `counters["spec_number_retries"]`,
    and returns to `SPEC_WRITING` with reason `spec_number_retry`; the next snapshot builds
    the new `number`/`path`. It does not touch `spec_write_retries` (`SPEC_WRITE_RETRY_MAX =
    1`), so a collision never uses up the single text retry. `SPEC_NUMBER_RETRY_MAX = 2`
    bounds the loop; when `spec_number_retries` exceeds it the campaign parks
    `spec_write_failed` with `park.detail` = `SPEC-NNNN is already used by <file> on <ref>`
    (the last collision) and `resume_to: SPEC_WRITING`. Both counters reset to 0 where
    `spec_write_retries` is reset today (a committed spec, a split part, a manual resume at
    `queue.py` ~11296).
- **Docs.** `AGENTS.md` ("New work") and `docs/README.md` (step 1) say: get the number from
  `nw spec reserve` (or `nw spec new --from-idea`), never from `ls`; copy `TEMPLATE.md` to
  that number; run `nw spec check-numbers` before committing. No other contract text changes.

## Alternatives considered

- **A git `pre-commit`/`reference-transaction` hook** — would stop a hand-numbered commit
  outright, but hooks are per-clone, need installing on every checkout and worker box, and
  the repository already routes main-checkout protection through the SPEC-0544 agent hook.
  The check command plus the queue guard covers the observed path without new install state.
- **Placeholder files committed at reservation time** — the manual practice SPEC-0208
  replaced; pollutes lint and needs cleanup on abandon.
- **Teach `tools/spec_lint.py` about reservations** — the linter is config-free and cannot
  locate `<data_dir>/spec-reservations.json`; check 11 already catches duplicates inside one
  tree, which is too late.
- **Only fix the docs** — leaves the remote-ref and mid-run cases unprotected and gives no
  machine check.

## Acceptance criteria

- [ ] `AGENTS.md` and `docs/README.md` no longer tell a session to pick "the next free
      number" by hand; both name `nw spec reserve` / `nw spec new --from-idea` and
      `nw spec check-numbers`.
- [ ] `allocate` returns a number above one that exists only on a remote-tracking ref
      (`refs/remotes/origin/main`), and ignores a symbolic `origin/HEAD`.
- [ ] `collisions` reports a filename collision when `0490-a.md` is in the checked tree and
      `0490-b.md` exists on another local branch, on a remote-tracking ref, or in another
      worktree's working tree; it reports nothing when the same filename `0490-a.md` exists
      on the other ref.
- [ ] `collisions` reports a reservation collision for a number held by a live reservation of
      campaign `C1` when the checked tree is `nw/C2` (or `campaign="C2"`); nothing when the
      tree is `nw/C1`, when the branch is `nw/C1#2`'s base `C1`, or when the reservation has
      no campaign.
- [ ] A pruned reservation (number committed on a branch) is not reported as a reservation
      collision, and `collisions` leaves `spec-reservations.json` byte-identical.
- [ ] `nw spec check-numbers` exits 0 with `no spec-number collisions` on a clean repo and
      exits 1 naming each colliding number and the other file/ref/campaign otherwise;
      `--json` carries schema `nw.spec.collisions.v1`; `--specs-dir` targets another
      repository.
- [ ] `number_taken` returns the locations of a differently named file with that number on
      another local branch, remote-tracking ref or worktree, when the checked tree holds no
      file with that number; it returns `[]` for a leftover file in the tree's own worktree
      and for the campaign's own `nw/<id>` ref, and leaves `spec-reservations.json`
      byte-identical.
- [ ] A queue spec-write whose reserved number is taken before the writer starts uses a new
      number for the first writer run, with the old reservation gone and the new one live,
      both when the colliding file is committed on `main` (the reservation is already pruned:
      the queue tolerates the `ValueError` from `SN.release` and does not crash) and when it
      is uncommitted in another worktree (the reservation is still live and is released).
- [ ] A number taken during the writer's run is reported as `w["collision"]`, not in
      `w["problems"]`: the queue rolls the write back, re-reserves, returns to `SPEC_WRITING`
      with a new `number`/`path`, `brief["write_problems"]` does not contain the collision
      (nor other text problems found on that attempt), and `spec_write_retries` is unchanged;
      a text problem on a later attempt still gets its one retry.
- [ ] The collision retry has its own counter `spec_number_retries` bounded by
      `SPEC_NUMBER_RETRY_MAX = 2`; when it is exceeded the campaign parks `spec_write_failed`
      (not `FAILED`) with the collision (`SPEC-NNNN is already used by <file> on <ref>`) in
      `park.detail`.
- [ ] Existing SPEC-0208 behavior is unchanged: its acceptance tests still pass.

## Test plan

- **Automated tests:** extend `tests/test_spec_numbers.py` (a scratch git repo with
  `git worktree add`, a bare "origin" added as a remote and fetched, a temp
  `cfg["data_dir"]`, the existing helpers) with one test per criterion:
  - remote-ref scan: commit `0490` on a clone's `origin/main` only; `allocate` returns 491 or
    higher; `refs/remotes/origin/HEAD` present and ignored;
  - `collisions` filename cases (other local branch, remote-tracking ref, other worktree,
    same filename ⇒ empty);
  - `collisions` reservation cases (`campaign` explicit, `nw/C1` branch detection, `#k`
    suffix, no-campaign reservation, committed ⇒ pruned);
  - read-only: the store bytes are equal before and after;
  - CLI via CliRunner: clean repo exit 0; colliding repo exit 1, text and `--json`;
    `--specs-dir`.
  - `number_taken`: other branch, remote-tracking ref, other worktree ⇒ locations; own
    worktree leftover and own `nw/<id>` ref ⇒ `[]`; store bytes unchanged.
  Extend `tests/test_agentic_spec_writer.py` (queue spec-write tests), one fixture each:
  - **committed collision before start:** another branch (`main`) commits `<n>-other.md`
    after the reservation, so `_prune` already dropped it; the snapshot's `SN.release` raises
    `ValueError`, the queue swallows it, and the first writer run uses a new number;
  - **uncommitted collision before start:** `<n>-other.md` sits uncommitted in another
    worktree; the reservation is live, `number_taken` reports it, release + allocate gives a
    new number, old reservation gone and new one live;
  - **collision during the run:** the writer fake's run creates `<n>-other.md` in another
    worktree; the result carries `collision` and no collision text in `problems`, the write
    is rolled back, `spec_write_retries` stays 0, `brief["write_problems"]` has no collision
    text, `spec_number_retries` is 1 and the second attempt uses the new number/path;
  - **collision plus a text problem on one attempt:** only the re-reserve happens, the text
    problem is not in the brief, and a later text problem still gets its one retry;
  - **budget exhausted:** collisions on every attempt park `spec_write_failed` after
    `SPEC_NUMBER_RETRY_MAX` re-reserves, with the collision named in `park.detail`. Both modules already carry their single module-level speed-tier marker; no new
  `tests/test_*.py` is introduced, so no new `pytestmark` is added. When `tools/test_tiers.py`
  exists, `python3 tools/test_tiers.py --check` exits 0.
  - Docs guard: a test in `tests/test_spec_numbers.py` asserts `AGENTS.md` and
    `docs/README.md` contain `nw spec reserve` and `nw spec check-numbers` and do not contain
    the phrase `(next free number)`.
- **Manual verification:** in a scratch repo, `nw spec reserve` on branch `b1`; on `main`
  `cp docs/specs/TEMPLATE.md docs/specs/<that number>-x.md` (committed on `b1` as `-y.md`);
  `nw spec check-numbers` on `main` exits 1 naming `b1`.
- **Regression guard:** `uv run pytest tests/test_spec_numbers.py
  tests/test_agentic_spec_writer.py` plus
  `python3 tools/affected_tests.py --run uv run pytest -q`; `python3 tools/spec_lint.py`
  exits 0 with the ledger regenerated.

## Rollout / migration

1. Land the scan, `collisions`, the CLI command and the queue guard with their tests.
2. Update `AGENTS.md` and `docs/README.md`.
3. No data migration: the reservation store format is unchanged; existing reservations keep
   working. Verify with `nw spec reservations` and `nw spec check-numbers` on the main
   checkout.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected automated tests pass; manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (status + date).

## Open questions

- None.
