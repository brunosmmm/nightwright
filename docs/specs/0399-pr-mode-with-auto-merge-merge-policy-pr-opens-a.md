---
id: SPEC-0399
title: "PR mode with auto-merge: nw rebases, re-gates, force-pushes with lease and merges the PR"
status: proposed
owner: user
created: 2026-11-02
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, close, git, pr]
source_idea: IDEA-647
depends_on: [SPEC-0191, SPEC-0220, SPEC-0255, SPEC-0283, SPEC-0359]
---

## Context

Under `merge.policy: pr` the close step (`close.py`, `finish_close` section 7) pushes the
campaign branch, opens a PR with `gh pr create` and ends the campaign in `HANDED_OFF`. After
that nothing touches the PR except the SPEC-0283 sweep, which only notices that a human merged
it. On comfort-assistant on 2026-11-01, PRs #3-#12 were open at once; each merge moved the base
and invalidated the siblings, and **six manual rebase + force-push rounds** were needed. Most of
the conflicts were in `docs/LEDGER.md`, a file whose correct content is a pure function of the
rest of the tree.

What exists today and is reused:

- `merge.policy: merge` already does the right thing locally: rebase onto the current base
  (with `merge.regenerate` resolution, SPEC-0191/SPEC-0255), re-gate (close tier, with the
  SPEC-0359 reuse rules), then fast-forward only if the base has not moved (`prepare_close` /
  `finish_close`, SPEC-0220). PR mode skips all of it after the PR is opened.
- `merge.regenerate: [{path, cmd}]` resolves conflicts on declared generated files. It is opt-in
  per project, so a project that forgot to declare `docs/LEDGER.md` still conflicts on it.
- `launch.py` `DEFAULTS["disallowed_tools"]` denies `Bash(gh pr merge *)` (and agent force
  pushes) to every agent session. That is deliberate and stays: the merge decision must not be
  an agent's.
- `gitops.gh_pr_view` (SPEC-0283) reads `state,mergedAt,mergeCommit`; `queue._sweep_write` moves
  `HANDED_OFF -> MERGED` with `pr_merged` and reconciles the spec's idea.

## Goals / Non-goals

**Goals**
- A project can opt in (`merge.pr.auto_merge: true`, with `merge.policy: pr`) so that nw itself
  drives each handed-off PR to merged: rebase onto the current base, re-gate, force-push with
  lease, wait for the PR to be `MERGEABLE` and its GitHub checks green, run `gh pr merge`, and
  mark the campaign `MERGED`. The PR stays the reviewable record.
- One PR merges at a time per project base. When a merge moves the base, the other open
  auto-merge PRs are rebased and force-pushed so they do not sit `CONFLICTING`.
- A conflict on a generated ledger file is structurally impossible to park on: the ledger is
  always regenerated, never textually merged, whether or not the project declared it.
- Everything is recorded: ledger findings for each rebase, push, wait and merge; a parked PR is
  visible and resumable.

**Non-goals**
- Changing `merge.policy: merge` behaviour. (It gains the implicit ledger regeneration below,
  which is additive: it can only turn a `rebase_conflict` park into a resolved rebase.)
- Auto-merging `draft-pr` PRs: a draft is a human's signal that the PR is not ready.
- Resolving non-generated conflicts. A non-generated conflict parks the PR (`rebase_conflict`);
  the SPEC-0221 conflict-resolver persona is not invoked on PRs in this spec.
- Replacing GitHub branch protection, required reviews or merge queues. nw merges only when
  `gh` reports the PR mergeable; protection that blocks the merge parks it with the `gh` output.
- Letting any agent run `gh pr merge` or force-push.

## Decision

Add an opt-in **PR auto-merge lane** driven by nw's own process (never an agent session). A
campaign closed under `merge.policy: pr` with `merge.pr.auto_merge: true` still ends `HANDED_OFF`
with its PR open, but its state carries `close.pr_auto`. On each queue tick, `advance_pr_merges`
takes the oldest such campaign per `(project, base)` lane and runs: sync -> rebase (regenerate) ->
re-gate -> `git push --force-with-lease` -> wait for `MERGEABLE` + green checks -> re-check the base
-> `gh pr merge --match-head-commit`. On success the campaign becomes `MERGED` (reason
`pr_auto_merged`), exactly like a sweep-detected merge. After the merge, the lane's other PRs are
refreshed (rebase + regenerate + force-with-lease, no gate); the next lane head is gated and
merged. For the ledger, nw adds a built-in **implicit `merge.regenerate` entry** for
`docs/LEDGER.md` (`python3 tools/spec_lint.py --write-ledger`), applied to every rebase nw does
(close and PR lane) unless the project declares that path itself. Merging the ledger by row id
was rejected: the block is already generated, so regeneration is both simpler and the only
result that can never be wrong.

