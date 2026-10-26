---
id: SPEC-0234
title: "Docs-only merge requests and conductor/steward skill routing"
status: done
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
`wt queue merge` and the `kind: "merge"` campaign. Its close re-runs **every** project gate. For
this repo that means a full `uv run pytest` (minutes). The idea asks for `--docs-only`, a
lighter path for changes no gate can break.

The idea's first framing ("docs paths still lint") does not hold in this repo, because the
gates read most of `docs/` and many `*.md` files:
- `tools/spec_lint.py` and pytest read `docs/specs/**` and `docs/LEDGER.md`;
- `tests/test_docs_*.py` and the mkdocs build read `docs/product/**` and `README.md`;
- `tests/test_skills.py` reads `skills/*/SKILL.md`;
- `src/wt/agentic/personas/*.md` are packaged runtime prompts.

A path-shape rule (`docs/` or `*.md`) would let any of these merge without the gate that checks
them. Only paths that **no gate reads** can skip the gates, and only the project knows which
those are.

The conductor ([SPEC-0182](./0182-event-driven-wt-conduct-monitor-armed-conductor.md)) and the
steward ([SPEC-0184](./0184-wt-steward-skill-monitor-armed-project-steward-on.md)) skills
still have the conductor complete merges through sub-agents or background shells. Those are
the jobs that stalled invisibly on 2026-10-25.

## Goals / Non-goals

**Goals**
- G1. A project can declare an explicit allowlist of docs-only paths in `.wt/project.yaml`
  (`merge.docs_only_paths`, default empty, which means docs-only is disabled).
- G2. `wt queue merge BRANCH --docs-only` is accepted only when every path the branch changes
  is on the allowlist. Rename detection is off, so a deleted source path always counts. The
  close repeats the check on the rebased tree and then merges without running gates.
- G3. `skills/wt-conduct/SKILL.md` routes steward merge requests through `wt queue merge`, and
  `skills/wt-steward/SKILL.md` tells the steward to request merges from the conductor.

**Non-goals**
- Glob patterns. Entries are exact paths or directory prefixes. Globs can follow from evidence.
- A lighter gate subset (per-gate flags). By definition no gate reads an allowlisted path, so
  running any gate on such a change tests nothing the branch changed.
- Changing which gates run for spec campaigns or for full merge requests.

## Decision

Add `merge.docs_only_paths: [str]` (default `[]`) to the project config. Each entry is an exact
repo-relative path (`docs/WORKFLOW.md`) or a directory prefix ending in `/` (`docs/design/`).
`queue.docs_only_violations(paths, allow) -> list[str]` returns the paths not covered.
`plan_merge(..., docs_only=True)` refuses when the allowlist is empty or when
`gitops.changed_files(root, base, head=branch, renames=False)` (that is,
`git diff --name-only --no-renames base...branch`) has any violation. The close checks the
rebased tree again, parks `not_docs_only` on a violation, and otherwise skips the re-gate and
merges. This repo's allowlist is `docs/design/` and `docs/WORKFLOW.md`. Spec files,
`docs/LEDGER.md`, `docs/product/`, `README.md`, skills and persona templates are **not**
docs-only, because `spec_lint` or pytest read them.

## Design

### Project config (`project.py`)

- `PROJECT_DEFAULTS["merge"]` gains `"docs_only_paths": []`. `validate` checks
  `merge.docs_only_paths` with `_check_str_list` and also rejects:
  - absolute paths;
  - entries containing `..`, `*`, `?` or `[`;
  - the empty string.

  Each error names its key path (`merge.docs_only_paths[<i>]`). `STARTER` gets the commented
  line `  docs_only_paths: []  # paths no gate reads; enables wt queue merge --docs-only`.
