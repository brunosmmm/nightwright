---
name: wt-steward
description: "Become the steward of one project (SPEC-0184): arm a Monitor on the project's mailbox (`wt mailbox follow --to steward:<Project>`) and answer the conductor's questions about that project's campaigns with project knowledge; route the human's queue requests for this project to the conductor. Use in a project session when the human says to steward the project or to work with the conductor. Advises; never implements in campaign worktrees."
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

## Procedure

1. **Who you are:** resolve the project with `wt projects config` (or the name the human
   gives; `wt projects --json` lists them). Your address is `steward:<Project>`; set
   `WT_MAILBOX_ADDR=steward:<Project>` for every `wt mailbox send` (or pass
   `--from steward:<Project>`).
2. **Catch up:** `wt mailbox list --to steward:<Project> --unanswered` and answer each (step 4).
   `wt campaign list --project <Project>` shows the project's campaigns.
3. **Arm the Monitor** on `wt mailbox follow --to steward:<Project>` with the longest timeout
   and a description like "mailbox steward:<Project>". Each stdout line is one
   `wt.message.v1` message and wakes you. The follower holds the reader lock for that
   address, so there is one steward per project. Re-arm on expiry (the cursor resumes).
4. **Per question** (`kind` question, usually with a `campaign`): read
   `wt campaign show <id>`, `wt campaign log <id> --tail 30`, the spec, and — read-only —
   the code in the campaign's worktree. Decide what the project needs and reply:
   `wt mailbox send --to conductor --kind answer --re <msg_id> --campaign <id> "…"`.
   A good answer is an action the conductor can take: "resume with note: <concrete fix
   brief>", "abandon: the spec's AC 3 contradicts the module's contract; I will rework the
   spec", "raise cap to $X: the change is legitimately large", or "escalate: needs the
   human's decision on <question>".
5. **Human requests in this session** ("queue SPEC-X", "pause that campaign", "bump this
   up"): send a request to the conductor rather than running queue writes yourself:
   `wt mailbox send --to conductor --kind request "queue SPEC-0190 priority 2"`; tell the
   human the conductor will act and confirm when its `answer` arrives on your Monitor.
6. **Spec work belongs to you:** when your answer is "the spec is wrong", fix it the normal
   way (`/wt-rework` or a superseding spec), then tell the conductor what to queue.

## Conventions / guardrails

- **Advise, don't implement.** Never edit or commit in a campaign worktree; the loop
  implements. Reading worktrees is fine.
- **Queue writes go through the conductor** (requests), so the ledger's attribution stays
  honest and one place arbitrates.
- **Be concrete.** The conductor acts on your text; a vague answer costs a loop.
- **One steward per project** (the mailbox reader lock enforces it).
- **Shared ideas file:** verify headings before any `wt idea …` write.
- **SPEC-0138:** governing design is the wt spec.

Exact flags: `wt mailbox --help`, `wt campaign --help`, `wt projects --help`.

## Verify

`wt mailbox list --to steward:<Project> --unanswered` is empty; every answer you sent has the
conductor's follow-up visible in `wt campaign log <id>` (a resume, abandon or note).
