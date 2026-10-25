# Skills

Agent playbooks shipped with the repo. Humans can ignore them; agents should install
once and follow stage names.

## Install

```bash
wt skills install                 # → ~/.claude/skills (symlink)
wt skills list
wt skills install --dest ~/.cursor/skills   # optional other host
```

## Stage map

| Skill | When |
|-------|------|
| `/wt-orient` | Cold session bootstrap |
| `/wt-capture` / `/wt-seed` | Park a thought / distill a chat |
| `/wt-related` | Search before capture/promote |
| `/wt-explore` | Enrich idea — never promote |
| `/wt-new-work` | Explored idea → accepted spec |
| `/wt-generate` | Accepted internal SPEC → org tasks |
| `/wt-rework` | Supersede bad design / extend epic |
| `/wt-export` | Outbound portable → target repo |
| `/wt-implement-spec` | In **target** repo after export |
| `/wt-verify` | Post-hoc Definition of Done |
| `/wt-sheet-sync` | Sheet plan/record loop |
| `/wt-conduct` | Long-lived conductor woken by queue events ([Agentic loop](agentic.md)) |
| `/wt-steward` | Project steward answering the conductor over the mailbox |

Triage JSON: [Hub](hub.md). Full agent catalog: [Agent surface](../guides/agent-surface.md).
Product narrative for humans: [Guides](../guides/index.md).