- This repo's `.wt/project.yaml`: `docs_only_paths: [docs/design/, docs/WORKFLOW.md]`. Why:
  `grep -rn` over `tests/`, `src/` and `tools/` finds no reader of either. `docs/design/` holds
  prototypes and `*.py` scratch that pytest does not collect (`testpaths = ["tests"]`), and
  mkdocs builds only `docs/product`. A later test that reads one of them must remove it from
  the list. That is noted in a YAML comment next to the key.

### Path rule (`queue.py`, `gitops.py`)

- `docs_only_violations(paths, allow) -> list[str]` keeps the paths `p` for which no entry `e`
  satisfies `p == e` or (`e.endswith("/")` and `p.startswith(e)`).
- `gitops.changed_files(repo, base, head="HEAD", *, renames=True)` gains `head` and `renames`.
  With `renames=False` it passes `--no-renames`. The existing caller
  (`personas.py:438`) keeps the defaults. With `--no-renames`, a move from `src/x.py` to
  `docs/design/x.py` lists both paths, so the deletion of `src/x.py` is a violation.

### Planning and CLI

- `plan_merge(..., docs_only=False)`. With `docs_only`:
  1. `pcfg.merge.docs_only_paths` must be non-empty, otherwise ValueError `--docs-only is
     disabled for <project>: set merge.docs_only_paths in .wt/project.yaml`;
  2. `violations = docs_only_violations(changed_files(root, base, head=branch,
     renames=False), allow)`. Any violation raises ValueError `--docs-only refused: <n> paths
     outside merge.docs_only_paths (<first 5>)`. Diffing from `root` with `head=branch` is
     the single code path, whether or not the worktree exists yet.
- The plan carries `docs_only: true` and `docs_only_paths` (the allowlist used).
  `state.merge.docs_only` is set, and the `open` event gains the extra `docs_only`. Both are
  part of `wt.queue.merge.v1` (`--json`).
- `wt queue merge --docs-only`. The text plan prints `gates: none (docs-only; <n> paths within
  merge.docs_only_paths)`.

### Close (SPEC-0233's merge path in SPEC-0220 `prepare_close`)

For a merge campaign with `merge.docs_only`, after the rebase (step 4) and instead of the
re-gate (step 5):
- Recompute `docs_only_violations(changed_files(worktree, target_sha, renames=False), allow)`,
  with `allow` re-read from the current project config. Diffing against the target means
  commits that landed on base meanwhile never count. Any violation returns a park
  `not_docs_only` with `resume_to: QUEUED`. The detail lists up to 10 paths and ends with
  the recovery: `abandon this campaign (wt queue abandon <cid> --yes), then wt queue merge
  <branch> --yes without --docs-only`. A plain resume would only park again.
  `PARK_REASONS` gains `not_docs_only`. Its SPEC-0222 owner, when that map exists, is
  `conductor`.
- No violation: no gate runs and no `gate` event is written. The prepared result records the
  rebased head as the head to merge, so SPEC-0220's gated-tree check (step 4) compares against
  the tree the docs-only check ran on. The `close` event has `gates_seq: null` and the extra
  `docs_only: true`. `close` requires only `mode`, so `schemas.ledger_problems` accepts it.
- The SPEC-0233 abandon rule applies: abandoning a parked docs-only campaign with an adopted
  worktree removes neither the worktree nor the branch. That is what makes the documented
  recovery safe.

### Skills

