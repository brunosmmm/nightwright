# Projects & outbound

Register where explore research and export land, then hand portables to other repos.

## When to use

Any idea with a `:PROJECT:` that is not Meta-Tools / this checkout; multi-repo epics.

## Commands

| Command | Role |
|---------|------|
| `nw projects` | List known projects (`--json` for agents) |
| `nw projects add NAME --repo PATH` | Dry-run plan; `--yes` writes / retargets `repo_path` |
| `nw projects rename OLD NEW` | Dry-run bucket rename; `--yes` moves config key + outbox dir + idea `:PROJECT:` (ids unchanged in v1) |
| `nw spec new --from-idea …` | Routes to outbox when project is outbound |
| `nw spec export` | Copy portable into target `docs/specs/`; idea → EXPORTED |
| `nw spec pull-status` / `sweep` | Mirror done → SHIPPED |
| `nw spec schemes` | List export schemes |
| `nw spec fit-log` | Post-ship fit notes |

Workflow: [Outbound](../guides/outbound.md), [Multi-project](../guides/multi-project.md).
