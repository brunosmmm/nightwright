---
id: SPEC-0547
title: main_dirty is conductor-owned; the skill row plans and escalates
status: accepted
owner: user
created: 2026-11-11
updated: 2026-11-11
kind: feature
tags: [agentic, conductor, queue]
depends_on: [SPEC-0546]
supersedes: [SPEC-0542]
source_idea: IDEA-1159
---

## Context

[SPEC-0222](./0222-queue-watch-needs-you-vs-conductor-handling.md) makes `main_dirty`
human-owned (`PARK_OWNERS["main_dirty"] = "human"`), so it is never overdue, `park_undecided`
never fires, the conductor ping fingerprint skips it, and herdr stays silent. On the night of
IDEA-1159 one stray tracked edit sat in **Needs you** and blocked every close: zero deliveries.

This is the second of six specs that replace [SPEC-0537](./0537-conductor-must-clear-main-dirty-itself-a-stray.md)
(human chose "split / narrow" at ESC-0067) and split
[SPEC-0542](./0542-main-dirty-plan-and-escalate.md) per its spec review. The chain, in queue
order: [SPEC-0546](./0546-main-dirty-plan-classifier.md) (classifier), **SPEC-0547 (this
spec)**, [SPEC-0548](./0548-main-dirty-apply-core-commit.md) (verb core),
[SPEC-0549](./0549-main-dirty-apply-wip-save-and-hold.md) (verb WIP save),
[SPEC-0550](./0550-conductor-git-guard-hook.md) (git-guard hook),
[SPEC-0551](./0551-conductor-runs-main-dirty-apply-trivial-only.md) (conductor trivial-only).

With SPEC-0546's `nw queue main-dirty-plan` in place, this spec flips the owner so the
conductor is woken, and gives it a row that plans and escalates once with the exact commands.
No git write by anyone is added here.

**Carries SPEC-0542 AC1, AC2, AC3, AC4, AC13, AC14, AC15.** It also folds in the SPEC-0542
review's row findings: the plan is run with the campaign's base, a plan that exits non-zero
still leads to a recorded decision, the open-escalation check matches a `root:` fact (not a
substring of the question), facts are single-quoted, and AC5 pins literal phrases.

## Decision

Drop `main_dirty` from `queue.PARK_OWNERS`. The conductor's new `parked.main_dirty` row runs
`nw queue main-dirty-plan` and either resumes (`clean` on the base) or escalates
`human_checkout` with no campaign, once per root, then resumes on the wake after the checkout
is clean. A plan failure is escalated the same way.

## Design

### Ownership and wake

`PARK_OWNERS` keeps `main_not_on_base`, `human_decision`, `restart`; `park_owner` defaults to
`conductor`. Existing paths pick the owner up with no new branch: `quiet.ping_fingerprint`,
`quiet.classify` (a runner-emitted `main_dirty` park is `wake`), `herdr_watch.notification_for`,
`dashboard_payload` (Conductor handling), `mvp_report.e5`. Delete the `main_dirty` entry of
`dashboard/inbox.py` `HOST_FIX`; keep `main_not_on_base`.

### Skill (`skills/nw-conduct/SKILL.md`)

The combined `parked.main_dirty, parked.main_not_on_base` row is split. The
`parked.main_not_on_base` row is today's combined row verbatim, with `` `parked.main_dirty`, ``
removed from the first column and `` `main_dirty` / `` removed from the second; its body (third
column) is unchanged. The `wt_broken` row
stops saying "same as `main_dirty`" and spells out its current move procedure. The "Clear
blockers yourself" bullet names `parked.main_dirty` as the exception for a dirty main checkout
(plan and escalate, never clear).

The `escalation_answered` row gains one sentence at its start: "A record with
`topic: human_checkout` and no campaign (read it with `nw escalations show <id>`) is a
`main_dirty` answer: run `parked.main_dirty` step 5, nothing else." Its campaign-based steps do
not apply to such a record, because the event `data` carries no topic and no campaign.

The new `parked.main_dirty` row, triggered by `parked` `main_dirty`, and by a
`conductor_ping` / `park_undecided` / `escalation_answered` that concerns an undecided
`main_dirty` park or its escalation:

**Quoting rule (one rule for the whole row).** Every value substituted into a command in this
row (`--root`, `--base`, `--question`, `--option`, `--fact`, `--decision`) is a single-quoted
shell string with each `'` written as `'\''`; the commands below are written with single-quoted
placeholders (`--root '<root>'`, `--question '<question>'`). Campaign ids (`<id>`) and ESC ids
are plain tokens and are not quoted.

