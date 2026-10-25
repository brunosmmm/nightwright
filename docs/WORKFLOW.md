# wt workflow — idea → shipped work

One-page cheatsheet for **humans**. Agent playbooks live in `skills/` (`wt-orient`,
`wt-new-work`, …); this page is what you keep in muscle memory.

## The pipeline

```
capture → explore (durable) → decide → author → generate → build → verify → (export if outbound)

wt idea "…"          park a thought (seconds)
/wt-seed             Cursor: distill conversation → idea (+ optional Summary/Log)
wt idea log …        append a Log note (date+time); was `wt idea explore`
wt idea summary …    set durable Summary (gate for promote)
                     bodies are org: quote code/paths/identifiers as ~x~ or =x= (left
                     untouched); multi-line code as #+begin_src (SPEC-0164)
wt idea show …       read headline + Summary / questions / Log
                     (TTY: Rich+Pygments org highlight; --plain / --json stay plain;
                     theme via config idea_show_theme, default nord)
wt ideas / wt next   triage; next says log/summary before promote
wt ideas --query …   related-idea search over open+closed (SPEC-0086)
wt ideas --json      agents: structured list (also: wt next --json, wt idea show ID --json)
/wt-related          Cursor: search before capture/promote
/wt-explore          Cursor: enrich idea, never promote
/wt-new-work         Cursor: explored idea → accepted spec (post-explore)
/wt-generate         Cursor: accepted spec → org tasks
implement            you or the agent
/wt-verify           Cursor: post-hoc DoD audit (global) — internal: `wt spec verify`;
                     outbound portable: audit the file in the target repo
/wt-rework           redesign (supersede) or extend incomplete epic
/wt-export           only if outbound → another repo
```

In a Cursor/Claude session you can say the stage instead of remembering flags:
“new work on IDEA-003”, “generate SPEC-0023”, “export ACME-0001”.

**Superpowers coexistence (SPEC-0138):** if Cursor injects Superpowers skills, they are
tactics only. Governing design stays in wt (`docs/specs/` / outbox portables). Agents:
`/wt-orient` — do not write governing docs under `docs/superpowers/`; “keep working” →
`wt hub` / `wt next`; claim done via `/wt-verify`.

## Idea states → what you do

| State | Meaning | Your move |
|-------|---------|-----------|
| `IDEA` | parked, not yet incubating | capture; optionally start explore |
| `INCUBATE` | exploring (durable notes on the idea) | `/wt-explore` / `wt idea log` — **not** promote yet ([SPEC-0024](./specs/0024-formal-explore-stage-wt-explore-skill.md), [SPEC-0031](./specs/0031-rename-idea-explore-to-log-datetime-log-stamps.md)) |
| `SPECCED` | a draft/accepted spec exists (`:SPEC:` set) | finish authoring → `accepted`, then generate (internal) or export (outbound); if design wrong or epic incomplete → `/wt-rework` |
| `PROMOTED` | internal: org tasks exist (`wt spec generate`) | build; track time against tasks; incomplete epic → `/wt-rework` (extend), not bare `wt tasks` |
| `EXPORTED` | outbound: handed off via `wt spec export` | build in the **target repo** on the portable spec (update `status` + AC); `wt spec pull-status` / `sweep` when portable is `done` → **SHIPPED** |
| `SHIPPED` | finished (post-export or outbound reconcile) | ignore (`wt ideas` hides these by default); force via `wt idea state … SHIPPED` or desk `m` → state |
| `DROPPED` | dead | ignore (`wt ideas` hides these by default) |
| `RESEARCHED` | explored, no implementation | ignore |

```bash
wt ideas                 # open only
wt ideas --all           # include PROMOTED/EXPORTED/SHIPPED/DROPPED/…
wt idea state IDEA-00N SHIPPED   # force lifecycle keyword (SPEC-0135)
wt state IDEA-00N DROPPED        # generic org state (any task)
COLUMNS=200 wt ideas | less -S    # piping: Rich falls back to 80 cols without COLUMNS
```

