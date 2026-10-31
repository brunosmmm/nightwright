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
limits, the model and the session registration; `--resume` restarts the same session.

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
   armed: stop and tell the human.
2. **Ensure the loop runs:** if `lease.held` is false and work is active or queued, start it:
   `nw queue run --detach` (it confirms the lease or prints why not; `.lease.log` names its
   output file), then re-check `nw queue conduct-status`. To restart it: `nw queue stop --yes`
   then `nw queue run --detach` (`stop` refuses inside the close guard: then wait for the next
   event).
3. **Resolve what is already waiting** (parked items, unanswered mail) with the table below.
4. **Arm the Monitor** on `nw queue events --follow --for conductor` with the longest timeout
   and a description like "nw queue events (conductor)". Each stdout line is one
   `nw.event.v1` JSON event and wakes you. The follower holds the `conductor` reader lock, so
   a second conductor cannot arm. When the Monitor expires, **re-arm it** (the cursor resumes
   after the last delivered event; nothing is lost or repeated). The follower also exits
   after one `code_changed` line when nightwright's code changes; that is not an error (see the table).

   **Quiet stream (SPEC-0300).** The follower holds events that never need a decision and folds
   them into one `conductor_digest` line (every 30 min at most; `nw queue events --follow
   --all` turns this off); a digest holding only pings without `ping_items` is not delivered, and
   `nw queue wakeups` counts what it delivered. An event type it does
   not know always wakes you. The held types are mirrored in `quiet.RULES`:

   - **Always held:** `admitted`, `resumed`, `waiting`, `abandoned`, `cost_warning`, `escalation`, `escalation_withdrawn`, `rebase_resolved`, `run_failed`, `merged`, `handed_off`, `spec_written`, `spec_reviewed`, `spec_accepted`, `gate_waiting`
   - **Held while a fact says so:** `backlog` (only an ordinary dependency wait), `backlog_blocked_long` (only an ordinary dependency wait whose dependencies each have a live campaign), `conductor_ping` (nothing new to act on), `runner_draining` (within its wait window), `campaign_silent`, `state_stuck` (its gate or close is still running in the runner, or it waits in the close train behind a head that is), `parked` (a park the queue decided itself, with auto set in its data: SPEC-0302, or a park you made yourself, with by set to conductor: SPEC-0315; every other parked wakes you)

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
| `lease_stale` | `lease_stale` | the runner is hung (no run in flight within its timeout): report pid + heartbeat age to the human; do not kill it unless told |
| `run_overdue` | `run_overdue` | a persona run exceeded its timeout + grace: dispatch a sub-agent to check the run's process and logs and recommend park+resume, wait, or escalate; apply its recommendation |
| `campaign_silent` | `campaign_silent` | no ledger activity for long while active: same as `run_overdue` (sub-agent investigates) |
| `conductor_digest` | `conductor_digest` | the held events since the last digest (synthetic, SPEC-0300; `data`: `held` counts by type, `campaigns`, `merged`, `pings_held`, `ping_items`). If `merged` is non-empty: `nw spec reconcile --apply` and give the human one line per merge (spec, outcome, cost). If any merged campaign changed its project config (`.nw/project.yaml`), run the runner re-exec check of the `code_changed` row. Scan `held`: a `cost_warning` or `run_failed` count needs no action unless a park follows. If `ping_items` is non-empty (`[category, campaign]` pairs the held pings kept listing), act on those items per the table. A digest holding only pings without `ping_items` is not delivered. Do nothing else |
| `conductor_ping` | `conductor_ping` | periodic digest (SPEC-0212, every 10 min; arrives only when its actionable items changed, see *Quiet stream*): read `data`; for each `blocked`, `stuck`, `ready_not_admitted` or `waiting_not_admitted` item, a non-null `runner` (the `runner_journal_silent` condition: act per that row) and each conductor-owned `parked` item with `decided: false`, act per the table or dispatch a sub-agent; nothing needing action means do nothing, with no report to the human |
| `park_undecided` | `park_undecided` | you left a park without a decision note for 5 min (SPEC-0222): act per the park reason's row now, and record the decision with `nw queue decision`; it fires once per park |
| `backlog_blocked_long` | `backlog_blocked_long` | a queued item is blocked on its dependencies: read `blocked_by`; if the dependency is itself queued, parked or being merged, make sure it progresses (act on it); an unknown or wrong spec → escalate; else record `nw queue decision <cid> --kind wait --rule backlog_blocked_long` and wait; it arrives only when a dependency has no live campaign or the blocker is not an ordinary dependency wait; in a `conductor_digest` that lists it, check that each `waiting on SPEC-X` has a campaign; if none, queue it or escalate |
| `state_stuck` | `state_stuck` | an active campaign made no progress for too long (`stuck_s` is idle time since the last `run.end` in persona states, or time since the latest close/gate attempt in GATING/CLOSING; `in_state_s` is the whole stay): same as `campaign_silent` (sub-agent checks the run, gate and process, recommends park+resume, wait or escalate) |
| `ready_not_admitted` | `ready_not_admitted` (or a `conductor_ping` showing `waiting` items while `counts.active` < slots) | a ready or WAITING item is not starting with a free slot. Since SPEC-0268 the alarm means a slot has been free for over `ready_grace_s` (`data.slot_free_s`; a brief free slot that the queue refilled does not fire it). **Act within one event, never wait it out.** Check `nw queue runner-log --tail 5` first: an `admission_blocked` line means the slots are full and lists the holders (`deferred` = a draining tick skipped that close or gate), so nothing is stuck; otherwise: if the last journal line is older than 2× the poll interval and the runner is stuck on a long step (a code-change drain keeps admitting since SPEC-0229, so a drain alone is not a reason), restart the runner immediately: `nw queue stop --yes` then `nw queue run --detach` (`stop` refuses inside the close guard: then wait for the next event); an interrupted persona run is cheap compared with a stalled queue, and its worktree edits survive. If the runner is idle or dead, restart it (`nw queue run --detach` alone when dead). If admission still fails right after a restart, dispatch a sub-agent to diagnose. The item's `drain` (SPEC-0228: `{reason, since, draining_s, waiting_on}`, else null) set means the queue is stalled behind a drain, not busy; `runner_busy` keeps its meaning. `runner_draining: true` on the item: admission continues while the runner drains for a re-exec (SPEC-0229), so handle it as an ordinary `ready_not_admitted`; a `runner_draining` event ⇒ see that row. The close guard is defined in the `runner_journal_silent` row. Record the action with `nw queue decision <cid> --kind restart_runner --rule ready_not_admitted`. |
| `waiting_not_admitted` | `waiting_not_admitted` | a WAITING campaign was not resumed although a slot has been free for over `waiting_grace_s` (SPEC-0228; `data`: `waiting_for_s`, `slot_free_s`, `resume_to`, `active`, `slots`, `runner_pid`, `drain`). Same action as `ready_not_admitted`, **within one event**: if `drain` is set or the journal is silent, restart the runner: `nw queue stop --yes` then `nw queue run --detach` (`stop` refuses inside the close guard: then wait for the next event); if it is dead, start it (`nw queue run --detach`). The close guard is defined in the `runner_journal_silent` row. Record the action with `nw queue decision <cid> --kind restart_runner --rule waiting_not_admitted`. Escalate only if admission still fails after a restart |
| `runner_journal_silent` | `runner_journal_silent` | the runner's loop has not ticked for 3 poll intervals although its lease is held and its heartbeat is fresh, and it is not inside a declared `open` / `close` / `drain` step within budget (SPEC-0228; `data`: `silent_s`, `interval_s`, `last_tick_at`, `last_progress_at`, `last_tick_s`, `last_journal_ts`, `draining`, `busy`; the loop counts as alive while it marks progress inside a long tick, SPEC-0313): the loop is stalled while alive. Restart the runner immediately: `nw queue stop --yes` then `nw queue run --detach` (`stop` refuses inside the close guard: then wait for the next event): an interrupted persona run is cheaper than a stalled queue, its cost is marked estimated (SPEC-0224) and its worktree edits survive. **Close guard** (`nw queue stop` computes it and refuses inside it, exit 1; `--wait-guard` waits it out): no SIGTERM while `nw queue list --json` (`.lease`) shows `busy.kind` `open` or `close` within `since` + `budget_s`, or with an unparseable `since` (a killed close can leave the main checkout mid-merge, a killed open leaves a half-built worktree in OPENING): wait for the next event. An open or close past its budget arrives as `runner_journal_silent` with `busy` set: dispatch a sub-agent to check the worktree or main checkout and the process before any restart. A `drain` busy is not guarded. If it recurs within minutes of a restart, read `nw queue runner-log --tail 30` and dispatch a sub-agent to diagnose |
| `temp_pressure` | `temp_pressure` | the temp filesystem (`data.path`: `$TMPDIR` (default `/tmp`) or the agentic dir) is short of free space or inodes (SPEC-0253; `data`: `free_mb`, `free_inodes`, `free_inodes_pct`, `thresholds`). The runner **pauses admission** (QUEUED items stay QUEUED, in-flight campaigns carry on); nothing has failed. Find the consumer (`df -i <path>`, `du -sh` / `find <path> -xdev | wc -l` under it), remove stale `pytest-of-*` dirs and `tmp/` dirs of finished campaigns (`<data_dir>/agentic/campaigns/<id>/tmp`); **never** the `tmp/` of an active campaign. Confirm the event clears (admission resumes on the next tick). No report to the human unless the space cannot be freed |
| `runner_draining` | `runner_draining` | the runner has been draining (`data.reason`) for over 60 s, waiting on `data.waiting_on` (each `{campaign, kind, run_ids, timeout_s}`) with `data.ready` backlog items ready (SPEC-0224). Since SPEC-0229 the list holds only what blocks the exec (a gate, close or FakeRunner step, or a run in `spawning` / `collect`); detachable persona runs do not block it and are not listed. It is waiting on a gate, close or FakeRunner step, so wait. Past the largest `waiting_on[].timeout_s` + 5 min, treat it as `lease_stale`. No report to the human |
| `runner_config_invalid` | `runner_config_invalid` | a config file the runner watches is broken (SPEC-0226; `data.scope`, `path`, `error`). `scope: user` → the human's config file is broken: the runner keeps the last good config, so nothing is stopped; tell the human the path and the error in one line and do not edit their config. `scope: project:<P>` → as `parked` `config_invalid`: if a steward is armed (`nw mailbox list --to steward:<P>`), mail it the error with `nw mailbox send --to steward:<P> --kind question`, otherwise tell the human. The condition clears when the file is fixed |
| `run_failed`, `run_failed.transient` | `run_failed` (arrives only in `conductor_digest`) | not delivered on its own: look it up when a park needs it, with `nw queue events --campaign <id> --type run_failed`. `transient: true` is an account limit / overload: take the reset time from `error` if present and, once it parks, resume after the reset with `--note "transient API failure"`; this is not the work failing. Not transient: the queue's triage handles it; act only if it parks |
| `parked.cost_cap` | `parked` `cost_cap` | the queue already resumes a phantom-charge cap park itself (SPEC-0302: runs that failed transiently without reporting cost were charged their budget as an estimate; it raises the cap by that charge and retries after the reset, logged `by: queue`), so a `cost_cap` park that reaches you is real spend: resume once with `--raise-cap` up to 2× the profile cap and a note; a second cost park → escalate |
| `parked.base_moving` | `parked` `base_moving` | reaches you only after the queue's automatic resumes are spent (it retries 3 times with a growing backoff, logged `by: queue`): the base keeps moving under the close; check what is merging into main (a self-inflicted merge loop, a human lane), then resume to `CLOSING` or escalate |
| `parked.config_invalid` | `parked` `config_invalid` | the queue resumes it itself once the project config loads again (SPEC-0194, logged `by: queue`); if it stays parked, read the detail and tell the human which config is broken |
| `auto_resume_failed` | `auto_resume_failed` | the queue gave up an automatic resume (`data.rule`, `data.error`: a failed resume, an open escalation, or the feature switched off): the park is yours again, read the error, then decide it per its reason's row |
| `parked.loop_exhausted`, `parked.triage_escalated`, `parked.implementer_blocked` | `parked` `loop_exhausted` / `triage_escalated` / `implementer_blocked` | first read the campaign's recent failures with `nw queue events --campaign <id> --type run_failed` (the `transient` flag; the reset time is in `error`), since the failures are not delivered to you. If they were transient, resume with `--reset-loops` after the reset; else read the last triage brief; if a steward is armed for the project (`nw mailbox list --to steward:<P>`), ask it with `nw mailbox send --to steward:<P> --kind question --campaign <id> "…"` and wait for its `message`; else escalate with the brief and your recommendation |
| `parked.rebase_conflict` | `parked` `rebase_conflict` | the close already tried: generated-files-only conflicts are regenerated (SPEC-0191) and any other conflict goes to the `conflict_resolver` persona, which may fail `MAX_RESOLVE_ATTEMPTS` (2) times (SPEC-0221; successful runs do not count against that, and a base that keeps conflicting after `MAX_RESOLVE_RUNS` (4) resolutions parks `base_moving` instead, SPEC-0303); so this park means the resolver failed twice (the detail says `last: …`), or the project opted out with `merge.resolver: false`. Read the park detail and the last `conflict resolver did not resolve` finding. If its questions are answerable from project knowledge (ask the steward), `nw campaign note` the answer and resume to `CLOSING`: the resume resets the resolver budget and the next resolver run sees the note through `previous_verdicts` and the ledger. Do not hand-spawn a resolver sub-agent. On a merge campaign (`kind: merge`, SPEC-0233) the resolver works in `repo.worktree` (the requester's adopted worktree, or the one nightwright created) and the campaign resumes to `CLOSING`: merge campaigns have no `GATING`. Otherwise escalate |
| `parked.diverging` | `parked` `diverging` | the fix loop is not converging (SPEC-0247; the `loop_divergence` ledger event names the signals). Dispatch a fresh sub-agent with that evidence, the last `pending_brief`, the spec and the diff; it recommends one of: **continue** (the findings converge despite the signal; `nw queue resume <id> --by conductor --decision "…" --rule parked.diverging --yes` — bounded, a second trip needs fresh rounds), **rework** (the spec is the problem; `nw queue abandon <id> --by conductor --decision "…" --rule parked.diverging --yes`, then `/nw-rework` to supersede the spec, briefing it with the accumulated findings as constraints, what was learned and a simpler scope; queue the new spec), or **escalate** (a scope decision — split or narrow what gets built — or no clear pick; `nw queue escalate <id> --question … --option continue --option rework --option split --recommend N --fact <evidence> --yes`, which is a dry run without `--yes`). Record the decision with `nw queue decision <id> --kind resume|abandon|escalate --rule parked.diverging` |
| `parked.review_escalated` | `parked` `review_escalated` | the conformance reviewer failed to produce a usable verdict twice (SPEC-0187): dispatch a sub-agent to read the campaign's last review runs and the diff and recommend one of: resume to `REVIEWING`, resume to `IMPLEMENTING` with a brief, or escalate; apply its recommendation |
| `parked.main_dirty`, `parked.main_not_on_base` | `parked` `main_dirty` / `main_not_on_base` | someone edited the main checkout in place: dispatch a sub-agent to move the uncommitted work intact onto a new branch in its own worktree (copy changed + untracked files, commit as WIP there, verify the copy, then restore main with `git checkout -- <files>` / remove the moved untracked files), notify the owner (steward) by mail where the work now lives, then resume the campaign to `CLOSING`. Off-base main: switch it back to the base branch only if clean; otherwise move the work first the same way |
| `wt_broken` | live `nw` broken (any `nw` command fails to import) | same as `main_dirty`: move the in-place WIP to a worktree branch, restore main, notify the owner; use `uv run nw` from a worktree meanwhile |
| `waiting` | `waiting` (arrives only in `conductor_digest`) | none: a started campaign resumed with no free slot (SPEC-0200); it takes the next free slot ahead of the backlog by itself |
| `parked.human_decision` | `parked` `human_decision` | waiting for the human's answer (SPEC-0232; once it is answered, see `escalation_answered`; `detail` starts with the `ESC-NNNN` id, `escalated_from` is the original reason). Do nothing. If the situation resolves without the human, `nw escalations withdraw <id> --reason … --yes`, then `nw queue resume <campaign> --by conductor --decision "…" --rule parked.human_decision --yes` (a withdraw does not resume) |
| `escalation_answered` | `escalation_answered` | the human answered an escalation (SPEC-0235; `data`: `escalation_id`, `kind`, `summary`, `answer`). The answer command never moves the campaign: it is still PARKED `human_decision`, and you apply the answer. First read `nw escalations show <id>` and `nw campaign log <campaign> --tail 10`, then act on `kind`. `choose`: do what the chosen option says (resume with `--to` / `--note`, abandon, `nw queue prioritize`, raise the cap…). `override` (waived findings / attested ACs, stored on the campaign as `human_overrides`): `nw queue resume <cid> --to REVIEWING --note "human override <id>" --by conductor --decision "…" --rule escalation_answered --yes`; the fresh review is told about the overrides and triages with them. `text`: act on it per this table, or, if it is not actionable, escalate again: `nw queue escalate` is allowed on a `human_decision` park whose escalation is answered (it keeps `escalated_from` / `resume_to`; the new record is a fresh `ESC-NNNN`). Then `nw queue decision <cid> --kind apply_answer --rule escalation_answered "applied <id>: …"` and give the human one line |
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
| `message.request` | `message` (`kind` request) | a steward asks for a queue action (add, merge, prioritize, park, resume): do it via dry-run then `--yes`, and reply with `nw mailbox send --to <from> --kind answer --re <msg_id> "done: …"`. A merge request runs `nw queue merge <branch> --by steward:<Project> [--docs-only]` (dry run, then `--yes`); never merge a branch any other way (not by hand in a shell, not in a background job, not through a sub-agent). `--docs-only` is refused unless every changed path is in the project's `merge.docs_only_paths` |
| `merged`, `handed_off` | `merged` / `handed_off` (arrives only in `conductor_digest`) | the reconcile and the one-line report are the `conductor_digest` row's. Optional deeper verification of a merge goes to a sub-agent |
| `code_changed` | `code_changed` | nightwright's code or skills changed under the follower (it printed this line and exited 0): **re-arm the Monitor now**, with the same command. If `skills_changed` has `nw-conduct`, re-invoke `/nw-conduct` (re-read it) before handling the next event; if it has `nw-orient`, re-read `/nw-orient` first. If the paths touch `src/nightwright/agentic/`, run the **runner re-exec check**: `nw queue runner-log --tail 3` within one poll interval; if the runner has not re-exec'd (drain waiting on an in-flight persona run), restart it right away: `nw queue stop --yes` then `nw queue run --detach` (`stop` refuses inside the close guard: then wait for the next event; see `runner_journal_silent` for the guard). Do not wait for the drain (an old runner; SPEC-0229 makes the drain admit and exec promptly). The `conductor_digest` row repeats this check when a merged campaign changed `.nw/project.yaml`. No report to the human |
| `backlog.unknown_spec` | `backlog` with `blocked_by` an unknown spec | the spec is wrong: escalate |
| `admitted`, `resumed`, `cost_warning`, `abandoned`, `rebase_resolved` | `admitted`, `resumed`, `cost_warning`, `abandoned`, `rebase_resolved` (arrive only in `conductor_digest`) | informational; act only if it contradicts what you decided |

Exact flags: `nw queue --help`, `nw campaign --help`, `nw mailbox --help`.

## Compaction and handoff

Your context is not the record; the files are. `nw queue conductor-brief` shows
`conductor.context_tokens`, `compact_at` (`agentic.conductor.compact_at_tokens`, default
150000) and `compact_now`.

- When `compact_now` is true, or your session's context indicator passes the threshold, finish
  the event in hand. Record any in-flight dispatch with `nw queue decision --kind dispatch`,
  then compact.
- After compacting, or on any fresh start, run `nw queue conductor-brief` and re-arm the
  Monitor. Never re-read event history or old conversation to orient.
- Check the brief once per `conductor_ping` digest and after each escalation.

## Conventions / guardrails

- **Never ask a steward for mechanical work.** Spec numbers come from `nw spec reserve` (or `nw spec new`, which reserves); merges, bookkeeping and restarts are yours or a tool's. Mail a steward only for project judgment.

- **A re-exec drain stalls admitted campaigns too.** While the runner drains (lease `draining`), nothing new is submitted: campaigns sitting in GATING / REVIEWING / CLOSING with no child process of theirs (`ps --ppid <runner pid> -o comm=` shows only `claude`) are frozen, not just the backlog. If the drain waits only on long `implement` runs and other campaigns are frozen behind it, `nw queue stop --yes` then `nw queue run --detach`; if a gate or close child (`uv`/`python`) is running, wait for it. (Until SPEC-0225/0229 land.)

- **One spec review round, never more.** A spec gets exactly one `nw spec review`. `approve` ⇒ queue it. `revise` ⇒ queue the spec as is: `nw queue add` amends it headlessly (a `spec_writer` run applies the findings) and goes straight to implementing with no second review; the implementer's brief carries the findings. A steward may help when asked but is never required. `split` ⇒ split, and each part gets its own single review. A `spec_write_failed` park from an amend, or a `split` refusal, goes to the project's steward only when one is armed (`nw mailbox list --to steward:<P>`); otherwise escalate it to the human. Never run review → revise → re-review loops.

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
  `nw queue park|resume|abandon`. A `nw queue park` needs `--decision "why" --rule <row id>`
  (the record is written right after the park); a `nw queue resume|abandon` of a PARKED
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
- **Escalate crisply** (decisions about what gets built only: scope, priorities between
  features, product direction, or anything touching the human's own checkout): campaign, the
  facts, what you recommend, what you need decided. Routine recovery, blocker clearing and
  judgment calls never go to the human. Every "escalate" in the table above means this:
  `nw queue escalate <campaign> --question "…" --option "…" --option "…" --recommend N
  --fact "…" --yes` (no campaign for a runner or global matter). It writes the `ESC-NNNN`
  record, parks the campaign as `human_decision`, mails `human` and sends a desktop
  notification (SPEC-0232). Then give the human exactly one chat line:
  `escalated ESC-NNNN: <question>`. One open escalation per campaign.
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