1. Find the campaigns with `nw queue list --json` (campaigns PARKED with `park.reason`
   `main_dirty`). For each, read `repo.root` and `repo.base` from `nw campaign show <id> --json`;
   group by (`root`, `base`). Per group run
   `nw queue main-dirty-plan --root '<root>' --base '<base>' --json` in the conductor's own
   context (read-only, no sub-agent). **If it exits non-zero**, escalate once per root (step 3's
   check first) with `--question 'Cannot plan the main checkout <root>: <first stderr line>'`,
   options `I fixed it` / `I cleared it another way`, `--fact 'root: <root>'`, then step 4's
   decisions; the answer re-runs step 1.
2. `action: resume` (clean and on the base): for each campaign of that group still PARKED
   `main_dirty`, `nw queue resume <id> --to CLOSING --by conductor --decision 'main checkout
   clean' --rule parked.main_dirty --yes`. Done.
3. Otherwise, if `nw escalations list --json` already has an open `human_checkout` record whose
   facts contain exactly `root: <root>`, do not escalate again; record
   `nw queue decision <id> --kind wait --rule parked.main_dirty 'waiting on ESC-NNNN'` once per
   campaign that has no decision after its park.
4. Else escalate once, no campaign:
   `nw queue escalate --topic human_checkout --question '<question>' --option 'I ran the recommended commands' --option 'I cleared it another way' --recommend 1 --fact 'root: <root>' --fact 'class: <class>, action: <action>' --fact '<command>' --yes`.
   The `--fact '<command>'` clause stands for a variable number of facts, each quoted by the
   rule above: one per `recommended` line (none for `class: merge`, whose `recommended` is
   `[]`), then one per path in `paths`, for the first 10 paths. Paths beyond the first 10 are
   summarised in one fact `<m> more paths (see nw queue main-dirty-plan)`. A path that contains
   a newline is not listed (a fact is one line) and counts toward `<m>`; a path with `$`, a
   backtick or `"` is listed like any other, since single-quoting keeps it literal.
   `<question>` is "Main checkout <root> has <class> tracked edits blocking closes: run the
   commands below in order, stop at the first error, then answer", with three variants:
   - `class: clean` with `action: hold`: "Main checkout <root> is not on <base>: run the
     command below, then answer".
   - `class: merge` (always `action: escalate`): "Main checkout <root> has a <in_progress> in
     progress, blocking closes: finish or abort it, then answer", no command facts, and the
     first option is `I will finish the <in_progress> in the main checkout`.
   - every other class: the question above.

   Every placeholder maps to a SPEC-0546 `nw.main_dirty_plan.v1` key (`root`, `class`,
   `action`, `in_progress`, `recommended`, `paths`) or to `base` read in step 1. Then
   `nw queue decision <id> --kind escalate --rule parked.main_dirty 'escalated ESC-NNNN'` for
   each `main_dirty` campaign of that root, so the park counts as decided and `park_undecided`
   does not repeat.
5. On `escalation_answered` for that record, run step 1 again: `resume` → step 2; still dirty →
   step 4 with an extra `--fact 'still dirty after ESC-NNNN'`. A campaign that is no longer
   PARKED `main_dirty` is skipped.

The row runs no git command other than through `main-dirty-plan`, dispatches no sub-agent, and
never names `main-dirty-apply` (SPEC-0551 later adds the `--trivial-only` step as `2b`).

**E5 blind spot.** `mvp_report.e5` counts human-owned `park` events and escalations with
`campaign == cid`. After this spec a `main_dirty` park is conductor-owned and its
`human_checkout` record has no campaign, so E5 shows no touchpoint although the human still
clears the checkout until SPEC-0551. This blind spot is accepted and not fixed here.

**What wakes a decided park.** Once the campaigns carry the `escalate` decision they leave the
ping fingerprint and `park_undecided` does not fire again; `main_dirty` has no auto-resume. The
only wake is the answer to the escalation (step 5). A checkout the human cleans without
answering leaves the campaigns parked until the human answers ESC-NNNN or runs
`nw queue resume`; the question says "then answer" for that reason. A withdrawn ESC-NNNN has
the same effect as an unanswered one (`escalation_withdrawn` reaches the conductor only in the
digest and is `hold`): the human resumes the campaigns with `nw queue resume`. This is
accepted: the open campaign-less `human_checkout` record never times out and stays visible in
`nw escalations list` and in the dashboard inbox `escalations` section until it is answered or
withdrawn (AC9), unlike the silent park this spec removes. The parks themselves are under
Conductor handling, no longer under Needs you.

### Files the implementer may cherry-pick from `nw/IDEA-1159-20261110-02ed`

An optional source (tip `61876af9`), never the contract:

- `src/nightwright/agentic/queue.py` (`PARK_OWNERS`), `src/nightwright/agentic/dashboard/inbox.py`
  (`HOST_FIX`).
- `docs/product/loop/operate.md`, `docs/product/capabilities/agentic.md`: reword to "the
  conductor classifies and escalates"; name `main-dirty-plan` only.
