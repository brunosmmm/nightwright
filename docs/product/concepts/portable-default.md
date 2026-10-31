# Default portable (`nw-native`)

When nightwright exports work into another repo, the **default** scheme is `nw-native`. That is not
an optional “fancy” format — it **is** nightwright’s lightweight outbound workflow.

## What you get

`nw spec export` (with no `--scheme` override) resolves:

```text
CLI --scheme  >  outbox_targets[project].scheme  >  nw-native
```

For `nw-native`, the target repo receives:

1. **Portable spec** — nearly an identity copy of the outbox markdown (same sections and
   frontmatter shape the author already wrote), plus provenance fields
   (`source_outbound_id`, `source_content_hash`).
2. **Consumption contract** — a short sibling `{name}.contract.md` that tells implementers
   how to treat the portable: it is the working copy for status/AC; run the Test plan; do
   not silently diverge from Design; do not `nw spec generate` on the outbound id.

Required fields on the outbound before export: `id`, `title`, `context`, `decision`,
`acceptance`, `test_plan` (see `nw spec schemes`).

## Why this shape

The portable is a **governing brief**, not a ticket dump:

| Section | Role |
|---------|------|
| Context / Decision / Design | What and why — enough to build without the nightwright outbox |
| Acceptance criteria | Objective “done” checks (checkboxes on the portable) |
| Test plan | How those checks are proven |
| Frontmatter `status` | `accepted` → `in-progress` → `done` in the **target** repo |

nightwright does not install a heavy foreign tracker. It ships markdown that **expects** implement →
verify before anyone claims `done`. Skills `/nw-implement-spec` and `/nw-verify` are the
agent-facing loop; humans use the same AC + Test plan on the file.

**Merged PR ≠ done.** Portable `status: done` only after AC are true and the Test plan has
been executed. Back in nightwright, the idea stays `EXPORTED` until `nw spec pull-status` / `sweep`
mirrors that and advances the idea to `SHIPPED`.

## Built-in schemes

| Scheme | Default? | What it optimizes for |
|--------|----------|------------------------|
| `nw-native` | **Yes** | Full portable + contract; requires Decision + AC + Test plan |
| `plain-md` | No | Flat single-doc remap for simple foreign layouts; weaker required set (Decision / Test plan not required to export) |

List live metadata anytime:

```bash
nw spec schemes
```

Org-specific declarative schemes can live under `<config_dir>/schemes/*.toml` (XDG config).
They do not replace the product default unless you set `scheme:` on the outbox target.

## Related

- Walkthrough: [Outbound](../guides/outbound.md)
- Done matrix: [Lifecycle & DoD](lifecycle.md)
- Agent loop: [Agent surface](../guides/agent-surface.md)
- Internal path (ledger + `nw spec verify`): [Idea → ship](../guides/idea-to-ship.md)
