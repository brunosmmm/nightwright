---
id: SPEC-0169
title: "Mini agentic workflow: queue, campaigns, headless personas, conductor"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: epic
tags: [agentic, queue, campaigns, personas, worktrees, safety]
source_idea: IDEA-016
---

## Context

Promoted from idea `IDEA-016`; the source document is `plans/mini-agentic-wt.org`.

wt already carries a thought from capture to shipped work: idea → explore → accepted spec
→ implement → `/wt-verify`. Every step is still driven by a human at a keyboard, one spec at
a time. The plan asks for the same pipeline to run **by itself**: a deterministic queue moves
accepted specs through implementation, gates, triage and merge; the judgment steps run as
fresh headless Claude sub-agents with a budget; every transition, run, gate result, finding
and dollar lands in a per-campaign ledger; a human sees an item only when the system has
parked it. The system should then build its own later pieces (dogfooding).

Constraints that shape the design:

- **The dogfood target is wt itself.** The user's global `wt` is an editable install of this
  checkout, so a merge to `main` changes the live tool at once. Work must happen in git
  worktrees and land only after gates pass on the rebased tree.
- **Spec-first repo.** AGENTS.md needs an accepted governing spec per behavior change and
  `/wt-verify` to claim done. The loop has to honor that, not bypass it.
- **Shared ideas file.** Several sessions write the same `ideas.org`; ids are global
  (SPEC-0167). Any automation that touches ideas verifies the heading first.
- **No daemon precedent.** SPEC-0001 and SPEC-0064 ruled out daemons *for their scope*
  (time tracking, sheet sync). This epic deliberately adds a lease-guarded foreground loop
  for the queue; those decisions are not superseded, their scope is simply different.
- **Existing building blocks** (reused, not rewritten): the `--yes` dry-run / `--json`
  `{"schema": "wt.x.v1"}` convention, `report._dump_json`, `export._atomic_write`,
  `rules.resolve_research_context` / `wt_checkout` for project → repo root,
  `specmeta`, `verify.verify_spec`, `specs.seed_ideas_from_epic`, `reconcile_ideas`.
  Missing today and added here: a lock primitive, git branch/worktree/`gh` operations, and
  a headless runner that reads cost and enforces a timeout (the only `claude -p` call so
  far, `meetings.py`, does neither).

Decisions taken with the user on 2026-10-25: wt owns the loop (not a Claude session); the
first slice is the thin implement loop; the default profile is "balanced" (Sonnet for
implementer/triager, Opus for later reviewers, ~$10 per campaign, 3 loops, 2 slots);
per-project config lives in-repo at `.wt/project.yaml`; wt itself merges straight to `main`;
personas run with `bypassPermissions` scoped to the worktree; the service is a detached
process started by `/wt-conduct`; worktrees live beside the repo.

## Goals / Non-goals

**Goals**
- An accepted spec can be queued and, with no human action, end up merged into its base
  branch (or handed off as a PR), with gates green on the rebased tree.
- Python decides every state transition. Agents only produce commits and one structured
  verdict per run. Gates are truth; verdicts steer.
- Every run is a fresh `claude -p` with a model, an effort, a dollar budget and a wall-clock
  timeout. Nothing is resumed. Every run's cost is in the ledger before the next transition.
- A campaign that exhausts its loops or budget, hits a rebase conflict, or finds the main
  checkout dirty is **parked** with a reason; humans resume, abandon or raise the cap.
- Project-specific knowledge (gates, merge policy, worktree setup) is committed with the
  project, not kept in wt's user config.
- The whole loop is testable offline: a scripted fake runner, shell-script gates, temporary
  git repos, a fake `gh`.
- `wt queue list --json` / `wt queue watch` show what is in flight, in which state, and what
  it has cost.

**Non-goals (this epic)**
- Reviews (conformance / test / adversarial), the spec-writing and spec-review phase, the
  mailbox and `/wt-steward`, the TUI watch tab and exploratory campaigns are **later waves**:
  they appear in the Breakdown as child ideas and get their own specs once the loop runs.
