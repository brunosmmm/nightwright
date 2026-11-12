# nightwright

**nightwright** (CLI `nw`) runs a spec-first, agentic development pipeline. Ideas become
governing specs. Accepted specs are written, implemented, gated, reviewed and merged by
headless Claude or Cursor personas on a deterministic queue, and a human is pulled in only when
the queue parks something. The same CLI also does **passive time tracking**. This repo is
built with nightwright itself: read [`AGENTS.md`](AGENTS.md) before changing anything.

## How work flows

```
idea ───► spec ───► queue ─────────────────────────────────────► merged / PR
nw idea   nw spec new    nw queue add → one campaign per spec or idea
          docs/specs/    SPEC_WRITING → SPEC_REVIEW      (only when an idea is queued)
          LEDGER.md      IMPLEMENTING → GATING ⇄ TRIAGING (a gate failed)
                         REVIEWING    → CLOSING → MERGED | HANDED_OFF (PR)
                         any active state can PARK and wait for a decision
```

1. **Ideas** live in an org file (`org_ideas_file`). Capture and explore them, then promote
   one with `nw spec new` or `/nw-new-work`.
2. **Specs** in [`docs/specs/`](docs/specs/) are the governing design: schema-checked
   frontmatter ([`SCHEMA.md`](docs/specs/SCHEMA.md)), a lifecycle (`draft → proposed →
   accepted → in-progress → done`) and a generated index, [`docs/LEDGER.md`](docs/LEDGER.md).
3. **The queue** turns each queued item into a **campaign** with its own git worktree and an
   append-only ledger under `<data_dir>/agentic/campaigns/`. Each judgment step is a fresh (or,
   with `lean.sessions`, resumed) headless persona run (`spec_writer`, `spec_reviewer`, `implementer`, `gate_triager`,
   `conformance_reviewer`, `test_reviewer`, `adversarial_reviewer`, `review_triager`,
   `conflict_resolver`, plus the lean-loop `review_marshal` and `findings_collator`). Plain code picks the next state.
4. **Gates** are the project's own commands from `<repo>/.nw/project.yaml`, run in the
   worktree under a host-wide gate-slot pool. **Close** rebases, re-gates, checks the spec's
   Definition of Done, then fast-forward merges or opens a PR (`merge.policy`).
5. **Workflow profiles** set models, effort, budgets, loop limits and slots. Builtins
   (`balanced`, `frugal`, `thorough`) ship as package YAML; `<config_dir>/profiles/` and
   `<repo>/.nw/profiles/` layer over them. The spec reviewer rates size (`xs`..`xl`) and tone
   (`cost-effective`, `high-end`, `critical`), and a size x tone matrix picks the profile.
6. **Coordination.** A **conductor** session watches the queue's event stream and resolves
   parks, failures and merges from a decision table. A **steward** per project answers its
   questions. They talk over a file **mailbox** (`<data_dir>/agentic/mailbox/`); decisions
   that need a human become **escalations**.

**Remote workers** ship gate dispatch, persona dispatch, pull workers, and `nw gate` for a campaign-less run.
`nw worker` is the job runner and `nw workers` is the registry. Still open is federation: a project host
running its own queue. A declarative pipeline definition is planned.

## Install and start working

