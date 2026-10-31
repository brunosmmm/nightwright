# Desk (TUI)

Optional Textual **ideas desk** for humans. Agents stay on CLI + `--json`.

## Install

```bash
uv sync --extra tui                 # checkout
uv tool install --editable '.[tui]'   # PATH entry from this checkout
nw tui
```

Without the extra, `nw tui` prints an install hint and exits 1.

## What it does

Browse / filter / search ideas, capture, edit metadata (project, state, tags), toggle
clock (`i`), open detail. Project chooser uses known projects from config + payload
(empty chooser notifies instead of hanging).

Demo desk (hard-refresh if this still looks empty — browsers cache the SVG). Fixture has
eight ideas with project/epic/priority, open clock, and a filled Summary / Questions / Log:

![nw tui ideas desk](../assets/captures/tui-desk.svg?v=4)

See `nw tui --help` for keys and flags. Triage without a TTY: [Hub](hub.md) /
[Ideas & next](ideas.md).
