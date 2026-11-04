# Agentic loop

A deterministic queue moves **accepted specs** through implementation, gates, triage and
merge without a human at the keyboard. Judgment steps (implement, triage a gate failure)
run as fresh headless Claude or Cursor personas with a budget and a timeout; every transition, run,
gate result, finding and dollar lands in a per-campaign ledger. Humans see an item only
when the loop has **parked** it. Governing epic: SPEC-0169; the sections below are filled
in by its child specs as they land.

`nw doctor` (SPEC-0349) checks the agentic setup read-only: launch-model prices, the agentic
and mailbox dirs, the runner lease, the conductor session, the worker registry and each
project's `.nw/project.yaml` and profiles. When any live resolution is `cursor`, it also
probes `cursor-agent --version` and login (`status` / `whoami`, not a paid `-p`).

## Project configuration

`<repo>/.nw/project.yaml` is committed with the project and declares what the loop needs
to know about it (SPEC-0170):

- `gates`: ordered shell commands (`name`, `cmd`, `cwd`, `timeout_s`, `required`) run
  inside the campaign worktree; `required` gates block, optional ones only report.
- `merge`: `policy` (`merge` | `pr` | `draft-pr`), `base`, `remote`, `push_after_merge`,
  `delete_branch`, `post_cmds`, `pr.labels` / `reviewers` / `title_prefix`.
- `worktrees`: `root` (default `<repo>/../.nw-worktrees/<project>`), `setup` commands,
  `cleanup` (`on_merge` | `on_close` | `never`).
- `agents`: `context_files` read first by every persona, `permission_mode`,
  `allowed_tools`, `commit_trailer`, optional `runner` (`claude` | `cursor`).

Unknown keys fail; every problem is reported by key path. Gate commands may use the
placeholders `{spec_id}`, `{campaign}`, `{worktree}`, `{base}`.

nightwright finds the file through the project registry (`nw projects --json`): an outbound
project's `repo_path`, or this nightwright checkout for `Meta-Tools`. Never from the current
directory alone.

```bash
nw projects config Meta-Tools          # resolved document, defaults filled
nw projects config --json              # nw.project.config.v1 (project from cwd)
nw projects init acme                  # print a starter; --yes writes it, --force overwrites
```

## Campaigns and the ledger

A campaign is one spec's trip through the loop. Each lives under
`<data_dir>/agentic/campaigns/<id>/` as an atomically written `state.json` (schema
`nw.campaign.v1`: state, counters, cost, last gate, park reason) plus an append-only
`ledger.jsonl` with one event per line (`open`, `transition`, `run.start`/`run.end`,
`commit`, `gate`, `finding`, `cost`, `note`, `park`, `resume`, `close`, plus the backlog
`backlog` and `priority` events of SPEC-0180), each carrying a
monotonic `seq`. Writers hold the campaign's `flock`, so the loop and a human command can
touch the same campaign safely. Campaign ids are `<SPEC-ID>-<YYYYMMDD>-<hex>`.

```bash
nw campaign list                 # open campaigns (--all for terminal ones)
nw campaign show CAMPAIGN        # state + last events (--json → nw.campaign.v1)
nw campaign log CAMPAIGN --tail 20   # the ledger (--json → nw.campaign.log.v1)
nw campaign note CAMPAIGN "text" # append a note; inside a persona run $NW_CAMPAIGN is set
```

A single `nw queue run` holds the runner lease (`queue.lock` + `queue.lease.json` with a
heartbeat); a second runner is refused and names the holder. A heartbeat older than
`agentic.lease_ttl_s` is reported as `stale`, never taken over (SPEC-0171).

### Outbound projects

An outbound spec (`ACME-0001`, authored in the outbox) is queued like any other once it has
been exported: `nw spec export ACME-0001`, then `nw queue add ACME-0001`. When its campaign
reaches `MERGED` the close step runs `nw spec pull-status` for it: the outbox copy takes the
merged file's `status` and the linked `EXPORTED` idea becomes `SHIPPED`, with an
`outbound_pulled` event in the ledger. A pull failure is a minor `bookkeeping` finding and
never undoes the merge. A `HANDED_OFF` campaign pulls nothing; the PR sweep (`nw queue
sweep-prs`) runs the pull when it records the merge, which needs the target checkout to have
pulled the merged file (otherwise a finding says to pull and rerun `nw spec pull-status`).

## Profiles

Models, effort, budgets, loop limits and slots are workflow profiles (SPEC-0171, SPEC-0237).
Builtin profiles `balanced` (default: Sonnet implementer and triager, $10 cap, 3 loops,
2 slots), `frugal` and `thorough` ship as YAML files in the package
(`src/nightwright/agentic/profile_defs/<name>.yaml`).

### Size x tone matrix (SPEC-0219)

When you do not pass `--profile`, `nw queue add` picks the campaign profile from the spec's
approving (or revising) review, which records a **size** (`xs`, `s`, `m`, `l`; `xl` is too big
for one campaign and has no profile: split it) and a **tone** (`cost-effective`, `high-end`,
`critical`). Precedence: `--profile` flag, then the matrix, then `agentic.default_profile`.
The builtin matrix:

| Tone | xs | s | m | l |
|------|----|---|---|---|
| `cost-effective` | `frugal` | `frugal` | `balanced` | `balanced` |
| `high-end` | `balanced` | `balanced` | `balanced` | `thorough` |
| `critical` | `thorough` | `thorough` | `thorough` | `thorough` |

Override cells under `agentic.profile_matrix` in `config.yaml` (deep-merged over the builtin;
an unknown tone, size or profile, or an `xl` cell, is an error):

```yaml
agentic:
  profile_matrix:
    cost-effective: {l: thorough}
```

`nw profiles list|show|check` shows what each profile resolves to and which file set each value.

### Profile files and layers

Four layers define profiles; a higher layer wins, key by key:

1. builtin files (package data);
2. user files, `<config_dir>/profiles/*.yaml`;
3. the `agentic.profiles` deltas in `config.yaml`;
4. project files, `<repo>/.nw/profiles/*.yaml`, read from the project's **main checkout** (a
   campaign cannot change its own profile by editing its branch). `slots` are a property of the
   whole queue, so they ignore the project layer.

The profile name is the file stem (`^[a-z][a-z0-9_-]*$`; a `*.yml` file is an error). A file
is either complete, or a delta: it extends the same name in a lower layer (`extends: <own
name>`, or simply the same stem), or another profile (`extends: balanced`). A `config.yaml`-only
new name merges over `balanced`, as before. Files are re-read on every tick, so an edit applies
without restarting the runner.

