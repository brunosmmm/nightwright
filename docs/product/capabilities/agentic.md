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
`commit`, `gate`, `finding`, `cost`, `note`, `park`, `resume`, `close`, plus the backlog
`backlog` and `priority` events of SPEC-0180), each carrying a
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

### Outbound projects

An outbound spec (`ACME-0001`, authored in the outbox) is queued like any other once it has
been exported: `wt spec export ACME-0001`, then `wt queue add ACME-0001`. When its campaign
reaches `MERGED` the close step runs `wt spec pull-status` for it: the outbox copy takes the
merged file's `status` and the linked `EXPORTED` idea becomes `SHIPPED`, with an
`outbound_pulled` event in the ledger. A pull failure is a minor `bookkeeping` finding and
never undoes the merge. A `HANDED_OFF` campaign pulls nothing; the PR sweep (`wt queue
sweep-prs`) runs the pull when it records the merge, which needs the target checkout to have
pulled the merged file (otherwise a finding says to pull and rerun `wt spec pull-status`).

## Profiles

Models, effort, budgets, loop limits and slots are workflow profiles (SPEC-0171, SPEC-0237).
Builtin profiles `balanced` (default: Sonnet implementer and triager, $10 cap, 3 loops,
2 slots), `frugal` and `thorough` ship as YAML files in the package
(`src/wt/agentic/profile_defs/<name>.yaml`).

### Profile files and layers

Four layers define profiles; a higher layer wins, key by key:

1. builtin files (package data);
2. user files, `<config_dir>/profiles/*.yaml`;
3. the `agentic.profiles` deltas in `config.yaml`;
4. project files, `<repo>/.wt/profiles/*.yaml`, read from the project's **main checkout** (a
   campaign cannot change its own profile by editing its branch). `slots` are a property of the
   whole queue, so they ignore the project layer.

The profile name is the file stem (`^[a-z][a-z0-9_-]*$`; a `*.yml` file is an error). A file
is either complete, or a delta: it extends the same name in a lower layer (`extends: <own
name>`, or simply the same stem), or another profile (`extends: balanced`). A `config.yaml`-only
new name merges over `balanced`, as before. Files are re-read on every tick, so an edit applies
without restarting the runner.

```yaml
# <repo>/.wt/profiles/careful.yaml
schema: wt.profile.v1            # required in files
extends: balanced                # optional; a delta over the effective `balanced`
description: "opus implements, one more loop"
loop_max: 4
campaign_cap_usd: 20.0           # number > 0, or null for no cap
personas:
  implementer: {model: opus, effort: high, max_budget_usd: 8.0}
```

Keys: `slots`, `loop_max`, `run_timeout_s` (integers >= 1), `campaign_cap_usd`, `reviewers`, the
`review:` block (below) and `personas.<name>` with `model` (`opus|sonnet|haiku` or a
`claude-*` id), `effort` (`low|medium|high|xhigh|max`), `max_budget_usd`, `permission_mode`
(`default|acceptEdits|bypassPermissions|dontAsk|plan`) and `read_only`. Every source is
validated: an unknown key, persona, model, effort or permission mode, or a wrong type, fails
with `<file>: <key.path>: <problem>`, and only the profile that file defines breaks. A campaign
of that profile parks `open_failed` with that message; `wt queue add` and `wt spec review` fail
with it. The resolved profile lists its `sources`, lowest layer first.

The `config.yaml` block holds the same deltas:

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
(`implementer`, `gate_triager`, `conformance_reviewer`) is filled with the campaign's context
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
`counters.review_retries_by_persona` appears only after the first retry. `wt campaign show`
prints a `review:` line (score, verdict, advisory count and how many are `major`) and
`wt queue list --json` rows carry `review_score`.

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
(`wt.agentic.followups`) lists the `major`/`minor` findings (`nit` too with
`review.followups_nits`) that were advisory or waived in **one grouped idea per campaign**
(`kind: improvement`, the campaign's project, the spec's `parent` epic when it has one);
`review.followups: false` files nothing. It never blocks a merge: a failure is a `note` event and
the findings are reported `unfiled`.

- **The grouped idea.** Its heading carries the marker `wtfug:<campaign id>` and the count
  (`wtfug:<cid> [SPEC-NNNN] review follow-ups (<n> findings)`); its Summary is a checklist with one
  line per finding (`- [ ] wtfu:<key12> <severity> persona/slot: title - detail (files: …)`),
  ordered by severity, `advisory` before `waived`, then title. Ticked (`- [X]`) lines still count.
- **Key.** `key = sha1(spec id | normalized title | sorted files)`; the first 12 hex characters
  are the `wtfu:<key12>` token of the checklist line. The key ignores persona and severity, so a
  finding whose severity or reporters change keeps its key; a reworded title or other cited files
  is a new finding (accepted: a spurious follow-up is cheaper than a lost one).
- **Listed once.** A finding already listed in another **open** grouped idea of the same spec
  (including a `legacy-` group) gets no new line; its `followup` event points at that group. A
  closed group does not suppress filing.
