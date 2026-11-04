# nightwright workflow — idea → shipped work

One-page cheatsheet for **humans**. Agent playbooks live in `skills/` (`nw-orient`,
`nw-new-work`, …); this page is what you keep in muscle memory.

## The pipeline

```
capture → explore (durable) → decide → author → generate → build → verify → (export if outbound)

nw idea "…"          park a thought (seconds)
/nw-seed             Cursor: distill conversation → idea (+ optional Summary/Log)
nw idea log …        append a Log note (date+time); was `nw idea explore`
nw idea summary …    set durable Summary (gate for promote)
                     bodies are org: quote code/paths/identifiers as ~x~ or =x= (left
                     untouched); multi-line code as #+begin_src (SPEC-0164)
nw idea show …       read headline + Summary / questions / Log
                     (TTY: Rich+Pygments org highlight; --plain / --json stay plain;
                     theme via config idea_show_theme, default nord)
nw ideas / nw next   triage; next says log/summary before promote
nw ideas --query …   related-idea search over open+closed (SPEC-0086)
nw ideas --json      agents: structured list (also: nw next --json, nw idea show ID --json)
/nw-related          Cursor: search before capture/promote
/nw-explore          Cursor: enrich idea, never promote
/nw-new-work         Cursor: explored idea → accepted spec (post-explore)
/nw-generate         Cursor: accepted spec → org tasks
nw spec review SPEC  agentic (SPEC-0188): reviewer persona; `queue add` needs an approving review
nw queue add SPEC    agentic loop (SPEC-0169/0176): queue an accepted spec; `--yes` writes
nw queue run / list  the loop (one process, lease) / what is in flight; PARKED = your turn
nw queue watch       live dashboard: runner, active campaigns, backlog, delivered
nw campaign log ID   every transition, run, gate, finding and dollar of one campaign
nw mailbox send|follow|list|thread   conductor <-> steward <-> human messages (SPEC-0183)
nw escalations list|show|answer|withdraw   questions the conductor escalated to you (SPEC-0232)
nw profiles list|show|check   workflow profiles and which file set each value (SPEC-0238)
nw personas list|show        persona registry across builtin/user/project layers (SPEC-0370)
nw queue conductor-launch / steward-launch PROJECT   start the conductor / a steward session
                     (needs agentic.conductor.prices in config)
implement            you or the agent
/nw-verify           Cursor: post-hoc DoD audit (global) — internal: `nw spec verify`;
                     outbound portable: audit the file in the target repo
/nw-rework           redesign (supersede) or extend incomplete epic
/nw-export           only if outbound → another repo
```

In a Cursor/Claude session you can say the stage instead of remembering flags:
“new work on IDEA-003”, “generate SPEC-0023”, “export ACME-0001”.

**Superpowers coexistence (SPEC-0138):** if Cursor injects Superpowers skills, they are
tactics only. Governing design stays in nightwright (`docs/specs/` / outbox portables). Agents:
`/nw-orient` — do not write governing docs under `docs/superpowers/`; “keep working” →
`nw hub` / `nw next`; claim done via `/nw-verify`.

## Idea states → what you do

| State | Meaning | Your move |
|-------|---------|-----------|
| `IDEA` | parked, not yet incubating | capture; optionally start explore |
| `INCUBATE` | exploring (durable notes on the idea) | `/nw-explore` / `nw idea log` — **not** promote yet ([SPEC-0024](./specs/0024-formal-explore-stage-wt-explore-skill.md), [SPEC-0031](./specs/0031-rename-idea-explore-to-log-datetime-log-stamps.md)) |
| `SPECCED` | a draft/accepted spec exists (`:SPEC:` set) | finish authoring → `accepted`, then generate (internal) or export (outbound); if design wrong or epic incomplete → `/nw-rework` |
| `PROMOTED` | internal: org tasks exist (`nw spec generate`) | build; track time against tasks; incomplete epic → `/nw-rework` (extend), not bare `nw tasks` |
| `EXPORTED` | outbound: handed off via `nw spec export` | build in the **target repo** on the portable spec (update `status` + AC); `nw spec pull-status` / `sweep` when portable is `done` → **SHIPPED** |
| `SHIPPED` | finished (post-export or outbound reconcile) | ignore (`nw ideas` hides these by default); force via `nw idea state … SHIPPED` or desk `m` → state |
| `DROPPED` | dead | ignore (`nw ideas` hides these by default) |
| `RESEARCHED` | explored, no implementation | ignore |

