# Desk (TUI)

Textual **ideas desk** for humans (Textual is a core dependency). Agents stay on CLI + `--json`.

## Install

```bash
uv sync                             # checkout
uv tool install --editable.        # PATH entry from this checkout
nw tui
```

If Textual is missing anyway (a broken install), `nw tui` prints an install hint and exits 1.
The `tui` extra remains as an empty alias, so `--extra tui` / `'.[tui]'` still work.

## Tabs

`nw tui --tab NAME` opens on a tab; keys `1`..`5` switch between them.

| Tab | Shows |
|-----|-------|
| `ideas` | The ideas desk (default). Key `1` |
| `hub` | The triage union of ideas, specs and outbound ([Hub](hub.md)). Key `2` |
| `queue` | The live agentic queue. Key `3` |
| `mailbox` | Conductor/steward/human inboxes and threads; reply as the human. Key `4` |
| `events` | The queue event stream and watchdog conditions. Key `5` |

`nw tui --campaign ID` opens one campaign's timeline, runs, gates and reviews.

## What the ideas tab does

Browse / filter / search ideas, capture, edit metadata (project, state, tags), toggle
clock (`i`), open detail. Project chooser uses known projects from config + payload
(empty chooser notifies instead of hanging).

Demo desk (hard-refresh if this still looks empty — browsers cache the SVG). Fixture has
eight ideas with project/epic/priority, open clock, and a filled Summary / Questions / Log:

![nw tui ideas desk](../assets/captures/tui-desk.svg?v=4)

See `nw tui --help` for keys and flags. Triage without a TTY: [Hub](hub.md) /
[Ideas & next](ideas.md).
