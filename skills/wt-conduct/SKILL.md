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
`wt queue resume|park|abandon|prioritize`, `wt campaign note`, `wt mailbox send`,
`wt spec reconcile --apply`: commands that return in seconds.

**Everything else is dispatched, never run inline.** Dispatch work to fresh sub-agents (general-purpose, or Explore for read-only search) with a self-contained brief and a model chosen per task (sonnet for routine work, opus only for hard design or review). Never use forks — they inherit the whole conversation and cost an order of magnitude more; write any needed context into the brief. The brief names the
campaign, the governing spec path, the worktree, the files to read, the files the sub-agent may
change, these rules, and what to report.

- **Sub-agent** (preferred) for any work that needs judgment or more than a few seconds:
  resolving a rebase conflict in a campaign worktree, completing or retrying a merge,
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
   after the last delivered event; nothing is lost or repeated).
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
| `run_failed` with `transient: true` | account limit / overload: note the reset time from `error` if present; once it parks, resume after the reset with `--note "transient API failure"`; this is not the work failing |
| `run_failed` (not transient) | informational; the queue's triage handles it — act only if it parks |
| `parked` `cost_cap` | resume once with `--raise-cap` up to 2× the profile cap and a note; a second cost park → escalate |
| `parked` `loop_exhausted` / `triage_escalated` / `implementer_blocked` | if the failures were transient (`run_failed` with `transient: true` right before), resume with `--reset-loops`; else read the last triage brief; if a steward is armed for the project (`wt mailbox list --to steward:<P>`), ask it with `wt mailbox send --to steward:<P> --kind question --campaign <id> "…"` and wait for its `message`; else escalate with the brief and your recommendation |
| `parked` `rebase_conflict` | generated-files-only conflicts are now resolved automatically by the close step (SPEC-0191, `merge.regenerate` in `.wt/project.yaml`), so such parks should no longer occur; if one does, treat it like any other conflict: dispatch a resolver sub-agent into the campaign worktree to rebase onto the base and resolve the conflicts preserving both intents, commit on the campaign branch, and report; then resume to `GATING` (not `CLOSING`) so the rebased result passes the gates and the conformance review (REVIEWING, SPEC-0187) before the close step. Escalate only if the resolver reports a semantic conflict it cannot settle |
| `parked` `review_escalated` | the conformance reviewer failed to produce a usable verdict twice (SPEC-0187): dispatch a sub-agent to read the campaign's last review runs and the diff and recommend one of: resume to `REVIEWING`, resume to `IMPLEMENTING` with a brief, or escalate; apply its recommendation |
| `parked` `main_dirty` / `main_not_on_base` | someone edited the main checkout in place: dispatch a sub-agent to move the uncommitted work intact onto a new branch in its own worktree (copy changed + untracked files, commit as WIP there, verify the copy, then restore main with `git checkout -- <files>` / remove the moved untracked files), notify the owner (steward) by mail where the work now lives, then resume the campaign to `CLOSING`. Off-base main: switch it back to the base branch only if clean; otherwise move the work first the same way |
| live `wt` broken (any `wt` command fails to import) | same as `main_dirty`: move the in-place WIP to a worktree branch, restore main, notify the owner; use `uv run wt` from a worktree meanwhile |
| `parked` `open_failed` / `gh_failed` | read the detail; retry once with `wt queue resume`; if it fails again, dispatch a sub-agent to diagnose, then act or escalate |
| `message` (`kind` answer) | apply it: `wt campaign note`, then resume / abandon / reprioritize as the answer says |
| `message` (`kind` request) | a steward asks for a queue action (add, prioritize, park, resume): do it via dry-run then `--yes`, and reply with `wt mailbox send --to <from> --kind answer --re <msg_id> "done: …"` |
| `merged` / `handed_off` | `wt spec reconcile --apply`; one-line report; the runner re-execs on code changes by itself (check `wt queue runner-log --tail 3`); optional deeper verification goes to a sub-agent |
| `backlog` with `blocked_by` an unknown spec | the spec is wrong: escalate |
| `admitted`, `resumed`, `cost_warning`, `abandoned` | informational; act only if it contradicts what you decided |

Exact flags: `wt queue --help`, `wt campaign --help`, `wt mailbox --help`.

## Conventions / guardrails

- **One conductor.** The `conductor` reader lock on the event follower enforces it.
- **Never block.** Quick reads and writes only in your own context; everything else is a
  sub-agent or a background shell (see *Never block*).
- **Never implement in your own context.** Conflict resolution, merge repair and fixes are
  done by sub-agents you dispatch, inside the campaign worktree, never in the main checkout;
  feature implementation stays with the loop's personas.
- **Every park gets a decision** (resume, abandon, steward question, or escalation) and a
  `wt campaign note` saying which and why.
- **Clear blockers yourself.** Anything stopping the queue (a broken or dirty main checkout, a
  dead runner, a stuck campaign, a conflict) is yours to resolve through sub-agents; escalate
  to the human only for decisions about what gets built. Never discard anyone's work: move it,
  don't delete it.
- **Escalate crisply** (decisions about what gets built only): campaign, the facts, what you
  recommend, what you need decided. Routine recovery and blocker clearing never go to the
  human.
- **Mail identity:** send with `WT_MAILBOX_ADDR=conductor` set (or `--from conductor`).
- **Shared ideas file:** verify headings before any `wt idea …` write.
- **SPEC-0138:** the spec in the queue is the governing design.

## Verify

`wt queue conduct-status` shows the runner lease held, `conductor_pid` = your follower, no
parked item without a decision note, and no unanswered conductor mail.
