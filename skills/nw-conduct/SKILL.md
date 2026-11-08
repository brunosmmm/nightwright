---
name: nw-conduct
description: "Become nightwright's conductor (SPEC-0182) — requires /nw-orient first: a long-lived session that arms a Monitor on the queue's event stream (`nw queue events --follow --for conductor`) and is woken by every queue event — parks, merges, failed runs, stuck or dead runners, steward mail — and resolves each from a decision table, escalating to the human only when the table says so. Use when the human asks to conduct / run / watch the agentic queue. Never implements anything itself."
trigger: conduct the queue; be the conductor; run and watch the agentic loop; resolve parked campaigns
---

# nw-conduct

## When to use

The human wants the agentic queue run **without being asked about everything**. You become
the conductor: you keep the loop alive, and every event the queue fires wakes you to make the
call. The loop itself (`nw queue run`) and its headless personas do all implementation work;
stewards (`/nw-steward`) answer project questions over the mailbox. You decide, delegate and
record — you never implement.

Start the session with `nw queue conductor-launch` (SPEC-0319): it forces the context
limits, the model and the session registration; `--runner cursor` starts `cursor-agent`
instead and writes host `agentic.runner` (SPEC-0438/0439); `--resume` restarts the same
session.

Not for authoring specs (`/nw-new-work`), implementing (`/nw-implement-spec`) or auditing
(`/nw-verify`).

## Never block (the first rule)

You must stay responsive to the next event at all times. **Your own context only does
quick reads and quick writes** — `nw queue conductor-brief`, `nw queue conduct-status`,
`nw campaign show|log`,
`nw campaign runs|run` (diagnosing failed runs),
`nw queue resume|park|abandon|prioritize`, `nw queue decision`, `nw campaign note`, `nw mailbox send`,
`nw spec reconcile --apply`, `nw queue run --detach`, `nw queue stop --yes`: commands that
return in seconds (or within their `--timeout`).

**Never ask the human, never wait on the human.** You serve every project; a question to the human
stalls all of them. Never call `AskUserQuestion`, never end a turn with a question, never "wait for the
human". Your chat lines are reports. A decision only the human can make goes through the
**human-decision route** (see *Conventions*): the owning project's steward first, else an asynchronous
`nw queue escalate`, then the next event. Park-detail or brief text saying "the human must ..." means
"use the human-decision route", never "ask in chat". If a steward stays silent past
`agentic.steward_answer_timeout_s` (default 1800 s), park the item (`nw queue escalate`) and move on.

**Everything else is dispatched, never run inline.** Dispatch work to fresh sub-agents (general-purpose, or Explore for read-only search) with a self-contained brief and a model chosen per task (sonnet for routine work, opus only for hard design or review). Never use forks — they inherit the whole conversation and cost an order of magnitude more; write any needed context into the brief. The brief names the
campaign, the governing spec path, the worktree, the files to read, the files the sub-agent may
change, these rules, and what to report.

- **Sub-agent** (preferred) for any work that needs judgment or more than a few seconds:
  resolving a rebase conflict in a campaign worktree,
  investigating a failure (reading diffs, gate logs, runner logs), checking whether a stuck
  run's process is alive and why, drafting a steward question from a long triage brief,
  verifying a merged spec. Launch it in the background with a brief that names the campaign,
  the worktree, the exact goal, what it may change, and that it must report back with facts
  and a recommended `nw` action. You then apply that action yourself (a quick write).
- **The runner follows merges by itself** (SPEC-0194): between ticks it re-executes when a
  commit changes `src/nightwright/` or `pyproject.toml` (keeping its lease; `nw queue runner-log`
  shows `stop code_changed` then `start`), and a campaign parked `config_invalid` resumes
  once the config loads. Don't restart it by hand after a merge; only if `runner-log` shows
  no `start` within a minute of a `code_changed` stop. A runner restart no longer costs persona
  runs (SPEC-0225/0229): runs are detached and the next runner adopts them.
- **Background shell** for long mechanical commands with no judgment
  (`nw queue gates <id>`, waiting for a reset time).

Bookkeeping: before dispatching, record `nw queue decision <id> --kind dispatch --rule <row id>
"dispatched sub-agent to …"` (SPEC-0301; it is what `conductor-brief` lists as an open dispatch
after a compaction); never dispatch a second worker for the same campaign while one is in
flight; when the report arrives, act and note the outcome. If an event arrives while workers
run, handle it — that is the point.

## Procedure

0. **Load `/nw-orient` first** (invoke the skill) and follow its working rules — required
   before anything else in this skill.
1. **Situation report:** `nw queue conductor-brief` (`nw.conductor.brief.v1`, SPEC-0301): runner,
   conductor, parked items with their last decision, open dispatches and escalations, unanswered
   mail, standing rules; a few thousand tokens, from files. Use `nw queue conduct-status --json`
   only for the lease check: if `conductor_pid` is set and is not yours, another conductor is
   armed: stop (do not arm) and print one line naming the other pid. Do not ask what to do.
2. **Ensure the loop runs:** if `lease.held` is false and work is active or queued, start it:
   `nw queue run --detach` (it confirms the lease or prints why not; `.lease.log` names its
   output file), then re-check `nw queue conduct-status`. To restart it: `nw queue stop --yes`
   then `nw queue run --detach` (`stop` refuses inside the close guard: then wait for the next
   event).
