# Bootstrap from zero

This guide takes you from nothing to a spec built and merged by the agentic loop. Each step
ends with a **check**, so you know it worked before you move on. Allow about an hour the
first time.

```mermaid
flowchart LR
    S1["1 · Install"] --> S2["2 · Configure"] --> S3["3 · Register<br/>a project"] --> S4["4 · Describe the<br/>project for the loop"]
    S4 --> S5["5 · Capture<br/>an idea"] --> S6["6 · Explore it"] --> S7["7 · Write &<br/>review the spec"] --> S8["8 · Run the loop"] --> S9["9 · Watch it<br/>merge"]
```

## 1 · Install

```bash
git clone <this repo> nightwright && cd nightwright
uv sync                                 # creates .venv
uv tool install --editable '.[tui]'     # puts `nw` on your PATH, editable
nw skills install                       # installs /nw-* skills for Claude Code / Cursor
```

!!! check "Check"
    `nw --help` lists `idea`, `spec`, `queue`, `campaign` and `mailbox`. (A different tool on
    PyPI is also called `nw`. If its help talks about static sites, you have the wrong one.)

Install **editable**. The skills, the internal specs, and the agentic runner all run from
this checkout, and a `git pull` (or a merge by the loop) takes effect without reinstalling.

## 2 · Configure

nightwright keeps its config and data outside the repo (XDG):

| Path | Holds |
|---|---|
| `~/.config/nightwright/config.yaml` | where your org files live, timezone, outbound projects |
| `~/.local/share/nightwright/` | the data dir: campaigns, ledgers, events, mailbox, backups |
| your org dir (e.g. `~/org/`) | `ideas.org`: your ideas, as plain text you own |

Coming from the tool's earlier name, `wt`? Run `nw migrate-from-wt` once. It is a dry run by
default, and `--yes` applies it. It moves `~/.config/wt` and `~/.local/share/wt` here and fixes
the paths in campaign state. Then run `nw skills install` to replace the old `wt-*` skills and
allow rules.

```yaml
# ~/.config/nightwright/config.yaml (minimal)
org_files: [~/org]
org_ideas_file: ~/org/ideas.org
timezone: Europe/Lisbon
```

!!! check "Check"
    `nw idea "hello nightwright"` prints `✓ IDEA-001 …`, and `nw ideas` lists it.

## 3 · Register a project

Ideas and campaigns belong to a **project**, a repository the loop works on. nightwright's own repo
is the built-in project `Meta-Tools`. Register others by path:

```bash
nw projects add Acme --repo ~/src/acme --yes
nw projects                      # every project: topics, outbound, repo
```

!!! check "Check"
    `nw projects` shows `Acme`, with its repo path.

## 4 · Describe the project for the loop

The loop needs to know how to test and merge your code. That lives **in the repo**, in
`.nw/project.yaml`:

```bash
nw projects init Acme            # prints a starter (dry run)
nw projects init Acme --yes      # writes <repo>/.nw/project.yaml
```

```yaml
# <repo>/.nw/project.yaml
schema: nw.project.v1
project: Acme
base: main
gates:                     # run in the campaign's worktree; required ones must pass
  - name: tests
    cmd: uv run pytest -q
    timeout_s: 1200
    required: true
  - name: lint
    cmd: ruff check .
    required: false
merge:
  policy: merge            # merge | pr | draft-pr
worktrees:
  setup: [uv sync]         # run once in each new worktree
```

Commit this file. It's part of the project, and campaigns read it from main. (a campaign
branch may only raise a gate's `timeout_s`, up to `gate_budget_s`).

!!! check "Check"
    `nw projects config Acme` prints the resolved file with the defaults filled in, and no
    errors.

## 5 · Capture an idea

```bash
nw idea capture "CSV export for the weekly report" --project Acme --kind improvement
```

!!! check "Check"
    `nw next` shows it with a suggested next command.

See [Capture ideas](../ideas/capture.md) for the details.

## 6 · Explore it

Write down what you learn until the idea could be built by someone with no context:

```bash
nw idea summary IDEA-002 --set "Weekly report has no machine-readable export. Add
  'nw report --week --csv': one row per project+topic, hours as decimals. Out of scope:
  XLSX, scheduling."
nw idea questions IDEA-002 --add "Include zero-hour topics?"
nw idea log IDEA-002 --note "report.py builds rows in week_rows(); reuse it"
nw idea questions IDEA-002 --resolve 1        # once decided (record the answer in Summary)
```

Or, in an agent session: `/nw-explore IDEA-002`.

!!! check "Check"
    Your answer to "is it ready?" on [Explore](../ideas/explore.md#is-it-ready) is yes.

## 7 · Write and review the spec

```bash
nw spec new --from-idea IDEA-002      # scaffold docs/specs/NNNN-….md in the project repo
```

Then fill in the Decision, Design, Acceptance criteria and Test plan, or let an agent do it
with `/nw-new-work IDEA-002`, and set `status: accepted`. Commit it to main. Then:

```bash
nw spec review SPEC-0001              # approve | revise | split, with findings
```

Fix findings until it approves. See
[Write a spec the loop can build](../ideas/spec.md).

!!! check "Check"
    `nw spec review SPEC-0001` says `approve`.

## 8 · Run the loop

```bash
nw queue add SPEC-0001                # dry run: shows the plan
nw queue add SPEC-0001 --yes          # queue it
```

Then, in Claude Code, start the conductor: `/nw-conduct`. It starts the runner
(`nw queue run`) if it isn't running, and watches every event from then on.

Prefer to have nightwright start the conductor session for you? `nw queue conductor-launch`
does, but it exits 1 until `config.yaml` has a price for the model, for example
`agentic: {conductor: {prices: {"claude-": {input: 3, output: 15}}}}` (dollars per million
tokens, keys are model-name prefixes). See
[Operating the loop](../loop/operate.md#launching-the-conductor-and-stewards).

!!! check "Check"
    `nw queue list` shows the campaign, and within a minute it moves from QUEUED to OPENING
    to IMPLEMENTING.

## 9 · Watch it merge

```bash
nw queue watch                        # live dashboard
nw campaign log SPEC-0001-… --tail 20 # the story so far
```

You'll see it implement, run your gates, get reviewed, rebase onto main, re-run the gates
and merge. If it parks, the reason tells you why. See
[when something parks](../loop/operate.md#when-something-parks).

!!! check "Check"
    The campaign ends `MERGED`, and `git log` on main shows the implementer's commits.

## Where next

- **Make your ideas better inputs:** [Ideas to specs](../ideas/index.md).
- **Understand the machine you just ran:** [How the agentic loop works](../loop/index.md).
- **Track the hours around the work:** [Track time](track-time.md).
