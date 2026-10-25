---
id: SPEC-0190
title: "Concurrency-safe org writes: identity-checked headlines and one write lock"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M4: cli ergonomics"
kind: feature
tags: [org, concurrency, ideas]
source_idea: IDEA-054
---

## Context

Promoted from bug `IDEA-054` (Idea writes target a line number resolved before the write; a
concurrent archive shifts lines and the write fails or lands on the wrong headline).

The user runs several sessions (wt, comfort-assistant, a conductor and its sub-agents)
against the same ideas file. Every org writer in `org_write`, `explore` and `idea_ext` worked
the same way. It parsed the file, kept `Task.line`, later re-read the file and edited
`lines[task.line - 1]`, then atomically replaced the file. Two holes:

1. **Wrong headline.** The only guard was "the headline at that line is in the expected
   state". On 2026-10-25 a concurrent archive of IDEA-051 shifted every idea below it, and
   `wt idea kind IDEA-052 …` failed with `line 441 is not a headline`. That time the failure
   was harmless; had another `IDEA` headline moved onto the stale line, the write would have
   silently edited it. Memory `wt-shared-ideas-file-guard` records exactly that
   (IDEA-003 overwritten by another session's write).
2. **Lost updates and duplicate ids.** No lock spanned the read-modify-write, so two writers
   could each read the old file and the second `os.replace` would drop the first's change.
   `add_idea` computed `next_idea_number` and then wrote, so two concurrent captures could
   take the same `IDEA-NNN`.

## Goals / Non-goals

**Goals**
- A writer never edits a headline other than the one it was asked to: it locates the entry
  by identity (`:ID:`, or level + heading text for id-less tasks) in the file as it is now,
  follows it if it moved, and refuses with a drift error if it is gone or ambiguous.
- Every org read-modify-write (and id allocation) runs under one cross-process lock, so
  concurrent writers serialize instead of losing updates.
- A test fails if a new writer skips the lock.

**Non-goals**
- Locking readers (`load_tasks`) — they read a consistent file thanks to atomic replace.
- Fixing the separate duplicate-drawer bug found on the way (IDEA-055) or the
  scaffold drawer leak (IDEA-053).
- Protection against wt versions that predate this spec (all sessions run the same checkout).

## Decision

Add to `wt.org_write`: `org_write_lock(cfg)` (re-entrant `flock` on
`<data_dir>/org-write.lock`), the `@org_writer` decorator, and `_locate_headline(task, lines)`.
Every function that writes an org file is an `@org_writer`; every
`idx = task.line - 1` lookup becomes `_locate_headline`.

## Design

- **Lock:** `org_write_lock` takes a process-wide `RLock` plus, at depth 0, `fcntl.flock(EX)`
  on `<data_dir>/org-write.lock`; a thread-local depth makes it re-entrant (writers call
  writers: `add_idea` → `add_task`, `set_idea_kind` → `set_property`, `archive_idea` writes
  two files). One lock for all org files: writes are small and rare, and a single lock
  cannot deadlock.
- **Decorated writers:** `org_write`: `clear_property`, `set_state`, `set_property`,
  `clock_in`, `clock_out`, `add_clock_entry`, `ensure_idea_keyword`, `add_task`,
  `generate_tasks`, `archive_idea`, `set_idea_priority`, `set_idea_tags`, `add_idea` (id
  allocation inside the lock), `maybe_rotate_ideas_file`; `explore`:
  `_ensure_question_header`, `_replace_section_body`, `normalize_log_stamps_in_file`,
  `append_log`, `retitle_idea`; `idea_ext`: `rename_idea_ext_namespace`,
  `set_idea_extension`, `clear_idea_extension`. Selector-taking writers resolve inside the
  lock, so they always see the current file.
- **Identity:** `_is_same_headline(task, lines, i)` — when the task has an `:ID:`, the
  headline's drawer must carry the same `:ID:` (`_drawer_id` scans every drawer up to the
  next headline, as leniently as orgparse); otherwise same star level and the heading text
  appears in the headline. `_locate_headline` trusts `task.line` only when it passes; else it
  scans the file and returns the unique match, or raises
  `drift: <ID|heading> is no longer at <file>:<line> (not found|N matches) — the file changed
  since it was read; re-run`.
- **Call sites:** the duplicated "line out of range / not a headline" blocks in
  `org_write` (7), `explore._idea_span`, `explore.retitle_idea` and
  `idea_export.idea_subtree_lines` now call `_locate_headline`. The existing state-drift
  checks after it stay.

## Alternatives considered

- **Re-parse with orgparse inside each writer** — heavier, and still needs the lock to be
  correct.
- **Per-file locks** — finer, but `archive_idea` holds two files and ordering rules would be
  needed; one lock is simpler and contention is negligible.
- **Optimistic retry (compare mtime before replace)** — still races between the check and
  the replace; a lock is the honest fix.

## Acceptance criteria

- [x] A writer handed a stale `Task` whose entry moved edits that entry (not whatever is now
      at the old line).
- [x] When a different same-state idea sits at the stale line, it is not touched.
- [x] When the entry is gone, every writer raises a `drift: …` error (selector writers: "no
      task matched") and the file is byte-identical.
- [x] Id-less tasks are located by level + heading.
- [x] The lock is re-entrant and released after use.
- [x] Four processes capturing 8 ideas each produce 32 ideas with 32 distinct ids; four
      processes setting properties lose none.
- [x] A guard test fails if any function calling `_atomic_backup_write` lacks
      `@org_writer`.
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_org_write_race.py` — relocation after a removed subtree;
  the same-state-idea-at-stale-line case (the IDEA-003 shape); vanished entry refuses for
  `set_property`, `set_state`, `_replace_section_body` (and `append_log` re-resolves and
  refuses) with the file unchanged; section writers relocate; id-less task by heading; lock
  re-entrancy + release (`LOCK_NB` probe); 4×8 concurrent `add_idea` processes → unique ids,
  then 4 concurrent `set_idea_kind` processes → all written; AST guard over `src/wt`.
- **Sabotage check:** with `_locate_headline` reverted to "any headline at the line" and the
  decorator made a no-op, the wrong-idea, vanished-idea and concurrent-process tests fail.
- **Regression guard:** full `uv run pytest`.

### Executed (2026-10-25)

- `tests/test_org_write_race.py`: 8 passed. With the sabotage applied, 3 failed
  (`another_idea_at_the_stale_line`, `vanished_idea_refuses`, `concurrent_processes`); the
  code was then restored.
- Full `uv run pytest`: 1574 passed, 1 skipped; spec_lint exits 0.
- One existing fixture (`test_export::test_scaffold_outbound_from_idea_prefills_and_links_back`)
  puts body text before the drawer. `_drawer_id` was made as lenient as orgparse to handle it,
  which surfaced IDEA-055 (a second drawer gets created there).

## Rollout / migration

Built on branch `idea-054-write-race` (worktree `/home/bruno/work/wt-idea-054`), merged
through the conductor. The lock file is created on first write; no data migration. Sessions
pick it up as soon as main moves (every `wt` call imports the checkout).

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