- **Cap.** `review.followups_max` (integer >= 0, default 10, `0` = no cap) bounds the lines per
  campaign. The findings past the cap are reported as `capped` (no `followup` event, so a re-run
  with a raised cap can still list them) and an overflow line
  `- +K more findings not listed (wt campaign show <cid>)` ends the Summary.
- **Carry-over.** On every `fix` outcome `_merge_carry` merges the review's advisory and waived
  findings into `state.review_carry` (de-duplicated by key) and drops every key that is blocking
  in that review, so advisory findings of earlier loops are still filed at the close step.
- **Idempotent and crash-safe.** A key with a `followup` ledger event is skipped (and still
  reported). Otherwise the group is looked up by its `wtfug:` marker in the ideas file and its
  archive and **adopted**: the filer writes add_idea, Summary, title, then the events, and a
  re-run appends only the missing lines and recounts the title, so a crash anywhere leaves no
  second idea and no second line.
- **Ledger and reports.** Each listed finding is a `followup` event
  (`persona, severity, title, key, idea_id, origin`), whose `idea_id` is the group. `state.close.followups`
  holds `{filed, unfiled, waived, capped, idea_id}`; `wt campaign show` prints
  `follow-ups IDEA-NNN (<n> listed, <k> capped)`, the follow-up lines (`IDEA-NNN severity
  title`), the `[capped]`, unfiled and waived findings. The close row of `wt.queue.tick.v1`
  carries a `followups` key (`filed`, `unfiled` and `capped` counts, `idea_id`, the distinct
  `ids`, plus the `items`, `unfiled_items` and `waived` lists).
- **Migration.** `wt ideas group-followups [--yes] [--json]` folds the open single `wtfu:` ideas
  that SPEC-0207 used to file into one `wtfug:legacy-<SPEC-NNNN>` idea per spec and closes each
  single as `DROPPED` with `FOLDED_INTO` the group. Without `--yes` it only prints the plan
  (`wt.ideas.group_followups.v1`, `written: false`); a re-run is a no-op.

## Closing

Closing is the step that guards the base branch, and for wt itself the live editable install
(SPEC-0175). It is plain Python over the git wrapper and the gate executor, and it never
forces anything:

1. The spec in the worktree must verify (`wt spec verify` checks) **and** be `status: done`;
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

Idea states are not touched here; `/wt-conduct` runs `wt spec reconcile --apply` after merges.

## Queue

The queue is the engine (SPEC-0176). Python decides every transition; personas only produce
commits and one verdict. One process runs the loop at a time (the lease), two or three
campaigns advance concurrently (the profile's `slots`).

A spec is admitted only with an approving review for its current text (SPEC-0188):
`wt spec review SPEC-0170` runs the read-only `spec_reviewer` persona and stores the verdict in
`docs/specs/reviews/SPEC-0170.json`, keyed by the spec file's sha256 — editing the spec
invalidates it. `wt queue add --force` skips the check and records `review: forced`.

```bash
wt spec review SPEC-0170          # approve | revise | split, with findings
wt spec review SPEC-0170 --show   # read the stored review; runs nothing (SPEC-0242)
wt spec reviews --verdict revise  # the latest review of every spec
wt queue add SPEC-0170            # plan only (dry-run)
wt queue add SPEC-0170 --yes      # create the campaign in QUEUED
wt queue list                     # Active (slots) and Backlog (admission order) tables
wt queue backlog                  # only the backlog: priority, what each item waits on
wt queue add SPEC-0171 --priority 1 --yes   # 1 (first) .. 5; default 3
wt queue prioritize CAMPAIGN 2 --yes        # re-prioritize a QUEUED / PARKED item
wt queue watch                    # the same, live
wt queue run                      # the loop; wt queue tick = one iteration
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
per unchanged tick), and `wt queue prioritize` records a `priority{from,to}` event.

Park reasons: `cost_cap` (checked pessimistically before every spawn), `loop_exhausted`,
`implementer_blocked` (blocked twice in a row), `triage_escalated`, `review_escalated`,
`open_failed`,
`rebase_conflict`, `main_dirty`, `main_not_on_base`, `base_moving`, `gh_failed`, `human`.
A parked item keeps its worktree; `wt queue resume ID [--to STATE] [--note …]
[--raise-cap USD] [--reset-loops] --yes` continues it, `wt queue abandon ID --yes` ends it.
With a loop running, `wt queue park` applies after the current step is recorded.

Every transition, run (with cost), gate run, finding, park and close is in the campaign's
ledger (`wt campaign log ID`). `--json` shapes: `wt.queue.v1`, `wt.queue.add.v1`,
`wt.queue.backlog.v1`, `wt.queue.prioritize.v1`, `wt.queue.tick.v1`, `wt.queue.park.v1`,
`wt.queue.resume.v1`, `wt.queue.abandon.v1`.

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
`busy`) for these. Each watchdog condition fires once until it
clears. The follower also appends a `conductor_ping` every `ping_interval_s` (default 600,
SPEC-0212) with a digest of blocked, stuck, parked (and whether the conductor decided),
waiting and ready-not-admitted items, so slow problems reach the conductor even when no
event fires.

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
