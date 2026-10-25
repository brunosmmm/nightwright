---
id: SPEC-0072
title: "Idea-archival primitive: atomic, drift-guarded subtree move"
status: superseded
superseded_by: SPEC-0168
owner: user
created: 2026-07-26
updated: 2026-07-26
source_idea: IDEA-084
parent: SPEC-0070
milestone: "M4: cli ergonomics"
tags: [org, maintenance]
---

## Context

Promoted from idea `IDEA-084` (SPEC-0072).

  :PROPERTIES:
  :ID: IDEA-084
  :EPIC: SPEC-0070
  :END:
** Summary
Child of epic `SPEC-0070`.

SPEC-0072 — Idea-archival primitive (`archive_idea`): atomic, drift-guarded subtree
** Open questions

** Log

## Goals / Non-goals

**Goals**
- A new `org_write.py::archive_idea(cfg, task)` primitive: cuts one idea's whole subtree
  (headline through the line before the next top-level headline, i.e. everything nested under
  it — properties, Summary, Open questions, Log) out of its source file and appends it verbatim
  to a dedicated archive file.
- Same rigor as the existing `set_property`/`set_state` primitives: line-anchored, drift-
  guarded (refuses to act if the on-disk headline no longer matches what was parsed), atomic
  writes on both ends (source truncation and archive append), automatic backup via the existing
  `_atomic_backup_write`.
- Lossless: every line of the subtree moves byte-for-byte; nothing is summarized, reformatted,
  or dropped.

**Non-goals**
- Not deciding *when* to archive — that's SPEC-0073 (the automatic hooks). This spec only
  builds the primitive and proves it works correctly in isolation.
- Not a new CLI verb — per SPEC-0070's Non-goals (and SPEC-0056's prior rejection of a
  dedicated archive verb), this is a library-level primitive other code calls into, not
  something a human invokes directly.
- Not merging/deduplicating archive content — a straight append; the archive file simply grows
  (its own eventual rotation, if ever needed, is out of scope here).

## Decision

Mirror `set_state`'s existing line-anchored + drift-guarded + atomic-write shape exactly, but
instead of rewriting one line, compute the full line range of the idea's subtree (headline
through the line before the next `^\*` top-level headline, or EOF) and move that whole range:
remove it from the source file, append it to a new dedicated archive file
(`cfg['org_ideas_archive_file']`, new config key, bootstrapped with the same `#+TODO` header
convention `add_idea` already uses).

## Design

- **New config key** (`config.py::DEFAULT_CONFIG`): `org_ideas_archive_file:
  "~/work/org/ai/ideas-archive.org"`.
- **`org_write.py::archive_idea(cfg, task)`** (new):
  ```python
  def archive_idea(cfg, task):
      if not task.is_idea:
          raise ValueError(f"{task.id} is not an idea")

      lines = open(task.file).read().splitlines(keepends=True)
      idx = task.line - 1
      if not (0 <= idx < len(lines)):
          raise ValueError(f"line {task.line} out of range in {task.file}")
      m = _HEADLINE_RE.match(lines[idx].rstrip("\n"))
      if not m:
          raise ValueError(f"line {task.line} in {task.file} is not a headline: {lines[idx]!r}")
      stars, _gap, rest = m.group(1), m.group(2), m.group(3)
      if len(stars) != 1:
          raise ValueError(f"{task.file}:{task.line}: expected a top-level idea headline")

      first = rest.split(None, 1)[0] if rest.split() else ""
      if task.state and first != task.state:
          raise ValueError(f"drift: {task.file}:{task.line} expected state {task.state!r}, "
                           f"found {first!r} — re-run after a fresh parse")

      end = idx + 1
      while end < len(lines) and not re.match(r"^\*\s", lines[end]):
          end += 1
      subtree = lines[idx:end]

      _atomic_backup_write(cfg, task.file, "".join(lines[:idx] + lines[end:]))

      configured = cfg.get("org_ideas_archive_file")
      if configured:
          archive_path = os.path.expanduser(configured)
      else:
          ideas_dir = os.path.dirname(os.path.expanduser(cfg["org_ideas_file"]))
          archive_path = os.path.join(ideas_dir, "ideas-archive.org")
      if os.path.exists(archive_path):
          existing = open(archive_path).read()
          if existing and not existing.endswith("\n"):
              existing += "\n"
      else:
          keywords = cfg.get("org_idea_keywords") or [
              "IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "DROPPED"]
          existing = f"#+TODO: {' '.join(keywords)}\n\n"
      _atomic_backup_write(cfg, archive_path, existing + "".join(subtree))
      return archive_path
  ```
- Reuses `_HEADLINE_RE` and `_atomic_backup_write`, already defined in this module — no new
  parsing machinery.
- **Shipped refinement over the original sketch:** `archive_path` falls back to
  `<dirname(org_ideas_file)>/ideas-archive.org` when `cfg['org_ideas_archive_file']` isn't set,
  rather than requiring the key outright — several existing test fixtures across the codebase
  build minimal `cfg` dicts predating this new key, and `set_state`'s SPEC-0073 hook calls into
  `archive_idea` from all of them.

## Alternatives considered

- **Reparse the file with `orgparse` and reconstruct it from the node tree, minus the archived
  node** — rejected: every existing mutation primitive in this file (`set_property`,
  `set_state`) already works by line-slicing the raw text directly, specifically to guarantee
  byte-for-byte preservation of everything *not* being changed (comments, blank-line spacing,
  unusual formatting). Line-slicing for the subtree boundary is the same approach, just over a
  range instead of one line.
- **Soft-delete (tag as archived, leave in place) instead of physically moving** — rejected:
  doesn't address either problem from SPEC-0070's explore (raw growth or signal-to-noise) —
  the entry is still sitting in the same file either way.

## Acceptance criteria

- [x] `archive_idea` on a fixture idea removes its exact subtree (headline through Log, nothing
      more, nothing less) from the source file and appends it verbatim to the archive file.
- [x] The archive file is bootstrapped with a `#+TODO` header on first use, matching
      `add_idea`'s existing convention, and appends (doesn't overwrite) on subsequent calls.
- [x] Calling `archive_idea` on a stale `task` (headline changed on disk since it was parsed)
      raises a drift error rather than silently corrupting either file.
- [x] Calling `archive_idea` on a non-idea task raises a clear error.
- [x] After archival, the idea is still discoverable via `load_tasks` (now living in the
      archive file) with all its properties/Summary/Log/questions intact.

## Test plan

- **Automated tests:** `tests/test_ideas.py` (or a new dedicated file) — archiving a fixture
  idea moves its full subtree correctly (multi-idea file: only the target subtree moves,
  siblings before/after are untouched); archive file bootstraps its header on first use and
  appends correctly on a second archival; a drift scenario (headline mutated between parse and
  call) raises without touching either file; a non-idea task raises; `load_tasks` sees the
  idea's full content (properties + Summary + Log) correctly in the archive file post-move.
- **Manual verification:** archive a real scratch idea from a scratch fixture file (never the
  real `ideas.org`), inspect both files by hand.
- **Regression guard:** full `uv run pytest` green; `set_property`/`set_state` behavior
  unaffected (no shared state, purely additive new function).

## Rollout / migration

1. Add the `org_ideas_archive_file` config key + `archive_idea` primitive.
2. No migration and no wiring into any automatic trigger yet — that's SPEC-0073. This spec
   ships the primitive alone, fully tested in isolation.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- None blocking.
