---
id: SPEC-0535
title: Specs cite the public request they come from
status: done
owner: user
created: 2026-11-10
updated: 2026-11-10
kind: feature
tags: [public-backlog]
depends_on: [SPEC-0480]
source_idea: IDEA-1021
---

## Context

A private idea stays an `IDEA-NNN` in the local org file. Graduation is
unchanged: the idea becomes `SPECCED` when a spec is written, and the spec
still lives in `docs/specs/` and still goes through a pull request.
[SPEC-0480](./0480-link-a-private-idea-to-a-public-backlog-request.md) publishes
that idea to one `REQ-` file on `refs/heads/nw-backlog` and records `:REQ:` and
`:REQ_BASE:` on the idea. The public file has no idea id. `:REQ_BASE:` is
`req_base` of the canonical title and summary at publish.

`nw spec new --from-idea` (`scaffold_from_idea` in `src/nightwright/specs.py`)
prefills Context from the org subtree, sets `source_idea`, and writes `:SPEC:`
plus `SPECCED`. The campaign spec writer
(`src/nightwright/agentic/spec_write.py`, `src/nightwright/agentic/queue.py`)
does the same link after the writer commits the spec. Neither path reads the
public file. A spec written from a published idea therefore carries the private
wording, and the request file never learns the spec id.

The text other people have seen is the public file. A spec from a linked pair
has to wait until the private title and summary and that file still both equal
`:REQ_BASE:`. Push, pull, and adopt are later specs named in SPEC-0480. This
spec does not add those commands. It refuses while the pair has drifted, and
when the pair agrees it records the request on the spec and the spec on the
request.

## Goals / Non-goals

**Goals**

- An idea with no `:REQ:` keeps today's scaffold, today's campaign prompt, and
  today's `:SPEC:` write. That path does not resolve the linked repo and does
  not read `refs/heads/nw-backlog`.
- Writing a spec from an idea whose `:REQ:` is set waits until
  `req_base(task.heading, Summary)` and `req_base(file title, file summary)`
  both equal the stored `:REQ_BASE:`. A drifted pair produces no spec file, no
  `:SPEC:`, no `SPECCED` change, and no backlog commit.
- The spec's Context comes from the public summary. `source_idea` still names
  the private idea. The spec also records `source_request: REQ-…`. That key is
  the provenance that travels with the repo.
- When the new spec file is a `docs/specs/SPEC-NNNN` of a worktree of the repo
  `resolve_linked_repo` returned (the main checkout or a campaign worktree of
  that same repo), the public file gains `spec: SPEC-NNNN` in one
  compare-and-swap commit on that repo's `refs/heads/nw-backlog`. The idea
  gains `:SPEC:` and `SPECCED` as it does today. `:REQ:` and `:REQ_BASE:` stay.
- The campaign spec writer is held to the same pair, the same public summary,
  and the same `source_request` key. After a successful first write, the queue
  cites the spec on that repo's public file in the same commit shape only when
  the campaign worktree is a worktree of `resolve_linked_repo`. When a
  Meta-Tools idea's `outbox_targets` repo is a different checkout from
  `nw_checkout`, the spec still records `source_request` and the idea still
  gains `:SPEC:` and `SPECCED`, and that outbox repo's `nw-backlog` is
  unchanged.

**Non-goals**

- `nw idea push`, `nw idea pull`, and `nw idea adopt`. A drifted pair is
  refused with those words in the error. This spec does not move title,
  summary, or `:REQ_BASE:`.
- Drift display on `nw idea show` and `nw next`. SPEC-0480 left `link_status`
  and the `link` argument of `idea_show_payload` for a later spec. Callers keep
  today's one-argument `idea_show_payload`.
- `nw queue add REQ-…`. That is IDEA-1022.
- Outbound specs. `scaffold_outbound` and `promote_idea`'s outbound route do
  not read the request and do not write `source_request` or `spec:`.
- `nw idea spec-link`, reconcile's auto-link, split parts, and amend. They do
  not add or rewrite `spec:` on the request. An amend still may change only
  `updated`. A revise of a spec that already has `source_request` must keep
  that key; it does not commit a new cite.
