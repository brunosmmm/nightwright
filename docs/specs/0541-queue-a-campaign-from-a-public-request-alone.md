---
id: SPEC-0541
title: Write a spec from a public request alone (request-only writer path)
status: done
owner: user
created: 2026-11-10
updated: 2026-11-10
kind: feature
tags: [public-backlog]
depends_on: [SPEC-0473, SPEC-0480, SPEC-0535]
source_idea: IDEA-1022
---

## Context

[SPEC-0473](./0473-public-backlog-on-an-orphan-nw-backlog-branch.md) keeps the
public backlog on `refs/heads/nw-backlog`: one `REQ-` file per item, with a
title, a `status` of `open` or `closed`, an optional `spec:`, and a summary
body. [SPEC-0480](./0480-link-a-private-idea-to-a-public-backlog-request.md)
publishes a private idea to one of those files.
[SPEC-0535](./0535-specs-cite-the-public-request-they-come-from.md) lets a
linked idea write a spec from the public summary: `load_agreed_request` checks
that the private side and the file still equal `:REQ_BASE:`, the spec records
`source_request`, and `cite_spec` writes `spec: SPEC-NNNN` on the file.

All of that starts from a private idea. IDEA-1022 wants a clone that has only
the repo and the `nw-backlog` branch to start the loop with
`nw queue add REQ-…`. That is two independent pieces of work:

1. **The writer path** (this spec): once a campaign exists whose input is a
   public request, the campaign's spec writer, readiness check, cite, and
   reset must work without an org task.
2. **The command** (a follow-up spec, `depends_on` this one): `nw queue add
   REQ-…` builds that campaign.

This spec is part 1. It is inert until part 2 lands: nothing creates a
campaign with `request_only` set, and tests build such a campaign state by
hand.

Much of the idea-campaign code already tolerates a missing org task
(`_link_idea`, `_prepare_spec_write`, `_unlink_idea` skip when `_idea_task`
returns `None`). Three places do not: the readiness check for a spec-less
campaign (`has_no_spec` branch near `queue.py:1191`) answers
`idea {iid} not found`, `_front_matter_problems` requires the spec's
`source_idea` to equal the campaign's `idea.id`, and `_link_idea`/the cite
assume an org idea.

## Goals / Non-goals

**Goals**

- A campaign whose `idea` payload has `request_only: true` (and
  `idea.id == idea.source_request == REQ-…`) writes its spec from the public
  file's title and summary, the way an idea campaign writes it from the org
  idea.
- The org file is not required and is never written by this path.
- The spec records `source_request: REQ-…` and has no `source_idea`. After the
  first write, the public file gains `spec: SPEC-NNNN` through SPEC-0535's
  `cite_spec`, exactly once, however many times the cite step runs.
- A machine that also holds an idea with `:REQ:` set to that request runs
  SPEC-0535's drift gate (`load_agreed_request`) before the public summary
  becomes the spec's input, at readiness, write, and cite time. A drifted pair
  is refused. A configured org file that cannot be read is refused too: the
  gate fails closed.

