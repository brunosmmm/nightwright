---
id: SPEC-0167
title: "Refuse promoting terminal or archived ideas without --force"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M3: idea→spec pipeline"
kind: feature
tags: [ideas, specs, safety, bug]
source_idea: IDEA-012
---

## Context

Promoted from idea `IDEA-012`.

`promote_idea`, `scaffold_from_idea` and `scaffold_outbound(from_idea=…)` in
`src/wt/specs.py` guard only against an idea that already carries `:SPEC:`. They accept
ideas that are:

- in a **terminal** state (`SHIPPED`, `PROMOTED`, `EXPORTED`, `DROPPED`, `RESEARCHED`, the done
  side of `org_idea_keywords`), or
- living in the **ideas archive** file (`archive_idea`, SPEC-0073).

They then move the idea back to `SPECCED` with a new `:SPEC:`. One ideas file is routinely
shared by several concurrent sessions working on different projects, and IDEA ids are
allocated globally. So a mistyped or assumed id silently rewrites another project's finished
work. That happened on 2026-10-25: a session assumed its fresh capture was IDEA-003 (it was
IDEA-011), and `wt spec new --from-idea IDEA-003 --internal` turned the comfort-assistant
idea from `SHIPPED` back to `SPECCED`. It had to be restored from `org-backups`. The CLI
success line (`✓ SPEC-0165 <path> internal <- IDEA-003`) also never names the idea's
heading, so nothing showed that the wrong idea was hit.

## Goals / Non-goals

**Goals**
- Promoting a terminal or archived idea fails with a clear message (id, heading, state, file)
  unless `--force` is passed.
- Nothing is written and the idea is not mutated when the guard fires.
- The `wt spec new --from-idea` success line names the promoted idea's heading.

**Non-goals**
- `--internal` on an idea whose `:PROJECT:` is an outbound target. SPEC-0020 decides that
  `--internal` overrides routing, and `tests/test_routing.py` covers it.
- Meta-Tools routing / "unknown project" warnings.
- Locking or id reservation for concurrent writers.
- Lint detection of template placeholders in accepted specs (noticed while authoring this;
  separate).

## Decision

Add one guard, `_refuse_closed_idea(cfg, task, force)`, called right after each promote path
resolves its idea. It raises `ValueError` when the idea is terminal (`task.is_done`) or its
file is the ideas archive, unless `force`. The archive path moves into a helper,
`org_write.ideas_archive_path(cfg)`, that `archive_idea` also uses. The CLI success line
appends the idea heading.

## Design

- `src/wt/org_write.py`: `ideas_archive_path(cfg)` returns `org_ideas_archive_file`
  (expanded), else `<dir of org_ideas_file>/ideas-archive.org`. `archive_idea` calls it, with
  no behavior change.
- `src/wt/specs.py`: `_refuse_closed_idea(cfg, task, force)`.
  - **Terminal:** `task.is_done` (the state is in the file's done keywords; for ideas, the done
    side of the idea vocabulary).
  - **Archived:** the real path of `task.file` equals the real path of `ideas_archive_path(cfg)`.
  - Message:
    `idea IDEA-NNN "<heading>" is <STATE>[ and archived in <file>]; refusing to re-promote a
    closed idea (pass --force if you really mean it)`.
  - Called in `promote_idea` (before routing, next to the `:SPEC:` guard), in
    `scaffold_from_idea`, and in `scaffold_outbound` when `from_idea` is set. The scaffolders
    are also called directly, so every entry point is covered.
- `src/wt/cli.py` (`spec new`): when `--from-idea` was used, the success line ends with
  `<- IDEA-NNN '<heading>'`.
  - *As shipped:* the idea is resolved once **before** promotion (the CLI already resolved it
    to read `:PROJECT:`), and its id and heading are printed. The heading is escaped for Rich,
    so brackets in a heading survive.
  - *As shipped:* `scaffold_outbound` has no `:SPEC:` check of its own; only `promote_idea`
    does. That is unchanged here. The new closed-idea guard does run in `scaffold_outbound`.

## Alternatives considered

- **Refuse only archived ideas** — rejected. A terminal idea still in the live file is
  just as finished, and re-promoting it is just as wrong without intent.
- **Confirm interactively** — rejected. Agents drive `wt` non-interactively, and an explicit
  `--force` is the established override in this CLI.
- **Refuse ideas from another `:PROJECT:` than the cwd repo** — rejected for now. wt has no
  reliable "current project" signal, and it would collide with SPEC-0020's `--internal`.

## Acceptance criteria

- [x] `promote_idea`, `scaffold_from_idea` and `scaffold_outbound(from_idea=…)` raise for an
      idea in each terminal state and for an idea in the archive file. The message names
      id, heading and state, and the archive file when relevant.
- [x] When the guard fires, no spec/outbound file is written and the idea's state and
      properties are unchanged.
- [x] `--force` bypasses the guard, as before.
- [x] Open (`IDEA`/`INCUBATE`/`SPECCED`) live ideas promote as before; the existing routing
      and explore tests pass unchanged.
- [x] `wt spec new --from-idea` success output contains the idea's heading.
- [x] `archive_idea` behavior unchanged, now using `ideas_archive_path`.
- [x] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_promote_guard.py` (new), tmp config with `specs_dir`,
  outbox and archive.
  - Parametrized over terminal states via `set_state`.
  - An idea archived via `archive_idea` while in a terminal state.
  - Assert `ValueError` text, no new files under specs/outbox, and the idea unchanged;
    `force=True` succeeds.
  - CLI `spec new` refuses (exit ≠ 0, message), and its success line shows the heading.
  - An `ideas_archive_path` unit test covers the configured and default paths.
- **Manual verification:** replay the incident shape on a scratch config: capture → SHIPPED
  → archived, then `wt spec new --from-idea <id> --internal` refuses, and `--force` works.
- **Regression guard:** full `uv run pytest`.

### Test record (2026-10-25)

- **Automated:** `tests/test_promote_guard.py` (new) has 29 tests: `ideas_archive_path`
  default and configured. Every terminal state × {`promote_idea`, `promote_idea(internal)`,
  `scaffold_from_idea`} and × `scaffold_outbound(from_idea)` raises, writes nothing, and leaves
  the idea unchanged. The archive-file message is checked. An archived idea hand-reopened to
  INCUBATE is still refused. `force=True` works. Open ideas promote. The CLI refuses and writes
  nothing, and its success line names a bracketed heading. **Mutation check:** with the guard
  disabled, 23 of the 29 fail (the 6 that still pass are the ones that should: archive path,
  force, open ideas, heading). Full `uv run pytest`: **1187 passed, 82 skipped, 0 failed**.
- **Manual:** the incident replayed on a scratch config: capture `--project
  comfort-assistant` → summary → `wt idea state … SHIPPED` (archived). Then
  `wt spec new --from-idea IDEA-001 --internal` fails, exit 1, with the message naming the id,
  heading, SHIPPED and the archive file. No spec written. `--force` promotes, and its success
  line shows the heading.
- **Found while testing (separate idea):** `DEFAULT_CONFIG["org_ideas_archive_file"]` is
  hard-coded to `~/work/org/ai/ideas-archive.org`. A config that moves `org_ideas_file` but not
  the archive path still archives into the user's real archive file. A first replay attempt
  leaked two test ideas into it, and they were removed by hand.

## Clock Log

Internal work: clock on IDEA-012 with `wt idea clock-in` / `clock-out`.

## Rollout / migration

Code-only. Callers that intentionally re-promote closed ideas must add `--force`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
