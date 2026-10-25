# Agent / automation surface

Front door for agents and scripts. Humans can stay on [Daily ops](daily-ops.md); agents
should install skills once, prefer `--json`, and treat verify/reconcile as the claim-done
path ([Lifecycle & DoD](../concepts/lifecycle.md)).

## Install skills

```bash
wt skills install                 # ~/.claude/skills (symlink)
wt skills list
wt skills install --dest ~/.cursor/skills
```

Getting started Path C: [Getting started](getting-started.md).

## Skill → CLI map

| Skill | Primary CLI | Stage |
|-------|-------------|-------|
| `/wt-orient` | `wt --help`, `wt hub --json`, `wt next --json` | Cold start |
| `/wt-capture` | `wt idea "…"` | Park a one-liner |
| `/wt-seed` | `wt idea` + `summary` / `log` | Distill a chat → idea |
| `/wt-related` | `wt ideas --query … --json` | Search before capture/promote |
| `/wt-explore` | `wt idea show\|summary\|questions\|log` | Enrich — **no** promote |
| `/wt-new-work` | `wt spec new --from-idea`, accept, ledger | Explored idea → accepted spec |
| `/wt-generate` | `wt spec generate` | Accepted **internal** SPEC → org tasks |
| `/wt-rework` | supersede / `seed-children` | Bad design or unfinished epic |
| `/wt-export` | `wt spec export` | Outbox portable → target repo |
| `/wt-implement-spec` | (in **target** repo) portable AC + tests | Build exported work |
| `/wt-verify` | `wt spec verify` (+ ledger / pull-status) | Post-hoc Definition of Done |
| `/wt-sheet-sync` | `wt sheet plan\|record --json` | Optional sheet triage |
| `/wt-conduct` | Monitor on `wt queue events --follow --for conductor`; `wt queue conduct-status\|resume`, `wt mailbox send` | Long-lived conductor woken by queue events |
| `/wt-steward` | Monitor on `wt mailbox follow --to steward:<P>`; `wt mailbox send` | Project steward answering the conductor |

Capability stub: [Skills](../capabilities/skills.md).

## JSON schema catalog

Every machine-readable command prints a top-level `"schema"` string. Prefer that over
scraping tables. Shapes below are illustrative — fields evolve; trust the live payload.

