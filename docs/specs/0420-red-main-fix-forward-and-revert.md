---
id: SPEC-0420
title: "Red-main fix-forward and revert"
status: done
owner: user
created: 2026-11-04
source_idea: IDEA-926
parent: SPEC-0419
milestone: "M9: agentic workflow"
tags: [agentic, sentinel, queue, close, conductor]
---

## Context

Child 1 of [SPEC-0419](./0419-self-healing-queue.md). Today a red sentinel
([SPEC-0359](./0359-close-tier-reuse-of-a-green-loop-gate-with-a-full.md),
[SPEC-0379](./0379-time-based-sentinel-on-worker-slots.md)) does three things in
`src/nightwright/agentic/sentinel.py`:

- `settle()` (`:665-702`) sets `force_full_close` in `gate_reuse.json`.
- It appends a `sentinel` event.
- Unless `_repeat_red()` (`:654-662`) finds that the previous `sentinel` event was red at the
  **same sha**, it calls `_escalate()` (`:705-725`).

`_escalate` files a campaign-less escalation with three options and no recommendation. Nothing in
the queue acts on a red base:

- There is no fix campaign, no revert, and no `sentinel`/`red_main` row in
  `skills/nw-conduct/SKILL.md`.
- Every later merge moves the base, changes the tested sha and files another escalation. ESC-0007
  to 0012, 0015 to 0018, 0024 and 0025 (12 records) cover 5 episodes.
- Merges keep landing on the red base, because `force_full_close` only disables close-tier
  reuse (`reuse_blocker`, `:315-330`).
- The full-suite sentinel at the base is what finds the failure. The close `pytest` gate is
  affected-only (SPEC-0378).

Every fix so far came from a human-assistant session. In the last episode the fix was ready 4.5 h
before it landed, because the conductor waited for "the human's OK".

Data the queue already has:

- `sentinel.run()` returns `failing` (pytest ids, from `_failing()`) and per-gate `log` paths.
- Every merged campaign's `close` ledger event carries `target_sha` (base before the merge) and
  `merged_sha` (`close.finish_close`), so the merges between the last green sha and the red sha
  are known exactly.
- Merges are ff-only, so each campaign's change is the commit range `target_sha..merged_sha` on
  the base.

## Goals / Non-goals

**Goals**
- A red sentinel opens a **red episode** per project. The queue owns the episode until a green
  sentinel closes it.
- The queue opens a `fixforward` campaign at once. If it fails N times, it reverts the merge that
  introduced the failure.
- Non-fix merges of the project are held while the base is red.
- At most one human escalation per episode, filed only when red persists with no fix in flight or
  every automatic fix failed. It is withdrawn automatically on green.
- The conductor's decision table has rows for the new events. The conductor may land a branch
  that fixes the red base through the gated merge, with no human OK.

**Non-goals**
- Faster detection. The sentinel cadence (`after_reuses`, `interval_hours`) is unchanged, except
  that a fix merge makes the sentinel due at once.
- LLM review of fix diffs: the full suite plus the weakening check (Design §5) gate them.
- Pushing, PRs, or `gh pr merge`: fixes merge locally, ff-only, like every Meta-Tools campaign.
- Red episodes of `pr` / `auto-merge` policy projects. The base is remote there, so their
  fix-forward campaigns hand off a PR like any campaign, and revert is skipped (Design §6).

## Decision

Add an episode state machine in a new module `src/nightwright/agentic/redmain.py`. It is driven by
`sentinel.settle()` and by one runner-tick step:

1. **Red sentinel.** `settle()` calls `redmain.on_red()` instead of `_escalate()`. The call opens
   or updates the project's episode, which records the failing ids, their fingerprint and the
   suspect merges.
2. **Fix forward.** With no fix in flight, the tick step `queue._red_main_step()` opens a
   `fixforward` campaign. It has priority 1, `full_suite: true`, and a generated brief (failing
   ids, gate-log tails, suspects with their spec ids and diffstats). The fix loop is
   implementer → loop gates (full suite) → triage, as usual. Review is skipped. Close runs the full
   suite and a weakening check, then merges ff-only.
3. **Revert.** After `fixforward_attempts` (default 2) failed fix-forward campaigns, the queue
   localizes the culprit merge: a binary search over the episode's merge range, running only the
   failing ids in the sentinel worktree. It then opens a `revert` campaign, which reverts that
   campaign's commit range, needs no persona and is gated by the full suite.
