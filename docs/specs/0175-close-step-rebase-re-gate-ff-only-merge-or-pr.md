---
id: SPEC-0175
title: "Close step: rebase, re-gate, ff-only merge or PR hand-off"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, merge, git, safety]
source_idea: IDEA-022
depends_on: [SPEC-0172, SPEC-0173]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-022`).

Closing is where an autonomous loop can do real damage: the dogfood target is this repo,
and the user's global `wt` is an editable install of it, so a fast-forward of `main` changes
the live tool immediately. The user chose merge-to-main anyway, on the condition that the
merge is gated. This spec is that gate: rebase in the worktree, re-run every gate on the
rebased tree, audit the spec's Definition of Done mechanically, check the main checkout's
preconditions, and only then fast-forward. When the project's policy is a PR, it pushes the
branch and opens the PR with a body generated from the spec.

## Goals / Non-goals

**Goals**
- A pure-Python close procedure that never force-pushes, never rewrites `main`, never
  touches a dirty or off-base main checkout, and re-runs gates on the exact tree it merges.
- Spec DoD audited in the worktree through the existing verifier (`verify.verify_spec`,
  factored into `verify_spec_file(path)`), so a lazy implementer costs a loop, never a bad
  merge.
- Policy `pr` / `draft-pr`: push + `gh pr create` with a body built from the spec's
  Decision, its AC checklist, the gate table and campaign stats.
- Every outcome is a documented next state or park reason with the git output tail.

**Non-goals**
- Resolving rebase conflicts (later wave: resolver persona). Wave 1 parks.
- Touching idea states (`/wt-conduct` runs `wt spec reconcile --apply`).
- Pushing `main` after a merge by default (`push_after_merge: false`; configurable).

## Decision

Add `src/wt/agentic/close.py` with `close_campaign(cfg, state, project_cfg, *, gate_seq)
-> CloseOutcome` and `pr_body(...)`. It uses only SPEC-0172 `gitops`, SPEC-0173 `gates`,
and `verify.verify_spec_file`. Factor `verify_spec_file(path) -> dict` out of
`verify.verify_spec` (which then delegates), so the same mechanical checks run on a path
inside a worktree.

## Design

```python
@dataclass(frozen=True)
class CloseOutcome:
    next_state: str                   # MERGED | HANDED_OFF | TRIAGING | PARKED
    park: dict | None                 # {reason, detail, resume_to}
    close: dict | None                # {mode, merged_sha, pr_url, gates_seq, target_sha}
    gate_run: GateRun | None
    synthetic_gate: dict | None       # the spec-dod failure, when that is why we go to TRIAGING
    events: list[dict]                # ledger events for the caller to append (type, payload)