- Automatic rebase-conflict resolution (wave 1 parks).
- Locking or id reservation for concurrent writers of the ideas file.
- An MCP server (SPEC-0053 stands).
- A persistent supervisor (systemd); the service is a detached process plus a lease.
  *Note (2026-11-09): the human reversed this non-goal; the runner is supervised by a
  systemd user unit, [SPEC-0483](./0483-runner-under-a-systemd-user-unit.md).*

## Decision

Ship a new package `src/wt/agentic/` with a deterministic campaign store, a singleton
runner lease, a queue state machine, a headless persona runner, a gate executor, git
worktree operations and a close step, exposed as `wt queue …`, `wt campaign …` and
`wt projects config|init`, plus a `/wt-conduct` skill that starts and supervises the loop.
Wave 1 implements the **thin implement loop**: `QUEUED → OPENING → IMPLEMENTING → GATING →
(TRIAGING →)* → CLOSING → MERGED | HANDED_OFF`, with `PARKED` as the only human touchpoint.
Later waves add states and personas without changing the store, the ledger or the runner
contract.

## Architecture / cross-cutting design

### Invariant

Python decides every transition; agents only produce commits and one structured verdict
(`--json-schema`). Gates are truth; verdicts steer. Fresh `claude -p` per run, never resumed.
Cost of every run is logged before the next transition; before every spawn the campaign cap
is checked pessimistically (`total_usd + persona.max_budget_usd > cap_usd` ⇒ PARKED).

### Modules (`src/wt/agentic/`)

| file | role |
|---|---|
| `project.py` | load / validate / default `<repo>/.wt/project.yaml`; `project_config(cfg, project)` resolves the repo through `rules.resolve_research_context`; `expand_cmd` with `{spec_id} {campaign} {worktree} {base}`; `find_project_for_cwd` |
| `profiles.py` | `BUILTIN_PROFILES` (`balanced`, `frugal`, `thorough`) and `resolve_profile(cfg, name)`, deep-merging user deltas because `load_config` merges one level only |
| `store.py` | campaign ids and dirs, `read_state` / `write_state` (atomic), `append_event` (`O_APPEND` + `flock`, monotonic `seq`), `read_ledger` (tolerant), `item_lock`, `list_campaigns`, `TERMINAL_STATES` |
| `lease.py` | singleton runner lease: `flock` on `queue.lock` plus `queue.lease.json` `{pid, host, argv, started_at, heartbeat_at, ttl_s}`; `lease_status()` reports `stale` (advisory; the flock is the exclusion) |
| `gitops.py` | thin `git()` wrapper: worktree add/remove, `commit_all`, `rebase` (aborts on conflict and returns the conflicted paths), `ff_merge`, `fetch` / `push`, `is_clean`, `current_branch`, `ahead_behind`, `gh_pr_create` (`gh` from PATH); `GitError(cmd, code, tail)` |
| `gates.py` | run every configured gate in the worktree (no short-circuit), timeout kills the process group, per-gate logs, `GateRun(seq, ok, results[])`; `required` decides blocking |
| `runner.py` | `RunRequest` / `RunResult`, `PersonaRunner` protocol, `ClaudeRunner`, `FakeRunner` (scripted edits + canned verdicts), `select_runner(cfg)` |
| `personas.py` + `personas/*.md` | deterministic prompt assembly (regex placeholders, no timestamps), verdict JSON schemas, read-only flag |
| `close.py` | rebase → re-gate → ff-only merge or PR hand-off; `pr_body()` as a pure function |
| `queue.py` | `plan_step` (pure) / `apply_step`, FIFO admission by slots, `tick`, `run`, `queue_payload`, signal handling |
| `cli.py` | click groups `queue` and `campaign`, `projects config|init`; registered from `wt/cli.py` with `cli.add_command` |