```yaml
# <repo>/.nw/profiles/careful.yaml
schema: nw.profile.v1            # required in files
extends: balanced                # optional; a delta over the effective `balanced`
description: "opus implements, one more loop"
loop_max: 4
campaign_cap_usd: 20.0           # number > 0, or null for no cap
personas:
  implementer: {model: opus, effort: high, max_budget_usd: 8.0}
```

Keys: `slots`, `loop_max`, `run_timeout_s` (integers >= 1), `campaign_cap_usd`, `reviewers`, the
`review:` block (below) and `personas.<name>` with `model` (`opus|sonnet|haiku` or a
`claude-*` id; Cursor maps aliases via `cursor_map.yaml`), `effort`
(`low|medium|high|xhigh|max`), `max_budget_usd`, `permission_mode`
(`default|acceptEdits|bypassPermissions|dontAsk|plan`) and `read_only`. Every source is
validated: an unknown key, persona, model, effort or permission mode, or a wrong type, fails
with `<file>: <key.path>: <problem>`, and only the profile that file defines breaks. A campaign
of that profile parks `open_failed` with that message; `nw queue add` and `nw spec review` fail
with it. The resolved profile lists its `sources`, lowest layer first.

The `config.yaml` block holds the same deltas:

```yaml
agentic:
  default_profile: balanced
  runner: claude                 # or cursor (SPEC-0436); default stays claude
  lease_ttl_s: 120
  profiles:
    balanced:
      personas:
        implementer: {max_budget_usd: 6.0}
```

## Gates

Gates are the project's own proof commands from `.nw/project.yaml` — tests, lint, anything
with an exit code (SPEC-0173). The loop runs them in the campaign worktree, **in order and
all of them every time**, so triage and humans see every failure at once rather than the
first one. Each gate has a `timeout_s`; a timeout kills the gate's whole process group.
A campaign branch may raise a gate's `timeout_s` in its own `.nw/project.yaml` (up to the
nightwright-level `gate_budget_s`), never lower it, and the raise is recorded as a `gate-limits` finding;
nothing else in a gate is read from the branch (SPEC-0316).
`required: true` gates block the campaign; optional gates are recorded but never block.

A gate may add `paths:`, a list of globs (SPEC-0350). At the close re-gate the gate runs only
when the branch changes a path that matches one; otherwise it is skipped, counts as satisfied
and a `gate.skipped` event lands in the campaign ledger. A gate without `paths` always runs, and
when the changed paths cannot be computed every gate runs. Globs are anchored at the repo root:
`*` stays within one path segment, `**` crosses `/`, a trailing `/` means a directory, and a
pattern with no `/` matches that name at any depth. When unsure whether a test reads a file,
list it under the full-suite gate.