## Design

### Configuration (`project.py`)

`merge.pr` gains keys (validated like the existing ones; `_check_mapping` allows exactly the
`PROJECT_DEFAULTS["merge"]["pr"]` keys, so they are added there):

| key | default | meaning |
|---|---|---|
| `auto_merge` | `false` | enable the lane for this project; invalid unless `merge.policy` is `pr` (a `draft-pr` policy with `auto_merge: true` is a validation error naming `merge.pr.auto_merge`) |
| `method` | `rebase` | `rebase` \| `squash` \| `merge`: the `gh pr merge` flag. The branch is on the base tip when merged, so every method yields the gated tree |
| `max_rebases` | `5` | rebase rounds per PR before it parks `base_moving` (int >= 1) |
| `checks_timeout_s` | `3600` | how long a PR may wait for GitHub checks before it parks `pr_checks_timeout` (int >= 1) |

Global settings `agentic.pr_merge.interval_s` (default `120`, `0` disables the lane) and
`agentic.pr_merge.budget_s` (default `1800`, wall time one tick may spend) sit beside
`agentic.pr_sweep`.

### State (`close.pr_auto`)

Written at handoff when the lane is enabled, as an additive key of the `close` dict (the close
event payload is unchanged apart from this key):

```
close.pr_auto = {
  "status": "queued" | "working" | "waiting_checks" | "parked",
  "pr_head": "<sha last pushed or at handoff>",   # the lease value
  "target_sha": "<base sha the PR was last gated on>" | null,
  "queued_at": "<iso handoff time>", "rebases": 0, "checks_since": null | "<iso>",
  "park": null | {"reason": "...", "detail": "..."}, "updated_at": "<iso>"
}
```

The campaign `state` stays `HANDED_OFF` until the PR is merged, so slot accounting and all
terminal-state readers (SPEC-0283 sweep, steward notices) are unchanged. Parking sets
`pr_auto.status = "parked"` and appends a major `finding` (`gate: "pr-merge"`) so the conductor
sees it; it does not change the campaign state.

### Keep the worktree

`_cleanup` already skips removal for a non-merge mode under the default `worktrees.cleanup:
on_merge`. For `on_close` the PR lane needs the worktree: with `auto_merge` the handoff does not
remove it; removal happens after the merge (cleanup mode `merge`, branch deleted when
`merge.delete_branch`). If the worktree is gone at tick time (`never`/manual removal, or a
different machine) the lane re-creates it detached at `<remote>/<branch>` under the project's
worktree root, then force-pushes from it.

### The lane (`agentic/pr_merge.py`, called from `queue.tick`)

`advance_pr_merges(cfg, *, apply, limit, budget_s, campaign=None)`:

1. **Candidates:** `HANDED_OFF` campaigns with `close.mode == "pr"`, `close.pr_auto` present and
   not `parked`, `close.pr_state` not `CLOSED`, whose project's current config has
   `merge.policy: pr` and `auto_merge: true`. Group by `(project, base)`; order by
   `close.pr_auto.queued_at` (the handoff time), oldest first. A per-lane lock
   (`<agentic_dir>/pr_merge-<project>-<base>.lock`) allows one process to work a lane.
2. **Lane head, step A -- observe:** `gitops.gh_pr_status(url)` returns `state`, `mergeable`,
   `head_sha`, `checks` (`passing | pending | failing | none`, from `statusCheckRollup`). A `MERGED`
   or `CLOSED` PR is handed to the SPEC-0283 `_sweep_write` path (merged ->
   `MERGED/pr_merged`; closed -> the existing finding, then `pr_auto` is dropped from the lane).
   If the remote head != `pr_auto.pr_head`, someone else pushed: park `pr_head_moved`; nw never
   overwrites commits it did not make.
