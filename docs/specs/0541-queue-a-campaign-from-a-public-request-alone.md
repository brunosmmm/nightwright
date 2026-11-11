---
id: SPEC-0541
title: Queue a campaign from a public request alone
status: proposed
owner: user
created: 2026-11-10
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

All of that starts from a private idea. `nw queue add` takes `SPEC-NNNN` or
`IDEA-NNN` (`_queue_add_one` in `src/nightwright/agentic/cli_queue.py`,
`plan_add_idea` and `apply_add_idea` in `src/nightwright/agentic/queue.py`).
`plan_add_idea` reads the org idea; a clone that has the repo and the
`nw-backlog` branch but no org file cannot start the loop from a public
request. `nw queue add REQ-…` today falls through to the spec path and answers
that the spec is unknown.

Much of the idea-campaign code already tolerates a missing org task
(`_link_idea`, `_prepare_spec_write`, `_unlink_idea` skip when `_idea_task`
returns `None`). Three places do not: `plan_add_idea` and `apply_add_idea`
refuse an unknown idea, the readiness check for a spec-less campaign
(`has_no_spec` branch near `queue.py:1191`) answers `idea {iid} not found`, and
`_front_matter_problems` requires the spec's `source_idea` to equal the
campaign's `idea.id`.

## Goals / Non-goals

**Goals**

- `nw queue add REQ-XXXXXXXX` (dry-run, `--yes`, `--json`) queues one campaign
  whose input is the public file on `refs/heads/nw-backlog`. The campaign's
  spec writer writes the spec from that title and summary, the way
  `nw queue add IDEA-NNN` writes it from the org idea.
- The private org file is not required and is not written. A machine with no
  org ideas file queues, writes, and cites a request.
- The spec records `source_request: REQ-…` and has no `source_idea`. After the
  first write, the public file gains `spec: SPEC-NNNN` through SPEC-0535's
  `cite_spec`.
- A machine that also holds an idea with `:REQ:` set to that request runs
  SPEC-0535's drift gate (`load_agreed_request`) before the public summary
  becomes the spec's input, at queue time and again at write time. A drifted
  pair is refused.
- Everything refused is refused before a campaign, a spec number, or a backlog
  commit exists, with one error naming the cause.

**Non-goals**

- Writing `:CAMPAIGN:`, `:SPEC:`, or `SPECCED` on a linked idea that happens to
  be on the machine. The request campaign never writes the org file. The
  public file's `spec:` is the record. A later spec (reconcile or pull) may
  advance the private side.
- Fetching or pushing `refs/heads/nw-backlog`. The branch is read and, for the
  cite, written locally; a clone that lacks the branch gets
  `{req_id} is not on refs/heads/nw-backlog`.
- A batch of requests. `nw queue add REQ-A REQ-B`, `--from-file`, and
  `--epic` do not accept a request id. `nw queue add IDEA-NNN` and
  `nw queue add SPEC-NNNN` keep today's behavior, including the SPEC-0535
  gate on a linked idea.
- Creating, editing, closing, or reopening requests (`nw backlog …`), and
  moving title or summary between sides (`nw idea push`, `pull`, `adopt`).
- Outbound specs, split parts, and any request whose `status` is not `open`.
- Changing the public file's format or the SPEC-0535 error texts.

## Decision

`nw queue add REQ-XXXXXXXX` is a third input kind beside specs and ideas. The
plan and the apply read the request file from the repo `resolve_linked_repo`
returns for the campaign's project. The campaign is a `kind: idea` campaign
(the writer, review, close, and merge flow stay as they are) whose `idea`
payload is built from the public file instead of the org task, and is marked
`request_only: true`. Its `idea.id` is the request id, so every existing
lookup keyed on the campaign's idea id finds no org task and leaves the org
file alone.

