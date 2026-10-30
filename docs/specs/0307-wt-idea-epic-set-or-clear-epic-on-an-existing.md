---
id: SPEC-0307
title: "wt idea epic: set or clear :EPIC: on an existing idea"
status: proposed
owner: user
created: 2026-10-30
kind: feature
parent: SPEC-0298
source_idea: IDEA-416
tags: [ideas, cli]
---

## Context

An idea's `:EPIC:` property links it to an epic spec. Readers of that link exist:
`wt ideas list --epic`, `wt queue add --epic` (`Q.expand_epic`), `wt spec new --from-idea`
(sets `parent:` from `:EPIC:`, `src/wt/specs.py:192`), and the epic grouping in `report.py`.

Writers are not symmetric. Only two paths write `:EPIC:`:

- `wt idea capture --epic` (`add_task` in `src/wt/org_write.py`), which works only at
  capture time.
- `wt spec seed-children` (`seed_ideas_from_epic`, `src/wt/specs.py`), which creates new
  ideas under an epic.

An idea captured without `--epic`, or one that belongs to a different epic than first
thought, can never be re-associated from the CLI. The only recourse is hand-editing
`ideas.org`. This is the same gap [SPEC-0109](./0109-found-another-shortcoming-when-capturing-ideas.md)
closed for `:PROJECT:` with `wt idea project`. Under the agentic MVP epic
([SPEC-0298](./0298-agentic-mvp-cheap-durable-quiet-conductor.md)) the conductor and
stewards file ideas that later turn out to belong to an epic, and the queue's `--epic`
expansion only sees ideas that carry the link. A missing CLI also means a one-off file edit,
which the missing-CLI rule says to avoid.

All the library parts exist: `set_property`, `clear_property` and `_warn_unknown_associations`
(already epic-aware) in `src/wt/org_write.py`. Only the idea-level mutator and the CLI verb
are missing.

## Goals / Non-goals

**Goals**
- `wt idea epic <selector> --set <SPEC-ID>` sets `:EPIC:` on an existing idea, and
  `--set ""` clears it.
- Same semantics as `wt idea project` (SPEC-0109): permissive validation, clearing removes the
  property, `:UPDATED:` is stamped, other properties are untouched.
- Shell completion for the epic id, using the existing `C.complete_epic`.

**Non-goals**
- No change to how `:EPIC:` is read, or to `capture --epic` and `seed-children`.
- No change to a spec's `parent:` frontmatter. The idea's `:EPIC:` and the spec's `parent:`
  stay independent, as today. An idea that already has a linked spec is not re-parented.
- No new gate: an unknown epic id warns and is still written, as SPEC-0018 requires.
- No TUI wiring in this spec.

## Decision

Add `set_idea_epic(cfg, selector, epic)` to `src/wt/org_write.py`, parallel to
`set_idea_project`, and a `wt idea epic SELECTOR --set EPIC` command in `src/wt/cli.py`
parallel to `wt idea project`. Validation is permissive: an id that is not a known
`kind: epic` spec warns on stderr and is still written. An empty value clears the property.

## Design

**Library (`src/wt/org_write.py`, next to `set_idea_project`):**

```python
def set_idea_epic(cfg, selector, epic):
    task = resolve_selector(cfg, selector)
    if not task.is_idea:
        raise ValueError(f"{selector!r} is not an idea")
    value = (epic or "").strip()
    if not value:
        return clear_property(cfg, task, "EPIC")
    _warn_unknown_associations(cfg, epic=value)
    return set_property(cfg, task, "EPIC", value)
```

- Setting reuses `set_property`, so the write is line-anchored, drift-guarded, backed up and
  atomic, and it bumps `:UPDATED:` on an idea (SPEC-0076).
- Clearing reuses `clear_property`, which removes the line rather than writing an empty
  value (SPEC-0109) and bumps `:UPDATED:`.
- `_warn_unknown_associations(cfg, epic=...)` already warns with the known epic list.
- Clearing an idea that has no `:EPIC:` is a no-op write that does not fail.
- Surrounding whitespace in the value is stripped. The id is otherwise stored as given
  (no case folding), the same as `capture --epic`.

**CLI (`src/wt/cli.py`, in `idea_group`, after `idea_project_cmd`):**

```
wt idea epic SELECTOR --set SPEC-0298
wt idea epic SELECTOR --set ""        # clear
```

