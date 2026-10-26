---
id: SPEC-0239
title: "Grouped review follow-ups: one idea per campaign, and a migration for the singles"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, review, followups, ideas, migration]
source_idea: IDEA-198
depends_on: [SPEC-0207]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), promoted
from `IDEA-198` (kind bug).

[SPEC-0207](./0207-automatic-review-followups.md) files every non-blocking review finding of a
merged campaign as its **own** idea (`followups.file_followups` → one `org_write.add_idea` per
finding, headline `wtfu:<key12> [SPEC-NNNN] persona/slot: title — detail (files: …)`). Between
18:51 and 20:15 on 2026-10-25 it created `IDEA-105`..`IDEA-197`, mostly `test_reviewer` minors.
The result:
- the ideas file is unreadable, and `wt ideas` / `wt hub` are dominated by follow-ups;
- each idea is far too small to become a campaign on its own;
- findings of one campaign are scattered across many ideas, so nobody can triage them together.

The `followups_nits` profile flag already exists and defaults to false in the three builtin
profiles, so nits are already off; the flood comes from the one-idea-per-finding shape, not
from nits.

## Goals / Non-goals

**Goals**
- G1. The close-step filer writes **one** idea per campaign. Its title says how many findings it
  holds, and its Summary is a checklist with one line per finding.
- G2. A finding (by its SPEC-0207 `key`) is listed once: across the review loops of a campaign
  (already the case through `review_carry`), and across campaigns of the same spec whose grouped
  idea is still open.
- G3. A per-campaign cap (`review.followups_max`, default 10), so one noisy review cannot write
  a 40-line idea. Nits stay off by default.
- G4. Crash-safe and idempotent, like SPEC-0207: re-running the close step never duplicates an
  idea or a checklist line, and filing never blocks a merge.
- G5. `wt ideas group-followups [--yes] [--json]` is a one-off migration. It folds the existing
  open single `wtfu:` ideas into one grouped idea per spec, and closes the singles as `DROPPED`
  with `FOLDED_INTO` pointing at the group.

**Non-goals**
- Changing what is considered or filed (the SPEC-0207 considered set, severities, carry-over,
  `key`). Only the shape of the output changes.
- Deduplicating against ideas this filer did not create, or against closed grouped ideas. A
  finding listed in a group the human already closed is filed again by a later campaign.
  Accepted: the human closed the group, maybe without fixing that finding.
- Rewriting the `followup` events of past campaigns. They keep pointing at the single ideas,
  and each single's `FOLDED_INTO` leads to its group.
- Automatically turning a grouped idea into a spec or a campaign.

## Decision

`followups._file` collects the findings to file, drops those already listed in an open
grouped idea of the same spec, sorts and caps the rest, and writes them into one idea per
campaign. The idea is found by a new heading marker `wtfug:<campaign id>` or created. Its
Summary is the checklist and its title carries the count. Every filed finding still gets its
SPEC-0207 `followup` ledger event, whose `idea_id` is now the group's id. The migration lives in
the same module (`group_legacy`) behind a new `wt ideas group-followups` command.

## Design

### Shapes

- **Group marker:** `GROUP_MARKER = "wtfug:"`. It is distinct from SPEC-0207's `MARKER = "wtfu:"`
  (`"wtfug:"` does not contain `"wtfu:"`), so `find_marker(cfg, "wtfu:<key12>")` can never hit a
  group. A campaign's group token is its campaign id (for example
  `wtfug:SPEC-0230-20261025-6126`). A migrated group's token is `legacy-<SPEC-NNNN>`.
- **Title:** `group_title(token, spec_id, n)` returns `wtfug:<token> [<spec_id>] review follow-ups
  (<n> findings)`, or `(1 finding)` when n is 1. `n` counts the checklist lines only, not the
  overflow line.
- **Checklist line:** `checklist_line(entry)` returns `- [ ] wtfu:<key12> <severity>
  <persona>/<slot>: <title> — <detail> (files: …)`. It reuses `idea_text`'s rules (`clean`,
  `TITLE_MAX`, `DETAIL_MAX`, `SLOT_MAX`, `FILES_MAX`): the line is `idea_text(spec_id, entry)`
  with the `[<spec_id>] ` part removed and the severity inserted after the per-finding marker.
  Each line keeps the per-finding marker `wtfu:<key12>`. A body line is not a heading, so
  `find_marker` (headings only) never matches it. The line is the dedup handle.