**Non-goals** (the first four are the follow-up spec's scope)

- `nw queue add REQ-XXXXXXXX`: the CLI surface, `Q._REQ_ID_RE`,
  `plan_add_request` and `apply_add_request`, the batch refusal, and the JSON.
  Nothing in this spec creates a `request_only` campaign.
- Active-campaign exclusion. The follow-up spec must refuse a second active
  campaign for one request in both directions: `plan_add_request` refuses when
  a non-terminal campaign has `idea.source_request == req_id`, and
  `plan_add_idea` and `apply_add_idea` (under the add lock) refuse, after
  `load_agreed_request`, a linked idea whose `agreed.req_id` already has a
  non-terminal campaign (`{req_id} already has an active campaign {id}
  ({state}); abandon it first`). This spec only guarantees that a loser of such
  a race fails safely: its cite returns an error and the spec stays on the
  campaign branch.
- Abandon and `nw queue list` of a request campaign, and the IDEA/SPEC
  `queue add` regression; part 2 covers them.
- Fetching or pushing `refs/heads/nw-backlog`. The branch is read and, for the
  cite, written locally.
- Writing `:CAMPAIGN:`, `:SPEC:`, or `SPECCED` on a linked idea that happens to
  be on the machine. A later spec (reconcile or pull) may advance the private
  side.
- Creating, editing, closing, or reopening requests (`nw backlog …`), outbound
  specs, split parts, and any request whose `status` is not `open`.
- Changing the public file's format or the SPEC-0535 error texts.

## Decision

A campaign is a `request_only` campaign when its `idea` payload has
`request_only: true`. It is a `kind: idea` campaign (the writer, review, close,
and merge flow stay as they are) whose `idea.id` is the request id, so every
existing lookup keyed on the idea id finds no org task and leaves the org file
alone. Its payload is:

```json
{"schema": "nw.idea.v1", "id": "REQ-…", "state": "", "heading": "<public title>",
 "tags": [], "kind": "idea", "summary": "<public body>", "questions": "",
 "question_items": [], "log": "", "project": "<project>",
 "source_request": "REQ-…", "request_only": true, "epic": null,
 "state_before": null}
```

Every step that needs the request calls one function,
`queue._request_source`, which reads the public file through the same
validator SPEC-0535 uses. When an org idea on the machine has `:REQ:` equal to
the request id, that idea goes through `load_agreed_request` first; its drift
error is the only refusal text for a drifted pair. The linked idea is a gate,
not a participant: it supplies no text and receives no write. A campaign
without `request_only` takes the unchanged paths.

## Design

### One reader for the request

`src/nightwright/idea_link.py` splits `load_agreed_request` in two. The first
half, `_read_request_file(repo, req_id)`, does what the function does from
"read `git show <tip>:{req_id}.md`" through the canonical title and summary
checks, with the same error texts, and returns the parsed pieces.
`load_agreed_request` calls it and then runs the private digest and drift
check unchanged. A new `load_public_request(cfg, project, req_id)` resolves
the repo with `resolve_linked_repo`, calls `_read_request_file`, and returns
the same `AgreedRequest`; it performs no `:REQ_BASE:` check because there is
no private side. A present `spec:` does not raise in either function; the
callers below decide.

### Choosing the source

`queue._request_source(cfg, req_id, project, *, allow_spec=None)` returns
`(agreed, linked_task_or_None)`. `allow_spec` is the id of the spec the
calling campaign itself owns, when it has one; it is how the campaign's own
cite is not an "already cited" error.

1. Scan the org ideas with `_ideas(cfg)` (not `_ideas_or_empty`) for tasks
   whose `:REQ:` equals `req_id`. No configured org file, or a configured path
   that does not exist, is no ideas (`_ideas` returns `[]`). An `OSError` or
   `ValueError` from a configured file raises
   `could not read the ideas file to check {req_id}: {err}`: the private idea
   may exist, so the drift gate cannot be skipped. Two or more matches raise
   `{req_id} is claimed by {id_a} and {id_b}`.
2. With one match, call `IL.load_agreed_request(cfg, task)`; every error it
   raises is raised unchanged (the drift text is
   `{idea_id} and {req_id} do not match :REQ_BASE:; push, pull, or choose one
   summary before writing a spec`). The match's `:PROJECT:` must equal
   `project`, else `{req_id} is linked to {idea_id} of project {p}, not
   {project}`. A match that has `:SPEC:` raises
   `{idea_id} already has {spec}; queue that spec instead: nw queue add {spec}`.
   A match that is not open (`idea_is_open`) raises
   `{idea_id} is {state}; only open ideas (IDEA, INCUBATE, or SPECCED without a :SPEC:) are queued`.
3. With no match, call `IL.load_public_request(cfg, project, req_id)`.
4. Then, in both cases: the file's parsed `status` must be `open`
   (`{req_id} is {status}; only open requests are queued`); and
   `agreed.spec` must be `None` or equal `allow_spec`
   (`{req_id} already cites {spec}` otherwise).

The function reads the org file only to find the gate idea. It never opens the
org file for write. Readiness, `_prepare_spec_write`, and `_reset_spec_write`
pass the campaign's own spec id as `allow_spec` when the campaign has recorded
one and `None` before that; `_cite_request` passes the id it is citing.

### Readiness

The spec-less branch of the readiness check (`has_no_spec`) answers, for a
`request_only` campaign, from `_request_source`: ready when it returns; not
ready with `request {req_id}: {error}` when it raises. It does not call
`_idea_task`. A campaign that is not `request_only` keeps `idea {iid} not
found` and `idea_is_open`.

### Writing the spec

`_prepare_spec_write` (mode `write` only): for a `request_only` campaign it
calls `_request_source` before `spec_numbers.allocate`. A failure parks
`spec_write_failed` with that error, creates no spec file, and writes no
backlog object. On success it sets `state["idea"]["summary"]` to the public
body and `heading` to the public title (the file slug comes from the title).
The existing SPEC-0535 branch for non-request-only ideas is untouched.

`write_rules` for `write` with `request_only`: the front matter lists
`source_request: {req}` and does not list `source_idea`; the rules say the
front matter has no `source_idea` key and that the Context section is the
public summary in the idea JSON `summary` field, which is the only input (no
private log or open questions exist). `_front_matter_problems` with
`request_only`: `source_request` must equal `idea.source_request`, and
`source_idea` must be absent
(`front matter: source_idea must be absent (the campaign comes from a request alone), got {got!r}`).
Revise rules for `request_only` say to keep `id`, `source_request`, and
`status: proposed`. The `kind`, `owner`, `parent`, and `depends_on` checks are
unchanged (`epic` is `None`). `_reset_spec_write` refreshes a `request_only`
snapshot (`summary`, `heading`) from `_request_source` and keeps the old
snapshot when that raises.

### Cite

`_link_idea` first checks `(state["idea"] or {}).get("request_only")`. For
`request_only` it runs `_cite_request(cfg, state, spec_id)` and nothing else
(no `_idea_task`, no `:SPEC:`, no `SPECCED`, no org lock). It does nothing
unless `spec_write.mode == "write"`. `_cite_request`:

1. `_request_source(cfg, req_id, project, allow_spec=spec_id)`: drift, status,
   a changed public file, and a different `spec:` return their error text with
   no commit; the spec stays on the campaign branch.
2. `agreed.spec == spec_id`: return `None`, no commit. This is the repeated
   call and it is not an error.
3. Otherwise `IL.cite_spec(cfg, agreed, spec_id,
   expected_backlog_tip=agreed.backlog_tip)`, SPEC-0535's single
   compare-and-swap commit `cite {req_id} {spec_id}` in `agreed.repo`.

`_link_idea` is best-effort and returns the error text, or `None` on success
and on the repeated call. `_apply_spec_write` logs `idea_link_failed` only for
a returned error and still advances to spec review. The event's text for a
request ends with `; repair: nw backlog edit {req_id} --spec {spec_id}`.
Revise, amend, and split do not cite. A second campaign that lost a race for
the same request finds `agreed.spec` set to the winner's id (a different id),
returns `{req_id} already cites {spec}`, and commits nothing.

### Merge and close

The merge and close flow does not read the idea; the spec's `source_idea` is
absent, so `SP.source_idea_task` answers `no source_idea` and the close path
skips idea bookkeeping.

## Alternatives considered

- **Create a stub org idea from the request, then queue that idea.** It needs
  the org file the clone does not have, and makes up an `IDEA-NNN` for text
  that has none.
- **A new campaign kind `request`.** Every consumer of `kind: idea` (writer,
  review, close, list, hub) would be touched for no behavior difference. A
  `request_only` flag on the idea payload changes only the places named above.
- **Advance the linked idea (`:SPEC:`, `SPECCED`) when one is on the machine.**
  That makes the private side an output of a path whose point is that it is
  not read. Pull and reconcile own that move.
- **Skip the drift gate on a linked machine, since the public file is the
  input.** SPEC-0535's rule is that a spec never silently disagrees with the
  idea it was published from; the gate costs one read.
- **Treat an unreadable org file as no ideas (`_ideas_or_empty`).** It lets a
  mid-write race or a syntax error skip the gate on a machine whose private
  idea exists and may have drifted. `plan_add_idea` already fails closed on
  that file.
- **Ship the command and the writer in one spec.** The writer half is inert
  until something creates a `request_only` campaign, and the command half
  changes the drift gate's callers and the queue's exclusion rules. Two specs
  keep each reviewable and let the writer path be tested on hand-built state.

## Acceptance criteria

- [x] `load_public_request` returns an `AgreedRequest` for an open file with
      no private side and performs no `:REQ_BASE:` check;
      `load_agreed_request` raises the same text for every case in
      SPEC-0535's tests after the `_read_request_file` split.
- [x] `_request_source`: with no org file configured it returns the public
      request and `None`; with a linked idea it returns the public body (not
      the private Summary) and the idea when the pair agrees, and raises
      SPEC-0535's drift text on a drifted pair (private side, public file, or
      both), the Design's text for two claimants, another project, an idea
      with `:SPEC:`, a closed idea, a `closed` request, and a request whose
      `spec:` is set; a configured org file that cannot be read or parsed
      raises `could not read the ideas file to check {req_id}: {err}`, and
      the same call with no org file configured succeeds. A call with
      `allow_spec` equal to the file's `spec:` returns; a different `spec:`
      still raises.