3. **Step B -- rebase and gate** when the branch is not on the current base tip or `target_sha`
   differs from it (after `git fetch <remote> <base>`): run the close worktree phase
   (`close.prepare_close`: spec guard, snapshot commit, `git.rebase` with regenerate resolution,
   SPEC-0255 regenerate-after-rebase, close-tier gates with the SPEC-0359 reuse rules). Outcomes
   map as in close: gates red -> `TRIAGING` is *not* entered; the PR parks `gates_failed` with the
   gate detail (a human or `nw queue pr-merge --resume` after a fix pushed to the branch);
   `rebase_conflict` -> park `rebase_conflict` with the conflicted paths.
4. **Step C -- push:** `gitops.push_branch(..., force_with_lease=<pr_head>)` is `git push
   --force-with-lease=<branch>:<pr_head> <remote> <branch>`. A refused lease parks
   `pr_head_moved`. Success records the new `pr_head`, `target_sha`, `rebases += 1` and an info
   finding "PR rebased onto <sha> and force-pushed". `rebases > max_rebases` parks `base_moving`.
5. **Step D -- wait:** poll `gh_pr_status`. `mergeable == MERGEABLE` and `checks` in
   `{passing, none}` proceed; `pending`/`UNKNOWN` set `waiting_checks` (stamp `checks_since`) and
   return until the next tick; `checks == failing` parks `pr_checks_failed`; `CONFLICTING` goes
   back to step B (counts as a rebase round); waiting longer than `checks_timeout_s` parks
   `pr_checks_timeout`.
6. **Step E -- merge:** re-fetch the base. If it moved since `target_sha`, go back to step B.
   Otherwise `gitops.gh_pr_merge(url, method, match_head=<pr_head>, delete_branch=<merge.delete_branch>)`
   runs `gh pr merge <url> --<method> --match-head-commit <pr_head> [--delete-branch]`. A
   non-zero exit (branch protection, review required, head mismatch) parks `gh_failed` with the
   output tail; a head mismatch is retried once from step A first.
7. **Step F -- record:** `gh_pr_view` must report `MERGED`; then apply the same writes as
   `_sweep_write`'s merge action (set `merged_sha`/`merged_at`/`pr_state`, `_reconcile_closed`,
   `_release_idea_link`, `_queue_split_children`) and `_transition(... "MERGED",
   "pr_auto_merged", by="pr-merge")`; run `_cleanup(..., "merge")`.
8. **Siblings:** after a merge, loop in the same call (budget permitting). Every other candidate
   of the lane whose branch is not on the new base tip is *refreshed*: rebase with regenerate +
   regenerate-after-rebase + `--force-with-lease`, **no gate** (the gate runs when the PR becomes
   head, step B). A refresh conflict parks that PR `rebase_conflict`; a refresh never merges.
   The next head then runs steps A-F.

`gh` and `git` are called only from `gitops.py` (`gh_pr_status`, `gh_pr_merge`, `push_branch`
with `force_with_lease`), in nw's own process. `launch.py` is not changed: `Bash(gh pr merge *)`
stays in `DEFAULTS["disallowed_tools"]`, so no agent can reach the merge or a force push.

### Ledger conflicts made structurally impossible

`close.py` builds `regen` from `merge.regenerate` (line ~762). A new helper
`effective_regenerate(project_cfg, worktree)` returns that mapping plus an **implicit** entry
`docs/LEDGER.md -> python3 tools/spec_lint.py --write-ledger` when both `docs/LEDGER.md` and
`tools/spec_lint.py` exist in the worktree and the project did not declare `docs/LEDGER.md`
itself (a declared entry wins). It is used by `prepare_close` (both policies), the PR lane's
rebase and refresh, and `_regenerate_after_rebase`. A conflicted ledger is therefore always
regenerated from the rebased tree, never edited by hand, so the only way to see a ledger
conflict is a regenerate command that fails (`regenerate_failed` park, unchanged).

### CLI