4. **Hold.** While an episode is open, `close.prepare_close()` holds every non-fix campaign of
   the project in CLOSING (`close_held`, no park, no attempt counted). Fix campaigns are exempt:
   `fixforward`, `revert`, and merge requests marked `--fixes-red`. `--docs-only` merges are
   exempt too.
5. **Escalate rarely.** The episode escalates once, with topic `recovery` and a recommendation,
   only when one of these holds:
   - it has been open longer than `escalate_after_s` (default 4 h) with no fix in flight;
   - automatic fixing is exhausted: the revert conflicted, the revert merged and the base stayed
     red, or no culprit could be localized.
6. **Green.** `settle(ok=True)` calls `redmain.on_green()`. It closes the episode, withdraws its
   escalation and any open legacy "Sentinel red on <project>" escalation, and abandons any
   not-yet-closing fix campaigns as moot. Held campaigns merge on their next tick.

**The conductor may merge a red-base fix through the gated path.** `nw queue merge <branch> --by
conductor --fixes-red --yes` creates a merge campaign that is exempt from the hold and gated by
the full suite. The skill allows it without a human OK: it is the queue's normal rebase,
full-suite, ff-only path, and nothing reaches the base ungated. Pushes to the base and `gh pr
merge` stay human-only (SPEC-0419).

## Design

### 1. Episode state: `redmain.py` (new)

File `<agentic>/projects/<project>/red.json`. Writes are atomic (`export._atomic_write`) and
happen under `red.json.lock` (`events._locked`):

```json
{"schema": "nw.red.v1", "episode": 3, "open": true,
 "opened_at": "...", "closed_at": null, "first_red_sha": "...", "last_red_sha": "...",
 "last_green_sha": "...",                     // the green sha before the episode (sentinel state)
 "failing": ["tests/test_x.py::test_a", ...], // union over the episode's red runs, max 200
 "fingerprint": "sha256:<hex16>",             // of the sorted current failing ids
 "runs": 2,
 "merges": [{"campaign": "...", "spec_id": "SPEC-0410", "target_sha": "...", "merged_sha": "..."}],
 "culprit": null,                             // a merges[] entry once localized
 "fixes": [{"campaign": "...", "kind": "fixforward|revert|merge", "opened_at": "...",
            "outcome": null}],                 // null | merged | failed | moot
 "escalation": null,                          // ESC id
 "history": [{"at": "...", "what": "...", "by": "queue"}]}
```

Functions (pure where marked, `now` always an argument):

- `settings(cfg)` (pure) reads `agentic.red_main`: `enabled` (true), `fixforward_attempts` (2),
  `revert` (true), `hold_merges` (true), `escalate_after_s` (14400), `brief_log_lines` (120).
  Bad values fall back to their defaults.
- `read(cfg, project)` returns the state dict, or None when no episode was ever opened.
  `is_red(cfg, project)` is `read(...)` with `open` true.
- `on_red(cfg, project, sha, failing, *, root, base, now)`:
  - **No open episode:** open one with `episode = previous + 1`. Take `last_green_sha` from
    `gate_reuse.json`. Compute `merges` from the `close` events of MERGED campaigns of the
    project whose `merged_sha` is an ancestor of `sha` and not of `last_green_sha` (an unknown
    `last_green_sha` means the 20 newest merges). Append a `red_main` event: data `episode`,
    `sha`, `failing[:20]`, `merges` (campaign + spec ids).
  - **Open episode:** update `last_red_sha`, `runs`, the `failing` union and `fingerprint`, and
    append the merges that reached the base since. Append a `red_main` event only when the
    fingerprint changed.
  - Never escalates.
- `on_green(cfg, project, sha, *, now)`:
  - If an episode is open, close it (`closed_at`, history) and append `red_main_cleared` (episode,
    duration, fixes).
  - Withdraw `escalation` if open, with `escalations.withdraw(..., by="queue", reason="sentinel
    green at <sha10>")`.
  - Also withdraw every open campaign-less escalation whose question starts with `Sentinel red on
    <project>`. These are the legacy records (ESC-0024/0025).
  - Abandon each fix campaign in QUEUED, OPENING, IMPLEMENTING, GATING, TRIAGING or PARKED with
    `plan_abandon/apply_abandon(by="queue", decision=...)`, rule `red_main.moot`, and mark it
    `moot`. A CLOSING fix finishes; its merge is gated and harmless.
