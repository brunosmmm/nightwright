---
id: SPEC-0480
title: Publish a backlog request and record the link
status: done
owner: user
created: 2026-11-08
updated: 2026-11-09
kind: feature
tags: [public-backlog]
source_idea: IDEA-1020
---

## Context

A private idea is an `IDEA-NNN` headline in the local org file. It keeps its TODO
state, a Summary, open questions, a log, and clock entries. Graduation is
unchanged: the idea becomes `SPECCED` when a spec is written. The org file is
not copied between machines.

IDEA-1019 is the public record: an orphan branch `refs/heads/nw-backlog` in the
project's git repo, one `REQ-` file per item at the branch root. The file has
no idea id. IDEA-1020 is the link from one local idea to one of those files.
This spec is the publish slice of that link: create the file, record `:REQ:`
and `:REQ_BASE:` on the idea, and keep a heading lookup that has no level from
dropping Summary when the properties drawer is created. Adopt, push, pull, and
drift display are later specs, listed under Non-goals.

A heading lookup with no level must keep the headline at `task.line`. The
summary reader that did not keep it emptied Summary after a stamp created the
properties drawer, and
`tests/test_idea_stamps.py::test_stamp_creates_missing_drawer_without_corrupting_sections`
failed. `_is_same_headline` today returns on `:ID:` before it looks at level,
so a task with no level still takes the drawer path.

## Goals / Non-goals

**Goals**

- `canonical_pair` and `req_base` define the title and summary that count as
  the same text. `:REQ_BASE:` stores that digest. The private title that is
  hashed is `task.heading`.
- `nw idea publish` creates one public file from that title and the Summary
  and sets `:REQ:` and `:REQ_BASE:` in the same org write. The idea stays
  `IDEA-NNN` and keeps its TODO state.
- The request file is UTF-8, with a double-quoted title, a newline-terminated
  closing fence, an empty body of zero bytes, and `created` written as
  `YYYY-MM-DDTHH:MM:SSZ` text.
- `resolve_linked_repo` finds the project's repo. Meta-Tools uses
  `nw_checkout`. Every other project uses `project_root`. A blank project
  exits 1 with `project is required` and does not call `project_root`.
- When `task.level` is absent or None, `_is_same_headline` runs before the
  `:ID:` short-circuit and matches only `task.line` with `task.heading` in
  `m.group(3)`. The stamp test stays green.

**Non-goals**

- **Adopt, push, and pull the title and summary.** A later spec, depending on
  this one. `nw idea adopt`, `nw idea push`, and `nw idea pull` are not
  commands here. That spec refuses adopt and pull before any write when
  `_RE_HEADING_TAGS` matches the canonical file title. Stderr is
  `title must not end in a tag cookie`. The org line is unchanged and the
  branch tip is unchanged. A file title `Ship:docs:` exits 1 with that stderr.
  Push and pull exit 1 and write nothing when `:REQ:` is unset or the file is
  missing, and they do not mint a replacement file. `_pull_rewrite` sets
  `tag_part` to `rest[tm.end(1):]` when `_RE_HEADING_TAGS` matches and to empty
  otherwise, so `* INCUBATE  Link :session-triage:` pulled to `Widgets` is
  `* INCUBATE  Widgets :session-triage:`. Push copies every other front-matter
  key and writes `created` back with the renderer from this spec. File
  `status` and dropping the idea do not cross.
- **Report drift only from show and next.** A later spec, depending on this
  one. `link_status` is not called here. That spec adds `link` to
  `idea_show_payload`: `req` and `req_link` appear only when `link=True`, the
  default is `link=False`, and only `nw idea show` and `nw next` pass true.
  `nw ideas` and `nw hub` keep building rows with `idea_row`, which calls
  `next_step_for_idea`, and the branch stays unread. The TUI list keeps
  `_row_for`, which does not call `idea_show_payload` or `next_step_for_idea`.
  The TUI detail keeps `load_detail`, which calls `idea_show_payload` with
  `link` left false and `next_step_for_idea` with the branch unread. The
  callers in `agentic/queue.py` and `agentic/spec_review.py` also leave the
  branch unread. Show and `nw next` write nothing and create no ref. This
  spec leaves those callers on today's one-argument `idea_show_payload`.