```bash
nw ideas                 # open only
nw ideas --all           # include PROMOTED/EXPORTED/SHIPPED/DROPPED/…
nw idea state IDEA-00N SHIPPED   # force lifecycle keyword (SPEC-0135)
nw state IDEA-00N DROPPED        # generic org state (any task)
COLUMNS=200 nw ideas | less -S    # piping: Rich falls back to 80 cols without COLUMNS
```

## Internal vs outbound

| Association | Where the spec lands | After `accepted` |
|-------------|----------------------|------------------|
| No project, or Meta-Tools / this repo | `docs/specs/SPEC-NNNN` | `nw spec generate` → **PROMOTED** → implement here |
| `:PROJECT:` with `outbox_targets` entry | `<data_dir>/outbox/<project>/…` | `nw spec export` → **EXPORTED** → implement in target (portable working copy); `pull-status`/`sweep` when portable `done` → **SHIPPED** |

`outbox_targets[project].repo_path` is also the **explore research root** for that project
(`nw idea show` → `research`; SPEC-0042). Meta-Tools is internal — explore uses this nightwright repo.
Projects with a `:PROJECT:` but no usable `repo_path` get `research.root: null` / `outbound:
false` until you register them (or explore from conversation only).

```bash
nw idea "…" --project Meta-Tools          # internal tooling
nw idea "…" --project Example               # outbound if Example has a repo_path
nw projects --json                        # agent map: name → research/outbound (SPEC-0095)
# register a missing project (SPEC-0137) — dry-run first, then --yes
nw projects add Example --repo ~/work/example-repo           # prints plan; does not write
nw projects add Example --repo ~/work/example-repo --yes     # commits to ~/.config/nightwright/config.yaml
nw projects add StubName --yes                      # association stub (outbound: false)
nw spec new --from-idea IDEA-00N          # routing follows the idea's :PROJECT:
nw spec new --from-idea IDEA-00N --internal   # force docs/specs/ even with a project
```

**Multi-project chats (SPEC-0095):** if the conversation names ≥2 tracked projects or paths,
preview a project map (`nw projects --json`), fan out as an epic + per-project children, and
do not write Summary until that map is confirmed. Unknown paths → `nw projects add …` (confirm
plan, then `--yes`). Soft-warn if continuing a single-root explore anyway.

## Commands you actually need

```bash
# Capture — ideas are seeds; actionable work is `nw add` (SPEC-0091)
nw idea "short thought" --project Meta-Tools --tag ergonomics
nw add "fix the flaky CI gate" --project Meta-Tools     # actionable → task
nw idea "systemic gate design" --kind bug --force-idea --project Meta-Tools
# `--kind bug` without --force-idea soft-warns and suggests `nw add` (still writes idea)
nw ideas --kind bug                                       # filter; kinds ≠ TODO states

# Triage
nw ideas
nw agenda                    # dated tasks for today

# Spec (CLI; or use /nw-new-work in Cursor)
nw spec new --from-idea IDEA-00N
# …author Decision/Design/AC/Test plan…
# set status: accepted in the spec frontmatter
python3 tools/spec_lint.py --write-ledger

# Tasks + build (internal)
nw spec generate SPEC-NNNN
nw tasks
# implement; mark tasks done with `nw done …`

# Outbound only (do NOT generate on PROJ-NNNN)
# Default scheme is nw-native: portable + contract; AC + Test plan; verify before done
# (docs/product/concepts/portable-default.md). Override with --scheme / outbox target.
nw spec export PROJ-NNNN          # → idea EXPORTED; portable file in target repo
# foreign agent updates portable status / AC in the target; close with /nw-verify
nw spec pull-status PROJ-NNNN     # required when portable is done → idea SHIPPED
nw spec schemes                   # nw-native (default), plain-md
# Optional id pin when a scheme allocates X.Y.Z ids:
# neighbors (e.g. 1.3.2 after 1.3.1) without juggling task_base —
#   frontmatter: task_id: "1.3.2"
#   or: nw spec export PROJ-NNNN --scheme nw-native --task-id 1.3.2 --to …
```

Exact flags: `nw <cmd> --help`. Bare `nw idea TEXT` captures; `nw idea show|log|…`
enrich/read; listing is `nw ideas`.

### Seeing the queue (SPEC-0356)

