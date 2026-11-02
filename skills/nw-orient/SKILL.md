---
name: nw-orient
description: "One-read bootstrap for the nightwright work-tracking CLI. Use at the start of a fresh session when you need to drive nightwright (capture ideas, promote them to specs, generate tasks, export) but haven't read its code yet."
trigger: orienting to nightwright / starting work in a repo that uses nightwright
---

# nw-orient

## When to use

First thing in a fresh session, whenever you're about to do anything with `nw` (the passive
work-time tracker + idea→spec→task pipeline) and don't already have context on it. Read this
once instead of `src/nightwright/`.

## Working rules (all nightwright sessions)

These apply to every session that touches nightwright — conductor, steward, builders, humans' agents.

- **Always work in a git worktree** (`git worktree add ../nw-<topic> -b <branch>`); never edit
  the main checkout in place. The global `nw` is typically an editable install of it, so a
  half-done edit breaks the live tool for every session (happened 2026-10-25). Land work with
  an `--ff-only` merge only when the worktree's gates (`uv run pytest`,
  `python3 tools/spec_lint.py`) are green, and keep the main checkout clean so queue
  campaigns can close.
- **Do real work through background sub-agents** — specs, implementation, investigations,
  rebases, merges, conflict resolution. The main session stays responsive for events and
  decisions. Dispatch work to fresh sub-agents (general-purpose, or Explore for read-only search) with a self-contained brief and a model chosen per task (sonnet for routine work, opus only for hard design or review). Never use forks — they inherit the whole conversation and cost an order of magnitude more; write any needed context into the brief. The brief names the governing spec path, the worktree, the
  files to read, the files the sub-agent owns, these rules, and what to report.
- **Missing-CLI rule:** a one-off python / cat / jq peek into nightwright's data means a `nw` command is
  missing — file an idea (`nw idea capture --kind improvement --project Meta-Tools …`,
  heading-verified) and carry on.
- **Shared ideas file:** idea ids are global across concurrent sessions — read the id from the
  capture output and verify the heading (`nw idea show ID --json`) before every write.
