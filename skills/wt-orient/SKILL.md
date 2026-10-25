---
name: wt-orient
description: "One-read bootstrap for the wt work-tracking CLI. Use at the start of a fresh session when you need to drive wt (capture ideas, promote them to specs, generate tasks, export) but haven't read its code yet."
trigger: orienting to wt / starting work in a repo that uses wt
---

# wt-orient

## When to use

First thing in a fresh session, whenever you're about to do anything with `wt` (the passive
work-time tracker + idea→spec→task pipeline) and don't already have context on it. Read this
once instead of `src/wt/`.

## Working rules (all wt sessions)

These apply to every session that touches wt — conductor, steward, builders, humans' agents.

- **Always work in a git worktree** (`git worktree add ../wt-<topic> -b <branch>`); never edit
  the main checkout in place. The global `wt` is typically an editable install of it, so a
  half-done edit breaks the live tool for every session (happened 2026-10-25). Land work with
  an `--ff-only` merge only when the worktree's gates (`uv run pytest`,
  `python3 tools/spec_lint.py`) are green, and keep the main checkout clean so queue
  campaigns can close.
- **Do real work through background sub-agents** — specs, implementation, investigations,
  rebases, merges, conflict resolution. The main session stays responsive for events and
  decisions.
- **Missing-CLI rule:** a one-off python / cat / jq peek into wt's data means a `wt` command is
  missing — file an idea (`wt idea capture --kind improvement --project Meta-Tools …`,
  heading-verified) and carry on.
- **Shared ideas file:** idea ids are global across concurrent sessions — read the id from the
  capture output and verify the heading (`wt idea show ID --json`) before every write.

## What `wt` is

`wt` turns Claude transcripts + meetings into per-topic hours, and separately runs an
idea→explore→spec→task→export pipeline for turning a stray thought into shipped, tracked work:

```
idea  →  explore  →  accepted spec  →  (internal: generate → PROMOTED)
                                    →  (outbound: export → EXPORTED; portable tracks build)
```

- **Ideas** are short captures in an org file (`wt idea` / `wt ideas`).
- **Entry skills:** `/wt-capture` (one-liner) or `/wt-seed` (distill conversation → idea +
  optional Summary/Log).
- **Explore** enriches an idea in place (Summary / questions / Log) via `wt idea log|…`
  and `/wt-explore` — no promotion yet.
- **Specs** are markdown design docs under `docs/specs/` (internal) or the data-dir outbox
  (outbound), governed by `docs/specs/SCHEMA.md` and `AGENTS.md`.
- **Tasks** are org headlines from an accepted **internal** spec (`wt spec generate` →
  PROMOTED).
- **Export** files an outbound spec into its target repo (`wt spec export` → EXPORTED). The
  portable file there is the working copy for build; optional `wt spec pull-status`.

## Where state lives

- Org files (`org_files` / `org_ideas_file` in config) — ideas, tasks, agenda.
- `docs/specs/` — internal specs; `docs/LEDGER.md` index.
- Outbox under the wt data dir — outbound specs for other repos.
- Human cheatsheet: `docs/WORKFLOW.md`.

## Procedure

1. `wt --help` and `wt <group> --help` for command surface.
2. Day-to-day skills: `wt-capture` / `wt-seed` → `wt-explore` → `wt-new-work` →
   (`wt-generate` if internal, `wt-export` if outbound); `wt-related` before capture/promote
   when the thought may already exist; after build, **`/wt-verify`** (global skill: internal
   `SPEC-NNNN` via `wt spec verify`, outbound portable via file audit in the target repo);
   `wt-rework` when a linked design is wrong or an epic still needs children.
   `/wt-implement-spec` is for *building* outbound portables — it hands off to `/wt-verify`
   to close the loop.
3. Triage in-flight work: `wt hub --json` (`wt.hub.v1` — ideas + active internal + outbound).
   No MCP server; skills + CLI `--json` / hub are the agent surface (SPEC-0053).
4. **Multi-project gate (SPEC-0095):** if the conversation names ≥2 tracked projects or repo
   paths, run `wt projects --json` (`wt.projects.v1`) early, preview a project map, and fan out
   as an **epic + per-project children** — Breakdown bullets carry `(project: Name)`
   (SPEC-0097); do not collapse into one `:PROJECT:` / one `research.root`. Soft-warn if you
   continue a single-root explore anyway.
5. **Missing project (SPEC-0137):** never invent `outbox_targets` by hand-editing YAML. Show
   a dry-run plan with `wt projects add NAME [--repo PATH]`, get human agreement, then
   `… --yes`. Re-run `wt projects --json`. Stub (no `--repo`) registers the name;
   `--repo` sets research/outbound. `outbound` in JSON means non-empty `repo_path`.
6. Spec substance conventions: `AGENTS.md` + `docs/specs/SCHEMA.md`.
7. **Coexistence with Superpowers (SPEC-0138):** Claude’s Superpowers plugin may inject
   brainstorming/TDD skills into Cursor even when the model is not Claude Code. When wt
   governs the work (project in `wt projects --json`, or repo `AGENTS.md` + specs/outbox):

   | Concern | Owner |
   |---------|--------|
   | Capture / explore / promote / export / verify / next / hub | **wt** |
   | Governing design artifact | **wt** portable or `docs/specs/` — **not** `docs/superpowers/**` |
   | Brainstorm Q&A, TDD, systematic debug (inside a slice) | Superpowers OK as *tactics* |
   | Claiming `done` | **`/wt-verify`** (+ outbound `pull-status`) — Superpowers “verification” alone is not enough |

   - **Priority:** wt lifecycle skills win over Superpowers process skills for *what work
     exists* and *whether it is done*.
   - **Hard gate:** before the first feature `Write`/`StrReplace` for new behavior, ensure the
     project is registered and there is a governing idea (non-empty Summary) or an
     accepted/in-progress spec/portable — unless the human **explicitly** waives wt for this
     slice. “Just build” / “keep working” is **not** a waive: run `wt hub --json` / `wt next`
     and continue that item (or promote a thin accepted slice first).
   - **Path forbid:** do not write governing design to `docs/superpowers/specs` or
     `docs/superpowers/plans` when wt is in play. Optional non-governing notes may go under
     `docs/design/` if the repo already uses that.

## Verify

You can name the pipeline stages (including explore), where ideas/specs/tasks live, and which
`wt-*` skill covers the transition you're about to make. Prefer `wt <cmd> --help` over guessing.
Know that multi-repo chats need `wt projects --json` + fan-out before Summary, and that new
projects are registered with `wt projects add` (dry-run then `--yes`), not by editing config.
Know that Superpowers must not replace wt lifecycle or governing docs when wt is in play
(SPEC-0138).