`nw queue pr-merge [--campaign ID] [--limit N] [--yes] [--json]`: dry-run by default (consistent
with `sweep-prs`); prints one row per candidate `{campaign, pr_url, lane, status, next_action,
detail}`; with `--yes` runs `advance_pr_merges(apply=True)` once. `nw queue pr-merge --resume
CAMPAIGN --yes` clears a parked `pr_auto` (sets `queued`, `rebases = 0`, `park = null`).
`queue.tick` runs the lane at most once per `agentic.pr_merge.interval_s`, claiming the slot
with a stamp file the way `_maybe_sweep_prs` does (`pr_merge.json`/`pr_merge.lock`); the
worktree phase runs on the step pool like `_do_prepare_close`, and the force-push, `gh pr merge`
and campaign writes run on the main thread under `store.item_lock`.

## Alternatives considered

- **`gh pr merge --auto` (GitHub-side auto-merge)** -- GitHub would merge when checks pass, but
  it neither rebases onto the moved base nor runs nw's gates, which is the whole problem; the
  merged tree would not be the gated tree.
- **Merge `docs/LEDGER.md` by row id with a custom git merge driver** -- drivers are local
  config (not present on GitHub's side or on other clones) and the block is already derived
  data; regenerating is simpler and always correct.
- **Make the ledger not committed (generate at read time)** -- changes the ledger contract that
  `spec_lint`, pytest and every project's CI rely on; far larger than the need.
- **Move the campaign back to `CLOSING` for each rebase** -- reuses more code, but un-terminals
  `HANDED_OFF` for slot accounting, the sweep, steward notices and `nw queue gc`, all of which
  rely on it being terminal.
- **Let an agent (conductor) run the rebases and `gh pr merge`** -- the merge denial is a
  deliberate guard; the lane is deterministic code with a lease and a head-pinned merge.

## Acceptance criteria

- [ ] `merge.pr.auto_merge`, `method`, `max_rebases`, `checks_timeout_s` validate and default as
      in the table; `auto_merge: true` with `merge.policy: merge` or `draft-pr` is a
      `ValueError` naming `merge.pr.auto_merge`; a bad `method` or non-positive int is rejected.
- [ ] With `auto_merge` off (the default) a `pr` close behaves exactly as before: `close` has no
      `pr_auto` and `advance_pr_merges` returns no rows for the project.
- [ ] With it on, a handed-off PR whose branch is behind the base is rebased onto the current base,
      the close-tier gates run on the rebased tree, and the branch is pushed with
      `--force-with-lease=<branch>:<recorded pr_head>`; the recorded `pr_head` is updated.
- [ ] The push uses a lease: if the remote branch has a commit nw did not record, nothing is
      overwritten and the PR parks `pr_head_moved`.
- [ ] `gh pr merge` runs only when the PR is `MERGEABLE`, checks are `passing` or `none`, the base
      has not moved since the gated `target_sha`, and always with `--match-head-commit <pr_head>`
      and the configured method flag; pending checks leave the PR `waiting_checks` with no merge
      call; failing checks park `pr_checks_failed`; waiting past `checks_timeout_s` parks
      `pr_checks_timeout`.
- [ ] After a successful merge the campaign is `MERGED` with reason `pr_auto_merged`, `close`
      has `merged_sha`/`merged_at`/`pr_state: "MERGED"`, the idea link is released and the worktree
      is cleaned per `worktrees.cleanup`.
- [ ] When a merge moves the base, the other auto-merge PRs of the same project/base are
      rebased and force-pushed with lease without running gates; only the next lane head is
      gated and merged, and never two PRs of a lane in the same pass without a re-check of the
      base between them.
- [ ] A red gate parks the PR `gates_failed`; a non-generated conflict parks it `rebase_conflict`
      listing the paths; rounds beyond `max_rebases` park `base_moving`; every park appends a major
      `gate: "pr-merge"` finding, leaves the campaign `HANDED_OFF`, and `pr-merge --resume`
      re-queues it.
- [ ] A rebase conflicting only on `docs/LEDGER.md` is resolved by regeneration in `merge.policy:
      merge` closes and in the PR lane, even when the project's `merge.regenerate` is empty; an
      explicit `merge.regenerate` entry for the same path overrides the implicit one; a project
      without `tools/spec_lint.py` gets no implicit entry.
- [ ] `gh pr merge` and force pushes are invoked only from `gitops.py`; `launch.py`
      `DEFAULTS["disallowed_tools"]` still contains `Bash(gh pr merge *)` and the force-push guards.
- [ ] `nw queue pr-merge` is a dry-run without `--yes` (no git/gh write calls) and `--json`
      emits the rows; `--resume` works as described.
- [ ] `agentic.pr_merge.interval_s: 0` disables the tick hook; a tick inside the interval does
      not run the lane twice.
- [ ] `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_project.py`: defaults and validation of the four new `merge.pr` keys and
    the `auto_merge` x `policy` rule.
  - `tests/test_agentic_pr_merge.py` (new). Uses real `git` against a local bare remote (so the
    rebase, `--force-with-lease` and lease refusal are the real behaviour) and a `gh` shim
    script on `PATH` that logs its argv and serves canned JSON for `pr view`/`pr merge`.
    Cases: behind-base PR is rebased, gated (stub gate) and pushed with the lease argument;
    foreign commit on the remote parks `pr_head_moved` and the remote is untouched; `pending`
    checks make no `pr merge` call; `failing` parks; timeout parks (fake clock); `CONFLICTING`
    goes back to rebase; merge argv contains `--match-head-commit <sha>` and `--rebase` by default
    and `--squash`/`--merge` per `method`; base moved after the gate re-runs step B and, past
    `max_rebases`, parks `base_moving`; successful merge yields `MERGED/pr_auto_merged`, reconciled
    idea and cleaned worktree; three queued PRs in one lane: merge of #1 refreshes #2 and #3
    (pushed, no gate call), then #2 is gated and merged, then #3; two lanes (different bases) do
    not block each other; red gate and non-generated conflict park and `--resume` re-queues;
    `auto_merge` off yields no candidates.
  - `tests/test_agentic_close.py`: implicit `docs/LEDGER.md` regeneration resolves a ledger-only
    conflict with empty `merge.regenerate`; explicit entry overrides; no `tools/spec_lint.py` ->
    still `rebase_conflict`; a ledger plus a code conflict still parks.
  - `tests/test_agentic_queue_cli.py`: `nw queue pr-merge` dry-run makes no writes, `--json`
    shape, `--yes`, `--resume`; tick hook interval stamp (0 disables, second tick inside the
    interval is a no-op).
  - `tests/test_agentic_launch.py`: a guard that `Bash(gh pr merge *)` stays in
    `DEFAULTS["disallowed_tools"]`, plus a source scan asserting `"pr", "merge"` and
    `--force-with-lease` appear only in `gitops.py`.
- **Manual verification (real `gh`, required before `done`; fakes are not verification):** on a
  scratch GitHub repo with `merge.policy: pr`, `merge.pr.auto_merge: true` and a trivial gate,
  hand off three campaign PRs that each add a spec (so each conflicts on `docs/LEDGER.md`), run
  `nw queue pr-merge` (dry-run, expect three rows), then `nw queue pr-merge --yes` repeatedly /
  let the tick run. Expected: all three PRs merge in order with no manual rebase, the campaigns
  are `MERGED` with reason `pr_auto_merged`, `git log origin/main` is linear, `docs/LEDGER.md`
  on main passes `spec_lint`, and the PR timeline shows the force-pushes. Record the PR URLs
  and the one real `gh pr merge` argv in this spec's "As shipped" note.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus
  `tests/test_agentic_close.py`, `tests/test_agentic_queue_merge.py`,
  `tests/test_agentic_launch.py`, `tests/test_agentic_conductor_guard.py` and the SPEC-0283
  sweep tests unchanged and green; a `merge.policy: merge` close on a project with a ledger
  conflict behaves as before for non-ledger conflicts.

## Rollout / migration

1. Land the implicit ledger regeneration and the config keys (no behaviour change for existing
   projects, `auto_merge` defaults to off).
2. Land `gh_pr_status`, `gh_pr_merge`, `push_branch(force_with_lease=)` and the lane, tick hook
   and CLI.
3. Run the manual verification on a scratch repo, then enable `merge.pr.auto_merge: true` on
   comfort-assistant's `.nw/project.yaml`. PRs handed off before enabling carry no
   `close.pr_auto` and are not picked up by the lane; they are merged by hand once (the
   sweep still marks them `MERGED`). Adopting them automatically is out of scope.
4. No data migration: `close.pr_auto` is additive and ignored by older readers.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; the real-`gh` manual run is performed and noted.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None. Defaults (`rebase` method, 5 rebase rounds, 3600 s checks wait, 120 s interval) are
  starting values to tune from the first runs' ledgers.
