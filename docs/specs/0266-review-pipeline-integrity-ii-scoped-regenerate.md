---
id: SPEC-0266
title: "Review pipeline integrity II: scoped regenerate, divergence reset, locked follow-up groups"
status: accepted
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-352
parent: SPEC-0169
tags: [agentic, close, divergence, followups, review-follow-up]
---

## Context

Split out of SPEC-0264 so each spec stays one campaign; SPEC-0264 keeps the attestation and
escalation-answer fixes. This spec comes from the same triage of `wtfug:` review follow-ups
(minor findings raised on merged specs SPEC-0202..0259). We re-checked each finding kept
here against `main` on 2026-10-27, and it is still true.

Kept findings:

- **IDEA-352** `wtfu:71ee806477f9` (SPEC-0255): *Regenerate commit commits the whole index,
  not exactly the declared paths.* A regenerate command that stages files itself gets
  undeclared content into the commit. The stray-file check cannot see that content, because
  it is already committed.
- **IDEA-344** `wtfu:a9038f6f3bb1` (SPEC-0247): *Resuming a loop_exhausted park with
  --reset-loops keeps the old fix rounds for divergence.* The conductor's reset is undone in
  practice: the next fix round can trip `diverging` on rounds from before the reset.
- **IDEA-325** (SPEC-0239)
  - `wtfu:29b92671b469`: *Unlocked read-modify-write of the group Summary.* A human edit
    made between the read and the write is lost.
  - `wtfu:2031670b701b` and `wtfu:ba2432d61e35`: *The filer adopts its own group even when
    that group is closed or archived.*

The other findings in these ideas were dropped. Most were already fixed:
- `git add` moved inside the regenerate `try`
- the docs-only allowlist check now runs before the regenerate commands
- `range-diff` uses `-s`

The rest were test-only, cosmetic, or a design limit we accept (for example, the revert
guard detects only exact single-commit reverts).

## Goals / Non-goals

**Goals**
- The regenerate commit holds only declared paths.
- `--reset-loops` gives divergence detection a fresh start.
- The follow-up filer never loses a concurrent edit to its group, and never writes into a
  closed group.

**Non-goals**
- Strengthening the revert guard or the absorbed-commit heuristics (SPEC-0252 follow-ups).
- Catching a regenerate command that runs `git commit` itself; only staged-but-uncommitted
  undeclared paths are caught.

## Decision

Make three local fixes:
- Refuse a regenerate step that staged undeclared paths, and unstage them.
- Cut divergence rounds at a `resume` that reset loops.
- Do the follow-up group read-modify-write under `org_write_lock`, and match only open
  groups.

## Design

1. **Regenerate commit scope** (`src/wt/agentic/close.py` `_regenerate_after_rebase`).
   - After `git add -- <declared>`, read `staged = git diff --cached --name-only -z`.
   - If `extra = set(staged) - set(declared)` is non-empty, first run
     `git reset -q -- <extra>` (this unstages the undeclared paths in every case; the
     existing `put_back()` does not: for an adopted worktree it resets only `declared`, and
     for an own, already-dirty worktree it does nothing). Then call `put_back()` and return
     `"regenerate commands staged undeclared files: " + ", ".join(sorted(extra)[:10])`.
     That return value is the `regenerate_failed` park.
   - Otherwise commit as today.

2. **Divergence reset** (`src/wt/agentic/divergence.py` `rounds`).
   - In the cut loop, a `resume` record with `loops_reset` truthy also sets `cut = i + 1`,
     whatever park preceded it.
   - Update the docstring and the SPEC-0247 note in the docstring.

3. **Locked, open-only follow-up groups** (`src/wt/agentic/followups.py`).
   - `find_marker(cfg, marker, *, open_only=False)`. With `open_only`, it skips nodes whose
     TODO keyword is in `dones`, and it scans the ideas file only, not the archive. It keeps
     raising `ValueError` for a matching open node with no `:ID:`.
   - Why not reuse `_open_group` (followups.py ~191): it silently skips an open heading
     with no `:ID:`, which would let the filer double-file. `find_marker`'s guard is worth
     keeping, so the flag is added there.
   - `_file` calls it with `open_only=True`. A closed own group then counts as absent: a new
     group is created. Recovery of already-filed lines still works through the `followup`
     ledger events (step 1).
   - `_file` wraps the span from `find_marker` (step 2) through `set_summary` /
     `retitle_idea` (step 5) in `with OW.org_write_lock(cfg):`. That lock is re-entrant,
     so the inner `OW.add_idea` / `EX.set_summary` calls still work. `own_text` is read
     inside the lock.
   - `group_legacy` keeps the current `find_marker` default.

## Alternatives considered

- **Commit with a pathspec (`git commit -- <declared>`).** This silently leaves the
  undeclared files staged and needs the stray check to catch them. An explicit refusal is
  clearer.

## Acceptance criteria

- [ ] A regenerate command that runs `git add` on an undeclared file parks
      `regenerate_failed` with that path in the detail, the branch gets no regenerate
      commit, and the index holds none of the undeclared paths afterwards (for an own and
      for an adopted worktree).
- [ ] After `wt queue resume <cid> --reset-loops`, `divergence.rounds` returns only rounds
      recorded after that resume.
- [ ] A Summary edit made by another writer while `_file` holds the group is not lost. The
      edit waits on `org_write_lock`, and `_file` re-reads inside the lock.
- [ ] A campaign whose `wtfug:<cid>` group is closed gets a new group instead of lines
      appended to the closed one.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_close_regenerate.py::test_regenerate_that_stages_undeclared_file_parks`:
    the regenerate cmd is `python3 tools/spec_lint.py --write-ledger && touch x && git add x`.
    Close parks `regenerate_failed` with `x` in the detail, `git log` shows no regenerate
    commit, and `git diff --cached --name-only` is empty. Repeat for an adopted
    (`own=False`) worktree.
  - `tests/test_agentic_divergence.py::test_reset_loops_resume_cuts_rounds`: the ledger
    holds 3 fix rounds, a `park` (`loop_exhausted`), a `resume` (`loops_reset: true`) and 1
    new round. `rounds()` returns 1 round. The same ledger with `loops_reset: false` returns
    4.
  - `tests/test_agentic_review_triage.py::test_filer_rereads_group_under_lock`:
    monkeypatch `EX.read_idea_enrichment` so that the first call (outside any lock, in the
    old code) is followed by a concurrent `EX.set_summary` adding a line from another
    thread. That thread must block on `org_write_lock`. Assert the final Summary holds both
    the human line and the new follow-up lines.
  - `tests/test_agentic_review_triage.py::test_filer_ignores_closed_own_group`: create the
    `wtfug:<cid>` group, then close it (state DROPPED). `file_followups` creates a new group
    id and leaves the closed group's Summary unchanged.
- **Sabotage checks.** Each one must fail its test; record the result in the spec:
  - remove the staged-scope check (or the `git reset -- <extra>`)
  - remove the `loops_reset` cut
  - drop the `org_write_lock` wrapper
  - pass `open_only=False` in `_file`
- **Regression guard:** run the full `uv run pytest`. In particular,
  `test_agentic_close_regenerate.py`, `test_agentic_divergence.py`,
  `test_agentic_review_triage.py` and `test_ideas_group_followups.py` must stay green.
- **Manual verification:** none. Every path is exercised in-process.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- (none)

## Clock Log