## Internal vs outbound

| Association | Where the spec lands | After `accepted` |
|-------------|----------------------|------------------|
| No project, or Meta-Tools / this repo | `docs/specs/SPEC-NNNN` | `wt spec generate` → **PROMOTED** → implement here |
| `:PROJECT:` with `outbox_targets` entry | `<data_dir>/outbox/<project>/…` | `wt spec export` → **EXPORTED** → implement in target (portable working copy); `pull-status`/`sweep` when portable `done` → **SHIPPED** |

`outbox_targets[project].repo_path` is also the **explore research root** for that project
(`wt idea show` → `research`; SPEC-0042). Meta-Tools is internal — explore uses this wt repo.
Projects with a `:PROJECT:` but no usable `repo_path` get `research.root: null` / `outbound:
false` until you register them (or explore from conversation only).

```bash
wt idea "…" --project Meta-Tools          # internal tooling
wt idea "…" --project Example               # outbound if Example has a repo_path
wt projects --json                        # agent map: name → research/outbound (SPEC-0095)
# register a missing project (SPEC-0137) — dry-run first, then --yes
wt projects add Example --repo ~/work/example-repo           # prints plan; does not write
wt projects add Example --repo ~/work/example-repo --yes     # commits to ~/.config/wt/config.yaml
wt projects add StubName --yes                      # association stub (outbound: false)
wt spec new --from-idea IDEA-00N          # routing follows the idea's :PROJECT:
wt spec new --from-idea IDEA-00N --internal   # force docs/specs/ even with a project
```

**Multi-project chats (SPEC-0095):** if the conversation names ≥2 tracked projects or paths,
preview a project map (`wt projects --json`), fan out as an epic + per-project children, and
do not write Summary until that map is confirmed. Unknown paths → `wt projects add …` (confirm
plan, then `--yes`). Soft-warn if continuing a single-root explore anyway.

## Commands you actually need

```bash
# Capture — ideas are seeds; actionable work is `wt add` (SPEC-0091)
wt idea "short thought" --project Meta-Tools --tag ergonomics
wt add "fix the flaky CI gate" --project Meta-Tools     # actionable → task
wt idea "systemic gate design" --kind bug --force-idea --project Meta-Tools
# `--kind bug` without --force-idea soft-warns and suggests `wt add` (still writes idea)
wt ideas --kind bug                                       # filter; kinds ≠ TODO states

# Triage
wt ideas
wt agenda                    # dated tasks for today

# Spec (CLI; or use /wt-new-work in Cursor)
wt spec new --from-idea IDEA-00N
# …author Decision/Design/AC/Test plan…
# set status: accepted in the spec frontmatter
python3 tools/spec_lint.py --write-ledger

# Tasks + build (internal)
wt spec generate SPEC-NNNN
wt tasks
# implement; mark tasks done with `wt done …`

# Outbound only (do NOT generate on PROJ-NNNN)
# Default scheme is wt-native: portable + contract; AC + Test plan; verify before done
# (docs/product/concepts/portable-default.md). Override with --scheme / outbox target.
wt spec export PROJ-NNNN          # → idea EXPORTED; portable file in target repo
# foreign agent updates portable status / AC in the target; close with /wt-verify
wt spec pull-status PROJ-NNNN     # required when portable is done → idea SHIPPED
wt spec schemes                   # wt-native (default), plain-md
# Optional id pin when a scheme allocates X.Y.Z ids:
# neighbors (e.g. 1.3.2 after 1.3.1) without juggling task_base —
#   frontmatter: task_id: "1.3.2"
#   or: wt spec export PROJ-NNNN --scheme wt-native --task-id 1.3.2 --to …
```

