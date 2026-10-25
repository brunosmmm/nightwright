---
id: SPEC-0234
title: "Docs-only merge requests and conductor/steward skill routing"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, queue, merge, gates, conductor, steward, skills]
source_idea: IDEA-080
depends_on: [SPEC-0233]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), second half
of `IDEA-080`. [SPEC-0233](./0233-queue-merge-requests-branch-merges-as-campaigns.md) adds
`wt queue merge` and the `kind: "merge"` campaign. Its close re-runs **every** project gate.
Most steward merge requests are spec or doc edits. For this repo that means a full
`uv run pytest` (minutes), when only `spec-lint` can fail on such a change. The idea asks for
`--docs-only`: skip the heavy gates but still lint.

The conductor ([SPEC-0182](./0182-event-driven-wt-conduct-monitor-armed-conductor.md)) and the
steward ([SPEC-0184](./0184-wt-steward-skill-monitor-armed-project-steward-on.md)) skills
still tell agents to merge steward branches by hand or with a background shell job. Those are
the jobs that stalled invisibly on 2026-10-25.

## Goals / Non-goals

**Goals**
- G1. `wt queue merge BRANCH --docs-only` creates a merge campaign whose close re-gate runs only
  the gates the project marks `docs_only: true`.
- G2. A docs-only request is refused at plan time, and parked at close time, when the branch
  changes anything outside the docs paths. A code change can never merge on the light gates.
- G3. `skills/wt-conduct/SKILL.md` and `skills/wt-steward/SKILL.md` route every branch merge
  through `wt queue merge`. Shell-job merges are removed from both.

**Non-goals**
- Configurable docs paths per project. The fixed rule below fits every current project; a
  config key can follow from evidence.
- Changing which gates run for spec campaigns or for full merge requests.

## Decision

Add an optional per-gate key `docs_only: bool` (default `false`) to `.wt/project.yaml`
gates. For a merge campaign with `merge.docs_only: true`, `gates.run_all` runs only the gates
that have `docs_only: true`. `queue.is_docs_path(path)` defines docs paths as anything under
`docs/` or ending in `.md`. `plan_merge` refuses `--docs-only` when
`git.changed_files(<branch>, base)` contains another path. The close repeats the check on the
rebased tree before the re-gate and parks `not_docs_only` when it fails. This repo's
`.wt/project.yaml` marks `spec-lint` as `docs_only: true`.

## Design

### Project config (`project.py`)

- `GATE_DEFAULTS` gains `"docs_only": False`. `validate` accepts `gates[i].docs_only` as a bool
  through `_check_bool` (path `gates[<i>].docs_only`). Any other value is an error by key path,
  as for every gate key. `STARTER` documents it as a commented line under the example gate.
- This repo's `.wt/project.yaml`: the `spec-lint` gate gets `docs_only: true`.

### Docs-path rule (`queue.py`)

`is_docs_path(path: str) -> bool` returns `path.startswith("docs/") or path.endswith(".md")`.
Generated docs (`docs/LEDGER.md`) are inside `docs/`.

### Planning and CLI

- `plan_merge(..., docs_only=False)`. With `docs_only`, `git.changed_files` is called with the
  worktree (or `root` for a not-yet-created worktree, diffing `base...<branch>`). Any path
  where `is_docs_path` is false raises ValueError: `--docs-only refused: <n> non-docs paths
  (<first 5>)`. When the project has no gate with `docs_only: true`, the plan carries
  `warnings: ["no docs_only gates: the merge runs no gates"]` and is still allowed. The spec
  linter is a gate only where the project says so.
- The plan and `state.merge.docs_only` carry the flag. The `open` event gains the extra
  `docs_only`.
- `wt queue merge --docs-only`. The text plan prints `gates: <names> (docs-only)`, or the
  warning.

### Gates and close

- `gates.run_all(project_cfg, campaign_state, cfg, *, seq)`: when
  `(campaign_state.get("merge") or {}).get("docs_only")` is true, the gate list is filtered to
  `g.get("docs_only")`. `GateRun` and the sidecars are unchanged, so the `gate` ledger event
  lists only the gates that ran.
