---
id: SPEC-0168
title: "Derive the ideas archive path from org_ideas_file by default"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M3: idea→spec pipeline"
kind: feature
tags: [ideas, org, config, safety, bug]
source_idea: IDEA-014
supersedes: [SPEC-0072]
---

## Context

Promoted from idea `IDEA-014`.

[SPEC-0072](./0072-idea-archival-primitive.md) added `archive_idea` and the config key
`org_ideas_archive_file`, with a **hard-coded** default
`"~/work/org/ai/ideas-archive.org"` (`src/wt/config.py:49`). Its shipped fallback (archive next
to `org_ideas_file`) only applies when the key is absent. Because `DEFAULT_CONFIG` always
supplies it, the fallback never applies to a real `load_config()` config.

So any config that moves `org_ideas_file` without also setting `org_ideas_archive_file` still
archives terminal ideas into `~/work/org/ai/ideas-archive.org`. That covers scratch and manual
test configs, `scripts/install-bundled-config.sh` (SPEC-0136, which sets `org_ideas_file` and
`specs_dir` only), and any user with a different layout. The leaked ideas land in whatever
live archive sits at that path. On 2026-10-25, replaying SPEC-0167 on a scratch config
leaked two test ideas into the user's real archive, shared with another project's session.
They had to be removed by hand.

## Goals / Non-goals

**Goals**
- When `org_ideas_archive_file` is not set by the user, the archive is
  `ideas-archive.org` **next to the effective `org_ideas_file`**.
- No change for a default install: the derived path from the default `org_ideas_file` is
  exactly the old default, `~/work/org/ai/ideas-archive.org`.
- An explicit `org_ideas_archive_file` still wins.

**Non-goals**
- Moving existing archive files. Nothing on disk is touched.
- Any other change to `archive_idea` (subtree move, drift guard, `#+TODO` bootstrap,
  SPEC-0073 auto-archive hook). Those are carried forward unchanged from SPEC-0072.

## Decision

`DEFAULT_CONFIG["org_ideas_archive_file"] = None`. `org_write.ideas_archive_path(cfg)` (added
in SPEC-0167) keeps its existing precedence: the explicit key, else `ideas-archive.org` next to
`org_ideas_file`. This spec **supersedes SPEC-0072** only to replace the default value. All of
SPEC-0072's other decisions stand as shipped: the atomic, drift-guarded subtree move into the
archive file, the `#+TODO` header bootstrap, and `archive_idea` returning the archive path.

## Design

- `src/wt/config.py`: the default becomes `None`, with a comment saying the effective path is
  derived next to `org_ideas_file` when unset.
- `src/wt/org_write.py::ideas_archive_path`: no logic change. The docstring cites SPEC-0168.
- `docs/product/guides/org-storage.md`: the config table default reads "`ideas-archive.org`
  next to `org_ideas_file`", and the archive section says the same.
- `tests/test_archive_path_default.py` (new) goes through the real `load_config()` with
  `WT_CONFIG_DIR` pointing at a tmp config, so it tests the default merge itself and not a
  hand-built dict.

## Alternatives considered

- **Keep the hard-coded default and document "set both keys"** — rejected. It is the hazard
  itself: any config that forgets the second key writes into someone's real archive.
- **Derive in `load_config` (write the computed path into cfg)** — rejected. Resolving in one
  helper at use time keeps hand-built test configs and loaded configs on the same code path.

## Acceptance criteria

- [x] With no user config, the effective archive path is `~/work/org/ai/ideas-archive.org`
      (unchanged).
- [x] With a user config that sets only `org_ideas_file: <dir>/ideas.org`, the effective
      archive path is `<dir>/ideas-archive.org`, and archiving a terminal idea writes there
      and nowhere else.
- [x] An explicit `org_ideas_archive_file` still wins.
- [x] `docs/product/guides/org-storage.md` documents the derived default.
- [x] SPEC-0072 set `status: superseded`, `superseded_by: SPEC-0168`.
- [x] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_archive_path_default.py`. Set `WT_CONFIG_DIR` / `WT_DATA_DIR`
  / `HOME` to tmp, then call `load_config()` for:
  1. no config file, where the path is `$HOME/work/org/ai/ideas-archive.org`;
  2. only `org_ideas_file` set, where the path is next to it, and a capture → summary →
     `set_idea_state SHIPPED` run writes the archive next to the ideas file, while
     `$HOME/work/org/ai/` stays absent;
  3. an explicit `org_ideas_archive_file`, which wins.
- **Manual verification:** repeat the SPEC-0167 replay with a scratch config that sets only
  `org_ideas_file`. Check the archive lands in the scratch dir and that the md5 of the real
  `~/work/org/ai/ideas-archive.org` is unchanged.
- **Regression guard:** full `uv run pytest`.

### Test record (2026-10-25)

- **Automated:** `tests/test_archive_path_default.py` (new) has 3 tests through the real
  `load_config()`, with tmp HOME, `WT_CONFIG_DIR` and `WT_DATA_DIR`. The relocation test
  **failed before the fix** (the archive resolved to `$HOME/work/org/ai/`) and passes after it.
  It checks that a SHIPPED idea is archived next to the relocated ideas file and that
  `$HOME/work` never appears. The default-path and explicit-key tests passed both before and
  after, which shows no change for default installs. Full `uv run pytest`:
  **1190 passed, 82 skipped, 0 failed**.
- **Manual:** the leaking replay was repeated with a scratch config that sets only
  `org_ideas_file`: capture → summary → `wt idea state … SHIPPED`.
  `ideas-archive.org` was created in the scratch dir and contains the probe. The real
  `~/work/org/ai/ideas-archive.org` has an unchanged md5 and 0 occurrences of the probe.
- **As shipped:** matches Design; no logic change was needed in `ideas_archive_path`.

## Clock Log

Internal work: clock on IDEA-014 with `wt idea clock-in` / `clock-out`.

## Rollout / migration

Takes effect on the next run. A user who relocated `org_ideas_file` but wants archives at the
old location must set `org_ideas_archive_file` explicitly. Existing archive files are not
moved.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
