---
id: SPEC-0293
title: "Gate ladder v2: a cross-campaign flaky-test record the triager sees"
status: done
owner: user
created: 2026-10-30
milestone: "M9: agentic workflow"
kind: feature
source_idea: IDEA-027
parent: SPEC-0169
depends_on: [SPEC-0289]
tags: [agentic, gates, triage]
---

## Context

Promoted from idea `IDEA-027` (gate ladder v2). Part 2 of 2, after
[SPEC-0289](./0289-gate-ladder-v2.md). SPEC-0289 gives each gate result a `flaky` list: the
pytest ids that failed and then passed on a targeted rerun. That evidence lives in only one
campaign:

- The gate triager (`src/wt/agentic/personas/gate_triager.md`) decides `suspected_flaky` from
  one log, with no memory. A test that has flaked in ten campaigns this month looks the same as
  a fresh real failure.
- `src/wt/agentic/queue.py:decide_triage` honors `retry_gate` once per loop. When that retry
  turns the gates green (`_apply`, the `gate` branch: TRIAGING → GATING → `gates_green`), the
  ids that failed before were flaky too, but nothing records them.
- Nothing in the data dir aggregates gate outcomes across campaigns, and there is no command
  to list the known flaky tests (the missing-CLI rule).

## Goals / Non-goals

**Goals**
- A data-dir file, `<agentic_dir>/flaky-tests.json` (`wt.flaky.v1`), that records per project
  and test id how often the test flaked, when, and in which campaigns.
- It is fed from two sources: SPEC-0289's `flaky` ids, and the ids of a failed run that a
  triager `retry_gate` turned green.
- The triager prompt shows the history of the ids that are failing right now.
- `wt queue flaky [--project P] [--json]` lists the record.

**Non-goals**
- Quarantining or auto-skipping flaky tests in the target repo.
- Filing ideas or bugs for flaky tests automatically. The triager's `followup_ideas` already
  covers that.
- Changing the ladder (SPEC-0289) or `decide_triage` based on the history. The history only
  informs the triager.

## Decision

Keep one small JSON file per data dir, rewritten atomically under its own flock. It is not a
ledger: it is a derived, bounded summary. Each entry keeps counts, first and last seen, and
the last 10 campaign ids. Entries not seen for 90 days are dropped on every write. Writes are
best-effort: a failed write is a `note` event and never changes a campaign's state.

The triager sees the record through a new placeholder, `{{flaky_history}}`. Only the ids
failing in the current `last_gate` are looked up, so the prompt stays small.

## Design

- New `src/wt/agentic/flaky.py`:
  - `path(cfg)` returns `store.agentic_dir(cfg) / "flaky-tests.json"`. The lock file is
    `flaky.lock`, taken with `store._flocked`, the same way as `store.add_lock`.
  - The file shape is `{"schema": "wt.flaky.v1", "projects": {P: {test_id: {"count", "first_seen",
    "last_seen", "sources": {"rerun": n, "triage_retry": n}, "campaigns": [≤10 ids]}}}}`.
  - `record(cfg, project, ids, *, campaign, source, now=None)` does a read-modify-write under
    the lock. It writes atomically through `export._atomic_write`, which `store` already
    uses. It applies the 90-day expiry and caps each project at 500 entries, keeping the
    most recently seen. An unreadable or foreign file is moved aside to
    `flaky-tests.json.bad-<ts>` and replaced with a fresh record.
  - `lookup(cfg, project, ids)` returns `{id: entry}` for the ids that are known.
  - `list_entries(cfg, project=None)` returns rows sorted by `count` desc, then `last_seen`.
- `src/wt/agentic/queue.py:_apply`, in the `kind == "gate"` branch, after
  `state["last_gate"] = payload`:
  - Collect the ids from `r["flaky"]` for every result. These are recorded with
    `source="rerun"`.
  - If `run.ok`, and the transition reason that led here was `retry_gate` (meaning the
    previous `last_gate` failed and `counters["gate_retries"] > 0` in this loop), collect the
    `summary.failing` ids of the previous `last_gate`'s failed results. These are recorded
    with `source="triage_retry"`.
  - Call `flaky.record(...)` once for each source, wrapped in `try/except (OSError,
    ValueError)`. A failure appends a `note` event (`could not record flaky tests: …`).
  - The ledger gets a `flaky_recorded` event `{ids, source}` when ids were recorded. Register
    it in `schemas.EVENT_KEYS` as `("ids", "source")`.
- `src/wt/agentic/personas.py`:
  - Add `"flaky_history"` to `PLACEHOLDERS`.
  - New `render_flaky_history(cfg, state)`: for each failing id in `last_gate`, the line is
    `- a::b — flaked 4× in 3 campaigns (last 2026-10-28; via rerun 3, triage retry 1)`. When
    nothing is known, it renders `(none known)`.
  - The context builder (the `ctx = {...}` that has `"gate_results"`) adds `flaky_history`.
- `src/wt/agentic/personas/gate_triager.md`: add a `## Known flaky tests` section with
  `{{flaky_history}}` after `## Gate results`. Add to the `retry_gate` bullet: "a failing id
  with a flaky history raises the odds; an id never seen flaky with a deterministic assertion
  error does not."
- `src/wt/agentic/cli_queue.py`: new `wt queue flaky [--project P] [--json]`.
  - The text form prints one row per entry: id, count, campaigns, last seen and sources.
  - `--json` emits `{"schema": "wt.queue.flaky.v1", "path", "entries": [...]}`.
  - Read-only: it never writes and never prunes.

## Alternatives considered

- **Derive the history by scanning every campaign ledger at prompt time.** Rejected. It is
  O(campaigns) per triage, and the cost grows with every campaign.