- File `status` following the spec lifecycle. Cite copies the file's `status`.
- Storing the idea id in the public file.
- Backfilling specs and request files that already exist.

## Decision

A linked pair is agreed only when both sides still hash to the stored
`:REQ_BASE:`. `req_base` is SPEC-0480's digest of the canonical title and the
canonical summary. The private title is `task.heading`. The private summary is
`idea_summary_text(..., strict=True)`. The public title and summary are the
request file's parsed title and its body. Agreement is those two digests each
equal to the `:REQ_BASE:` hex on the idea. Matching each other while differing
from `:REQ_BASE:` is still drift.

The spec is still the decision to build. On an agreed pair, Context is the
public summary, because that is the text other people have seen. When the
private wording is the one the spec should use, it is pushed to the file first,
by a later command. Until then, `nw spec new --from-idea` and `nw queue add` of
that idea exit 1 with

`{idea_id} and {req_id} do not match :REQ_BASE:; push, pull, or choose one summary before writing a spec`

and write nothing.

`source_request` is an optional front-matter key, `REQ-` plus eight Crockford
characters. `source_idea` stays. The public file records `spec:` with the same
renderer as publish, on a new commit whose message is `cite REQ-<id> SPEC-NNNN`.
That commit runs only when `will_cite` is true: `docs/specs` of the main
checkout, or of a campaign worktree, of the repo `resolve_linked_repo`
returned. An `--internal` spec written into a different checkout records
`source_request` and does not commit `spec:`. A Meta-Tools `outbox_targets`
checkout that is not that repo does not receive the cite.

## Design

### Schema

`docs/specs/spec.schema.json` gains

```json
"source_request": {
  "type": "string",
  "pattern": "^REQ-[0-9A-HJKMNP-TV-Z]{8}$",
  "description": "The public request this spec was written from (SPEC-0535)."
}
```

`additionalProperties` stays false. `REQ-IIIIIIII` (an `I`), `REQ-123`, and a
bare `hello` fail validation. A missing key stays valid.

`docs/specs/SCHEMA.md`'s front-matter table gains a row: `source_request`,
optional, string, the `REQ-` id of the public file, set when the spec is
written from an idea that has `:REQ:` (SPEC-0535). The key is the provenance
that travels with the repo. `source_idea` still names the private idea on the
machine that holds it.

### Agreed request

`idea_link.load_agreed_request(cfg, task)` in `src/nightwright/idea_link.py`.

When `:REQ:` is missing or blank, return `None` and do not call
`resolve_linked_repo` or git. Otherwise require `:REQ:` to match
`^REQ-[0-9A-HJKMNP-TV-Z]{8}$`. A bad value raises `{idea_id} has invalid :REQ: {value}`.

`:REQ_BASE:` blank raises `{idea_id} has {req_id} but no :REQ_BASE:`.

Resolve the repo with `resolve_linked_repo`. Read
`git show refs/heads/nw-backlog:{req_id}.md`. A missing ref or path raises
`{req_id} is not on refs/heads/nw-backlog`. Bytes that are not UTF-8, or that
`split_frontmatter` does not parse to a dict, raise `{req_id} file is not a request`.

The file's `id` must equal `:REQ:`. Otherwise `{req_id} file id is {other}`.
The front matter may contain only `id`, `title`, `status`, `created`,
`updated`, `created_by`, `updated_by`, and `spec`. Any other key raises
`{req_id} file has an unexpected key {key}` (the first such key). `title`,
`status`, `created`, `created_by`, `updated`, and `updated_by` must be present.
`spec`, when present, must match `^SPEC-[0-9]{4}$` or the error is
`{req_id} file spec is {value}`.

`created` is the raw `created:` line value, the `YYYY-MM-DDTHH:MM:SSZ` text,
not the datetime `yaml.safe_load` returns. The same raw read supplies
`created_by` after YAML parsing so the renderer can quote it again.

The summary bytes are the `split_frontmatter` body. They must equal
`canonical_pair(title, body)[1]` encoded as UTF-8 with no added trailing
newline. Otherwise `{req_id} file summary is not canonical`. The parsed title
must equal `canonical_pair(title, "")[0]`. Otherwise
`{req_id} file title is not canonical`.