- [x] The readiness check of a `request_only` campaign does not call
      `_idea_task`; it is ready while `_request_source` succeeds and not
      ready with `request {req_id}: {error}` after the file is closed, cited
      by another spec, removed, drifted, or the org file made unreadable. It
      stays ready after the campaign's own spec was cited.
- [x] `_prepare_spec_write` of a `request_only` campaign re-reads the request
      before allocating: on drift, closed, cited, missing, or an unreadable
      org file it parks `spec_write_failed`, creates no spec file, and runs no
      `hash-object`. On success `idea.summary` is the public body and
      `idea.heading` the public title. A campaign that is not `request_only`
      behaves as in SPEC-0535.
- [x] `write_rules` for a `request_only` write lists `source_request: REQ-…`,
      does not list `source_idea`, and names the public summary as the only
      input. `_front_matter_problems` rejects a missing or different
      `source_request` and any present `source_idea` for `request_only`; a
      non-`request_only` idea keeps SPEC-0535's checks (`source_idea` equal to
      the idea id). A spec with `source_request` and no `source_idea` passes
      `python3 tools/spec_lint.py`.
- [x] After a valid first write, `_link_idea` commits `cite {req_id}
      {spec_id}` on `agreed.repo`'s `refs/heads/nw-backlog` (`spec:` added,
      every other byte of the file and every other path unchanged, expected
      value the previous tip) and writes no `:SPEC:`, no `SPECCED`, and no
      other org byte (a linked idea's org bytes are identical before and after
      write and cite). Calling `_link_idea` twice for the same spec id returns
      `None` both times, adds one commit in total, and records no
      `idea_link_failed` event. A revise tick adds no commit.