- **Decide, don't defer.** Resolve design details, defaults, thresholds, judgment calls,
  conflicts and blockers yourself, record the reasoning where it belongs (spec Design / As
  shipped, campaign note), and tune later from real evidence. Escalate to the human only
  decisions about what gets built (scope, priorities, product direction) or anything touching
  their own checkout. Never hand the human a list of parameters or judgment calls to adjust.
  **Only single-project sessions (a steward, the human's own session) may ask the human and
  wait. The conductor never does: it routes to `steward:<P>` or `nw queue escalate`.**

## What `nw` is

`nw` turns Claude transcripts + meetings into per-topic hours, and separately runs an
idea→explore→spec→task→export pipeline for turning a stray thought into shipped, tracked work:

```
idea  →  explore  →  accepted spec  →  (internal: generate → PROMOTED)
                                    →  (outbound: export → EXPORTED; portable tracks build)
```

- **Ideas** are short captures in an org file (`nw idea` / `nw ideas`).
- **Entry skills:** `/nw-capture` (one-liner) or `/nw-seed` (distill conversation → idea +
  optional Summary/Log).
- **Explore** enriches an idea in place (Summary / questions / Log) via `nw idea log|…`
  and `/nw-explore` — no promotion yet.
- **Specs** are markdown design docs under `docs/specs/` (internal) or the data-dir outbox
  (outbound), governed by `docs/specs/SCHEMA.md` and `AGENTS.md`.
- **Tasks** are org headlines from an accepted **internal** spec (`nw spec generate` →
  PROMOTED).
- **Export** files an outbound spec into its target repo (`nw spec export` → EXPORTED). The
  portable file there is the working copy for build; optional `nw spec pull-status`.

## Where state lives

- Org files (`org_files` / `org_ideas_file` in config) — ideas, tasks, agenda.
- `docs/specs/` — internal specs; `docs/LEDGER.md` index.
- Outbox under the nightwright data dir — outbound specs for other repos.
- Human cheatsheet: `docs/WORKFLOW.md`.

## Procedure

1. `nw --help` and `nw <group> --help` for command surface.
2. Day-to-day skills: `nw-capture` / `nw-seed` → `nw-explore` → `nw-new-work` →
   (`nw-generate` if internal, `nw-export` if outbound); `nw-related` before capture/promote
   when the thought may already exist; after build, **`/nw-verify`** (global skill: internal
   `SPEC-NNNN` via `nw spec verify`, outbound portable via file audit in the target repo);
   `nw-rework` when a linked design is wrong or an epic still needs children.
   `/nw-implement-spec` is for *building* outbound portables — it hands off to `/nw-verify`
   to close the loop.
3. Triage in-flight work: `nw hub --json` (`nw.hub.v1` — ideas + active internal + outbound).
   No MCP server; skills + CLI `--json` / hub are the agent surface (SPEC-0053).
4. **Multi-project gate (SPEC-0095):** if the conversation names ≥2 tracked projects or repo
   paths, run `nw projects --json` (`nw.projects.v1`) early, preview a project map, and fan out
   as an **epic + per-project children** — Breakdown bullets carry `(project: Name)`
   (SPEC-0097); do not collapse into one `:PROJECT:` / one `research.root`. Soft-warn if you
   continue a single-root explore anyway.
5. **Missing project (SPEC-0137):** never invent `outbox_targets` by hand-editing YAML. Show
   a dry-run plan with `nw projects add NAME [--repo PATH]`, get human agreement, then
   `… --yes`. Re-run `nw projects --json`. Stub (no `--repo`) registers the name;
   `--repo` sets research/outbound. `outbound` in JSON means non-empty `repo_path`.
6. Spec substance conventions: `AGENTS.md` + `docs/specs/SCHEMA.md`.
7. **Coexistence with Superpowers (SPEC-0138):** Claude’s Superpowers plugin may inject
   brainstorming/TDD skills into Cursor even when the model is not Claude Code. When nightwright
   governs the work (project in `nw projects --json`, or repo `AGENTS.md` + specs/outbox):

   | Concern | Owner |
   |---------|--------|
   | Capture / explore / promote / export / verify / next / hub | **nightwright** |
   | Governing design artifact | **nightwright** portable or `docs/specs/` — **not** `docs/superpowers/**` |
   | Brainstorm Q&A, TDD, systematic debug (inside a slice) | Superpowers OK as *tactics* |
   | Claiming `done` | **`/nw-verify`** (+ outbound `pull-status`) — Superpowers “verification” alone is not enough |

   - **Priority:** nightwright lifecycle skills win over Superpowers process skills for *what work
     exists* and *whether it is done*.
   - **Hard gate:** before the first feature `Write`/`StrReplace` for new behavior, ensure the
     project is registered and there is a governing idea (non-empty Summary) or an
     accepted/in-progress spec/portable — unless the human **explicitly** waives nightwright for this
     slice. “Just build” / “keep working” is **not** a waive: run `nw hub --json` / `nw next`
     and continue that item (or promote a thin accepted slice first).
   - **Path forbid:** do not write governing design to `docs/superpowers/specs` or
     `docs/superpowers/plans` when nightwright is in play. Optional non-governing notes may go under
     `docs/design/` if the repo already uses that.

## Verify

You can name the pipeline stages (including explore), where ideas/specs/tasks live, and which
`nw-*` skill covers the transition you're about to make. Prefer `nw <cmd> --help` over guessing.
Know that multi-repo chats need `nw projects --json` + fan-out before Summary, and that new
projects are registered with `nw projects add` (dry-run then `--yes`), not by editing config.
Know that Superpowers must not replace nightwright lifecycle or governing docs when nightwright is in play
(SPEC-0138).