Private digest is `req_base(task.heading, idea_summary_text(cfg, task, strict=True))`.
A Summary read error raises `could not read Summary for {idea_id}: {err}`.
Public digest is `req_base(parsed title, body)`. Both must equal the stored
`:REQ_BASE:` hex. Otherwise the drift error above.

The return value carries `req_id`, `repo`, `title`, `summary` (the body text),
`status`, `created`, `created_by`, `updated_by`, `spec` (or `None`), and the
raw file bytes. A present `spec:` does not raise.

### Where a cite commit is allowed

`will_cite(cfg, task, specs_dir)` is true when `load_agreed_request` returns a
request and `Path(specs_dir).resolve()` equals
`(wt / "docs" / "specs").resolve()` for a worktree `wt` listed by
`git -C agreed.repo worktree list --porcelain`, including the main checkout.
`agreed.repo` is the path `resolve_linked_repo` returned. The main checkout
passes. A campaign worktree of that same repo passes. Any other specs
directory fails, including `--internal` into another checkout. `cite_spec`
runs `hash-object`, `mktree`, `commit-tree`, and `update-ref` in
`agreed.repo`. `_link_idea` calls `cite_spec` only when `will_cite` is true
for `Path(state["repo"]["worktree"]) / "docs" / "specs"`. When that check
fails, `_link_idea` still writes `:SPEC:` and `SPECCED` and leaves
`refs/heads/nw-backlog` unchanged. `plan_add_idea` stores `repo.root` from
`project_root`. For Meta-Tools, `project_root` follows `outbox_targets` when
that key is set, and `resolve_linked_repo` uses `nw_checkout`.

`promote_idea`'s outbound route never calls `load_agreed_request`.

### Scaffold

`scaffold_from_idea` calls `load_agreed_request` before `spec_numbers.allocate`.

- `None`: today's path. Context stays `_idea_context`. Front matter gains
  `source_idea` and no `source_request`. No backlog commit.
- Agreed, and `will_cite` is false (`--internal` into another checkout): still
  require agreement. Context and `source_request` are written as below. No
  identity check, no checkout check, no backlog commit. The request blob sha
  stays.
- Agreed, and `will_cite` is true: before allocate, require the linked repo's
  `user.name` and `user.email` (same errors as publish), require `nw-backlog`
  not checked out (`refusing to update nw-backlog while it is checked out`,
  and no `hash-object`), and if the file's `spec` is already set and `--force`
  is false, raise `{req_id} already cites {spec}` with no allocate and no
  commit.

The spec file, the cite commit, and `:SPEC:` / `SPECCED` run under
`org_write_lock`. Inside the lock the idea is resolved again and
`load_agreed_request` runs again. A drift on that second read releases the
number, does not write the spec file, and does not commit. If `will_cite` is
true and the file's `spec` is set to a value other than the id about to be
written and `--force` is false, release the number, write no spec file,
commit nothing, and raise `{req_id} already cites {spec}`.

Front matter, both `TEMPLATE.md` and `TEMPLATE-epic.md`, inserts
`source_request: {req_id}` immediately after `source_idea: {task.id}`. `parent`
stays after that when `:EPIC:` resolves. Context replaces the template
placeholder with

`Promoted from request `{req_id}` (`{public title}`).`

plus, when the public summary is non-empty, a blank line and that summary
body. No `from_org_body` conversion. The org log and open questions are not
copied. An empty public summary is the lead line only.

Then `:SPEC:` and `SPECCED`, same calls as today. `:REQ:` and `:REQ_BASE:` are
untouched. Headline, Summary, log, questions, tags, and clocks stay.

### Cite commit

`idea_link.cite_spec(cfg, agreed, spec_id)` reuses `_publish_commit`.

If `agreed.spec == spec_id`, return without a commit.

Otherwise render with `render_request`: same `id`, `title`, `status`,
`created` text, `created_by`, and summary body; `updated` is a new UTC
`YYYY-MM-DDTHH:MM:SSZ`; `updated_by` is the linked repo's `Name <email>`;
`spec` is `spec_id`. Key order is publish's seven keys, then `spec`, then the
closing fence. The summary bytes after that fence are unchanged. The `created`
line is byte-for-byte unchanged. A title `say "hi"` still round-trips through
`split_frontmatter`.

