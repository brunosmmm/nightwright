---
id: SPEC-0463
title: Periodic deterministic resolution of queue dependency conflicts
status: done
owner: user
created: 2026-11-07
updated: 2026-11-07
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, queue]
source_idea: IDEA-1024
---

## Context

[SPEC-0423](./0423-idea-campaign-ordering-with-after-and-epic.md) is the wait. `queue.after_met` (`src/nightwright/agentic/queue.py`) turns a campaign's `after` list into `blocked_by`, and `quiet.dependency_wait` (`src/nightwright/agentic/quiet.py`) holds a backlog line only when every entry starts with `waiting on ` and every id in it is a live campaign's spec id, campaign id, or idea id. Any other text wakes the conductor with the raw string. A plain `backlog` event is held more loosely (`backlog_resolved` only checks the `waiting on ` prefix); `backlog_blocked_long` and the conductor ping use `dependency_wait`. `live_specs` comes from `list_campaigns` without terminal states, so a `HANDED_OFF` campaign id is not live.

That wake is wrong for four conflicts seen on 2026-11-07. Each has one outcome.

- An idea ref is treated as met when the main checkout's copy of the prerequisite campaign's `spec_id` is `done`. A reused number trips this. IDEA-967's live harness campaign `IDEA-967-20261107-2a79` is REVIEWING with `spec_id` SPEC-0446, while main's `docs/specs/0446-sdk-model-effort-catalog.md` is a different spec already marked `done`. `after_met` releases the wait while that campaign is still running. The same release happens once that campaign is `HANDED_OFF`: `after_met` returns met only for `MERGED` before the spec-file branch, and `ABANDONED` is the other special case, so a `done` file on main satisfies the idea ref while the PR is open and the campaign's spec is still on the branch. `dependency_wait` cannot hold the campaign id in that window.
- The prerequisite spec is not on main, but a live campaign carries the id. IDEA-888 waits on IDEA-887, whose campaign carries SPEC-0407, a file that exists only in that campaign's worktree. `after_met` reports `SPEC-0407 not found`.
- The prerequisite idea has no campaign and no `:SPEC:`. IDEA-889 waits on IDEA-1015. `after_met` returns `waiting on IDEA-1015 (after: no campaign queued for IDEA-1015)`. IDEA-1015 is not a live id, so the ping and `backlog_blocked_long` wake the conductor with that string. There is nothing to wait on, and nothing to queue.
- A QUEUED campaign already has its branch checked out at `repo.worktree`. Admission calls `git.worktree_add` (`gitops.worktree_add`), which refuses an existing path or branch. IDEA-887, IDEA-888, and IDEA-889 were re-queued onto worktrees that already exist. IDEA-887's spec file is still `proposed` and `spec.id` is set, with an empty `pending_brief`. IDEA-888 and IDEA-889 have `accepted` spec files (the accept commit is the last commit on each branch). A fresh open would send IDEA-887 to IMPLEMENTING, because `spec.id` is set and `has_no_spec` is false. Landing that proposed file in SPEC_WRITING also fails: `plan_step` parks SPEC_WRITING when `spec.id` is set and `pending_brief` is empty (`spec_review_unavailable`, detail "nothing to revise"). A committed proposed spec leaves SPEC_WRITING: `_apply_spec_write` finishes SPEC_REVIEW.

## Goals / Non-goals

**Goals**
- Keep a wait unmet, with text `waiting on <campaign-id> (after: campaign <campaign-id> is <state>)`, while the prerequisite idea's own campaign is still live, and through `HANDED_OFF`, including when main's file for that campaign's `spec_id` is already `done`. The `done` file satisfies the ref when the campaign is `MERGED`.
- When the spec file is missing, an idea ref waits on that idea's campaign that carries the id, even when another campaign is the newest hit for the `spec_id`. A spec ref waits on the newest non-terminal campaign with that id. Do not report `not found`.
- When the prerequisite idea has no campaign and no spec, mail the conductor once, naming the waiter, the idea, and that it has no campaign. Do not queue the idea. Do not keep waking on the raw string. The mail marker survives the tick's backlog write.
- When a QUEUED campaign's branch is already checked out at its planned worktree, resume into that worktree at the phase that file and brief support. Do not call `worktree_add`.

