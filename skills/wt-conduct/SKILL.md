---
name: wt-conduct
description: "Become wt's conductor (SPEC-0182) — requires /wt-orient first: a long-lived session that arms a Monitor on the queue's event stream (`wt queue events --follow --for conductor`) and is woken by every queue event — parks, merges, failed runs, stuck or dead runners, steward mail — and resolves each from a decision table, escalating to the human only when the table says so. Use when the human asks to conduct / run / watch the agentic queue. Never implements anything itself."
trigger: conduct the queue; be the conductor; run and watch the agentic loop; resolve parked campaigns
---

# wt-conduct

## When to use

The human wants the agentic queue run **without being asked about everything**. You become
the conductor: you keep the loop alive, and every event the queue fires wakes you to make the
call. The loop itself (`wt queue run`) and its headless personas do all implementation work;
stewards (`/wt-steward`) answer project questions over the mailbox. You decide, delegate and
record — you never implement.

Not for authoring specs (`/wt-new-work`), implementing (`/wt-implement-spec`) or auditing
(`/wt-verify`).

## Never block (the first rule)

You must stay responsive to the next event at all times. **Your own context only does
quick reads and quick writes** — `wt queue conduct-status`, `wt campaign show|log`,
`wt campaign runs|run` (diagnosing failed runs),
`wt queue resume|park|abandon|prioritize`, `wt campaign note`, `wt mailbox send`,
`wt spec reconcile --apply`: commands that return in seconds.

**Everything else is dispatched, never run inline.** Dispatch work to fresh sub-agents (general-purpose, or Explore for read-only search) with a self-contained brief and a model chosen per task (sonnet for routine work, opus only for hard design or review). Never use forks — they inherit the whole conversation and cost an order of magnitude more; write any needed context into the brief. The brief names the
campaign, the governing spec path, the worktree, the files to read, the files the sub-agent may
change, these rules, and what to report.

- **Sub-agent** (preferred) for any work that needs judgment or more than a few seconds:
  resolving a rebase conflict in a campaign worktree,
  investigating a failure (reading diffs, gate logs, runner logs), checking whether a stuck
  run's process is alive and why, drafting a steward question from a long triage brief,
  verifying a merged spec. Launch it in the background with a brief that names the campaign,
  the worktree, the exact goal, what it may change, and that it must report back with facts
  and a recommended `wt` action. You then apply that action yourself (a quick write).
- **The runner follows merges by itself** (SPEC-0194): between ticks it re-executes when a
  commit changes `src/wt/` or `pyproject.toml` (keeping its lease; `wt queue runner-log`
  shows `stop code_changed` then `start`), and a campaign parked `config_invalid` resumes
  once the config loads. Don't restart it by hand after a merge; only if `runner-log` shows
  no `start` within a minute of a `code_changed` stop.
- **Background shell** for long mechanical commands with no judgment (starting the loop,
  `wt queue gates <id>`, waiting for a reset time).

Bookkeeping: before dispatching, record `wt campaign note <id> "conductor: dispatched
sub-agent to …"`; never dispatch a second worker for the same campaign while one is in
flight; when the report arrives, act and note the outcome. If an event arrives while workers
run, handle it — that is the point.

## Procedure

0. **Load `/wt-orient` first** (invoke the skill) and follow its working rules — required
   before anything else in this skill.
1. **Situation report:** `wt queue conduct-status --json` (`wt.conduct.status.v1`): runner
   lease, active / queued / parked items, unanswered conductor mail, recent events. If
   `conductor_pid` is set and is not yours, another conductor is armed: stop and tell the
   human.
2. **Ensure the loop runs:** if `lease.held` is false and work is active or queued, start it:
   `setsid nohup wt queue run >> ~/.local/share/wt/agentic/runner.log 2>&1 &`, then re-check
   `wt queue conduct-status`.
3. **Resolve what is already waiting** (parked items, unanswered mail) with the table below.
4. **Arm the Monitor** on `wt queue events --follow --for conductor` with the longest timeout
   and a description like "wt queue events (conductor)". Each stdout line is one
   `wt.event.v1` JSON event and wakes you. The follower holds the `conductor` reader lock, so
   a second conductor cannot arm. When the Monitor expires, **re-arm it** (the cursor resumes
   after the last delivered event; nothing is lost or repeated). The follower also exits
   after one `code_changed` line when wt's code changes; that is not an error (see the table).