The commit message is `cite {req_id} {spec_id}`. `hash-object`, `mktree`,
`commit-tree`, and `update-ref` run in `agreed.repo`. Author and committer
are `agreed.repo`'s `user.name` and `user.email`. The tree replaces only
`{req_id}.md`. Every other path stays byte-for-byte. `update-ref`'s expected
value is the current tip. The command runs no `git checkout`, `git switch`,
`git add`, `git fetch`, or `git push`. Symbolic `HEAD`, the index, and the
worktree stay.

`--force` on `nw spec new` may replace a different well-formed `spec:` with
the new id. It is the only caller that invokes `cite_spec` with a different
`spec:`. Without `--force`, the existing `:SPEC:` guard still refuses a
second promote (`idea {id} already promoted to {existing}`) before cite.

A failed `update-ref` writes no `:SPEC:`. Scaffold deletes the spec file it
just wrote, releases the number, leaves the ref, and raises
`{req_id} was not cited; refs/heads/nw-backlog unchanged`.

### Org-write failure after cite

Same restore as publish, with these stderr lines:

- Tip restored (or `update-ref -d` when the old tip was absent, which cite
  does not hit because the file already existed): `{spec_id} was not cited on {req_id}; restored refs/heads/nw-backlog`.
  Scaffold then deletes the spec file and releases the number. Org bytes are
  unchanged.
- Ref no longer points at the new commit: `{spec_id} was not cited on {req_id}; refs/heads/nw-backlog no longer points at {new}; left unchanged`.
  The spec file stays. The number stays reserved.
- Restore command fails: `{spec_id} was not cited on {req_id}; refs/heads/nw-backlog restore failed; left unchanged`.
  The spec file stays.

### Campaign

`plan_add_idea` calls `load_agreed_request` on the live idea. Drift, a missing
file, a blank `:REQ_BASE:`, or a non-canonical summary raises
`load_agreed_request`'s error before `store.create_campaign` and before
`:CAMPAIGN:` is written. When that load returns a request whose `spec` is not
`None`, `plan_add_idea` raises `{req_id} already cites {spec}` and also stops
before `store.create_campaign`. An idea with no `:REQ:` does not call
`resolve_linked_repo`.

`apply_add_idea` calls `load_agreed_request` again before `create_campaign`.
On agreement it stores `idea["source_request"]` as the req id and replaces
`idea["summary"]` with the public body. Questions, log, and heading stay.
`idea_show_payload` is still called with one argument.

`_prepare_spec_write`, for `mode == "write"` only, resolves the live idea with
`_idea_task`. A missing task leaves the gate alone. A task with `:REQ:` runs
`load_agreed_request` before `spec_numbers.allocate`. Failure raises that
error; the tick parks `spec_write_failed`; no spec file is created; no backlog
object is written. On agreement, `state["idea"]["summary"]` becomes the public
body and `state["idea"]["source_request"]` becomes the req id before
`write_rules` and before the worker's `validate_spec_change(..., state.get("idea"))`.

`write_rules` for `write`, when `idea["source_request"]` is set, lists
`` `source_request: {req}` `` in the required front matter and adds this
sentence: the Context section is the public summary in the idea JSON `summary`
field for that request, the text other people have seen; the private log and
open questions stay available for the rest of the spec and are not a
replacement for that summary. When `source_request` is absent, the rules say
the front matter has no `source_request` key.

`_front_matter_problems`: if `idea["source_request"]` is set,
`fm["source_request"]` must equal it
(`front matter: source_request must be {req!r}, got {got!r}`). If it is absent
and the key is present,
`front matter: source_request must be absent (the idea has no :REQ:), got {got!r}`.
Revise uses the same check. `write_rules` for revise says to keep
`source_request` when the idea has one. Split rules are unchanged.

