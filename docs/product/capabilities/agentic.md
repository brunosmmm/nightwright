# Agentic loop

A deterministic queue moves **accepted specs** through implementation, gates, triage and
merge without a human at the keyboard. Judgment steps (implement, triage a gate failure)
run as fresh headless Claude personas with a budget and a timeout; every transition, run,
gate result, finding and dollar lands in a per-campaign ledger. Humans see an item only
when the loop has **parked** it. Governing epic: SPEC-0169; the sections below are filled
in by its child specs as they land.

## Project configuration

`<repo>/.wt/project.yaml` is committed with the project and declares what the loop needs
to know about it (SPEC-0170):

- `gates`: ordered shell commands (`name`, `cmd`, `cwd`, `timeout_s`, `required`) run
  inside the campaign worktree; `required` gates block, optional ones only report.
- `merge`: `policy` (`merge` | `pr` | `draft-pr`), `base`, `remote`, `push_after_merge`,
  `delete_branch`, `post_cmds`, `pr.labels` / `reviewers` / `title_prefix`.
- `worktrees`: `root` (default `<repo>/../.wt-worktrees/<project>`), `setup` commands,
  `cleanup` (`on_merge` | `on_close` | `never`).
- `agents`: `context_files` read first by every persona, `permission_mode`,
  `allowed_tools`, `commit_trailer`.

Unknown keys fail; every problem is reported by key path. Gate commands may use the
placeholders `{spec_id}`, `{campaign}`, `{worktree}`, `{base}`.

wt finds the file through the project registry (`wt projects --json`): an outbound
project's `repo_path`, or this wt checkout for `Meta-Tools`. Never from the current
directory alone.

```bash
wt projects config Meta-Tools          # resolved document, defaults filled
wt projects config --json              # wt.project.config.v1 (project from cwd)
wt projects init acme                  # print a starter; --yes writes it, --force overwrites
```

## Campaigns and the ledger

A campaign is one spec's trip through the loop. Each lives under
`<data_dir>/agentic/campaigns/<id>/` as an atomically written `state.json` (schema
`wt.campaign.v1`: state, counters, cost, last gate, park reason) plus an append-only
`ledger.jsonl` with one event per line (`open`, `transition`, `run.start`/`run.end`,
`commit`, `gate`, `finding`, `cost`, `note`, `park`, `resume`, `close`), each carrying a
monotonic `seq`. Writers hold the campaign's `flock`, so the loop and a human command can
touch the same campaign safely. Campaign ids are `<SPEC-ID>-<YYYYMMDD>-<hex>`.

```bash
wt campaign list                 # open campaigns (--all for terminal ones)
wt campaign show CAMPAIGN        # state + last events (--json → wt.campaign.v1)
wt campaign log CAMPAIGN --tail 20   # the ledger (--json → wt.campaign.log.v1)
wt campaign note CAMPAIGN "text" # append a note; inside a persona run $WT_CAMPAIGN is set
```

A single `wt queue run` holds the runner lease (`queue.lock` + `queue.lease.json` with a
heartbeat); a second runner is refused and names the holder. A heartbeat older than
`agentic.lease_ttl_s` is reported as `stale`, never taken over (SPEC-0171).

## Profiles

Models, effort, budgets, loop limits and slots live under `agentic:` in `config.yaml`.
Builtin profiles `balanced` (default: Sonnet implementer and triager, $10 cap, 3 loops,
2 slots), `frugal` and `thorough` ship in code; the user block holds only deltas, deep-merged
over the builtin of the same name (SPEC-0171):

```yaml
agentic:
  default_profile: balanced
  lease_ttl_s: 120
  profiles:
    balanced:
      personas:
        implementer: {max_budget_usd: 6.0}
```

## Gates

Gates are the project's own proof commands from `.wt/project.yaml` — tests, lint, anything
with an exit code (SPEC-0173). The loop runs them in the campaign worktree, **in order and
all of them every time**, so triage and humans see every failure at once rather than the
first one. Each gate has a `timeout_s`; a timeout kills the gate's whole process group.
`required: true` gates block the campaign; optional gates are recorded but never block.

