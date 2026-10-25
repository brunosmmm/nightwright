# wt

**wt** is two things sharing one CLI:

1. **Passive time tracking** — hours per topic from Claude Code / Cursor transcripts and
   Outlook meetings (no timer for that signal).
2. **Idea → ship** — capture, explore, accept governing specs, generate or export work,
   verify done.

Org `clock-in` / `clock-out` is **supplemental** wall-time on an idea while you build —
it does not replace passive tracking.

> **Retention:** Claude Code deletes old transcripts. Run `wt snapshot` regularly (and
> prefer a longer `cleanupPeriodDays`). Details:
> [`docs/product/concepts/time-and-retention.md`](docs/product/concepts/time-and-retention.md).

## Docs

Themed product site (MkDocs Material):

```bash
scripts/docs-serve.sh          # or: uv run mkdocs serve
```

Source: [`docs/product/`](docs/product/) — [Getting started](docs/product/guides/getting-started.md),
[Track time](docs/product/guides/track-time.md),
[Idea → ship](docs/product/guides/idea-to-ship.md),
[Capabilities](docs/product/capabilities/index.md),
[Command reference](docs/product/reference/index.md).

Dense cheatsheet: [`docs/WORKFLOW.md`](docs/WORKFLOW.md).

## Install

Requires [uv](https://docs.astral.sh/uv/). From this checkout:

```bash
uv sync
uv run wt --help               # must list idea / report / projects (not a static-site tool)
uv tool install --editable .   # PATH entry
uv tool install --editable '.[tui]'   # optional desk: wt tui
uv run pytest
```

Install **editable**: skills, internal specs and the Meta-Tools research root live in this
checkout, and a `git pull` takes effect without reinstalling. For a non-editable install, set
`wt_checkout: /path/to/wt` in `config.yaml` (SPEC-0166).

Config/data live under XDG (`~/.config/wt/`, `~/.local/share/wt/`), not in the repo.

```bash
wt                             # dashboard
wt topics --unmapped
wt report --week
wt idea "…" --project Meta-Tools
wt skills install              # agent playbooks → /wt-orient, …
```

Shell completion: `wt completion install --shell fish` (or bash/zsh).

Agentic loop (accepted specs → implemented, gated, merged by headless personas):
[`docs/product/capabilities/agentic.md`](docs/product/capabilities/agentic.md).

## Contribute

Spec-first: [`AGENTS.md`](AGENTS.md), [`docs/specs/`](docs/specs/),
[`docs/LEDGER.md`](docs/LEDGER.md). Product docs contribution notes:
[`docs/product/contribute/`](docs/product/contribute/index.md).
