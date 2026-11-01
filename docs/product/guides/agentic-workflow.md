# The normal agentic workflow

This page is the everyday path once nightwright is installed: one project, specs written and
accepted, work queued and delivered by the loop while you make product decisions. For the
first-time walkthrough with checks at each step, see
[Bootstrap from zero](bootstrap.md). For park reasons and recovery, see
[Operating the loop](../loop/operate.md).

## 1 · Register the project

```bash
nw projects add Acme --repo ~/src/acme          # dry run: shows the plan
nw projects add Acme --repo ~/src/acme --yes    # writes outbox_targets.Acme to config.yaml
nw projects                                     # Acme listed with its repo path
```

nightwright's own checkout is the built-in project `Meta-Tools` and needs no registration.

## 2 · Describe it for the loop

```bash
nw projects init Acme          # prints a starter .nw/project.yaml (dry run)
nw projects init Acme --yes    # writes <repo>/.nw/project.yaml
nw projects config Acme        # the resolved file, defaults filled in
```

Edit the file and commit it to the project's main branch. The two parts that matter most:

- `gates`: the commands that prove a change works (tests, lint), each with `required` and
  `timeout_s`. They run in the campaign worktree, all of them, every time.
- `merge.policy`: `merge` (fast-forward into `merge.base`), `pr` or `draft-pr`.

Optional: profiles for this project in `<repo>/.nw/profiles/*.yaml`. Check them with
`nw profiles list` and `nw profiles show NAME --project Acme`.

## 3 · Ideas, then specs

```bash
nw idea capture "CSV export for the weekly report" --project Acme
nw next                                   # what each open idea needs next
```

Explore an idea until someone with no context could build it: `nw idea summary`,
`nw idea questions`, `nw idea log`, or `/nw-explore IDEA-NNN` in an agent session. Then
either:

- **write the spec yourself or with an agent**: `nw spec new --from-idea IDEA-NNN`, fill it
  in (or `/nw-new-work IDEA-NNN`), set `status: accepted`, commit, and run
  `nw spec review <ID>` once; or
- **queue the idea itself**: its campaign starts in `SPEC_WRITING`, where the `spec_writer`
  persona writes the spec and the `spec_reviewer` reviews it before implementation.

See [Write a spec the loop can build](../ideas/spec.md).

## 4 · Start the conductor and a steward

Use two Claude Code sessions:

```bash
nw queue conductor-launch          # session 1: runs /nw-orient, then /nw-conduct
nw queue steward-launch Acme       # session 2, opened in Acme's repo: /nw-steward
```

Both accept `--dry-run` to print the plan first. Both refuse to start until the session model
has a price in `config.yaml` (`agentic.conductor.prices`, keyed by model prefix, e.g.
`claude-: {input: 3, output: 15}`). (Typing `/nw-conduct` or `/nw-steward` in an
existing session also works; the launch commands add a pinned model, forced context limits
and a registered session id.) The
conductor starts the runner (`nw queue run --detach`) if none holds the lease.

## 5 · Queue work through the steward

In the steward session, ask in plain words: "queue the CSV export spec", "bump SPEC-0007 to
priority 1", "merge branch my-fix". The steward does not run queue writes itself. It sends a
request to the conductor over the mailbox, for example:

```bash
nw mailbox send --to conductor --kind request "queue SPEC-0007 priority 2"
```

The conductor acts (`nw queue add … --yes`, `nw queue prioritize`, `nw queue merge`) and
answers; the steward reports back. You can also run `nw queue add SPEC-0007 --yes` yourself.

## 6 · Watch

```bash
nw queue watch             # live dashboard: runner, active campaigns, backlog, delivered
nw queue list              # what is in flight, in which state, at what cost
nw queue backlog           # queued items in admission order, and what each waits on
nw queue conduct-status    # the conductor's situation report: lease, slots, parks, mail
nw queue decisions         # the conductor's decision log
nw tui --tab queue         # the same in the desk (also --tab events, --tab mailbox)
```

## Who decides what

| Agents decide (conductor, steward, personas) | You decide |
|---|---|
| Resuming, retrying or abandoning parked campaigns from the decision table | What gets built: scope, priorities between features, product direction |
| Clearing blockers: dead runner, rebase conflicts, flaky gates, cost-cap raises within 2x | Open escalations: `nw escalations list`, then `nw escalations show ID` |
| Judgment calls inside a spec: defaults, thresholds, option names, small scope additions | Answering them: `nw escalations answer ID --choose N --yes` (or `--text`, or `--waive` / `--attest` with `--reason`) |
| Implementation, gate triage, review and merge of accepted specs | Anything that touches your own checkout |

An escalation parks its campaign as `human_decision`. Your answer wakes the conductor, which
applies it. You can answer in the steward session too; the steward records it with
`nw escalations answer … --by steward:Acme`. Never edit the main checkout while the loop runs:
work on a branch in its own worktree and ask the conductor to merge it.