**Non-goals**
- Replacing SPEC-0423. `after` storage, `nw queue after`, epic `:AFTER:` seeding, and the spec writer's must-include `depends_on` stay as they are.
- Holding a spec ref (`--after SPEC-…`) unmet because some other campaign reused a number whose file on main is `done`. A done file still satisfies a spec ref.
- Changing the wait text for a live idea campaign that has no `spec_id` yet (`waiting on <idea> (after: campaign <id> is <state>)`).
- Queueing the missing idea, removing the `after` entry, deleting a worktree or branch, or resetting a branch onto main.
- Merge open (`_do_open_merge`), which already adopts a branch's checkout. Split, revert, and red-main opens, which keep today's `worktree_add` behavior. A revert open still calls `worktree_add`, then `revert_campaign_range`, and `_apply` still finishes CLOSING or parks `revert_conflict`.
- A new timer or config knob. The queue tick is the period.
- Writing the main branch, or weakening merge, the campaign lease, or cost caps.

## Decision

1. **SPEC-0423 stays the wait.** `after_met` still decides met or unmet. This spec corrects two of its answers, and adds the two actions `after_met` cannot take.
2. **A live idea campaign wins over the file on main, and the hold lasts through `HANDED_OFF`.** For an idea ref, if the idea's newest campaign is non-terminal or `HANDED_OFF`, and `_spec_after_met` would report that campaign's `spec_id` met from the main checkout's file, the ref stays unmet. The `blocked_by` entry is `waiting on <campaign-id> (after: campaign <campaign-id> is <state>)`, and at `HANDED_OFF` that text is `waiting on <campaign-id> (after: campaign <campaign-id> is HANDED_OFF)`. The `done` file satisfies the ref when the campaign is `MERGED`. `ABANDONED` stays today's result. A spec ref whose file is `done` stays met.
3. **A missing file waits on the campaign that carries the id.** When `_spec_after_met` reports `<spec_id> not found`, the idea ref stays unmet as `waiting on <campaign-id> (after: campaign <campaign-id> is <state>)` for that idea's own campaign, even when `list_campaigns(..., spec_id=)`'s first hit is a different campaign. A spec ref uses that first hit when it is non-terminal, with the same campaign-wait text. Neither says `not found`. With no such campaign, today's `not found` and `abandoned` texts stay, and they still wake the conductor.
4. **Nothing to wait on is one mail.** Once per tick, before admission, `resolve_dependency_conflicts` mails the conductor one request for each QUEUED waiter whose unmet idea ref is `waiting on <idea> (after: no campaign queued for <idea>)`. The queue does not queue that idea. It sets `conflict_mail` on the QUEUED dict the tick is about to record, so readiness rewrites that entry and the ping and `backlog_blocked_long` hold it. A second pass does not send again.
5. **An existing checkout is resumed, not re-opened.** At open, for a campaign that is not merge, split, revert, or red-main, if `worktree_for_branch` is already the planned path, skip `worktree_add` and skip setup when `base_sha_at_open` is already set. A present `accepted` or `in-progress` file finishes in IMPLEMENTING, except when `admission == "revise_pending"`, which resumes to SPEC_WRITING (SPEC-0279: the writer must amend before implement). `admission == "after_revision"` with a pending brief still finishes in IMPLEMENTING. A present `proposed` file whose `spec.id` is set and whose `pending_brief` is empty finishes in SPEC_REVIEW; IDEA-887 resumes to SPEC_REVIEW, and `plan_step` on that state returns `spec_review`. A present `draft` or `proposed` file finishes in SPEC_WRITING when `spec.id` is unset or `pending_brief` is set, and `plan_step` returns `write_spec`. A present `draft` file with `spec.id` set and an empty `pending_brief` uses the `open_failed` park. A missing spec file resumes to SPEC_WRITING when `has_no_spec(state)` or `admission == "revise_pending"`. Otherwise, when `spec.id` is set and the file is absent, park `open_failed` with the missing path in the detail, the same park as an unreadable spec, and leave the worktree and branch. IDEA-888 and IDEA-889 resume to IMPLEMENTING because their worktree specs are `accepted`. A later campaign uses the same rule on its own worktree. A successful resume writes the ledger note `resumed into existing worktree → {state}` with author `queue`.