A loop-tier pytest gate (`when: loop`, `results: {format: pytest}`) may set `failed_first: true`
(SPEC-0358, default off). A round after a red round then starts by running the failing ids of the
previous result *for that gate name* (pytest `--lf` semantics driven by the ledger), through the
gate's `retry_cmd` (else the expanded `cmd` followed by the ids), in the log
`gates/NNN-<name>.ff.log`. The prelude is attempt kind `failed-first` and runs only when the
previous result was red (not interrupted or timed out) with a complete failing-id list; the close
tier never gets one, and `last_gate` after a close run holds close gates, so no prelude follows it.
Its outcome: **still red** (exit code not 4 or 5) ends the round red with that one attempt, no main
run and no retry ladder; **interrupted** by a stop gives an interrupted result; **green, timed out
or not runnable** (pytest exit 4 or 5) falls through to the normal run and ladder, with the prelude
first in `attempts` and its time in `duration_s`. A gate named `<other>.ff` is rejected (it would
share the prelude's log).

### Close-tier reuse and the sentinel (SPEC-0359)

Every gate sees `NW_GATE_REPORT` (`gates/NNN-<name>.report.json`); a gate that writes a JSON
selection report there (`tools/affected_tests.py` does) carries it as `report` on its result
(dropped at 64 KiB or more). Gate payloads and `last_gate` record the worktree `head` and `tree`
the run gated, and the campaign keeps `last_green_loop`, the last green loop-tier payload (a red
loop run clears it).

A close gate may set `reuse_from: <loop gate>` (validated: the gate is `when: close`, the name a
`when: loop` gate). At the close it is then skipped, and the loop gate's green result stands for
it, when the rebase is clean by rules R1-R7: **R1** reuse is enabled (`merge.policy: merge`, not
a merge request, no `close.force_full`, no `NW_CLOSE_FULL=1` in the runner's environment, no red
or overdue sentinel, a readable `gate_reuse.json`); **R2** the green source result has a report
whose `head_sha` is the gated head `G`, and `G` is still a commit, and no full close run of the
gate was red in this campaign since its last green full run (the campaign state's `close_red`;
so a triage `retry_gate` whose loop gate goes green again does not skip the suite that just
failed); **R3** no file the branch
changes differs between `G` and the rebased head `H`; **R4** main's delta `D` (`git diff G H`)
touches no `.nw/**` file and does not select the full suite, and neither `D` nor the branch
changes the reuse tooling (`tools/affected_tests.py`, or any `tools/*.py` the close gate or its
`reuse_from` gate runs), so a branch is never judged by its own edited selector; **R5** the branch's selection
recomputed on `H` did not grow; **R6** the forward static closure of the branch's tests contains
no path in `D`, and that of `D`'s tests no branch path; **R7** `G` was gated less than
`close.reuse_max_age_s` (86400) ago and `D` has at most `close.reuse_max_delta` (200) paths. Any
error or timeout of `git` or `affected_tests.py --json [--closure]` declines with the rule being
evaluated. A reuse appends `gate.reused` (with its evidence), a decline `gate.reuse_declined`
(the first failing rule) and the gate runs. `close.force_full: true` in `.nw/project.yaml`, or
`NW_CLOSE_FULL=1`, turns reuse off.

A reused close that **merges** is counted in `<agentic>/projects/<project>/gate_reuse.json`
(`reuses_since_green`, `suspects`); nothing else moves the counter. The **sentinel** is due after
`sentinel.after_reuses` (3) such merges, or one and no green sentinel within
`sentinel.max_age_s` (86400 s). When it is due and no campaign waits for a gate slot, the queue
runner runs the project's close gates (no `paths` skip, no reuse) on the base HEAD in a detached
worktree `<agentic>/sentinel/<project>`, under a normal gate slot. Green settles the suspects the
tested sha contains; red sets `force_full_close` (every close of the project runs full), names
those suspects in a `sentinel` queue event and opens an escalation. Reuse also declines while
the sentinel is overdue (due for longer than `max_age_s`, or disabled with `after_reuses`
reuses pending). A missing state file is rebuilt from the queue events (a red last sentinel
keeps `force_full_close`); an unreadable one declines reuse until a sentinel run rewrites it.

Runs of one project are serialized by `<agentic>/sentinel/<project>.lock` (a non-blocking lock
held for the worktree reset, the gates and the settle); the runner skips a locked project. Each
finished run is noted in `<project>.last.json`, and the runner does not rerun the sentinel at the
base sha the last run tested when that run was green or its red was cleared, nor a red or failed
run there within an hour; a moved base always runs. A red run at the sha the previous red
`sentinel` event already reported does not escalate again. A green run settles only suspects
that are its tested sha or an ancestor of it; one git cannot answer for (an unknown sha, a git
error) stays a suspect, and one git knows but that is no longer on the base branch (a rewritten
base) stays too and is named as `orphans` on the event. A git error (an unresolvable
`merge.base`) is journaled as `sentinel_failed` and never stops the runner. The worktree's
`worktrees.setup` is rerun until it has succeeded once (`<project>.setup.json`), and again when
the setup commands change.

**Affected-only close and the clock sentinel (SPEC-0378/0379).** nightwright's own close-tier
`pytest` gate runs `tools/affected_tests.py ... --run uv run pytest`, the tests the branch affects
against `merge-base($NW_BASE, HEAD)`, never the bare full suite; its fallbacks are narrowed
(nested conftest, metadata-only `pyproject.toml`, own-version `uv.lock` bump, removed modules,
hub-only modules, unmapped files and the old 40% cap no longer force it) and `close.force_full` /
`NW_CLOSE_FULL` only turn reuse off. The full suite runs on the sentinel: `affected_tests.py`
selects everything under `NW_SENTINEL` (local run), an `NW_CAMPAIGN` starting `sentinel-` (the
worker run) or `NW_AFFECTED_FULL=1`. Besides the reuse triggers the runner starts a project's
sentinel on the clock when it has close gates, its base moved since the last green sentinel and
that run is older than `agentic.sentinel.interval_hours` (nw config, default 6; 0, `false` or
null turns the clock off). The run takes a worker-box gate slot while a worker is placeable
(SPEC-0374, never local overflow then) and runs there through `remote_gates.run_detached`; a
worker transport failure is a failed run (retried after the hour or when the base moves), not a
local fallback.

- `nw queue sentinel run --project P` runs the sentinel now (waits for a gate slot; exits 1 when
  red or when another run of `P` holds the lock; `--json` emits `nw.queue.sentinel.v1`).
- `nw queue sentinel clear --project P` clears `force_full_close` and `due_since`; the suspects
  stay, and the runner does not rerun the cleared red sha.

Every gate writes one log under the campaign directory, `gates/NNN-<name>.log`, starting
with a header line that names the command and its working directory; the last 60 lines
travel with the result into the ledger (`gate` event) and into `nw campaign show`. Gate
commands see `NW_CAMPAIGN`, `NW_SPEC_ID`, `NW_WORKTREE`, `NW_BASE`, `NW_DATA_DIR` and
`NW_CONFIG_DIR`, and may use `{spec_id}`, `{campaign}`, `{worktree}`, `{base}` placeholders.

`nw queue gates CAMPAIGN` runs the gates on a campaign's worktree right now — for example
on a parked item — and logs a `gate` event. It never changes the campaign's state or its
`last_gate`, so a debug run cannot steer the queue. `--json` emits `nw.campaign.gate.v1`.

## Personas

Each judgment step is one fresh headless run (SPEC-0174, SPEC-0436): a persona markdown
template (`implementer`, `gate_triager`, `conformance_reviewer`) is filled with the campaign's
context — the spec text as it is on the branch, the worktree path, the last gate logs, the
brief from the previous loop — and ends with a fixed invariant block (work only in the
worktree, do not commit, finish with the JSON verdict). Default is `claude -p` with
`--json-schema`. Opt in to `cursor-agent -p` (logged in with `cursor-agent login`; no
`CURSOR_API_KEY`); Cursor has no schema flag, so the same JSON object is asked for in a
prompt footer. Both get a `--max-budget-usd` (Cursor reports no USD, so the queue estimates
from the cap), a wall-clock timeout, the profile's model and effort, and an explicit
permission mode (`bypassPermissions` scoped to the worktree for the implementer;
`--restricted` / Cursor `--mode ask` for read-only personas). Cost, usage and session id
come back in the result and go to the ledger. Identical inputs give a byte-identical prompt;
`nw campaign prompt CAMPAIGN PERSONA` prints it without running anything.

For tests and dry runs, `agentic.runner: fake` (or `NW_AGENTIC_RUNNER=fake`) replays a JSON
script of scripted edits and canned verdicts instead of calling `claude` or `cursor-agent`.

### Backend (Claude or Cursor)

Default is `claude`. Cursor is opt-in. A queue process may mix backends. Resolution is per
spawn (SPEC-0436), highest first:

1. `NW_AGENTIC_RUNNER` (`claude` / `cursor` / `fake`; `fake` is valid only here)
2. campaign pin `state.runner`
3. `agentic.project_runners[project]`
4. `.nw/project.yaml` `agents.runner`
5. host `agentic.runner` in user `config.yaml`
6. `claude`

An in-flight persona finishes on the backend it started; the next spawn uses the new
resolution. Nothing is SIGTERM'd.

```bash
nw queue add SPEC-0170 --runner cursor --yes     # pin this campaign
nw queue backend CAMPAIGN cursor --yes           # pin or clear (`default`) a live campaign
nw queue backend --host cursor --yes             # write user agentic.runner (loop reloads)
nw queue backend --all cursor --yes              # pin every non-terminal campaign
nw queue backend --host --all cursor --yes       # full in-flight switch
```

`--host` and `--all` take the backend as the only argument (`nw queue backend --host cursor`).
`conductor-launch --runner cursor` also writes the host default; `steward-launch --runner`
does not (SPEC-0438, SPEC-0439).

Profile `model: opus|sonnet|haiku` is unchanged. On a Cursor spawn those aliases map through
YAML (`src/nightwright/agentic/cursor_map.yaml`, overlay
`<config_dir>/cursor_map.yaml`, `agentic.cursor` in config, `<project>/.nw/cursor_map.yaml`).
A persona may set `cursor_model:` in a user/project profile to pin a Cursor id.

### Declared personas (SPEC-0371, SPEC-0394)

A project can declare its own personas in its repo (a crypto repo a security reviewer, a UI repo
an accessibility reviewer) without patching nightwright: one `nw.persona.v1` YAML file per
persona in `<main checkout>/.nw/personas/` (or, for every project, in the user config dir's
`personas/`), with its markdown template next to it.

```yaml
# <project main checkout>/.nw/personas/security_reviewer.yaml
schema: nw.persona.v1
name: security_reviewer
role: reviewer
template: security_reviewer.md
description: "crypto misuse, secrets, injection"
category: [key_handling, timing, randomness]
model: opus                 # opus|sonnet|haiku or a claude-* model id
effort: high                # low|medium|high|xhigh|max
max_budget_usd: 2.0         # a finite number > 0
required: true              # reviewer only; default true (blocking vs advisory)
when:                       # reviewer only; omitted or {} means always
  paths: ["src/crypto/**", "**/*.rs"]
  labels: [security, crypto]
```

The role, not the file, fixes the output schema and the permissions:

| role | output schema | read-only | template placeholders |
|---|---|---|---|
| `implementer` | the implementer verdict | no (the project's `agents.permission_mode` / allowlist) | the implementer's |
| `reviewer` | the shared findings schema, `category` = the file's `category` list plus `other` | yes (`dontAsk`) | the reviewers' (diff, test files, changed files, ...) |
| `triager` | the gate triager verdict | yes (`dontAsk`) | the gate triager's |

Keys beyond the registry's common ones (`schema`, `name`, `role`, `template`, `description`,
`category`):

- `model`, `effort`, `max_budget_usd`: the persona's own run defaults. A run request layers
  nightwright defaults (`sonnet`, `medium`, $1.00) < the profile's entry for the role's builtin
  (`conformance_reviewer`, `implementer`, `gate_triager`) < these keys < an explicit profile
  `personas.<name>` entry. A read-only role is always `read_only: true` / `dontAsk`.
- `required` (bool) and `when` (`paths` and/or `labels`, each a non-empty list of non-empty
  strings; both given means both must match): reviewer-role routing metadata for the review set.
- `read_only`, `permission_mode`, `output_schema` and `invariant` are refused; so are a builtin's
  name and the new keys in a builtin definition. A reviewer's prompt gets a generated `## Output`
  section (verdict, score, findings with its category list, fix brief), so the template carries
  only the review lens.

`nw personas list [--project P] [--json]` lists every definition (builtin, user, project) with
its origin; `nw personas show NAME` adds the run settings, `required`, `when` and the template's
sha256; `nw personas validate [--project P]` reports every problem of every declared file in one
run and exits 1 on any. Definitions are read only from the project's main checkout, never from a
campaign worktree.

**Declared personas are validated and listed, but the queue does not run them yet.** Profiles
cannot name them in `reviewers:` and no campaign reaches them until Pipeline A2b (profiles
referencing declared personas) and A3 (the review set, `when` filtering, required vs advisory)
ship.

## Reviews

Green gates say the build works, not that the change does what the spec says. After every
green gate run the campaign enters **REVIEWING** (SPEC-0187) and a fresh, read-only
`conformance_reviewer` (`--restricted`; Opus in `balanced` and `thorough`, Sonnet in
`frugal`; about $1.50) gets the spec as it is on the branch, the diff against the base
(at most 1500 lines), the test files touched under `tests/`, and the gate summary. For every
acceptance criterion it decides `met` with concrete evidence, flags criteria "met" only by a
trivially passing test as `major` and scope creep as `minor`, and never edits anything. Its
verdict is `{verdict: pass|fix, score, findings, ac_assessment, fix_brief}`:

- the verdicts are triaged by a deterministic score (SPEC-0203, below): `pass` ⇒ CLOSING;
- `fix` ⇒ `loops += 1` and back to IMPLEMENTING with the `fix_brief` and the blocking
  findings as the next implementer's brief (`loop_max` reached ⇒ `loop_exhausted`);
- a malformed verdict is retried once per loop, then the campaign parks as `review_escalated`.

Each finding is a `finding` ledger event (`persona: conformance_reviewer`); the score is in
the `run.end` verdict and in `last_review` of the campaign state. The cost cap applies before
the spawn like for any persona. A close that bounces back to triage and ends in a new
implementer run is reviewed again.

### Test reviewer (SPEC-0205)

The second reviewer asks the question the conformance reviewer is not tasked with: **would the
tests catch a regression?** The read-only `test_reviewer` (`--restricted`; Opus in `balanced` and
`thorough`, Sonnet in `frugal` where it is opt-in via `reviewers`; about $1.00) gets the same
context plus `tests_touched`, computed by code from git rather than by the model: per test file
the `changed` test names (`Class::name` for methods) and the `removed` ones. It never runs the
suite or edits anything. For every acceptance criterion it names the test that would fail if the
criterion broke — none is `must_fix`, one that would still pass with the behavior removed is
`major` — then flags untested edge and failure cases (`major` when behavior-bearing, `minor`
when cosmetic). A removed test or a visibly weakened assertion without a reason in the spec is
`must_fix`. Its verdict is `{verdict, score, findings, coverage_assessment, fix_brief}`;
findings are `finding` events with `persona: test_reviewer` and `ac`, and `coverage_assessment`
is kept in the run record and in `last_review.verdicts`. At defaults it adds $1.00 to every
review pass; to get the old cost set `reviewers: [conformance_reviewer]` in the profile.

### Shipped reviewers

- `conformance_reviewer` (SPEC-0187): judges the change against the spec's acceptance criteria.
- `adversarial_reviewer` (SPEC-0202): attacks the code itself, by reading only. It assumes the
  change is wrong, asks per hunk what input, ordering, timing or environment breaks it, and
  checks shell strings built from input, `shell=True`, unvalidated paths, secrets in logs,
  unlocked read-modify-write, swallowed exceptions, non-atomic writes and unguarded
  destructive git or file operations. It gets the spec (for intent), the diff, the sorted list
  of changed files (first 300, then `… N more`), the gate summary and earlier verdicts. Each
  finding carries a closed `category` — `bug`, `logic`, `security`, `injection`,
  `path_traversal`, `subprocess`, `secrets`, `concurrency`, `error_handling`, `data_loss`,
  `resource_leak` or `other` — plus a concrete `trigger` (a finding without one is at most
  `minor`, by prompt rule). `must_fix` is an exploitable flaw, data loss or a certain crash on
  the main path; `major` a plausible defect on a realistic path. Its verdict is
  `{verdict, score, findings, fix_brief}` with `score` = confidence the change is safe to
  merge; a missing or unknown `category` makes the verdict malformed (one retry, then
  `review_escalated`). Its `finding` events carry `category` with the trigger folded into
  `detail`, and the implementer's brief shows `- MUST FIX [category]: title (files) — detail`.
  Budget: Opus/high $1.50 in `balanced`, $2.50 in `thorough`; `frugal` has settings (Sonnet,
  $0.60) for opting in through `reviewers`.

### Several reviewers (SPEC-0201)

A profile's `reviewers` key lists the reviewer personas of REVIEWING (`balanced` and `thorough`
list `conformance_reviewer`, `test_reviewer` and `adversarial_reviewer`, `frugal` only the
conformance reviewer; a user delta replaces the list wholesale; an absent or empty key
means the conformance reviewer). Each name must be a read-only reviewer persona with settings in
the profile. REVIEWING runs all of them **in parallel** — each a fresh run with its own budget,
run directory (`runs/NNN-<persona>/`) and ledger events, none of which sees the runs of the
current review pass — and waits for every verdict before deciding. The cost cap is checked
before any spawn against the **sum** of the pending reviewers' budgets (`cost_cap` park, no
partial fan-out).