- `holding(cfg, project, state)` (pure over the read state) returns a hold reason or None. None
  when there is no open episode, `hold_merges` is off, the campaign kind is `fixforward` or
  `revert`, `state["fixes_red"]` is true, or the close is `docs_only`.
- `localize(cfg, project, *, root, stop)` runs only when there are 2 or more candidate merges. In
  the sentinel worktree (`sentinel._prepare_worktree` at a given sha, under `sentinel.run_lock`
  and a gate slot, like `sentinel.run`), it binary-searches `merges` in base order. At each probe
  it runs `uv run pytest -q -p no:cacheprovider <failing ids…>` (the project's `close` pytest gate
  command is not used, because the ids are given). The answer is the first merge whose
  `merged_sha` fails any of the ids while its `target_sha` passes them all. At most ⌈log2 n⌉ + 1
  probes, and only the failing ids, so a probe takes seconds. With one candidate there is no
  probe. When nothing is found, the result is `{"culprit": null, "why": ...}`.
- `step(cfg, project, *, now, active)` is the tick's decision. It returns one of `none`,
  `open_fixforward`, `localize`, `open_revert` or `escalate`, plus the reason:
  - Fix in flight: a `fixes[]` entry with `outcome` null whose campaign is not terminal, or any
    active merge campaign of the project with `fixes_red`. → `none`.
  - Count each `fixforward` entry as **failed** when its campaign is ABANDONED (not `moot`), or
    PARKED with any reason the queue does not auto-resume (`autopark` rules excluded). Also failed:
    it merged, but the next red sentinel after the merge still fails an id of the episode
    (`on_red` marks it).
  - Failed fix-forwards fewer than `fixforward_attempts` → `open_fixforward`.
  - Else `revert` on and no revert yet: `localize` if `culprit` is unknown, else `open_revert`.
  - Else (revert failed, revert off, or no culprit) → `escalate`, once.
  - Independently, when no fix is in flight and `now - opened_at > escalate_after_s` → `escalate`,
    once.

### 2. Sentinel wiring (`sentinel.py`)

- `settle()`: the red branch calls `redmain.on_red(cfg, project, sha, failing, root=root,
  base=base, now=now)` (best effort: an OSError is journaled, never raised). The green branch calls
  `redmain.on_green(...)` after the state write. `_escalate()` and `_repeat_red()` are removed. The
  `sentinel` event is unchanged.
- When `agentic.red_main.enabled` is false, `settle()` keeps today's behaviour: `_escalate` is
  kept behind the flag, renamed `_legacy_escalate`, and still de-duplicated by sha.
- A merge of a fix campaign calls `sentinel.mark_due(cfg, project, reason="fix merged")`. This new
  helper sets `due_since` and lets `check_due` return true even with zero reuses: a new
  `due_reason` key, persisted only in `red.json`, read by `check_due`. The green confirmation then
  runs on the next idle tick and does not wait for `interval_hours`. `backoff()` already lets a
  moved base run.

### 3. Queue (`queue.py`)

- **Campaign kinds** `fixforward` and `revert`, created by `redmain` only. The campaign id comes
  from `store.new_campaign_id(cfg, "FIX-<project>")` or `"REVERT-<project>"`. State keys:
  - `kind`, `project`, `priority: 1`, `full_suite: true`, `red_episode: <n>`, `profile` (the
    project default);
  - `repo` (worktree from the current base HEAD, branch `nw/fix-<episode>-<n>` or
    `nw/revert-<episode>`);
  - `pending_brief` (the fixforward brief), `spec_id: null`.
- `plan_step` for `fixforward` is the implementer path, as for a spec campaign. Two differences:
  `GATING` green goes straight to `CLOSING`, skipping `REVIEWING`, like `merge`/`split` at
  `queue.py:2188` and `:2575`; and the spec DoD check in close is skipped (no spec). For `revert`,
  OPENING runs the revert (§6) and then goes to `CLOSING`. A revert campaign has no GATING; its
  close gate is the full suite.
- **Admission.** Priority 1 already sorts first in `admission_key`. Fix campaigns may also take one
  slot over the profile's `slots` (`SLOT_STATES` accounting adds `+1` while a fix campaign is
  admitted), so a queue full of held CLOSING campaigns cannot starve the fix.
