---
name: nw-sheet-sync
description: "Sync nw ideas against a project's thin external triage backlog (a Google Sheet), one project at a time. Use when a project has a configured outbox_targets[project].sheet target and you want to push new/changed opted-in ideas as rows and optionally adopt new sheet rows as ideas."
trigger: syncing nw ideas with a project's Google Sheet backlog
---

# nw-sheet-sync

## When to use

A project (e.g. Example) has an external, thin pre-triage backlog living in a Google Sheet, and
you want it to stay roughly in sync with nightwright's own idea corpus for that project — without nightwright
ever talking to the network itself (SPEC-0123/0124/0125/0126). Use this when you're about to
run (or have been asked to run) a sync pass for a specific project.

**`nw` never calls the Sheets API.** It only computes a plan and records an outcome, both as
plain JSON. You (the driving agent, with your own Google Sheets tools) do every actual read/
write against the sheet, in between those two calls.

## Procedure

1. **Confirm the target exists:** `nw sheet plan --project <P>` — a clean error means
   `outbox_targets[<P>].sheet` isn't configured yet (see `docs/specs/0124-…md` for the config
   shape); stop and configure it first, don't invent one. Exact flags for both commands:
   `nw sheet plan --help` / `nw sheet record --help`.
2. **Read the sheet.** Using your own Google Sheets tools, read the configured `tab` (see the
   project's `outbox_targets[<P>].sheet.spreadsheet_id`/`tab` in `~/.config/nightwright/config.yaml`).
   Shape each data row into `{header: value}` using the manifest's exact column headers (`nw
   sheet plan`'s error names the manifest; read it under
   `~/.config/nightwright/schemes/<manifest>.toml` if unsure of headers). Write the array of row dicts
   to a tmp JSON file.
3. **Get the full plan:** `nw sheet plan --project <P> --sheet-rows <tmp-file>` — now returns
   both:
   - `push`: ideas needing a row written/updated (`action: "new"` needs a fresh row;
     `"update"` needs the existing row — found by `row_id` — updated in place).
   - `pull`: sheet rows whose id doesn't match any known idea (candidates to adopt).
4. **Apply `push`** via your Sheets tools: for `action: "new"`, append a row and mint/assign
   whatever id scheme the sheet already uses for that project (e.g. `EXAMPLE-NNNN` — check the
   sheet's own existing ids first so you don't collide, per the residual risk noted in
   SPEC-0123's Architecture); for `"update"`, find the row by its `row_id` under the
   manifest's `id_column` and overwrite only the **nightwright-owned** columns (`entry["row"]`) —
   never touch a sheet-owned column (e.g. `Costumer`) you didn't get from `entry["row"]`.
5. **Decide on `pull`** entries: default is to adopt each as a new nw idea (that's what a
   pre-triage backlog is for) unless the operator says otherwise for a specific row.
6. **Record the outcome:** build `{"pushed": [{"idea": ..., "row_id": ...}, ...], "adopted":
   [{"row": {...}}, ...]}` from what you actually did in steps 4–5, write it to a tmp file,
   then `nw sheet record --project <P> --payload <tmp-file>`.
7. **Report** counts (pushed / adopted / skipped) to the operator. Never claim the sheet is
   in sync without having actually called `record` — that's the only thing that persists
   bookkeeping (`Ext <P>.sheet_row`/`sheet_hash`) so the *next* run's `plan` is accurate.

## Conventions / guardrails

- **Column ownership is fixed by the manifest, not negotiable per run:** a `nw`-owned column
  is always pushed from nightwright's idea content; a `sheet`-owned column is never written by this
  skill, only read back (and nightwright doesn't even store it — see SPEC-0123's Non-goals).
- **Scope is always `:PROJECT: <P>` AND `Ext <P>.sheet_sync == "yes"`.** Don't push an idea
  just because it has the right `:PROJECT:` — the Ext opt-in is deliberate per SPEC-0118's
  non-goal against required-at-capture fields; most ideas will never opt in.
- **Never go live against a sheet with known data problems.** Before the first real run
  against any given tab, check for duplicate/ambiguous ids in the `id_column` — the Example
  Pre-triage Backlog is known (as of writing) to already have one (`EXAMPLE-0002` used twice);
  fix that by hand in the sheet before trusting id-based matching there.
- **`nw` itself is offline by design** — if you find yourself reaching for a way to make `nw`
  call the Sheets API directly, stop; that contradicts SPEC-0123's Architecture. The fix is
  always "do the Sheets call yourself, then tell `nw` what happened."

## Verify

Re-run `nw sheet plan --project <P>` (no `--sheet-rows` needed) right after `record` — every
idea you just pushed should now be absent from `push` (content hash matches what's stored).
Spot-check one adopted row became a real `nw idea show <NEW-ID>` with `Ext <P>.sheet_row` set
to that row's id.