- **SQLite.** Rejected. One small bounded JSON file matches the rest of the data dir and is
  easy to inspect.
- **Let the history decide `retry_gate` deterministically.** Rejected for now. The triager
  weighs it with the log. Revisit once the data shows a clear threshold.

## Acceptance criteria

- [x] `flaky.record` writes `<agentic_dir>/flaky-tests.json` (`wt.flaky.v1`) atomically under
      `flaky.lock`. It increments counts, keeps first and last seen and at most 10 campaigns
      per id, drops entries unseen for 90 days, and caps 500 entries per project. A corrupt
      file is moved aside, not crashed on.
- [x] A gate run whose results carry SPEC-0289 `flaky` ids records them with `source: rerun`
      and appends a `flaky_recorded` ledger event.
- [x] A triager `retry_gate` that turns the gates green records the previously failing ids
      with `source: triage_retry`. A green run that did not follow a retry records nothing.
- [x] A failed record write leaves the campaign's transition unchanged and appends a `note`.
- [x] The gate-triager prompt contains a `Known flaky tests` section with the history of the
      currently failing ids, or `(none known)`.
- [x] `wt queue flaky [--project] [--json]` lists entries sorted by count. `--json` emits
      `wt.queue.flaky.v1`.

## Test plan

- New `tests/test_agentic_flaky.py`:
  - `test_record_creates_and_increments`: two records of the same id give `count == 2`, two
    campaigns and the `sources` counters.
  - `test_campaign_list_capped_at_10`.
  - `test_expiry_drops_old_entries`: a record with `now` 91 days later drops an entry last
    seen on day 0.
  - `test_project_cap_500_keeps_recent`.
  - `test_corrupt_file_moved_aside`: garbage in the file gives a `.bad-*` sibling and a fresh
    record.
  - `test_concurrent_records_do_not_lose_updates`: two processes (multiprocessing) each record
    50 times, and the final count is 100.
  - `test_lookup_and_list_sorted`.
- `tests/test_agentic_queue.py`, using the fake runner and fake gates:
  - `test_gate_flaky_ids_recorded`: a gate result with `flaky: ["t::a"]` gives a file entry
    and a `flaky_recorded` event that validates against `schemas`.
  - `test_triage_retry_green_records_previous_failing`: gates fail with `summary.failing:
    ["t::b"]`, the triager returns `retry_gate`, and the gates then pass. `t::b` is recorded
    with source `triage_retry`.
  - `test_green_without_retry_records_nothing`.
  - `test_record_failure_is_note_only`: `flaky.record` is monkeypatched to raise `OSError`.
    The campaign still reaches REVIEWING and gets a `note` event.
- `tests/test_agentic_personas.py`:
  - `test_triager_prompt_has_flaky_history`: a seeded file plus a failing `last_gate` gives
    a prompt containing `flaked 2×`.
  - `test_flaky_history_none_known`.
- `tests/test_agentic_queue_cli.py::test_queue_flaky_json` and `::test_queue_flaky_text` (via
  `CliRunner`).
- **Sabotage check:**
  - Remove the lock around the read-modify-write.
    `test_concurrent_records_do_not_lose_updates` must fail (or flake visibly).
  - Record on every green run, not only after a `retry_gate`.
    `test_green_without_retry_records_nothing` must fail.
- **Regression guard:** full `uv run pytest`. A triage with no flaky file renders
  `(none known)`, and the other prompt sections are unchanged.

### Test record (2026-10-30)

- New `tests/test_agentic_flaky.py` (7 tests), the SPEC-0293 tests in `tests/test_agentic_queue.py`
  (4), `tests/test_agentic_personas.py` (2) and `tests/test_agentic_queue_cli.py` (2, plus
  `flaky` in the help listing): all pass.
- Sabotage: dropping the `flaky.lock` around the read-modify-write makes
  `test_concurrent_records_do_not_lose_updates` fail; recording on every green run (not only
  after a `retry_gate`) makes `test_green_without_retry_records_nothing` fail. Both reverted.
- Full `uv run pytest`: 3812 passed, 3 skipped, 1 failed (`test_no_bare_join_before_result_read`:
  my new test used a bare `p.join(60)`); fixed with `waits.join` and re-run on the touched files
  (`tests/test_test_waits.py`, `tests/test_agentic_flaky.py`): pass.
- `python3 tools/spec_lint.py --write-ledger` then `python3 tools/spec_lint.py`: exit 0.

### As shipped (review round 1)

- `triage_retry` is keyed on an explicit marker, `state["gate_retry_pending"]`: set in `_apply`'s
  triage `gate` branch next to `retry_gate`, read and cleared on every gate run, copied by
  `finish()` and dropped by `apply_resume`. It replaces the Design's `gate_retries > 0` test,
  which a park + plain resume left stale.
- `wt queue flaky` text cells are `rich.text.Text`, so ids like `t::x[json]` print verbatim.
- `flaky._valid` checks entry field types (a malformed file is moved aside); a read `OSError` is
  not a bad file and propagates as a best-effort note. `_flaky_events` and `render_flaky_history`
  also swallow `TypeError`/`AttributeError`.
- Round-1 tests: retry_gate whose rerun still fails, fail → retry → fail → escalate → plain
  resume → green (records nothing), malformed entry fields, unreadable file not moved aside,
  markup-like ids in the text table. Sabotage: reverting to `gate_retries > 0` fails the resume
  test. Targeted run (flaky, queue, personas, queue_cli, test_waits, schema/state tests): 350
  passed; the full suite was left to gating.

## Rollout / migration

New file, created on first write. No migration. Old campaigns are not back-filled.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
