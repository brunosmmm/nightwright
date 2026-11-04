---
id: SPEC-0415
title: "Campaign history fixture: tools/capture_history_fixture.py, the sanitized tests/fixtures/agentic_history/ data dir with MANIFEST.json, and tests/history_scale.clone_fixture"
status: proposed
owner: user
created: 2026-11-04
source_idea: IDEA-909
kind: feature
tags: [tui, agentic, observability, testing]
---

## Context

Promoted from idea `IDEA-909`. This is the fixture half of "child 2" of epic
[SPEC-0405](./0405-rich-queue-tui-prioritized-live-queue-campaign.md) (rich queue TUI over a
shared history layer). That epic's child 2 bundles the history data layer
(`agentic/history.py`, `nw campaign history`) with "a committed sanitized fixture of real closed
campaigns under `tests/fixtures/agentic_history/`" and a capture tool. The fixture is a
prerequisite of the data layer, not part of it: the data layer's acceptance criteria (SPEC-0405
AC1 to AC5), `tests/test_history_fixture.py` and `tests/test_cli_campaign_history.py` all read it.
So the fixture lands first, as its own spec. The history-layer spec (SPEC-0406) lists this spec
in `depends_on`. This spec is not a child by `parent`, because the work is tracked as a single
idea; it is a part of SPEC-0405's breakdown and is cited by id in prose only.

**What exists today**

- The real data dir (`~/.local/share/nightwright/agentic/`) holds 217 campaign dirs, 1.2 GiB.
  Surveyed on 2026-11-04: 202 MERGED, 14 ABANDONED, 7 PARKED, 3 other live. **No campaign is
  HANDED_OFF** in it today, although `HANDED_OFF` is a terminal state
  (`store.TERMINAL_STATES`, `close.py`).
- Layout of one campaign (`store.campaign_dir`): `state.json` (`nw.campaign.v1`),
  `ledger.jsonl` (`nw.ledger.event.v1`), `runs/<run>/{run.json,result.json,stream.jsonl,
  launch.json,exit.json,proc.json,child.json,request.json,stderr.log,wrapper.log,prompt.md}`,
  `gates/NNN-<gate>[.rK].{log,json}`, `spec_at_open.md`. Around the campaigns sit the
  cross-campaign sources listed in SPEC-0405's *Source inventory*: `conductor/decisions.jsonl`,
  `escalations/ESC-NNNN.json`, `mailbox/<inbox>.jsonl`, `events.jsonl`, `runner.jsonl`,
  `reviews/`, `spec-reviews.jsonl`, `flaky-tests.json`, `sentinel/`.
- Schemas carry the `wt.` prefix (before SPEC-0330) or `nw.`; readers accept both.
- `tests/fixtures/dashboard_data.py` seeds a tiny synthetic data dir for the dashboard tests.
  It is hand-made, has no stream, no multi-loop campaign and no real text, so it cannot
  exercise phase timing, links or inference over real data.
- Nothing scales a fixture up. SPEC-0405's budgets (500 campaigns, 50 000 records) have no
  data to be measured on.
- The real data dir is 1.2 GiB, holds paths, host names and possibly secrets in prompts,
  env and streams, and changes every hour. It cannot be committed and tests cannot read it.

**The problem.** Every later spec in the epic (history layer, harness snapshots, popup,
History and Findings tabs, perf) needs the same small, stable, safe, real-looking data dir.
Without a reproducible capture tool each author would hand-copy files from the live dir,
leak local content and drift from each other.

## Goals / Non-goals

**Goals**
- **G1. A capture tool.** `tools/capture_history_fixture.py` reads a nightwright data dir
  (read-only) and writes a sanitized, size-bounded, deterministic subset to
  `tests/fixtures/agentic_history/`.
- **G2. A committed fixture.** `tests/fixtures/agentic_history/` is itself a valid
  nightwright `data_dir` (it contains `agentic/…`), so `cfg["data_dir"]` can point at a copy of it and the
  existing readers (`store`, `queue`, `campaign_export`, `dashboard`) run on it unchanged.
  It covers the scenarios SPEC-0405's Test plan names.
- **G3. A manifest.** `MANIFEST.json` lists every file with size and sha256, every campaign
  with the scenarios it covers, the sanitization rules applied and the totals, so a test
  can detect drift, tampering and un-sanitized content.
