---
name: wt-conduct
description: "Run, watch and unblock wt's agentic queue (SPEC-0169/0176): start `wt queue run` detached when no live lease exists, report what is in flight, decide parked campaigns (resume / abandon / escalate to the human), reconcile idea states after merges. Use when the human asks to conduct, run, watch or unpark the agentic loop. Never implements anything itself."
trigger: conduct the queue; run the agentic loop; what is parked; unpark a campaign; watch campaigns
---

# wt-conduct

## When to use

The human wants the agentic loop running, wants to know what it is doing, or wants a
parked campaign dealt with. You are the **conductor**: the loop itself is a Python process
(`wt queue run`), personas are its headless sub-agents, and your job is to start it,
observe it, and make the few decisions it parks for a human — without ever doing the
implementation work yourself.

Not for authoring specs (`/wt-new-work`), implementing (`/wt-implement-spec`) or auditing a
spec (`/wt-verify`). Queue entry is `wt queue add` on an **accepted** spec; it only happens
when the human names the spec.

## Procedure

1. **Look first:** `wt queue list --json` (`wt.queue.v1`). Read `lease` (held? stale?),
   `slots`, and every item's `state`, `loops`, `cost_usd`, `park`.
2. **Start the service if nothing holds the lease** (`lease.held` false). The launcher
   comes from config `agentic.run_launcher` (`wt projects config` shows the project side;
   the runner side is `~/.config/wt/config.yaml`):
   - `nohup` (default): `nohup wt queue run >> <data_dir>/agentic/runner.log 2>&1 &`
     (`<data_dir>` is usually `~/.local/share/wt`), then re-run `wt queue list` and confirm
     `lease.held` with the new pid.
   - `tmux`: `tmux new -d -s wt-queue 'wt queue run'`.
   - `foreground`: run `wt queue run` in a dedicated pane when the human asks for that.
   A **stale** lease (`stale: true`: held, heartbeat older than `ttl_s`) is a hung runner.
   Report its pid; do not kill it or take the lease over unless the human says so.
3. **Report** the items: state, loop count, cost against cap, age. Exact flags and the
   watch view: `wt queue --help` (`wt queue watch` renders the same payload live).
4. **For each `PARKED` item:** read `wt campaign show <id>` and
   `wt campaign log <id> --tail 20` (`wt campaign --help`). Decide:
   - `wt queue resume <id> --yes` (optionally `--to`, `--note "…"` for the next
     implementer, `--raise-cap`, `--reset-loops`) when the cause is understood and the
     fix is a human-side action already taken (main checkout cleaned, cap too low, flaky
     environment) or a brief the implementer can act on;
   - `wt queue abandon <id> --yes` when the spec should not proceed in this form (then tell
     the human which spec and why, so they can rework it via `/wt-rework`);
   - **escalate to the human** with the park reason, the log tail and your recommendation
     when it is a design conflict, a rebase conflict in real code, or anything that
     changes what gets built. Say what you need decided.
   Every resume/abandon is a `--yes` write: dry-run first and show the plan if unsure.
5. **After merges** (`MERGED` / `HANDED_OFF` items appear under `wt campaign list --all`):
   run `wt spec reconcile --apply` so the source ideas move on (PROMOTED / SHIPPED), and
   mention merged spec ids to the human.
6. **Queue new work only on request:** `wt queue add SPEC-NNNN` (dry-run), then `--yes`.
   The spec must be `accepted`; the project must have `.wt/project.yaml`
   (`wt projects config NAME`).

## Conventions / guardrails

- **One conductor.** The lease enforces one `wt queue run`; never start a second, never
  kill a live holder without the human.
- **Never implement.** Do not edit code in a campaign worktree, do not commit on a
  campaign branch, do not run gates by hand except `wt queue gates <id>` for diagnosis.
- **Parks are decisions, not noise.** Each parked item gets a `resume`, an `abandon`, or an
  explicit escalation; none is silently left.
- **No `--force` past safety.** `rebase_conflict`, `main_dirty`, `main_not_on_base` mean a
  human-side condition; fix the condition, then resume.
- **Shared ideas file.** Before any `wt idea …` write, verify the heading with
  `wt idea show ID --json`; ids are global across projects.
- **SPEC-0138:** the governing design is the spec in the queue; Superpowers notes never
  override it.

## Verify

`wt queue list` shows `lease.held` true (fresh heartbeat) and items moving between ticks;
every `PARKED` item has a `resume` / `abandon` event in its log or an escalation message
to the human; after a merge the spec's idea is no longer `SPECCED`
(`wt spec reconcile` reports nothing to advance).
