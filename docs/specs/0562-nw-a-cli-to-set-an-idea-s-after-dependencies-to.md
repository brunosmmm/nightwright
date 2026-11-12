---
id: SPEC-0562
title: "`nw idea after`: set an idea's `:AFTER:` prerequisites from the CLI"
status: accepted
owner: user
created: 2026-11-12
updated: 2026-11-12
kind: feature
tags: [ideas, queue, cli]
source_idea: IDEA-1065
---

## Context

[SPEC-0423](./0423-idea-campaign-ordering-with-after-and-epic.md) made an idea's `:AFTER:`
property a campaign ordering input: `plan_add_idea` and the batch planner read the ids in it
(`_idea_after_refs`, `src/nightwright/agentic/queue.py`) and order or hold the idea's campaign
behind each prerequisite (ideas, and specs via the idea's `:SPEC:`).

The only writer of that property is `seed_ideas_from_epic` (`src/nightwright/specs.py`): it
stamps `:AFTER:` from "(depends_on child N)" annotations in an epic's Breakdown, and only
between sibling ideas of one epic, and never overwrites an existing value. An idea that must
wait for something outside its own spec (for example IDEA-1051 waiting for IDEA-987, which
belongs to another spec or to none) has no CLI route: the human or an agent hand-edits
`ideas.org`, bypassing the org writer's drift checks, backup and `:UPDATED:` stamping.
`nw queue after` edits a campaign's `after`, but only once the campaign exists; it cannot
express the dependency before the idea is queued, and the dependency does not survive in the
ideas file, which is where SPEC-0423 says it should be recorded.

Every other idea property has a verb (`nw idea epic`, `project`, `kind`, `workstream`,
`priority`, `tags`). `:AFTER:` is the gap.

## Goals / Non-goals

**Goals**
- A `nw idea after <selector>` verb that adds, removes, replaces or clears the ids in an idea's
  `:AFTER:`, with the same write path (`org_writer`, atomic backup, `:UPDATED:` stamp) as the
  other `nw idea` property verbs.
- Refuse input that would make the property useless or the ordering unsatisfiable: malformed
  ids, an idea after itself, and a cycle among ideas' `:AFTER:` edges.
- Work for any prerequisite: an idea in another spec or in no spec, or a spec id.

**Non-goals**
- Editing the `after` list of an already-created campaign: that stays `nw queue after`
  (SPEC-0423). The verb prints that hint but does not touch the queue.
- Changing how the queue reads, orders or holds on `:AFTER:`.
- Changing `seed-children` (it keeps its never-overwrite behavior).
- Campaign ids as prerequisites: those are queue state, not durable idea data.

## Decision

Add `nw idea after SELECTOR` with `--add REF` (repeatable), `--remove REF` (repeatable),
`--set REFS` (replace the whole list; empty string clears) and `--clear`, backed by a new
`set_idea_after` in `src/nightwright/org_write.py` that rewrites `:AFTER:` through
`set_property` / `clear_property`. At least one of the four options is required, and
`--set` / `--clear` cannot be combined with `--add` / `--remove`. The result is a
space-separated, de-duplicated, order-preserving list, which is the format
`seed_ideas_from_epic` writes and `_idea_after_refs` already parses. Unlike
`seed-children`, an explicit command overwrites: it is the human's or agent's direct intent.
Malformed ids, self-reference and cycles are hard errors with no write; ids that match no known
idea or spec only warn (SPEC-0018 permissive association), as `nw idea epic` does.

## Design

**Library, `src/nightwright/org_write.py`.** Next to `set_idea_epic`:

```python
def set_idea_after(cfg, selector, *, add=(), remove=(), replace=None) -> tuple[Path, list[str]]:
```

- Resolve `selector` with `resolve_selector`; raise `ValueError` if it is not an idea.
- Current list = the ids in the idea's `:AFTER:` (split on whitespace and commas; the same
  `[A-Za-z][A-Za-z0-9]*-\d+` token pattern `_idea_after_refs` uses; the shared tokenizer is
  imported or moved to one place so the writer and the queue reader cannot drift).
- New list = `replace` (when given) else current + `add` minus `remove`; every ref is
  stripped and upper-cased, de-duplicated keeping first occurrence. Removing a ref that is not
  present is a no-op, not an error (idempotent).
- Validation, all before any write (`ValueError`, message names the ref):
  1. each added/replacing ref matches the token pattern exactly (`IDEA-1051`, `SPEC-0423`,
     outbound `PROJ-0007`); anything else is "invalid id";
  2. no ref equals the idea's own `:ID:`;
  3. no cycle: walking `:AFTER:` edges between ideas from each new ref (through the loaded
     ideas, ignoring non-idea refs) must not reach the idea itself. The error shows the path,
     for example `IDEA-987 -> IDEA-1051`.
- Unknown refs (no idea with that `:ID:`, no spec file with that id) print a warning on stderr
  through the same helper `_warn_unknown_associations` family and are still written.
- An empty resulting list calls `clear_property(cfg, task, "AFTER")`; otherwise
  `set_property(cfg, task, "AFTER", " ".join(refs))`. `set_property` already creates the drawer,
  bumps `:UPDATED:` and takes the atomic backup. If the new value equals the current one no
  write happens and the file's bytes are unchanged.

**CLI, `src/nightwright/cli.py`.** `@idea_group.command("after")` after `idea epic`, with
`shell_complete=C.complete_idea_id` on the selector and on `--add` / `--remove` (idea ids):

