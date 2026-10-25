# Multi-project

When work spans **more than one** tracked repo, do not collapse everything under a single
`:PROJECT:` / research root.

## Map first

```bash
wt projects --json
```

![projects](../assets/captures/cli-projects.svg)

Confirm which names are internal (this wt checkout) vs outbound (`repo_path` set). Unknown
paths → [Outbound](outbound.md) registration (`wt projects add …` dry-run, then `--yes`).

## Epic fan-out with `seed-children`

1. Promote an **epic** for the cross-cutting story (coordinating project, often Meta-Tools).
2. In the epic Breakdown, one bullet per child with `(project: Name)` when routing differs.
3. After the epic is `accepted`:

   ```bash
   wt spec seed-children SPEC-NNNN
   ```

   That seeds `INCUBATE` child ideas stamped with `:EPIC:` + `:PROJECT:` from each bullet.
4. Promote each child to its own spec (internal `SPEC-…` or outbox `PROJ-…`).
5. Explore and implement **per child** — never one Summary that pretends one research root
   covers every repo.

`wt next` / `wt hub --json` show the fan-out as separate rows on purpose.

## Soft rules

- Naming ≥2 projects in a chat → show the project map before locking Summary.
- Incomplete epics: extend with `/wt-rework` + `seed-children`, not only ad-hoc tasks.
- Each outbound child still needs **export → implement → pull-status/sweep → SHIPPED**
  ([Outbound](outbound.md)).

Back to [Idea → ship](idea-to-ship.md) or [Outbound](outbound.md) per child.