Every gate writes one log under the campaign directory, `gates/NNN-<name>.log`, starting
with a header line that names the command and its working directory; the last 60 lines
travel with the result into the ledger (`gate` event) and into `wt campaign show`. Gate
commands see `WT_CAMPAIGN`, `WT_SPEC_ID`, `WT_WORKTREE`, `WT_BASE`, `WT_DATA_DIR` and
`WT_CONFIG_DIR`, and may use `{spec_id}`, `{campaign}`, `{worktree}`, `{base}` placeholders.

`wt queue gates CAMPAIGN` runs the gates on a campaign's worktree right now — for example
on a parked item — and logs a `gate` event. It never changes the campaign's state or its
`last_gate`, so a debug run cannot steer the queue. `--json` emits `wt.campaign.gate.v1`.

## Personas

Each judgment step is one fresh `claude -p` run (SPEC-0174): a persona markdown template
(`implementer`, `gate_triager`; reviewers come later) is filled with the campaign's context
— the spec text as it is on the branch, the worktree path, the last gate logs, the brief
from the previous loop — and ends with a fixed invariant block (work only in the worktree,
do not commit, finish with the JSON verdict). The runner passes a `--json-schema` so the
verdict is structured, a `--max-budget-usd`, a wall-clock timeout, the profile's model and
effort, and an explicit permission mode (`bypassPermissions` scoped to the worktree for the
implementer; `--restricted` for read-only personas). Cost, usage and session id come back in
the result and go to the ledger. Identical inputs give a byte-identical prompt; `wt campaign
prompt CAMPAIGN PERSONA` prints it without running anything.

For tests and dry runs, `agentic.runner: fake` (or `WT_AGENTIC_RUNNER=fake`) replays a JSON
script of scripted edits and canned verdicts instead of calling `claude`.

## Closing

Closing is the step that guards the base branch, and for wt itself the live editable install
(SPEC-0175). It is plain Python over the git wrapper and the gate executor, and it never
forces anything:

1. The spec in the worktree must verify (`wt spec verify` checks) **and** be `status: done`;
   otherwise the campaign goes back to triage with a synthetic `spec-dod` gate naming what is
   missing.
2. Anything uncommitted in the worktree is snapshotted into a commit.
3. The worktree is rebased onto the target (the local base for `merge`; the fetched remote
   base for `pr` / `draft-pr`). A conflict parks the campaign (`rebase_conflict`) with the
   worktree restored byte-for-byte.
4. **Every gate runs again on the rebased tree.** A required failure goes to triage.
5. `merge` policy: the main checkout must be on the base branch and have no uncommitted
   tracked changes (untracked files are fine), and the base must not have moved since it
   was captured — else `main_not_on_base`, `main_dirty`, or (after two attempts)
   `base_moving`. Then `git merge --ff-only`, the configured `post_cmds`, and an optional
   push.
