# Skills

Agent playbooks shipped with the repo. Humans can ignore them; agents should install
once and follow stage names.

## Install

```bash
nw skills install                 # → ~/.claude/skills (symlink)
nw skills list
nw skills install --dest ~/.cursor/skills   # optional other host
```

## Stage map

| Skill | When |
|-------|------|
| `/nw-orient` | Cold session bootstrap |
| `/nw-capture` / `/nw-seed` | Park a thought / distill a chat |
| `/nw-related` | Search before capture/promote |
| `/nw-explore` | Enrich idea — never promote |
| `/nw-new-work` | Explored idea → accepted spec |
| `/nw-generate` | Accepted internal SPEC → org tasks |
| `/nw-rework` | Supersede bad design / extend epic |
| `/nw-export` | Outbound portable → target repo |
| `/nw-implement-spec` | In **target** repo after export |
| `/nw-verify` | Post-hoc Definition of Done |
| `/nw-sheet-sync` | Sheet plan/record loop |
| `/nw-conduct` | Long-lived conductor woken by queue events ([Agentic loop](agentic.md)) |
| `/nw-steward` | Project steward answering the conductor over the mailbox |

Triage JSON: [Hub](hub.md). Full agent catalog: [Agent surface](../guides/agent-surface.md).
Product narrative for humans: [Guides](../guides/index.md).