## Design

### Wait text (`queue.after_met`)

`_spec_after_met` is unchanged for its callers' met/unmet result, except that `after_met` does not take two of those results at face value.

Idea ref. Newest means the first campaign of that idea, and the wait names that campaign. When that campaign is not `MERGED` and not `ABANDONED`, and it has a `spec_id`:

- `_spec_after_met` reports met from the file on main: return unmet, `waiting on <that campaign id> (after: campaign <id> is <state>)`. Do not return met. The hold includes `HANDED_OFF`, with text `waiting on <campaign-id> (after: campaign <campaign-id> is HANDED_OFF)`. The `done` file satisfies the ref when the campaign is `MERGED`. `ABANDONED` stays today's result.
- When `_spec_after_met` reports `<spec_id> not found`, the idea ref stays unmet as `waiting on <campaign-id> (after: campaign <campaign-id> is <state>)` for this idea's campaign, even when `list_campaigns(..., spec_id=)`'s first hit is a different campaign. Do not return `waiting on <spec_id> (… not found)`.
- Any other unmet why (the file exists and is `accepted`, `in-progress`, `draft`, `proposed`, …) stays today's `waiting on <spec_id> (after: <idea> -> <why>)`.
- No `spec_id` on that campaign: today's `waiting on <idea> (after: campaign <id> is <state>)` stays.

Spec ref: when the why ends in `not found` and `list_campaigns(..., spec_id=)`'s newest hit is non-terminal, return the campaign-wait text for that hit. When the file is met, or the newest hit is `MERGED`, return met as today, even if an older campaign reused the id. When the newest hit is missing or terminal and the file is missing, keep `after <spec_id> not found`.

For a spec ref, newest means that first hit, and an older non-terminal row behind a newer terminal one stays behind it. For an idea ref, newest means the first campaign of that idea, and the wait names that campaign.

### One mail (`queue.resolve_dependency_conflicts`)

Called from `_tick` after the parked-campaign recovery steps and before the admission loop that calls `backlog_readiness` and `_record_backlog`. It scans QUEUED campaigns only. `resolve_dependency_conflicts` mutates the QUEUED dicts the tick is about to pass to `backlog_readiness` (the `queued` list, including campaigns recovery just appended) and sets `conflict_mail` on those dicts before `_record_backlog`. Those are the same objects the admission loop reads. `list_campaigns` returns deep copies; a marker written on a second copy is erased when `_record_backlog` `write_state`s the tick's dict. A failed send appends nothing.

For each unmet `after` entry whose `after_met` text is exactly `waiting on <idea> (after: no campaign queued for <idea>)`:

- That text is already the "idea exists, `:SPEC:` empty, no campaign" case. Do not queue the idea (`plan_add_idea` is not called). Do not remove the `after` entry.
- If `state["conflict_mail"]` (a list of strings, absent on older campaigns) does not contain the idea id, send one mailbox message and then append the idea id on that QUEUED dict. A failed send appends nothing, so the next tick retries.
- Message: `to=conductor`, `from=conductor`, `kind=request`, `campaign` the waiter id, text `{waiter} waits on {idea}. {idea} has no campaign and no spec. Do not queue {idea}.`

