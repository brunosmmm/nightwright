---
id: SPEC-0177
title: "wt-conduct skill, docs and dogfood bootstrap for wt itself"
status: superseded
superseded_by: SPEC-0182
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, skills, docs, dogfood]
source_idea: IDEA-024
depends_on: [SPEC-0176]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-024`).

With SPEC-0170–0176 in place the loop exists but nothing starts it, this repo has no
`.wt/project.yaml`, and the product docs do not mention any of it. The plan's conductor is
"a session that runs exclusively to manage the queue and never blocks". Under the decision
that wt owns the loop, the conductor is a thin skill: it starts `wt queue run` detached when
no live lease exists, watches `wt queue list`, deals with parked items, keeps idea states in
sync, and never implements anything itself. This spec also commits wt's own project file so
the first real campaign can run against this checkout.

## Goals / Non-goals

**Goals**
- `skills/wt-conduct/SKILL.md` that passes the thinness guard and tells an agent exactly
  how to start, watch, and unpark the loop with wt commands.
- `.wt/project.yaml` for this repo: gates `uv run pytest -q -x` and `python3
  tools/spec_lint.py`, policy `merge`, base `main`, worktree setup `uv sync --extra tui`,
  context files `AGENTS.md` and `docs/specs/SCHEMA.md`, allowed tools for `uv run wt *`.
- Docs: the capability page completed, agent-surface rows for the new schema ids and the
  skill, WORKFLOW and README pointers, a `M9` note in the ledger header.
- The first real campaign on this machine, recorded.

**Non-goals**
- `/wt-steward`, the mailbox, and any conductor logic beyond start / watch / unpark /
  reconcile (later waves).
- A systemd unit (the user chose a detached process).

## Decision

Ship the skill, the project file and the docs together, then run one real campaign on a
trivial accepted spec with the balanced profile and record its id, loops and cost here.

## Design

### `skills/wt-conduct/SKILL.md`

Frontmatter `name: wt-conduct`, `description` with a "Use when…" clause, `trigger`.
Sections (same shape as the other skills):

- **When to use:** the human asks to run, watch or unblock the agentic queue.
- **Procedure:** (1) `wt queue list --json`; if `lease.held` is false, start the service per
  `agentic.run_launcher` (`nohup wt queue run >> <data_dir>/agentic/runner.log 2>&1 &`
  by default; `tmux new -d -s wt-queue 'wt queue run'`; or run it in the foreground when
  told to); (2) report the items, states and costs; (3) for each `PARKED` item read
  `wt campaign show` and `wt campaign log --tail 20`, then choose `wt queue resume` (with
  `--note`, `--raise-cap`, `--reset-loops`), `wt queue abandon`, or escalate to the human
  with the park reason and the log tail; (4) after merges run `wt spec reconcile --apply`
  so ideas move to PROMOTED/SHIPPED; (5) `wt queue add SPEC_ID --yes` only when the human
  names a spec. Exact flags via `wt queue --help` and `wt campaign --help`.
- **Conventions / guardrails:** only one conductor (the lease enforces it; never kill a
  live holder without the human); never implement or edit code in a campaign worktree;
  never `--force` past a park without saying why; a stale lease (`stale: true`) is
  reported, not taken over; the shared ideas file rule (verify headings before writes).
- **Verify:** `wt queue list` shows the lease held and items progressing; parked items
  have a decision logged (`resume`/`abandon` events or a human escalation).

Add `wt-conduct` to `tests/test_skills.py::EXPECTED_SKILLS`.

### `.wt/project.yaml` (repo root)

```yaml
schema: wt.project.v1
project: Meta-Tools
base: main
gates:
  - {name: pytest, cmd: uv run pytest -q -x, timeout_s: 1200, required: true}
  - {name: spec-lint, cmd: python3 tools/spec_lint.py, timeout_s: 120, required: true}
merge: {policy: merge, base: main, push_after_merge: false, delete_branch: true}
worktrees: {setup: ["uv sync --extra tui"], cleanup: on_merge}
agents:
  context_files: [AGENTS.md, docs/specs/SCHEMA.md]
  allowed_tools: ["Bash(uv run wt *)", "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)"]