5. **Per event:** look up its `type` in the table, gather facts (`wt campaign show <id>`,
   `wt campaign log <id> --tail 20`), act with `wt` commands (dry-run first for writes, then
   `--yes`), and record the decision on the campaign: `wt campaign note <id> "conductor: …"`.
   Anything beyond a quick read or write goes to a sub-agent or a background shell (see
   *Never block*). Then wait for the next event.
6. **Reporting:** after a merge or an escalation, give the human one short line (spec, outcome,
   cost). Don't narrate routine events.

## Decision table

| event `type` | action |
|---|---|
| `runner_dead` | start the loop (step 2); if it dies again within minutes, read `wt queue runner-log --tail 30` (a `crash` record carries the error) and escalate |
| `lease_stale` | the runner is hung (no run in flight within its timeout): report pid + heartbeat age to the human; do not kill it unless told |
| `run_overdue` | a persona run exceeded its timeout + grace: dispatch a sub-agent to check the run's process and logs and recommend park+resume, wait, or escalate; apply its recommendation |
| `campaign_silent` | no ledger activity for long while active: same as `run_overdue` (sub-agent investigates) |
| `conductor_ping` | periodic digest (SPEC-0212, every 10 min): read `data`; for each `blocked`, `stuck`, `ready_not_admitted` or `waiting_not_admitted` item, a non-null `runner` (the `runner_journal_silent` condition: act per that row) and each conductor-owned `parked` item with `decided: false`, act per the table or dispatch a sub-agent; nothing needing action means do nothing, with no report to the human |
| `park_undecided` | you left a park without a decision note for 5 min (SPEC-0222): act per the park reason's row now, and record the decision with `wt campaign note`; it fires once per park |
| `backlog_blocked_long` | a queued item is blocked on its dependencies: read `blocked_by`; if the dependency is itself queued, parked or being merged, make sure it progresses (act on it); an unknown or wrong spec → escalate; else note and wait |
| `state_stuck` | an active campaign sat in one state too long with no run in budget: same as `campaign_silent` (sub-agent checks the run, gate and process, recommends park+resume, wait or escalate) |
| `ready_not_admitted` (or a `conductor_ping` showing `waiting` items while `counts.active` < slots) | a ready or WAITING item is not starting with a free slot. **Act within one event, never wait it out.** Check `wt queue runner-log --tail 5`: if the last journal line is older than 2× the poll interval and the runner is draining for a code-change re-exec (or stuck on any long step), SIGTERM the runner and restart it immediately, subject to the close guard below (step 2); an interrupted persona run is cheap compared with a stalled queue, and its worktree edits survive. If the runner is idle or dead, restart it. If admission still fails right after a restart, dispatch a sub-agent to diagnose. The item's `drain` (SPEC-0228: `{reason, since, draining_s, waiting_on}`, else null) set means the queue is stalled behind a drain, not busy; `runner_busy` keeps its meaning. `runner_draining: true` on the item (or a `runner_draining` event) ⇒ see the `runner_draining` row too. **Close guard:** never SIGTERM while `wt queue list --json` (`.lease`) shows `busy.kind` `open` or `close` within `since` + `budget_s` (a killed close can leave the main checkout mid-merge, a killed open leaves a half-built worktree in OPENING): wait for the next event. An open or close past its budget arrives as `runner_journal_silent` with `busy` set: dispatch a sub-agent to check the worktree or main checkout and the process before any restart. A `drain` busy is not guarded. Note the action on the affected campaigns. (Until SPEC-0224/0225 land, every code merge on a busy queue causes this.) |
| `waiting_not_admitted` | a WAITING campaign was not resumed although a slot has been free for over `waiting_grace_s` (SPEC-0228; `data`: `waiting_for_s`, `slot_free_s`, `resume_to`, `active`, `slots`, `runner_pid`, `drain`). Same action as `ready_not_admitted`, **within one event**: if `drain` is set or the journal is silent, SIGTERM and restart the runner (step 2); if it is dead, start it. **Close guard:** never SIGTERM while `wt queue list --json` (`.lease`) shows `busy.kind` `open` or `close` within `since` + `budget_s` (a killed close can leave the main checkout mid-merge, a killed open leaves a half-built worktree in OPENING): wait for the next event. An open or close past its budget arrives as `runner_journal_silent` with `busy` set: dispatch a sub-agent to check the worktree or main checkout and the process before any restart. A `drain` busy is not guarded. Note the action on the campaign. Escalate only if admission still fails after a restart |
| `runner_journal_silent` | the runner's loop has not ticked for 3 poll intervals although its lease is held and its heartbeat is fresh, and it is not inside a declared `open` / `close` / `drain` step within budget (SPEC-0228; `data`: `silent_s`, `interval_s`, `last_tick_at`, `last_journal_ts`, `draining`, `busy`): the loop is stalled while alive. SIGTERM and restart the runner immediately (step 2): an interrupted persona run is cheaper than a stalled queue, its cost is marked estimated (SPEC-0224) and its worktree edits survive. **Close guard:** never SIGTERM while `wt queue list --json` (`.lease`) shows `busy.kind` `open` or `close` within `since` + `budget_s` (a killed close can leave the main checkout mid-merge, a killed open leaves a half-built worktree in OPENING): wait for the next event. An open or close past its budget arrives as `runner_journal_silent` with `busy` set: dispatch a sub-agent to check the worktree or main checkout and the process before any restart. A `drain` busy is not guarded. If it recurs within minutes of a restart, read `wt queue runner-log --tail 30` and dispatch a sub-agent to diagnose |
| `runner_draining` | the runner has been draining (`data.reason`) for over 60 s, waiting on `data.waiting_on` (each `{campaign, kind, run_ids, timeout_s}`) with `data.ready` backlog items ready (SPEC-0224). If it lists persona runs (`implement` / `triage` / `review`) and `ready` > 0: note it on the waiting items (`wt campaign note`), **do not SIGTERM the runner** (an interrupt now costs that run, honestly marked estimated), and re-check at the next event. If it lists only a `gate`, wait. If `draining_s` exceeds the largest `waiting_on[].timeout_s` + 5 min, treat it as `lease_stale`. If a `runner_journal_silent` event also fired, follow that row instead (with its close guard); after commit a43fef6 the human's policy is to restart at once when admission is blocked, which supersedes the "do not SIGTERM" above. No report to the human |
| `runner_config_invalid` | a config file the runner watches is broken (SPEC-0226; `data.scope`, `path`, `error`). `scope: user` → the human's config file is broken: the runner keeps the last good config, so nothing is stopped; tell the human the path and the error in one line and do not edit their config. `scope: project:<P>` → as `parked` `config_invalid`: if a steward is armed (`wt mailbox list --to steward:<P>`), mail it the error with `wt mailbox send --to steward:<P> --kind question`, otherwise tell the human. The condition clears when the file is fixed |
| `run_failed` with `transient: true` | account limit / overload: note the reset time from `error` if present; once it parks, resume after the reset with `--note "transient API failure"`; this is not the work failing |
| `run_failed` (not transient) | informational; the queue's triage handles it — act only if it parks |
| `parked` `cost_cap` | resume once with `--raise-cap` up to 2× the profile cap and a note; a second cost park → escalate |
| `parked` `loop_exhausted` / `triage_escalated` / `implementer_blocked` | if the failures were transient (`run_failed` with `transient: true` right before), resume with `--reset-loops`; else read the last triage brief; if a steward is armed for the project (`wt mailbox list --to steward:<P>`), ask it with `wt mailbox send --to steward:<P> --kind question --campaign <id> "…"` and wait for its `message`; else escalate with the brief and your recommendation |
| `parked` `rebase_conflict` | the close already tried: generated-files-only conflicts are regenerated (SPEC-0191) and any other conflict goes to the `conflict_resolver` persona up to `MAX_RESOLVE_ATTEMPTS` (2) times (SPEC-0221); so this park means the resolver gave up or failed (the detail says `last: …`), or two verified resolutions were each overtaken by a base move that conflicted again (the detail says `verified`), or the project opted out with `merge.resolver: false`. Read the park detail and the last `conflict resolver did not resolve` finding. If its questions are answerable from project knowledge (ask the steward), `wt campaign note` the answer and resume to `CLOSING`: the resume resets the resolver budget and the next resolver run sees the note through `previous_verdicts` and the ledger. Do not hand-spawn a resolver sub-agent. On a merge campaign (`kind: merge`, SPEC-0233) the resolver works in `repo.worktree` (the requester's adopted worktree, or the one wt created) and the campaign resumes to `CLOSING`: merge campaigns have no `GATING`. Otherwise escalate |
| `parked` `review_escalated` | the conformance reviewer failed to produce a usable verdict twice (SPEC-0187): dispatch a sub-agent to read the campaign's last review runs and the diff and recommend one of: resume to `REVIEWING`, resume to `IMPLEMENTING` with a brief, or escalate; apply its recommendation |
| `parked` `main_dirty` / `main_not_on_base` | someone edited the main checkout in place: dispatch a sub-agent to move the uncommitted work intact onto a new branch in its own worktree (copy changed + untracked files, commit as WIP there, verify the copy, then restore main with `git checkout -- <files>` / remove the moved untracked files), notify the owner (steward) by mail where the work now lives, then resume the campaign to `CLOSING`. Off-base main: switch it back to the base branch only if clean; otherwise move the work first the same way |
| live `wt` broken (any `wt` command fails to import) | same as `main_dirty`: move the in-place WIP to a worktree branch, restore main, notify the owner; use `uv run wt` from a worktree meanwhile |
| `waiting` | none: a started campaign resumed with no free slot (SPEC-0200); it takes the next free slot ahead of the backlog by itself |
| `parked` `human_decision` | waiting for the human's answer (SPEC-0232; `detail` starts with the `ESC-NNNN` id, `escalated_from` is the original reason). Do nothing. If the situation resolves without the human, `wt escalations withdraw <id> --reason … --yes`, then `wt queue resume <campaign> --yes` (a withdraw does not resume) |
| `escalation` / `escalation_withdrawn` | informational: an escalation you or a steward opened or closed (`data.escalation_id`); act only if it contradicts what you decided |
| `parked` `open_failed` / `gh_failed` | read the detail; retry once with `wt queue resume`; if it fails again, dispatch a sub-agent to diagnose, then act or escalate |
| `parked` `gates_failed` / `worktree_dirty` on a merge campaign (`kind: merge`) | the requester's branch is not mergeable as it stands: mail the requester (`merge.requested_by`, e.g. `steward:<P>`) the park detail with `wt mailbox send --to <requester> --kind question --campaign <id> "…"` and wait; they fix the branch, then you `wt queue resume <id> --yes`. Never fix the branch yourself |
| `parked` `not_docs_only` | a `--docs-only` merge changed a path outside `merge.docs_only_paths`, so no gate has checked it. Resuming would only park again: `wt queue abandon <id> --yes` (an adopted worktree and the branch are kept), then `wt queue merge <branch> --by <requester>` without `--docs-only` (dry run, then `--yes`), and tell the requester |
| `message` (`kind` answer) | apply it: `wt campaign note`, then resume / abandon / reprioritize as the answer says |
| `message` (`kind` request) | a steward asks for a queue action (add, merge, prioritize, park, resume): do it via dry-run then `--yes`, and reply with `wt mailbox send --to <from> --kind answer --re <msg_id> "done: …"`. A merge request runs `wt queue merge <branch> --by steward:<Project> [--docs-only]` (dry run, then `--yes`); never merge a branch any other way (not by hand in a shell, not in a background job, not through a sub-agent). `--docs-only` is refused unless every changed path is in the project's `merge.docs_only_paths` |
| `merged` / `handed_off` | `wt spec reconcile --apply`; one-line report. If the merge touched `src/wt/agentic/` or `.wt/project.yaml`, check `wt queue runner-log --tail 3` within one poll interval: if the runner has not re-exec'd (drain waiting on an in-flight persona run), SIGTERM and restart it right away (but never inside the close guard: `busy.kind` `open` or `close` within its budget, see `runner_journal_silent`), do not wait for the drain (IDEA-100; SPEC-0225 removes this). Optional deeper verification goes to a sub-agent |
| `code_changed` | wt's code or skills changed under the follower (it printed this line and exited 0): **re-arm the Monitor now**, with the same command. If `skills_changed` has `wt-conduct`, re-invoke `/wt-conduct` (re-read it) before handling the next event; if it has `wt-orient`, re-read `/wt-orient` first. If the paths touch `src/wt/agentic/`, check the runner re-exec'd (the `merged` row). No report to the human |
| `backlog` with `blocked_by` an unknown spec | the spec is wrong: escalate |
| `admitted`, `resumed`, `cost_warning`, `abandoned` | informational; act only if it contradicts what you decided |

Exact flags: `wt queue --help`, `wt campaign --help`, `wt mailbox --help`.

## Conventions / guardrails

- **A re-exec drain stalls admitted campaigns too.** While the runner drains (lease `draining`), nothing new is submitted: campaigns sitting in GATING / REVIEWING / CLOSING with no child process of theirs (`ps --ppid <runner pid> -o comm=` shows only `claude`) are frozen, not just the backlog. If the drain waits only on long `implement` runs and other campaigns are frozen behind it, SIGTERM and restart; if a gate or close child (`uv`/`python`) is running, wait for it. (Until SPEC-0225/0229 land.)

- **One spec review round, never more.** A spec gets exactly one `wt spec review`. `approve` ⇒ queue it. `revise` ⇒ the author applies the findings (with the conductor's decisions on each) and the spec is queued without a second review; the implementer's brief carries the findings. `split` ⇒ split, and each part gets its own single review. Until the single-round admission rule ships (IDEA-101 spec), `wt queue add` still demands an approve for the current text, so a revised spec waits for that rule instead of being re-reviewed. Never run review → revise → re-review loops.

- **One conductor.** The `conductor` reader lock on the event follower enforces it.
- **Never block.** Quick reads and writes only in your own context; everything else is a
  sub-agent or a background shell (see *Never block*).
- **Never implement in your own context.** Conflict resolution and fixes are done by
  sub-agents you dispatch, inside the campaign worktree, never in the main checkout; branch
  merges, including your own spec edits, go through `wt queue merge --by conductor`; feature
  implementation stays with the loop's personas.
- **Every park gets a decision** (resume, abandon, steward question, or escalation) and a
  `wt campaign note` saying which and why.
- **Clear blockers yourself.** Anything stopping the queue (a broken or dirty main checkout, a
  dead runner, a stuck campaign, a conflict) is yours to resolve through sub-agents; escalate
  to the human only for decisions about what gets built. Never discard anyone's work: move it,
  don't delete it. Judgment calls that sub-agents or specs report (defaults, thresholds,
  profile contents, option names, small scope additions) are yours to settle: decide, note
  the reasoning with `wt campaign note`, and tune later from evidence; never forward them to
  the human as things to adjust.
- **Escalate crisply** (decisions about what gets built only: scope, priorities between
  features, product direction, or anything touching the human's own checkout): campaign, the
  facts, what you recommend, what you need decided. Routine recovery, blocker clearing and
  judgment calls never go to the human. Every "escalate" in the table above means this:
  `wt queue escalate <campaign> --question "…" --option "…" --option "…" --recommend N
  --fact "…" --yes` (no campaign for a runner or global matter). It writes the `ESC-NNNN`
  record, parks the campaign as `human_decision`, mails `human` and sends a desktop
  notification (SPEC-0232). Then give the human exactly one chat line:
  `escalated ESC-NNNN: <question>`. One open escalation per campaign.
- **Mail identity:** send with `WT_MAILBOX_ADDR=conductor` set (or `--from conductor`).
- **Shared ideas file:** verify headings before any `wt idea …` write.
- **SPEC-0138:** the spec in the queue is the governing design.

## Verify

`wt queue conduct-status` shows the runner lease held, `conductor_pid` = your follower, no
parked item without a decision note, and no unanswered conductor mail.