- **Overflow line** (only when the cap cut findings): `- +<K> more findings not listed (wt
  campaign show <campaign id>)`.
- The idea: `kind="improvement"`, `project=state["project"]`, `epic=parent_epic(state)`, as in
  SPEC-0207.

### Profile key

- `profiles.REVIEW_KEYS` gains `followups_max`.
- The three builtin profiles set `followups_max: 10`. If [SPEC-0237](./0237-workflow-profiles-as-yaml-files-builtin-user-project.md)
  has landed, the builtin YAML files set it instead.
- `_check_review` raises `ValueError` naming `review.followups_max` unless it is an integer
  (not a bool) and `>= 0`. `0` means no cap.
- A new `followup_cap(profile) -> int` returns the value, or 10 when it is absent.
  `followup_params` is unchanged.
- `followups_nits` keeps its default `false` everywhere. A test asserts that each builtin
  profile resolves to `followups_nits: false`, so the default stays a regression guard.

### Filing (`followups._file`, rewritten)

The considered set, `enabled`/`nits` and the `waived` summary are computed as today. Then:

1. **Already filed in this campaign:** for each finding whose `key` has a `followup` event in
   the ledger, report it in `filed` with that event's `idea_id`, as today, and set it aside.
2. **Listed in another open group of the same spec:** `open_group_keys(cfg, spec_id) -> dict[key12,
   idea_id]`.
   - It loads the ideas through the org reader, from the ideas file and its archive, the same
     two paths `find_marker` uses.
   - It keeps ideas in an active state whose heading contains `GROUP_MARKER` and `[<spec_id>]`.
   - It maps every `wtfu:<key12>` token of each idea's Summary to that idea's `:ID:`. The
     Summary is read with `explore.read_idea_enrichment(cfg, task)["summary"]`.
   A finding whose `key12` is in that map gets a `followup` event with that `idea_id` and is
   reported in `filed`. No line is written for it.
3. **Sort and cap:** order the remaining findings by severity rank (`major`, `minor`, `nit`),
   then `advisory` before `waived`, then title. With cap `C > 0`:
   - Count `L`, the lines already in this campaign's group. `L` is 0 when the group does not
     exist yet.
   - At most `max(0, C - L)` findings get a line.
   - The rest go to the summary's new `capped` list (`[{severity, title}]`) and get **no**
     `followup` event, so a later re-run with a raised cap can still list them.
4. **Write the group** (only when step 3 kept at least one finding):
   - `idea_id = find_marker(cfg, "wtfug:<campaign id>")`. If it is None, create the idea with
     `OW.add_idea(cfg, group_title(cid, spec_id, 0), …)`. If the returned headline lacks the
     group marker, add a `note` event, as SPEC-0207 does for the single marker.
   - Read the group's Summary and append the new lines whose `wtfu:<key12>` is not in it yet,
     followed by the overflow line, which replaces any previous overflow line. Write the
     Summary with `explore.set_summary`.
   - `explore.retitle_idea` sets the title to `group_title(cid, spec_id, n)`, where `n` is the
     number of checklist lines now in the Summary.
   - Each newly listed finding then gets its `followup` event (`idea_id` = the group) and its
     `filed` entry.
5. Any exception in step 4 makes every finding of that step `unfiled` (with the error) and adds
   a `note` event, with no `followup` event. The outer `except Exception` of `file_followups`
   stays, so the filer still never blocks `close_campaign`.

Crash safety: the writes happen in the order add_idea → set_summary → retitle → events (the
events are returned and appended by `_apply`). A crash anywhere before the events leaves a group
the re-run **adopts** by its marker. It then appends only the lines that are missing and
recounts the title. So a re-run writes no second idea and no second line.

Summary shape: `{filed, unfiled, waived, capped, idea_id}`. `idea_id` is this campaign's group,
or None when every finding was already listed elsewhere or nothing was filed. These are
additive keys on `state.close.followups`, and on the `wt.queue.tick.v1` close row's
`followups` (which also gains `capped` as a count).