- `SELECTOR` uses `C.complete_idea_id`. `--set` uses `shell_complete=C.complete_epic`.
- Omitting `--set` raises `click.UsageError("pass --set (use --set '' to clear)")`.
- A `ValueError` (no match, ambiguous, not an idea) becomes `click.ClickException`.
- Success prints `✓ epic ← <selector>  <value or (none)>  <path>`.
- The command's help text gives the two examples above and notes that unknown ids warn but
  still write.

**Docs:** add `wt idea epic` next to `wt idea project` wherever the idea verbs are listed
(`wt idea --help` docstring in `idea_group`, and the `/wt-orient` skill's idea command
list if it enumerates them).

## Alternatives considered

- **Generic `wt idea set <prop> <value>`** — broader, but it bypasses per-property
  validation and the existing one-verb-per-property convention (`project`, `kind`,
  `workstream`, `priority`, `tags`).
- **Add `--epic` to an existing verb such as `wt idea project`** — mixes two properties in one
  command and breaks the `--set ""` clear convention.
- **Strict validation (reject unknown epics)** — SPEC-0018 deliberately keeps associations
  permissive, and an epic spec may be drafted after the idea is filed.
- **Keep hand-editing `ideas.org`** — the status quo; it skips the backup, the `:UPDATED:`
  stamp and the drift guard.

## Acceptance criteria

- [ ] `set_idea_epic(cfg, "IDEA-N", "SPEC-0298")` writes `:EPIC: SPEC-0298` to that idea's
      drawer, creating the drawer if absent, and returns the file path.
- [ ] Setting a different value on an idea that already has `:EPIC:` replaces it, leaving
      exactly one `:EPIC:` line.
- [ ] `set_idea_epic(cfg, "IDEA-N", "")` (or `None`) removes the `:EPIC:` line entirely. No
      empty `:EPIC:` line remains. Clearing an idea with no `:EPIC:` does not raise.
- [ ] An id that is not a known `kind: epic` spec prints a warning on stderr naming the known
      epics, and is still written.
- [ ] Calling it on a non-idea task raises `ValueError`, and `wt idea epic` turns that into a
      clean `ClickException` (non-zero exit, no traceback).
- [ ] Setting or clearing bumps `:UPDATED:` on the idea, and leaves `:PROJECT:`, `:KIND:`,
      `:ID:` and other properties and the headline unchanged.
- [ ] After setting, `wt ideas list --epic SPEC-0298` includes the idea. After clearing, it
      does not.
- [ ] `wt idea epic X` without `--set` exits non-zero with a usage message.
- [ ] `wt idea epic X --set SPEC-0298` prints a success line, and `--set ""` prints
      `(none)`.
- [ ] `--set` offers epic ids through shell completion.
- [ ] `wt spec new --from-idea` on an idea given an `:EPIC:` through this command sets
      `parent:` to that epic.

## Test plan

- **Automated tests (`tests/test_idea_epic.py`, using the fixture shape of
  `tests/test_idea_project.py`):**
  - `test_set_idea_epic_sets_and_changes`: set, then change, with exactly one `:EPIC:` line.
  - `test_clearing_removes_the_property_rather_than_blanking_it`, plus clearing when absent.
  - `test_unknown_epic_warns_but_still_writes` (capsys; a temp `specs_dir` with one known
    epic checks the warning lists it, and a known epic produces no warning).
  - `test_set_idea_epic_rejects_a_non_idea`.
  - `test_setting_an_epic_stamps_updated` and `test_other_properties_survive_an_epic_change`.
  - `test_cli_epic_set_and_clear` through `CliRunner`: the output lines and the file content.
  - `test_cli_epic_requires_set` and `test_cli_epic_on_a_non_idea_is_a_clean_error`.
  - `test_epic_makes_the_idea_visible_to_epic_filtering`: `wt ideas list --epic`.
  - `test_spec_new_from_idea_uses_the_set_epic_as_parent`.
  - `test_epic_option_has_completion`: the `--set` option carries `C.complete_epic`.
- **Manual verification:** in a scratch data dir run `wt idea capture "x"`, then
  `wt idea epic <id> --set SPEC-0298`, `wt idea show <id>` (the property is shown),
  `wt idea epic <id> --set ""`. Check that the property is gone and `ideas.org` has a backup
  of each write.
- **Regression guard:** full `uv run pytest`, including `tests/test_idea_project.py` and the
  capture tests (`capture --epic` and `seed-children` untouched), and
  `python3 tools/spec_lint.py`.

## Rollout / migration

Additive. No data migration. Ideas filed before this change are re-associated by running the
new command.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
