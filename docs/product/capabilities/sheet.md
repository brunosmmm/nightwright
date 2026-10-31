# Sheet sync

Optional loop against a project’s thin Google Sheet backlog
(`outbox_targets[project].sheet`).

## Design

`nw sheet` is **JSON-only and never talks to the network**. It computes a sync plan and
records outcomes; the driving agent performs Sheets I/O (see `/nw-sheet-sync`).

## Commands

```bash
nw sheet plan PROJECT             # push + optional pull plan as JSON
nw sheet record …                 # persist a completed run’s outcome
```

Configure the sheet target in nightwright config for that project, then drive the skill. Related:
[Projects & outbound](projects.md).