### Reporting

- `wt campaign show` prints `follow-ups IDEA-NNN (<n> listed, <k> capped)` before the existing
  per-finding lines when `idea_id` is set, and a `[capped] <severity> <title>` line per capped
  finding.
- The existing `follow-up IDEA-NNN severity title` lines stay. They now all carry the group's
  id, or the id of the other open group that already lists the finding.

### Migration: `wt ideas group-followups [--yes] [--json]`

`followups.group_legacy(cfg, *, apply: bool) -> dict` does the work. The command is registered
on `ideas_group` in `cli.py`.

- **Singles:** every idea in an active (not done) state whose heading matches
  `^wtfu:([0-9a-f]{12}) \[(SPEC-\d{4})\] (.+)$`. This matches the live `IDEA-105`..`IDEA-197`;
  the number range is not hard-coded. Closed singles are ignored.
- **Plan:** singles are grouped by spec, ordered by idea number. For each spec the plan has:
  - the singles' ids;
  - the target, an existing open group with marker `wtfug:legacy-<SPEC-NNNN>` (found with
    `find_marker`) or `new`;
  - the lines to add, one per distinct `key12`. A line is `- [ ] wtfu:<key12> <rest of
    heading> (was IDEA-NNN)`. When two singles share a key12, one line is written and both
    singles fold into the group.
- **Dry run** (no `--yes`):
  - The text output prints one row per spec: `SPEC-NNNN  <k> singles (IDEA-105, …)  → new |
    IDEA-NNN`. It ends with the totals and the hint `re-run with --yes`.
  - `--json` emits `wt.ideas.group_followups.v1` with `written: false` and `groups: [{spec_id,
    singles, target, lines}]`.
  - Nothing is written.
- **Apply** (`--yes`), per spec:
  1. Create the group when the target is `new`. It uses `kind="improvement"`, and the
     `:PROJECT:` / `:EPIC:` of the spec's lowest-numbered single (none when absent).
  2. Append the missing lines with `set_summary` and retitle it with `group_title`, the same way
     the filer does.
  3. Close each single with `explore.close_idea(cfg, id, "drop", reason="merged into grouped
     review follow-ups", folded_into=<group id>)`.
  The output is the same report with the group ids and `written: true`.
- **Idempotent:** a re-run finds no open singles and reports nothing to do. After a crash
  mid-spec, the re-run adopts the existing legacy group by its marker, adds only the missing
  lines, and closes the remaining singles.

### Docs

`docs/product/capabilities/agentic.md`: the follow-ups section describes the grouped idea, the
`wtfug:` marker, `review.followups_max`, and the migration command.

## Alternatives considered

- **One idea per spec instead of per campaign.** Rejected for new filings: a campaign is the
  unit that produced the findings and the unit `wt campaign show` reports. Cross-campaign dedup
  against open groups of the same spec gives the same "listed once" property. The migration
  does group per spec, because a single carries its spec in the heading but not its campaign.
- **Keep singles and only hide them from `wt ideas`.** Rejected: they would still not be
  campaign-sized, and the ideas file would keep growing.
- **Cap by dropping findings silently.** Rejected: capped findings are reported in the summary
  and in `campaign show`, and the overflow line in the idea points at them.
- **Tags or a property instead of a heading marker.** Rejected: SPEC-0207's adoption already
  relies on a heading substring that `add_task` leaves untouched. A second marker of the same
  kind reuses `find_marker` unchanged.

## Acceptance criteria

- [ ] AC1. `review.followups_max` is accepted and validated: an integer `>= 0` (not a bool);
      any other value raises `ValueError` naming the key. `followup_cap` returns 10 when the key
      is absent. Every builtin profile has `followups_max: 10` and `followups_nits: false`.
- [ ] AC2. A merged campaign with several major/minor findings produces **one** idea: kind
      improvement, the campaign's project and parent epic, titled
      `wtfug:<cid> [<spec>] review follow-ups (<n> findings)`. Its Summary has one
      `- [ ] wtfu:<key12> <severity> …` line per finding, ordered by severity, `advisory`
      before `waived`, then title. Each finding has a `followup` event whose `idea_id` is the
      group.