6. `pr` / `draft-pr` policy: push the branch and `gh pr create` with a generated body (the
   spec's Decision, its acceptance checklist, the gate table, campaign cost). A failing `gh`
   parks (`gh_failed`).
7. Cleanup per `worktrees.cleanup`; a merged branch is deleted, a PR branch kept.

Idea states are not touched here; `/wt-conduct` runs `wt spec reconcile --apply` after merges.

## Queue

The queue is the engine (SPEC-0176). Python decides every transition; personas only produce
commits and one verdict. One process runs the loop at a time (the lease), two or three
campaigns advance concurrently (the profile's `slots`).

```bash
wt queue add SPEC-0170            # plan only (dry-run)
wt queue add SPEC-0170 --yes      # create the campaign in QUEUED
wt queue list                     # in flight: state, loops, cost, lease health
wt queue watch                    # the same, live
wt queue run                      # the loop; wt queue tick = one iteration
```

State machine:

```
QUEUED → OPENING → IMPLEMENTING → GATING → CLOSING → MERGED | HANDED_OFF
                        ▲            │ required gate failed
                        └─ TRIAGING ◀┘   fix (loops+1) · retry_gate (once per loop) ·
                                          waive (optional gates only) · escalate
any state → PARKED (humans only) → resume | abandon
```

- **OPENING** creates the worktree and branch and runs `worktrees.setup`.
- **IMPLEMENTING** spawns a fresh implementer; the runner commits whatever changed.
- **GATING** runs every gate; all required green ⇒ CLOSING, else TRIAGING.
- **TRIAGING** spawns the read-only gate triager only after a failure.
- **CLOSING** rebases, re-gates, audits the spec's Definition of Done, then fast-forwards
  the base or opens a PR.

Park reasons: `cost_cap` (checked pessimistically before every spawn), `loop_exhausted`,
`implementer_blocked` (blocked twice in a row), `triage_escalated`, `open_failed`,
`rebase_conflict`, `main_dirty`, `main_not_on_base`, `base_moving`, `gh_failed`, `human`.
A parked item keeps its worktree; `wt queue resume ID [--to STATE] [--note …]
[--raise-cap USD] [--reset-loops] --yes` continues it, `wt queue abandon ID --yes` ends it.
With a loop running, `wt queue park` applies after the current step is recorded.

Every transition, run (with cost), gate run, finding, park and close is in the campaign's
ledger (`wt campaign log ID`). `--json` shapes: `wt.queue.v1`, `wt.queue.add.v1`,
`wt.queue.tick.v1`, `wt.queue.park.v1`, `wt.queue.resume.v1`, `wt.queue.abandon.v1`.

## Events and watchdog

`wt queue events` is the queue's alert stream (SPEC-0181): `<data_dir>/agentic/events.jsonl`,
one `wt.event.v1` JSON record per line — `admitted`, `parked`, `resumed`, `merged`,
`handed_off`, `abandoned`, `run_failed` (with `transient: true` for account limits and API
overload), `cost_warning`, `backlog`, and mailbox `message`s to the conductor. Events are
projected from the campaign ledgers exactly once, so the stream works with any loop version.
`wt queue events --follow --for conductor` streams new events forever with a per-reader cursor
(a restarted follower resumes after the last delivered event) and runs a **watchdog** outside
the loop: `runner_dead` (work pending, no runner), `lease_stale` (runner hung, no run in
flight), `run_overdue` (a persona run past its timeout), `campaign_silent` (no activity for a
long time). Each watchdog condition fires once until it clears.

## Mailbox

`wt mailbox send|follow|list|thread` (SPEC-0183): per-recipient JSONL inboxes for
`conductor`, `human` and `steward:<Project>`, `wt.message.v1` envelopes with `kind`
(question / answer / request / info), `campaign` and `re` threading. Messages to the
conductor also appear in its event stream, so one Monitor wakes it for both.

## Conductor

`/wt-conduct` (SPEC-0182) turns a session into the **conductor**: it arms Claude Code's
Monitor on `wt queue events --follow --for conductor` and is woken by every event — a park,
a merge, a dead or hung runner, an overdue run, a steward's message — and resolves it from a
decision table (resume, raise the cap once, ask the project's steward, restart the loop,
reconcile ideas after a merge), escalating to the human only for design questions and the
human's own checkout. `wt queue conduct-status` is its one-call situation report. The
follower's reader lock means only one conductor can be armed.

## Stewards

`/wt-steward` (SPEC-0184) turns a project session into `steward:<Project>`: a Monitor on
`wt mailbox follow --to steward:<Project>` wakes it for the conductor's questions about that
project's campaigns; it answers with a concrete action, and routes the human's queue requests
for the project to the conductor. One steward per project (reader lock); stewards advise and
never implement in campaign worktrees.

wt dogfoods itself: the repo's own `.wt/project.yaml` declares `uv run pytest -q -x` and
`python3 tools/spec_lint.py` as required gates, merge policy `merge` into `main`, and
`uv sync --extra tui` as worktree setup. `wt projects config Meta-Tools` shows it.