- Fetch, fast-forward push, same-file merge, and force-push policy for
  `nw-backlog`. Those belong to IDEA-1019. No command in this spec runs
  `git checkout`, `git switch`, `git add`, `git fetch`, or `git push`.
- Writing `source_request` on a spec, or writing `spec:` because a spec was
  created. That is IDEA-1021.
- `nw queue add REQ-…`. That is IDEA-1022.
- Syncing the org file between machines. The link exists only on the machine
  that holds the idea.
- Publishing ideas while loading the org file, from `nw hub`, or from the TUI.
  An existing idea stays unpublished until `nw idea publish`.
- Storing the idea id in the public file.

## Decision

Publishing is a link, not a graduation. The idea keeps its TODO state.

`:REQ:` is `REQ-` plus eight Crockford characters
(`0123456789ABCDEFGHJKMNPQRSTVWXYZ`). `:REQ_BASE:` is the SHA-256 hex digest of
the canonical title and summary at publish. The private title that is hashed
is `task.heading` (the heading text, with the TODO keyword and the tag cookie
already removed). The private summary is the Summary body. There is no markup
conversion and no internal-whitespace collapse.

`resolve_linked_repo` exits 1 with `project is required` on a blank project
and does not call `project_root`. Meta-Tools uses `nw_checkout` and ignores
`outbox_targets` and the current directory. Every other project uses
`project_root`. No command takes `--repo`. An idea with no `:PROJECT:` cannot
be published, because the branch lives in that project's repo.

The publish commit is one compare-and-swap of `refs/heads/nw-backlog`. The
expected value is the all-zero object id when the ref is absent. `:REQ:` and
`:REQ_BASE:` land in the same org write. If that write fails, the recorded tip
is restored.

When `task.level` is absent or None, `_is_same_headline` decides before the
`:ID:` short-circuit. It matches only the headline at `task.line`, and only
when `task.heading` is in `m.group(3)`.

## Design

### Canonical pair

`idea_link.canonical_pair(title, summary)` in `src/nightwright/idea_link.py`
strips the title, converts CR LF and remaining CR in the summary to LF, then
strips the summary. An empty summary is `""`. Internal spaces and marker
characters such as `**` stay.

`idea_link.req_base(title, summary)` is the lowercase SHA-256 hex digest of the
UTF-8 bytes of `canonical_title + "\n" + canonical_summary`. There is no
byte-order mark and no extra newline after the summary.

Publish hashes `task.heading` and the Summary from `idea_summary_text`. It
does not hash the raw headline line.

| Inputs | Payload | Digest |
|---|---|---|
| `("  Link  ", " \n Hello \n ")`, `("Link", "Hello")`, `("Link", "Hello\n")` | `Link\nHello` | `9700eaa1f712c11423e0542ae8882ba96653f4575be89e3f481d05df79eff208` |
| `("Link", "Hello\r\nworld\r")` | `Link\nHello\nworld` | `a0d1682323b9536aa06d0ce3c5a21ab22ac8f0be7cf24906aa88df17bc50c45d` |
| `("Link", " \r\n ")` | `Link\n` | `f8e7c032715c4691e8f0e4bfa4fae7b80f449f9fcae4c077b00d1bb1dd9fa654` |

### Request file

`idea_link.render_request(...)` returns the UTF-8 file body. Publish writes
that body and no other path. One file, `REQ-<eight>.md`, at the root of
`refs/heads/nw-backlog`. It starts with a line `---`, then YAML keys in this
order: `id`, `title`, `status`, `created`, `updated`, `created_by`,
`updated_by`. Publish passes no `spec` key, so the file has those seven keys
and no other key. `id` is the `:REQ:` value, with no `.md` suffix.

`title` is a double-quoted YAML scalar. A backslash is written as two
backslashes and a double quote is written as backslash-quote, so a title
`say "hi"` is the scalar `"say \"hi\""` and `split_frontmatter` returns
`say "hi"`. A title `a\b` is written with `\\` and parses back to one
backslash. `status` is `open`. `created` and `updated` are unquoted UTC
instants `YYYY-MM-DDTHH:MM:SSZ`. The renderer takes those two values as that
original text and writes the lines `created: YYYY-MM-DDTHH:MM:SSZ` and
`updated: YYYY-MM-DDTHH:MM:SSZ`. It does not dump the datetime
`split_frontmatter` returns (`yaml.safe_load` turns
`2026-11-08T12:00:00Z` into a datetime; dumping that value would change the
line). A second render that is given the same created text leaves the
`created` line byte-for-byte the same. `created_by` and `updated_by` parse as
`Name <email>` from that repo's `user.name` and `user.email`.