`_link_idea`, when the live idea has `:REQ:` and this write's mode is `write`,
under `org_write_lock` re-reads the request. Drift returns the drift error and
sets neither `:SPEC:` nor a cite commit. The campaign branch already holds the
spec; that commit stays. If `agreed.spec` is not `None` and is not this spec
id, return `{req_id} already cites {spec}`, do not call `cite_spec`, and do
not write `:SPEC:`. If `agreed.spec` equals this spec id, skip the commit and
only finish `:SPEC:` and `SPECCED`. Otherwise call `cite_spec` only when
`will_cite` is true for `Path(state["repo"]["worktree"]) / "docs" / "specs"`,
then today's `:SPEC:` and `SPECCED` writes. When that check fails, still write
`:SPEC:` and `SPECCED` and leave `refs/heads/nw-backlog` unchanged.
`cite_spec` still replaces a different `spec:` when a caller invokes it;
`nw spec new --force` is the only caller that does. A cite or org failure
returns the error text; `_apply_spec_write` keeps today's `idea_link_failed`
note and still advances to spec review.

Revise and amend do not call `cite_spec`.

## Alternatives considered

- **Cite only in Context prose.** A clone could not find the request without
  parsing sentences, and the schema could not reject a bad id.
- **Prefill Context from the org subtree when the digests match.** The words
  can match while the subtree still carries the log and the questions. The
  public body is the text other people have seen.
- **Wait to build this until push and pull exist.** The gate only needs an
  already agreed `:REQ_BASE:`. Push and pull stay their own specs.
- **Write `IDEA-NNN` into the public file next to `spec:`.** SPEC-0480's file
  has no idea id. `source_request` on the spec is the direction that travels
  with the repo.
- **Block outbound promotion of a linked idea.** Outbound specs are not
  `docs/specs/` in the request's repo. They keep today's route.

## Acceptance criteria

- [x] `docs/specs/spec.schema.json` accepts `source_request: REQ-01234567` and
      rejects `REQ-IIIIIIII`, `REQ-123`, and `hello`. A spec with no
      `source_request` still validates. `docs/specs/SCHEMA.md` documents the
      key. `python3 tools/spec_lint.py` exits 0.
- [x] `load_agreed_request` on an idea with no `:REQ:` returns `None` and does
      not call `resolve_linked_repo`. `scaffold_from_idea` of that idea writes
      `source_idea`, writes no `source_request`, prefills Context with
      `Promoted from idea`, and runs no `git show` of `refs/heads/nw-backlog`.
      `plan_add_idea` of that idea also does not call `resolve_linked_repo`.
- [x] On an agreed linked idea, `nw spec new --from-idea` writes
      `source_request` equal to `:REQ:` on the line after `source_idea`, for
      both `TEMPLATE.md` and `TEMPLATE-epic.md`. Context is
      `Promoted from request `{req_id}` (`{title}`).` and the public body. A
      private log line that is not in the public body does not appear in the
      spec. The idea becomes `SPECCED` with `:SPEC:` set to that id. `:REQ:`
      and `:REQ_BASE:` are unchanged. Summary, log, questions, tags, and the
      headline stay.
- [x] That command's cite commit is `cite {req_id} {spec_id}` via
      `hash-object`, `mktree`, `commit-tree`, and `update-ref` in
      `agreed.repo`, whose expected value is the previous tip. The file gains
      `spec: {spec_id}` after `updated_by`. `created` is byte-for-byte
      unchanged. The summary bytes after the closing fence are unchanged.
      `status` is unchanged. The blob gains no idea id. Every other path's
      blob sha is unchanged. Author and committer are `agreed.repo`'s
      identity. Symbolic `HEAD` is unchanged. The command runs no
      `git checkout`, `git switch`, `git add`, `git fetch`, or `git push`.
      A second `cite_spec` of the same spec id adds no commit.
      `load_agreed_request` still returns a request whose `spec` is set; a
      present `spec:` does not raise from that function.
- [x] Drift (private digest differs, public digest differs, or either differs
      from `:REQ_BASE:`), a missing `:REQ_BASE:`, a missing file, a
      non-canonical summary (`Hello\n`), an unexpected front-matter key, a
      file id mismatch, and `{req_id} already cites {spec}` each exit 1 with
      the Design's stderr, add no commit, write no spec file, and leave the
      org bytes unchanged. Unset `user.name` or `user.email`, and `nw-backlog`
      checked out, refuse before `hash-object` with publish's stderr, only
      when `will_cite` is true.