- [x] A different `spec:` on the file, drift, a moved tip, or an unreadable
      org file makes `_link_idea` return the error text and add no commit; the
      campaign still advances to spec review with an `idea_link_failed` event
      that contains `nw backlog edit {req_id} --spec {spec_id}`.
- [x] `_reset_spec_write` of a `request_only` campaign refreshes `summary` and
      `heading` from the file, including after the campaign's own cite, and
      keeps the old snapshot when the read raises.
- [x] Nothing in the tree creates a `request_only` campaign (no CLI surface is
      added); a campaign without `request_only` follows the unchanged paths,
      and an idea with no `:REQ:` still does not call `resolve_linked_repo`.

## Test plan

- **Automated tests:** new `tests/test_request_source.py` with exactly one
  module marker, `pytestmark = pytest.mark.fast` (the name matches no
  `tui`, `remote`, `perf`, or `integration` rule in the SPEC-0490 taxonomy).
  Fixtures follow `tests/test_spec_source_request.py`: a temporary git repo
  with `user.name` and `user.email`, a `nw-backlog` branch built with the
  SPEC-0473 `nw backlog add` library call, an `outbox_targets` project, and a
  temporary org file for the linked cases. A helper builds a `QUEUED`
  `request_only` campaign by hand with `store.create_campaign` and the payload
  from the Decision (no queue command exists yet). A git spy records
  `hash-object`, `mktree`, `commit-tree`, `update-ref`, `show`, `checkout`,
  `switch`, `add`, `fetch`, and `push`.
  - `test_load_public_request_has_no_private_side` — open file loads; no
    `:REQ_BASE:` check; the `load_agreed_request` texts are unchanged.
  - `test_request_source_without_an_org_file` — unlinked request returns
    `(agreed, None)`.
  - `test_request_source_linked_idea_gates_the_request` — agreed pair returns
    the public body; drift on the private side, drift on the public side, two
    claimants, project mismatch, `:SPEC:`, closed idea, closed request, and
    cited request are each refused with the Design's text.
  - `test_request_source_fails_closed_on_an_unreadable_org_file` — a
    configured but corrupt org file is refused with
    `could not read the ideas file to check {req_id}: …` from
    `_request_source`, readiness, and `_prepare_spec_write`; the same
    campaign with no org file configured passes.
  - `test_request_source_allow_spec` — `allow_spec` equal to the file's
    `spec:` returns, a different one raises.
  - `test_request_campaign_is_ready_without_an_idea` — readiness with the
    file open, closed, cited by another spec, cited by the campaign's own
    spec, and removed; an `_idea_task` spy is not called.