- Test updates in `tests/test_agentic_queue.py`, `test_agentic_events.py`,
  `test_agentic_dashboard.py`, `test_agentic_queue_cli.py`, `test_tui_queue_model.py`,
  `test_dashboard_cli.py`, `test_dashboard_inbox.py`, `test_dashboard_page.py`,
  `test_dashboard_snapshot.py`, `test_herdr.py`, `test_agentic_mvp_report.py`,
  `test_skills.py`, `test_rich_output_guard.py`.
- Not `launch.py`, not `test_agentic_launch.py`, not the branch's `SKILL.md` text.

## Alternatives considered

- **Leave `main_dirty` human-owned and only rewrite the skill row.** The fingerprint and
  `park_undecided` would still ignore a missed park, which is how the checkout sat overnight.
- **Mail the steward instead of escalating.** The edit is in the human's own checkout, which
  the skill's human-decision route reserves for the human (`human_checkout` never times out).
- **Match the open escalation by its question text.** Root `/home/u/proj` would match a
  question about `/home/u/proj2`; a dedicated `root:` fact matches exactly.

## Acceptance criteria

- [ ] AC1 (SPEC-0542 AC1) `park_owner("main_dirty") == "conductor"`; `main_not_on_base`,
      `human_decision`, `restart` stay `human`.
- [ ] AC2 (SPEC-0542 AC2) An undecided `main_dirty` park older than `park_decide_s` is overdue,
      is in Conductor handling and not in Needs you, and `events.sync` emits exactly one
      `park_undecided` for it; a decision note after the park clears the overdue flag and no
      second `park_undecided` fires.
- [ ] AC3 (SPEC-0542 AC3) `quiet.ping_fingerprint` contains `["parked", <cid>]` for a real
      `main_dirty` park written by `finish_close` (not a hand-built dict); `quiet.classify`
      returns `wake` for it.
- [ ] AC4 (SPEC-0542 AC4) Herdr emits `nw: parked` for a `main_dirty` park; `HOST_FIX` has no
      `main_dirty` key; `mvp_report.e5` on a campaign that really parked `main_dirty` has no
      touchpoint for it, while a `main_not_on_base` park still is one. This is the accepted E5
      blind spot (Design): the campaign-less `human_checkout` record is not counted as a
      touchpoint until SPEC-0551.
- [ ] AC5 (SPEC-0542 AC13) Skill: the `parked.main_dirty` row (extracted by its rule id)
      contains the literal substrings `main-dirty-plan`, `--root '<root>'`, `--base '<base>'`,
      `nw queue list --json`, `still PARKED`, `nw campaign show <id> --json`,
      `Cannot plan the main checkout`, `human_checkout`, `--yes`, `--recommend 1`,
      `recommended`, `escalation_answered`, `--to CLOSING`, `--rule parked.main_dirty`,
      `stop at the first error`, `is not on <base>`, `nw escalations list --json`,
      `root: <root>`, `nw queue decision`, `--kind escalate`, `--kind wait`,
      `still dirty after`, `more paths`, and `'\''`; it does not contain `is on <branch>`, and
      contains none of `git add`, `git commit`, `git checkout`, `git rm`, `git worktree add`,
      `main-dirty-apply`, `isolation`. The `escalation_answered` row names `human_checkout` and
      `parked.main_dirty`. The "Clear blockers yourself" bullet names `parked.main_dirty`. The
      `wt_broken` row does not contain `same as`; `parked.main_not_on_base` is its own row, and
      the test (holding the pre-change combined row as a literal fixture) asserts that row
      equals the old row with only `` `parked.main_dirty`, `` removed from the first cell and
      `` `main_dirty` / `` removed from the second, so the body is byte-identical.