```

Algorithm (`policy`, `base`, `remote` from `project_cfg["merge"]`; `root`, `worktree`,
`branch` from `state["repo"]`):

1. **Spec DoD.** `verify_spec_file(worktree / spec_rel_path)`; require `ok` and
   `status == "done"`; otherwise `next_state="TRIAGING"` with `synthetic_gate = {name:
   "spec-dod", required: True, ok: False, tail: <failing checks>}` so the triager and the
   next implementer see exactly what is missing. (Outbound portable specs use the same
   function on the portable file; their `kind` hint comes from frontmatter.)
2. **Worktree clean.** `gitops.commit_all(worktree, "wt(<id>): close-time snapshot")` if
   anything is uncommitted; record a `commit` event.
3. **Target.** `merge`: `target_sha = rev_parse(root, base)`; `pr`/`draft-pr`:
   `fetch(root, remote, base)`, `target = f"{remote}/{base}"`.
4. **Rebase.** `gitops.rebase(worktree, target)`; `ok=False` ⇒
   `PARKED(rebase_conflict, detail="conflicts: a, b", resume_to="CLOSING")`.
5. **Re-gate.** `gates.run_all(...)` with the caller's `gate_seq`; a required failure ⇒
   `next_state="TRIAGING"` carrying the `GateRun` (the queue counts a loop).
6. **`merge`.** Preconditions on `root`: `current_branch == base` else
   `PARKED(main_not_on_base)`; `is_clean(root, untracked=False)` else
   `PARKED(main_dirty)` (untracked files such as `plans/` are fine); `rev_parse(root,
   base) == target_sha` else the caller retries from step 3 (`close_attempts` ≤ 2, then
   `PARKED(base_moving)`). Then `merged_sha = ff_merge(root, branch)`; assert it equals the
   worktree HEAD; run `merge.post_cmds` in `root` (failure ⇒ `finding(major)`, merge
   stands); `push_after_merge` ⇒ `push_branch(root, remote, base)`. `next_state="MERGED"`.
7. **`pr` / `draft-pr`.** `push_branch(worktree, remote, branch)`; write
   `runs/pr-body.md`; `pr_url = gh_pr_create(worktree, base=base, head=branch,
   title=f"{title_prefix}{spec_id}: {spec title}", body_file=…, draft=(policy ==
   "draft-pr"), labels=…)`; `GitError` ⇒ `PARKED(gh_failed, detail=tail,
   resume_to="CLOSING")`. `next_state="HANDED_OFF"`.
8. **Cleanup** per `worktrees.cleanup`: `on_merge` ⇒ after `MERGED` only; `on_close` ⇒
   after `MERGED` or `HANDED_OFF`; `worktree_remove(root, worktree, force=True)` and, for
   `merge` with `delete_branch`, `branch_delete`. The PR branch is kept.

`pr_body(state, spec_fm, spec_sections, gate_run) -> str` (pure): title line; `Spec:
<rel_path> (<id>, status done)`; **Summary** = `## Decision` text; **Acceptance criteria**
= the checklist verbatim; **Gates** = table `name | result | duration`; **Campaign** = id,
loops, runs, total cost, models used; footer `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

### `verify.verify_spec_file(path, *, kind_hint=None) -> dict`

Same checks and payload as `verify_spec` (status, acceptance boxes, test plan, open
questions, epic children when applicable) computed from a path; `verify_spec(cfg, id)`
resolves the path and delegates. Epic-children checks need a specs dir; `verify_spec_file`
takes an optional `specs_dir` and skips them when absent.

## Alternatives considered

- **Merge from the worktree with `git push . HEAD:main`** — rejected. Git refuses to update
  a checked-out branch that way without config changes, and it would bypass the
  main-checkout preconditions.
- **Auto-stash a dirty main checkout** — rejected. Stashing the user's uncommitted work from
  a background process is exactly the surprise this spec exists to prevent; park and say so.
- **Skip the re-gate when the rebase was a no-op** — rejected. The re-gate also covers the
  case where the implementer's own gate run was stale or partial; it is the one run that
  guards the live install.
- **Mark the spec `done` from the close step** — rejected. AGENTS.md puts that on the
  implementer; the close step verifies, it does not author.

## Acceptance criteria

- [ ] Happy path (`merge`) on temp repos: branch fast-forwarded, main HEAD == worktree
      HEAD, worktree removed and branch deleted per config, `close` payload filled,
      `post_cmds` ran in the main checkout.
- [ ] Dirty main (tracked change) ⇒ `PARKED(main_dirty)` and main untouched; untracked file
      alone does not park; main on another branch ⇒ `PARKED(main_not_on_base)`.
- [ ] Base advanced without conflict ⇒ rebase then merge; with a conflict ⇒
      `PARKED(rebase_conflict)` naming the file, worktree tree identical to before.
- [ ] Base moved between target capture and merge ⇒ caller-visible retry signal, and
      `PARKED(base_moving)` after the configured attempts.
- [ ] Re-gate required failure ⇒ `next_state == "TRIAGING"` with the `GateRun`; spec not
      `done` or with unchecked ACs ⇒ `TRIAGING` with the `spec-dod` synthetic gate.
- [ ] `pr` policy with a fake `gh`: branch pushed to the bare remote, `HANDED_OFF` with the
      URL, body contains the Decision text, the AC checklist, the gate table and the
      footer; `draft-pr` passes `--draft`; a failing `gh` ⇒ `PARKED(gh_failed)`.
- [ ] `verify_spec_file` returns the same payload as `verify_spec` for an existing spec;
      `wt spec verify` behavior unchanged.
- [ ] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_close.py` with fixtures: a "main" repo cloned
  from a bare origin, a worktree created via `gitops`, a spec file in the worktree whose
  status/AC boxes the test edits, a `.wt/project.yaml` with two `sh -c` gates, a fake `gh`
  on `PATH`; one test per criterion above. `tests/test_verify.py` (existing or new) covers
  `verify_spec_file` parity.
- **Manual verification:** on a scratch clone of this repo (not the live checkout), run the
  close step for a trivially-done spec on a branch and watch it fast-forward; repeat with
  a modified tracked file in the clone to see `main_dirty`.
- **Regression guard:** full `uv run pytest`.

## Clock Log

Internal work: clock on IDEA-022 with `wt idea clock-in` / `clock-out`.

## Rollout / migration

Library only; the queue (SPEC-0176) calls it. The first real merge into this checkout
happens under SPEC-0177's manual verification.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