Also touched: `completion.complete_campaign_id`, `verify.verify_spec_file(path)` factored out
of `verify_spec`, `config.DEFAULT_CONFIG["agentic"]`, `skills/wt-conduct/SKILL.md`, and the
repo's own `.wt/project.yaml`.

### Data layout (`<data_dir>/agentic/`)

```
queue.lock  queue.lease.json  runner.log
campaigns/<id>/.lock  state.json  ledger.jsonl
campaigns/<id>/runs/NNN-<persona>/{prompt.md,result.json,stdout.log,stderr.log}
campaigns/<id>/gates/NNN-<gate>.log
```

The queue is a scan of `campaigns/*/state.json`; there is no second index.
`campaign_id = <SPEC-ID>-<YYYYMMDD>-<4 hex>`, branch `wt/<campaign_id>`, one non-terminal
campaign per spec. Worktrees default to `<repo>/../.wt-worktrees/<project>/<campaign_id>`.

### Shapes

- **`wt.campaign.v1`** (`state.json`): `id, kind, spec_id, project, profile, state,
  prev_state, created_at, updated_at, repo{root, base, base_sha_at_open, branch, worktree,
  head}, spec{rel_path, internal, sha256_at_open}, counters{loops, runs, gate_runs,
  gate_retries, close_attempts, parks}, cost{total_usd, cap_usd, by_persona}, last_gate,
  last_verdict, pending_brief{source, fix_brief, must_fix[], human_notes[]}, park{reason,
  detail, since, resume_to} | null, close{mode, merged_sha, pr_url, gates_seq} | null`.
- **Ledger events** (`ledger.jsonl`, envelope `ts, seq, campaign, type, by` with
  `by ∈ {tick, human, runner, agent:<persona>}`): `open`, `transition{from, to, reason}`,
  `run.start`, `run.end{exit_code, is_error, timed_out, interrupted, duration_s, cost_usd,
  usage, session_id, verdict_ok, verdict}`, `commit`, `gate`, `finding{severity ∈ must_fix |
  major | minor | nit | info}`, `cost{delta_usd, total_usd, cap_usd}`, `note`, `park`,
  `resume`, `close`.
- **`wt.queue.v1`** (the wave-1 watch): `lease{held, pid, heartbeat_age_s, ttl_s, stale},
  slots{total, busy}, items[{campaign, spec_id, project, state, loops, cost_usd, cap_usd,
  age_s, updated_age_s, park, worktree, branch}]`.
- Other schema ids: `wt.queue.add.v1`, `wt.queue.tick.v1`, `wt.queue.park.v1`,
  `wt.queue.resume.v1`, `wt.queue.abandon.v1`, `wt.campaigns.v1`, `wt.campaign.log.v1`,
  `wt.campaign.gate.v1`, `wt.project.config.v1`.

### `.wt/project.yaml` (strict: unknown keys fail)

`schema: wt.project.v1; project; base; gates[{name, cmd, cwd, timeout_s, required}];
merge{policy merge | pr | draft-pr, base, remote, push_after_merge, delete_branch,
post_cmds[], pr{labels, reviewers, title_prefix}}; worktrees{root, setup[], cleanup
on_merge | on_close | never}; agents{context_files[], permission_mode, allowed_tools[],
commit_trailer}`. wt's own file: gates `uv run pytest -q -x` and
`python3 tools/spec_lint.py`, policy `merge`, base `main`, setup `uv sync --extra tui`.

### User config `agentic:`

`runner claude | fake, fake_script, default_profile, poll_interval_s, lease_ttl_s,
run_launcher nohup | tmux | foreground, profiles{<name>: deltas}`. Builtin `balanced`:
`slots 2, loop_max 3, campaign_cap_usd 10, run_timeout_s 1800, personas{implementer{model
sonnet, effort high, max_budget_usd 4, permission_mode bypassPermissions},
gate_triager{model sonnet, effort medium, max_budget_usd 0.5, read_only}}`. Model values are
`claude --model` aliases so defaults do not rot.