`_after_blocked` rewrites a no-campaign line to `mailed conductor: {idea} has no campaign ({waiter})` when the idea id is in `conflict_mail` on the dict `backlog_readiness` reads. Any other `after_met` result is kept as-is, so a campaign that appears later replaces the mailed line with an ordinary wait on its own. The same tick's `_record_backlog` persists that dict, including `conflict_mail` and the mailed `blocked_by`.

`quiet._BENIGN_BLOCKED` gains the prefix `mailed conductor: `. `dependency_wait` holds an entry with that prefix without requiring a live id. `dependency_wait` also holds an entry of the exact shape `waiting on <campaign-id> (after: campaign <campaign-id> is HANDED_OFF)` without the id being in `live_specs`. Every other `waiting on` entry still needs a live id. Other entries in the same list are judged as today. `backlog_resolved` holds the mailed prefix because the prefix is benign.

### Resume into an existing worktree (`_do_open_unwrapped`)

Before `git.worktree_add`, for a campaign that is not a merge, split, revert, or red-main open: if `git.worktree_for_branch(root, repo.branch)` resolves to `Path(repo.worktree)`, adopt that checkout.

- Do not call `worktree_add` or `worktree_add_existing`. Do not reset the branch, commit, or remove the worktree.
- Keep a `base_sha_at_open` that is already set. If it is null, set it the way a fresh open does, and run `_run_worktree_setup`. If it is set, skip setup.
- Refresh `repo.head` from the worktree. Keep `spec_at_open.md` only when it is a non-empty regular file under the campaign dir whose bytes match `sha256_at_open`; otherwise rewrite it from the worktree spec via a temp file and `os.replace` (fresh open uses the same path). A failed rewrite clears `sha256_at_open` and parks `open_failed`.
- `worktree_resume_state(state, worktree)` picks the phase. The spec file is `spec.rel_path` inside the worktree when that path is set and the file exists.
  - File present, status `accepted` or `in-progress`: `IMPLEMENTING`, except `admission == "revise_pending"` resumes to `SPEC_WRITING`. `admission == "after_revision"` with a pending brief stays `IMPLEMENTING`.
  - File present, status `proposed`, `spec.id` set, and `pending_brief` empty: `SPEC_REVIEW`. IDEA-887 resumes to SPEC_REVIEW, and `plan_step` on that state returns `spec_review`.
  - File present, status `draft` or `proposed`, when `spec.id` is unset or `pending_brief` is set: `SPEC_WRITING`. `plan_step` returns `write_spec`.
  - File present, status `draft`, `spec.id` set, and `pending_brief` empty: the `open_failed` park. Leave the worktree and branch.
  - File absent: `SPEC_WRITING` when `has_no_spec(state)` or `admission == "revise_pending"`. Otherwise, when `spec.id` is set and the file is absent, park `open_failed` with the missing path in the detail, the same park as an unreadable spec, and leave the worktree and branch.
  - File present with any other status (`done`, `rejected`, `superseded`, empty, unreadable): do not adopt into a phase. Set the open error to a string that includes that status, so today's `open_failed` park applies (`resume_to` QUEUED). Leave the worktree and branch. Mail once, deduped by `conflict_mail` containing `worktree:<status>`, text `{campaign} already has worktree {path} on {branch}, and its spec status is {status}. Not opened.` Same sender, recipient, and `kind=request` as the other mail. Add `conflict_mail` to the keys `finish` copies onto the re-read state, and set the `worktree:<status>` marker on that same dict before `_park`. A failed send appends nothing.
