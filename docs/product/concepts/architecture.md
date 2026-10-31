# Storage architecture

Where nw state lives. Four stores, one CLI — they do not all sit in the git checkout.

```mermaid
flowchart TB
  subgraph Config["Config — XDG config dir"]
    CY[config.yaml]
    MY[mappings.yaml]
    OY[overrides.yaml]
    SCH[schemes/]
  end
  subgraph Data["Data — XDG data dir"]
    HIST[history.jsonl]
    MEET[meetings.jsonl]
    OUT[outbox per project]
  end
  subgraph Org["Org corpus — configured paths"]
    IDEAS[ideas.org + rotations]
    ARCH[ideas-archive.org]
    TASKS[task/capture .org files]
  end
  subgraph Repo["This checkout — when developing nightwright"]
    SPECS[docs/specs/SPEC-*.md]
    LEDGER[docs/LEDGER.md]
  end
  CLI[nightwright CLI] --> Config
  CLI --> Data
  CLI --> Org
  CLI --> Repo
  OUT -->|nw spec export| Foreign[Foreign repo docs/specs/PROJ-*.md]
```

Defaults (overridable in config):

| Store | Typical path | Holds |
|-------|----------------|-------|
| Config | `~/.config/nightwright/` | mappings, overrides, schemes, user config |
| Data | `~/.local/share/nightwright/` | history, meetings, **outbox** portables |
| Org | paths in `org_files` / `org_ideas_file` | ideas + actionable tasks |
| Package specs | `docs/specs/` in the nightwright repo | **internal** `SPEC-NNNN` only |

## Project (three meanings)

| Sense | Where | Role |
|-------|--------|------|
| **Facet value** | `mappings.yaml` (`bucket=` / `project=`) | Groups passive hours in reports |
| **Idea association** | org `:PROJECT:` on an idea | Routes research root + outbound vs internal |
| **Outbox target** | `outbox_targets` in config | Named foreign repo for `nw spec export` |

Saying “project” without which sense is the usual source of confusion — prefer
**facet**, **idea project**, or **outbox target** in docs and skills.

## Internal vs outbound specs

```mermaid
flowchart LR
  Idea[Idea with Summary] -->|no outbox target| Int[docs/specs/SPEC-NNNN]
  Idea -->|outbox target set| OB[data-dir outbox/PROJ-NNNN]
  Int -->|nw spec generate| Tasks[org tasks in this world]
  OB -->|nw spec export| Port[target repo portable]
  Port -->|pull-status / sweep| Ship[Idea SHIPPED]
```

- **Internal** — governing design for nightwright itself (or work without a foreign repo). Ledger is
  `docs/LEDGER.md`. Close with AC + test plan + `nw spec verify` → `status: done`, idea
  often `PROMOTED` then settled.
- **Outbound** — design for another repo. Source of truth while drafting is the **outbox**;
  after export the portable in the target repo is what implementers edit. Idea goes
  `EXPORTED` → `SHIPPED` when reconcile says the portable is done.

## Related

- Lifecycles and DoD: [Lifecycle](lifecycle.md)
- Time files: [Time model & retention](time-and-retention.md)
- Command walkthroughs: [Workflow architecture](workflows.md)
