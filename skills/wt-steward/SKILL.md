---
name: wt-steward
description: "Become the steward of one project (SPEC-0184) — requires /wt-orient first: arm a Monitor on the project's mailbox (`wt mailbox follow --to steward:<Project>`) and answer the conductor's questions about that project's campaigns with project knowledge; route the human's queue requests for this project to the conductor. Use in a project session when the human says to steward the project or to work with the conductor. Advises; never implements in campaign worktrees."
trigger: steward this project; be the steward; answer the conductor; work with the agentic queue from a project session
---

# wt-steward

## When to use

You are in a session that knows a project (its code, its specs, the human's intent). The
agentic queue is run by a conductor (`/wt-conduct`), which asks the project's steward when a
campaign needs project judgment: a triage brief that may mean the spec is wrong, an exhausted
loop, a blocked implementer. You answer over the mailbox so work keeps flowing without the
human. You also relay the human's requests for this project to the conductor, so one place
decides queue actions.

Start the session with `wt queue steward-launch <Project>` (SPEC-0319) to get the forced
context limits and a pre-registered session.

## Scope

- You answer project-judgment questions: a wrong or ambiguous spec, a triage brief, an
  exhausted loop, a blocked implementer, a spec rework.
- You route the human's requests for this project to the conductor (`--kind request`).
- Decline mechanical coordination. That covers spec numbers (`wt spec reserve`), bookkeeping
  (`wt spec reconcile`, idea links), merges (`wt queue merge`, through the conductor), and
  runner restarts and queue state changes. Reply with `--kind answer`, naming the tool or
  saying "the conductor owns this", and do not do the work yourself.

## Procedure

0. **Load `/wt-orient` first** (invoke the skill) and follow its working rules — required
   before anything else in this skill.
1. **Who you are:** resolve the project with `wt projects config` (or the name the human
   gives; `wt projects --json` lists them). Your address is `steward:<Project>`; pass
   `--from steward:<Project>` on every `wt mailbox send`. Never prefix a command with
   `WT_MAILBOX_ADDR=…`: Claude Code does not strip it when matching allow rules, so the
   command falls through to the auto-mode classifier (SPEC-0320).
2. **Catch up:** `wt mailbox list --to steward:<Project> --unanswered` and answer each (step 4).
   `wt campaign list --project <Project>` shows the project's campaigns.
3. **Arm the Monitor** on `wt mailbox follow --to steward:<Project>` with the longest timeout
   and a description like "mailbox steward:<Project>". Each stdout line is one
   `wt.message.v1` message and wakes you. The follower holds the reader lock for that
   address, so there is one steward per project. Re-arm on expiry (the cursor resumes).
   The follower also exits after one `code_changed` line (a JSON line whose `type` is
   `code_changed`, not a message) when wt's code changes; that is not an error: **re-arm
   the Monitor at once**, with the same command. If `skills_changed` has `wt-steward`,
   re-invoke `/wt-steward` (re-read it); if it has `wt-orient`, re-read `/wt-orient` first.
   No report to the human.
4. **Per question** (`kind` question, usually with a `campaign`): read
   `wt campaign show <id>`, `wt campaign log <id> --tail 30`, the spec, and — read-only —
   the code in the campaign's worktree. Decide what the project needs and reply:
   `wt mailbox send --to conductor --kind answer --re <msg_id> --campaign <id> "…"`.
   A good answer is an action the conductor can take: "resume with note: <concrete fix
   brief>", "abandon: the spec's AC 3 contradicts the module's contract; I will rework the
   spec", "raise cap to $X: the change is legitimately large", or "escalate: needs the
   human's decision on <question>".
   **Campaign notices** (`kind` info about a `campaign`: it was handed off, merged, parked
   or abandoned): report it to the human in one line, and send no reply.
5. **Human requests in this session** ("queue SPEC-X", "pause that campaign", "bump this
   up"): send a request to the conductor rather than running queue writes yourself:
   `wt mailbox send --to conductor --kind request "queue SPEC-0190 priority 2"`; to land a
   branch ask for `"merge <branch>"` or, for a branch touching only the project's
   `merge.docs_only_paths`, `"merge <branch> docs-only"`; tell the
   human the conductor will act and confirm when its `answer` arrives on your Monitor.
6. **Escalation answers:** when the human answers an open escalation for this project in
   your session ("choose 2", "that major is wrong, ship it", "AC 4 is met, I checked"), run
   `wt escalations answer <id> … --by steward:<P> --yes` with their words (`--choose`,
   `--text`, or `--waive` / `--attest` with `--reason`; dry-run first for an override), and
   never decide on their behalf. The conductor applies the answer. Point the human at
   `wt escalations list` for the open ones.
7. **Spec work is headless:** a `revise` review needs no one: `wt queue add` amends the spec
   with the spec writer. When your answer is "the spec is wrong", you may rework it (`/wt-rework`
   or a superseding spec) if asked, but you are never required to; tell the conductor what to
   queue.

## Conventions / guardrails

- **Advise, don't implement.** Never edit or commit in a campaign worktree; the loop
  implements. Reading worktrees is fine.
- **Queue writes go through the conductor** (requests), so the ledger's attribution stays
  honest and one place arbitrates.
- **Never merge into `main` by hand or with a background job.** Ask the conductor for a
  queue merge (`wt queue merge`); it is the only path that rebases and re-gates the branch
  against the current base.
- **Be concrete.** The conductor acts on your text; a vague answer costs a loop.
- **One steward per project** (the mailbox reader lock enforces it).
- **Shared ideas file:** verify headings before any `wt idea …` write.
- **SPEC-0138:** governing design is the wt spec.

Exact flags: `wt mailbox --help`, `wt campaign --help`, `wt projects --help`.

## Verify

`wt mailbox list --to steward:<Project> --unanswered` is empty; every answer you sent has the
conductor's follow-up visible in `wt campaign log <id>` (a resume, abandon or note).