- `_apply`'s `open` branch, for a successful adopt, finishes at `worktree_resume_state` with reason `resumed_worktree` and a ledger `note` `resumed into existing worktree → {state}` (author `queue`). The fresh-open target is not used when the spec file is present. A present `accepted` or `in-progress` file finishes in IMPLEMENTING except when `admission == "revise_pending"` (SPEC_WRITING). A present `proposed` file whose `spec.id` is set and whose `pending_brief` is empty finishes in SPEC_REVIEW. A present `draft` or `proposed` file finishes in SPEC_WRITING when `spec.id` is unset or `pending_brief` is set.
- Any other layout (path exists but is not that branch's checkout, or the branch is checked out elsewhere) is unchanged: `worktree_add` runs and parks `open_failed` as today. No mail.

Merge, split, revert, and red-main opens do not use this adopt path. A revert open still calls `worktree_add`, then `revert_campaign_range`, and `_apply` still finishes CLOSING or parks `revert_conflict`.

## Alternatives considered

- **Keep waking the conductor with the raw `blocked_by` string for all four.** Lost: three of the four have one correct outcome, and the raw string is the wake this pass replaces.
- **Treat main's `done` file as met whenever the `spec_id` matches, including for an idea ref.** Lost: that is the reused-id release. SPEC-0446 on main is not the harness campaign's spec. The same release at `HANDED_OFF` would satisfy the idea ref while the PR is still open.
- **Apply that same hold to spec refs.** Lost: a waiter of the shipped SPEC-0446 would then wait on the harness campaign that reused the number.
- **Queue the missing idea automatically.** Lost: the outcome for IDEA-1015 is that there is nothing to wait on, and the queue must not queue it.
- **Admit by calling `worktree_add` at a new path and leaving the old worktree.** Lost: the branch already exists, and the spec commits already on it would be left behind. A fresh open would also put a `proposed` spec whose `spec.id` is set into IMPLEMENTING, and SPEC_WRITING would park that file on an empty brief.
- **Write `conflict_mail` on a freshly loaded campaign copy.** Lost: `_record_backlog` and `finish` write a different dict, and the next tick would mail again.

## Acceptance criteria

- [x] AC1: An idea ref whose newest campaign is non-terminal and has a `spec_id` stays unmet when the main checkout's file for that id is `done`. `blocked_by` is exactly `waiting on <campaign-id> (after: campaign <campaign-id> is <state>)`. `dependency_wait` holds it. The same campaign-wait text stays when that campaign is `HANDED_OFF`, exactly `waiting on <campaign-id> (after: campaign <campaign-id> is HANDED_OFF)`, and `dependency_wait` is true even though that id is not in `live_specs`. After that campaign is `MERGED`, readiness is `(True, [])`: the `done` file satisfies the ref. `ABANDONED` stays today's result.
- [x] AC2: An idea ref whose file is absent stays unmet when that idea's non-terminal campaign carries the id, even when `list_campaigns(..., spec_id=)`'s first hit is a different campaign. `blocked_by` is `waiting on <that idea's campaign-id> (after: campaign <campaign-id> is <state>)` and does not contain `not found`. `dependency_wait` holds it. A spec ref whose file is absent stays unmet when `list_campaigns(..., spec_id=)`'s first hit is non-terminal. `blocked_by` is the campaign-wait text for that hit and does not contain `not found`. `dependency_wait` holds it. With no non-terminal campaign carrying the id, a spec ref still blocks as `after <spec_id> not found`, which is not a `dependency_wait`.
- [x] AC3: A spec ref whose file on main is `done` is met even when some other non-terminal campaign has that `spec_id`. An idea ref whose live campaign has no `spec_id` still blocks as `waiting on <idea> (after: campaign <id> is <state>)`.
- [x] AC4: For a QUEUED waiter of an idea that has no campaign and no `:SPEC:`, the first `resolve_dependency_conflicts` sends one conductor request whose text is `{waiter} waits on {idea}. {idea} has no campaign and no spec. Do not queue {idea}.` A second call sends nothing. No campaign is created for the idea. The `after` entry remains. `conflict_mail` on the QUEUED dict passed in contains the idea id. Readiness then reports `mailed conductor: {idea} has no campaign ({waiter})`, and both `dependency_wait` and a `backlog_blocked_long` classification hold that entry. The pre-mail text `waiting on {idea} (after: no campaign queued for {idea})` still does not.
- [x] AC5: `queue.tick` runs `resolve_dependency_conflicts` on the QUEUED dicts it is about to pass to `backlog_readiness` (the `queued` list, including campaigns recovery just appended) and sets `conflict_mail` on those dicts before `_record_backlog`. The state `_record_backlog` wrote contains `conflict_mail` and the mailed `blocked_by` text. A second tick sends no mail.
- [x] AC6: Opening a campaign that is not merge, split, revert, or red-main, whose branch is already checked out at `repo.worktree`, does not call `worktree_add`, does not change the branch HEAD, and does not run setup when `base_sha_at_open` is set. The transition reason is `resumed_worktree`, and the ledger note is `resumed into existing worktree → {state}` with author `queue`. A present `accepted` or `in-progress` file finishes in IMPLEMENTING except when `admission == "revise_pending"`, which resumes to SPEC_WRITING; `admission == "after_revision"` with a pending brief stays IMPLEMENTING. On adopt or fresh open, `spec_at_open.md` is kept only when it is a non-empty regular file under the campaign dir whose bytes match `sha256_at_open`; otherwise it is rewritten from the worktree spec, and a failed rewrite parks `open_failed` with the hash cleared. A present `proposed` file whose `spec.id` is set and whose `pending_brief` is empty finishes in SPEC_REVIEW, and `plan_step` on that state returns `spec_review`. A present `draft` or `proposed` file finishes in SPEC_WRITING when `spec.id` is unset or `pending_brief` is set, and `plan_step` returns `write_spec`. A present `draft` file with `spec.id` set and an empty `pending_brief` parks `open_failed`. A missing spec file resumes to SPEC_WRITING when `has_no_spec(state)` or `admission == "revise_pending"`. Otherwise, when `spec.id` is set and the file is absent, the open parks `open_failed` with the missing path in the detail, the same park as an unreadable spec, and leaves the worktree and branch. A campaign with no such checkout still calls `worktree_add` and uses today's fresh-open target. A revert open is not this path: it still calls `worktree_add`, then `revert_campaign_range`, and `_apply` finishes CLOSING or parks `revert_conflict`. It does not finish with reason `resumed_worktree`.
- [x] AC7: A worktree spec whose status is `done` does not move the campaign to SPEC_WRITING, SPEC_REVIEW, or IMPLEMENTING. The open parks `open_failed` with that status in the detail, the worktree and branch remain, and one conductor mail names the campaign, the path, and the status. The parked state still contains `conflict_mail` (the `worktree:<status>` marker). A second open attempt does not send a second mail.

## Test plan

- **Automated tests**
  - Extend `tests/test_idea_ordering.py`. AC1: idea campaign with `spec_id` pointing at a fixture spec flipped to `done`, state REVIEWING; a second idea `--after` the first; assert the exact campaign-wait `blocked_by`, `QT.dependency_wait` true; `_transition` to HANDED_OFF and assert `blocked_by` is `waiting on <campaign-id> (after: campaign <campaign-id> is HANDED_OFF)` and `QT.dependency_wait` is true without that id in `live_specs`; then `_transition` to MERGED and assert readiness `(True, [])`. AC2: set `spec_id` to an id with no file in the fixture `docs/specs` (SPEC-0407), state QUEUED; the idea ref stays on that idea's non-terminal campaign that carries the id, including when `list_campaigns(..., spec_id=)`'s first hit is a different campaign, and the spec ref follows that first hit when it is non-terminal; both get campaign-wait text and not `not found`; abandon the carrying campaign and assert the spec ref is `after SPEC-0407 not found` and not a `dependency_wait`. AC3: a spec ref to the `done` fixture spec is ready while another live campaign carries that `spec_id`; a live idea campaign with `spec_id` left null still produces `waiting on <idea> (after: campaign <id> is QUEUED)`, the assertion `test_after_idea_waits_then_is_ready_when_prerequisite_merges` already pins.
  - New `tests/test_queue_conflict_resolve.py`, using the `env` fixture from `tests/test_agentic_idea_campaign.py`. AC4: waiter `--after` an idea that was captured and then has no campaign (do not call `add_idea_campaign` for it) and no `:SPEC:`; call `resolve_dependency_conflicts` twice on the QUEUED dict the caller holds; assert one `mailbox.read_inbox(cfg, "conductor")` request with the exact text, `needs_reply` true, `campaign` the waiter; `list_campaigns` grows by none for that idea; `conflict_mail` on that dict contains the idea id; `backlog_readiness` returns the `mailed conductor:` line; `QT.dependency_wait` holds it and `QT.classify` holds a `backlog_blocked_long` event of it; the same classify on the pre-mail text wakes. AC5: run `queue.tick` once on that fixture and read `conflict_mail` and the mailed `blocked_by` from the state `_record_backlog` wrote; a second tick sends no mail. AC6: `git.worktree_add` the campaign's own planned path and branch, write `docs/specs/` files, set `spec.rel_path`, set `base_sha_at_open`, transition to OPENING, `_do_open` then `_apply`; monkeypatch `git.worktree_add` and `_run_worktree_setup` to fail the test if called; assert unchanged HEAD, reason `resumed_worktree`, and a ledger note `resumed into existing worktree → {state}` with author `queue`. A present `accepted` or `in-progress` file finishes in IMPLEMENTING. A present `proposed` file with `spec.id` set and `pending_brief` empty finishes in SPEC_REVIEW, and the test calls `plan_step` and expects `spec_review`. A present `draft` or `proposed` file finishes in SPEC_WRITING when `spec.id` is unset or `pending_brief` is set, and `plan_step` returns `write_spec`. A present `draft` file with `spec.id` set and an empty `pending_brief` parks `open_failed`. One adopted campaign with `spec.id` set and no spec file parks `open_failed` with the missing path in the detail, and the worktree and branch remain. A campaign with no checkout asserts `worktree_add` is called and a spec-less idea (`has_no_spec`) finishes SPEC_WRITING. No AC6 campaign is a revert, and the test does not finish a revert with reason `resumed_worktree`. AC7: status `done` in the worktree; assert `open_failed`, detail contains `done`, worktree still present, one mail, and the parked state still contains `conflict_mail`; a second attempt sends no new mail.
- **Manual verification:** none. The tests build the four situations in a temp repo and a temp data dir. Do not run them against the live `NW_DATA_DIR` or the existing IDEA-887, IDEA-888, IDEA-889, or IDEA-967 worktrees.
- **Regression guard:** `tests/test_idea_ordering.py` and `tests/test_agentic_quiet.py` pass without weakening their current assertions. A campaign with no `after` is unchanged. A revert open still calls `worktree_add`. `python3 tools/affected_tests.py --run uv run pytest -q` on the touched files, plus `python3 tools/spec_lint.py`.

### Test record (2026-11-07)

- `uv run pytest -q tests/test_idea_ordering.py tests/test_queue_conflict_resolve.py tests/test_agentic_quiet.py` — 167 passed.
- `python3 tools/spec_lint.py --write-ledger` — passed.
- Full suite left to gating.

### Test record (2026-11-07, loop 1 review fixes)

- `uv run pytest -q tests/test_idea_ordering.py tests/test_queue_conflict_resolve.py tests/test_agentic_quiet.py` — 179 passed.
- `python3 tools/spec_lint.py` — passed.
- Full suite left to gating.

### Test record (2026-11-07, loop 2 review fixes)

- `uv run pytest -q tests/test_idea_ordering.py tests/test_queue_conflict_resolve.py tests/test_agentic_quiet.py` — 192 passed.
- `python3 tools/spec_lint.py --write-ledger` && `python3 tools/spec_lint.py` — passed.
- Full suite left to gating.

### Test record (2026-11-07, loop 3 implementer)

- `uv run pytest -q tests/test_idea_ordering.py tests/test_queue_conflict_resolve.py tests/test_agentic_quiet.py tests/test_agentic_spec_drift.py` — 240 passed.
- `python3 tools/spec_lint.py --write-ledger` && `python3 tools/spec_lint.py` — passed.
- Full suite left to gating.

### Test record (2026-11-08, loop 3 review fixes)

- `uv run pytest -q tests/test_idea_ordering.py tests/test_queue_conflict_resolve.py tests/test_agentic_quiet.py` — 205 passed.
- `python3 tools/spec_lint.py --write-ledger` && `python3 tools/spec_lint.py` — passed.
- Full suite left to gating.

### Test record (2026-11-07, loop 1 implementer review fixes)

- `uv run pytest -q tests/test_queue_conflict_resolve.py tests/test_idea_ordering.py tests/test_agentic_quiet.py tests/test_agentic_spec_drift.py` — passed.
- `python3 tools/spec_lint.py --write-ledger` && `python3 tools/spec_lint.py` — passed.
- Full suite left to gating.

### Test record (2026-11-07, loop 2 implementer)

- `uv run pytest -q tests/test_idea_ordering.py tests/test_queue_conflict_resolve.py tests/test_agentic_quiet.py` — 214 passed.
- `python3 tools/spec_lint.py --write-ledger` && `python3 tools/spec_lint.py` — passed.
- Full suite left to gating.

### Test record (2026-11-07, loop 1 implementer)

- `uv run pytest -q tests/test_idea_ordering.py tests/test_queue_conflict_resolve.py tests/test_agentic_quiet.py` — passed.
- `python3 tools/spec_lint.py --write-ledger` && `python3 tools/spec_lint.py` — passed.
- Full suite left to gating.

### Test record (2026-11-07, loop 0 implementer)

- `uv run pytest -q tests/test_idea_ordering.py tests/test_queue_conflict_resolve.py tests/test_agentic_quiet.py` — passed.
- `python3 tools/spec_lint.py --write-ledger` && `python3 tools/spec_lint.py` — passed.
- Full suite left to gating.

### As shipped

- Renumbered to **SPEC-0463** so **SPEC-0458** on `main` stays the tail-cards spec.
- `dependency_wait` holds the full campaign-wait shape for every state (`(\S+)` backreference): `HANDED_OFF` without a live id, other states when that full campaign id is live (fixes minted ids such as `MERGE-…`, `3M-…`, `FIX-…`).
- Worktree adopt: `spec_at_open.md` is kept only when it is a regular file under the campaign dir
  and its bytes match `sha256_at_open`; otherwise it is rewritten from the worktree spec via
  `O_CREAT|O_EXCL` temp + `os.replace` (fresh open uses the same path). A failed write clears
  the hash and parks; partial or mismatched copies are not drift baselines.
- `worktree_resume_state`: `admission == "revise_pending"` with `accepted`/`in-progress` resumes to `SPEC_WRITING`; `after_revision` with a pending brief still resumes to `IMPLEMENTING`.

## Clock Log

No implementation session is clocked here. Implementation clocks IDEA-1024 with `nw idea clock-in` and `nw idea clock-out`.

## Rollout / migration

1. Land the `after_met` corrections, the quiet prefix and the `HANDED_OFF` hold, the tick pass, and the open adoption together. They are one change.
2. No migration and no rewrite of existing campaign files. The next tick recomputes `blocked_by`. A waiter of an idea with no campaign gets one mail, and `conflict_mail` is on the dict `_record_backlog` writes. A QUEUED campaign is adopted only when it is admitted, so one that is still blocked keeps its worktree untouched until then. Merge, split, revert, and red-main opens are unchanged.
3. Confirm a campaign without `after`, and a fresh open whose path does not exist, follow the current open target.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None._