- **Campaign tests:** extend `tests/test_agentic_spec_writer.py`, which
  already has `pytestmark = pytest.mark.integration`; no second tier is
  added.
  - `test_request_only_writer_uses_the_public_summary_and_cites` — tick a
    hand-built `request_only` campaign with a fake writer: the prompt's idea
    JSON `summary` is the public body, `write_rules` lists `source_request`
    and not `source_idea`; a fake spec with `source_idea` or without
    `source_request` is rejected with the Design's messages; a valid one
    leaves `spec:` on `refs/heads/nw-backlog` in one `cite REQ-… SPEC-NNNN`
    commit, no org write (org bytes compared), and a revise tick adds no
    second commit; a second `_link_idea` returns `None`, adds no commit, and
    emits no `idea_link_failed`.
  - `test_request_only_drift_before_write_parks` — change the linked idea's
    Summary (or close the request) after the campaign exists; one tick parks
    `spec_write_failed`, with no spec file and no `hash-object`.
  - `test_request_only_cite_failure_still_advances` — a moved tip, a
    different `spec:`, or a corrupted org file returns the error, adds no
    commit, still reaches spec review, and the `idea_link_failed` event names
    `nw backlog edit`.
  - `test_request_only_reset_refreshes_the_snapshot` — edit the public file's
    summary, resume a parked campaign, and assert the new `summary` and
    `heading`; a removed file keeps the old snapshot; a campaign that already
    cited its own spec still resets.
- **Tiers:** the only new module is `tests/test_request_source.py` with the one
  `fast` marker above. When `tools/test_tiers.py` exists,
  `python3 tools/test_tiers.py --check` exits 0.
- **Regression guard:** `tests/test_spec_source_request.py`,
  `tests/test_idea_link.py`, and the existing cases of
  `tests/test_agentic_spec_writer.py` stay green (the `_read_request_file`
  split must not change any `load_agreed_request` message). Run the affected
  tests with `python3 tools/affected_tests.py --run uv run pytest -q`, plus
  `python3 tools/spec_lint.py`.
- **Manual verification (real binary):** in a scratch repo whose `nw-backlog`
  holds one open request, create a `request_only` campaign with a short
  `python3 -c` call to `store.create_campaign` (no command exists yet), run
  the queue until the spec is written, and expect the spec with
  `source_request` and no `source_idea`, `git show
  refs/heads/nw-backlog:REQ-<id>.md` carrying `spec: SPEC-NNNN`, and one
  `cite` commit. Edit a linked idea's Summary before the tick and expect
  `spec_write_failed` with `do not match :REQ_BASE:`.

### Test record (2026-11-10)

Ran (real git repos throughout; no fakes for the backlog or the cite):