- [ ] AC6 (SPEC-0542 AC14) The test takes the backticked span of the `parked.main_dirty` row that
      begins at `nw queue escalate` and ends at `--yes` (the single-line template of step 4),
      expands the `--fact '<command>'` clause itself (one `--fact` per sample `recommended` line,
      then one per sample path up to 10, then the `<m> more paths` fact), substitutes the
      `<question>`, `<root>`, `<class>` and `<action>` placeholders, appends nothing else, drops
      `--yes`, and runs the result through `CliRunner` as `nw queue escalate … --json` against a
      temp cfg (via `shlex.split`). The sample root contains a space, a `'` and a `$`; one
      sample `recommended` line and one sample stderr line (in the step-1 failure `--question`)
      contain a backtick and a `$`. The test runs three step-4 variants and asserts exit 0,
      `written: false`, `topic: human_checkout`, `campaign: null`, `recommendation: 1`:
      - `class: branch`: the row's question, two options, a `root: <root>` fact equal to the
        input byte for byte, one fact per sample `recommended` line equal to the input byte for
        byte, a path containing `$` and a path containing a space and a `'` each listed
        verbatim, and, with 12 sample paths, the 11th and 12th absent and one
        `2 more paths (see nw queue main-dirty-plan)` fact present.
      - `class: clean`, `action: hold`: the `is not on <base>` question and two options.
      - `class: merge`: the `has a <in_progress> in progress` question, two options whose first
        label is `I will finish the <in_progress> in the main checkout`, and no fact other than
        `root: …` and `class: merge, action: escalate`.
      The question and the `root:` fact round-trip byte for byte, with the `$` and backtick
      arriving unexpanded. A fourth run builds the step-1 failure command from the same span:
      `<question>` becomes `Cannot plan the main checkout <root>: <first stderr line>` (sample
      root and sample stderr line as above), the first option becomes `I fixed it`, and the
      `class:` fact, the `<command>` clause and `--recommend 1` are removed. It asserts two
      options (`I fixed it`, `I cleared it another way`), the `root: <root>` fact, and the
      question equal byte for byte to the sample text.
- [ ] AC7 (SPEC-0542 AC15) `launch.py` and `tests/test_agentic_launch.py` are unchanged by this
      spec; `finish_close` still parks `main_dirty` with `resume_to: CLOSING` and still merges
      once the checkout is tracked-clean.
- [ ] AC8 A grep test over `docs/product/loop/operate.md` and
      `docs/product/capabilities/agentic.md` finds wording that the conductor classifies and
      escalates a dirty main checkout, finds `main-dirty-plan`, and finds no `main-dirty-apply`.
- [ ] AC9 With a campaign-less open `human_checkout` record (created through the row's step-4
      command with `--yes` against a temp cfg), `nw escalations list --json` includes it and the
      dashboard inbox payload's `escalations` section includes it; after
      `nw escalations withdraw` it is gone from both, and the `main_dirty` campaigns stay
      PARKED until `nw queue resume`.

## Test plan

- **Automated tests:** the existing-module updates listed in Design (each module already has its
  tier marker); E5 uses a `main_dirty` park planted through `finish_close` (AC3, AC4). The
  skill-row tests (AC5, AC6) and the docs grep test (AC8) go in `tests/test_main_dirty_plan.py`
  (SPEC-0546's module, which already carries its one `pytestmark` tier; no second marker is
  added), the branch's `test_skill_row_phrases` rewritten to AC5 (rows extracted by rule id;
  negative phrases). The AC9 visibility check goes in `tests/test_dashboard_inbox.py`.
- **Regression guard:** `tests/test_agentic_close.py`, `tests/test_agentic_mvp_integration.py`,
  `tests/test_agentic_launch.py` pass unchanged. `python3 tools/affected_tests.py --run uv run
  pytest -q`, `python3 tools/test_tiers.py --check`, `python3 tools/spec_lint.py`.
- **Manual verification:** with a scratch queue cfg, plant a `main_dirty` park on a scratch repo
  with a frontmatter edit; run `uv run nw queue list --json` and the dashboard payload → the
  park is under Conductor handling; run the row's step-4 command with `--json` and without
  `--yes` → a dry-run `human_checkout` record whose facts list the `recommended` lines. Record
  the output in As shipped.

## Goals / Non-goals

**Goals.** A `main_dirty` park wakes the conductor, which plans once and escalates once per
root with the exact commands.

**Non-goals.**

- The classifier and `nw queue main-dirty-plan` (SPEC-0546).
- Any git write by the conductor or a sub-agent for `main_dirty`; no sub-agent is dispatched
  for it. The write verb is SPEC-0548/0549; conductor writes are SPEC-0551.
- `launch.py` allow or deny lists and `CONDUCTOR_CURSOR_POLICY`.
- `main_not_on_base`, `human_decision`, `restart` stay human-owned; the
  `parked.main_not_on_base` row keeps its current procedure (SPEC-0551 changes it).
- `finish_close` is unchanged.

## Rollout / migration

No data move. The owner change reclassifies existing `main_dirty` parks on the next read; an
old one with no decision emits one `park_undecided`, which wakes the conductor to plan and
escalate. Once decided, the only wake for a `main_dirty` park is the answer to its escalation
(Design, "What wakes a decided park"). This spec removes the silent park, not the human step:
every class, trivial included, still needs the human until SPEC-0551 lands, and IDEA-1159 stays
open until SPEC-0551 is done. Queued under project Meta-Tools after SPEC-0546.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed, including the manual check; affected tests pass.
- [ ] No regressions in close, dashboard, herdr, launch, E5 tests.
- [ ] `docs/product/loop/operate.md` and `docs/product/capabilities/agentic.md` say the
      conductor classifies and escalates, and name only `main-dirty-plan` (AC8).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