- Close (SPEC-0233's merge path in `prepare_close`), after the rebase and before the re-gate:
  for a docs-only merge, recompute `git.changed_files(worktree, target_sha)`. Any non-docs
  path returns a park `not_docs_only` with `resume_to: QUEUED` and a detail listing the
  paths. The requester then re-queues without `--docs-only`, or `wt queue resume` after
  splitting the branch. `PARK_REASONS` gains `not_docs_only`. Its SPEC-0222 owner (when that
  map exists) is `conductor`.

### Skills

- `skills/wt-conduct/SKILL.md`: the decision-table row for a steward's merge request (mailbox
  `merge` ask) becomes `wt queue merge <branch> --by steward:<Project> [--docs-only] --yes`.
  New rows: `gates_failed` / `worktree_dirty` / `not_docs_only` on a merge campaign ⇒ mail
  the requester (`merge.requested_by`) with the park detail and wait. The conductor no longer
  fixes the branch itself. The `rebase_conflict` row gains: on a merge campaign, the resolver
  sub-agent works in `repo.worktree` and the campaign resumes to `CLOSING` (merge campaigns
  have no GATING, SPEC-0233). Every instruction to merge a branch with shell commands or a
  background job is removed. The conductor's own branches (spec edits) also go through
  `wt queue merge --by conductor`.
- `skills/wt-steward/SKILL.md`: "to land a branch, ask the conductor for a queue merge (or
  run `wt queue merge <branch> --by steward:<Project>` yourself when the conductor is not
  running)". Never merge into `main` by hand.

## Alternatives considered

- **A `merge.docs_gates: [names]` list in the merge block**: rejected. A per-gate flag keeps
  the gate's definition in one place, and a renamed gate cannot silently fall out of the list.
- **Skipping gates entirely for docs-only** (the idea's "GATING skipped"): rejected. The idea
  itself says docs-only "still lints". The flag lets each project name its lint.
- **Trusting the requester's `--docs-only` without a diff check**: rejected. A wrong flag would
  merge code on the light gates. Both checks are cheap.

## Acceptance criteria

- [ ] `.wt/project.yaml` validation accepts `gates[i].docs_only: true|false` and rejects a
      non-bool with the key path. The default is `false`. This repo's `spec-lint` gate sets
      it to `true`.
- [ ] `plan_merge(docs_only=True)` refuses a branch that changes a non-docs path (message
      names the count and up to 5 paths). It accepts `docs/**` and `*.md` changes, and warns
      when the project has no `docs_only` gate.
- [ ] A docs-only merge campaign's close re-gate runs only the `docs_only` gates. The `gate`
      ledger event lists only those, and the campaign reaches MERGED.
- [ ] A docs-only merge whose rebased diff contains a non-docs path parks `not_docs_only`
      (`resume_to: QUEUED`) before any gate runs.
- [ ] `skills/wt-conduct/SKILL.md` and `skills/wt-steward/SKILL.md` route branch merges through
      `wt queue merge`, contain the three merge-park rows and the merge-campaign
      `rebase_conflict` resume to `CLOSING`, and no longer describe merging a branch with
      shell commands or a background job.
- [ ] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_project.py`: `docs_only` default, bool accepted, a non-bool rejected
    with `gates[0].docs_only` in the message.
  - `tests/test_agentic_queue_merge.py`: `plan_merge(docs_only=True)` refusal and acceptance,
    the no-docs-gate warning, `is_docs_path` cases (`docs/x`, `README.md`, `src/x.py`,
    `docs.py`).
  - `tests/test_agentic_gates.py`: `run_all` filters for a docs-only merge state and runs
    everything otherwise.
  - `tests/test_agentic_e2e.py`: docs-only merge to MERGED where the `pytest`-like gate (a
    command that would fail) is not run. A base that gains a code change during the queue
    wait does not trigger `not_docs_only`, because the diff is taken against the target. A
    branch amended with a code file after queueing parks `not_docs_only`.
  - `tests/test_skills.py` (or the existing skill-content test): both skills mention
    `wt queue merge`, and the conduct skill names the three park reasons.
- **Manual verification:** `uv run wt queue merge <a docs-only branch> --docs-only` (dry run)
  lists `spec-lint` as the only gate. A dry run on a branch touching `src/` is refused.
- **Regression guard:** full `uv run pytest`. Gate runs for spec campaigns and full merges are
  unchanged (existing gate and e2e tests).

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

Ship with this repo's `.wt/project.yaml` change. Other projects get no `docs_only` gates until
they opt in. Their docs-only merges then warn and run no gates, which the plan says up front.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