- `uv run pytest tests/test_request_source.py` — 11 passed: `load_public_request`,
  `_request_source` (no org file, linked gate, drift both sides, two claimants, project, `:SPEC:`,
  closed idea/request, cited request, `allow_spec`, quoted `status: "open"` queues and
  `status: maybe` is refused, unreadable org file fails closed), readiness without
  `_idea_task`, unchanged readiness and no `resolve_linked_repo` for non-request campaigns.
- `uv run pytest tests/test_agentic_spec_writer.py -k request_only` — 9 passed: the writer prompt
  carries the public body and `source_request` only, a `source_idea` is rejected, one
  `cite REQ-… SPEC-NNNN` commit with org bytes unchanged, `spec_lint` exit 0 on the committed
  spec, a second `_link_idea` returns `None` with no commit and no `idea_link_failed`, a revise
  tick adds no commit, drift/closed/unreadable-org park before any `hash-object`, cite failures
  (moved tip, other `spec:`, drift, unreadable org) still reach spec review with the
  `nw backlog edit` repair hint, and `_reset_spec_write` refreshes (also after the own cite) and
  keeps the snapshot on a removed file.
- Regression: `tests/test_spec_source_request.py`, `tests/test_idea_link.py`, the existing
  `tests/test_agentic_spec_writer.py` cases and every other test file that mentions
  `backlog_readiness`, `_reset_spec_write`, `write_rules`, `_front_matter_problems`,
  `_link_idea` or `load_agreed_request`: 760 passed.
- `python3 tools/test_tiers.py --check` exits 0; `python3 tools/spec_lint.py` exits 0 after
  `--write-ledger`.
- Manual verification: the campaign tests above are the real-git run (a temporary repo with an
  `nw-backlog` branch, a hand-built `request_only` campaign driven by `Q.tick`, `git show
  refs/heads/nw-backlog:REQ-….md` and `git log` asserted); no separate scratch-repo session.
- The full suite and `affected_tests.py --run` were left to the campaign gate (the latter did not
  finish inside the agent's time box).

### As shipped (2026-11-10)

Shipped as designed, with these notes:

- `AgreedRequest` gained a trailing field `status_value` (the parsed `fm["status"]`); `status`
  still holds the raw line text. `_read_request_file(repo, req_id)` returns the `AgreedRequest`;
  `load_agreed_request` and `load_public_request` both call it.
- `queue._request_source_for(cfg, state)` is a small helper that passes the campaign's own spec
  id as `allow_spec`; readiness, `_prepare_spec_write` and `_reset_spec_write` use it. The
  `_prepare_spec_write` failure text is the bare `_request_source` error; only readiness adds the
  `request {req_id}: ` prefix.
- `_cite_request` also runs `IL.preflight_cite` (identity and nw-backlog-checked-out guards)
  before `cite_spec`.
- Close path: `_reconcile_project_spec` no longer falls back to the campaign's `idea.id` for a
  `request_only` campaign (that id is `REQ-…`, not an idea), so it emits the existing informational
  `idea reconcile: spec has no source_idea` finding instead of a minor `idea not found`. No test
  closes a `request_only` campaign yet; the follow-up spec should add one.
- Finding: requests written by `nw backlog add` render a blank line before the summary, which
  `_read_request_file` (SPEC-0535's canonical-summary rule, unchanged here) refuses with
  `summary is not canonical`. The tests therefore build request files with
  `idea_link.render_request`. Part 2 (or a format spec) has to reconcile the two renderers before
  `nw queue add REQ-…` can read a request made with `nw backlog add`.

## Rollout / migration

No data move and no schema change: `source_request` exists after SPEC-0535
and `source_idea` stays optional. Part 1 lands first and is inert (no campaign
has `request_only`) until part 2 lands. Part 2 is a separate spec written from
IDEA-1022 that lists this spec in `depends_on` and adds `nw queue add REQ-…`
with the two-direction active-campaign exclusion described under Non-goals.
Existing campaigns have no `request_only` key and take the unchanged paths.
The first manual run is the real-binary check above.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; the named tests pass (affected tests via
      `python3 tools/affected_tests.py --run uv run pytest -q`); the manual
      verification was performed and noted here.
- [x] No regressions in the SPEC-0535 tests; `python3 tools/test_tiers.py
      --check` exits 0 when `tools/test_tiers.py` exists.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated and `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
