---
id: SPEC-0283
title: "HANDED_OFF sweep: a merged PR moves its campaign to MERGED"
status: done
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

- `src/wt/agentic/gitops.py:gh_pr_view(url, *, timeout_s=30) -> dict` (new). It runs `gh pr
  view <url> --json state,mergedAt,mergeCommit` through `_run(..., timeout=timeout_s)`. It
  does not use `_NETWORK_TIMEOUT` (1800 s), which suits pushes, not a status lookup in a
  follower. It returns `{"state": "OPEN"|"MERGED"|"CLOSED", "merged_at", "merge_sha"}` and
  raises `GitError` when `gh` is missing, exits non-zero, times out, or prints invalid JSON.
- `src/wt/agentic/queue.py:sweep_handed_off(cfg, *, apply=False, limit=20, budget_s=60,
  view=None, clock=time.monotonic) -> list[dict]` (new). `view` defaults to `gh_pr_view`, and
  `view` and `clock` can be injected in tests.
  - **Selection.** It reads `store.list_campaigns(cfg, include_terminal=True,
    state="HANDED_OFF")`, keeps those with a `close.pr_url`, and drops those whose
    `close.pr_state` is already `"CLOSED"`, because a closed PR needs no more polling. It
    orders the rest **least recently checked first**, by `close.pr_checked_at`, where a
    missing stamp sorts first and ties go oldest `created_at` first. It takes up to `limit`
    campaigns. `list_campaigns` returns newest first, so this ordering is explicit. Old,
    long-open PRs cannot starve newer ones, because each check moves its campaign to the back
    of the order.
  - **Budget.** Before each `view` call it checks `clock() - start >= budget_s`. When the
    budget is spent it stops, and the remaining campaigns get no row; the next sweep starts
    with them because their stamp is older. The worst case is `budget_s + timeout_s`
    (90 s by default), not `limit × timeout`.
  - It builds one row per checked campaign: `{campaign, pr_url, pr_state, action, detail}`.
  - **Every write happens under `store.item_lock(cfg, cid)`**, after re-reading the state and
    re-checking the precondition for that action. If the precondition no longer holds, the
    action becomes `skipped` with a detail (for example "state is ABANDONED"), and nothing is
    written. With `apply`, every checked campaign whose precondition holds also gets
    `close.pr_checked_at = now`.
  - `MERGED` gives action `merge`. Its precondition is `state == "HANDED_OFF"`. With `apply`,
    under the lock:
    1. Set `close.merged_sha`, `close.merged_at` and `close.pr_state = "MERGED"`.
    2. Call `_reconcile_merged(cfg, state, events)`.
    3. Write the state, and append the collected events plus `transition {from: HANDED_OFF,
       to: MERGED, reason: "pr_merged"}` with `by="sweep"`.
  - `CLOSED` gives action `closed`. Its precondition is `state == "HANDED_OFF" and
    close.pr_state != "CLOSED"`. With `apply`, under the lock, it sets `close.pr_state =
    "CLOSED"` and appends a `finding` with `persona: "close"`, `severity: "major"`, `gate:
    "pr"`, `title: "PR closed without merging"`, `detail: <pr_url>` and `files: []` (all keys
    that `schemas.EVENT_KEYS["finding"]` requires). Because both the check and the write run
    under the lock, two sweeps that run at once (the CLI and a follower, or two followers)
    write one finding.
  - `OPEN` gives action `open`. With `apply`, only `pr_checked_at` is written.
  - A `GitError` gives action `error` with its tail. With `apply`, only `pr_checked_at` is
    written, so a PR that keeps failing moves to the back of the order. A failure on one PR
    never stops the sweep.
- `src/wt/agentic/events.py:sync`: in the `if ping:` block, after `ESC.remind_due`, call a new
  `_maybe_sweep_prs(cfg, now)`. It reads `agentic_dir/pr_sweep.json` (`{"last": iso}`) and
  claims the slot under the existing `_locked(...)` helper on a separate lock file,
  `pr_sweep.lock`. Inside the lock it re-reads `last`, returns if the interval has not
  passed, and otherwise writes `last = now` before releasing. Only then, outside the lock, it
  runs `queue.sweep_handed_off(cfg, apply=True)`. Of several followers, only one sweeps per
  interval. It swallows every exception, as the reminder does.
- `src/wt/config.py`: add the default `agentic.pr_sweep: {interval_s: 900}`. `load_config`
  merges only top-level keys, so a user `agentic:` block replaces the default wholesale. The
  code therefore reads the value as `((cfg.get("agentic") or {}).get("pr_sweep") or
  {}).get("interval_s", 900)`.
- `src/wt/agentic/cli_queue.py`: a new `@queue_group.command("sweep-prs")` with `--yes`,
  `--json` and `--limit N` (default 20). It prints one table row per campaign, with columns
  campaign, PR URL, PR state, action and detail. `--json` prints a list of the row dicts
  `{campaign, pr_url, pr_state, action, detail}`, where `action` is one of `merge`, `closed`,
  `open`, `error` or `skipped`, and `pr_state` is null for `error`. The command exits 0 even
  when some rows are `error`, because the rows are the report.
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

- [x] `wt queue sweep-prs` (dry run) lists each `HANDED_OFF` campaign with its PR state and
      planned action, and writes nothing.