- [x] `--internal` into another checkout, whose specs directory is not
      `docs/specs` of a worktree listed for `agreed.repo`, still requires
      agreement, writes `source_request` and the public Context, and leaves
      the request blob sha unchanged. Outbound `promote_idea` of a linked
      idea does not call `load_agreed_request`.
- [x] `plan_add_idea` raises `{req_id} already cites {spec}` when
      `load_agreed_request` returns a request whose `spec` is not `None`, and
      `create_campaign` is not called. `_link_idea` raises
      `{req_id} already cites {spec}` when `agreed.spec` is a different id,
      does not call `cite_spec`, and does not write `:SPEC:`. When
      `agreed.spec` equals this spec id, `_link_idea` skips the commit and
      only finishes `:SPEC:` and `SPECCED`. `nw spec new --force` is the only
      path that replaces `spec:`. Without `--force`, a different `spec:` seen
      inside `org_write_lock` releases the number, writes no spec file, and
      commits nothing.
- [x] If the org write fails after the cite commit, the org bytes are
      unchanged and the Design's restore stderr is raised. A successful
      restore deletes the new spec file and releases the number. A lost
      compare-and-swap leaves the spec file and the ref.
- [x] `plan_add_idea` and `apply_add_idea` raise the drift error and do not
      create a campaign or set `:CAMPAIGN:`. On agreement, `apply_add_idea`
      sets `idea["source_request"]` and sets `idea["summary"]` to the public
      body. `_prepare_spec_write` in `write` mode re-checks before allocate
      and parks `spec_write_failed` on drift with no spec file and no
      `hash-object`. `write_rules` names `source_request` and the public
      summary sentence. `_front_matter_problems` rejects a missing or different
      `source_request` when the idea has one, and rejects a present key when
      the idea has none. After a valid first write, when `will_cite` is true,
      `_link_idea` commits `spec:` on `agreed.repo`'s `refs/heads/nw-backlog`
      and sets `:SPEC:` and `SPECCED`. When `will_cite` is false, `_link_idea`
      still sets `:SPEC:` and `SPECCED` and leaves `refs/heads/nw-backlog`
      unchanged. Revise does not commit another cite.
- [x] When the campaign worktree is a worktree of `resolve_linked_repo`, the
      cite commit is on that repo's `refs/heads/nw-backlog`. When a Meta-Tools
      idea's `outbox_targets` repo is a different checkout from `nw_checkout`,
      the spec records `source_request`, the idea gains `:SPEC:` and
      `SPECCED`, and that outbox repo's `nw-backlog` is unchanged.
- [x] `nw ideas` and `nw hub` still build rows with `idea_row`. The TUI list
      still uses `_row_for`. `load_detail` and the callers in
      `agentic/queue.py` and `agentic/spec_review.py` still call
      `idea_show_payload` with one argument.

## Test plan