- [ ] AC3. With `followups_max: 2` and five findings, two lines and an overflow line
      `- +3 more findings not listed (wt campaign show <cid>)` are written. The summary's
      `capped` lists three findings, which get no `followup` event. With `0`, all five are
      listed.
- [ ] AC4. A finding whose key12 already appears in another **open** group of the same spec
      (including a `legacy-` group) gets no new line. Its `followup` event and `filed` entry
      carry that group's id. When every finding is listed elsewhere, no new idea is created and
      `idea_id` is None. A closed group does not suppress filing.
- [ ] AC5. Idempotency: running the filer twice writes one idea and no duplicate line.
      Simulated crashes after `add_idea`, and after `set_summary` before the events, are
      recovered by adoption through the `wtfug:` marker, with the right count in the title. An
      exception from `set_summary` reports those findings `unfiled` with a `note` event, and
      `close_campaign` still runs.
- [ ] AC6. `wt campaign show` prints the `follow-ups IDEA-NNN (<n> listed, <k> capped)` line
      and the `[capped]` lines. The `wt.queue.tick.v1` close row's `followups` carries
      `idea_id` and `capped`.
- [ ] AC7. `wt ideas group-followups` (dry run) lists one group per spec with its singles and
      target and writes nothing, and `--json` emits `wt.ideas.group_followups.v1` with
      `written: false`. With `--yes` it creates one grouped idea per spec (one line per
      distinct key12, project/epic from the lowest-numbered single) and closes each single as
      `DROPPED` with `CLOSE_REASON` and `FOLDED_INTO` set to the group. Closed singles are
      untouched. A second `--yes` run is a no-op. A run after a crash mid-spec adopts the
      legacy group and adds no duplicate line.
- [ ] AC8. `docs/product/capabilities/agentic.md` documents the grouped idea, `wtfug:`,
      `review.followups_max` and the migration (grep test). Full `uv run pytest` is green and
      `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_profiles.py`: `followups_max` validation (bool, negative, string
    rejected; 0 accepted), `followup_cap` default, builtin values incl. `followups_nits: false`.
  - `tests/test_agentic_review_triage.py`, with a temp org data dir and the real `add_idea`,
    `set_summary` and `retitle_idea`:
    - `group_title` / `checklist_line` shapes, and the bounds inherited from `idea_text`;
    - one idea per campaign, plus ordering and the `followup` events (AC2);
    - the cap and overflow line, with `capped` and no events for capped findings (AC3);
    - `open_group_keys` across ideas file and archive, open vs closed groups, and
      `legacy-` groups (AC4);
    - re-run, the crash after `add_idea` (monkeypatch `set_summary` to raise once, re-run),
      the crash before events, and the `set_summary` exception path (AC5).
  - `tests/test_agentic_queue.py`: `campaign show` follow-up lines, `state.close.followups`
    keys, and the close row `idea_id` / `capped` (AC6).
  - `tests/test_agentic_e2e.py`: the SPEC-0207 AC7 paths (b)–(d) updated to assert one grouped
    idea instead of singles.
  - `tests/test_ideas_group_followups.py` (new), seeding an ideas file with singles in the live
    heading shape (two specs, a shared key12, one closed single) (AC7):
    - dry run text and JSON;
    - `--yes` result read back (states, `CLOSE_REASON`, `FOLDED_INTO`, Summary lines, title
      count, project/epic);
    - the second run as a no-op;
    - a crash between the group creation and the closes.
  - A docs grep test for AC8.
- **Manual verification:** copy the live ideas file to a scratch data dir and run `uv run wt
  ideas group-followups` there (dry run), then `--yes`. Record the number of singles and groups
  and spot-check one group with `wt idea show`. Then run the migration once on the live data dir
  and record the result here.
- **Regression guard:** full `uv run pytest`. The SPEC-0207 idempotency, carry-over and
  never-block tests must pass with their expectations moved from singles to the group.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

1. Ship the filer change. The next close step writes grouped ideas, and no data migration is
   needed for it.
2. Run `wt ideas group-followups` (dry run) on the live data dir, check it, then run it with
   `--yes`. The singles become `DROPPED` with `FOLDED_INTO`. Nothing is deleted, and the archive
   rotation handles them like any other closed idea.
3. Restart the running loop to load the code.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