A reviewer missing a valid verdict (malformed, timed out, crashed) is retried once, alone —
valid siblings are kept and never re-run — and nothing is routed until every verdict is in; a
reviewer still missing after its retry parks the campaign as `review_escalated`, naming it.
Entering REVIEWING always starts a fresh review (verdicts are cleared).

### Triage: score and routing (SPEC-0203)

Once every verdict is in, a pure function (`triage_reviews`, no LLM) decides. Findings get ids
`<persona>#<index>`; identical findings (same files and normalized title) from several
reviewers are de-duplicated and counted once. Then, in order:

1. any `must_fix` ⇒ `fix`;
2. a reviewer's `fix` verdict blocks when it is backed by a `major` finding of its own, or when
   it has no findings at all (a synthetic `major`, `<persona>#fix`); a `fix` over only
   `minor` / `nit` findings does not block by itself;
3. every `ac_assessment` entry with `met: false` is a hard stop (a synthetic `major`
   `<persona>#ac<i>` unless the reviewer's own `major` / `must_fix` finding covers that AC);
4. otherwise `score = max(0, 100 − Σ weight)` over the findings (default weights `must_fix` 100,
   `major` 15, `minor` 4, `nit` 1) must reach the profile's `pass_score`.

A `pass` verdict carrying a real `major` is respected: it costs 15 points and is `advisory`, and
the queue writes a `pass over major: <id> <title>` note. Every triage writes a
`review score <n>/<pass_score> <verdict>` note.

`pass` ⇒ CLOSING; `fix` ⇒ IMPLEMENTING with one merged brief (`source: review`; `personas`;
each blocking reviewer's `fix_brief` prefixed `[persona]`; when only the score failed, the line
`Review score <n> is below the pass score <m>; …`; one bullet per blocking finding, and the
`must_fix` entries carry `id`, `severity` and `personas`). With one reviewer the brief is the
reviewer's own, as before.

Profile keys (the `review:` block, validated by `resolve_profile`; any other key is rejected):

```yaml
agentic:
  profiles:
    balanced:
      review:
        pass_score: 80          # int 0..100; thorough 90, frugal 70
        weights: {must_fix: 100, major: 15, minor: 4, nit: 1}   # a partial delta merges
        triager: true           # bool (SPEC-0206); true in balanced / thorough, false in frugal
        followups: true         # file non-blocking findings as ideas at close (SPEC-0207)
        followups_nits: false   # also file `nit` findings
```

`last_review` in the campaign state is
`{loop, entry_run, run, runs, verdicts, verdict, score, findings, must_fix, waived, advisory}`:
`verdicts` holds one entry per reviewer (`verdict`, `score`, `fix_brief`, the full `findings`,
`ac_assessment` for the conformance reviewer, `coverage_assessment` for the test reviewer, `run`,
`loop`), `runs` the run id per reviewer,
`run` the last reviewer's run. Written once every verdict is in: `verdict` (the triage outcome,
`pass|fix`), `score` (the weighted triage score; each reviewer's own score stays in
`verdicts[*].score`), `findings` / `must_fix` (counts of the surviving findings), `waived`
(the findings the review triager waived, each with its `reason`; empty otherwise) and `advisory` (the non-blocking findings of the current
review: `{id, personas, severity, title, detail, files, ac, category}`).
`counters.review_retries_by_persona` appears only after the first retry. `nw campaign show`
prints a `review:` line (score, verdict, advisory count and how many are `major`) and
`nw queue list --json` rows carry `review_score`.

