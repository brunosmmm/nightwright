---
id: SPEC-0297
title: "Outbound projects end to end: a merged campaign pulls the portable spec's status back"
status: accepted
kind: feature
owner: user
created: 2026-10-30
updated: 2026-10-30
source_idea: IDEA-038
parent: SPEC-0169
depends_on: [SPEC-0243, SPEC-0257, SPEC-0283]
tags: [agentic, queue, close, outbound, export]
---

## Context

Promoted from idea `IDEA-038`, a later-wave child of epic SPEC-0169: *Outbound projects end
to end — portable file in the target repo plus `pull-status` on merge.*

An **outbound** spec is a `<PROJ>-NNNN` spec authored in wt's outbox
(`<data_dir>/outbox/<project>/`) and exported (`wt spec export`) as a *portable file* into the
target repo, frozen in the outbox copy's `target_spec_path` (SPEC-0065). The human loop then
ends with `wt spec pull-status` (`export.pull_status`), which mirrors the portable file's
`status` / `updated` back into the outbox copy and, the first time that status is `done`,
advances the linked idea `EXPORTED → SHIPPED` (SPEC-0135).

The agentic queue already admits and builds outbound specs:

- `queue.plan_add` resolves an outbound id to its project through `specs.outbound_project`,
  requires the exported file at `target_spec_path` (else "run `wt spec export` first"), and
  queues the target repo's copy of the file (`spec.internal: false`).
- SPEC-0243 reviews an outbound spec with the target project's checkout as root.
- The close step audits the portable file with `verify_spec_file` and merges or hands off per
  the target's `.wt/project.yaml`; `tests/test_agentic_e2e.py::test_e2e_outbound_pr_hands_off`
  covers `policy: pr`.

The loop is **not closed** for outbound specs. After `MERGED` the target repo's file says
`status: done`, but nothing pulls it back: `queue._reconcile_closed` runs
`specs.reconcile_ideas`, which only knows wt's own spec store, so for an outbound campaign it
records the info finding "idea reconcile skipped: spec store is not this repo" and stops. The
outbox copy keeps `status: accepted`, the idea stays `EXPORTED`, and a human has to remember
`wt spec pull-status` (or `wt spec sweep`). The same hole exists for a `HANDED_OFF` campaign
whose PR the SPEC-0283 sweep later moves to `MERGED`. Nor is there an end-to-end test of an
outbound campaign that *merges* (only the PR hand-off variant), so the outbound path through
`merge` policy is unproven.

## Goals / Non-goals

**Goals**
- When an outbound campaign reaches `MERGED` (directly with `merge.policy: merge`, or later
  via the HANDED_OFF sweep), the close bookkeeping runs the existing `export.pull_status` for
  the spec, so the outbox copy shows `done` and the linked `EXPORTED` idea becomes `SHIPPED`
  with no human action.
- The pull is best-effort and observable: a failure never changes the (irreversible) merge
  outcome, and every pull — done, not-yet-done, failed — is visible in the campaign ledger and
  in `wt campaign log`.
- A `HANDED_OFF` outbound campaign does **not** pull (the merged-to-base file does not exist
  yet); it is pulled when the sweep records the merge.
- An end-to-end test proves an outbound spec goes accepted → queued → built → merged →
  outbox `done` + idea `SHIPPED`, so the whole outbound path is covered for both merge
  policies.

**Non-goals**
- No change to `pull_status` itself, to `wt spec export`, to the portable file format, or to
  `pull-clock` / `wt spec sweep` (still the manual tools).
- No automatic `wt spec export` at `wt queue add`; an unexported outbound spec is still refused
  with the existing message.
- No change to how internal (`SPEC-NNNN`) campaigns reconcile their ideas.
- No writes to the target repo's main checkout (a spec-file write there would park the next
  close; the pull only *reads* the target and writes the outbox copy and the ideas file).

## Decision

`queue._reconcile_closed` branches on `specs._is_outbound_spec_id(spec_id)`. For an outbound
spec it skips `reconcile_ideas` and, when the outcome is `MERGED`, calls
`export.pull_status(cfg, spec_id)` with no `to`, so the frozen `target_spec_path` (SPEC-0065)
is read: the same file `queue.plan_add` queued. Only an outbox copy with no frozen path (an
old export) is pulled with `to=<campaign repo.root>`. The result is ledgered as a new
`outbound_pulled` event; any exception becomes a minor `bookkeeping` finding, exactly like the
existing reconcile failure path. For `HANDED_OFF` it does nothing beyond a note that the pull
waits for the merge. Because `_sweep_write` already calls `_reconcile_closed(…, "MERGED")`,
the sweep path needs no extra wiring.