A line `---` closes the header. That closing line is newline-terminated. The
summary body is the canonical summary and nothing else, with no extra heading.
An empty summary is zero bytes after that newline. A non-empty summary is the
canonical summary's UTF-8 bytes and no added trailing newline.

`split_frontmatter` (`src/nightwright/specmeta.py`) matches
`^---\n(.*?)\n---\n(.*)$`. A file that ends on the closing `---` with no
following newline does not match. The renderer emits the newline so an empty
body still parses, with the body equal to `""`.

Publish and the renderer refuse before any write when `user.name` or
`user.email` is unset in the linked repo. Stderr names the missing key.

### Repository

`idea_link.resolve_linked_repo(cfg, project)` exits 1 with
`project is required` on a blank project, including a missing `:PROJECT:`, and
does not call `project_root`. For Meta-Tools it uses `nw_checkout` (config
`nw_checkout`, otherwise the discovered nightwright checkout) and ignores
`outbox_targets` and the current directory, even when `outbox_targets` also
has a Meta-Tools `repo_path`. For every other project it uses `project_root`
(`src/nightwright/agentic/project.py`), which reads
`outbox_targets[project].repo_path`. A missing root, including an empty
`repo_path`, exits 1 with a stderr line that starts
`cannot resolve a repo for project ` and names the project.

The Meta-Tools test directory contains `src/nightwright` and `pyproject.toml`,
so `nw_checkout` accepts it. It is also a git repo with local `user.name` and
`user.email`.

### Publish

Registered on the existing `idea` group in `src/nightwright/cli.py` as
`nw idea publish SELECTOR`.

Publish reads the Summary through `idea_summary_text` and
`read_idea_enrichment` on the Task from `resolve_selector`. It mints one
Crockford id, renders one file with `status: open` and no `spec` key, and
builds one commit:

1. `git hash-object -w` writes the blob.
2. `git mktree` writes the tree: the parent tree, or an empty tree when there
   is no parent, with only `REQ-<id>.md` added or replaced at mode `100644`.
   Every other path keeps its blob byte-for-byte.
3. `git commit-tree` writes the commit. The parent is the current tip, or
   there is no parent when the ref is absent. The message is
   `publish REQ-<id>`. Author and committer are that repo's `user.name` and
   `user.email` (the linked repo's config, not the current directory's).
4. `git update-ref refs/heads/nw-backlog <new> <expected>` compare-and-swaps
   the ref. `<expected>` is the previous tip, or
   `0000000000000000000000000000000000000000` when the ref was absent.

Publish does not run `git checkout`, `git switch`, `git add`, `git fetch`, or
`git push`. If `HEAD` is `refs/heads/nw-backlog`, publish exits 1 before any
object is written, stderr
`refusing to update nw-backlog while it is checked out`. Otherwise the
symbolic `HEAD`, the index, and the worktree stay as they were. Read of the
new file is `git show refs/heads/nw-backlog:REQ-<id>.md`.

If `update-ref` exits non-zero, publish exits 1, sets neither property, and
leaves the ref as it was.

On success, one atomic org write sets `:REQ:` and `:REQ_BASE:` together.
`:REQ_BASE:` equals `req_base` of `task.heading` and the Summary, which equals
`req_base` of the file title and the file summary. TODO state, headline,
Summary, log, questions, tags, clocks, and every other property stay.
`:UPDATED:` may move, stamped last. Stdout is the idea's `:ID:` and a newline.
The blob contains no `IDEA-NNN` unless that text was already in the title or
the summary.

Publish refuses, writes no commit, and sets neither property when the selector
is not an idea (`{selector!r} is not an idea`), the canonical title is empty
(`title is empty`) or contains a line feed (`title must be one line`), or
`:REQ:` is already set (`<id> is already linked to <REQ-…>`). A second publish
of another idea leaves the first file's blob sha unchanged. The new commit's
parent is the previous tip.