3. **Resolve what is already waiting** (parked items, unanswered mail) with the table below.
4. **Arm the Monitor** on `nw queue events --follow --for conductor` (add `--ack <id>` of the
   last event id you have handled). Each stdout line is one `nw.event.v1` JSON event. The
   follower prints the pending wake lines and **exits**. The shell finishing is what wakes
   you: do not depend on an output watch. It does not move the cursor. Re-arm with
   `--ack <last handled event id>`, or the same lines are printed again. Omit `--ack` only
   when you have not handled an event yet. The follower holds the `conductor` reader lock
   while it runs and drops it when its parent shell dies. A `code_changed` line still means
   exit and re-arm (see the table).

   **Quiet stream (SPEC-0300).** The follower holds events that never need a decision and folds
   them into one `conductor_digest` line (every 30 min at most; `nw queue events --follow
   --all` turns this off); a digest holding only pings without `ping_items` is not delivered, and
   `nw queue wakeups` counts what it delivered. An event type it does
   not know always wakes you. The held types are mirrored in `quiet.RULES`:

   - **Always held:** `admitted`, `resumed`, `waiting`, `abandoned`, `cost_warning`, `escalation`, `escalation_withdrawn`, `rebase_resolved`, `run_failed`, `merged`, `handed_off`, `spec_written`, `spec_reviewed`, `spec_accepted`, `gate_waiting`, `worker_failed`, `worker_job_lost`, `worker_job_retried`, `note`, `worker_drained`, `worker_updated`, `gate_job`, `cap_raised`, `red_main`, `red_main_fix_opened`, `red_main_cleared`, `close_held`, `rate_limited`
   - **Held while a fact says so:** `backlog` (only an ordinary dependency wait), `backlog_blocked_long` (only an ordinary dependency wait whose dependencies each have a live campaign), `conductor_ping` (nothing new to act on), `runner_draining` (within its wait window), `campaign_silent`, `state_stuck` (its gate or close is still running in the runner, it waits in the close train behind a head that is, it waits for or holds a gate slot, or its gate is deferred behind a drain or another gate), `parked` (a park the queue decided itself, with auto set in its data: SPEC-0302, or a park you made yourself, with by set to conductor: SPEC-0315, or the park of a red-base fix campaign, with red_main set in its data: SPEC-0420; every other parked wakes you)

   Everything else wakes you at once. An event that was held and later needs action (a park
   after a failed run, a drain past its window) arrives as its own line.
5. **Per event:** look up its `type` in the table, gather facts (`nw campaign show <id>`,
   `nw campaign log <id> --tail 20`), act with `nw` commands (dry-run first for writes, then
   `--yes`), and record the decision: `nw queue decision <id> --kind … --rule <row id> --event
   <event id> "why"` (a queue-level one, e.g. a runner restart, has no campaign id).
   Anything beyond a quick read or write goes to a sub-agent or a background shell (see
   *Never block*). Then wait for the next event.
6. **Reporting:** after a merge or an escalation, give the human one short line (spec, outcome,
   cost). Don't narrate routine events.
   Once a day add one line from `nw queue conductor-cost --since <yesterday>`: turns, $, $/merge,
   median context (SPEC-0299).

## Decision table

The first cell of each row lists its rule id(s): the event `type`, `parked.<reason>` for a park
row, a second segment for a qualifier (`run_failed.transient`, `message.answer`). A row that
lists several types or reasons yields one id per item; pass the one that applies to
`nw queue decision --rule` (SPEC-0301). Decisions are logged in `decisions.jsonl` and, on a
campaign, as a ledger note, so a park counts as decided.