## Design

**`src/wt/agentic/queue.py`**
- `_reconcile_closed(cfg, state, events, outcome)`: after the `spec_id` guard, if
  `SP._is_outbound_spec_id(spec_id)` → `return _pull_outbound(cfg, state, events, spec_id,
  outcome)`. The internal path is untouched (including `_mark_handed_off`).
- `_pull_outbound(...)` (new, same file, next to `_mark_handed_off`):
  - `outcome != "MERGED"` → append an `info` finding (`gate: bookkeeping`, title "outbound
    pull-status waits for the merge") and return.
  - `from .. import export as EX`; `path, status = EX.pull_status(cfg, spec_id)`, with no
    `to`, so the frozen `target_spec_path` (SPEC-0065) is read, which is the same file
    `queue.plan_add` queued. Only when the outbox copy has no `target_spec_path` (an old
    export) pass `to=state["repo"]["root"]`, the repo the file merged into. Note that an
    explicit `to` always overrides the frozen path in `_portable_dest_path`
    (`src/wt/export.py:180-195`), so it must not be passed when the path is frozen.
  - Before the call, snapshot the linked idea's state with the same `_ideas(cfg)` /
    `SP._sole_claimant` helpers `_mark_handed_off` uses; after it, re-read. Append
    `("outbound_pulled", {"spec_id", "status", "outbox_path": str(path), "idea_id", "from",
    "to"})`; `idea_id`, `from`, `to` are `None` when no idea claims the spec or its state did
    not move.
  - `status != "done"` (e.g. the user's local target checkout has not yet pulled a PR that was
    merged on the remote) → still ledger `outbound_pulled`, plus a minor finding `outbound
    pulled status is <status>, not done` with detail "run `wt spec pull-status <id>` after
    pulling the target repo"; the idea is untouched (that is `pull_status`'s own rule).
  - `ValueError` / `OSError` / any `Exception` → minor `bookkeeping` finding "outbound
    pull-status failed" with the message; never raises, never alters the campaign state.
- Only the main-thread close/sweep paths call `_reconcile_closed`, already under `item_lock`.

**`src/wt/agentic/schemas.py`**: `EVENT_KEYS["outbound_pulled"] = ("spec_id", "status",
"outbox_path", "idea_id", "from", "to")`, so the ledger validator (SPEC-0192) accepts it.

**`src/wt/render.py`**: a `campaign` event line for `outbound_pulled`
(`outbox <id> → <status>; idea <idea_id>: <from> -> <to>`), next to `idea_reconciled`.

**Docs**: `docs/product/capabilities/agentic.md` gains an "Outbound projects" paragraph:
export first, `wt queue add <PROJ-NNNN>`, merge pulls status back, handed-off specs pull on
the sweep's merge; `docs/product/loop/states.md` notes the `outbound_pulled` event.

**Ordering note.** Idea linkage (SPEC-0292, `_release_idea_link`) is untouched; the pull only
advances `EXPORTED → SHIPPED` via `pull_status`, which also archives through the SPEC-0073 hook.

## Alternatives considered

- **Extend `specs.reconcile_ideas` to outbound ids** — it reads wt's spec store and
  `source_idea` backlinks; outbound specs live in the outbox with `:SPEC:` links set at export,
  and `pull_status` already encodes the `EXPORTED → SHIPPED` rule. Reusing `pull_status`
  avoids a second implementation.
- **A separate `wt spec sweep` call by the conductor** — keeps the loop open and relies on a
  skill step a human can forget; the close step already owns this bookkeeping for internal
  specs (SPEC-0257).
- **Pull on `HANDED_OFF` too** — the PR branch's file says `done` but the base does not; the
  target checkout still shows the old status, so it would record a wrong or no-op pull.
- **Write the status into the target repo's main checkout** — would dirty the main checkout
  and park the next close (the reason SPEC-0257 uses `backlinks=False`).

## Acceptance criteria

- [ ] With `merge.policy: merge`, an outbound spec queued with `wt queue add <PROJ-NNNN>`
      reaches `MERGED`; its outbox copy then has `status: done`.
- [ ] The idea linked to that outbound spec (state `EXPORTED`) is `SHIPPED` after the merge
      without any `wt spec pull-status` call.
- [ ] The campaign ledger has exactly one `outbound_pulled` event with `spec_id`, `status:
      done`, the outbox path, and the idea's `from`/`to`; `wt campaign log` renders it.
- [ ] With `merge.policy: pr` the campaign ends `HANDED_OFF`, the outbox copy and idea are
      unchanged, and the ledger has an info `bookkeeping` finding that the pull waits for the
      merge and no `outbound_pulled` event.
- [ ] When the HANDED_OFF sweep records the PR as merged, the same pull runs: with the target
      checkout holding the `done` file the outbox copy turns `done` and the idea `SHIPPED`;
      with the target checkout still holding a non-`done` status, `outbound_pulled` is ledgered
      with that status, a minor finding is added, and the idea is unchanged.
- [ ] A failing `pull_status` (outbox copy missing or ambiguous, portable file unreadable)
      leaves the campaign `MERGED`, adds a minor `bookkeeping` finding "outbound pull-status
      failed", and raises nothing.
- [ ] An outbound campaign no longer records the "idea reconcile skipped: spec store is not
      this repo" finding; an internal campaign's reconcile behaviour and events are unchanged.
- [ ] The pull reads the outbox copy's frozen `target_spec_path` (no `to` passed); `to=<repo
      root>` is passed only when the outbox copy has no `target_spec_path`.
- [ ] `outbound_pulled` passes the ledger validator and is documented in
      `docs/product/capabilities/agentic.md`.
- [ ] `uv run pytest` and `python3 tools/spec_lint.py` pass (docs build included).

## Test plan

- **Automated tests:** new `tests/test_agentic_outbound_e2e.py`, reusing the helpers of
  `tests/test_agentic_e2e.py` (`make_repo`, `make_cfg`, `script`, `impl_done`, `review`,
  `fake_gh`, `write_review`) and an outbox target `{"acme": {"repo_path": repo}}`:
  1. `test_outbound_merge_pulls_status_and_ships_idea` — policy `merge`, an `EXPORTED` idea in
     the temp org file claiming `ACME-0001`; `queue run --until-idle`; assert `MERGED`, outbox
     file `status: done`, idea `SHIPPED`, one `outbound_pulled` event (AC 1–3).
  2. `test_outbound_pr_handoff_does_not_pull` — policy `pr`; assert outbox copy still
     `accepted`, idea `EXPORTED`, the info finding, no `outbound_pulled` (AC 4).
  3. `test_outbound_sweep_merge_pulls` — a `HANDED_OFF` outbound campaign with a fake `gh pr
     view` reporting `MERGED`; two sub-cases: target checkout file `done` → pulled and
     `SHIPPED`; file still `in-progress` → `outbound_pulled` status `in-progress`, minor
     finding, idea `EXPORTED` (AC 5).
  4. `test_outbound_pull_failure_is_a_finding` — remove the outbox copy before close;
     campaign `MERGED`, minor "outbound pull-status failed" finding (AC 6).
  5. `test_outbound_pull_uses_frozen_target_path` — export into a non-default spec dir so
     the frozen `target_spec_path` differs from the `spec_dir` derivation; the pull reads
     that file (outbox `done`); a second case strips `target_spec_path` from the outbox
     copy and asserts the pull falls back to `<repo root>/<spec_dir>/` (AC 8).
  6. `test_internal_campaign_unchanged` — an internal merge still emits `idea_reconciled` and
     no `outbound_pulled`, and an outbound one has no "reconcile skipped" finding (AC 7).
- `tests/test_agentic_logs.py` (ledger schema) gets an `outbound_pulled` validity case;
  `tests/test_render.py` a rendering case for the event (AC 3, 9).
- Any defect in the outbound `merge` path that test 1 exposes (spec-DoD audit, rel path,
  worktree setup) is fixed in this spec's change and noted under "As shipped".
- **Manual verification:** in a scratch git repo registered as an `outbox_targets` project:
  `wt spec new`-style outbound spec → `wt spec export ACME-0001` → accept → `wt queue add
  ACME-0001 --yes` → `wt queue run --until-idle` with the fake runner; then `wt spec status`
  / `cat` the outbox copy shows `done`, `wt campaign log <id>` shows `outbound_pulled`.
- **Regression guard:** `uv run pytest` (existing `test_agentic_e2e.py`,
  `test_agentic_pr_sweep.py`, `test_agentic_idea_link.py`, `test_export.py`,
  `test_docs_*`) and `python3 tools/spec_lint.py`.

## Rollout / migration

No data migration. Campaigns already `MERGED` are not back-filled; `wt spec sweep` remains
the way to reconcile earlier outbound specs. Merge via the normal campaign close.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions taken above (best-effort pull; pull only on `MERGED`; no target-checkout writes)._