- **G4. A scale helper.** `tests/history_scale.py` exposes `clone_fixture(dst, campaigns=…,
  records=…)`, which builds a larger data dir from the fixture for perf tests, and
  `copy_fixture(dst)` for tests that need a writable copy.
- **G5. Reproducible.** The same source dir and arguments give byte-identical output.

**Non-goals**
- `agentic/history.py`, `nw campaign history`, `nw campaign records`, the history schemas
  and the cache. Those are SPEC-0406. This spec ships no expected history numbers: those are
  computed by SPEC-0406's tests from the fixture.
- Any TUI view, the Pilot harness (`tests/tui_harness.py`, SPEC-0409's second half), snapshots
  and the perf test itself (`tests/test_history_perf.py`). They consume the fixture and the
  scale helper.
- Changing any production writer or reader. This spec adds only a tool under `tools/`, a
  fixture and test-support code under `tests/`.
- Anonymizing project, spec and persona names. They are not secret and the Test plan filters
  on them (`project:Meta-Tools`). Only the categories listed in *Sanitization* are rewritten.
- Capturing live campaigns, or any campaign whose sanitization scan fails.

## Decision

Add a stdlib-only capture script that copies a fixed, scenario-driven selection of closed
campaigns plus the cross-campaign records that name them out of a data dir, applies
deterministic redaction and size caps, and writes the result and a `MANIFEST.json` under
`tests/fixtures/agentic_history/agentic/`. Commit the output. The directory layout mirrors a
real `data_dir`, so tests copy it and point `cfg["data_dir"]` at the copy. A second script,
`tests/history_scale.py`, clones the fixture's campaigns under new ids to reach any campaign
and record count. `tests/test_history_fixture.py` guards the fixture's integrity,
sanitization, scenario coverage and the clone helper, using the committed fixture and a
synthetic source dir (never the developer's real dir).

Because the real data has no HANDED_OFF campaign, the tool derives one from a captured merged
campaign and marks it `origin: "derived"` in the manifest. Every other campaign is
`origin: "captured"`.

## Design

### Capture tool: `tools/capture_history_fixture.py`

Python 3, standard library only (it must run with `python3 tools/…` like `spec_lint.py`).

```
python3 tools/capture_history_fixture.py [--src DIR] [--out DIR] [--cid CID …]
        [--no-derive-handed-off] [--check]
```

- `--src`: a nightwright data dir (the one that contains `agentic/`). Default:
  `$NW_DATA_DIR` if set, else `~/.local/share/nightwright`.
- `--out`: default `tests/fixtures/agentic_history` (relative to the repo root, found from
  the script path). The tool refuses an `--out` inside `--src` and refuses to write when
  `--src` is not a directory with `agentic/campaigns/`.
- `--cid`: pin a scenario's campaign by id (repeatable; the scenario is whichever it
  qualifies for). Without it the tool selects automatically.
- `--check`: write nothing; verify that `--out` matches its `MANIFEST.json` (every listed
  file present with the right sha256 and size, no unlisted file) and that the sanitization
  scan finds nothing. Exit 0 or 1. This is what the test calls.
- The source tree is opened read-only. The tool never creates, renames or modifies anything
  under `--src`, and never takes the store's locks.

**Scenario selection.** Only campaigns whose `state.json` state is in `{MERGED, HANDED_OFF,
ABANDONED}` are candidates. Each scenario has a predicate over `state.json`, the ledger and
the run dirs. For each scenario the tool picks the candidate with the smallest total byte
size after caps (ties: lexicographically smallest cid), skipping campaigns already chosen
unless the scenario list below says the same campaign may serve two:

| Scenario key | Predicate |
|---|---|
| `merged_multi_loop` | state MERGED; the ledger has at least two `transition` events into the same non-terminal state, at least two `gate` events and at least two `finding` events, and at least one `run.end` with a nonzero cost |
| `abandoned` | state ABANDONED, with at least one `run.end` |
| `handed_off` | state HANDED_OFF (real), else derived (see below) |
| `human_escalation` | the ledger has a `human_decision` event whose escalation id resolves to `escalations/ESC-NNNN.json` with `status` answered |
| `pre_stream` | no run dir has `stream.jsonl` (campaign predates SPEC-0362) and at least one `run.json` or `result.json` |
| `flaky_and_triage` | the ledger has a `flaky_recorded` event and a run whose `run.json` verdict has a nonempty `waive` list |
| `legacy_schema` | `state.json` `schema` starts with `wt.` (so the fixture holds both prefixes) |
| `no_run_json` | at least one run dir has only `result.json` (the 14 pre-`run.json` runs). Optional: recorded `absent_in_source` when none qualifies |

A campaign may satisfy several scenarios; the manifest lists all of them. The total number
of captured campaigns is at most 12. If a required scenario (every key except `no_run_json`
and `handed_off`) has no candidate, the tool exits 2 with the missing key and writes nothing.

**Derived HANDED_OFF.** With no real HANDED_OFF candidate, the tool takes the
`merged_multi_loop` campaign's files and writes a second campaign under a new id
`<PROJECT>-<NNNN>-<date>-hdof` (same project, the original id with suffix `hdof` replacing its
last hex segment). In the copy: `state.json` has `state: HANDED_OFF`, `prev_state` unchanged,
`close.mode` the value `close.py` writes for a handoff (read `close.py` around
`CloseOutcome("HANDED_OFF", …)`), a `pr_url` of `https://example.invalid/pr/1` and no
`merged_sha`; the final `transition` into `MERGED` is rewritten to `HANDED_OFF` and the `close`
event is rewritten the same way. Every other line, run and gate is the original's. Its
manifest entry has `origin: "derived"` and `derived_from`.

**Cross-campaign records.** For the chosen campaign ids the tool copies only matching
records, in source order:
- `conductor/decisions.jsonl` lines whose `campaign` is chosen, plus lines with no `campaign`
  that a mention scan (the cid as a substring of any string field) ties to a chosen one. The
  derived HANDED_OFF campaign gets none.
- `escalations/ESC-NNNN.json` for every escalation id cited by the chosen ledgers or whose
  `campaign` is chosen.
- `mailbox/<inbox>.jsonl` messages whose `campaign` is chosen or that mention a chosen cid.
  Inbox file names are kept.
- `events.jsonl` lines whose `campaign` is chosen (field `campaign` or `source.campaign`),
  and `runner.jsonl` lines inside the time window of the chosen campaigns' ledgers (the
  "runner down" evidence). `events.jsonl` ids and `source.ledger_seq` are kept as is.