### Org-write failure

Publish records the old `nw-backlog` tip (the sha, or the fact that the ref
is absent) before adding the file. If that add succeeds and the org write
then fails, the org bytes stay as they were and the command compare-and-swaps
the ref back: `git update-ref -d refs/heads/nw-backlog <new>` when the ref was
absent, otherwise `git update-ref refs/heads/nw-backlog <old> <new>`. Stderr
is `REQ-<id> was not linked; restored refs/heads/nw-backlog` and the exit code
is 1. If the ref no longer points at `<new>`, the command leaves it, exits 1,
and stderr is `REQ-<id> was not linked; refs/heads/nw-backlog no longer points at <new>; left unchanged`.

### Heading lookup

Do not add `_idea_summary_text_from_file`. `_is_same_headline`
(`src/nightwright/org_write.py`) keeps today's comparison when `task.level`
is an int: an `:ID:` matches the drawer id, and otherwise the star count
equals `task.level` and `task.heading` is in `m.group(3)` of `_HEADLINE_RE`
(`^(\*+)(\s+)(.*)$`).

When `task.level` is absent or None, that check runs before the `:ID:`
short-circuit. It returns true only for `idx == task.line - 1`, and only when
that line is a headline and `task.heading` is in `m.group(3)`. A missing or
non-string heading returns false. It returns false for every other index,
including another headline with the same heading text and a headline whose
drawer `:ID:` equals the task's `:ID:`. `_locate_headline` therefore does not
raise `N matches`. Creating the `:PROPERTIES:` drawer for `:REQ:` and
`:REQ_BASE:` stays the property edit, `stamp_updated` last, and does not
replace `** Summary`, `** Open questions`, or `** Log`. The stamp test remains
the guard.

## Alternatives considered

- **Graduate the idea into the file and drop the org headline.** The public
  request is the shareable statement. The log, the clocks, the questions, and
  the TODO state stay private, and the idea can keep moving after publication.
- **Store `IDEA-NNN` in the public file.** The file format has no idea id. The
  link is the `:REQ:` property on the machine that holds the idea.
- **Hash the raw headline line.** The line carries the TODO keyword and the
  tag cookie. `task.heading` is the title the file stores, and the digest
  covers that title.
- **Let Meta-Tools follow `outbox_targets` like every other project.**
  `project_root` reads `outbox_targets` first, so a Meta-Tools `repo_path`
  would hide `nw_checkout`. Publish of this repo uses the checkout.
- **Treat a missing level as "not this headline", or let `:ID:` win first.**
  That is the failure that emptied Summary for a hand-written idea with no
  properties drawer. A missing level that matched every headline would raise
  `N matches` and empty Summary again.

## Acceptance criteria

- [x] `test_req_base_known_vectors` asserts the three digests in the Design
      table. The expected strings are written in the test, not produced by
      calling `req_base` to obtain the expected value. The private title
      publish hashes is `task.heading`: an idea whose headline is
      `* INCUBATE Link :session-triage:` and whose Summary is `Hello` gets
      `:REQ_BASE:` `9700eaa1f712c11423e0542ae8882ba96653f4575be89e3f481d05df79eff208`.
      A heading `Link  two` and a summary `say **bold**` are stored with the
      internal spaces and the `**` unchanged.
- [x] `resolve_linked_repo("")` and publish of an idea with no `:PROJECT:`
      each exit 1 with stderr `project is required` and do not call
      `project_root`. Meta-Tools uses `nw_checkout` even when
      `outbox_targets` also has a Meta-Tools `repo_path` and the current
      directory is a different repo. The Meta-Tools test directory contains
      `src/nightwright` and `pyproject.toml`. Every other project uses
      `project_root` (`outbox_targets[project].repo_path`). A missing root,
      including an empty `repo_path`, exits 1 with a stderr line that starts
      `cannot resolve a repo for project ` and names the project, adds no
      commit, and sets neither property. The file is in the project's repo.