The request file is read through the same validator SPEC-0535 uses, so a file
that cannot be cited is refused at queue time. When an org idea on the machine
has `:REQ:` equal to the request id, that idea goes through
`load_agreed_request` first; its drift error is the only refusal text for a
drifted pair. The linked idea is a gate, not a participant: it supplies no
text and receives no write.

## Design

### Command

`Q._REQ_ID_RE = ^REQ-[0-9A-HJKMNP-TV-Z]{8}$`. In `_queue_add_one`, an id that
matches runs `Q.plan_add_request` and, with `--yes`, `Q.apply_add_request`;
`ValueError` becomes `click.ClickException` as for ideas. An id that starts
with `REQ-` but does not match raises `invalid request id {id}`. In the batch
path (`len(ids) != 1`, `--from-file`, `--epic`) a request id in the list is
refused before any plan with
`a request id is queued on its own: nw queue add REQ-…`;
`plan_add_batch` classifies by the same pattern so a programmatic caller gets
the same error. The `queue add` help text gains
`nw queue add REQ-0ABC1234 --yes   queue a public request (no private idea needed)`.
`--force` has no effect on a request. `--project`, `--profile`, `--base`,
`--priority`, `--after`, and `--runner` behave as for an idea. The JSON is
`nw.queue.add.v1` with the plan keys `plan_add_idea` returns plus
`request_id` (the id) and `linked_idea` (the gate idea's id, or `null`).

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

`queue._request_source(cfg, req_id, project)` is the one function every
request-campaign step calls. It returns `(agreed, linked_task_or_None)`.

1. Scan the org ideas with `_ideas_or_empty(cfg)` for tasks whose `:REQ:`
   equals `req_id`. An unreadable or missing org file is no ideas, not an
   error. Two or more matches raise
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
   (`{req_id} is {status}; only open requests are queued`); and `agreed.spec`
   must be `None` (`{req_id} already cites {spec}`).

The first step reads the org file only to find the gate idea. It does not
read the idea's Summary unless it found one, and it never opens the org file
for write.

### Plan and apply

`plan_add_request(cfg, req_id, *, project, profile, base, priority, after,
runner)`, no writes. Refusals, first match wins: invalid id; the project
(`--project`, else the linked idea's `:PROJECT:`, else `Meta-Tools`); an
active campaign for the request; `_request_source`'s errors; the project's
root (`P.project_config(...)["_root"]`) not being the main checkout or a
worktree of `agreed.repo`
(`{req_id} is on refs/heads/nw-backlog of {repo}, which is not a checkout of
project {project}`); then `IL.preflight_cite(agreed.repo)` (`user.name`,
`user.email`, `nw-backlog` not checked out, same errors as publish). An
active campaign is a non-terminal campaign, of any `kind: idea` input, whose
`idea.source_request` equals `req_id`; the error is
`{req_id} already has an active campaign {id} ({state}); abandon it first`.
The plan then follows `plan_add_idea`: campaign id from
`store.new_campaign_id(cfg, req_id)`, worktree, branch `nw/{cid}`, profile,
cap, `after`, `runner`. A campaign id is `REQ-XXXXXXXX-YYYYMMDD-hhhh`.

`apply_add_request(cfg, plan, *, by)` takes `store.add_lock(cfg)`, repeats
the active-campaign check and `_request_source`, and builds

```json
{"schema": "nw.idea.v1", "id": "REQ-…", "state": "", "heading": "<public title>",
 "tags": [], "kind": "idea", "summary": "<public body>", "questions": "",
 "question_items": [], "log": "", "project": "<project>",
 "source_request": "REQ-…", "request_only": true, "epic": null,
 "state_before": null}
```

then `store.create_campaign` in `QUEUED` exactly as `apply_add_idea` does. It
calls no `OW.set_property`, no `OW.set_state`, and does not take
`org_write_lock`. `request_only` is set whether or not a linked idea was
found.

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
snapshot from `_request_source` (`summary`, `heading`) and keeps the old
snapshot when that raises.

### Cite

`_link_idea` first checks `(state["idea"] or {}).get("request_only")`. For
`request_only` it runs `_cite_request(cfg, state, spec_id)` and nothing else
(no `_idea_task`, no `:SPEC:`, no `SPECCED`, no org lock). It does nothing
unless `spec_write.mode == "write"`. `_cite_request`:

1. `_request_source` again (drift, status, and a changed public file all
   return their error text, with no commit; the spec stays on the campaign
   branch).
2. `agreed.spec == spec_id`: return `None`, no commit.
   `agreed.spec` is another id: `{req_id} already cites {spec}`, no commit.
   (`_request_source` raises that case first; the check stays as the guard for
   a tip that moved between the two reads.)
3. Otherwise `IL.cite_spec(cfg, agreed, spec_id,
   expected_backlog_tip=agreed.backlog_tip)`, SPEC-0535's single
   compare-and-swap commit `cite {req_id} {spec_id}` in `agreed.repo`.

`_link_idea` is best-effort and returns the error text. `_apply_spec_write`
keeps its `idea_link_failed` event and still advances to spec review. The
event's text for a request ends with
`; repair: nw backlog edit {req_id} --spec {spec_id}`. Revise, amend, and split
do not cite. `plan_add_request` ran `preflight_cite`, so the usual cause of a
failed cite is a tip that moved or a drifted linked idea.

### Other campaign steps

Abandon (`_unlink_idea`) finds no org task for the request id, writes no org
file, and releases the campaign's spec-number reservations as today.
`_release_idea_link` clears `:CAMPAIGN:` only on ideas that name the campaign;
a request campaign set none. `nw queue list` shows the request id in
`idea_id`. The merge and close flow does not read the idea; the spec's
`source_idea` is absent, so `SP.source_idea_task` answers `no source_idea` and
the close path skips idea bookkeeping.

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
  idea it was published from; the gate costs one read and applies to both
  paths.
- **Accept several request ids per `queue add`.** Dependencies between
  requests are not defined by the public file; the batch ordering is
  `depends_on` based and does not apply.

## Acceptance criteria

- [ ] `nw queue add REQ-0ABC1234` on a machine with the repo, the
      `nw-backlog` branch and no org ideas file prints the plan and writes
      nothing. With `--yes` it creates one `QUEUED` campaign
      `REQ-0ABC1234-YYYYMMDD-hhhh` of `kind: idea` whose `idea` has
      `id`, `source_request` equal to the request id, `request_only: true`,
      `heading` equal to the public title, `summary` equal to the public
      body, empty `log` and `questions`, and `state_before: null`.
      `--json` emits `nw.queue.add.v1` with `request_id` and
      `linked_idea: null`.
- [ ] `apply_add_request` calls no `OW.set_property`, no `OW.set_state`, and
      not `org_write_lock`; the org bytes of a machine that holds a linked
      idea are identical before and after queue, write, and cite.
- [ ] Refused with exit 1, one error line, and no campaign, no spec number,
      no backlog commit: an id starting `REQ-` that does not match the
      pattern; a request id in a batch (`--from-file`, `--epic`, two ids);
      a request not on `refs/heads/nw-backlog`; a file that SPEC-0535's reader
      refuses (unexpected key, id mismatch, non-canonical summary or title);
      `status: closed`; `spec:` already set (`{req_id} already cites
      {spec}`); an active campaign for the request, including an idea
      campaign whose `idea.source_request` is the request id; a project whose
      root is not a checkout of `agreed.repo`; `user.name` or `user.email`
      unset, or `nw-backlog` checked out (publish's text).
- [ ] With a linked idea on the machine: a drifted pair (private side, public
      file, or both differ from `:REQ_BASE:`) is refused with SPEC-0535's
      drift text and writes nothing; an agreed pair queues, and the campaign
      `summary` is the public body and not the private Summary; two ideas
      with the same `:REQ:`, an idea of another project, an idea with
      `:SPEC:`, and a closed idea are each refused with the Design's text.
      An unreadable org file queues as if no idea were linked.
- [ ] `apply_add_request` repeats the checks under `store.add_lock`: a
      request that gained `spec:` or a drift after the plan is refused and no
      campaign is created.
- [ ] The readiness check of a `request_only` campaign does not call
      `_idea_task`; it is ready while `_request_source` succeeds and not
      ready with `request {req_id}: {error}` after the file is closed, cited,
      or removed.
- [ ] `_prepare_spec_write` of a `request_only` campaign re-reads the request
      before allocating: on drift, closed, cited, or missing it parks
      `spec_write_failed`, creates no spec file, and runs no `hash-object`.
      On success `idea.summary` is the public body. A campaign that is not
      `request_only` behaves as in SPEC-0535.
- [ ] `write_rules` for a `request_only` write lists `source_request: REQ-…`,
      does not list `source_idea`, and names the public summary as the only
      input. `_front_matter_problems` rejects a missing or different
      `source_request` and any present `source_idea` for `request_only`;
      a non-`request_only` idea keeps SPEC-0535's checks (`source_idea` equal
      to the idea id).
- [ ] After a valid first write, `_link_idea` commits `cite {req_id}
      {spec_id}` on `agreed.repo`'s `refs/heads/nw-backlog` (`spec:` added,
      every other byte of the file and every other path unchanged, expected
      value the previous tip) and writes no `:SPEC:`, no `SPECCED`, and no
      other org byte. A second `_link_idea` for the same spec id adds no
      commit. A revise tick adds no commit. A different `spec:` on the file,
      drift, or a moved tip returns the error text, adds no commit, and the
      campaign still advances to spec review with an `idea_link_failed` event
      that contains `nw backlog edit {req_id} --spec {spec_id}`.
- [ ] The spec the writer commits has `source_request` equal to the request
      id and no `source_idea`; `python3 tools/spec_lint.py` exits 0 on it.
- [ ] `_reset_spec_write` of a `request_only` campaign refreshes `summary` and
      `heading` from the file and keeps the old snapshot when the read
      raises.
- [ ] Abandoning a `request_only` campaign writes no org file, and releases
      its spec-number reservations. `nw queue list` renders the campaign and
      its `idea_id` is the request id.
- [ ] `nw queue add IDEA-NNN` and `nw queue add SPEC-NNNN` behave as before;
      an idea with no `:REQ:` still does not call `resolve_linked_repo`.
      `load_agreed_request` raises the same text for every case in
      SPEC-0535's tests.

## Test plan

- **Automated tests:** new `tests/test_queue_add_request.py` with exactly one
  module marker, `pytestmark = pytest.mark.fast` (the name matches no
  `tui`, `remote`, `perf`, or `integration` rule in the SPEC-0490 taxonomy).
  Fixtures follow `tests/test_spec_source_request.py`: a temporary git repo
  with `user.name` and `user.email`, a `nw-backlog` branch built with the
  SPEC-0473 `nw backlog add` library call, an `outbox_targets` project (and a
  Meta-Tools case with `nw_checkout`), and a temporary org file for the
  linked cases. A git spy records `hash-object`, `mktree`, `commit-tree`,
  `update-ref`, `show`, `checkout`, `switch`, `add`, `fetch`, and `push`.
  - `test_queue_add_request_without_an_org_file` — no org file configured;
    dry-run writes nothing; `--yes` creates the campaign with the state in
    the first criterion; `--json` keys; `OW.set_property` and `OW.set_state`
    are spies that must not be called.
  - `test_queue_add_request_refusals` — parametrized over each refusal in the
    third criterion, asserting the exact message, no campaign directory, no
    spec reservation, and no backlog commit (ref unchanged).
  - `test_queue_add_request_in_a_batch_is_refused` — `--from-file`, `--epic`
    and two ids; `plan_add_batch` with a request id.
  - `test_linked_idea_gates_the_request` — publish an idea; agreed pair
    queues with the public body as `summary` and org bytes unchanged; drift
    on the private side, drift on the public side, two claimants, a project
    mismatch, an idea with `:SPEC:`, and a closed idea are each refused.
    An unreadable org file queues as unlinked.
  - `test_apply_add_request_rechecks_under_the_lock` — mutate the file
    (cite it, drift it) between plan and apply.
  - `test_request_campaign_is_ready_without_an_idea` — readiness with the
    file open, closed, cited, and removed; `_idea_task` spy not called.
  - `test_abandon_request_campaign_touches_no_org_file` — abandon, assert org
    bytes and that reservations are released; `nw queue list --json` row
    `idea_id`.
  - `test_idea_and_spec_add_unchanged` — `plan_add_idea` of an unlinked idea
    does not call `resolve_linked_repo`; `plan_add` of a spec is unchanged.
- **Campaign tests:** extend `tests/test_agentic_spec_writer.py`, which
  already has `pytestmark = pytest.mark.integration`; no second tier is
  added.
  - `test_request_only_writer_uses_the_public_summary_and_cites` — queue a
    request, tick with a fake writer: the prompt's idea JSON `summary` is the
    public body, `write_rules` lists `source_request` and not `source_idea`;
    a fake spec with `source_idea` or without `source_request` is rejected
    with the Design's messages; a valid one leaves `spec:` on
    `refs/heads/nw-backlog` in one `cite REQ-… SPEC-NNNN` commit, no org
    write, and a revise tick adds no second commit.
  - `test_request_only_drift_before_write_parks` — change the linked idea's
    Summary (or close the request) after queue; one tick parks
    `spec_write_failed`, with no spec file and no `hash-object`.
  - `test_request_only_cite_failure_still_advances` — a moved tip or a
    different `spec:` returns the error, adds no commit, still reaches spec
    review, and the `idea_link_failed` event names `nw backlog edit`.
  - `test_request_only_reset_refreshes_the_snapshot` — edit the public file's
    summary, resume a parked campaign, and assert the new `summary`; a
    removed file keeps the old snapshot.
- **Regression guard:** `tests/test_spec_source_request.py`,
  `tests/test_idea_link.py`, and the existing cases of
  `tests/test_agentic_spec_writer.py` stay green (the `_read_request_file`
  split must not change any `load_agreed_request` message). Run the affected
  tests with `python3 tools/affected_tests.py --run uv run pytest -q`, plus
  `python3 tools/spec_lint.py` and `python3 tools/test_tiers.py --check`,
  which exits 0 (`tools/test_tiers.py` is in the tree).
- **Manual verification (real binary):** in a second clone of a repo whose
  `nw-backlog` holds one open request and which has no org file,
  `git fetch origin nw-backlog:nw-backlog`, then `nw queue add REQ-<id>` (the
  plan lists the campaign), `nw queue add REQ-<id> --yes`, and run the queue
  until the spec is written. Expect the spec with `source_request` and no
  `source_idea`, and `git show refs/heads/nw-backlog:REQ-<id>.md` carrying
  `spec: SPEC-NNNN`. On the first clone, which holds the linked idea, edit the
  idea's Summary and run `nw queue add REQ-<id>`: exit 1 with
  `do not match :REQ_BASE:`.

## Rollout / migration

No data move and no schema change: `source_request` exists after SPEC-0535
and `source_idea` stays optional. Land the command, the reader split, and the
writer and cite changes in one change so a `request_only` campaign is never
queued before its writer rules exist. Existing campaigns have no
`request_only` key and take the unchanged paths. The first manual run is the
real-binary check above.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; the named tests pass (affected tests via
      `python3 tools/affected_tests.py --run uv run pytest -q`); the manual
      verification was performed and noted here.
- [ ] No regressions in the SPEC-0535 tests and the existing queue-add
      tests; `python3 tools/test_tiers.py --check` exits 0.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated and `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