- `spec-reviews.jsonl` and `reviews/<SPEC>/…` for the chosen campaigns' specs, `flaky-tests.json`
  filtered to entries whose `campaigns[]` includes a chosen id (the `campaigns[]` lists are
  filtered to chosen ids), and `sentinel/<project>.last.json` for the chosen projects (without
  `.logs/`).
- Absent sources are skipped and noted in the manifest as `absent_in_source`; they are not
  an error.

**Per-campaign copy rules.** Copied: `state.json`, `ledger.jsonl`, `spec_at_open.md`,
`runs/<run>/{run.json,result.json,exit.json,request.json,stderr.log,stream.jsonl}` and
`gates/*`. Not copied, because they carry local process data and nothing the history reads:
`launch.json`, `proc.json`, `child.json`, `wrapper.log`, `prompt.md`, `tmp/`. Size caps (bytes
and lines, counted before redaction):

| File | Cap |
|---|---|
| `stream.jsonl` | last 120 lines, each line at most 4 KiB (longer lines are replaced by a JSON object with the same `type`, a `truncated: true` flag and the first 512 characters of text); the first line of the original is always kept |
| `gates/*.log`, `stderr.log` | last 200 lines, each at most 1 KiB |
| `result.json`, `run.json` | unchanged unless over 64 KiB; then free-text values are cut to 4 KiB and `truncated_by_fixture: true` is set |
| `ledger.jsonl` | never truncated (phase sums need every event) |
| Whole fixture | at most 3 MiB (excluding `MANIFEST.json`); the tool exits 3 if exceeded and tells which campaign is largest |

### Sanitization

Applied to every captured text file, line by line for `.jsonl` and `.log` and value by value
inside JSON (keys are untouched), by one table of rules in the script:

1. The source home directory and the repo checkout paths become `/home/user` and
   `/home/user/work/<repo-name>`. Both the absolute path and `~`-relative forms are covered.