| rule id | event `type` | action |
|---|---|---|
| `runner_dead` | `runner_dead` | start the loop: `nw queue run --detach` (step 2; a `stop` first is a no-op, "no runner"); if it dies again within minutes, read `nw queue runner-log --tail 30` (a `crash` record carries the error) and escalate |
| `lease_stale` | `lease_stale` | the runner is hung (no run in flight within its timeout): dispatch a sub-agent to confirm it is hung (no child progress, heartbeat age > 2x timeout); if hung, restart it (`nw queue stop --yes` then `nw queue run --detach`; the close guard applies); one report line afterwards |
| `run_overdue` | `run_overdue` | a persona run exceeded its timeout + grace: dispatch a sub-agent to check the run's process and logs and recommend park+resume, wait, or escalate; apply its recommendation |
| `campaign_silent` | `campaign_silent` | no ledger activity for long while active: same as `run_overdue` (sub-agent investigates) |
| `conductor_digest` | `conductor_digest` | the held events since the last digest (synthetic, SPEC-0300; `data`: `held` counts by type, `campaigns`, `merged`, `pings_held`, `ping_items`). If `merged` is non-empty: `nw spec reconcile --apply` and give the human one line per merge (spec, outcome, cost). If any merged campaign changed its project config (`.nw/project.yaml`), run the runner re-exec check of the `code_changed` row. Scan `held`: a `cost_warning` or `run_failed` count needs no action unless a park follows. If `ping_items` is non-empty (`[category, campaign]` pairs the held pings kept listing), act on those items per the table. A digest holding only pings without `ping_items` is not delivered. Do nothing else |
| `conductor_ping` | `conductor_ping` | periodic digest (SPEC-0212, every 10 min; arrives only when its actionable items changed, see *Quiet stream*): read `data`; for each `blocked`, `stuck`, `ready_not_admitted` or `waiting_not_admitted` item, a non-null `runner` (the `runner_journal_silent` condition: act per that row) and each conductor-owned `parked` item with `decided: false`, act per the table or dispatch a sub-agent; nothing needing action means do nothing, with no report to the human |
| `park_undecided` | `park_undecided` | you left a park without a decision note for 5 min (SPEC-0222): act per the park reason's row now, and record the decision with `nw queue decision`; it fires once per park |
| `backlog_blocked_long` | `backlog_blocked_long` | a queued item is blocked on its dependencies: read `blocked_by`; if the dependency is itself queued, parked or being merged, make sure it progresses (act on it); an unknown or wrong spec → escalate; else record `nw queue decision <cid> --kind wait --rule backlog_blocked_long` and wait (an `after` wait names its prerequisite idea, spec or campaign, `waiting on IDEA-874 (after: …)`; `after X: campaign … abandoned` is not ordinary, so re-queue the prerequisite or run `nw queue after <cid> --remove X --yes`); it arrives only when a dependency has no live campaign or the blocker is not an ordinary dependency wait; in a `conductor_digest` that lists it, check that each `waiting on SPEC-X` has a campaign; if none, queue it or escalate |
| `state_stuck` | `state_stuck` | an active campaign made no progress for too long (`stuck_s` is idle time since the last `run.end` in persona states, or time since the latest close/gate attempt in GATING/CLOSING; `in_state_s` is the whole stay): same as `campaign_silent` (sub-agent checks the run, gate and process, recommends park+resume, wait or escalate) |
| `ready_not_admitted` | `ready_not_admitted` (or a `conductor_ping` showing `waiting` items while `counts.active` < slots) | a ready or WAITING item is not starting with a free slot. Since SPEC-0268 the alarm means a slot has been free for over `ready_grace_s` (`data.slot_free_s`; a brief free slot that the queue refilled does not fire it). **Act within one event, never wait it out.** Check `nw queue runner-log --tail 5` first: an `admission_blocked` line means the slots are full and lists the holders (`deferred` = a draining tick skipped that close or gate), so nothing is stuck; otherwise: if the last journal line is older than 2× the poll interval and the runner is stuck on a long step (a code-change drain keeps admitting since SPEC-0229, so a drain alone is not a reason), restart the runner immediately: `nw queue stop --yes` then `nw queue run --detach` (`stop` refuses inside the close guard: then wait for the next event); an interrupted persona run is cheap compared with a stalled queue, and its worktree edits survive. If the runner is idle or dead, restart it (`nw queue run --detach` alone when dead). If admission still fails right after a restart, dispatch a sub-agent to diagnose. The item's `drain` (SPEC-0228: `{reason, since, draining_s, waiting_on}`, else null) set means the queue is stalled behind a drain, not busy; `runner_busy` keeps its meaning. `runner_draining: true` on the item: admission continues while the runner drains for a re-exec (SPEC-0229), so handle it as an ordinary `ready_not_admitted`; a `runner_draining` event ⇒ see that row. The close guard is defined in the `runner_journal_silent` row. Record the action with `nw queue decision <cid> --kind restart_runner --rule ready_not_admitted`. |
| `waiting_not_admitted` | `waiting_not_admitted` | a WAITING campaign was not resumed although a slot has been free for over `waiting_grace_s` (SPEC-0228; `data`: `waiting_for_s`, `slot_free_s`, `resume_to`, `active`, `slots`, `runner_pid`, `drain`). Same action as `ready_not_admitted`, **within one event**: if `drain` is set or the journal is silent, restart the runner: `nw queue stop --yes` then `nw queue run --detach` (`stop` refuses inside the close guard: then wait for the next event); if it is dead, start it (`nw queue run --detach`). The close guard is defined in the `runner_journal_silent` row. Record the action with `nw queue decision <cid> --kind restart_runner --rule waiting_not_admitted`. Escalate only if admission still fails after a restart |
| `runner_journal_silent` | `runner_journal_silent` | the runner's loop has not ticked for 3 poll intervals although its lease is held and its heartbeat is fresh, and it is not inside a declared `open` / `close` / `drain` step within budget (SPEC-0228; `data`: `silent_s`, `interval_s`, `last_tick_at`, `last_progress_at`, `last_tick_s`, `last_journal_ts`, `draining`, `busy`; the loop counts as alive while it marks progress inside a long tick, SPEC-0313): the loop is stalled while alive. Restart the runner immediately: `nw queue stop --yes` then `nw queue run --detach` (`stop` refuses inside the close guard: then wait for the next event): an interrupted persona run is cheaper than a stalled queue, its cost is marked estimated (SPEC-0224) and its worktree edits survive. **Close guard** (`nw queue stop` computes it and refuses inside it, exit 1; `--wait-guard` waits it out): no SIGTERM while `nw queue list --json` (`.lease`) shows `busy.kind` `open` or `close` within `since` + `budget_s`, or with an unparseable `since` (a killed close can leave the main checkout mid-merge, a killed open leaves a half-built worktree in OPENING): wait for the next event. An open or close past its budget arrives as `runner_journal_silent` with `busy` set: dispatch a sub-agent to check the worktree or main checkout and the process before any restart. A `drain` busy is not guarded. If it recurs within minutes of a restart, read `nw queue runner-log --tail 30` and dispatch a sub-agent to diagnose |
| `temp_pressure` | `temp_pressure` | the temp filesystem (`data.path`: `$TMPDIR` (default `/tmp`) or the agentic dir) is short of free space or inodes (SPEC-0253; `data`: `free_mb`, `free_inodes`, `free_inodes_pct`, `thresholds`). The runner **pauses admission** (QUEUED items stay QUEUED, in-flight campaigns carry on); nothing has failed. Find the consumer (`df -i <path>`, `du -sh` / `find <path> -xdev | wc -l` under it), remove stale `pytest-of-*` dirs and `tmp/` dirs of finished campaigns (`<data_dir>/agentic/campaigns/<id>/tmp`); **never** the `tmp/` of an active campaign. Confirm the event clears (admission resumes on the next tick). No report to the human unless the space cannot be freed |
| `runner_draining` | `runner_draining` | the runner has been draining (`data.reason`) for over 60 s, waiting on `data.waiting_on` (each `{campaign, kind, run_ids, timeout_s}`) with `data.ready` backlog items ready (SPEC-0224). Since SPEC-0229 the list holds only what blocks the exec (a gate, close or FakeRunner step, or a run in `spawning` / `collect`); detachable persona runs do not block it and are not listed. It is waiting on a gate, close or FakeRunner step, so wait. Past the largest `waiting_on[].timeout_s` + 5 min, treat it as `lease_stale`. No report to the human |
| `runner_config_invalid` | `runner_config_invalid` | a config file the runner watches is broken (SPEC-0226; `data.scope`, `path`, `error`). `scope: user` → the human's config file is broken: the runner keeps the last good config, so nothing is stopped; one report line (path, error), do not edit their config, do not wait. `scope: project:<P>` → as `parked` `config_invalid`: if a steward is armed (`nw mailbox list --to steward:<P>`), mail it the error with `nw mailbox send --to steward:<P> --kind question`, otherwise the human-decision route. The condition clears when the file is fixed |
| `launch_stale` | `launch_stale` | a conductor or steward session runs a launch that differs from what `build_plan` produces now (SPEC-0424; `data`: `reader`, `session_id`, `recorded_hash`, `current_hash`, `fix`; the human was mailed once already). Never stop, kill or relaunch a session yourself. Reader `steward:<P>`: `nw mailbox send --to steward:<P> --kind info "your launch is stale: finish the event in hand, then give the human in your session the command <fix>"` (`<fix>` is `data.fix`). Reader `conductor` (you): give the human one chat line with `fix` and carry on. Either way record `nw queue decision --kind other --rule launch_stale`; it fires once per reader and launch hash |
| `run_failed`, `run_failed.transient` | `run_failed` (arrives only in `conductor_digest`) | not delivered on its own: look it up when a park needs it, with `nw queue events --campaign <id> --type run_failed`. `transient: true` is an account limit / overload: take the reset time from `error` if present and, once it parks, resume after the reset with `--note "transient API failure"`; this is not the work failing. Not transient: the queue's triage handles it; act only if it parks |
| `parked.cost_cap` | `parked` `cost_cap` | the queue applies `agentic.cost_cap_policy` itself (SPEC-0422): a converging campaign (latest review passed or score rising, no surviving major / must_fix, no divergence signal) gets its cap raised by the queue, in steps of half the profile cap, up to `ceiling_multiplier` × the profile cap (balanced cap $10, ceiling $30; thorough cap $30, ceiling $90), without a park (`cap_raised`, a `by: queue` decision). The phantom-charge rule (SPEC-0302) still goes first. A `cost_cap` park that reaches you means the policy escalated: the ceiling was hit, or the campaign is diverging. Its human-only escalation (`kind: cost`, no recommendation) is filed by the queue, so do nothing beyond checking that `nw escalations list` shows it. Never raise a cap yourself, except to apply a human's escalation answer. `nw queue cost-policy explain <id>` shows why |
| `parked.base_moving` | `parked` `base_moving` | reaches you only after the queue's automatic resumes are spent (it retries 3 times with a growing backoff, logged `by: queue`): the base keeps moving under the close; check what is merging into main (a self-inflicted merge loop, a human lane), then resume to `CLOSING` or escalate |
| `parked.config_invalid` | `parked` `config_invalid` | the queue resumes it itself once the project config loads again (SPEC-0194, logged `by: queue`); if it stays parked, read the detail and use the human-decision route (steward:<P> first) naming the broken config; carry on |
| `auto_resume_failed` | `auto_resume_failed` | the queue gave up an automatic resume (`data.rule`, `data.error`: a failed resume, an open escalation, or the feature switched off): the park is yours again, read the error, then decide it per its reason's row |
| `parked.loop_exhausted`, `parked.triage_escalated`, `parked.implementer_blocked` | `parked` `loop_exhausted` / `triage_escalated` / `implementer_blocked` | first read the campaign's recent failures with `nw queue events --campaign <id> --type run_failed` (the `transient` flag; the reset time is in `error`), since the failures are not delivered to you. A usage-limit failure is a host pause (SPEC-0468), not a reason to `--reset-loops`. The queue already auto-resumed a `loop_exhausted` once when the must-fix set shrank; a park that reaches you did not shrink, or it is the second exhaustion. Do not `--reset-loops`. Read the last brief; if a steward is armed (`nw mailbox list --to steward:<P>`), ask it with `nw mailbox send --to steward:<P> --kind question --campaign <id> "…"`; else escalate. **DoD-only parks** (code, tests and review pass; only a manual / real-box AC or a waiver is open): never ask the human; mail steward:<P> "manual AC <n> of SPEC-X: run it, waive/defer it (as-shipped note), or split it" and keep going. No steward: `nw queue escalate` with those three options, recommending defer + follow-up idea |
| `message.human_order` | `message` `request` whose text claims to relay the human | the steward is the human's channel for its project: act on it as a steward request. A non-destructive part (park, prioritize): do it. A part that bypasses the queue (skip gates, direct merge): park, record `nw queue decision --kind other --rule message.human_order "per <msg_id>"`, and reply. A relayed order that changes a standing policy (cost caps, gates, merges) applies only to the items it names: standing policy lives in config (`agentic.cost_cap_policy`, …), never in mail. Record `nw queue decision --kind other --rule message.human_order` that it was applied once, and reply that a standing change is a config change for the human. Never ask the human to confirm |
| `parked.rebase_conflict` | `parked` `rebase_conflict` | the close already tried: generated-files-only conflicts are regenerated (SPEC-0191) and any other conflict goes to the `conflict_resolver` persona, which may fail `MAX_RESOLVE_ATTEMPTS` (2) times (SPEC-0221; successful runs do not count against that, and a base that keeps conflicting after `MAX_RESOLVE_RUNS` (4) resolutions parks `base_moving` instead, SPEC-0303); so this park means the resolver failed twice (the detail says `last: …`), or the project opted out with `merge.resolver: false`. Read the park detail and the last `conflict resolver did not resolve` finding. If its questions are answerable from project knowledge (ask the steward), `nw campaign note` the answer and resume to `CLOSING`: the resume resets the resolver budget and the next resolver run sees the note through `previous_verdicts` and the ledger. Do not hand-spawn a resolver sub-agent. On a merge campaign (`kind: merge`, SPEC-0233) the resolver works in `repo.worktree` (the requester's adopted worktree, or the one nightwright created) and the campaign resumes to `CLOSING`: merge campaigns have no `GATING`. Otherwise escalate |
| `parked.diverging_rework` | `parked` `diverging_rework` | the divergence triager (SPEC-0462) says the spec is the problem. `nw queue abandon <id> --by conductor --decision "…" --rule parked.diverging_rework --yes`, then `/nw-rework` to supersede the spec, briefing it with the triager's items as constraints, what was learned and a simpler scope; queue the new spec. Do not resume to implementing: the queue rejects `IMPLEMENTING` / `GATING` / `REVIEWING` (SPEC-0468). A resume without `--to` goes to `SPEC_WRITING`. Record an abandon with `nw queue decision <id> --kind abandon --rule parked.diverging_rework` |
| `parked.spec_status` | `parked` `spec_status` | close failed only on `spec-dod` (SPEC-0468). Do not resume to implementing. Mark the spec `done` or waive the remaining manual AC, then `nw queue resume <id> --to CLOSING --by conductor --decision "…" --rule parked.spec_status --yes`. Any other target is rejected |
| `parked.round_budget` | `parked` `round_budget` | a cursor campaign spent `2 × loop_max` review-driven implement rounds (SPEC-0468). `--reset-loops` does not clear the count. Abandon, or resume to `SPEC_WRITING`. A resume to implementing is rejected |
| `parked.diverging` | `parked` `diverging` | a leftover park from before SPEC-0462 (new trips run `divergence_triager` in `DIVERGENCE_TRIAGE` and do not park here). The fix loop is not converging (SPEC-0247; the `loop_divergence` ledger event names the signals). Dispatch a fresh sub-agent with that evidence, the last `pending_brief`, the spec and the diff; it recommends one of: **continue** (the findings converge despite the signal; `nw queue resume <id> --by conductor --decision "…" --rule parked.diverging --yes` — bounded, a second trip needs fresh rounds), **rework** (the spec is the problem; `nw queue abandon <id> --by conductor --decision "…" --rule parked.diverging --yes`, then `/nw-rework` to supersede the spec, briefing it with the accumulated findings as constraints, what was learned and a simpler scope; queue the new spec), or **escalate** (a scope decision — split or narrow what gets built — or no clear pick; `nw queue escalate <id> --question … --option continue --option rework --option split --recommend N --fact <evidence> --yes`, which is a dry run without `--yes`). Record the decision with `nw queue decision <id> --kind resume|abandon|escalate --rule parked.diverging` |
| `parked.review_escalated` | `parked` `review_escalated` | the conformance reviewer failed to produce a usable verdict twice (SPEC-0187): dispatch a sub-agent to read the campaign's last review runs and the diff and recommend one of: resume to `REVIEWING`, resume to `IMPLEMENTING` with a brief, or escalate; apply its recommendation |
| `parked.main_dirty`, `parked.main_not_on_base` | `parked` `main_dirty` / `main_not_on_base` | someone edited the main checkout in place: dispatch a sub-agent to move the uncommitted work intact onto a new branch in its own worktree (copy changed + untracked files, commit as WIP there, verify the copy, then restore main with `git checkout -- <files>` / remove the moved untracked files), notify the owner (steward) by mail where the work now lives, then resume the campaign to `CLOSING`. Off-base main: switch it back to the base branch only if clean; otherwise move the work first the same way |
| `wt_broken` | live `nw` broken (any `nw` command fails to import) | same as `main_dirty`: move the in-place WIP to a worktree branch, restore main, notify the owner; use `uv run nw` from a worktree meanwhile |
| `waiting` | `waiting` (arrives only in `conductor_digest`) | none: a started campaign resumed with no free slot (SPEC-0200); it takes the next free slot ahead of the backlog by itself |
| `parked.human_decision` | `parked` `human_decision` | waiting for the human's answer (SPEC-0232; once it is answered, see `escalation_answered`; `detail` starts with the `ESC-NNNN` id, `escalated_from` is the original reason). Wait: an `escalation_timeout` arrives when the record is due (SPEC-0425). If the situation resolves without the human, `nw escalations withdraw <id> --reason … --yes`, then `nw queue resume <campaign> --by conductor --decision "…" --rule parked.human_decision --yes` (a withdraw does not resume) |
| `escalation_timeout` | `escalation_timeout` | an escalation with a recommendation and a `recovery` / `judgment` topic went 30 min unanswered (SPEC-0425; `data`: `escalation_id`, `campaign`, `recommendation`, `label`, `topic`). Run `nw escalations answer <id> --auto --by conductor` (dry run), then the same with `--yes`: it chooses the recommended option, marks it `auto`, and mails and notifies the human how to reverse it. A refusal names why (not due, no longer eligible, already answered): record `nw queue decision <cid> --kind other --rule escalation_timeout` and move on. Then handle the `escalation_answered` that follows as usual |
| `escalation_answered` | `escalation_answered` | the human answered an escalation (SPEC-0235; `data`: `escalation_id`, `kind`, `summary`, `answer`). **`data.reverses_auto: true`** (SPEC-0425): the human or a steward replaced your auto-applied answer (`data.previous`); the campaign has moved on, so first undo what the auto answer did where you can (re-park with `nw queue park`, abandon, lower a raised cap back, or revert a merge through `nw queue merge --fixes-red` / a revert campaign), then apply the new answer below and record `--kind apply_answer`. The answer command never moves the campaign: it is still PARKED `human_decision`, and you apply the answer. First read `nw escalations show <id>` and `nw campaign log <campaign> --tail 10`, then act on `kind`. `choose`: do what the chosen option says (resume with `--to` / `--note`, abandon, `nw queue prioritize`, raise the cap…). `override` (waived findings / attested ACs, stored on the campaign as `human_overrides`): `nw queue resume <cid> --to REVIEWING --note "human override <id>" --by conductor --decision "…" --rule escalation_answered --yes`; the fresh review is told about the overrides and triages with them. `text`: act on it per this table, or, if it is not actionable, escalate again: `nw queue escalate` is allowed on a `human_decision` park whose escalation is answered (it keeps `escalated_from` / `resume_to`; the new record is a fresh `ESC-NNNN`). Then `nw queue decision <cid> --kind apply_answer --rule escalation_answered "applied <id>: …"` and give the human one line |
| `escalation`, `escalation_withdrawn` | `escalation` / `escalation_withdrawn` (arrive only in `conductor_digest`) | informational: an escalation you or a steward opened or closed (`data.escalation_id`); act only if it contradicts what you decided |
| `parked.spec_writer_unavailable` | `parked` `spec_writer_unavailable` | legacy (SPEC-0217): produced only between the SPEC-0209 and SPEC-0215 merges, when the spec phase had no writer yet. Once SPEC-0215 is on main, `nw queue resume <id> --by conductor --decision "…" --rule parked.spec_writer_unavailable --yes` |
| `parked.spec_review_unavailable` | `parked` `spec_review_unavailable` | legacy (SPEC-0217): produced only between the SPEC-0215 and SPEC-0214 merges, when the spec phase had no reviewer yet. Once SPEC-0214 is on main, `nw queue resume <id> --by conductor --decision "…" --rule parked.spec_review_unavailable --yes` |
| `parked.spec_write_failed` | `parked` `spec_write_failed` | the spec writer's output was rejected or blocked: read the park detail (the problems, or `blocked: …`). Resume with a `--note` that answers them. If a steward is armed for the project (`nw mailbox list --to steward:<P>`), ask it first with `nw mailbox send --to steward:<P> --kind question --campaign <id> "…"`; otherwise escalate |
| `parked.spec_review_exhausted` | `parked` `spec_review_exhausted` | the spec reviewer failed repeatedly, or the spec review cannot be applied: read the reviews (`nw spec review --path …` or the review store) and the park detail. Resume with a `--note`, or with `--reset-loops`; if it recurs, escalate. Rework (`/nw-rework`) is the likely outcome when the reviews keep finding the same problem (IDEA-089) |
| `spec_written`, `spec_reviewed`, `spec_accepted` | `spec_written` / `spec_reviewed` / `spec_accepted` (arrive only in `conductor_digest`) | none: informational events of the idea-campaign spec phase (SPEC-0217; `spec_reviewed` carries `verdict` and `round`) |
| `parked.open_failed`, `parked.gh_failed` | `parked` `open_failed` / `gh_failed` | read the detail; retry once with `nw queue resume <id> --by conductor --decision "…" --rule parked.open_failed --yes`; if it fails again, dispatch a sub-agent to diagnose, then act or escalate |
| `parked.gates_failed`, `parked.worktree_dirty` | `parked` `gates_failed` / `worktree_dirty` on a merge campaign (`kind: merge`) | the requester's branch is not mergeable as it stands: mail the requester (`merge.requested_by`, e.g. `steward:<P>`) the park detail with `nw mailbox send --to <requester> --kind question --campaign <id> "…"` and wait; they fix the branch, then you `nw queue resume <id> --by conductor --decision "…" --rule parked.gates_failed --yes`. Never fix the branch yourself |
| `parked.not_docs_only` | `parked` `not_docs_only` | a `--docs-only` merge changed a path outside `merge.docs_only_paths`, so no gate has checked it. Resuming would only park again: `nw queue abandon <id> --by conductor --decision "…" --rule parked.not_docs_only --yes` (an adopted worktree and the branch are kept), then `nw queue merge <branch> --by <requester>` without `--docs-only` (dry run, then `--yes`), and tell the requester |
| `message.answer` | `message` (`kind` answer) | apply it: `nw queue decision --kind apply_answer` (it also notes the campaign), then resume / abandon / reprioritize as the answer says |
| `message.request` | `message` (`kind` request) | a steward asks for a queue action (add, merge, prioritize, park, resume): do it via dry-run then `--yes`, and reply with `nw mailbox send --to <from> --kind answer --re <msg_id> "done: …"`. "Queue X after Y" is `nw queue add X --after Y`. A merge request runs `nw queue merge <branch> --by steward:<Project> [--docs-only]` (dry run, then `--yes`); never merge a branch any other way (not by hand in a shell, not in a background job, not through a sub-agent). `--docs-only` is refused unless every changed path is in the project's `merge.docs_only_paths`. A steward's "merge <branch> fixes-red" runs `nw queue merge <branch> --by conductor --fixes-red` (dry run, then `--yes`) with **no human OK** (SPEC-0420): it is the queue's normal rebase, full-suite, ff-only path, exempt from the red-base hold, and refused when the project is not red. A branch your own sub-agent fixed in a worktree may be merged the same way once the episode's automatic fixes are exhausted. Pushes to the base and `gh pr merge` stay human-only |
| `merged`, `handed_off` | `merged` / `handed_off` (arrives only in `conductor_digest`) | the reconcile and the one-line report are the `conductor_digest` row's. Optional deeper verification of a merge goes to a sub-agent |
| `red_main` | `red_main` (arrives only in `conductor_digest`) | informational (SPEC-0420): the full-suite sentinel found the base red and the queue owns the episode. It opens a priority-1 `fixforward` campaign within one tick, reverts the localized culprit merge after 2 failed attempts, holds every non-fix close of the project, and files at most one escalation per episode. Check that `nw queue sentinel status --project <P>` shows a fix in flight; do nothing else. Never ask the human for an OK to fix a red base |
| `red_main_fix_opened`, `red_main_cleared` | `red_main_fix_opened` / `red_main_cleared` (arrive only in `conductor_digest`) | none: the queue opened a red-base fix, or a green sentinel closed the episode (its escalation and the legacy "Sentinel red on <P>" ones are withdrawn, moot fixes abandoned, held closes merge on their next tick). If an `info` mail says "SPEC-X reverted by <cid>; re-queue it", re-queue that spec (`nw queue add SPEC-X`, dry run, then `--yes`) |
| `close_held` | `close_held` (arrives only in `conductor_digest`) | none: a non-fix close waits in CLOSING while its project's base is red (no park, no close attempt counted); it merges by itself once the sentinel is green |
| `parked.fixforward_weakens`, `parked.revert_conflict` | `parked` `fixforward_weakens` / `revert_conflict` (held: `red_main` in the park) | decided by the queue (SPEC-0420): the fix-forward deleted, skipped or weakened a test, or the revert conflicted; the episode counts it failed and abandons it. Nothing to do unless an escalation follows |
| `escalation.red_main` | the human's answer to a `kind: red_main` escalation (it arrives as `escalation_answered`) | apply the human's choice. Choice 1 (another fix-forward attempt): `nw queue sentinel resume-fix --project <P> --yes`. Choice 2 (hand-resolved revert): dispatch a sub-agent to make the revert on a branch in its own worktree, then land it with `nw queue merge <branch> --by conductor --fixes-red` (dry run, then `--yes`). Choice 3: `nw queue sentinel clear --project <P>`. Record it with `nw queue decision --kind apply_answer --rule escalation.red_main` |
| `code_changed` | `code_changed` | nightwright's code or skills changed under the follower (it printed this line and exited 0): **re-arm the Monitor now** with `nw queue events --follow --for conductor --ack <last handled event id>` (omit `--ack` only when you have not handled an event yet). If `skills_changed` has `nw-conduct`, re-invoke `/nw-conduct` (re-read it) before re-arming; if it has `nw-orient`, re-read `/nw-orient` first. If the paths touch `src/nightwright/agentic/`, run the **runner re-exec check**: `nw queue runner-log --tail 3` within one poll interval; if the runner has not re-exec'd (drain waiting on an in-flight persona run), restart it right away: `nw queue stop --yes` then `nw queue run --detach` (`stop` refuses inside the close guard: then wait for the next event; see `runner_journal_silent` for the guard). Do not wait for the drain (an old runner; SPEC-0229 makes the drain admit and exec promptly). The `conductor_digest` row repeats this check when a merged campaign changed `.nw/project.yaml`. No report to the human |
| `backlog.unknown_spec` | `backlog` with `blocked_by` an unknown spec | the spec is wrong: escalate |
| `admitted`, `resumed`, `cost_warning`, `cap_raised`, `abandoned`, `rebase_resolved` | `admitted`, `resumed`, `cost_warning`, `cap_raised` (SPEC-0422: the queue raised a converging campaign's cap), `abandoned`, `rebase_resolved` (arrive only in `conductor_digest`) | informational; act only if it contradicts what you decided |

Exact flags: `nw queue --help`, `nw campaign --help`, `nw mailbox --help`. Every
`nw queue decision` call takes a rationale argument after the options
(`nw queue decision <cid> --kind wait --rule backlog_blocked_long "waiting on the dependency"`).
Flags alone exit 2. `nw queue conductor-cost --since` takes `YYYY-MM-DD`, not the word `yesterday`.

## Compaction and handoff

Your context is not the record; the files are. `nw queue conductor-brief` shows
`conductor.context_tokens`, `compact_at` (`agentic.conductor.compact_at_tokens`, default
150000) and `compact_now`.

- When `compact_now` is true, or your session's context indicator passes the threshold, finish
  the event in hand. Record any in-flight dispatch with `nw queue decision --kind dispatch`,
  then compact.
- After compacting, or on any fresh start, run `nw queue conductor-brief` and re-arm the
  Monitor with `--ack` of the last event id you have handled. Never re-read event history or
  old conversation to orient.
- Check the brief once per `conductor_ping` digest and after each escalation.

## Conventions / guardrails

- **Never ask a steward for mechanical work.** Spec numbers come from `nw spec reserve` (or `nw spec new`, which reserves); merges, bookkeeping and restarts are yours or a tool's. Mail a steward only for project judgment.

- **A re-exec drain does not wait on detachable persona runs.** Since SPEC-0229, a drain blocks on a gate, a close, or a run in `spawning` / `collect`. Detachable persona children (`claude`, `cursor-agent`, or the Pi child) do not block it. If campaigns are frozen behind a drain that is only waiting on those, `nw queue stop --yes` then `nw queue run --detach`. If a gate or close child (`uv` / `python`) is running, wait for it.

- **One spec review round, never more.** A spec gets exactly one `nw spec review`. `approve` ⇒ queue it. An unchanged internal `revise` ⇒ queue the spec as is: `nw queue add` amends it headlessly (a `spec_writer` run applies the findings) and goes straight to implementing with no second review. An unchanged outbound `revise` is refused until the spec is edited, then queued with no second review. A steward may help when asked but is never required. `split` ⇒ `nw queue add` the spec as is for an internal spec. A split campaign writes the parts (SPEC-0290) and queues them on merge (SPEC-0294). A `split_queued` event with refusals, or a split refused at add (outbound, stale review, wrong part count), goes to the project's steward only when one is armed (`nw mailbox list --to steward:<P>`); otherwise escalate it to the human. Never run review → revise → re-review loops. `--force` on `nw spec review` starts another review. It does not admit the spec.

- **One conductor.** The `conductor` reader lock on the event follower enforces it.
- **Never block.** Quick reads and writes only in your own context; everything else is a
  sub-agent or a background shell (see *Never block*).
- **Never implement in your own context.** Conflict resolution and fixes are done by
  sub-agents you dispatch, inside the campaign worktree, never in the main checkout; branch
  merges, including your own spec edits, go through `nw queue merge --by conductor`; feature
  implementation stays with the loop's personas.
- **Every park gets a decision** (resume, abandon, steward question, or escalation) and a
  `nw queue decision <cid> --kind … --rule … --event <id> "why"` saying which and why.
  `nw campaign note` stays for remarks that are not decisions. **Decide before you resume**
  (SPEC-0318): always pass `--by conductor` (or have `NW_MAILBOX_ADDR=conductor` set) to
  `nw queue park|resume|abandon`. A `nw queue park` needs `--reason "why"` plus
  `--decision "why" --rule <row id>` (the record is written right after the park).
  `--decision` and `--rule` alone are not enough: the command exits without `--reason`.
  A `nw queue resume|abandon` of a PARKED
  campaign is refused unless a decision record at or after the park exists, so record it first
  with `nw queue decision`, or pass `--decision "why" --rule <row id>` to the command (it
  writes the record, then acts). The refusal names the fix. Use the id of the row that led to
  the hold (`message.request` for a steward's park request, `wt_broken`, `parked.main_dirty`).
  After `nw queue escalate` record your `escalate` decision *after* the command (so the
  record's ts follows the park). The human's own commands need no record.
- **Clear blockers yourself.** Anything stopping the queue (a broken or dirty main checkout, a
  dead runner, a stuck campaign, a conflict) is yours to resolve through sub-agents; escalate
  to the human only for decisions about what gets built. Never discard anyone's work: move it,
  don't delete it. Judgment calls that sub-agents or specs report (defaults, thresholds,
  profile contents, option names, small scope additions) are yours to settle: decide, record
  the reasoning with `nw queue decision --kind other`, and tune later from evidence; never forward them to
  the human as things to adjust.
- **Human-decision route** (decisions about what gets built only: scope, priorities between
  features, product direction, DoD waivers, or anything touching the human's own checkout; the
  conductor never asks the human directly): campaign, the
  facts, what you recommend, what you need decided. First hop: if a steward is armed for the
  campaign's project, mail it (`--to steward:<P> --kind question`); it asks the human and answers
  with `nw escalations answer`. No steward, or silent past the timeout: `nw queue escalate`. Routine recovery, blocker clearing and
  judgment calls never go to the human. Every "escalate" in the table above means this:
  `nw queue escalate <campaign> --question "…" --option "…" --option "…" --recommend N
  --topic <topic> --fact "…" --yes` (no campaign for a runner or global matter). Every
  `--recommend` needs `--topic` (SPEC-0425). `recovery` and `judgment` time out: 30 min
  unanswered, you auto-apply the recommendation (`escalation_timeout`). The human-only topics
  never time out: `scope`, `product`, `spend_ceiling`, `spend_diverging`, `manual_ac`,
  `push_main`, `pr_merge`, `human_checkout`, `human_settings` (and every `kind: cost` record).
  Scope, split and product questions use `scope` / `product`. It writes the `ESC-NNNN`
  record, parks the campaign as `human_decision`, mails `human` and sends a desktop
  notification (SPEC-0232). Then give exactly one chat line:
  `escalated ESC-NNNN: <question>`, and go to the next event. Never `AskUserQuestion`, never a question in chat. One open escalation per campaign.
- **Mail identity:** pass `--from conductor` (mailbox) or `--by conductor` (queue writes that
  take it). Never prefix a command with `NW_MAILBOX_ADDR=…`: Claude Code does not strip that
  assignment when it matches allow rules, so a prefixed `nw` command falls through to the
  auto-mode classifier and can be denied. Start every command with `nw`, with no `cd … &&`
  in front (SPEC-0320).
- **Shared ideas file:** verify headings before any `nw idea …` write.
- **SPEC-0138:** the spec in the queue is the governing design.

## Verify

`nw queue conductor-brief` shows the runner held, `conductor.pid` = your follower, no
parked item with `decided: false`, and no unanswered conductor mail.