Needs git, [uv](https://docs.astral.sh/uv/) and the `claude` CLI. Optional: `cursor-agent`
(`cursor-agent login`) for the Cursor backend. `gh` if a project merges by PR.

```bash
git clone https://github.com/brunosmmm/nightwright.git && cd nightwright   # or git@github.com:brunosmmm/nightwright.git
uv sync && uv run nw --help           # lists idea / spec / queue / report …
uv tool install --editable.          # `nw` on PATH (`uv tool update-shell` if it is not)
nw skills install && nw completion install --shell fish   # skills -> ~/.claude/skills; shell: fish/bash/zsh
nw projects add Acme --repo ~/src/acme --yes && nw projects init Acme --yes
nw doctor --fix-hints                 # read-only install check; prints the fix for each finding
nw queue conductor-launch             # then `nw queue steward-launch Acme` in a second terminal
```

Before launching, create `~/.config/nightwright/config.yaml` with at least `timezone:` and
`agentic.conductor.prices` (Claude prefixes, e.g. `claude-: {input: 3, output: 15}`; Cursor
launches skip the USD table). Ideas go to `org_ideas_file` (default `~/work/org/ai/ideas.org`).
Data lives in `~/.local/share/nightwright/`; `NW_CONFIG_DIR` / `NW_DATA_DIR` override both.
Install **editable** so `git pull` updates skills and specs (else set `nw_checkout:`). Coming
from `wt`? Run `nw migrate-from-wt` once. Next: [Run the agentic loop](docs/product/guides/agentic-workflow.md).

## Quick tour

```bash
nw idea "retry flaky gates once" --project Meta-Tools
nw next                                 # next-step hints (`nw hub --json`: triage)
nw spec new --from-idea IDEA-00N        # promote an idea to a draft spec
nw spec review SPEC-NNNN                # spec_reviewer verdict, size and tone
nw queue add SPEC-NNNN --yes            # also IDEA-NNN, or --epic SPEC-NNNN
nw queue run --detach                   # lease-guarded runner; `nw queue stop` ends it
nw queue watch                          # live dashboard (`list`, `backlog`, `gate-slots`)
nw campaign show CAMPAIGN               # state + recent ledger events
nw queue resume CAMPAIGN                # continue a parked campaign
nw queue merge my-branch --yes          # rebase + re-gate + ff-merge a local branch
nw profiles list                        # then `show NAME` or `check`
nw queue conductor-launch               # /nw-conduct (`--runner cursor` opt-in); `steward-launch PROJECT`
nw escalations list                     # `answer ID` wakes the conductor; also `nw mailbox`
nw tui                                  # ideas desk; --tab queue|events|mailbox, --campaign ID
```

## Agent skills

`nw skills install` links the playbooks in [`skills/`](skills/) into Claude Code / Cursor.
`/nw-orient` is the bootstrap; load it first. `/nw-conduct` and `/nw-steward` turn a session
into the conductor or a project steward. `/nw-capture`, `/nw-seed` and `/nw-related` record
ideas; `/nw-explore` enriches one; `/nw-new-work` and `/nw-rework` make or supersede a spec;
`/nw-verify` audits a claimed-done spec. `/nw-generate`, `/nw-export`, `/nw-implement-spec`
and `/nw-sheet-sync` cover org tasks, outbound specs for other repos and external backlogs.

## Passive time tracking

Hours per topic come from Claude Code / Cursor transcripts and Outlook meetings, no timer.

```bash
nw                          # today + this-week dashboard
nw report --week
nw topics --unmapped        # topics that still need a mapping
nw snapshot                 # archive past days to history.jsonl
```

> **Retention:** Claude Code deletes old transcripts. Run `nw snapshot` regularly (and prefer
> a longer `cleanupPeriodDays`). See [time and retention](docs/product/concepts/time-and-retention.md).

## Docs and contributing

- Product site (MkDocs Material) in [`docs/product/`](docs/product/): `scripts/docs-serve.sh`
  or `uv run mkdocs serve`. Start at [Bootstrap](docs/product/guides/bootstrap.md), then
  [the loop](docs/product/loop/index.md), [agentic reference](docs/product/capabilities/agentic.md)
  and [commands](docs/product/reference/index.md).
- Cheatsheet [`docs/WORKFLOW.md`](docs/WORKFLOW.md); spec governance [`docs/README.md`](docs/README.md),
  [`docs/specs/SCHEMA.md`](docs/specs/SCHEMA.md), [`docs/LEDGER.md`](docs/LEDGER.md).
- Spec-first contract and Definition of Done: [`AGENTS.md`](AGENTS.md); docs notes in
  [`docs/product/contribute/`](docs/product/contribute/index.md). Before committing, run
  `python3 tools/spec_lint.py` (`--write-ledger` regenerates the ledger).