- **Automated tests:** new `tests/test_spec_source_request.py` with exactly one
  module marker, `pytestmark = pytest.mark.fast`. Fixture shape matches
  `tests/test_idea_link.py`: a temporary git repo with `user.name` and
  `user.email`, `outbox_targets`, and a temporary org file. Meta-Tools cases
  set `nw_checkout` to a repo that contains `src/nightwright`,
  `pyproject.toml`, and `docs/specs/TEMPLATE.md` (and `TEMPLATE-epic.md`), and
  set `specs_dir` to that repo's `docs/specs`. Request bytes are read with
  `git show refs/heads/nw-backlog:<path>`. A git spy records `hash-object`,
  `mktree`, `commit-tree`, `update-ref`, `show`, and any `checkout`, `switch`,
  `add`, `fetch`, or `push`.
  - `test_schema_accepts_source_request` — the four schema cases and the
    SCHEMA.md row. `python3 tools/spec_lint.py` exits 0 on a fixture spec that
    carries a valid `source_request`.
  - `test_unlinked_idea_does_not_read_the_backlog` — `load_agreed_request`
    returns `None`; scaffold Context starts with `Promoted from idea`; no
    `source_request`; the spy sees no `resolve_linked_repo` and no
    `nw-backlog` read. `plan_add_idea` of an unlinked idea likewise does not
    call `resolve_linked_repo`.
  - `test_scaffold_cites_the_public_summary` — agreed publish, then
    `nw spec new --from-idea` and `scaffold_from_idea(..., epic=True)`.
    Context, front matter, `SPECCED`, unchanged `:REQ_BASE:`, log line absent
    from the spec, cite commit message, key order, unchanged `created` line,
    unchanged summary bytes, unchanged sibling blob, `HEAD` untouched. The
    cite's `hash-object`, `mktree`, `commit-tree`, and `update-ref` run in
    `agreed.repo`. A second `cite_spec` of the same spec id adds no commit.
    `load_agreed_request` returns the request when `spec:` is set and does
    not raise.
  - `test_scaffold_refuses_drift_and_bad_files` — each refusal in the drift
    acceptance criterion, including `Hello\n`, an unexpected key, file id
    mismatch, `already cites`, unset identity, and checked-out `nw-backlog`.
    Org bytes and the ref stay. A different `spec:` seen on the second load
    inside `org_write_lock`, with `--force` false, releases the number and
    writes no spec file.
  - `test_force_replaces_an_existing_spec_cite` — `nw spec new --from-idea
    --force` is the only caller that replaces a different `spec:`. The new
    file's `spec:` is the new id. A call that is not `--force` raises
    `{req_id} already cites {spec}` and leaves the request blob unchanged.
  - `test_internal_into_another_checkout_records_source_request_only` —
    `--internal` writes `source_request` and does not change the foreign
    request blob. Outbound `promote_idea` does not call
    `load_agreed_request` (spy).
  - `test_cite_org_write_failure_restores_the_tip` — restored case deletes
    the spec file and releases the number; lost compare-and-swap leaves the
    file and the ref.
  - `test_write_rules_and_front_matter_require_source_request` — calls
    `write_rules` and `_front_matter_problems` directly. Present, missing, and
    foreign `source_request`, plus the absent-key error when the idea has no
    request.
- **Campaign tests:** extend `tests/test_agentic_spec_writer.py`. That module
  already has `pytestmark = pytest.mark.integration`; this adds no second
  tier.
  - `test_linked_idea_writer_uses_the_public_summary_and_cites` — publish the
    harness idea, queue it, and assert the writer prompt's idea JSON
    `summary` is the public body, `source_request` is the req id, and
    `write_rules` contains the public-summary sentence. A fake writer that
    omits `source_request` is rejected with `source_request must be`. A fake
    writer that includes it leaves `spec:` on `agreed.repo`'s
    `refs/heads/nw-backlog` (the campaign worktree is a worktree of
    `resolve_linked_repo`), `:SPEC:` and `SPECCED` on the idea, and a revise
    tick adds no second cite commit. `hash-object`, `mktree`, `commit-tree`,
    and `update-ref` run in `agreed.repo`.
  - `test_drift_before_write_parks_without_a_cite` — after queue, change the
    private Summary so the digest moves, tick once, and assert
    `spec_write_failed`, no spec file, and no `hash-object` on `nw-backlog`.
  - `test_queue_add_refuses_a_drifted_pair` — `plan_add_idea` raises the drift
    error and `apply_add_idea` is not reached; no `:CAMPAIGN:`.
  - `test_queue_add_refuses_when_the_request_already_cites` — `plan_add_idea`
    raises `{req_id} already cites {spec}` and `create_campaign` is not
    called.
  - `test_link_idea_refuses_a_different_cite` — when `agreed.spec` is a
    different id, `_link_idea` returns `{req_id} already cites {spec}`, does
    not call `cite_spec`, and does not write `:SPEC:`. When `agreed.spec`
    equals this spec id, it skips the commit and only finishes `:SPEC:` and
    `SPECCED`.
  - `test_campaign_cite_follows_resolve_linked_repo` — a Meta-Tools idea whose
    `outbox_targets` repo is a different checkout from `nw_checkout`.
    `plan_add_idea` stores `repo.root` from that outbox checkout.
    `resolve_linked_repo` is `nw_checkout`. The campaign specs directory is
    not a worktree of `nw_checkout`, so `will_cite` is false. The written
    spec records `source_request`. The idea gains `:SPEC:` and `SPECCED`.
    That outbox repo's `refs/heads/nw-backlog` is unchanged, and
    `nw_checkout`'s `refs/heads/nw-backlog` gains no cite commit.