- [x] `test_request_file_layout_and_invalid_file` asserts the published file
      is UTF-8 and starts with `---`, then the keys `id`, `title`, `status`,
      `created`, `updated`, `created_by`, `updated_by`, and no `spec` key.
      `title` is a double-quoted YAML scalar. A backslash is written as two
      backslashes and a double quote as backslash-quote. A title that contains
      a double quote, `say "hi"`, round-trips through `split_frontmatter` to
      `say "hi"`, and `a\b` round-trips to one backslash. The closing `---`
      line is newline-terminated, and an empty summary is zero bytes after
      that newline. A buffer that ends on the closing `---` with no following
      newline does not match `split_frontmatter`. `created` is copied as its
      original `YYYY-MM-DDTHH:MM:SSZ` text. The `created` line stays
      byte-for-byte `created: YYYY-MM-DDTHH:MM:SSZ` across a second
      `render_request` of that same text, and the renderer does not dump the
      datetime `split_frontmatter` returns. `created_by` and `updated_by`
      parse as `Name <email>`.
- [x] `nw idea publish` on an `INCUBATE` idea whose `:PROJECT:` resolves
      creates one commit on that repo's `refs/heads/nw-backlog` via
      `git hash-object`, `git mktree`, and `git commit-tree`, then
      compare-and-swaps `git update-ref`. When the ref was absent, the
      expected value is `0000000000000000000000000000000000000000`. The file
      is `open`, has the canonical title and summary, has no `spec` key, and
      has no idea id in the blob when the summary does not already contain
      that id. The idea's TODO state stays `INCUBATE`. `:REQ:` is that id.
      `:REQ_BASE:` is set in the same org write and equals `req_base` of
      `task.heading` and the Summary. Headline, Summary, log, questions,
      tags, and `CLOCK:` lines stay. When `nw-backlog` is not checked out, the
      symbolic `HEAD`, the index, and the worktree stay as they were. The
      commit message is `publish REQ-<id>`. Author and committer are that
      repo's `user.name` and `user.email`. Stdout is the idea's `:ID:` and a
      newline. The command runs no `git checkout`, `git switch`, `git add`,
      `git fetch`, or `git push`.
- [x] Publish of an idea that already has `:REQ:`, an empty title, a title
      that contains a line feed, or a selector that is not an idea exits 1
      with the Design's stderr (`<id> is already linked to <REQ-…>`,
      `title is empty`, `title must be one line`, `{selector!r} is not an idea`)
      and adds no commit. Unset `user.name` or `user.email` refuses before any
      object is written, and stderr names the missing key. Publish while
      `nw-backlog` is checked out exits 1 with stderr
      `refusing to update nw-backlog while it is checked out`, adds no commit,
      and runs no `hash-object`. A failed `update-ref` exits 1, sets neither
      property, and leaves the ref as it was.
- [x] A second publish of another idea leaves the first file's blob sha
      unchanged. The new commit's parent is the previous tip, the
      `update-ref` expected value is that tip, and every other path stays
      byte-for-byte.
- [x] If the org write fails after publish's add, the org bytes are unchanged,
      the command exits 1, and compare-and-swap restores the recorded tip, or
      deletes the ref when it was absent (`git update-ref -d
      refs/heads/nw-backlog <new>`). Stderr is
      `REQ-<id> was not linked; restored refs/heads/nw-backlog`. When the ref
      no longer points at that new commit, stderr is
      `REQ-<id> was not linked; refs/heads/nw-backlog no longer points at <new>; left unchanged`
      and the ref is left as it is.
- [x] When `task.level` is absent or None, `_is_same_headline` runs before the
      `:ID:` short-circuit and returns true only for `idx == task.line - 1`
      when that line is a headline and `task.heading` is in `m.group(3)`. It
      returns false for every other index, including a headline whose drawer
      `:ID:` equals the task's `:ID:` and a later headline with the same
      heading, so `_locate_headline` does not raise `N matches`. When
      `task.level` is an int, an `:ID:` still matches by drawer id. There is
      no `_idea_summary_text_from_file`.
      `tests/test_idea_stamps.py::test_stamp_creates_missing_drawer_without_corrupting_sections`
      passes: after a log append creates the properties drawer, Summary still
      contains `keep me`, Open questions still contain `keep this question`,
      and the log still contains `keep this finding` and the new finding.
      `nw ideas` and `nw hub` keep building rows with `idea_row`, which calls
      `next_step_for_idea`, and the branch stays unread. The TUI list keeps
      `_row_for`, which does not call `idea_show_payload` or
      `next_step_for_idea`. The TUI detail keeps `load_detail`, which calls
      `idea_show_payload` with one argument and `next_step_for_idea`. The
      callers in `agentic/queue.py` and `agentic/spec_review.py` keep that
      one-argument call. A git spy on `nw ideas --json` sees no read of
      `refs/heads/nw-backlog`.

