---
id: SPEC-0283
title: "HANDED_OFF sweep: a merged PR moves its campaign to MERGED"
status: accepted
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-026
parent: SPEC-0169
depends_on: [SPEC-0175, SPEC-0257]
tags: [agentic, queue, close, gh]
---

## Context

Promoted from idea `IDEA-026`, a wave-2 child of epic SPEC-0169 ("HANDED_OFF sweep via `gh pr
view`: mark handed-off campaigns merged when the PR merges").

Under `merge.policy: pr | draft-pr`, the close step pushes the branch, opens a PR and ends the
campaign in `HANDED_OFF`. This happens in the `pr` branch of the finish phase in
`src/wt/agentic/close.py`, which returns `CloseOutcome("HANDED_OFF", close={"mode",
"merged_sha": None, "pr_url", ...})`. Nothing looks at the PR again after that:

- The campaign stays `HANDED_OFF` even after the PR merges.
- `close.merged_sha` stays None, and the conductor never gets a `merged` event. The projector
  in `events.py` turns a `transition` into `merged` only when the target state is `MERGED`.
- `queue._reconcile_merged` (SPEC-0257) runs only on a `MERGED` close outcome (in
  `queue.py`, `if oc.next_state == "MERGED"`), so the spec's idea is never reconciled.
  SPEC-0280 (accepted) marks the idea `:HANDED_OFF:`, but it explicitly leaves polling the
  forge as a non-goal.

`gitops.gh_pr_create` already wraps `gh`: it finds the binary with `shutil.which("gh")`,
calls `_run` with `_NETWORK_TIMEOUT`, and raises `GitError` on failure.

## Goals / Non-goals

**Goals**
- `wt queue sweep-prs` checks the PR of each `HANDED_OFF` campaign with `gh pr view`. It is a
  dry run unless `--yes` is given.
- When a PR has merged, its campaign moves `HANDED_OFF → MERGED` with reason `pr_merged`, and
  the merge commit is recorded. The conductor gets a `merged` event, and the existing
  `_reconcile_merged` reconciles the spec's idea.
- When a PR was closed without merging, the sweep records one finding. The campaign stays
  `HANDED_OFF`.
- The event follower runs the sweep periodically, as a best-effort job next to the SPEC-0232
  escalation reminders.

**Non-goals**
- Re-opening, re-queueing or abandoning a campaign whose PR was closed. That decision belongs
  to a human or the conductor.
- Forges other than GitHub through `gh`.
- Watching CI status on the PR.

## Decision

Add `gitops.gh_pr_view(url)` and a module function `queue.sweep_handed_off(cfg, *, apply,
limit)`. Expose the sweep as `wt queue sweep-prs [--yes] [--json]`. Also call it from
`events.sync(..., ping=True)`, at most once per `agentic.pr_sweep.interval_s` (default 900 s;
0 disables it).

A terminal state may change only on this one edge, `HANDED_OFF → MERGED`, and the change is
written under `store.item_lock`.

## Design

- `src/wt/agentic/gitops.py:gh_pr_view(url) -> dict` (new). It runs `gh pr view <url> --json
  state,mergedAt,mergeCommit` through `_run(..., timeout=_NETWORK_TIMEOUT)` and returns
  `{"state": "OPEN"|"MERGED"|"CLOSED", "merged_at", "merge_sha"}`. It raises `GitError` when
  `gh` is missing, exits non-zero, or prints invalid JSON.
- `src/wt/agentic/queue.py:sweep_handed_off(cfg, *, apply=False, limit=20, view=None) ->
  list[dict]` (new). `view` defaults to `gh_pr_view` and can be injected in tests.
  - It reads campaigns from `store.list_campaigns(cfg, include_terminal=True)`. It takes
    those with `state == "HANDED_OFF"` and a `close.pr_url`, in age order, up to `limit`.
  - It builds one row per campaign: `{campaign, pr_url, pr_state, action, detail}`.
  - `MERGED` gives action `merge`. With `apply`, it takes `store.item_lock(cfg, cid)` and
    then:
    1. Re-reads the state and checks that it is still `HANDED_OFF`.
    2. Sets `close.merged_sha`, `close.merged_at` and `close.pr_state = "MERGED"`.
    3. Calls `_reconcile_merged(cfg, state, events)`.
    4. Writes the state and appends the collected events plus `transition {from: HANDED_OFF,
       to: MERGED, reason: "pr_merged"}` with `by="sweep"`.
  - `CLOSED` gives action `closed`, once. The row is skipped when `close.pr_state` is already
    `"CLOSED"`. With `apply`, it sets `close.pr_state` and appends a `finding` with `persona:
    "close"`, `severity: "major"`, `gate: "pr"` and `title: "PR closed without merging"`.
  - `OPEN` gives action `open` and writes nothing.
  - A `GitError` gives action `error` with its tail and writes nothing. A failure on one PR
    never stops the sweep.
- `src/wt/agentic/events.py:sync`: in the `if ping:` block, after `ESC.remind_due`, call a new
  `_maybe_sweep_prs(cfg, now)`. It keeps the time of the last sweep in
  `agentic_dir/pr_sweep.json` (`{"last": iso}`). When the interval has passed, it runs
  `queue.sweep_handed_off(cfg, apply=True)`. It swallows every exception, as the reminder
  does.
- `src/wt/config.py` default: `agentic.pr_sweep: {interval_s: 900}`.
- `src/wt/agentic/cli_queue.py`: a new `@queue_group.command("sweep-prs")` that prints one row
  per campaign. It exits 0 even when some rows are `error`, because the rows are the report.
- No schema change. `transition` and `finding` are existing ledger types
  (`schemas.EVENT_KEYS`), and `close` is a free-form dict.

## Alternatives considered

- **Poll from the runner tick.** Rejected because ticks are kept non-blocking (SPEC-0213),
  and a network call per tick is the wrong cadence. The follower already hosts a periodic
  best-effort job.
- **A GitHub webhook.** Rejected because it needs a server, and wt is local-only.
- **A new terminal state `PR_MERGED`.** Rejected because every consumer already understands
  `MERGED`.

## Acceptance criteria

- [ ] `wt queue sweep-prs` (dry run) lists each `HANDED_OFF` campaign with its PR state and
      planned action, and writes nothing.
- [ ] With `--yes`, the campaign of a merged PR becomes `MERGED` and `close.merged_sha` is
      set. The ledger gets a `transition` event with reason `pr_merged` that passes
      `schemas.ledger_problems`, and the conductor event stream gets `merged`.
- [ ] `_reconcile_merged` reconciles the spec's idea for a merged campaign. When the spec
      store is this repo, an `idea_reconciled` event is written.
- [ ] A PR closed without merging gives exactly one `major` finding, "PR closed without
      merging", however many sweeps run. The campaign stays `HANDED_OFF`.
- [ ] A `gh` failure on one PR gives an `error` row and writes nothing for that PR. The
      other PRs are still swept.
- [ ] The follower's ping path runs the sweep at most once per `agentic.pr_sweep.interval_s`
      and never raises. `interval_s: 0` disables the sweep.

## Test plan

The `gh` wrapper tests go in `tests/test_agentic_close.py`. The sweep tests go in a new
`tests/test_agentic_pr_sweep.py`. Both use tmp agentic dirs and the campaign fixtures from
`tests/test_agentic_queue.py`, with an injected `view`.

- `test_gh_pr_view_parses_json` and `test_gh_pr_view_missing_gh` monkeypatch
  `shutil.which` and `_run`.
- `test_sweep_dry_run_writes_nothing`: the state file and the ledger are unchanged.
- `test_sweep_merged_transitions_and_reconciles` checks that:
  - the state is `MERGED`, `merged_sha` is set and the transition reason is `pr_merged`;
  - `ledger_problems` returns `[]`;
  - `events.sync` emits `merged`;
  - `_reconcile_merged` is called (a monkeypatched spy).
- `test_sweep_closed_finding_once`: two sweeps write one finding, and the state stays
  `HANDED_OFF`.
- `test_sweep_error_isolated`: the first PR's `view` raises `GitError`, and the second PR
  still merges.
- `test_sync_ping_runs_sweep_on_interval` uses a frozen `now`:
  - two calls within the interval sweep once;
  - with `interval_s: 0` there is no sweep;
  - a sweep that raises does not break `sync`.
- **Sabotage check:**
  - Drop the `state == "HANDED_OFF"` re-check under the lock, and feed a campaign that turned
    `ABANDONED`. The guard test `test_sweep_skips_state_changed_under_lock` must fail.
  - Drop the `pr_state` memo. `test_sweep_closed_finding_once` must fail.
- **Regression guard:** the full `uv run pytest`. The existing HANDED_OFF close tests are
  unchanged.
- **Manual:** none required, because no project uses a `pr` policy today. The wrapper tests
  cover the `gh` call.

## Rollout / migration

None. The first sweep picks up existing `HANDED_OFF` campaigns.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