- **Tick step.** In `_tick`, `_red_main_step(cfg, ctx, report)` runs after `_sentinel_step`. For
  each project with an open episode it calls `redmain.step()` and acts:
  - `open_fixforward`: create the campaign and journal `red_main_fix_opened`.
  - `localize`: submit `redmain.localize` on the pool like a sentinel run, at most one at a time,
    sharing `pool.sentinel`'s slot. Store the result.
  - `open_revert`: create the campaign.
  - `escalate`: `escalations.plan_escalate(cfg, None, kind="red_main", topic="recovery",
    options=…, recommend=1, facts=…, frm="conductor")`, project set. Record the id in `red.json`.
    `topic` is a no-op until SPEC-0425 lands: pass it only if `plan_escalate` accepts it, using
    `inspect.signature`, removed once 0425 lands.

  Every action writes a queue decision record (`_record_queue_decision`, `by: queue`, rule
  `red_main.<action>`).
- **Escalation options**, recommendation 1:
  1. "open another fix-forward attempt (resume the episode)";
  2. "revert <culprit or newest suspect> by hand-resolved revert";
  3. "accept red for now: nw queue sentinel clear --project <P>".

  Facts: the failing ids, the culprit, the fixes tried and their outcomes. Choice 1 resets the
  failed count by one; `nw queue sentinel resume-fix --project P` applies it, see §7.
- **`nw queue merge --fixes-red`** (`plan_merge`/`apply_merge`, `cli_queue.py`) sets
  `fixes_red: true` and `full_suite: true` on the merge campaign, and priority 1. It is refused
  (ValueError) when the project has no open episode. A `--fixes-red` merge in flight counts as a
  fix in flight; when it merges, `redmain` records it in `fixes` with `kind: merge`.

### 4. Gates (`gates.py`, `remote_gates.py`, `remote_personas.py`)

`gate_env()` and the remote env dicts add `NW_AFFECTED_FULL=1` when `state.get("full_suite")`.
`tools/affected_tests.py` already forces the full suite on it (`forced_full`). Both loop and close
gates of a fix campaign therefore run the full suite.

### 5. Fix-forward brief and weakening check

- **Brief.** Built by `redmain.brief(cfg, episode)` and stored as the campaign's `pending_brief`,
  so `personas.render_pending_brief` shows it. It contains:
  - the failing ids (max 50) and the last `brief_log_lines` lines of each failing sentinel gate
    log (`sentinel.last_run` paths);
  - the merges with spec id, `git diff --stat target..merged`, and the culprit if known;
  - the rules: make the failing ids pass on the base. Never delete, skip, xfail or
    `importorskip` a test. Never weaken a guard test or its scan. Prefer fixing the newly merged
    code, or the newly merged test that violates a guard. Change nothing unrelated.
- **Implementer prompt.** `skills`-independent: `personas/implementer.md` gains a "Red-base fix
  (no spec)" paragraph, keyed on `campaign_kind == "fixforward"` in `build_context`. It replaces
  the spec section with the brief.
- **Weakening check** `close.fixforward_weakens(worktree, target_sha) -> list[str]`, run in
  `prepare_close` for `fixforward` only, before the gates. Over `git diff target_sha...HEAD` it
  reports:
  - a deleted `tests/**/*.py` file;
  - a net decrease in `def test_` definitions;
  - an added line matching `pytest.mark.(skip|skipif|xfail)`, `importorskip(`, `pytest.skip(` or
    `collect_ignore`;
  - any removed line in a module marked `tree_guard` (SPEC-0421; ignored until it lands).

  A non-empty result parks `fixforward_weakens`. That counts as a failed attempt, and redmain
  decides the park (`by: queue`), so the conductor is not woken.

### 6. Revert

`redmain.open_revert` creates the campaign. OPENING (`queue._open_revert`):

- creates the worktree at the base HEAD;
- runs `git revert --no-edit <culprit.target_sha>..<culprit.merged_sha>` (newest first; git
  orders the range);
- regenerates `merge.regenerate` paths on conflicts in those paths only, as close does
  (`_regenerate_after_rebase`);
- on any other conflict, `git revert --abort`, then parks `revert_conflict`. That counts as failed
  revert, and the episode escalates.

The reverted range includes the culprit's own spec status edit, so its spec returns to the status
it had before. The ledger is regenerated by the spec-lint gate. The revert campaign's merge note
names the reverted campaign. Its `red_main_cleared` follow-up, once green, mails the conductor
`kind: info` "SPEC-X reverted by <cid>; re-queue it". The conductor re-queues it (table row). For
`pr` / `auto-merge` projects, `revert` is skipped and the episode goes straight to `escalate` once
fix-forward is exhausted.