## Test plan

- **Automated tests:** `tests/test_idea_link.py`. The fixture is a temporary
  git repo with `user.name` and `user.email` set and a config whose
  `outbox_targets[<project>].repo_path` is that repo, plus a temporary org
  ideas file. A second temporary repo is the current directory when a test
  checks that cwd is ignored. Meta-Tools cases set `nw_checkout` to a
  temporary git repo that contains `src/nightwright` and `pyproject.toml`,
  and also set `outbox_targets` for Meta-Tools to a different repo. Request
  bytes are read with `git show refs/heads/nw-backlog:<path>`. CliRunner
  covers exit codes. A git spy records `hash-object`, `mktree`, `commit-tree`,
  `update-ref`, and any `checkout`, `switch`, `add`, `fetch`, or `push`.
  - `test_req_base_known_vectors` — the three payloads, including whitespace,
    a trailing newline, CR LF, and an empty summary. The expected digests are
    literals. The private title is `task.heading`, including the pinned
    `Link` / `Hello` digest on a headline that also has a tag cookie, and
    `Link  two` / `say **bold**` stored unchanged.
  - `test_publish_uses_the_project_repo_not_cwd` — Meta-Tools uses
    `nw_checkout` even when `outbox_targets` also has a Meta-Tools
    `repo_path` and the test directory contains `src/nightwright` and
    `pyproject.toml`. Another project uses `project_root`
    (`outbox_targets[project].repo_path`). cwd is a different repo. A spy
    shows Meta-Tools publish does not call `project_root`. Symbolic `HEAD`
    is unchanged, and the command runs no `git checkout`, `git switch`,
    `git add`, `git fetch`, or `git push`.
  - `test_publish_refuses_without_a_repo` — blank project and a missing
    `:PROJECT:` exit 1 with `project is required` and do not call
    `project_root`. Unknown project and empty `repo_path` exit 1 with stderr
    that starts `cannot resolve a repo for project `. Unset `user.name` and
    unset `user.email` refuse before `hash-object`. Publish while
    `refs/heads/nw-backlog` is checked out exits 1 with stderr
    `refusing to update nw-backlog while it is checked out` and adds no
    commit.
  - `test_publish_links_without_changing_state_or_sections` — the INCUBATE
    publish criterion, including the pinned digest, the absence of the idea
    id, the seven keys, a double-quoted title, `Name <email>`, the message
    `publish REQ-<id>`, author and committer from that repo, `:REQ:` and
    `:REQ_BASE:` in the same org write, and stdout equal to the idea's
    `:ID:` plus a newline. When the ref was absent, the spied `update-ref`
    expected value is `0000000000000000000000000000000000000000`.
  - `test_second_publish_preserves_existing_requests` — a second publish of
    another idea leaves the first file's blob sha unchanged. The new
    commit's parent is the previous tip. The spied `update-ref` expected
    value is that tip. Every other path stays byte-for-byte.
  - `test_publish_refuses_empty_title_multiline_title_and_second_link` —
    also a selector that is not an idea, and a failed `update-ref` that
    leaves the ref and the org file unchanged.
  - `test_request_file_layout_and_invalid_file` — key order, no `spec` key,
    `say "hi"` and `a\b` round-trip through `split_frontmatter`, the closing
    fence is newline-terminated, an empty body is zero bytes after that
    newline, a closing `---` with no following newline does not parse, and
    the `created` line stays byte-for-byte `created: YYYY-MM-DDTHH:MM:SSZ`
    across a second `render_request` of that original text. The renderer
    does not dump the datetime `split_frontmatter` returns.
  - `test_publish_org_write_failure_restores_the_old_tip` — both the restored
    case (including `update-ref -d` when the ref was absent) and the lost
    compare-and-swap case.
  - `test_heading_without_level_matches_only_task_line` — when `task.level`
    is absent or None, `_is_same_headline` is true only for
    `idx == task.line - 1` when `task.heading` is in `m.group(3)`, including
    when the task has an `:ID:` and that line's drawer does not. It is false
    for every other index, so `_locate_headline` does not raise `N matches`.
    When `task.level` is an int, `:ID:` still matches the drawer.