### Wave-1 state machine

```
QUEUED ─slot free─▶ OPENING ─▶ IMPLEMENTING ─▶ GATING ─all required ok─▶ CLOSING ─▶ MERGED | HANDED_OFF
                                   ▲             │ required failure              │ re-gate fail / spec DoD
                       fix (loops<max)           ▼                               ▼
                                   └──────── TRIAGING ◀──────────────────────────┘
                                                 │ retry_gate (≤1/loop) → GATING · waive (all failing optional) → CLOSING
any state ─cost cap · loop_max · 2× blocked · escalate · precondition─▶ PARKED ─resume─▶ resume_to
PARKED | QUEUED ─abandon─▶ ABANDONED
```

Park reasons: `cost_cap, loop_exhausted, implementer_blocked, triage_escalated, open_failed,
rebase_conflict, main_dirty, main_not_on_base, base_moving, gh_failed`. The triager runs
**only** on gate failure. `tick` = acquire lease → scan → admit → agent steps
(`IMPLEMENTING`, `GATING`, `TRIAGING`) in a `ThreadPoolExecutor(slots)`; `OPENING` and
`CLOSING` run serially on the main thread; all state writes happen on the main thread
under `item_lock`. `run` = loop + heartbeat + SIGINT/SIGTERM (stop admitting, terminate the
child `claude` with a grace period, log `run.end{interrupted}`, leave state unchanged).

### Persona contract

