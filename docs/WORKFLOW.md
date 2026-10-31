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
nw campaign log ID   every transition, run, gate, finding and dollar of one campaign
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
| `INCUBATE` | exploring (durable notes on the idea) | `/nw-explore` / `nw idea log` — **not** promote yet ([SPEC-0024](./specs/0024-formal-explore-stage-nw-explore-skill.md), [SPEC-0031](./specs/0031-rename-idea-explore-to-log-datetime-log-stamps.md)) |
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

## Optional interactive desk (`nw tui`)

`nw tui` is a human-only ideas desk behind the optional `tui` extra (SPEC-0098/0099). The
default install stays click/rich/pyyaml/orgparse; agents keep the classic commands and
`--json`.

```bash
uv sync --extra tui          # from a checkout
uv tool install --editable '.[tui]'   # from the nightwright checkout (NOT PyPI `nw` — that is a different tool)
# editable: skills/specs/Meta-Tools resolve from the checkout; non-editable needs nw_checkout: in config
nw tui                       # without the extra: install hint + exit 1
```

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