2. The local user name (from `$USER` and the home dir name) becomes `user`.
3. Email addresses become `user@example.invalid`.
4. Secrets: strings matching `sk-[A-Za-z0-9_-]{16,}`, `ghp_[A-Za-z0-9]{20,}`,
   `github_pat_[A-Za-z0-9_]{20,}`, `xox[abprs]-[A-Za-z0-9-]{10,}`, `AKIA[0-9A-Z]{16}`,
   `Bearer [A-Za-z0-9._~+/=-]{20,}`, PEM blocks, and the value of any JSON key whose name
   matches `(?i)(token|secret|password|api[_-]?key|authorization|cookie)` become
   `[REDACTED]`.
5. Host and URL names: SSH/worker host names found in `workers.json` of the source and
   private IPv4 addresses become `worker-1…`/`10.0.0.N` by order of first appearance (a stable
   map); `pr_url` hosts other than `github.com` become `example.invalid`.
6. `env` objects anywhere (launch environments in `run.json`) are dropped to `{}`.

Rule application is idempotent. The tool counts hits per rule and records the counts in the
manifest. After writing, a **verification scan** re-reads the output and fails (exit 4, output
deleted) if it still finds the source home path, the user name as a path segment, an email
other than the placeholder, any secret pattern, or any string from the optional file
`tools/capture_history_deny.txt` (one literal per line; absent means none).

Sanitization never changes ids, timestamps, numbers, states, personas, cost, usage, file
names or JSON structure, so durations, costs and links stay real. It never reflows JSON:
each JSONL line is re-serialized with `json.dumps(…, ensure_ascii=False, separators=(",", ":"))`
only if a rule changed it; unchanged lines are written byte for byte.

### Fixture layout and `MANIFEST.json`

```
tests/fixtures/agentic_history/
  MANIFEST.json
  agentic/
    campaigns/<cid>/{state.json,ledger.jsonl,spec_at_open.md,runs/…,gates/…}
    conductor/decisions.jsonl
    escalations/ESC-NNNN.json
    mailbox/<inbox>.jsonl
    events.jsonl
    runner.jsonl
    reviews/…  spec-reviews.jsonl  flaky-tests.json  sentinel/<project>.last.json
```

`MANIFEST.json` (`nw.history.fixture.v1`, keys sorted, two-space indent, trailing newline):

```
{
  "schema": "nw.history.fixture.v1",
  "tool": "tools/capture_history_fixture.py",
  "tool_version": 1,
  "source_label": "~/.local/share/nightwright",
  "source_max_ts": "<latest timestamp seen in the chosen ledgers>",
  "campaigns": [
    {"cid": "...", "project": "...", "state": "MERGED|HANDED_OFF|ABANDONED",
     "origin": "captured|derived", "derived_from": "<cid>|null",
     "schema": "nw.campaign.v1|wt.campaign.v1",
     "scenarios": ["merged_multi_loop", ...],
     "runs": N, "ledger_lines": N, "has_stream": true|false}
  ],
  "scenarios": {"<key>": ["<cid>", ...] | "absent_in_source"},
  "sources": {"<name>": "present|absent_in_source"},
  "sanitization": {"rules": {"home_path": N, "email": N, "secret": N, ...}},
  "totals": {"campaigns": N, "files": N, "bytes": N, "records": {"decisions": N,
             "escalations": N, "mail": N, "events": N}},
  "files": {"agentic/…relative path": {"sha256": "...", "bytes": N}}
}
```

There is no wall-clock `captured_at`: the same inputs must give the same manifest. `files`
lists every file under the fixture except `MANIFEST.json` itself.

### Scale helper: `tests/history_scale.py`

```python
FIXTURE_DIR: Path            # tests/fixtures/agentic_history
def copy_fixture(dst: Path) -> Path
def clone_fixture(dst: Path, campaigns: int = 500, records: int | None = None, seed: int = 0) -> Path
```

- Both return `dst`, which is a data dir (`dst/agentic/…`) usable as `cfg["data_dir"]`.
  `dst` must not exist or must be empty. `MANIFEST.json` is not copied.
- `copy_fixture` copies the tree; every file is a plain copy (the test may write to it).
- `clone_fixture` first copies the fixture's campaigns as the "base" set, then adds clones,
  round-robin over the base campaigns in manifest order, until the data dir has exactly
  `campaigns` campaign dirs. The i-th clone of base id `B` gets the id
  `<B-without-last-segment>-c<i:04d>` with the project and sequence prefix kept, so it is a
  valid campaign id of the same project. A clone rewrites only: the directory name; `id`
  in `state.json`; every `campaign` (and `source.campaign`) field and cid substring in
  `ledger.jsonl`, `run.json`, `result.json`; and `created_at`/ledger timestamps are shifted by
  `i × 7 minutes` so clones spread over time (all timestamps of one clone shift by the same
  delta, so durations and phase sums are unchanged). `stream.jsonl`, gate logs and
  `spec_at_open.md` are hardlinked to the base file (falling back to a copy on `OSError`):
  they hold no cid, and the helper never rewrites them.
