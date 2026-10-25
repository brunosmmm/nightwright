---
name: wt-conduct
description: "Become wt's conductor (SPEC-0182): a long-lived session that arms a Monitor on the queue's event stream (`wt queue events --follow --for conductor`) and is woken by every queue event — parks, merges, failed runs, stuck or dead runners, steward mail — and resolves each from a decision table, escalating to the human only when the table says so. Use when the human asks to conduct / run / watch the agentic queue. Never implements anything itself."
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

## Procedure

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
   Anything long (reading a big diff, investigating a failure) goes to a sub-agent so you stay
   responsive. Then wait for the next event.
6. **Reporting:** after a merge or an escalation, give the human one short line (spec, outcome,
   cost). Don't narrate routine events.

## Decision table

| event `type` | action |
|---|---|
| `runner_dead` | start the loop (step 2); if it dies again within minutes, read the `runner.log` tail and escalate |
| `lease_stale` | the runner is hung (no run in flight within its timeout): report pid + heartbeat age to the human; do not kill it unless told |
| `run_overdue` | a persona run exceeded its timeout + grace: check the process (`ps`); if gone, `wt queue park` then `wt queue resume`; if alive, wait one interval then escalate |
| `campaign_silent` | no ledger activity for long while active: same checks as `run_overdue` |
| `run_failed` with `transient: true` | account limit / overload: note the reset time from `error` if present; once it parks, resume after the reset with `--note "transient API failure"`; this is not the work failing |
| `run_failed` (not transient) | informational; the queue's triage handles it — act only if it parks |
| `parked` `cost_cap` | resume once with `--raise-cap` up to 2× the profile cap and a note; a second cost park → escalate |
| `parked` `loop_exhausted` / `triage_escalated` / `implementer_blocked` | if the failures were transient (`run_failed` with `transient: true` right before), resume with `--reset-loops`; else read the last triage brief; if a steward is armed for the project (`wt mailbox list --to steward:<P>`), ask it with `wt mailbox send --to steward:<P> --kind question --campaign <id> "…"` and wait for its `message`; else escalate with the brief and your recommendation |
| `parked` `rebase_conflict` | if every conflicted path is a generated file (e.g. `docs/LEDGER.md`), resume to `CLOSING` (the close step re-runs gates); otherwise escalate |
| `parked` `main_dirty` / `main_not_on_base` | the human's checkout: escalate (never stash or switch branches yourself) |
| `parked` `open_failed` / `gh_failed` | read the detail; retry once with `wt queue resume`; then escalate |
| `message` (`kind` answer) | apply it: `wt campaign note`, then resume / abandon / reprioritize as the answer says |
| `message` (`kind` request) | a steward asks for a queue action (add, prioritize, park, resume): do it via dry-run then `--yes`, and reply with `wt mailbox send --to <from> --kind answer --re <msg_id> "done: …"` |
| `merged` / `handed_off` | `wt spec reconcile --apply`; one-line report; if the merged spec changed `src/wt/agentic/`, restart the loop (SIGTERM the runner pid, then step 2) so it runs the new code |
| `backlog` with `blocked_by` an unknown spec | the spec is wrong: escalate |
| `admitted`, `resumed`, `cost_warning`, `abandoned` | informational; act only if it contradicts what you decided |

Exact flags: `wt queue --help`, `wt campaign --help`, `wt mailbox --help`.

## Conventions / guardrails

- **One conductor.** The `conductor` reader lock on the event follower enforces it.
- **Never implement.** No edits or commits in campaign worktrees or in the main checkout.
- **Every park gets a decision** (resume, abandon, steward question, or escalation) and a
  `wt campaign note` saying which and why.
- **Escalate crisply:** campaign, park reason, the facts, what you recommend, what you need
  decided. Escalation is for design and for the human's own checkout, not routine recovery.
- **Mail identity:** send with `WT_MAILBOX_ADDR=conductor` set (or `--from conductor`).
- **Shared ideas file:** verify headings before any `wt idea …` write.
- **SPEC-0138:** the spec in the queue is the governing design.

## Verify

`wt queue conduct-status` shows the runner lease held, `conductor_pid` = your follower, no
parked item without a decision note, and no unanswered conductor mail.