- `skills/wt-conduct/SKILL.md`:
  - The `message` (`kind` request) row's action list "(add, prioritize, park, resume)" becomes
    "(add, merge, prioritize, park, resume)". Its text gains: "a merge request runs `wt queue
    merge <branch> --by steward:<Project> [--docs-only]` (dry run, then `--yes`); never merge a
    branch any other way".
  - The *Sub-agent* bullet's phrase "completing or retrying a merge," is deleted. Merges are
    queue work, so no sub-agent completes them.
  - The guardrail "Conflict resolution, merge repair and fixes are done by sub-agents" becomes
    "Conflict resolution and fixes are done by sub-agents…; branch merges, including your own
    spec edits, go through `wt queue merge --by conductor`".
  - New decision-table rows:
    - `parked` `gates_failed` / `worktree_dirty` on a merge campaign: mail the requester
      (`merge.requested_by`) the park detail and wait. Never fix the branch yourself.
    - `parked` `not_docs_only`: abandon the campaign, re-run `wt queue merge <branch>` without
      `--docs-only`, and tell the requester.
  - The `parked` `rebase_conflict` row gains: "on a merge campaign, the resolver sub-agent works
    in `repo.worktree` and the campaign resumes to `CLOSING` (merge campaigns have no GATING,
    SPEC-0233)".
- `skills/wt-steward/SKILL.md`, add-only: procedure step 5's request examples gain `"merge
  <branch>"` / `"merge <branch> docs-only"`. A guardrail bullet says: "Never merge into
  `main` by hand or with a background job. Ask the conductor for a queue merge."

## Alternatives considered

- **A fixed `docs/` or `*.md` rule** (the first draft): rejected by review. In this repo it lets
  spec, ledger, product-doc, skill and persona-prompt changes merge with no pytest or spec-lint
  run, and those gates read exactly those files.
- **Glob patterns in the allowlist**: deferred. Exact paths and prefixes are enough for the one
  configured project and keep the match rule obvious.
- **A per-gate `docs_only` flag selecting a light gate subset**: rejected. With an allowlist
  of paths no gate reads, a light subset has nothing to check. A project that wants a check
  shortens its allowlist.
- **Trusting the requester's `--docs-only` without a diff check**: rejected. A wrong flag would
  merge code with no gates. Both checks are cheap.

## Acceptance criteria

- [x] `.wt/project.yaml` validation accepts `merge.docs_only_paths` (default `[]`) and rejects
      a non-list, a non-string, an absolute path, `..`, glob characters and an empty entry,
      each with its key path. `STARTER` documents the key. This repo sets `[docs/design/,
      docs/WORKFLOW.md]`.
- [x] `docs_only_violations` matches exact entries and `/`-terminated prefixes only.
      `gitops.changed_files(..., head=branch, renames=False)` lists both sides of a rename.
- [x] `plan_merge(docs_only=True)` refuses when the allowlist is empty and refuses a branch
      with any path outside it (count and up to 5 paths). In this repo that includes
      `docs/specs/x.md`, `docs/LEDGER.md`, `README.md`, `skills/x/SKILL.md`,
      `src/wt/agentic/personas/x.md`, and a rename of `src/x.py` to `docs/design/x.py`.
      It accepts a branch that touches only `docs/design/` and `docs/WORKFLOW.md`.
- [x] `wt queue merge --docs-only --json` (`wt.queue.merge.v1`) carries `docs_only` and
      `docs_only_paths`. The state has `merge.docs_only`, the `open` event carries
      `docs_only`, and the text plan prints the `gates: none (docs-only; …)` line.
- [x] An allowlisted docs-only merge reaches MERGED with no gate run and no `gate` event. Its
      `close` event has `gates_seq: null` and `docs_only: true` and passes
      `schemas.ledger_problems`.
- [x] A docs-only merge whose rebased diff violates the allowlist parks `not_docs_only`
      (`resume_to: QUEUED`) before merging, with the abandon-and-re-queue recovery in the
      detail. Following that recovery reaches MERGED through the full gates.
- [x] `skills/wt-conduct/SKILL.md` has `merge` in the request row with the `wt queue merge`
      command, the `gates_failed` / `worktree_dirty` / `not_docs_only` rows, and the
      merge-campaign `rebase_conflict` resume to `CLOSING`. It no longer contains "completing
      or retrying a merge" or "merge repair". `skills/wt-steward/SKILL.md` contains the merge
      request example and the never-merge-by-hand guardrail.