- [x] With `--yes`, the campaign of a merged PR becomes `MERGED` and `close.merged_sha` is
      set. The ledger gets a `transition` event with reason `pr_merged` that passes
      `schemas.ledger_problems`, and the conductor event stream gets `merged`.
- [x] When the spec store is this repo, the merged campaign's spec idea is reconciled
      through `_reconcile_merged` and an `idea_reconciled` event is written. A campaign
      that is no longer `HANDED_OFF` when the lock is taken (for example, `ABANDONED`) is
      reported `skipped` and left unchanged.
- [x] A PR closed without merging yields exactly one `major` finding, "PR closed without
      merging", even across repeated or concurrent sweeps. The campaign stays
      `HANDED_OFF`.
- [x] If `gh` fails or times out (30 s) on one PR, that PR gets an `error` row. The other
      PRs are still checked. Each sweep stops once its `budget_s` is spent. Campaigns are
      checked least recently checked first, so with more campaigns than `limit`, every
      campaign is checked over successive sweeps.
- [x] The follower's ping path runs the sweep at most once per
      `agentic.pr_sweep.interval_s` across all followers, and never raises.
      `interval_s: 0` turns the sweep off.

## Test plan

The `gh` wrapper tests go in `tests/test_agentic_close.py`. The sweep tests go in a new
`tests/test_agentic_pr_sweep.py`. Both use tmp agentic dirs and the campaign fixtures from
`tests/test_agentic_queue.py`, with an injected `view`.

- `test_gh_pr_view_parses_json`, `test_gh_pr_view_missing_gh` and
  `test_gh_pr_view_uses_short_timeout` monkeypatch `shutil.which` and `_run`. The timeout
  test asserts that `_run` receives `timeout=30`, not `_NETWORK_TIMEOUT`.
- `test_sweep_dry_run_writes_nothing`: the state file and the ledger are unchanged.
- `test_sweep_merged_transitions_and_reconciles` checks that:
  - the state is `MERGED`, `merged_sha` is set, and the transition reason is `pr_merged`;
  - `ledger_problems` returns `[]`;
  - `events.sync` emits `merged`.
- `test_sweep_merged_writes_idea_reconciled`: uses a real spec and idea fixture in the style
  of SPEC-0257 (`specs_dir` inside the campaign's `repo.root`, and an idea created on or
  before the spec). It asserts an `idea_reconciled` ledger event.
- `test_sweep_skips_state_changed_under_lock`: `view` reports MERGED, but the campaign is
  turned `ABANDONED` before the write. Its row is `skipped`, and the state and ledger are
  unchanged.
- `test_sweep_closed_finding_once`:
  - two sequential sweeps write one finding, and the state stays `HANDED_OFF`;
  - two sweeps interleaved through an injected `view` that runs the second sweep from
    inside the first also write one finding;
  - the finding passes `ledger_problems`.
- `test_sweep_error_isolated`: the first PR's `view` raises `GitError`, and the second PR
  still merges.
- `test_sweep_budget_stops`: a fake `clock` passes `budget_s` after the first `view`. Only
  one row is returned.
- `test_sweep_fair_selection`: with `limit + 5` open-PR campaigns, two sweeps with `--yes`
  between them check every campaign at least once.
- `test_sync_ping_runs_sweep_on_interval`, with a frozen `now`:
  - two calls within the interval sweep once, including two `sync` calls that each act as
    a separate follower;
  - `interval_s: 0` never sweeps;
  - a user `agentic:` block without `pr_sweep` falls back to 900;
  - a sweep that raises does not break `sync`.
- **Sabotage check:**
  - Drop the `state == "HANDED_OFF"` re-check under the lock, and feed a campaign that turned
    `ABANDONED`. The guard test `test_sweep_skips_state_changed_under_lock` must fail.
  - Move the CLOSED `pr_state` check out of the lock, so it runs before the lock is taken.
    The interleaved case of `test_sweep_closed_finding_once` must fail.
  - Sort by `created_at` instead of `pr_checked_at`. `test_sweep_fair_selection` must fail.
- **Regression guard:** the full `uv run pytest`. The existing HANDED_OFF close tests are
  unchanged.
- **Manual:** none required, because no project uses a `pr` policy today. The wrapper tests
  cover the `gh` call.

### Test record (2026-10-27)

- `uv run pytest -q tests/test_agentic_pr_sweep.py tests/test_agentic_close.py`: all pass (the
  `gh_pr_view` wrapper tests live in `test_agentic_close.py`; the sweep, CLI and follower tests in
  `test_agentic_pr_sweep.py`).
- Sabotage checks run by hand, each failing the named test: dropping the `HANDED_OFF` re-check
  (`test_sweep_skips_state_changed_under_lock`), dropping the CLOSED re-check under the lock
  (`test_sweep_closed_finding_once_interleaved`), and removing the `pr_checked_at` sort
  (`test_sweep_fair_selection`).
- Full gates: `python3 tools/spec_lint.py --write-ledger && python3 tools/spec_lint.py`, `uv run pytest -q -x`.

### As shipped

- `test_sweep_closed_finding_once` is split in two: the sequential case, and
  `test_sweep_closed_finding_once_interleaved` for the nested-sweep case.
- `sweep_handed_off` maps a PR state outside OPEN/MERGED/CLOSED to an `error` row.
- `wt queue sweep-prs` also takes `--limit N`. `_maybe_sweep_prs` is inside the `if ping:` block, after the reminder.

## Rollout / migration

None. The first sweep picks up existing `HANDED_OFF` campaigns.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
