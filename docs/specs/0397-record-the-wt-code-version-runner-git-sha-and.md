---
id: SPEC-0397
title: "Record the nw code version and resolved-profile hash on each campaign open, and export them"
status: accepted
owner: user
created: 2026-11-02
updated: 2026-11-02
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, campaign, export, analysis, provenance]
source_idea: IDEA-420
depends_on: [SPEC-0236, SPEC-0238]
---

## Context

Promoted from `IDEA-420`. nightwright changed heavily during the 2026-10-25..30 campaigns, so
campaign outcomes (loops, cost, parks, review score) shift with the tool as well as with the
task. [SPEC-0236](./0236-wt-campaign-export-dataset-for-mis-sizing-analysis.md) exports one row
per campaign for after-the-fact analysis, but nothing in that row says which version of the
tool, or which effective profile, produced it. An analysis cannot stratify or time-split by
tool version.

What exists today:
- `store.create_campaign` (`src/nightwright/agentic/store.py:164`) writes the campaign's `open`
  ledger event at admission (`queue add`, `queue add-idea`, `queue merge`). Its payload carries
  the profile *name*, `profile_sources` ([SPEC-0238]), sizing, base and branch, but no code
  version and no content hash of the profile. A profile name is not stable: the same name
  resolves differently after a layer (`config.yaml`, `.nw/profiles`) is edited.
- The runner already knows its own code sha: `lease.code_sha` is taken with
  `git rev-parse HEAD` of `codewatch.code_checkout()` (`queue.py:5615-5621`, SPEC-0375).
- All three plan builders already hold the resolved profile dict (`plan_add` `queue.py:7044`,
  `plan_merge` `queue.py:7280`, `plan_add_idea` `queue.py:7359`).
- `campaign_export.campaign_row` reads the `open` event (`campaign_export.py:216`) and the
  columns list is additive within `nw.campaign.export.v1` (SPEC-0299, SPEC-0303 added columns).

## Goals / Non-goals

**Goals**
- G1. Every newly created campaign's `open` event records `nw_sha`, the git sha of the nightwright
  code checkout of the process that admitted it, and `profile_sha256`, a hash of the resolved
  profile in force at admission.
- G2. `nw campaign export` adds two columns to the `campaigns` table, `nw_sha` and
  `profile_sha256`, so an analysis can group by or order by tool version.
- G3. A campaign opened before this change, or admitted where the code is not a git checkout,
  exports `null` in both columns; nothing breaks and nothing is back-filled.

**Non-goals**
- Tracking the code version across the campaign's lifetime. The runner re-execs on code
  changes (SPEC-0229), so a long campaign can run under several shas; only the sha at open is
  recorded. A per-run sha on `run.start` is a possible follow-up.
- Recording a dirty-tree flag or a diff of uncommitted changes. The sha identifies commits only.
- A mapping from sha to date, version tag or changelog entry; `git log` does that.
- Back-filling past campaigns.
- Adding the columns to the `runs` or `findings` tables; both join to `campaigns` by `campaign`.
- Changing the schema id `nw.campaign.export.v1`; the columns are additive.

## Decision

At admission, `create_campaign` stamps two new keys into the `open` event: `nw_sha`
(`git rev-parse HEAD` of `codewatch.code_checkout()`, or `null`) and `profile_sha256`
(sha256 of a canonical JSON form of the resolved profile, supplied by the plan builder through
the campaign state, or `null`). `campaign_export` reads both from the `open` event into two new
`str` columns on the `campaigns` table. Both are additive: the `open` event's required keys in
`schemas.EVENT_KEYS` are unchanged, and the export schema stays `nw.campaign.export.v1`.

The sha is that of the admitting process, which for the queue is the runner that creates the
campaign or the CLI invocation that queues it; both run the checkout that is on disk (the
runner re-execs when it changes), so this is the tool version the campaign starts under.

## Design

**Profile hash.** A new helper `profiles.profile_sha256(profile: dict) -> str` in
`src/nightwright/agentic/profiles.py`: copy the resolved dict, drop the `sources` key (origin
paths differ per machine and are already recorded as `profile_sources`), then
`hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
default=str).encode("utf-8")).hexdigest()`. The full 64-hex digest is kept, matching
`review_sha256` and `prompt_sha256`. The `name` and `description` stay in the hashed document,
so a rename changes the hash; two machines with the same effective profile get the same hash.

**Plans and state.** `plan_add`, `plan_add_idea` and `plan_merge` add
`"profile_sha256": profile_sha256(prof)` to the plan; `apply_add`, `apply_add_idea` and
`apply_merge` copy it to `state["profile_sha256"]` (next to `profile_sources`). The existing
`resolve_profile` calls are reused: no second resolution, no new failure mode. The plans do
not persist the plan dict, so the state key is the carrier.

**Code sha.** A helper `codewatch.code_sha() -> str | None` returns
`_git_out(root, "rev-parse", "HEAD")` for `code_checkout()`, or `None` when it is not a
checkout or git fails (`_git_out` already swallows `OSError` and timeouts, and has a 10 s cap).
It reuses the existing function the runner's `lease.code_sha` is built from, so the two never
disagree. `create_campaign` calls it once per campaign. If importing `codewatch` into `store`
creates an import cycle, the implementer may take the sha as a keyword argument of
`create_campaign` and have the three `apply_*` callers pass it; the event payload is the same.