- **Regression guard:** `tests/test_idea_link.py` and
  `tests/test_idea_stamps.py::test_stamp_creates_missing_drawer_without_corrupting_sections`
  stay green. `tests/test_agentic_spec_writer.py` existing cases stay green,
  including a write that has no `source_request` on an idea with no `:REQ:`.
  `test_ideas_and_hub_keep_idea_row` in `tests/test_idea_link.py` still sees
  no `nw-backlog` read from `nw ideas --json`.
- **Manual verification:** publish an idea, then `nw spec new --from-idea
  IDEA-NNN`. The spec's Context contains the public summary and
  `source_request` matches `:REQ:`. `git show
  refs/heads/nw-backlog:REQ-<id>.md` contains `spec: SPEC-NNNN`. Edit the
  Summary so it no longer matches and run `nw spec new --from-idea` again:
  exit 1, stderr contains `do not match :REQ_BASE:`, and the ref is unchanged.
- **Tier check:** `python3 tools/test_tiers.py --check` exits 0.
  `tools/test_tiers.py` is in the tree.

## Clock Log

- 2026-11-10: SPEC-0535 implementation (IDEA-1021 campaign loop 0).
- 2026-11-10: Review fixes — resume overlay, cite rollback, `_raw_fm_line` (loop 1).

## Rollout / migration

No backfill. Specs already in `docs/specs/` stay valid without
`source_request`. Request files already on `nw-backlog` gain `spec:` only when
a new spec is written from that agreed pair. An idea that is already `SPECCED`
is left alone until someone runs `nw spec new --from-idea --force`, which
replaces `spec:` with the new id. Deploy the schema change in the same change
as the writer, so a campaign that emits `source_request` is not rejected as an
unknown key.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; the tests named above pass. The full suite stays on
      the queue's close gate.
- [x] No regressions in `tests/test_idea_link.py`, `tests/test_idea_stamps.py`,
      and `tests/test_agentic_spec_writer.py`.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated and `python3 tools/spec_lint.py` exits 0.

### Test record (2026-11-10)

- `python3 tools/test_tiers.py --check` — pass
- `uv run pytest tests/test_spec_source_request.py -q` — 8 passed
- `uv run pytest tests/test_idea_link.py tests/test_idea_stamps.py::test_stamp_creates_missing_drawer_without_corrupting_sections -q` — pass
- `uv run pytest tests/test_agentic_spec_writer.py -q -k "linked_idea or drift_before or queue_add_refuses or link_idea_refuses or campaign_cite or link_idea_returns"` — 7 passed
- `uv run pytest tests/test_promote.py tests/test_routing.py tests/test_spec_numbers.py -q` — 49 passed
- `python3 tools/spec_lint.py --write-ledger` — pass
- Full suite left to campaign gating.

### Test record (2026-11-10 loop 1)

- `python3 tools/test_tiers.py --check` — pass
- `uv run pytest tests/test_spec_source_request.py -q` — 8 passed
- `uv run pytest tests/test_agentic_spec_writer.py -q -k "linked_idea_resume or linked_idea_writer"` — 2 passed
- `python3 tools/spec_lint.py --write-ledger` — pass
- Full suite left to campaign gating.

### Test record (2026-11-10 loop 2)

- `python3 tools/test_tiers.py --check` — pass
- `uv run pytest tests/test_spec_source_request.py -q` — 9 passed
- `uv run pytest tests/test_agentic_spec_writer.py -q -k "linked_idea or drift_before or queue_add_refuses or link_idea_refuses or campaign_cite"` — 7 passed
- `uv run pytest tests/test_idea_link.py -q -k publish` — 29 passed
- `python3 tools/spec_lint.py --write-ledger` — pass
- Full suite left to campaign gating.

## Open questions

None.