```bash
nw queue list          # Active / Needs you / Waiting / Backlog: title, pri, park reason, waiting-on
nw campaign list       # every open campaign, same columns (--all for terminal ones)
nw queue backlog       # admission order with what each item waits on
nw queue graph         # dependency tree: dependents under their dependencies, stubs for
                       # dependencies with no campaign, cycles listed apart (--project, --all)
nw queue unqueued      # specs/ideas with no campaign and why; `ready` rows (forgotten) first
nw campaign show ID    # includes a `remote gate #N on worker W  job J  running 3m12s` line
```

The list tables fit the terminal width (`COLUMNS`): the campaign id and the state are never
cut, the title flexes (min 12 cells), and other columns drop in this order as the width
shrinks: updated/age first, then size/tone/profile, loops, project, cost, pri; park reason and
blocked-by go last and fold into the title cell (`[budget_cap] …`, `⧗ SPEC-0337 …`) instead of
vanishing. Every view has a `--json` twin (`nw.queue.v1`, `nw.campaigns.v1`,
`nw.queue.backlog.v1`, `nw.queue.graph.v1`, `nw.queue.unqueued.v1`) carrying `title,
priority, depends_on, blocked_by, waiting_on, park_reason, cost_usd, cap_usd`.

## Interactive desk (`nw tui`)

`nw tui` is a human-only ideas desk (SPEC-0098/0099). Textual is a core dependency since
SPEC-0368 (the `tui` extra is an empty alias); agents keep the classic commands and
`--json`.

```bash
uv sync                      # from a checkout (Textual is a core dependency, SPEC-0368)
uv tool install --editable .   # from the nightwright checkout (NOT PyPI `nw` — that is a different tool)
# editable: skills/specs/Meta-Tools resolve from the checkout; non-editable needs nw_checkout: in config
nw tui
nw tui --tab queue           # tabs: ideas, hub, queue, events, mailbox (keys 1..5 switch)
nw tui --campaign ID         # one campaign's timeline, runs, gates and reviews
```

Tabs: **ideas** (browse, search, capture), **hub** (the triage union), **queue** (live
campaigns), **events** (queue event stream and watchdog conditions) and **mailbox** (inboxes,
threads, reply as the human).

## Agent skills (installed once)

```bash
nw skills install            # symlink into ~/.claude/skills
nw skills list
# Optional: Cursor / other hosts — same skills, different dest
# nw skills install --dest ~/.cursor/skills
```

| Skill | When |
|-------|------|
| `nw-orient` | Cold session; read once instead of `src/nightwright/` |
| `nw-capture` | Park a one-liner without breaking flow |
| `nw-seed` | Distill conversation → idea (+ optional Summary/Log) |
| `nw-related` | Search open+closed ideas before capture/promote |
| `nw-explore` | Enrich an idea durably (Summary/Log) — **never** promote |
| `nw-new-work` | Explored idea → lint-clean **accepted** spec (post-explore) |
| `nw-generate` | Accepted **internal** SPEC-NNNN → org tasks |
| `nw-rework` | Linked spec wrong → supersede; incomplete epic → scaffold children |
| `nw-export` | Outbound spec → target repo |
| `nw-implement-spec` | In the **target** repo: implement an exported portable spec (AC + Test plan + `status`) |
| `nw-verify` | Post-hoc Definition-of-Done audit (internal `nw spec verify`, or an outbound portable in place) |
| `nw-conduct` | Become the conductor: watch the queue's event stream and resolve parks and failures |
| `nw-steward` | Become a project's steward: answer the conductor's questions over the mailbox |
| `nw-sheet-sync` | Sync ideas against a project's external triage sheet |

**Triage:** `nw hub --json` unions open ideas + active internal specs + outbound outbox
(`nw.hub.v1`). No MCP server — use skills + CLI `--json` / hub (SPEC-0053).

After `nw spec export`, tell the foreign agent `/nw-implement-spec` (requires `nw skills
install` on that machine) instead of a bare “implement”. Record post-ship fit with
`nw spec fit-log <OUTBOUND-ID> --note "…"`. Outbound AC should include an outcome (“I can …”)
or `outcome:` (SPEC-0048).


## Day-to-day ritual

1. **Thought while coding** → `nw idea "…"` (don’t open org by hand). After a design chat →
   `/nw-seed`.
2. **Morning** → `nw ideas` + `nw agenda`. Promote one ripe idea or leave it.
3. **Transitions** → let `/nw-new-work` / `/nw-generate` drive the CLI; you pick *which* idea.

Optional fish abbreviations:

```fish
abbr wi 'nw idea'
abbr wis 'nw ideas'
abbr wn 'nw next'
abbr wtoday 'nw ideas; and nw agenda; and nw next'
```

## Tree guards in the test gates (SPEC-0421)

The loop and close pytest gates run only the tests a branch affects (`tools/affected_tests.py`).
A test that scans the tests or src tree (SPEC-0269 waits, SPEC-0409 textual skips, SPEC-0185 raw
prints) imports nothing it checks, so a branch that breaks it would not select it. Such a module
is a **tree guard**: it sets `pytestmark = pytest.mark.tree_guard` at module level (a list holding
it also works), and both gates pass `--guards`, which adds every marked module to every non-full
selection, read from the tree being tested. A changed or added file under `tests/` or
`src/nightwright/` also selects the tests that scan that tree. To register a new guard, add the
marker in the same commit as the test; `tests/test_guard_registry.py` fails for a tree-scanning
module that is neither marked nor listed in its `GUARD_EXEMPT` (with a reason).

## CLI next-step hints

```bash
nw next                 # open ideas + recommended command per row
nw next --all           # also PROMOTED / EXPORTED (build reminders)
nw ideas --json         # the hint per row, for agents (no `next` column on the table)
```

`nw next` is the home of the hint. `nw ideas` dropped its `next` column (SPEC-0075) so the
headline gets that width back; the hint is still in `nw ideas --json`.

Hints follow the state table above (`nw spec new …`, `author … → accepted`,
`nw spec generate …` for internal, `nw spec export …` / `implement in …` /
`nw spec pull-status …` for outbound, `/nw-rework …` for incomplete epics,
`nw tasks` for PROMOTED). Copy/paste the `next` cell.

## Shell completion (when extending the CLI)

Fish/bash/zsh scripts are **live** (SPEC-0022) — new subcommands show up without rewriting
completion files. New closed-set options/arguments still need `shell_complete=…` in
`cli.py` (e.g. idea `selector` → `complete_idea_id`). See README “Shell completion” and
`REQUIRED_SHELL_COMPLETE_NAMES` in `src/nightwright/completion.py` (enforced by pytest, SPEC-0039).

## Remote gate worker

A second machine can run gates for the queue over SSH (SPEC-0310). The worker side is
`nw worker` (SPEC-0336): `gate-job` reads one job (a `nw.job.v1` header line and a git bundle)
on stdin, runs the gate commands it was sent in a throwaway clone, and streams
`nw.job.frame.v1` frames back. `info`, `have` and `gc` answer the coordinator and keep the
worker's object cache bounded.

**Threat model.** A gate is a command, so the worker's SSH key is equivalent to **shell access
as the worker user**. The forced command limits the SSH surface to `nw worker` subcommands; it
does not limit what a gate can do.

Setup on the worker (all of it is mandatory):

1. **A dedicated, unprivileged user** (e.g. `nw`) with no sudo, no group memberships that
   grant access to other users' data, and nothing in its home but nightwright and the job
   store. Never use your own account.
2. As that user: `uv tool install <path or git URL of nightwright>` (puts `nw` at
   `~/.local/bin/nw`), then check `nw worker info --json`.
3. In that user's `~/.ssh/authorized_keys`, one line per coordinator key, with the
   **absolute** path to `nw`:

   ```
   command="/home/nw/.local/bin/nw worker ssh-command",restrict ssh-ed25519 AAAA... coordinator
   ```

   `restrict` disables port, agent and X11 forwarding and the pty. `nw worker ssh-command`
   reads `SSH_ORIGINAL_COMMAND` and execs only `nw worker info [--json]`,
   `have --repo <key> <sha>...`, `gate-job`, `gc [--keep-days <n>]` and the async job
   commands (SPEC-0340) `submit`, `attach <job> [--from <n>]`,
   `poll <job> [--from <n>] [--renew]`, `cancel <job>` and `jobs [--json]`; anything else
   exits 2 with `refused: <reason>` and runs nothing.

**Async jobs (SPEC-0340).** `nw worker submit` takes the same header and bundle as
`gate-job`, starts a detached job process and returns at once; the job survives the SSH
connection. `attach <job> --from <n>` replays and follows its numbered frames (exit 0 after
`done`, 3 after `error`, 4 when the job is `lost`) and renews the job lease while connected;
a job nobody renews for `agentic.workers.job_lease_s` (default 900 s) cancels itself. A job
process that stops heartbeating (`job_heartbeat_s`, default 10 s) for `job_lost_after_s`
(default 60 s), or dies, is `lost`; `cancel` and `gc` kill what is left of it.

Register a worker on the coordinator with the registry commands (SPEC-0337; not to be
confused with `nw worker`, which runs on the worker host): `nw workers add NAME --ssh me@box
[--slots N] [--nw PATH] [--no-check]` registers it (it runs `check` first unless `--no-check`),
and `nw workers check [NAME] [--json]` probes over SSH with `nw worker info --json` (exit 1 if
any probe fails; a version mismatch or a `worker stale` build id only warns). `nw workers list|on|off|rm` manage
the rest.

**Bootstrap (SPEC-0354).** `nw workers bootstrap NAME --ssh nw@box [--slots N] [--install-key]
[--admin me@box] [--reinstall] [--dry-run]` does steps 2 and 3 and the registration, skipping what is already
done, and prints a status table (`done`, `skipped (already ...)`, `would run`, `failed`,
`not requested`, `not run`; exit 1 if a step failed):

- **admin** (only with `--admin`): probes `sudo -n true` on the admin account, then creates the
  worker user with `sudo -n useradd -m` when `id -u` says it is missing. With `--admin`, every
  worker-side command below runs through the admin account as `sudo -n -i -u USER`.
- **install**: needs `uv` for the worker user; `uv tool dir --bin` gives `<bin>`. When
  `<bin>/nw worker info --json` reports another **build id** than this checkout (SPEC-0367;
  an older worker reports none), or fails, a wheel built here with `uv build --wheel` is
  copied over and installed with `uv tool install --force` (with `--admin`, through a
  `mktemp -d` dir under `/tmp`). The build id (`b1:<sha256>`) hashes every shipped file, so
  it changes with any code change, with no version bump, and exists on wheel installs.
  `--reinstall` installs regardless. The status detail says why (`no build id (older
  worker)`, `build <old> -> <new>`, `--reinstall`).
- **verify**: `<bin>/nw worker info --json` must report this checkout's version and the build
  id of the wheel just installed (the working tree's, unless it is dirty: a note says so).
- **key**: the line `command="<bin>/nw worker ssh-command",restrict <worker public key>`.
  Without `--install-key` it is printed. With it, it is appended once to the worker's
  `~/.ssh/authorized_keys` (700/600). A root account (by name or uid 0) is refused, and so is
  a worker key already authorized by any other line. (A `root@...` `--ssh` is refused even
  without `--install-key`.)
- **register**: `nw workers add NAME --ssh DEST --nw <bin>/nw --identity <private key>`, with
  its check when `--install-key` (the check then goes through the forced command), else
  `--no-check`. A NAME already registered with other settings fails; `nw workers rm` it first.
  Already registered with the same settings, the step is skipped, but with `--install-key`
  its check still runs (the worker may have been added with `--no-check` before).

The worker key is **dedicated**: `agentic.workers.key` (in `config.yaml`, default
`~/.ssh/nw_worker_ed25519.pub`) names its public key, and the private key is the same path
without `.pub`. Both must exist (`ssh-keygen -t ed25519 -N '' -f ~/.ssh/nw_worker_ed25519`;
bootstrap never creates it). Never reuse your login key: sshd uses the first matching
`authorized_keys` line, so a forced line for a key that is already authorized has no effect.
The record's `identity` (`nw workers add --identity PATH`, an absolute path) makes every
coordinator connection to that worker add `-i <identity> -o IdentitiesOnly=yes`, and tags its
ControlMaster socket with a hash of the key (`%C-<tag>`), so it never rides a master opened
with another key for the same `user@host`. `IdentitiesOnly` still lets ssh fall back to
`IdentityFile` entries for that host in your ssh_config, so `nw workers check` on an
`identity` record also sends a command the forced command must refuse, and fails when it is
not refused. When that command runs (the key is not bound to the forced command), the check
closes the record's ControlMaster (`ssh -O exit`), so no later connection rides a session some
other key opened. A check that fails for any other reason (a timeout, an ssh error, missing
git) leaves the master alone, because running gate jobs share it. Bootstrap's own ssh/scp
sessions use `-o ControlPath=none`, so they never share the coordinator's ControlMaster
sockets.

The job store lives under `<data_dir>/agentic/worker/` (`repos/` object caches, `jobs/`).
Each job runs `gc` (default `--keep-days 2`) when it ends; `nw worker gc --keep-days 0`
empties everything no live job uses.

**Dispatch (SPEC-0338).** With a worker registered (`nw workers add`, SPEC-0337), every gate
run (the loop's GATING run, the close re-gate, `nw queue gates`) takes a slot from the
workers' pools. SPEC-0374: while any worker is placeable (enabled, not draining, out of
cooldown) a gate waits for a worker slot and never takes a local one; the wait re-reads the
registry, so a worker going into cooldown, off or draining sends its waiters to the local
pool. `agentic.gates.local_overflow: true` restores the old local-last overflow. A remote run bundles the commit under test (a
`git stash create` commit when tracked files are modified, else HEAD) and the base against
what the worker already has (`nw worker have`), runs `nw worker gate-job` over SSH, and
writes the returned `gates/<seq>-<name>.{log,json}` files where a local run writes them; the
`gate` event gains `worker: <name>`. A transport failure (SSH, protocol, an `error` frame,
a drop, a timeout) appends `worker_failed`, puts the worker in a 600 s cooldown, and re-runs
the same gates locally under the same seq. A gate that fails on the worker is a result and
is not re-run. A run stays local, with one `finding` per campaign saying why, when the
worktree has untracked non-ignored files (the bundle carries commits only), the project sets
`remote_gates: false` in `.nw/project.yaml`, or a gate sets `remote: false` (for gates that
read ignored files such as `.env`). A gate's writes to the tree (`spec_lint.py
--write-ledger`, a formatter, a code generator) stay in the worker's throwaway checkout: the
worker's `done` frame lists the files the gates changed there, and a run that changed any is
re-run locally under the same seq (`worker_failed` with `stage: "tree"`, no cooldown), so the
worktree the runner commits is always the one a local run leaves. A gate that writes the tree
on every run therefore never gains from a worker; give it `remote: false` to skip the wasted
remote run. `agentic.workers.slot_wait_s` (default 300) bounds the
wait for the worker's own slot, counted from the end of the upload; past it the run falls
back locally without a cooldown. The upload of the header and bundle has its own bound
(`slot_wait_s` + one second per 256 KiB of bundle); past it the run falls back with
`stage: "upload"` and a cooldown, since a link too slow for the bundle fails the same way on
the next run.
`nw queue gates <campaign> --worker <name>` forces one worker (its cooldown ignored);
`--local` never uses one. `nw workers off <name>` returns gates to local at any time.

**Re-attach and adoption (SPEC-0341).** A worker whose cached `info` (written by
`nw workers check` / `add`) lists the `async` feature gets `nw worker submit` and
`attach --from <n>` instead of `gate-job`; `check` warns about a worker without it, which
keeps the synchronous `gate-job`. Each such job has a record at
`<data_dir>/agentic/remote-jobs/<job_id>.json` (`nw.remote_job.v1`: worker, attempt, gate seq,
the last applied frame `cursor`, `state`, `deadline_at`, `last_seen_at`). A dropped
connection backs off (2, 4, 8 … 60 s), runs `poll --renew` and re-attaches after its cursor;
the step gives up, appending `worker_job_lost {worker, job_id, attempt, reason}` and running
the gates locally, when the worker reports the job `lost`, stays unreachable longer than
`agentic.workers.worker_ttl_s` (default 300), or the record's `deadline_at` passes. SPEC-0342:
before running locally, a given-up job is retried as attempt + 1 on a worker none of its
attempts was lost on (the first free slot of the eligible pools, local last, so a full fleet
runs it locally at once), up to `agentic.workers.max_remote_attempts` (default 2) remote
attempts; each new attempt appends `worker_job_retried {job_id, from_worker, to_worker,
attempt}` (`to_worker: null` for the local run). Losses are counted per worker in
`<data_dir>/agentic/worker-losses.json`: three within a sliding hour raise one campaign-less
escalation (`kind: worker_lost`, options `off` / `keep`, nothing parked), and the queue tick
carries out the answer (`off` disables the worker as `nw workers off` does). A runner
exit (stop, code change, crash) leaves the job running on the worker: the next runner's gate
step finds the campaign's live record before it picks a seq, attaches from frame 0 and
finishes it under the same seq (rebuilding every result, rewriting no file already applied;
the `gate` event's `event_uid` keeps it from being written twice). A park or
`nw queue abandon` cancels the campaign's remote jobs.

**Drain (SPEC-0343).** `nw workers drain <name>` takes a worker out of service without losing
its jobs: no new gate is placed on it (a gate already waiting passes it over, and one that
took its slot in the race runs locally), while jobs already on it, adopted ones included,
finish there. `nw workers list` shows `draining (k jobs)`, `k` its non-final job records. Each
queue tick disables a draining worker once no live record names it and no slot of its pool is
held, emitting the queue event `worker_drained {worker}`; with no runner ticking it stays
draining. `on` cancels a drain, `off` disables at once (a gate that then lands on its slot runs
locally).

**Pull workers (SPEC-0334).** A worker the coordinator cannot SSH into (behind NAT, no inbound
SSH) dials out instead. On the coordinator, `nw serve --jobs [--bind ADDR:PORT]` serves the job
API (default `127.0.0.1:8790`; without `--tls-cert/--tls-key` only loopback and Tailscale
addresses, `100.64.0.0/10` and `fd7a:115c:a1e0::/48`, may be bound; `--client-ca` adds mTLS with
the worker's name as the certificate CN). `nw workers add NAME --pull [--kinds gate,persona]
[--url http://<coordinator>:8790]` registers the worker (the registry keeps only the token's
sha256; the signing secret is `<config_dir>/secrets/workers/NAME.key`, mode 0600) and prints a
one-time enrolment blob. On the worker, `nw worker serve --enrol BLOB` stores it in
`<config_dir>/coordinator.json` (0600) and starts leasing; later starts need no flag. Jobs
for it are queued under `<agentic_dir>/job-queue/`; the worker checks each job's HMAC
signature, expiry (15 min, 120 s skew), nonce and bundle hash before running it with the same
job process as `nw worker submit`, and posts its frames back every second. A lease not renewed
within `job_lease_s` is given up like a lost SSH job (retried on another worker, or local after
`max_remote_attempts`); the old worker's next post gets 410 and it cancels the job. A queue
stop, a park or an abandon cancels the job (`cancel: true` on the worker's next post).
`nw workers token NAME --rotate` replaces the credential at once and `nw workers rm NAME`
revokes it: either way the worker cancels its jobs and exits non-zero, and their steps retry
on another worker (the attempt counts against `max_remote_attempts`) or run locally. A
rotation charges the worker nothing (no cooldown, no recorded loss); re-enrol it with the new
blob.
Running `nw worker serve` as a service is left to you, e.g. a systemd user unit
(`~/.config/systemd/user/nw-worker.service`, then `systemctl --user enable --now nw-worker`;
exit 3 means revoked, so it is not restarted):

```ini
[Unit]
Description=nightwright pull worker
After=network-online.target

[Service]
ExecStart=%h/.local/bin/nw worker serve
Restart=on-failure
RestartSec=30
RestartPreventExitStatus=3

[Install]
WantedBy=default.target
```

## Remote dashboard

`nw serve --dashboard` serves the phone page: live queue view (SSE with a 15 s polling
fallback, SPEC-0402), the decisions inbox where you answer escalations (choose, text or
override, then confirm; the same write as `nw escalations answer --yes`, SPEC-0429), and a
per-campaign drilldown with a live run tail (tap a campaign, SPEC-0430).

One server serves both places at once (default bind `auto:8791`, SPEC-0432):

- **Loopback, always:** `http://127.0.0.1:8791/`, no login (SPEC-0428; the page is protected
  against other web pages by a Host allowlist for `127.0.0.1` / `localhost` / `[::1]`,
  same-origin checks and a per-page CSRF value, and sends no CORS headers).
- **Tailnet, whenever this host has a Tailscale address:** `http://<tailnet-ip>:8791/` or the
  MagicDNS name, where every request passes the `tailscale whois` identity gate
  (`agentic.dashboard.allowed_logins`) and writes need the CSRF value too. The server checks
  `tailscale ip` every 15 s (`agentic.dashboard.tailnet_recheck_s`): it binds the tailnet
  address as soon as one appears and closes that listener when it goes away; loopback keeps
  serving throughout. A logged-out Tailscale is logged once (`tailnet: waiting for a Tailscale
  address (logged out)`) and is never an exit or a restart.

The auth rules belong to the socket a request arrives on, never to its `Host` header: a request
on the tailnet socket that claims `Host: 127.0.0.1` is refused. `--bind 127.0.0.1:PORT` serves
loopback only; `--bind tailscale[:PORT]` serves the tailnet only (and exits 3 when there is no
address, below).
Every answer, and every refused request, is a line in `<data_dir>/agentic/dashboard/audit.jsonl`
with the login and device (or `local`). `nw dashboard snapshot` prints the snapshot document
without a server. To keep the server running, install it as a **systemd user service** (SPEC-0427):

```
nw dashboard install [--bind auto|loopback|tailscale] [--port N] [--dry-run]
nw dashboard status [--json]        # unit, pid, listeners, code freshness, linger, log command
journalctl --user -u nw-dashboard -f
nw dashboard uninstall              # stop, disable, remove, daemon-reload
```

`install` writes `~/.config/systemd/user/nw-dashboard.service`, then runs `systemctl --user
daemon-reload` and `enable --now` (and `restart` when it was already running). `--dry-run`
prints the unit and the commands. The unit runs the `nw` of your `uv tool` install
(`~/.local/share/uv/tools/nightwright/bin/nw`; `--nw-bin PATH` overrides). It sets
`NW_CONFIG_DIR` / `NW_DATA_DIR` only when your shell resolves them to non-default
directories. Its output goes to the journal. The default `--bind auto` unit runs
`nw serve --dashboard --bind auto:8791`. Re-running `install` replaces the unit and prints what
changed (for example `bind tailscale:8791 (tailnet only) -> auto:8791 (loopback + tailnet)`).

How the unit reacts to the server's exit codes (SPEC-0404):

- **0**: a nightwright code change (the server's code watch, `agentic.dashboard.code_check_s`,
  default 10 s) or a clean stop. `Restart=always` with `RestartSec=5` starts it again on the new
  code. `systemctl --user stop` is not restarted.
- **2**: a refused bind or bad flag. `RestartPreventExitStatus=2` leaves the unit failed; fix
  the cause and re-run `nw dashboard install`.
- **3**: no tailnet address, only for a tailnet-only `--bind tailscale` unit while Tailscale is
  logged out or stopped (the default `auto` unit waits in-process instead). That unit passes
  `--tailnet-wait 60`: the server checks `tailscale ip` every 5 s for up to 60 s,
  then exits 3, and systemd starts it again 5 s later. A Tailscale outage is therefore about
  one restart a minute, and once Tailscale comes up the server binds within 5 s.
  `StartLimitBurst=20` in `StartLimitIntervalSec=300` stops only a real crash loop (exit 1,
  such as a port in use), which the exit-3 rhythm never reaches. The unit orders
  `After=tailscaled.service` when such a unit exists, but never requires it. A user manager
  cannot order against the system `tailscaled.service`, so for the usual install that line has
  no effect and `--tailnet-wait` is the real guard.

`nw dashboard status` prints one line per listener:

```
local    http://127.0.0.1:8791/ (no login)
tailnet  http://100.x.y.z:8791/ (whois)
tailnet  waiting: Tailscale logged out — run: sudo tailscale up     # instead, while logged out
```

It exits 1 on a problem: the unit is installed but not active, it is active but has not written
`server.json` yet, nothing is served (a tailnet-only unit waiting for Tailscale), or it runs
code older than the checkout's HEAD. A waiting tailnet next to a serving loopback is not a
problem. When linger is
off it prints the command that keeps the unit running after you log out,
`loginctl enable-linger $USER`. It never runs that command itself. `nw queue conduct-status`
prints a `dashboard:` line (JSON key `dashboard`), and `nw doctor` has an `agentic.dashboard`
check that warns when the unit is installed but not active.

Config (`agentic.dashboard` in `config.yaml`; a malformed value falls back to its default):
`bind` (`tailscale:8791`, not used by `nw serve`, whose default is `auto:8791`),
`allowed_logins` (`[]` means only your own Tailscale login), `refresh_s` (5), `max_stale_s`
(30), `max_clients` (8 open streams), `tail_poll_s` (1.0), `tailnet_recheck_s` (15) and
`code_check_s` (10; `0` turns the code watch off).

Tailscale, one time, for the phone: `sudo tailscale up` and log in, then
`sudo tailscale set --operator=$USER` so that `tailscale ip/status/whois` work without root.
Install the Tailscale app on the phone with the same account. Then run `nw dashboard install`
(the default bind) and open `http://<host-tailnet-ip>:8791/` (or the MagicDNS name) on the
phone; `http://127.0.0.1:8791/` keeps working on this host. A device on another login, a tagged
node, or any `whois` failure gets 403.
