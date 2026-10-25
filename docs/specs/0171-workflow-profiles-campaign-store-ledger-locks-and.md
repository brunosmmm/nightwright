---
id: SPEC-0171
title: "Workflow profiles, campaign store, ledger, locks and wt campaign commands"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, campaigns, ledger, locks, config]
source_idea: IDEA-018
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-018`).

Everything the loop does must be observable after the fact: which state an item is in, every
transition, every persona run with its cost, every gate result, every finding, every park.
wt has no such store. Its data-dir files are whole-file rewrites with tolerant line parsing
(`history.py:15-55`), its only append log is read-concat-write (`export.py:57-70`), and
**nothing in the repo takes a lock**; safety rests on drift guards (`org_write.py:208`).
The queue needs the repo's first lock primitive, both for a singleton runner and for
read-modify-write of campaign state from the loop and from human commands at once.

Profiles (models, effort, budgets, loop limits, slots) belong in the user's `config.yaml`,
but `load_config` merges one level deep (`config.py:84-102`): a user `agentic:` block would
replace built-in profiles wholesale. Resolution therefore deep-merges in code.

## Goals / Non-goals

**Goals**
- `agentic:` config with code-held builtin profiles (`balanced`, `frugal`, `thorough`) and
  user deltas merged over them; `persona_settings(profile, persona)`.
- A campaign store under `<data_dir>/agentic/campaigns/<id>/`: atomic `state.json`,
  append-only `ledger.jsonl` with monotonic `seq`, per-campaign flock, tolerant reader.
- A singleton runner lease: `flock` on `queue.lock` plus a heartbeat file; `lease_status()`
  with an advisory `stale` flag.
- Read and annotate commands: `wt campaign list|show|log|note` (`wt.campaigns.v1`,
  `wt.campaign.v1`, `wt.campaign.log.v1`), with `WT_CAMPAIGN` as the default campaign for
  `note` so agents can log from inside a run.

**Non-goals**
- Any state transition logic (SPEC-0176), spawning (SPEC-0174), gates (SPEC-0173).
- Cross-host locking; `fcntl.flock` on a local filesystem is the contract.
- A database. Tens of campaigns; files and a scan are enough.

## Decision

Add `src/wt/agentic/profiles.py`, `store.py`, `lease.py`, `DEFAULT_CONFIG["agentic"]` in
`config.py`, the `campaign` click group in `src/wt/agentic/cli.py` (registered from
`wt/cli.py` via `cli.add_command`), and `completion.complete_campaign_id`. State writes go
through `export._atomic_write`; ledger appends use `O_APPEND` with one `write()` per line
under the campaign flock; the lease is a held file descriptor with `LOCK_EX | LOCK_NB`.

## Design

### `config.py`

```python
"agentic": {
    "runner": "claude",            # claude | fake (tests)
    "fake_script": None,           # path to a FakeRunner script (SPEC-0174)
    "default_profile": "balanced",
    "poll_interval_s": 20,
    "lease_ttl_s": 120,
    "run_launcher": "nohup",       # nohup | tmux | foreground (used by /wt-conduct)
    "profiles": {},                # user deltas over BUILTIN_PROFILES
}
```

### `profiles.py`

- `BUILTIN_PROFILES`: `balanced` = `{slots: 2, loop_max: 3, campaign_cap_usd: 10.0,
  run_timeout_s: 1800, personas: {implementer: {model: "sonnet", effort: "high",
  max_budget_usd: 4.0, permission_mode: "bypassPermissions", read_only: false},
  gate_triager: {model: "sonnet", effort: "medium", max_budget_usd: 0.5,
  permission_mode: "dontAsk", read_only: true}}}`; `frugal` (1 slot, 2 loops, $3,
  implementer `max_budget_usd 1.5`, triager `haiku`); `thorough` (3 slots, 4 loops, $30,
  implementer `opus`). Reviewer personas are added by later specs with `opus` defaults.
- `resolve_profile(cfg, name=None) -> dict`: name defaults to
  `cfg["agentic"]["default_profile"]`; unknown name with no user delta ⇒ `ValueError`
  listing known names; result = `deep_merge(BUILTIN_PROFILES.get(name, BUILTIN_PROFILES[
  "balanced"]), cfg["agentic"]["profiles"].get(name, {}))` plus `"name"`.
- `persona_settings(profile, persona) -> dict`: `profile["personas"][persona]` or
  `ValueError`.
- `deep_merge(a, b)` is a small local helper (dicts recursively, everything else replaced).

### `store.py`

- Paths: `agentic_dir(cfg) = Path(cfg["data_dir"]) / "agentic"`, `campaigns_dir`,
  `campaign_dir(cfg, id)`; `runs_dir`, `gates_dir` under it.
- `TERMINAL_STATES = {"MERGED", "HANDED_OFF", "ABANDONED"}`; `STATES` is owned by
  SPEC-0176, the store only treats the terminal set specially for listing.
- `new_campaign_id(spec_id, now) -> str`: `f"{spec_id}-{now:%Y%m%d}-{secrets.token_hex(2)}"`;
  retried if the directory exists.
- `create_campaign(cfg, state: dict) -> Path`: `mkdir` (fails if it exists), write
  `state.json` atomically, append the `open` event.
- `read_state(cfg, id) -> dict` / `write_state(cfg, id, state)`: `state["updated_at"]` is
  set on write; `export._atomic_write` (mkstemp + `os.replace`).
- `item_lock(cfg, id)`: context manager, `flock(LOCK_EX)` on `campaigns/<id>/.lock`.
- `append_event(cfg, id, type, payload, by) -> dict`: under `item_lock`, reads the last
  line's `seq` (cached per process, re-read under the lock), writes
  `{"ts": <iso with tz>, "seq": n+1, "campaign": id, "type": type, "by": by, **payload}`
  as one line via `os.open(..., O_APPEND | O_CREAT | O_WRONLY)` and a single `os.write`.
- `read_ledger(cfg, id, types=None, tail=None) -> list[dict]`: line by line,
  `json.JSONDecodeError` on a line ⇒ skipped (a truncated last line must not break reads).
- `list_campaigns(cfg, include_terminal=False, project=None, spec_id=None,
  state=None) -> list[dict]`: scans `campaigns/*/state.json`, newest first by
  `created_at`.
- `active_campaign_for_spec(cfg, spec_id) -> dict | None`: a non-terminal campaign for the
  spec (used by `queue add` in SPEC-0176).

### `lease.py`

- `Lease(fd, path, info)`; `acquire(cfg, argv) -> Lease`: opens `queue.lock` (`a+`), keeps
  the fd, `flock(LOCK_EX | LOCK_NB)`; on `BlockingIOError` reads `queue.lease.json` and
  raises `ValueError("queue held by pid N on <host> (heartbeat Ns ago)")`. On success
  writes the lease json atomically `{pid, host, argv, started_at, heartbeat_at, ttl_s}`.
- `Lease.heartbeat()` rewrites `heartbeat_at`; `Lease.release()` removes the json and
  closes the fd (the flock dies with the fd / the process either way).
- `lease_status(cfg) -> dict`: `{held: bool, pid, host, started_at, heartbeat_age_s,
  ttl_s, stale: bool}`; `held` is probed with a non-blocking flock attempt on a fresh fd
  (released at once); `stale` = held and heartbeat age > ttl. Advisory only: no takeover.

### CLI (`campaign` group)

- `wt campaign list [--all] [--project P] [--spec SPEC_ID] [--state S] [--json]` →
  `wt.campaigns.v1` `{"campaigns": [state summaries]}`; human: Rich table
  `id | spec | project | state | loops | cost | updated`.
- `wt campaign show CAMPAIGN [--json]` → `wt.campaign.v1` (the state plus derived
  `last_events` (5) and `worktree_present`).
- `wt campaign log CAMPAIGN [--type T]* [--tail N] [--json]` → `wt.campaign.log.v1`
  `{"campaign", "events": [...]}`; human: one line per event `seq ts type by summary`.
- `wt campaign note [CAMPAIGN] TEXT [--author A]`: CAMPAIGN defaults to `$WT_CAMPAIGN`,
  else `click.UsageError`. *As shipped:* the arguments are one variadic list; the first
  word is taken as the campaign when a campaign directory of that name exists, otherwise
  the whole text is the note and `$WT_CAMPAIGN` must be set; appends `note{text, author}` with `by = "agent:<author>"` when
  author is given, else `"human"`.
- `CAMPAIGN` arguments use `shell_complete=C.complete_campaign_id` (non-terminal ids
  first); `"campaign"` is added to `completion.REQUIRED_SHELL_COMPLETE_NAMES`.
  *As shipped:* a `complete_campaign_state` completer was added as well, because the
  `--state` option is named `state` and the completion guard requires one.

### Docs

`docs/product/capabilities/agentic.md` gains "Campaigns and the ledger" and "Profiles"
sections; `docs/product/reference/index.md` gains the `campaign` row; agent-surface guide
gains the three schema ids.

## Alternatives considered

- **One flat jsonl for all campaigns** — rejected. Per-campaign files make the lock scope
  natural and let a human `tail -f` one campaign.
- **`sqlite3`** — rejected for now. It would be the first database in wt, and the queue
  is tens of items; a scan is cheap. Revisit if `list` ever needs indexes.
- **Lock files without `flock`** (pid files) — rejected. Stale pid files need recovery
  logic; `flock` releases itself when the holder dies.
- **TTL-based takeover of a stale lease** — rejected in wave 1. A hung runner holding the
  flock is a bug to surface (`stale: true`), not to paper over.

## Acceptance criteria

- [x] A user delta on one persona key keeps every other builtin key for that profile;
      unknown profile names error with the known list; `persona_settings` errors on an
      unknown persona.
- [x] `create_campaign` refuses a duplicate id; `write_state` is atomic (no partial file
      observable; temp file cleaned up); `read_state` round-trips.
- [x] `append_event` from 8 threads × 50 events yields 400 lines with strictly increasing
      `seq` and no interleaving; `read_ledger` skips a truncated last line and honors
      `types` and `tail`.
- [x] A second `acquire` while the first lease is held fails with the holder's pid; after
      `release` it succeeds; `lease_status().stale` is true once the heartbeat age exceeds
      `ttl_s` while held, false after `heartbeat()`.
- [x] `list_campaigns` hides terminal states unless asked and filters by project / spec /
      state; `active_campaign_for_spec` finds exactly the non-terminal one.
- [x] `wt campaign list|show|log --json` emit `wt.campaigns.v1`, `wt.campaign.v1`,
      `wt.campaign.log.v1`; `wt campaign note` with `WT_CAMPAIGN` set and no argument
      appends to that campaign; without either it is a usage error.
- [x] Completion, reference and agent-surface guards pass.
- [x] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_profiles.py` (merge semantics, unknown names,
  builtins have every persona key); `tests/test_agentic_store.py` (ids, create/read/
  write, atomicity via a crash-simulating monkeypatch of `os.replace`, threaded appends,
  truncated line, filters); `tests/test_agentic_lease.py` (two fds in one process conflict;
  release; stale flag with a monkeypatched clock); `tests/test_agentic_campaign_cli.py`
  (`CliRunner` with patched `load_config` and `monkeypatch.setenv("WT_CAMPAIGN", …)`).
- **Manual verification:** `wt campaign list` on an empty data dir prints an empty table
  and `--json` an empty list; `WT_DATA_DIR` pointed at a scratch dir keeps the real data
  dir untouched.
- **Regression guard:** full `uv run pytest`.

### Test record (2026-10-25)

- **Automated:** 31 new tests — `tests/test_agentic_profiles.py` (9: builtin completeness,
  DEFAULT_CONFIG block, copy semantics, one-key delta keeps siblings, new user profile over
  balanced, default_profile, unknown name, persona_settings, deep_merge),
  `tests/test_agentic_store.py` (12: id shape/uniqueness, create/read/duplicate, missing
  state, atomic write with a simulated crash in `os.replace`, event envelope, **8 threads ×
  50 appends ⇒ 401 lines with seq 1..401**, truncated last line skipped and seq still
  monotonic, filters/tail, item_lock exclusion, listing order/filters, active campaign,
  paths), `tests/test_agentic_lease.py` (5: acquire/release/status, second acquire names
  the holder, **stale flag with an injected clock** at 121 s > ttl 120 then cleared by
  heartbeat, ttl from config/override, leftover lease.json without flock is not held),
  `tests/test_agentic_campaign_cli.py` (5: list json/all/filters/human, empty data dir,
  show json/human/missing, log filters/tail, note by argument / by `WT_CAMPAIGN` / usage
  error / unknown campaign). Full suite in this worktree (textual installed, so the pilot
  tests run): **1300 passed, 2 failed, 1 skipped**. The 2 failures
  (`tests/test_idea_project.py::test_capture_flow_offers_a_project_and_can_decline`,
  `::test_metadata_chooser_sets_and_clears_project`) also fail at the scaffold commit
  without this change — the same `Example`/`Meta-Tools` chooser-order drift SPEC-0165
  fixed for `test_known_project_choices`, visible only when the `tui` extra is installed.
  Not caused here; flagged to the epic.
- **Manual:** with `WT_DATA_DIR`/`WT_CONFIG_DIR` on a scratch dir: `resolve_profile`
  → balanced/2 slots/sonnet; a campaign created, a transition appended, `acquire` held,
  second `acquire` refused naming the pid, released; `wt campaign list` table,
  `wt campaign show` (worktree reported missing), `WT_CAMPAIGN=… wt campaign note … --author
  implementer` appended `by=agent:implementer`, `wt campaign log --json` → seq 1,2,3. The
  real data dir was untouched.

## Clock Log

Internal work: clock on IDEA-018 with `wt idea clock-in` / `clock-out`.

## Rollout / migration

New data-dir subtree, created on first use. No migration. A missing `agentic:` block in
user config means builtin defaults.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