- `records`: when given, the helper appends clones of the fixture's decisions, mail and
  `events.jsonl` lines (cid rewritten to a clone's, new unique `id`s, timestamps shifted)
  until the sum of those line counts plus the ledger `finding` and `note` lines is at least
  `records`. Without it only the campaign-linked lines of each clone are cloned.
  Escalations are cloned with fresh `ESC-NNNN` ids after the highest existing one.
- Deterministic for a given `seed`: the order, ids and shifts depend only on the arguments and
  the manifest order; `seed` rotates the starting base campaign.
- It makes no network or git calls and imports nothing from `nightwright`.

## Alternatives considered

- **Synthetic generator only (extend `tests/fixtures/dashboard_data.py`)** — cheap, but it
  proves nothing about real ledgers: the epic's inference rules, gaps and the real
  distribution of events are the risk. Synthetic data is kept only for the one derived
  campaign the real data cannot supply.
- **Commit a full copy or a large share of the real data dir** — 1.2 GiB, secrets and local
  paths; unreviewable. A scenario-driven 12-campaign subset under 3 MiB covers the cases.
- **Read the live data dir in tests** — non-hermetic, differs per machine and moves. CI has
  no such dir.
- **Hand-curate the fixture, no tool** — not reproducible, no way to refresh it when the
  schemas gain fields (child 1 adds `run_id`, `key`, `triage`), and no sanitization audit.
- **Fold the fixture into SPEC-0406** — the idea was already split because the data layer's
  review needs the fixture to exist and be reviewable on its own.
- **Pseudonymize project and spec names** — breaks the epic's `project:Meta-Tools` filters
  and adds no safety: those names are not secret.

## Acceptance criteria

- [ ] **AC1.** `tools/capture_history_fixture.py --help` exits 0 and documents `--src`,
  `--out`, `--cid`, `--no-derive-handed-off` and `--check`. The script imports only the
  standard library.
- [ ] **AC2.** Running the tool on the maintainer's real data dir writes
  `tests/fixtures/agentic_history/` with at most 12 campaigns, at most 3 MiB of data files
  (excluding the manifest), and exits 0. The tree is committed.
- [ ] **AC3.** The fixture is a valid data dir: `store.campaigns_dir(cfg)` over a copy lists
  every campaign in the manifest, each `state.json` loads, and every `ledger.jsonl` line is
  valid JSON with a `type` and a timestamp.
- [ ] **AC4.** The manifest's `scenarios` covers `merged_multi_loop`, `abandoned`,
  `handed_off`, `human_escalation`, `pre_stream` and `flaky_and_triage`, each with at least
  one campaign. `legacy_schema` and `no_run_json` are covered or recorded `absent_in_source`.
  The fixture holds at least one MERGED, one ABANDONED and one HANDED_OFF campaign. A derived
  campaign is marked `origin: "derived"` with `derived_from`.
- [ ] **AC5.** `--check` on the committed fixture exits 0: every file is listed with the
  right sha256 and size and no unlisted file exists. Editing, adding or deleting any fixture
  file makes `--check` exit 1 and name the file.
- [ ] **AC6.** The sanitization scan finds nothing in the committed fixture: no home path,
  no user-name path segment, no non-placeholder email, no secret pattern from *Sanitization*
  rule 4, no `env` object with keys. A planted secret in a synthetic source is redacted in
  the output (and counted in the manifest), and a planted secret that no rule redacts makes
  the tool exit 4 with no output left behind.
- [ ] **AC7.** The tool is read-only on the source: a hash of every file under `--src` is
  identical before and after. Two runs with the same source and arguments write byte-identical
  output, `MANIFEST.json` included.
- [ ] **AC8.** Caps hold: no `stream.jsonl` has more than 120 lines or a line over 4 KiB, no
  gate log or `stderr.log` more than 200 lines, no ledger line is dropped (the line count in
  the fixture equals the source's), and a source over 3 MiB makes the tool exit 3.
- [ ] **AC9.** Records: every `campaign` named in the fixture's decisions, mail, events and
  escalations is a fixture campaign; every escalation id cited by a fixture ledger has its
  `ESC-NNNN.json`; `flaky-tests.json` `campaigns[]` lists only fixture campaigns.
- [ ] **AC10.** `tests/history_scale.copy_fixture(dst)` returns a data dir whose file hashes
  equal the manifest's. `clone_fixture(dst, campaigns=500)` returns a data dir with exactly
  500 campaign dirs, all ids unique and valid, `state.json` `id` equal to the directory name,
  no cid of another campaign inside a clone's ledger, and per-campaign sums of `run.end`
  cost and of transition-to-transition durations equal to its base campaign's. With
  `records=50000` the data dir holds at least 50 000 records (decisions, mail, events, ledger
  `finding` and `note` lines) and every `id` in `decisions.jsonl`, `events.jsonl` and in the
  mailbox files is unique. Two calls with the same arguments produce identical trees.
- [ ] **AC11.** The helper leaves the committed fixture unchanged (hashes equal to the
  manifest after `clone_fixture`), and `clone_fixture` onto a non-empty `dst` raises
  `FileExistsError`.
- [ ] **AC12.** Existing readers run on the fixture without error: `queue.dashboard_payload`
  over a copy, and `campaign_export` of every fixture campaign. Neither modifies the copy
  beyond files they already write (the test compares the campaign dirs' hashes).
- [ ] **AC13.** `python3 tools/spec_lint.py` exits 0, and `uv run pytest` is not slowed by the
  fixture: `tests/test_history_fixture.py` completes in under 20 s excluding the 500-campaign
  case, which runs in under 60 s.

## Test plan

**Automated tests.** `tests/test_history_fixture.py` (new). Two kinds of input:
the committed fixture, and a synthetic source dir built in `tmp_path` by a helper in the test
(about 6 small campaigns covering each scenario predicate, with planted home path, email and
secrets, and one oversized `stream.jsonl`). The tests never read the developer's real data dir.

- `test_tool_imports_only_stdlib` (AC1): parse the script's AST; every import is in
  `sys.stdlib_module_names`. `--help` via `subprocess.run([sys.executable, …])` exits 0 and
  mentions each flag.
- `test_capture_synthetic_source` (AC2, AC4, AC8): run the tool as a subprocess on the
  synthetic source into `tmp_path / "out"`; assert exit 0, each scenario in the manifest, the
  derived campaign's `origin`/`derived_from`, state `HANDED_OFF`, no `merged_sha`, caps on
  the stream and logs, and the ledger line counts equal to the source's.
- `test_missing_required_scenario_exits_2` (AC4): drop the abandoned campaign from the
  synthetic source; assert exit 2, the key named, `out` not created.
- `test_size_budget_exits_3` (AC8): inflate a synthetic campaign past 3 MiB with the cap
  lowered by `NW_FIXTURE_MAX_BYTES` (a test-only env override documented in `--help`); assert
  exit 3.
- `test_sanitization_redacts_and_verifies` (AC6): planted `/home/<user>/…`, email,
  `sk-…`, `ghp_…`, `Authorization` header values and a launch `env` are gone from the output
  and counted in `sanitization.rules`; a planted string listed in `tools/capture_history_deny.txt`
  (the test passes an alternate deny file via `NW_FIXTURE_DENY_FILE`) and not otherwise
  redacted makes the tool exit 4 and leaves no `out` dir.
- `test_source_untouched_and_deterministic` (AC7): hash the source tree before/after; run
  twice into two dirs and compare trees byte for byte.
- `test_check_detects_drift` (AC5): on a copy of the synthetic output, `--check` exits 0;
  modify, add and delete one file in turn and assert exit 1 naming the file.
- `test_committed_fixture_matches_manifest` (AC2, AC5): `--check` on the committed fixture
  exits 0; total bytes and campaign count within the AC2 limits.
- `test_committed_fixture_scenarios` (AC4): load the manifest; assert scenario coverage and
  one campaign per required state; for each scenario open the files and re-evaluate its
  predicate on the committed data (so a manifest claim cannot be false).
- `test_committed_fixture_sanitized` (AC6): run the verification scan over the committed
  tree (reuse the tool's `scan()` function, imported via `importlib` from `tools/`).
- `test_committed_fixture_is_a_data_dir` (AC3, AC12): copy via `copy_fixture`, build `cfg`
  with `data_dir=dst`, list campaigns with `store`, load every state and ledger, call
  `queue.dashboard_payload(cfg)` and `campaign_export` for each campaign, and assert the
  campaign dirs' hashes did not change.
- `test_records_reference_fixture_campaigns` (AC9): referential checks over decisions, mail,
  events, escalations, flaky.
- `test_copy_fixture_hashes_match_manifest` (AC10, AC11).
- `test_clone_fixture_500` (AC10, AC13): `clone_fixture(tmp_path / "scale", 500)`; assert
  count, uniqueness, id validity (the campaign id pattern from `store`), cross-cid
  isolation, and the per-campaign cost and duration invariants against the base.
- `test_clone_fixture_records` (AC10): `records=50000`; count and id uniqueness. Marked with
  the repo's slow marker if it exists, otherwise run with `records=5000` and the arithmetic
  asserted; the 50 000 case then runs only when `NW_TEST_SCALE=1`.
- `test_clone_deterministic_and_nondestructive` (AC10, AC11): two calls same arguments,
  identical trees; committed fixture hashes unchanged; non-empty `dst` raises
  `FileExistsError`.
- `test_hardlink_fallback` (AC10): monkeypatch `os.link` to raise `OSError`; the clone is
  complete and equal to the hardlinked one.

Fixtures needed: the committed fixture and the synthetic source builder
(`tests/test_history_fixture.py::build_source`), which later specs may import.

**Manual verification** (done once when the fixture is committed, and again whenever the
capture tool or a source schema changes):

1. `python3 tools/capture_history_fixture.py --src ~/.local/share/nightwright` on the real
   data dir. Expected: exit 0, a summary of the chosen campaigns per scenario, rule hit
   counts, and the total size under 3 MiB.
2. `python3 tools/capture_history_fixture.py --check`. Expected: exit 0.
3. `git status` shows only the fixture tree and `tests/history_scale.py`, and
   `grep -rIn "$HOME" tests/fixtures/agentic_history` prints nothing.
4. Open three fixture files by hand (a stream tail, a decisions line, a mail message) and
   confirm they read as real, redacted content.
5. `uv run nw queue ls` (or the CLI equivalent that accepts a data-dir override such as
   `NW_DATA_DIR=<copy of the fixture>`) against a copy of the fixture and confirm it lists
   the campaigns. This is one real call of the shipped CLI, not a fake.

**Regression guard.** No production module changes, so the existing suites are the guard:
`python3 tools/affected_tests.py --run uv run pytest -q`, the dashboard suite
(`tests/test_agentic_dashboard*.py`) and `tests/test_agentic_campaign_export.py` run unchanged
and green. `python3 tools/spec_lint.py` stays at exit 0. The fixture tree is covered by an
existing or new `.gitattributes` entry (`tests/fixtures/agentic_history/** -text`) so line
endings are not rewritten and the manifest hashes stay valid.

## Rollout / migration

1. Land this spec as `accepted`, then implement: the tool, `tests/history_scale.py`,
   `tests/test_history_fixture.py` (tested on the synthetic source first).
2. Run the capture on the real data dir, review the output (manual steps 3 and 4), commit
   the fixture and the manifest.
3. Mark the SPEC-0406 `depends_on` satisfied; its tests (`tests/test_cli_campaign_history.py`,
   its `history.py` tests, and later `tests/test_history_perf.py`) read the fixture through
   `copy_fixture` and `clone_fixture`.
4. Refresh: when SPEC-0408's `run_id`, `key` and `triage` events have real campaigns, re-run
   the tool (`--cid` pins what to keep) in a follow-up change that only regenerates the
   fixture. The manifest diff shows exactly what moved. When a real HANDED_OFF campaign
   exists, a refresh replaces the derived one automatically.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass (`python3 tools/affected_tests.py --run uv run pytest -q`);
  manual steps performed and noted in the spec.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped (chosen campaigns, final sizes, any rule
  added to the sanitization table).
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).
- [ ] `python3 tools/spec_lint.py` exits 0.

## Open questions

None. Decided here: no `parent` (tracked as its own idea; the epic is cited in prose), the
derived HANDED_OFF campaign is acceptable because the real data has none, project and spec
names are kept, and the 3 MiB / 12-campaign budgets are defaults to be tuned from the first
capture.