- [x] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_project.py`: `docs_only_paths` default and every rejection, each with
    its key path; this repo's `.wt/project.yaml` loads with the two entries; `STARTER`
    contains `docs_only_paths`.
  - `tests/test_agentic_gitops.py`: `changed_files` with
    `head=` and `renames=False` on a temp repo with a rename lists both paths.
  - `tests/test_agentic_queue_merge.py`:
    - `docs_only_violations` cases: exact, prefix, `docs/designer.md` not under `docs/design/`;
    - every `plan_merge(docs_only=True)` refusal and acceptance listed in the criteria, against
      a temp repo whose config mirrors this repo's allowlist;
    - `--json` and text plan output, state and `open` event keys.
  - `tests/test_agentic_close.py`: the docs-only close with no `gate` event, `close` with
    `gates_seq: null`, and `ledger_problems` empty. A violation parks `not_docs_only` before
    `ff_merge` (spy).
  - `tests/test_agentic_e2e.py`:
    - a docs-only merge to MERGED where the project's gate is a failing command, which proves
      no gate ran;
    - a code change landing on base during the queue wait does not trigger `not_docs_only`;
    - a branch amended with `src/x.py` after queueing parks `not_docs_only`, then
      `wt queue abandon --yes` and `wt queue merge` without `--docs-only` reach MERGED with
      the gates run, and the adopted worktree survives the abandon.
  - `tests/test_skills.py`:
    - the conduct skill contains `wt queue merge`, the three park reasons, and a
      `rebase_conflict` merge-campaign line naming `CLOSING`;
    - negative assertions: no "completing or retrying a merge", no "merge repair";
    - the steward skill contains `merge <branch>` and "Never merge into `main` by hand".
- **Manual verification:** `uv run wt queue merge <branch touching docs/WORKFLOW.md>
  --docs-only` (dry run) prints `gates: none (docs-only; 1 paths …)`. The same on a branch
  touching `docs/specs/` is refused.
- **Regression guard:** full `uv run pytest`. Gate runs for spec campaigns and full merges are
  unchanged (existing gate and e2e tests). `changed_files`' existing caller keeps its
  behaviour.

### Test record (2026-10-25)

- `uv run pytest` (full suite): 2482 passed, 1 skipped.
- New tests: `test_agentic_project.py` (default, every rejection with its key path, this repo's
  allowlist, `STARTER`), `test_agentic_gitops.py` (`changed_files` with `head=` and
  `renames=False` listing both sides of a rename; `docs_only_violations` exact / prefix),
  `test_agentic_queue_merge.py` (every `plan_merge(docs_only=True)` refusal and acceptance,
  rename out of `src/`, JSON and text plan, state and `open` event, and the three e2e cases),
  `test_agentic_close.py` (no gate and no `gate` event, `gates_seq: null`; a violation parks
  `not_docs_only` before `ff_merge`; the allowlist is re-read), `test_skills.py` (routing rows,
  negative assertions).
- Manual: `uv run wt queue merge <branch touching docs/WORKFLOW.md> --docs-only` (dry run)
  printed `gates: none (docs-only; 1 paths within merge.docs_only_paths)`; the same on a
  branch touching `docs/specs/zz.md` was refused with `--docs-only refused: 1 paths outside
  merge.docs_only_paths (docs/specs/zz.md)`. (Throwaway branches built with git plumbing,
  deleted afterwards.)

### As shipped (2026-10-25)

- `docs_only_violations` lives in `gitops.py` (the close shares it without importing `queue`);
  `queue.docs_only_violations` is the same function re-exported.
- The text plan's `gates: none (docs-only; …)` line comes from a `gates` key in the plan
  mapping, so it is also in the `wt.queue.merge.v1` JSON (only for `--docs-only`).
- The e2e cases are in `tests/test_agentic_queue_merge.py` (next to the merge-request helpers)
  instead of `tests/test_agentic_e2e.py`.
- The `not_docs_only` park is also documented in `docs/product/loop/operate.md`.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

Ship with this repo's `.wt/project.yaml` change. Every other project has an empty allowlist,
so `--docs-only` is refused there with a message naming the key until the project opts in.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