```
nw idea after IDEA-1051 --add IDEA-987
nw idea after IDEA-1051 --add IDEA-987 --add SPEC-0423 --remove IDEA-900
nw idea after IDEA-1051 --set "IDEA-987 IDEA-990"
nw idea after IDEA-1051 --clear
```

Output: `✓ after ← IDEA-1051  IDEA-987 SPEC-0423  <path>` (or `(none)` after a clear), followed by a
dim line `already-queued campaigns keep their own list; see: nw queue after <campaign> --add`.
Usage errors (`click.UsageError`): none of the options given; `--set`/`--clear` mixed with
`--add`/`--remove`; `--set` and `--clear` together. `ValueError` becomes `ClickException`
(exit 1), as in `idea_epic_cmd`.

**Reading.** `nw idea show` already prints the idea's properties; the new value shows there
and `nw queue add IDEA-1051` picks it up through the existing `plan_add_idea` path with no
queue change. No schema or `docs/specs` generated file changes beyond this spec and the
regenerated ledger.

## Alternatives considered

- **Make `nw queue after` accept an idea id.** Wrong layer: it edits a campaign ledger that
  may not exist yet, and the dependency would vanish from the ideas file where SPEC-0423
  records it.
- **Add an `--after` option to `nw idea capture` only.** Covers new ideas but not existing
  ones such as IDEA-1051; a dedicated verb covers both.
- **Overwrite-only `--set`, like `nw idea tags`.** Forces a read first for the common "add one
  prerequisite" case; `--add` / `--remove` avoid that, and `--set` stays for scripted replace.
- **Hard-fail on unknown ids.** Contradicts the permissive association stance of SPEC-0018
  and would block pointing at an id in a not-yet-synced file.

## Acceptance criteria

- [ ] `nw idea after IDEA-A --add IDEA-B` writes `:AFTER: IDEA-B` on IDEA-A, creating the
      property when absent, and bumps `:UPDATED:`; a second identical call leaves the file
      byte-identical.
- [ ] Repeated `--add` appends in order, de-duplicates, and keeps existing entries,
      including ones `seed-children` stamped.
- [ ] `--remove` drops only the named ids, a missing id is not an error, and removing the last
      id deletes the `:AFTER:` property.
- [ ] `--set "A B"` replaces the list; `--set ""` and `--clear` delete the property.
- [ ] Prerequisites outside the idea's own spec or epic are accepted (an idea in another
      epic, an idea with no spec, a `SPEC-NNNN` id).
- [ ] A malformed ref (`foo`, `IDEA`, `1051`), a self-reference and a cycle
      (IDEA-A after IDEA-B while IDEA-B is after IDEA-A, directly or transitively) each fail
      with exit code 1, name the ref or path, and leave the ideas file unchanged.
- [ ] An unknown but well-formed id writes and warns on stderr, exit 0.
- [ ] No option given, or `--set`/`--clear` combined with `--add`/`--remove`, is a usage error
      (exit 2) with no write.
- [ ] A non-idea selector (a TODO task) fails with exit 1 and no write.
- [ ] After `nw idea after IDEA-A --add IDEA-B`, `queue.plan_add_idea` for IDEA-A reports
      IDEA-B in its `after` list (the written property round-trips through the SPEC-0423 reader).
- [ ] The writer and `_idea_after_refs` share one tokenizer, so every value the verb writes
      parses back to exactly the list the verb reports.
- [ ] `nw queue after`, `seed-children` and every other `nw idea` verb behave as before.

## Test plan

- **Automated tests:** new `tests/test_idea_after.py`, modelled on `tests/test_idea_epic.py`
  (temp org dir, `CliRunner`, `load_tasks`), with `pytestmark = pytest.mark.fast` as the one
  module-level speed-tier marker (name `test_idea_after` is not `tui`/`remote`/`integration`
  by the SPEC-0490 taxonomy). It covers: add / repeat-add / dedupe / order; add onto a value
  stamped by `seed_ideas_from_epic`; remove, remove-missing, remove-last deletes the
  property; `--set` and `--clear`; cross-spec and spec-id prerequisites; malformed ref,
  self-reference, direct and transitive cycle (file bytes unchanged, error text names the
  path); unknown id warns and writes; usage errors; non-idea selector; idempotent no-op
  write; `:UPDATED:` bump; and the round trip through `_idea_after_refs` /
  `plan_add_idea` using the same temp config the queue tests use
  (`tests/test_idea_ordering_e2e.py` shows the fixtures). Also check completion lists idea
  ids for the selector.
- **Tier check:** `python3 tools/test_tiers.py --check` exits 0 with the new module.
- **Manual verification:** on a scratch copy of the ideas files (never the live file),
  `nw idea after <idea> --add <other-idea>`, then `nw idea show <idea>` shows `:AFTER:`, and
  `nw queue add <idea>` (dry-run) lists the prerequisite as held-behind.
- **Regression guard:** run the affected tests
  (`python3 tools/affected_tests.py --run uv run pytest -q`) plus `tests/test_seed_children.py`,
  `tests/test_idea_ordering_e2e.py`, `tests/test_idea_epic.py` and `tests/test_idea_kind.py`;
  `python3 tools/spec_lint.py` exits 0.

## Rollout / migration

No data migration. Existing `:AFTER:` values are already in the format the verb writes. Once
shipped, the one-off hand edits of `ideas.org` for cross-spec ordering are replaced by the
verb; campaigns already queued are updated with `nw queue after <campaign> --add`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass (`python3 tools/affected_tests.py --run uv run pytest -q`);
      `python3 tools/test_tiers.py --check` exits 0; manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`) and
      `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