Prompt = persona markdown + injected context (campaign, spec text read **from the
worktree**, worktree, base, loop / loop_max, context files, gate tails ≤ 60 lines,
pending brief, last two verdict summaries) + a fixed invariant block also passed as
`--append-system-prompt`: work only in the worktree, do not commit (the runner commits), use
`wt campaign note`, finish with the JSON verdict. `ClaudeRunner` argv: `claude -p
--output-format json --json-schema S --model M [--effort E] --max-budget-usd B
--permission-mode PM --permission-prompts none --no-session-persistence --add-dir
<worktree> --append-system-prompt I [--allowedTools …] [--restricted for read-only]`,
prompt on stdin, `cwd = worktree`, `subprocess.run(timeout = run_timeout_s)`, child env plus
`WT_CAMPAIGN`, `WT_DATA_DIR`, `WT_CONFIG_DIR`. The result's `structured_output`
(fallback: parse `result`), `total_cost_usd`, `usage`, `session_id`, `is_error` are read; a
missing cost is charged at the persona's `max_budget_usd`. Verdicts: implementer `{status
done | partial | blocked, summary, changed_files, tests_run, ac_status[], blockers,
followup_ideas}`; gate triager `{disposition fix | retry_gate | waive | escalate,
confidence, suspected_flaky, must_fix[], waived[], fix_brief, followup_ideas}`.

The **implementer** sets the spec's `status` (`in-progress`, then `done`) and regenerates
whatever the repo requires (AGENTS.md); the close step audits that mechanically.

### Close algorithm

1. `verify_spec_file(worktree/spec)` must be ok with `status: done`, else → `TRIAGING` with a
   synthetic failed gate `spec-dod` (counts a loop).
2. Worktree clean (the runner commits).
3. Target = local base (`merge`) or fetched remote base (`pr` / `draft-pr`).
4. `git rebase` in the worktree; conflict ⇒ abort ⇒ `PARKED(rebase_conflict, resume_to
   CLOSING)` naming the files.
5. **Re-run all gates on the rebased tree**; a required failure ⇒ `TRIAGING`.
6. `merge`: main checkout on base, `status --porcelain --untracked-files=no` empty, base
   sha unchanged (else retry from 3, max 2, then `PARKED(base_moving)`); `git -C root merge
   --ff-only wt/<id>`; `post_cmds`; optional push.
7. `pr`: push branch, `gh pr create --body-file` (Decision section, AC checklist, gate
   table, campaign stats, footer); `gh` failure ⇒ `PARKED(gh_failed)`.
8. Cleanup worktree / branch per config. Idea states are left to `wt spec reconcile --apply`,
   run by `/wt-conduct`.

### Trust boundary

Default implementer permission mode is `bypassPermissions` with `--add-dir` limited to the
campaign worktree; the worktree plus branch is the blast radius and the merge step stays
gated. A project may switch to an allowlist (`dontAsk` + `agents.allowed_tools`). Read-only
personas run `--restricted`.

## Breakdown / sub-specs

Wave 1 (this epic's children; each becomes a spec before it is built):

- [ ] Per-project config `.wt/project.yaml` and `wt projects config|init` (project: Meta-Tools) — strict loader with defaults, resolution through the project registry, dry-run `init`.
- [ ] Workflow profiles, campaign store, ledger, locks and `wt campaign list|show|log|note` (project: Meta-Tools) — `agentic:` config with code-held builtins, atomic `state.json` + append-only `ledger.jsonl`, per-campaign flock, singleton runner lease.
- [ ] Git worktree and branch operations for campaigns (project: Meta-Tools) — `gitops.py`: worktree add/remove, commit, rebase with conflict report, ff-only merge, fetch/push, `gh pr create` wrapper.
- [ ] Deterministic gate executor and `wt queue gates` (project: Meta-Tools) — run every configured gate in the worktree with timeout, per-gate logs, required/optional blocking.
- [ ] Headless persona runner, prompt assembly and verdict schemas (project: Meta-Tools) — `claude -p` with structured output, budget, timeout and permission mode; implementer and gate-triager personas; scripted `FakeRunner`.
- [ ] Close step: rebase, re-gate, ff-only merge or PR hand-off (project: Meta-Tools) — spec DoD audit in the worktree, main-checkout preconditions, PR body generator.
- [ ] Queue state machine with lease-guarded tick/run, park/resume and listing (project: Meta-Tools) — pure `plan_step`, admission by slots, `wt queue add|list|watch|tick|run|park|resume|abandon`, offline end-to-end tests.
- [ ] `/wt-conduct` skill, docs and dogfood bootstrap for wt itself (project: Meta-Tools) — conductor skill, committed `.wt/project.yaml`, capability page, first real campaign on this machine.

Later waves (seeded as ideas; specs follow once the loop runs):

- [ ] Canary: `wt spec status SPEC --set STATUS` one-line frontmatter setter (project: Meta-Tools) — the first child the loop builds by itself.
- [ ] HANDED_OFF sweep via `gh pr view` (project: Meta-Tools) — mark handed-off campaigns merged when the PR merges.
- [ ] Gate ladder v2 (project: Meta-Tools) — per-gate retries, warn and escalate rungs, flaky tracking across campaigns.
- [ ] Conformance reviewer persona and `REVIEWING` state (project: Meta-Tools) — does the implementation satisfy the spec; major/minor findings.
- [ ] Test reviewer persona (project: Meta-Tools) — do the tests cover the spec's cases.
- [ ] Adversarial reviewer persona (project: Meta-Tools) — security, logic and bug hunting.
- [ ] Review triage with numeric score, must-fix classification, waivers and follow-up idea filing (project: Meta-Tools) — decides fix vs forward.
- [ ] Spec-writer phase: IDEA to SPEC_WRITING headlessly (project: Meta-Tools) — explore and new-work procedure as a persona.
- [ ] Spec reviewer with sizing and tone to profile selection (project: Meta-Tools) — xs–xl sizing, split oversized specs.
- [ ] Rebase-conflict resolver persona (project: Meta-Tools) — replaces the human-only park on `rebase_conflict`.
- [ ] Mailbox between conductor and steward (project: Meta-Tools) — deterministic file-based inbox/outbox under the data dir.
- [ ] `/wt-steward` skill (project: Meta-Tools) — project session that talks to the conductor.
- [ ] TUI watch tab over `wt.queue.v1` (project: Meta-Tools) — follows the hub tab pattern.
- [ ] Outbound projects end to end (project: Meta-Tools) — portable file in the target repo plus `pull-status` on merge.
- [ ] Exploratory campaigns with hypotheses and evidence events (project: Meta-Tools) — `wt campaign open --kind explore`.
- [ ] `wt queue run --detach` and supervision (project: Meta-Tools) — if the nohup launcher proves inadequate.
- [ ] Idea linkage: `:CAMPAIGN:` extension and a hub agentic slice (project: Meta-Tools) — campaigns visible from ideas and the hub.

Sequencing / dependencies: {config, store, gitops} are independent and land first, in
parallel; {gates, runner} need config and store; close needs gitops and gates; the queue
needs all of them; the conductor skill lands last. Dogfooding starts after the conductor
skill merges: the canary is the first item the loop builds itself.

## Acceptance criteria

- [ ] An accepted internal spec queued with `wt queue add --yes` reaches `MERGED` on this
      machine with no human action, gates green on the rebased tree, the live `wt` touched
      only by the final fast-forward merge.
- [ ] Every transition, persona run (with cost), gate run, finding, park and close of that
      campaign is in its ledger and visible through `wt campaign log`.
- [ ] Loop exhaustion, cost cap, rebase conflict and a dirty main checkout each park the
      campaign with the named reason, and `wt queue resume` continues it.
- [ ] The whole loop runs offline in the test suite with the fake runner, script gates,
      temporary git repos and a fake `gh`.
- [ ] `wt queue list --json` (`wt.queue.v1`) shows in-flight items, state, loops, cost and
      lease health; `wt queue watch` renders it live.
- [ ] wt's own `.wt/project.yaml` is committed and resolves through `wt projects config`.
- [ ] `/wt-conduct` is installed with the other skills and documented in the agent surface
      guide; the capability page is in the docs nav.
- [ ] All wave-1 children `done`; later-wave bullets exist as `INCUBATE` ideas linked to this
      epic.

## Test plan

- **Integration/e2e tests:** `tests/test_agentic_e2e.py` (child: queue): temporary repo with
  `.wt/project.yaml`, script gates and an accepted spec → `wt queue add --yes` → `wt queue
  run --until-idle` with the fake runner → `MERGED`, main contains the change, worktree
  removed, ledger has `open, transition×n, run.start/end, commit, gate×2, cost, close`.
  Park variants: never-green gate ⇒ `loop_exhausted`; dirty main ⇒ `main_dirty` then
  `resume` ⇒ `MERGED`; cap 0.01 ⇒ `cost_cap`; conflicting base ⇒ `rebase_conflict`.
  Both an internal (`wt_checkout` pointed at the temp repo) and an outbound
  (`outbox_targets`) project.
- **Repo gates on every merge:** `uv run pytest`, `python3 tools/spec_lint.py`, the docs,
  skills and completion guards (`mkdocs build --strict` runs inside pytest).
- **Manual verification (child: conductor):** on this machine, `/wt-conduct` starts
  `wt queue run` detached; a trivial accepted spec reaches `MERGED`; campaign id, loop
  count and cost recorded in that child's spec.

## Rollout / sequencing

1. Author all wave-1 children to `accepted` (this epic's Breakdown seeds their ideas).
2. Build config, store and gitops in parallel worktrees; merge each to `main` when its
   gates are green. Then gates and runner; then close; then queue; then the conductor skill
   with the committed `.wt/project.yaml`.
3. Partial delivery behaves as plain library code plus read-only commands: nothing runs
   unattended until the queue child lands, and nothing starts by itself until `/wt-conduct`
   is invoked.
4. First dogfood campaign: the canary setter. Reviews and the spec phase follow once a few
   merges have been watched.

## Definition of done

- [ ] All child specs `done` or `superseded` (the linter gates this).
- [ ] Integration test plan executed; `uv run pytest` green.
- [ ] Acceptance criteria met; epic body reflects what shipped.
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

_None — decisions locked above; later-wave questions belong to their own specs._