| Schema id | Command | Shape (sketch) |
|-----------|---------|----------------|
| `wt.hub.v1` | `wt hub --json` | `{schema, ideas[], internal[], outbound[], stale_ideas, …}` |
| `wt.ideas.v1` | `wt ideas --json` | `{schema, ideas:[{id,state,heading,next,…}]}` |
| `wt.ideas.search.v1` | `wt ideas --query … --json` | `{schema, query, ideas:[…]}` |
| `wt.next.v1` | `wt next --json` | `{schema, ideas:[{id,state,next,…}]}` |
| `wt.idea.v1` | `wt idea show ID --json` | `{schema, id, state, summary, questions, log,…}` |
| `wt.tasks.v1` | `wt tasks --json` | `{schema, tasks:[{id,state,heading,…}]}` |
| `wt.projects.v1` | `wt projects --json` | `{schema, projects:[{name,outbound,research,…}]}` |
| `wt.projects.add.v1` | `wt projects add … --json` | plan payload before mutating config |
| `wt.spec.verify.v1` | `wt spec verify … --json` | verify report (AC / tests / ledger drift) |
| `wt.campaigns.v1` | `wt campaign list --json` | `{schema, campaigns:[{id,spec_id,state,loops,cost_usd,…}]}` |
| `wt.campaign.v1` | `wt campaign show ID --json` | campaign state + `last_events` (SPEC-0171) |
| `wt.campaign.log.v1` | `wt campaign log ID --json` | `{schema, campaign, events:[{seq,ts,type,by,…}]}` |
| `wt.campaign.gate.v1` | `wt queue gates ID --json` | `{schema, campaign, gate_seq, ok, results:[{name,required,ok,exit_code,timed_out,duration_s,log,tail}]}` (SPEC-0173) |
| `wt.queue.v1` | `wt queue list --json` | `{schema, lease:{held,pid,heartbeat_age_s,ttl_s,stale}, slots:{total,busy}, items:[{campaign,spec_id,project,state,loops,cost_usd,cap_usd,park,worktree,branch}]}` (SPEC-0176) |
| `wt.queue.add.v1` | `wt queue add SPEC --json [--yes]` | `{schema, campaign, spec_id, project, profile, base, worktree, branch, …, written}` (dry-run unless `--yes`) |
| `wt.queue.tick.v1` | `wt queue tick --json` / `wt queue run --json-lines` | `{schema, steps:[{campaign,from,to,action,run_id,cost_usd,reason}], admitted, idle}` |
| `wt.queue.park.v1` / `wt.queue.resume.v1` / `wt.queue.abandon.v1` | `wt queue park|resume|abandon ID --json [--yes]` | plan fields + `written` |
| `wt.event.v1` | `wt queue events --follow --for READER` (one per line) | `{schema, id, ts, type, campaign, spec_id, project, by, data:{…}}` — parks, merges, failed runs, watchdog signals, mail (SPEC-0181) |
| `wt.events.v1` | `wt queue events [--since ID] [--tail N]` | `{schema, events:[wt.event.v1…]}` |
| `wt.conduct.status.v1` | `wt queue conduct-status --json` | `{schema, lease, slots, conductor_pid, counts, parked[], active[], unanswered_mail[], recent_events[]}` (SPEC-0182) |
| `wt.message.v1` | `wt mailbox follow --to ADDR` (one per line) / `wt mailbox send --json` | `{schema, id, seq, ts, from, to, kind, campaign, re, text, needs_reply}` (SPEC-0183) |
| `wt.mailbox.list.v1` | `wt mailbox list --to ADDR --json` | `{schema, to, messages:[wt.message.v1…]}` |

Example (hub triage):

```bash
wt hub --json | jq '{schema, idea_count:(.ideas|length), stale:.stale_ideas}'
```

```json
{
  "schema": "wt.hub.v1",
  "ideas": [{"id": "IDEA-001", "state": "INCUBATE", "next": "wt idea summary …"}],
  "internal": [],
  "outbound": [],
  "stale_ideas": 0
}
```

![hub --json capture](../assets/captures/cli-hub.svg)

## Recipes

### Triage what’s open

```bash
wt next --json
wt hub --json
wt ideas --query "outbound" --json
```

### Enrich without promoting

```bash
wt idea show IDEA-NNN --json
wt idea summary IDEA-NNN --set "…"
wt idea questions IDEA-NNN --add "…"
wt idea log IDEA-NNN --note "…"
# skill: /wt-explore — stop before wt spec new
```

### Promote → build (internal)

```bash
wt spec new --from-idea IDEA-NNN          # then author + status: accepted
wt spec generate SPEC-NNNN
wt idea clock-in IDEA-NNN
# …implement…
wt idea clock-out IDEA-NNN
wt spec verify SPEC-NNNN
# skill chain: /wt-new-work → /wt-generate → /wt-verify
```

### Outbound (pointer)

Export and close-the-loop are mandatory ops — see [Outbound](outbound.md) and
`/wt-export` → `/wt-implement-spec` → `/wt-verify` (pull-status / sweep → idea `SHIPPED`).

## Guardrails

- Empty Summary → explore, don’t promote.
- `accepted` means funded WIP, not a draft guess.
- Merged PR ≠ `done`; run verify / reconcile.
- Never paste live user org corpora into tickets; use `--json` slices.

## Related

- [Lifecycle & DoD](../concepts/lifecycle.md) · [Storage architecture](../concepts/architecture.md)
- [Hub](../capabilities/hub.md) · [Daily ops](daily-ops.md) · [Weekly ops](weekly-ops.md)
