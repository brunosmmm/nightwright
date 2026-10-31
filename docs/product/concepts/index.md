# Concepts

Core vocabulary for nightwright product docs. Prefer these terms; link SPECs only when explaining *why*.

| Term | Meaning |
|------|---------|
| **Topic** | Unit of passive time (repo path, branch label, or similar base). |
| **Facet / mapping** | Label on a topic (`bucket=…`, `project=…`, …) in `mappings.yaml`. |
| **Override** | One-off time-range reclassification of a session. |
| **Idea** | Org headline (`IDEA-NNN`) — parked or incubating thought. |
| **Summary / questions / Log** | Enrichment on an idea; Summary gates promote. |
| **Spec** | Numbered design (`SPEC-NNNN` or outbound `PROJ-NNNN`) with lifecycle + AC. |
| **Ledger** | Index of internal specs (`docs/LEDGER.md`). |
| **Internal** | Spec in this nightwright repo; generate → org tasks → often `PROMOTED`. |
| **Outbound** | Spec in data-dir outbox; export → foreign portable. |
| **Portable** | Exported markdown spec in a foreign repo (working copy for build + status). |
| **`nw-native`** | **Default** export scheme: portable + contract; AC + Test plan; verify before `done`. |
| **`plain-md`** | Optional flat export remap for simple foreign layouts. |
| **Outbox** | Per-project dir under the XDG data dir for outbound drafts. |
| **Hub** | Read-only triage across ideas + specs (`nw hub --json`). |
| **Desk / TUI** | Interactive ideas UI (`nw tui`). |
| **Project (facet)** | Mapping axis value for hours. |
| **Project (idea)** | `:PROJECT:` on an idea — research root + routing. |
| **Outbox target** | Configured foreign repo name for export. |
| **Clock-in** | Supplemental org CLOCK on an idea (not passive hours). |
| **Passive tracking** | Hours from transcripts/meetings — no start/stop timer. |
| **RESEARCHED** | Terminal idea state: explored, **no** spec. |
| **Skill** | Agent playbook installed via `nw skills`. |
| **Sheet sync** | Optional Google Sheet triage for a configured project. |

## Concept pages

| Page | Contents |
|------|----------|
| [Storage architecture](architecture.md) | Config / data / org / specs topology; project senses |
| [Lifecycle & DoD](lifecycle.md) | Idea keywords, spec status, done matrix |
| [Default portable (`nw-native`)](portable-default.md) | Default outbound scheme = lightweight verify workflow |
| [Workflow architecture](workflows.md) | Dual systems + sequence diagrams |
| [Time model & retention](time-and-retention.md) | Passive history, snapshots, meetings |

See also [Guides](../guides/index.md) and [Capabilities](../capabilities/index.md).