Exact flags: `wt <cmd> --help`. Bare `wt idea TEXT` captures; `wt idea show|log|…`
enrich/read; listing is `wt ideas`.

## Optional interactive desk (`wt tui`)

`wt tui` is a human-only ideas desk behind the optional `tui` extra (SPEC-0098/0099). The
default install stays click/rich/pyyaml/orgparse; agents keep the classic commands and
`--json`.

```bash
uv sync --extra tui          # from a checkout
uv tool install '.[tui]'     # from the wt checkout (NOT PyPI `wt` — that is a different tool)
# or: uv tool install --editable '.[tui]'
wt tui                       # without the extra: install hint + exit 1
```

## Agent skills (installed once)

```bash
wt skills install            # symlink into ~/.claude/skills
wt skills list
# Optional: Cursor / other hosts — same skills, different dest
# wt skills install --dest ~/.cursor/skills
```

| Skill | When |
|-------|------|
| `wt-orient` | Cold session; read once instead of `src/wt/` |
| `wt-capture` | Park a one-liner without breaking flow |
| `wt-seed` | Distill conversation → idea (+ optional Summary/Log) |
| `wt-related` | Search open+closed ideas before capture/promote |
| `wt-explore` | Enrich an idea durably (Summary/Log) — **never** promote |
| `wt-new-work` | Explored idea → lint-clean **accepted** spec (post-explore) |
| `wt-generate` | Accepted **internal** SPEC-NNNN → org tasks |
| `wt-rework` | Linked spec wrong → supersede; incomplete epic → scaffold children |
| `wt-export` | Outbound spec → target repo |
| `wt-implement-spec` | In the **target** repo: implement an exported portable spec (AC + Test plan + `status`) |

**Triage:** `wt hub --json` unions open ideas + active internal specs + outbound outbox
(`wt.hub.v1`). No MCP server — use skills + CLI `--json` / hub (SPEC-0053).

After `wt spec export`, tell the foreign agent `/wt-implement-spec` (requires `wt skills
install` on that machine) instead of a bare “implement”. Record post-ship fit with
`wt spec fit-log <OUTBOUND-ID> --note "…"`. Outbound AC should include an outcome (“I can …”)
or `outcome:` (SPEC-0048).


## Day-to-day ritual

1. **Thought while coding** → `wt idea "…"` (don’t open org by hand). After a design chat →
   `/wt-seed`.
2. **Morning** → `wt ideas` + `wt agenda`. Promote one ripe idea or leave it.
3. **Transitions** → let `/wt-new-work` / `/wt-generate` drive the CLI; you pick *which* idea.

Optional fish abbreviations:

```fish
abbr wi 'wt idea'
abbr wis 'wt ideas'
abbr wn 'wt next'
abbr wtoday 'wt ideas; and wt agenda; and wt next'
```

## CLI next-step hints

```bash
wt next                 # open ideas + recommended command per row
wt next --all           # also PROMOTED / EXPORTED (build reminders)
wt ideas --json         # the hint per row, for agents (no `next` column on the table)
```

`wt next` is the home of the hint. `wt ideas` dropped its `next` column (SPEC-0075) so the
headline gets that width back; the hint is still in `wt ideas --json`.

Hints follow the state table above (`wt spec new …`, `author … → accepted`,
`wt spec generate …` for internal, `wt spec export …` / `implement in …` /
`wt spec pull-status …` for outbound, `/wt-rework …` for incomplete epics,
`wt tasks` for PROMOTED). Copy/paste the `next` cell.

## Shell completion (when extending the CLI)

Fish/bash/zsh scripts are **live** (SPEC-0022) — new subcommands show up without rewriting
completion files. New closed-set options/arguments still need `shell_complete=…` in
`cli.py` (e.g. idea `selector` → `complete_idea_id`). See README “Shell completion” and
`REQUIRED_SHELL_COMPLETE_NAMES` in `src/wt/completion.py` (enforced by pytest, SPEC-0039).