```

`tests/test_agentic_dogfood_config.py` loads it with `project.load_project_config` and
asserts both gates and the merge policy.

### Docs

- `docs/product/capabilities/agentic.md`: complete page (concepts, project file, profiles,
  state machine, commands, parks, the conductor); linked from `capabilities/index.md`;
  present in `mkdocs.yml` nav.
- `docs/product/guides/agent-surface.md`: skill map row for `/wt-conduct`; catalog rows
  for `wt.queue.v1`, `wt.queue.tick.v1`, `wt.campaign.v1`, `wt.campaign.log.v1`,
  `wt.campaigns.v1`, `wt.project.config.v1`.
- `docs/WORKFLOW.md` and `README.md`: a short pointer to the capability page.
- `docs/LEDGER.md` header: nothing to hand-edit (milestones are a generated column).

## Alternatives considered

- **A `wt conduct` command instead of a skill** — rejected. Starting a detached process
  and judging parked items is agent work; the deterministic parts are already commands.
- **systemd user unit** — rejected by the user's decision (portability); the lease
  detects a dead service, and `/wt-conduct` restarts it.
- **Merge policy `pr` for wt itself** — rejected by the user's decision; the close step's
  re-gate and preconditions are the safety net.

## Acceptance criteria

- [x] `tests/test_skills.py` passes with `wt-conduct` installed by `wt skills install`
      (symlinked like the others); the skill names `wt queue --help` and
      `wt campaign --help` and has no fenced flag blocks.
- [x] `wt projects config Meta-Tools --json` resolves the committed file and
      `load_project_config` accepts it; the dogfood config test passes.
- [x] `mkdocs build --strict` passes with the completed capability page; the reference,
      capabilities, guides and agent-surface guards pass.
- [x] **Manual:** on this machine, with the balanced profile, `wt queue add <trivial
      accepted spec> --yes` and `/wt-conduct` (service started detached) take the campaign
      to `MERGED`; `wt campaign log` shows the full event trail; the live `wt` still works
      afterwards; campaign id, loops, runs and total cost recorded in this spec.
- [x] After that merge, `wt spec reconcile --apply` moves the spec's idea to PROMOTED.
- [x] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** existing skills / docs / completion guards plus
  `tests/test_agentic_dogfood_config.py`.
- **Manual verification:** the first real campaign (above). The trivial spec is the wave-2
  canary (`wt spec status` setter) if it is accepted by then, otherwise a one-line docs
  spec created for the purpose. Record: campaign id, `loops`, `runs`, `cost.total_usd`,
  wall time, and anything that parked.
- **Regression guard:** full `uv run pytest` before and after the real campaign's merge.

### Test record (2026-10-25)

- **Automated:** `tests/test_skills.py` (with `wt-conduct`), `tests/test_agentic_dogfood_config.py`,
  docs guards and `mkdocs build --strict` pass; full `uv run pytest` on main after the
  merge of this spec: 1453 passed, 1 skipped, 0 failed. `wt skills install` linked
  `wt-conduct` into `~/.claude/skills`.
- **First real campaign:** `SPEC-0179-20261025-158c` (canary `wt spec status`), balanced
  profile with `slots: 1`. First attempt parked `triage_escalated` because every
  `claude -p` call returned HTTP 429 `usage_limit_reached` (3 runs, $0). After the account
  limit reset: `wt queue resume … --to IMPLEMENTING --reset-loops --yes`, loop started with
  `setsid nohup wt queue run`. One implementer run (sonnet, 190 s, **$0.301**, verdict ok),
  gates green (113 s), close re-gated on the rebased tree (114 s), **MERGED** fast-forward
  as `6a0eefa`; 0 fix loops, 4 runs total (3 failed on the 429), total cost $0.30. The
  re-gate is `uv run pytest -q -x` + lint on the exact merged tree, so main was verified
  green by the loop itself; `wt spec verify SPEC-0179` ok afterwards; live `wt` works.
- `wt spec reconcile --apply` moved IDEA-025 SPECCED → PROMOTED.
- **As shipped:** the spec-lint gate regenerates the ledger before linting; the skill
  starts the loop with `setsid nohup`. Findings filed: transient API errors (429) should be
  retried, not counted as triage attempts; heartbeat should tick during runs; the
  conductor must be event-driven (superseding design follows in a new spec).

## Clock Log

Internal work: clock on IDEA-024 with `wt idea clock-in` / `clock-out`.

## Rollout / migration

Committing `.wt/project.yaml` changes nothing until a campaign is queued. The skill is
installed with `wt skills install` (symlink; no reinstall needed).

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