### Review triager (SPEC-0206)

One case in the score is a judgment call: a `fix` caused **only** by the score (no `must_fix`, no
surviving `fix` verdict, no synthetic `major`) because several `minor` / `nit` findings add up
below `pass_score`. When waiving *every* waivable finding could lift the score to `pass_score`
(the pure `needs_triager`), the campaign stays in REVIEWING and runs the small read-only
`review_triager` persona (`--restricted`; Sonnet/medium, $0.50 in `balanced` and `thorough`;
Haiku, $0.25 in `frugal`, where `review.triager` is off by default). It gets the spec and a
`triage_input` JSON (score, pass score, the reviewers' verdicts and the waivable findings by id)
and answers `{waive: [{id, reason}], note}`: it may waive findings that are subjective, out of
scope or not defects, and nothing else. Code validates the answer — only surviving `minor` /
`nit` ids with a non-empty reason count; others are ignored with a `triager ignored <id>: <why>`
note — records each accepted waiver as a `waived <id>: <reason>` note and in
`last_review.waived`, and re-runs `triage_reviews` with them; the deterministic result decides
(`pass` ⇒ CLOSING, still `fix` ⇒ IMPLEMENTING). It never touches `must_fix`, `major` or synthetic
findings and never flips a verdict by itself.

`last_review.triage` tracks it: absent (not evaluated), `pending` (the triager step runs next;
transition reason `review_triage`), `done` (after the run, valid or not — a malformed, failed or
timed-out triager only adds a note and the deterministic outcome stands; it is never retried and
never causes `review_escalated`) or `skipped` (the cost cap could not fit its budget; a
`triager skipped: cost cap` note). It runs at most once per review; `triage`, `triager_run` and
`waived` clear on every entry into REVIEWING. The score note is written once, after the final
`triage_reviews` run. Set `review.triager: false` to opt out.

### Follow-up ideas (SPEC-0207, grouped by SPEC-0239)

What a change merges with is not forgotten. At the start of the close step a deterministic filer
(`nightwright.agentic.followups`) lists the `major`/`minor` findings (`nit` too with
`review.followups_nits`) that were advisory or waived in **one grouped idea per campaign**
(`kind: improvement`, the campaign's project, the spec's `parent` epic when it has one);
`review.followups: false` files nothing. It never blocks a merge: a failure is a `note` event and
the findings are reported `unfiled`.

- **The grouped idea.** Its heading carries the marker `nwfug:<campaign id>` and the count
  (`nwfug:<cid> [SPEC-NNNN] review follow-ups (<n> findings)`); its Summary is a checklist with one
  line per finding (`- [ ] nwfu:<key12> <severity> persona/slot: title - detail (files: …)`),
  ordered by severity, `advisory` before `waived`, then title. Ticked (`- [X]`) lines still count.
- **Key.** `key = sha1(spec id | normalized title | sorted files)`; the first 12 hex characters
  are the `nwfu:<key12>` token of the checklist line. The key ignores persona and severity, so a
  finding whose severity or reporters change keeps its key; a reworded title or other cited files
  is a new finding (accepted: a spurious follow-up is cheaper than a lost one).
- **Listed once.** A finding already listed in another **open** grouped idea of the same spec
  (including a `legacy-` group) gets no new line; its `followup` event points at that group. A
  closed group does not suppress filing.
- **Cap.** `review.followups_max` (integer >= 0, default 10, `0` = no cap) bounds the lines per
  campaign. The findings past the cap are reported as `capped` (no `followup` event, so a re-run
  with a raised cap can still list them) and an overflow line
  `- +K more findings not listed (nw campaign show <cid>)` ends the Summary.
- **Carry-over.** On every `fix` outcome `_merge_carry` merges the review's advisory and waived
  findings into `state.review_carry` (de-duplicated by key) and drops every key that is blocking
  in that review, so advisory findings of earlier loops are still filed at the close step.
- **Idempotent and crash-safe.** A key with a `followup` ledger event is skipped (and still
  reported). Otherwise the group is looked up by its `nwfug:` marker in the ideas file and its
  archive and **adopted**: the filer writes add_idea, Summary, title, then the events, and a
  re-run appends only the missing lines and recounts the title, so a crash anywhere leaves no
  second idea and no second line.
- **Ledger and reports.** Each listed finding is a `followup` event
  (`persona, severity, title, key, idea_id, origin`), whose `idea_id` is the group. `state.close.followups`
  holds `{filed, unfiled, waived, capped, idea_id}`; `nw campaign show` prints
  `follow-ups IDEA-NNN (<n> listed, <k> capped)`, the follow-up lines (`IDEA-NNN severity
  title`), the `[capped]`, unfiled and waived findings. The close row of `nw.queue.tick.v1`
  carries a `followups` key (`filed`, `unfiled` and `capped` counts, `idea_id`, the distinct
  `ids`, plus the `items`, `unfiled_items` and `waived` lists).
- **Migration.** `nw ideas group-followups [--yes] [--json]` folds the open single `nwfu:` ideas
  that SPEC-0207 used to file into one `nwfug:legacy-<SPEC-NNNN>` idea per spec and closes each
  single as `DROPPED` with `FOLDED_INTO` the group. Without `--yes` it only prints the plan
  (`nw.ideas.group_followups.v1`, `written: false`); a re-run is a no-op.

## Closing

Closing is the step that guards the base branch, and for nightwright itself the live editable install
(SPEC-0175). It is plain Python over the git wrapper and the gate executor, and it never
forces anything:

1. The spec in the worktree must verify (`nw spec verify` checks) **and** be `status: done`;
   otherwise the campaign goes back to triage with a synthetic `spec-dod` gate naming what is
   missing.
2. Anything uncommitted in the worktree is snapshotted into a commit.
3. The worktree is rebased onto the target (the local base for `merge`; the fetched remote
   base for `pr` / `draft-pr`). Declared generated files are regenerated (SPEC-0191). Any
   other conflict dispatches a `conflict_resolver` persona run inside the campaign (SPEC-0221,
   up to 2 per entry into CLOSING, `merge.resolver: false` opts out): it performs the rebase,
   a runner-side check verifies the result (attached branch, no dropped or added commit,
   clean tree) and `rebase_resolved` lists every path and how it was resolved; the close then
   redoes its guard, rebase and re-gate. An unresolved conflict parks the campaign
   (`rebase_conflict`) with the worktree restored byte-for-byte.
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

Idea states are not touched here; `/nw-conduct` runs `nw spec reconcile --apply` after merges.

## Queue

The queue is the engine (SPEC-0176). Python decides every transition; personas only produce
commits and one verdict. One process runs the loop at a time (the lease), two or three
campaigns advance concurrently (the profile's `slots`).

A spec is admitted only with an approving review for its current text (SPEC-0188):
`nw spec review SPEC-0170` runs the read-only `spec_reviewer` persona and stores the verdict in
`docs/specs/reviews/SPEC-0170.json`, keyed by the spec file's sha256 — editing the spec
invalidates it. `nw queue add --force` skips the check and records `review: forced`.

```bash
nw spec review SPEC-0170          # approve | revise | split, with findings
nw spec review SPEC-0170 --show   # read the stored review; runs nothing (SPEC-0242)
nw spec reviews --verdict revise  # the latest review of every spec
nw queue add SPEC-0170            # plan only (dry-run)
nw queue add SPEC-0170 --yes      # create the campaign in QUEUED
nw queue add SPEC-0170 --runner cursor --yes   # pin this campaign to Cursor
nw queue list                     # Active (slots) and Backlog (admission order) tables
nw queue backlog                  # only the backlog: priority, what each item waits on
nw queue add SPEC-0171 --priority 1 --yes   # 1 (first) .. 5; default 3
nw queue prioritize CAMPAIGN 2 --yes        # re-prioritize a QUEUED / PARKED item
nw queue watch                    # the same, live
nw queue run                      # the loop; nw queue tick = one iteration
```

State machine:

```
QUEUED → OPENING → IMPLEMENTING → GATING → REVIEWING → CLOSING → MERGED | HANDED_OFF
                        ▲            │            │ review fix (loops+1)
                        ├────────────┼────────────┘
                        └─ TRIAGING ◀┘ required gate failed:
                           fix (loops+1) · retry_gate (once per loop) ·
                           waive (optional gates only) · escalate
any state → PARKED (humans only) → resume | abandon
```

- **OPENING** creates the worktree and branch and runs `worktrees.setup`.
- **IMPLEMENTING** spawns a fresh implementer; the runner commits whatever changed.
- **GATING** runs every gate; all required green ⇒ REVIEWING, else TRIAGING.
- **TRIAGING** spawns the read-only gate triager only after a failure.
- **REVIEWING** spawns the read-only reviewers in parallel (see Reviews); all pass ⇒ CLOSING.
- **CLOSING** rebases, re-gates, audits the spec's Definition of Done, then fast-forwards
  the base or opens a PR.

### Backlog and admission

`QUEUED` is the inactive backlog (SPEC-0180): items claim a free slot on their own. Each
tick picks, among backlog items that are **ready**, the highest priority (`1` first, default
`3`), oldest first among equals, up to the free slots. Ready means, re-read from the project's
base checkout at admission time: the spec is still `accepted` (`in-progress` for a `--force`
add) and every `depends_on` spec is `done` or `superseded`. Anything else keeps the item
queued with a `blocked_by` reason (`spec status is draft`, `waiting on SPEC-0181 (accepted)`,
`SPEC-0181 not found`, `spec file missing`) — so a chain can be queued in one go and opens
link by link as each spec lands on the base. Each admission records a transition with reason
`admitted`; an item becoming blocked or unblocked records one `backlog` ledger event (none
per unchanged tick), and `nw queue prioritize` records a `priority{from,to}` event.

Park reasons: `cost_cap` (checked pessimistically before every spawn), `loop_exhausted`,
`implementer_blocked` (blocked twice in a row), `triage_escalated`, `review_escalated`,
`open_failed`,
`rebase_conflict`, `main_dirty`, `main_not_on_base`, `base_moving`, `gh_failed`, `human`.
A parked item keeps its worktree; `nw queue resume ID [--to STATE] [--note …]
[--raise-cap USD] [--reset-loops] --yes` continues it, `nw queue abandon ID --yes` ends it.
With a loop running, `nw queue park` applies after the current step is recorded.

Every transition, run (with cost), gate run, finding, park and close is in the campaign's
ledger (`nw campaign log ID`). `--json` shapes: `nw.queue.v1`, `nw.queue.add.v1`,
`nw.queue.backlog.v1`, `nw.queue.prioritize.v1`, `nw.queue.tick.v1`, `nw.queue.park.v1`,
`nw.queue.resume.v1`, `nw.queue.abandon.v1`.

## Gate slots

Gate runs share a host-wide pool of `agentic.gates.slots` slots (default 2, an integer from 1 to
16; SPEC-0309). A gate run takes one slot for all its gates, so concurrent parallel-pytest gates
never oversubscribe the CPU. A campaign waiting for a slot is not reported `campaign_silent` or
`state_stuck`, and the wait does not count against its gate budget. A wait over 30 s writes a
`gate_waiting` event, and the gate result records `slot_wait_s`. `nw queue gate-slots [--json]`
shows the holders and the waiters.
The quiet conductor stream (SPEC-0398) also holds `campaign_silent` / `state_stuck` for a
campaign waiting for or holding a gate slot, or whose gate is deferred behind a drain or another
gate, until the hold, drain or deferral passes its bound.

**Remote gate workers (in progress).** A second machine can run gates over SSH (SPEC-0310). The
coordinator keeps a registry (`nw workers add NAME --ssh me@box [--slots N]`, `list`, `check`,
`on`, `off`, `rm`; SPEC-0337) and the worker host answers `nw worker` (SPEC-0336). `nw workers
bootstrap NAME --ssh nw@box --install-key` automates the setup (SPEC-0354): it installs a wheel
of this checkout, the forced-command line for a dedicated worker key (`agentic.workers.key`)
and the registration. Setup and the threat model are in `docs/WORKFLOW.md` (Remote gate worker).

## Events and watchdog

`nw queue events` is the queue's alert stream (SPEC-0181): `<data_dir>/agentic/events.jsonl`,
one `nw.event.v1` JSON record per line — `admitted`, `parked`, `resumed`, `merged`,
`handed_off`, `abandoned`, `run_failed` (with `transient: true` for account limits and API
overload), `cost_warning`, `backlog`, and mailbox `message`s to the conductor. Events are
projected from the campaign ledgers exactly once, so the stream works with any loop version.
`nw queue events --follow --for conductor` streams new events forever with a per-reader cursor
(a restarted follower resumes after the last delivered event) and runs a **watchdog** outside
the loop: `runner_dead` (work pending, no runner), `lease_stale` (runner hung, no run in
flight), `run_overdue` (a persona run past its timeout), `campaign_silent` (no activity for a
long time), `backlog_blocked_long` (a queued item blocked past `blocked_after_s`, default 1200;
the quiet conductor stream holds it while every dependency it waits on is being built),
`state_stuck` (an active campaign in one state past `stuck_after_s`, default 900, unless a run
or gate is in flight within budget), `ready_not_admitted` (a ready item not starting with a
free slot past `ready_grace_s`, default 60), `waiting_not_admitted` (a WAITING campaign not
resumed although a slot has been free past `waiting_grace_s`, default 60) and
`runner_journal_silent` (the loop has not ticked for 3 poll intervals while the lease is held
and fresh, outside a declared long `open` / `close` / `drain` step; SPEC-0228). The loop counts
as alive while it marks progress inside a tick (`progress_at`, SPEC-0313), so a long tick with
several runs in flight is not a stall; the event carries `last_progress_at` and `last_tick_s`
(duration of the last completed tick). The runner publishes its loop state in the lease
(`tick_at`, `progress_at`, `last_tick_s`, `poll_interval_s`, `in_flight`, `slot_freed_at`,
`busy`) for these. `campaign_silent` skips a `CLOSING` campaign waiting in the close train
behind a live head (SPEC-0314), and the quiet stream holds one that still arrives while its
head is alive in the runner. Each watchdog condition fires once until it
clears. The follower also appends a `conductor_ping` every `ping_interval_s` (default 600,
SPEC-0212) with a digest of blocked, stuck, parked (and whether the conductor decided),
waiting and ready-not-admitted items, so slow problems reach the conductor even when no
event fires.

## Mailbox

`nw mailbox send|follow|list|thread` (SPEC-0183): per-recipient JSONL inboxes for
`conductor`, `human` and `steward:<Project>`, `nw.message.v1` envelopes with `kind`
(question / answer / request / info), `campaign` and `re` threading. Messages to the
conductor also appear in its event stream, so one Monitor wakes it for both.

Campaign stamps (SPEC-0431): without `--campaign`, `nw mailbox send` stamps the campaign of
the `--re` message (found in any inbox), else the one existing campaign id its text names;
two or more, or none, leave `campaign` null. An inferred stamp adds `campaign_from`
(`"re"` or `"text"`) to the envelope; explicit and absent stamps carry none.
`--no-campaign` skips inference (and conflicts with `--campaign`). `nw queue decision`
infers the same way: without CAMPAIGN it uses the campaign of the `--event` queue event,
else the one campaign its rationale and `--action` name, recording `campaign_from`
(`"event"` or `"text"`) and treating the decision exactly like an explicit campaign (ledger
note, park coverage); it takes `--no-campaign` too. Old records are never rewritten.

## Conductor

`/nw-conduct` (SPEC-0182) turns a session into the **conductor**: it arms a Monitor (Claude
Code or Cursor) on `nw queue events --follow --for conductor` and is woken by every event — a park,
a merge, a dead or hung runner, an overdue run, a steward's message — and resolves it from a
decision table (resume, raise the cap once, ask the project's steward, restart the loop,
reconcile ideas after a merge), escalating to the human only for design questions and the
human's own checkout. `nw queue conduct-status` is its one-call situation report. The
follower's reader lock means only one conductor can be armed.

`nw queue decision [CAMPAIGN] RATIONALE` (SPEC-0431) records the conductor's choice in
`decisions.jsonl`. Without CAMPAIGN it stamps the campaign of the `--event N` queue event, else
the one existing campaign its rationale and `--action` name; the record then carries
`campaign_from` (`"event"` or `"text"`), counts like an explicit campaign (ledger note, park
coverage) and the output says it was inferred. `--no-campaign` skips inference. Details under
[Mailbox](#mailbox).

## Launching the conductor and stewards

`nw queue conductor-launch` and `nw queue steward-launch PROJECT` start the session with a
pinned model and effort, forced context limits and (on Claude) a tool allow/deny list
(push `nw/*` branches and open PRs; never push `main` or merge a PR). Default is `claude`.
`--runner cursor` starts `cursor-agent` instead (`cursor-agent login`; `--force --trust` plus
a policy footer — Cursor has no allow/deny CLI). Conductor `--runner` also writes host
`agentic.runner`; steward-launch does not. The session id is registered before the binary
starts, so Claude cost is tracked (`nw queue conductor-session`, `nw queue conductor-cost`).
`--dry-run [--json]` prints the plan; `--resume` restarts the newest launched session
(without `--runner`, it follows that session's stored backend). Settings live under
`agentic.conductor.launch` (a `steward:` sub-mapping overrides them for stewards).

Default permission rules (SPEC-0353; code-held in `launch.py`, a configured `allowed_tools` /
`disallowed_tools` list replaces the default wholesale and gets none of the widening):

- **Pushes.** `nw/*` and `wt/*` branches may be pushed as plain `git` or `git -C <dir>`, plain or
  `--force-with-lease` / `--force-with-lease=<ref>:<sha>`, refspec `<branch>` or `HEAD:<branch>`.
  Pushes to `main` / `master` in any refspec spelling, bare `--force` / `-f`, `+` refspecs, and any
  extra refspec or flag after the branch are denied; so is `gh pr merge`. A `:<dst>` after a
  namespaced ref (`nw/x:release`, `nw/x:heads/main`, a tag), a second remote or token hidden in
  a `--force-with-lease=` value, and any `git -C <dir>` command whose ` push ` is not the
  subcommand are denied too.
- **Steward rebase set.** `steward_allowed_tools` (top level or `steward:`; `[]` removes it) is
  added to the allow list for stewards only: `git fetch|rebase|add|commit|restore` and
  `checkout --ours|--theirs`, plain or with `-C <dir>`. It is merged into `--allowedTools`.
- **Steward guard.** Explicit denies for shell-executing and destructive git forms (`--exec`,
  `-x`, `-c`, `--upload-pack`, `--git-dir`, `reset --hard`, `branch -D`, `clean`, a push to
  another ref hidden behind a steward keyword, and subcommands such as `config`, `rm`,
  `update-ref`, `stash`, `remote`, `worktree` that a `git -C *` rule could otherwise swallow). The
  `git -C *` denies can also match a non-push command whose arguments contain `push`, `main`,
  `config`, `remote`, … as separate words (e.g. in a commit message); keep such words out of
  `-m` or use `-F`. The steward verbs are also pinned structurally: `git -C <dir> * <verb> *` for
  `fetch|rebase|add|commit|restore|checkout` is denied, so the verb must be the subcommand right
  after `-C <dir>` and no other subcommand (`grep -O<cmd>`, `log --output=<file>`,
  `clone --template=<dir>`, …) can hide behind a steward allow rule. New false positive: a
  `commit -m` message containing ` add `, ` commit `, ` fetch `, … is denied; use `commit -F`.
  `grep -O`/`--open-files-in-pager`, `--output` and `--template` are also denied by name. The
  guard is still a denylist for other shapes (a `-C` directory containing a space, word splitting
  through `$`/`${IFS}` or a tab). Because git accepts abbreviated long options and a stuck
  short-option value, the
  shell-exec flags are denied by their shortest unambiguous prefix (`--ex*`, `--upl*`, `--rece*`,
  `--config-e*`, `--git-d*`, `--wo*`), `-x` with nothing after it (`-x'cmd'`), and any dash option
  of `rebase` followed by an `x` (`-ix'cmd'`), so a `rebase` with a dash option and an `x` later
  in the line is denied.

Permission flags are fixed when a session starts: relaunch running conductors and stewards
(`nw queue steward-launch PROJECT --resume`) to pick up changed rules.

Claude launches exit 1 when the model has no price; Cursor launches skip the USD table.
nightwright ships no price table; set one for Claude (keys are model-name prefixes, longest
match wins; values are dollars per million tokens):

```yaml
agentic:
  conductor:
    prices:
      claude-: {input: 3, output: 15}
```

## Escalations

`nw escalations list|show|answer|withdraw` (SPEC-0232): when the conductor cannot decide, it
escalates (`nw queue escalate`) and the campaign parks as `human_decision`. `list` shows the
open ones (`--all`, `--json`); `answer ESC-NNNN` takes exactly one of `--choose` (option number
or label), `--text`, or `--waive` / `--attest` with `--reason` (override review findings), and
is a dry run without `--yes`. The answer wakes the conductor, which applies it.

## Stewards

`/nw-steward` (SPEC-0184) turns a project session into `steward:<Project>`: a Monitor on
`nw mailbox follow --to steward:<Project>` wakes it for the conductor's questions about that
project's campaigns; it answers with a concrete action, and routes the human's queue requests
for the project to the conductor. One steward per project (reader lock); stewards advise and
never implement in campaign worktrees.

nightwright dogfoods itself: the repo's own `.nw/project.yaml` declares `uv run pytest -q -x` and
`python3 tools/spec_lint.py` as required gates, merge policy `merge` into `main`, and
`uv sync` as worktree setup. `nw projects config Meta-Tools` shows it.