- **Regression guard:**
  `tests/test_idea_stamps.py::test_stamp_creates_missing_drawer_without_corrupting_sections`
  remains the guard, and the other tests in `tests/test_idea_stamps.py`. An
  idea with no `:REQ:` still gets today's `workflow.next_step_for_idea` hint
  (`tests/test_workflow.py`). `test_ideas_and_hub_keep_idea_row` in
  `tests/test_idea_link.py` asserts `nw ideas` and `nw hub` build rows with
  `idea_row`, the TUI list uses `_row_for`, `load_detail` and the callers in
  `agentic/queue.py` and `agentic/spec_review.py` call `idea_show_payload`
  with one argument, and a git spy on `nw ideas --json` sees no read of
  `refs/heads/nw-backlog`.
- **Manual verification:** in a temporary repo whose `user.name` and
  `user.email` are set, `nw idea publish IDEA-NNN` prints that idea id. The
  idea stays in its TODO state, gains `:REQ:` and `:REQ_BASE:`, and
  `git show refs/heads/nw-backlog:REQ-<id>.md` shows the canonical title and
  summary. Symbolic `HEAD` is unchanged. Publish a second idea; the first
  file's blob sha is unchanged. `git rev-parse refs/heads/nw-backlog` is the
  new commit, and that commit's parent is the previous tip.

### Test record (2026-11-09)

- `uv run pytest tests/test_idea_link.py -q` — 12 passed.
- `uv run pytest tests/test_idea_stamps.py::test_stamp_creates_missing_drawer_without_corrupting_sections -q` — passed.
- `uv run pytest tests/test_workflow.py -q` — passed.
- Full suite left to queue gating.

### Test record (2026-11-09, review loop 2)

- `uv run pytest tests/test_idea_link.py -q` — 14 passed (adversarial-review fixes + tightened assertions).
- `uv run pytest tests/test_idea_stamps.py::test_stamp_creates_missing_drawer_without_corrupting_sections -q` — passed.
- `uv run pytest tests/test_workflow.py -q` — passed.
- `python3 tools/spec_lint.py --write-ledger && python3 tools/spec_lint.py` — exit 0.
- Full suite left to queue gating.

### Test record (2026-11-09, review loop 3)

- `uv run pytest tests/test_idea_link.py -q` — 15 passed (`--full-tree` mktree fix + subdirectory `repo_path` regression).
- `uv run pytest tests/test_idea_stamps.py::test_stamp_creates_missing_drawer_without_corrupting_sections tests/test_workflow.py -q` — 19 passed.
- `python3 tools/spec_lint.py --write-ledger && python3 tools/spec_lint.py` — exit 0.
- Full suite left to queue gating.

### As shipped (2026-11-09, review loop 2)

- `nw-backlog` tree rebuild uses `git ls-tree --full-tree -z` (one level) / `git mktree -z`, raw path bytes, and raises on unparsable entries; keeps each entry's mode and object type.
- Git env scrub (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_NAMESPACE`, …) on every linked-repo git call; commit identity from linked-repo config only.
- `nw-backlog` checked out in any linked worktree refuses publish like a checked-out branch.
- `created_by` / `updated_by` are double-quoted YAML scalars; publish uses `idea_summary_text(..., strict=True)` and refuses when Summary cannot be read.
- Restore after org-write failure checks `update-ref` exit code before reporting restored.

## Rollout / migration

No backfill. Ideas that already exist gain `:REQ:` only when someone runs
`nw idea publish`. The first publish creates `refs/heads/nw-backlog` when the
ref is absent, using the all-zero expected value. The product checkout is left
on its current branch. Publish refuses when that branch is the checked-out
`HEAD`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; the tests named above pass. The full suite stays on
      the queue's close gate.
- [x] No regressions in `tests/test_idea_stamps.py` and `tests/test_workflow.py`.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated and `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