### 7. CLI (`cli_queue.py`)

- `nw queue sentinel status [--project P] [--json]` shows the episode: open, age, failing, merges,
  culprit, fixes, escalation (`nw.queue.red.v1`).
- `nw queue sentinel resume-fix --project P [--yes]` lowers the failed fix-forward count by one
  and lets the next tick open another attempt. The conductor applies escalation choice 1 with it.
- `nw queue merge --fixes-red`, from §3.
- `nw queue sentinel clear` also closes an open episode (`by: <actor>`, history `cleared`), which
  releases held merges. It keeps its current meaning for `force_full_close`.

### 8. Events, quiet rules, skills

- New events:
  - `red_main` (episode opened or failing set changed);
  - `red_main_fix_opened` (data: campaign, kind, attempt);
  - `red_main_cleared`;
  - `close_held` (campaign ledger event once per campaign per episode, plus a queue event).

  All are **always held** in `quiet.RULES`. The queue acts on them, and the conductor sees them in
  `conductor_digest`. The escalation, when filed, arrives the usual way.
- `skills/nw-conduct/SKILL.md` gains these rows:
  - `red_main`: informational. Check that `nw queue sentinel status` shows a fix in flight.
  - `red_main_cleared`: none, or re-queue a reverted spec when the info mail says so.
  - `close_held`: none.
  - `parked.fixforward_weakens` and `parked.revert_conflict`: decided by the queue, nothing to do
    unless an escalation follows.
  - `escalation` on `kind: red_main`: apply the human's choice. Choice 1 is `nw queue sentinel
    resume-fix --project P --yes`.
  - `message.request` gains: a steward's "merge <branch> fixes-red" runs `nw queue merge <branch>
    --by conductor --fixes-red` (dry run, then `--yes`) with no human OK. A branch the conductor's
    own sub-agent fixed in a worktree may be merged the same way when the episode's automatic
    fixes are exhausted.
- `skills/nw-steward/SKILL.md`: a hand-made fix for a red base is requested as "merge <branch>
  fixes-red", never "wait for the human".

## Alternatives considered

- **Keep escalating, add a recommendation.** Lost: the human still babysits each episode, and the
  sha de-dup bug remains.
- **Revert first, fix forward second.** Lost: the culprit is usually a correct feature that
  collided with a guard landing in parallel, as in both audited cases. Reverting throws away
  reviewed work that a small fix keeps.
- **Freeze all merges and admissions while red.** Lost: admissions and persona work are fine on a
  red base; only merges compound it. Holding at close is enough.
- **Run reviewers on fix-forward.** Lost: it adds 3 opus runs and 20 to 40 min to every red
  episode. The full suite and the weakening check cover the real risks: a "fix" that deletes or
  skips the failing test, or weakens a guard.
- **A new `fixer` persona.** Lost: the implementer persona with a fix brief does the same job.
  There is one less persona definition to tune.
- **Git bisect over every commit.** Lost: campaigns are the unit of merge and revert, so the
  bisection runs over campaign ranges only.

## Acceptance criteria

- [x] AC1: Red sentinel.
  - A red `sentinel.run` opens an episode in `red.json` with the failing ids, fingerprint and
    merges since the last green.
  - It appends one `red_main` event and files **no** escalation.
  - A second red run with the same failing set at a new sha appends no event and files no
    escalation.
- [x] AC2: Fix-forward campaign. Within one tick after AC1, a `fixforward` campaign exists with:
  - priority 1, `full_suite: true`;
  - a `pending_brief` naming every failing id (up to 50) and each merge's campaign;
  - loop and close gates that run the full suite (`NW_AFFECTED_FULL=1` in the gate env).
- [x] AC3: Fix-forward flow.
  - The campaign skips REVIEWING. It merges ff-only after a green full suite.
  - A diff that deletes a test file, removes test functions, or adds a skip, xfail,
    `importorskip` or `collect_ignore` parks `fixforward_weakens`, which counts as a failed
    attempt.
- [x] AC4: Hold while red.
  - While the episode is open, a non-fix campaign of the project reaching close stays CLOSING
    with one `close_held` ledger event. It does not park, its close attempts do not grow, and it
    does not merge.
  - `fixforward`, `revert`, `--fixes-red` and `--docs-only` merges are not held.
- [x] AC5: Green sentinel.
  - After a fix merges, the sentinel runs without waiting for `interval_hours`.
  - A green result closes the episode, withdraws its escalation (if any) and every open legacy
    "Sentinel red on <project>" escalation, abandons unmerged non-closing fix campaigns as moot,
    and releases held campaigns. They merge on their next tick.
- [x] AC6: Revert.
  - After `fixforward_attempts` failed attempts, `localize` names the culprit merge using only
    the failing ids: with one candidate, without running tests.
  - A `revert` campaign reverts exactly that campaign's `target_sha..merged_sha` range and merges
    after a green full suite.
  - A revert conflict parks `revert_conflict` and escalates.
- [x] AC7: Escalation rule.
  - An episode escalates at most once: topic `recovery` when supported, recommendation 1.
  - It escalates only when open longer than `escalate_after_s` with no fix in flight, or when
    automatic fixing is exhausted (AC6 conflict, red after the revert, or no culprit).
- [x] AC8: `--fixes-red` merges.
  - `nw queue merge <branch> --by conductor --fixes-red --yes` creates a priority-1, full-suite,
    hold-exempt merge campaign.
  - It is refused when the project is not red.
  - The conductor skill allows it without a human OK.
- [x] AC9: Escape hatch. `agentic.red_main.enabled: false` restores today's behaviour exactly:
  escalate per red sha, no hold, no fix campaigns.
- [x] AC10: Skill and events.
  - The conductor skill has the `red_main`, `red_main_cleared`, `close_held`,
    `parked.fixforward_weakens` and `parked.revert_conflict` rows, and the `--fixes-red` merge
    rule.
  - `quiet.RULES` holds the four new events.
  - The skill and `quiet.RULES` comparison test passes.

## Test plan

- **Automated tests**
  - `tests/test_redmain.py` (new; tmp data dir, real git repo in `tmp_path`):
    - episode open and update; fingerprint de-dup (AC1);
    - merges computed from real `close` events against real ancestry;
    - every `step()` branch, including the escalate-after-threshold clock via `now` (AC7);
    - `holding()` for each campaign kind (AC4);
    - `on_green` withdraws episode and legacy records and abandons moot fixes (AC5).
  - `tests/test_redmain_localize.py` (new). The real fixture repo has 4 merge commits made by
    real `close` code paths (or `git` plus synthetic `close` events), the third breaking
    `tests/test_t.py::test_b`. `localize` names merge 3 in ≤ 3 probes, each a real `pytest` run of
    one id. With one candidate it runs no pytest (AC6).
  - `tests/test_agentic_sentinel.py` (extend):
    - a red `settle` calls `on_red` and never `plan_escalate` (patched to raise);
    - `enabled: false` keeps the legacy escalate-per-sha path (AC9).
  - `tests/test_agentic_close.py` (extend):
    - `fixforward_weakens` cases: deleted test, net-fewer tests, added skip/xfail/importorskip,
      unchanged (AC3);
    - `prepare_close` holds a spec campaign while red and does not count an attempt (AC4).
  - `tests/test_agentic_gates.py` (extend): `NW_AFFECTED_FULL=1` in `gate_env` for `full_suite`
    states, absent otherwise; same for the remote env dict (AC2).
  - `tests/test_agentic_queue_merge.py`:
    - `--fixes-red` sets flags and priority, and is refused when not red (AC8).
  - `tests/test_skills.py`: the rows and the `--fixes-red` rule exist (AC10).
- **Real verification (beyond fakes)**, in `tests/test_redmain_e2e.py`. The fixture is a real git
  repo with `.nw/project.yaml` whose loop and close `pytest` gates run real `python3 -m pytest`
  over a 3-test tree, a real `NW_DATA_DIR`, real `sentinel.run()` and real `queue.tick()` calls.
  Only the implementer persona is a scripted `FakeRunner` that edits a file. Steps:
  1. Commit a failing change on `main` directly. `sentinel.run` → red → episode, no escalation.
  2. Tick → `fixforward` campaign. Queue a normal spec campaign that reaches CLOSING → held.
  3. FakeRunner fixes the file. Real loop gate (full) → CLOSING → merged.
  4. Next tick: the sentinel runs (AC5) → green → episode closed, the held campaign merges.
  5. Repeat with a FakeRunner that cannot fix (twice) → localize → revert campaign → real `git
     revert` → merged → green.
  6. Repeat with a conflicting later commit → `revert_conflict` → exactly one escalation.
- **Manual verification (real persona)**, recorded in As shipped. In a scratch `NW_DATA_DIR` and
  config with the fixture repo above registered as a project and profile `frugal` with the
  implementer model set to haiku:
  1. Break the base.
  2. `nw queue sentinel run --project <fixture>`.
  3. `nw queue run --once` until the fix-forward campaign merges.
  4. Confirm with `nw queue sentinel status --json` that the episode closed, and that `nw
     escalations list` shows no new record.
- **Live check after landing**, recorded in As shipped: the next real red episode on Meta-Tools
  files ≤ 1 escalation, opens a fix-forward within one tick, and no non-fix campaign merges
  while it is red (`nw queue sentinel status`, `nw queue events --type close_held`).
- **Regression guard**: affected tests (`python3 tools/affected_tests.py --run uv run pytest -q`)
  and `python3 tools/spec_lint.py`. The existing sentinel and close tests pass unchanged with
  `red_main.enabled: false`.

## Clock Log

CLOCK-IN: [timestamp]
CLOCK-OUT: [timestamp]

## Rollout / migration

1. Land with `agentic.red_main.enabled` defaulting to true. No state migration: `red.json` is
   created on the first red run.
2. The first green sentinel after landing withdraws the legacy ESC-0024 and ESC-0025 (AC5).
3. Rollback: set `agentic.red_main.enabled: false` (AC9).

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## As shipped

Landed on 2026-11-04. It ships `src/nightwright/agentic/redmain.py` (episode state
`red.json`, `settings`, `on_red` / `on_green`, `holding`, `refresh`, `step`, `localize` /
`bisect`, `brief`, fix-campaign creation, `escalate`, `resume_fix`, `clear`, `status`).
The rest is wired in `sentinel.settle`, `close.prepare_close` (hold, `fixforward_weakens`,
`revert_campaign_range`), `queue._red_main_step` (tick step, admission, revert open, held-close
apply, close-train order), `gates.full_suite_env` (local and both remote job headers; the
worker's `ENV_ALLOWLIST` gains `NW_AFFECTED_FULL`), `nw queue merge --fixes-red`, and
`nw queue sentinel status|resume-fix` with `clear` closing the episode. It also adds the
events, `quiet.RULES`, the `nw-conduct` / `nw-steward` rows and the `operate.md` park rows.

**Deviations from Design (decided while building):**
- **The existing open escalations (ESC-0024/0025/0026).** They are **withdrawn as
  superseded, never adopted** as the episode's own record. Adopting one would use up the
  episode's single escalation on a question with no recommendation. The first tick after
  landing sees `force_full_close` with no `red.json` (`adopt_legacy`). It opens no fix,
  because the failure may already be fixed on the new base. It marks the sentinel due
  (`due_reason: legacy red adopted`), so the next idle tick runs the sentinel at the current
  base.
  - If that run is red, it opens episode 1 and withdraws every open "Sentinel red on
    Meta-Tools" escalation (`by: queue`, "superseded by red episode 1"). A fix-forward opens
    on the next tick.
  - If it is green, `on_green` withdraws them as resolved.
- **Moot fix campaigns are abandoned by the tick (`sweep`), not inside `on_green`.**
  `on_green` runs on the sentinel's worker thread, or in a separate `nw` process for a CLI
  run, and must not abandon or park campaigns there. The next tick abandons a parked, queued
  or waiting fix and park-requests an active one. A CLOSING fix finishes.
- **Failed fix campaigns are abandoned by the queue.** `refresh` counts a fix PARKED for any
  reason without `park.auto` as failed. `fix_in_flight` keys on live campaigns of the episode,
  so a conductor resume cannot run two fixes at once. A fix campaign's park carries
  `red_main: <episode>`, which the queue event passes on and `quiet` holds, so the conductor
  is not woken by it.
- **Merge order is base order.** Merges are sorted by the merged sha's commit depth.
  Same-second merges sorted by close time put the wrong campaign first: the e2e test caught
  it.
- **Localize probe command.** It is configurable as `agentic.red_main.probe_cmd` (default
  `uv run pytest -q -p no:cacheprovider`), with `probe_timeout_s` (900). An id whose file does
  not exist at a probed sha counts as passing. Probes are at most ⌈log2 n⌉ + 2: the answer's
  own merged sha is verified, and probes are cached by sha.
- **A held merge emits no transition.** The close step returns a `held` outcome before
  follow-ups, the rebase or the gates. The apply writes `closing.held_episode` and one
  `close_held` ledger event per episode. A red-base fix sorts first in its close train, so
  held closes never block it.
- **Fix campaign names.** The branch is `nw/<campaign id>` (unique across attempts), not
  `nw/fix-<episode>-<n>`. Fix campaigns are admitted at once, outside `pick_admissions` (the
  +1 slot).
- **Revert commit.** The revert is one commit (`git revert --no-commit` over the range;
  conflicts in regenerate paths take HEAD's side and are regenerated at the end).
- **Hold scope.** The hold applies only to `merge`-policy projects; PRs do not land on the base
  here.
- **`topic: recovery`.** It is passed only when `plan_escalate` accepts `topic` (checked with
  `inspect.signature`; SPEC-0425 not landed yet).

**Test record (2026-11-04).**
- `tests/test_redmain.py`: 22 tests. They cover episode open and update with fingerprint
  de-dup, merges from real `close` events and real ancestry, the newest-20 window, legacy
  withdraw on open, every `step()` branch including the `escalate_after_s` clock, `holding`
  per kind, a `prepare_close` hold that touches nothing, the `on_green` withdrawals,
  `adopt_legacy` with `check_due`, `bisect`, 9 `fixforward_weakens` cases, the local and
  remote full-suite env (the worker accepts the key), the affected tool honouring the flag,
  and status / resume-fix / clear.
- `tests/test_redmain_localize.py`: 3 tests. With real pytest probes of one id over four
  merges, merge 3 is named in 2 probes. One candidate runs no pytest. A running sentinel
  defers localize.
- `tests/test_redmain_e2e.py`: 4 tests on a real repo with a real pytest gate, a real data
  dir, real `sentinel.run`, real ticks (`nw queue tick` via the CLI in the first test), and a
  FakeRunner implementer.
  - Red, then one fix-forward campaign. A non-fix merge request is held: CLOSING, one
    `close_held`, 0 close attempts. The fix merges through the full suite with no REVIEWING.
    The sentinel is due at once, and green releases the held merge. No escalation.
  - Two fixes that delete the failing test are both parked `fixforward_weakens` and
    abandoned. `localize` names the culprit merge, and the revert merges through the full
    suite, back to green, with 0 escalations.
  - A revert conflict parks `revert_conflict` and files exactly 1 escalation (`kind:
    red_main`, recommendation 1), still 1 after more ticks and another red run. A green base
    withdraws it.
  - `--fixes-red` is refused while green. While red it gives priority 1 and the full suite,
    is not held, merges, and is recorded as `kind: merge`.
- `tests/test_agentic_sentinel.py`: a red `settle` opens an episode with `plan_escalate`
  patched to raise. The legacy tests run with `red_main.enabled: false` and pass unchanged
  (AC9).
- `tests/test_skills.py`: the new rows and the `--fixes-red` rule. The quiet-rules comparison
  passes.
- Affected tests via `tools/affected_tests.py` pass. The full suite is left to the close
  gate.

**Manual verification (real persona, 2026-11-04).** Scratch `NW_CONFIG_DIR` / `NW_DATA_DIR`
under `/home/bruno/work/.nw-tmp/0420/manual`, with a fixture repo registered as project
`Fixture` (outbox target) and profile `frugal` with implementer `haiku`, effort low, $0.50
budget.
1. Broke `mod.py` on main and ran `nw queue sentinel run --project Fixture`: red, episode 1,
   failing `tests/test_t.py::test_a`, no escalation.
2. `nw queue run --once` ticks adopted the detached haiku run. `nw queue run --until-idle`
   then ran the rest: the haiku implementer (one run, 22 s, $0.0088) restored `return 1`,
   the full-suite gate passed, and the campaign merged ff-only.
3. The sentinel became due and ran in the same session: green, episode closed.
4. `nw queue sentinel status --json` shows `open: false` with fix `outcome: merged`.
   `nw escalations list --all` shows none. Events: 1 `red_main`, 1 `red_main_fix_opened`,
   1 `red_main_cleared`.

**Live check after landing.** Recorded by the conductor on the next real red episode (≤ 1
escalation, a fix-forward within one tick, `nw queue events --type close_held`). This spec
predicts the ESC-0024/0025/0026 handling above.

## Open questions

_None._