**Open event.** In `create_campaign`, the payload gains `"nw_sha": code_sha()` and
`"profile_sha256": state.get("profile_sha256")`, always present (value `null` when unknown) for
campaigns created after this change. `EVENT_KEYS["open"]` is not extended, so older ledgers and
the ledger validator keep passing; a test pins that the two keys are optional.

**Export.** `campaign_export.CAMPAIGN_COLUMNS` gains `("nw_sha", "str")` and
`("profile_sha256", "str")`, appended after the SPEC-0303 columns. `campaign_row` fills them
from the first `open` event in `events`; a missing event or key yields `None`. A `NOTES` entry
documents both: that `nw_sha` is the code at admission only, that it is `null` for earlier
campaigns and for non-checkout installs, and that `profile_sha256` is of the resolved profile
without `sources`. CSV, JSONL and Parquet all get the columns through the existing
`COLUMNS` table; the manifest lists them automatically.

## Alternatives considered

- **Record the sha only in `state.json`.** The export and every other analysis read the ledger;
  the `open` event is where SPEC-0219 and SPEC-0238 already put sizing and profile data.
- **Read the runner's `lease.code_sha` instead of the admitting process's checkout.** Adds a
  lease read with no stability guarantee (a lease may be absent for one-shot tick and CLI
  admission), for the same value in practice; the runner re-execs on code change.
- **`nw_version` (package version) instead of a sha.** Not distinct between commits; the
  tool changed daily without a version bump.
- **Hash the profile with `sources`.** Origin paths would split identical profiles across
  machines and checkout locations.
- **A dirty-tree flag.** Would need a `git status` per campaign; the queue runs from clean
  checkouts by design. Left as a follow-up if it turns out to matter.

## Acceptance criteria

- [ ] A campaign created by `apply_add`, `apply_add_idea` and `apply_merge` has an `open`
      ledger event with `nw_sha` equal to `git rev-parse HEAD` of the code checkout, and
      `profile_sha256` a 64-hex string.
- [ ] `profile_sha256` is identical for two resolutions of the same profile and differs after
      any change to a resolved profile field (e.g. a persona model); it ignores `sources`.
- [ ] When the code is not a git checkout (or `git` fails), campaign creation succeeds and the
      `open` event has `nw_sha: null`.
- [ ] If the profile cannot be resolved the existing plan error still raises exactly as before;
      the hash adds no new failure.
- [ ] `nw campaign export` writes `nw_sha` and `profile_sha256` columns (type `str`) in the
      `campaigns` table in JSONL, CSV and (when `pyarrow` is importable) Parquet, and lists
      them in `manifest.json`; the schema id stays `nw.campaign.export.v1`.
- [ ] A campaign whose `open` event lacks both keys (created before this change) exports with
      both columns `null`, and the other columns are unchanged.
- [ ] `schemas.EVENT_KEYS["open"]` is unchanged and a ledger containing the old `open` payload
      still validates.
- [ ] The export `NOTES` document the new columns and their limits.

## Test plan

- **Automated tests**
  - `tests/test_agentic_store.py`: `create_campaign` with `state["profile_sha256"]` set writes
    both keys into the `open` event (monkeypatch `codewatch.code_sha` to a fixed sha); with the
    patch returning `None` the event has `nw_sha: null` and creation succeeds; a state without
    `profile_sha256` writes `null`.
  - `tests/test_agentic_profiles.py`: `profile_sha256` is stable across two `resolve_profile`
    calls, ignores `sources`, changes when a resolved field changes, and returns 64 hex chars.
  - `tests/test_agentic_queue.py` (plan/apply for `queue add`) and `tests/test_agentic_queue_merge.py` (merge):
    `apply_add`, `apply_add_idea` and `apply_merge` each produce an `open` event whose
    `profile_sha256` equals `profile_sha256(resolve_profile(...))`.
  - `tests/test_agentic_campaign_export.py`: the `campaigns` columns include `nw_sha` and
    `profile_sha256` with type `str`; a campaign fixture with the keys exports them; a fixture
    with an old-style `open` event exports `None` for both and every other column is equal to
    the pre-change value; the JSONL, CSV and Parquet (skipped without `pyarrow`) renderings
    carry the columns; `manifest.json` lists them; `EXPORT_SCHEMA` is still `nw.campaign.export.v1`.
  - The existing ledger-schema test (grep `EVENT_KEYS` under `tests/`): an `open` event
    without the new keys validates.
- **Manual verification:** in a scratch data dir run `nw queue add <SPEC> --project <p>` then
  `nw campaign export --out /tmp/exp` and check `campaigns.jsonl` for `nw_sha` equal to
  `git -C <nightwright checkout> rev-parse HEAD`. Edit the profile in `config.yaml`, add a
  second campaign, and confirm the two campaigns' `profile_sha256` differ.
- **Regression guard:** run the affected tests
  (`python3 tools/affected_tests.py --run uv run pytest -q`), which include
  `test_agentic_campaign_export.py`, `test_agentic_store.py`, `test_agentic_queue.py` and `test_agentic_queue_merge.py`;
  existing column-list assertions are updated only to add the two new columns.

## Rollout / migration

No data migration. Campaigns created before this lands keep their `open` events unchanged and
export `null`. The new columns appear in the next `nw campaign export` run. Analysts who
need a version stratum for earlier campaigns derive it from `created_at` and `git log`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

None.
