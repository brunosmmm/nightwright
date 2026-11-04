---
id: SPEC-0419
title: "Self-healing queue"
status: done
owner: user
created: 2026-11-04
source_idea: IDEA-921
kind: epic
milestone: "M9: agentic workflow"
tags: [agentic, queue, sentinel, cost, conductor, steward]
---

## Context

The human's complaint (2026-11-04): "I should not need to babysit." A four-agent read-only audit
of the live queue the same day found six places where the queue stops and waits for the human,
or does the wrong thing until the human notices. Each was checked against the code for this spec
(file:line as of `91d4811e`).

1. **Red main has no owner.** `sentinel.settle()` (`src/nightwright/agentic/sentinel.py:665-702`)
   ends in `_escalate()` (`:705-725`), which files a campaign-less escalation to the human with
   the options "fix forward on the base / revert the suspect merges / clear the flag" and no
   recommendation. `_repeat_red()` (`:654-662`) de-duplicates by tested sha only, so each new
   merge onto the red base files a new escalation: ESC-0007..0012, 0015..0018, 0024 and 0025 are
   all "Sentinel red on Meta-Tools". The skill's decision table (`skills/nw-conduct/SKILL.md`) has
   no sentinel row. There is no fix-forward campaign kind and no revert tool. A red sentinel only
   sets `force_full_close`, which turns off close-tier reuse and holds nothing: merges keep
   landing on the red base. There were 5 red episodes in 3 days, about 25 h red in total, and
   every fix was made by hand in a human-assistant session (`efbadd9d`, `cd75e6d6`, `91d4811e`,
   `5cb028d2`, `be726ec2`, …). In the latest episode the fix `cd75e6d6` was ready at 04:12 and
   landed at 08:51, because it waited on "the human's OK" (conductor decision #490).
2. **The close gate skips the tree-scanning guard tests.** Since SPEC-0378 the close `pytest`
   gate runs `tools/affected_tests.py --include <2 smoke files>` (`.nw/project.yaml`). Guards
   that scan the whole tests or src tree, such as `tests/test_test_waits.py`,
   `tests/test_no_textual_skips.py` and `tests/test_rich_output_guard.py`, import nothing the
   branch changes, so they are never selected. `_referencing()` (`tools/affected_tests.py:422-438`)
   adds directory readers only in `wide` (closure) mode, and never for a top-level `tests/` file.
   A branch cut before a guard landed therefore merges green and turns main red. Two cases:
   SPEC-0410's `importorskip` against SPEC-0409's guard, and dashboard tests using `.join(N)`
   against the SPEC-0269 guard.
3. **Cost caps.** A mailed "human order" (`conductor:155`, 2026-11-02) said "cap raises are the
   human's call" and overrode the table's 2× rule (`SKILL.md` row `parked.cost_cap`).
   `_cap_blocks()` (`queue.py:167-186`) reserves the persona's full `max_budget_usd` ($4
   implementer on balanced, $10 on thorough). Observed implementer runs since 2026-10-28 have a
   median cost of $0.52 and a p90 of $2.22 (413 runs). Balanced campaigns therefore park at about
   $12 of their $15 cap. The result was 12 cost escalations and parks of 5 to 11 h.
   `autopark.py:137-157` handles only phantom charges. **Decided by the human** (`conductor:238`,
   2026-11-04): raise the cap automatically while the campaign is converging, up to 3× the
   profile cap. Above that ceiling, or when the campaign diverges, escalate once. The rule lives
   in config and the decision table, not in mail. The reserve uses observed run costs.
4. **Epic children queued as ideas skip dependency checks.** `backlog_readiness()`
   (`queue.py:802-859`) treats a spec-less idea campaign as ready as soon as its idea is open.
   `spec_write.py` wires `depends_on` only in split mode (`_split_problems`, `:152-168`). In create
   mode (`_front_matter_problems`, `:112-132`) the writer is left to add it, and it cannot cite
   sibling specs that are not written yet. This produced the `implementer_blocked` and
   `spec_write_failed` parks of IDEA-874..877 and 884..889 (IDEA-880).
5. **Stale or permissive agent launches.** The live conductor was running a launch from before
   SPEC-0382, without `--permission-mode dontAsk`. Claude Code's auto-mode classifier blocked
   `nw queue merge` twice and `nw queue stop` once, despite the `Bash(nw queue *)` allow rules.
   `nw doctor` did not flag the stale launch. `nw queue steward-launch` still passes no
   `--permission-mode` at all (`launch.py:278`; confirmed with `nw queue steward-launch Meta-Tools
   --dry-run` on 2026-11-04), so stewards run in auto mode.
6. **Escalations never time out, and ledger attribution is noisy.** An escalation with a
   recommendation waits for the human indefinitely (`SKILL.md` row `parked.human_decision`: "Do
   nothing"). **Decided by the human:** after 30 min with no answer, the conductor applies its own
   recommendation, records it, and leaves it visible and reversible. Escalations without a
   recommendation keep waiting. `nw campaign note` defaults to `by: human`
   (`cli_campaign.py:413-415`). Persona runs set only `NW_PERSONA_RUN=1`, so their notes are
   attributed to the human: 75 `by: human` notes since 2026-11-01, mostly implementer prose.

One spec cannot carry this. The six fixes touch different modules, each needs its own real
verification, and most can be built in parallel.

## Goals / Non-goals

**Goals**
- A red base is owned by the queue. It opens a fix-forward campaign, falls back to a revert,
  holds other merges until the base is green, and escalates at most once per red episode.
- Guard tests that scan the tree run at every close and in every fix loop.
- Cost-cap parks on converging campaigns below 3× the profile cap never reach the human or the
  conductor.
- Epic children queued as ideas start in dependency order, and their specs cite their
  prerequisites.
- Every conductor and steward launch runs `dontAsk` with an allow-list that covers its job. A
  stale launch is detected and a relaunch is requested.
- An escalation that has a recommendation and is not human-only resolves itself 30 min after it
  was filed. Ledger notes name their real author.

**Non-goals**
- Faster red detection (sentinel cadence, `agentic.sentinel.interval_hours`). This is unchanged;
  child 1 only re-runs the sentinel promptly after a fix merges.
- Reviewing fix-forward diffs with LLM reviewers. Fix-forward is gated by the full suite and a
  weakening check instead (child 1 Design).
- Rewriting past ledger entries. Attribution changes apply to new notes only.
- Changing what the human must still decide. That list is below and in every child that touches
  it.

## Decision

Build six independent children under one invariant. The queue resolves routine failures by
itself and records each decision (`by: queue` / `by: conductor`). It escalates to the human only
for decisions on the human-only list, and then once per situation.

**Human-only (never automated, never timed out):**
- scope and product decisions (what gets built, splitting or narrowing a spec, priorities
  between features);
- spend above the 3× ceiling, or any cap raise on a diverging campaign;
- waivers or attestations of manual acceptance criteria that concern the human's own machines;
- `git push origin main` (or any push to the base);
- agents running `gh pr merge`;
- anything touching the human's own checkout or their settings (`~/.claude/settings.json`,
  `~/.config/nightwright/config.yaml`). Settings change only through `nw skills install`
  (SPEC-0320 `agent_permissions.sync`), which the human runs.

**Decided here:** the conductor (and a steward, through the conductor) may land a branch that
fixes a red base through the gated merge, `nw queue merge <branch> --by conductor --fixes-red
--yes`, without asking the human. That path rebases, runs the **full** suite and merges ff-only.
It is the same safety bar as any queue merge, raised to the full suite. Waiting on the human's OK
cost 4.5 h in the last episode.

## Architecture / cross-cutting design

- **Config.** New keys, each validated with fall-back-to-default the way `autopark.settings` and
  `divergence.settings` already do:
  - `agentic.red_main` (child 1);
  - `agentic.cost_cap_policy` (child 3);
  - `agentic.escalations.auto_apply_after_s` (child 6);
  - `agentic.conductor.launch.steward_permission_mode` (child 5).

  No key is ever written by an agent.
- **Escalation records** (`escalations.py`, SPEC-0232) gain a `topic` field (child 6). Queue-filed
  escalations set it in code:
  - red main → `recovery` (child 1);
  - above ceiling → `spend_ceiling`, diverging → `spend_diverging` (child 3).

  Human-only topics never time out. Records without a topic keep today's behaviour (no timeout)
  unless their `kind` is a recovery kind (child 6).
- **Events.** Every new event type has an entry in `quiet.RULES` and a row in
  `skills/nw-conduct/SKILL.md`. The existing test that compares the skill's quiet paragraph with
  `quiet.RULES` keeps them in step. Children that only inform are "always held". Children that
  need the conductor wake it.
- **Decision records.** Automatic actions write `nw queue decision`-shaped records with `by:
  queue` and a rule id (as `autopark` does, SPEC-0302). A compacted conductor sees them in
  `conductor-brief`.
- **Full-suite campaigns.** A campaign state flag `full_suite: true` makes `gates.gate_env` and the
  remote gate env export `NW_AFFECTED_FULL=1`, so `tools/affected_tests.py` runs the full suite
  (child 1). Child 2's guard registry is orthogonal: guards are part of every non-full selection.
- **File ownership** (for parallel building; see Rollout):
  - child 1: `sentinel.py`, the new `redmain.py`, `close.py`, `gates.py`;
  - child 2: `tools/affected_tests.py`, `.nw/project.yaml`, test guard markers;
  - child 3: `autopark.py`, the new `cost_policy.py`, `queue._cap_blocks`;
  - child 4: `queue.backlog_readiness` / `expand_epic` / batch add, `spec_write.py`, `specs.py`;
  - child 5: `launch.py`, `doctor.py`;
  - child 6: `escalations.py`, `cli_escalations.py`, `cli_campaign.py`.

  `queue.py`, `quiet.py` and the two skill files are shared. Each child edits different
  functions and rows there, so expect small textual merges and no semantic overlap.

## Breakdown / sub-specs

- [x] Red-main fix-forward and revert (project: Meta-Tools) — sentinel decision row; `fixforward` campaign kind opened by the queue on a red sentinel (priority 1, failing ids + suspects + gate log tails as the brief, full-suite gated, weakening check); revert of the localized suspect merge after N failed attempts; non-fix merges held while red; one escalation per red episode (failing-test-set fingerprint), auto-withdrawn on green; escalate only when red persists past a threshold with no fix in flight; conductor may land `--fixes-red` branches through the gated merge
- [x] Always-run guard tests at close and loop gates (project: Meta-Tools) — `tree_guard` marker registry read statically by `tools/affected_tests.py --guards`, both pytest gates in `.nw/project.yaml` pass it, a registry test proving every tree-scanning test is marked, and directory readers added for new or changed test and src files
- [x] Cost-cap policy: auto-raise while converging (project: Meta-Tools) — `agentic.cost_cap_policy` (3× ceiling, convergence from review outcomes + divergence signals), raise in place instead of parking, reserve from the observed median run cost × margin, one human-only escalation above the ceiling or when diverging, `nw queue cost-policy explain`, decision-table row rewritten and the mail-borne rule retired
- [x] Idea-campaign ordering with --after and epic derivation (project: Meta-Tools) — IDEA-880: `nw queue add --after REF` stored on the campaign, `backlog_readiness` gating every campaign kind on it, spec-writer must-include of the prerequisites' spec ids, `nw spec seed-children` stamping `:AFTER:` from "(depends_on child N)" annotations, `--epic` batches ordered by the existing Kahn sort
- [x] Launch freshness and steward dontAsk (project: Meta-Tools) — stewards launch `--permission-mode dontAsk` with a reviewed allow-list (read, mail, `nw`, tests, fix branches in `.nw-worktrees`), a launch-spec hash recorded at launch and compared in conduct-status / conductor-brief / `nw doctor`, a `launch_stale` event plus one relaunch request per stale launch, all verified against the real `claude` binary
- [x] Escalation timeout and note attribution (project: Meta-Tools) — `topic` on escalations, `agentic.escalations.auto_apply_after_s` (1800), an `escalation_timeout` event, `nw escalations answer --auto` applying the recommendation as `by: conductor` (visible, mailed, reversible by a later human answer), human-only topics never time out; `nw campaign note` author resolution (persona / conductor / steward / session / human) (depends_on child 3)

Sequencing / dependencies (prose on purpose: `nw spec seed-children` turns every bullet of this
section into a child idea). Child 6 depends on child 3. Its timeout must never auto-apply a spend
escalation, and child 3 defines those escalation topics. If child 3 lands first, its escalations
carry topics that child 6 already treats as human-only. If child 6 lands first, a topic-less
legacy escalation is never eligible for the timeout, so the order is safe either way; the edge
records the review dependency. Child 1 does **not** depend on child 2. It gates fix-forward with
the full suite, which already includes every guard. Child 2 lowers how often main turns red;
child 1 handles red when it happens anyway. Children 1 to 5 are independent and can be built in
parallel (see Rollout for the shared files).

## Acceptance criteria

- [x] On a red sentinel the queue opens a fix-forward campaign within one runner tick, at
      priority 1. No human escalation is filed unless the episode lasts longer than
      `agentic.red_main.escalate_after_s` with no fix in flight, or every fix-forward attempt and
      the revert failed. An episode files at most one escalation, and a green sentinel withdraws
      it.
- [x] While a project's base is red, no non-fix campaign of that project merges. Held campaigns
      merge without intervention after the next green sentinel.
- [x] Every close and loop `pytest` gate of nightwright runs every registered tree guard, and a
      test fails when a tree-scanning test module is not registered.
- [x] A converging campaign blocked by its cap below 3× its profile cap continues without a park
      and with a `by: queue` decision record. Above the ceiling, or when diverging, the queue
      files exactly one human-only escalation.
- [x] Children of an epic queued with `nw queue add --epic` start only after the siblings named in
      their Breakdown annotations merge, and their written specs list those siblings' spec ids in
      `depends_on`.
- [x] `nw queue steward-launch` and `conductor-launch` both pass `--permission-mode dontAsk`. A
      session launched with an older launch spec is reported stale by `nw doctor` and
      `conduct-status`, and a relaunch request reaches the human once.
- [x] An escalation with a recommendation and a non-human-only topic is applied by the conductor
      30 min after it was filed, mailed to the human, and reversible by a later human answer.
      Human-only escalations stay open until the human answers.
- [x] Notes written by persona runs, the conductor and stewards are attributed to them, not to
      `human`.

## Test plan

Integration-level. Each child's unit and real-verification steps are in the child.

- **Integration/e2e tests** (`tests/test_selfheal_e2e.py`, new). The fixture is a real git repo in
  `tmp_path` with a `.nw/project.yaml` whose loop and close gates run real `python3 -m pytest`
  over a tiny test tree that includes one marked tree guard. It uses a real `NW_DATA_DIR` and
  real `queue.tick()` calls. Scripted fake personas edit files in the worktree; everything else
  is real: gates, git, sentinel, escalations, events. The test asserts, in order:
  1. A branch cut before the guard merges only after its close runs the guard. The guard fails,
     so the campaign does not merge (child 2).
  2. A red commit forced onto the base gives a red sentinel. Within one tick there is a
     `fixforward` campaign at priority 1 and no escalation. A held ordinary campaign stays
     CLOSING (child 1).
  3. The fix merges, a sentinel runs and is green, the episode closes and the held campaign
     merges (child 1).
  4. A campaign blocked by its cost cap at 1.0× with a passing review continues with a raised cap
     and no park (child 3).
  5. An epic with two idea children and "(depends_on child 1)" admits child 2 only after child 1
     merges (child 4).
  6. An escalation with a recommendation, its due time passed through `now`, produces one
     `escalation_timeout` event. `answer --auto` records `answered_by: conductor` (child 6).
- **Real-binary smoke** (manual, recorded in the epic's As shipped):
  - `nw queue steward-launch Meta-Tools --dry-run` and `conductor-launch --dry-run` both show
    `--permission-mode dontAsk`.
  - A `claude -p` haiku session started with the steward plan's flags in a scratch repo is
    allowed `nw queue list` and an Edit under `.nw-worktrees/`, and is denied `git push origin
    main` (child 5).
  - One fix-forward campaign runs with a real implementer persona (haiku, scratch data dir,
    fixture repo) and turns a deliberately red fixture base green (child 1).
- **Live observation** (manual, the 7 days after the last child lands), compared with the
  audit's 3 days, recorded here:
  - sentinel escalations per red episode (target ≤ 1);
  - red hours per episode;
  - cost escalations (target: none below 3×);
  - escalations that waited longer than 30 min with a recommendation and a non-human-only topic
    (target 0);
  - `by: human` notes that the human did not write (target 0).

## Rollout / sequencing

1. Land children 2, 4 and 5 first, in parallel. They are self-contained, and child 2 lowers how
   often main turns red. Child 5 needs the human to relaunch the conductor and stewards once
   (`--resume`), and child 5's own detection asks for exactly that.
2. Land children 1 and 3 next, in parallel. Both add queue behaviour behind config keys that
   default on: `agentic.red_main.enabled` and `agentic.cost_cap_policy.enabled`. Turning a key off
   restores today's behaviour for that child.
3. Land child 6 last, after child 3. Its timeout default (1800 s) takes effect at once for new
   escalations. Open records from before the change carry no topic and keep waiting.
4. Close the open legacy sentinel escalations (ESC-0024, ESC-0025): child 1 withdraws them on the
   first green sentinel after it lands.

Partial delivery is safe. Each child is useful alone, and none of them changes what the human
must decide.

## As shipped

All six children are `done` on main: SPEC-0420 (`d72d467f`), SPEC-0421 (`669824a0`), SPEC-0422
(`375e03f7`), SPEC-0423 (`321d77a3`), SPEC-0424 (`e6845504`), SPEC-0425 (`91724651` /
`e18e808e`). The invariant holds: routine failures resolve in the queue with `by: queue` /
`by: conductor` records, and the human-only list is unchanged.

**Deviations from the epic text** (each is in the child's As shipped):
- **Red main (SPEC-0420).** Legacy sentinel escalations (ESC-0024/0025/0026) are withdrawn as
  superseded, never adopted as the episode's record (adopting one would spend the single
  escalation on a question with no recommendation). Moot fixes are abandoned by the tick
  (`sweep`), not inside `on_green`. Fix branches are `nw/<campaign id>`, not
  `nw/fix-<episode>-<n>`. Merge order is base order (commit depth), not close time.
- **Guard tests (SPEC-0421).** The detector resolves only paths it can evaluate statically;
  scans through an imported module's `__file__` are not detected and rely on normal selection.
  A changed data file under `tests/` now also selects tests that scan `tests/`.
- **Cost policy (SPEC-0422).** Run-cost statistics come from an incremental ledger scan
  (`cost_policy.refresh`, once per tick), not a `record_run` hook. `plan_step` takes
  `reserves=` / `ceiling=` from the tick. Cost escalations are always human-only (kind `cost`
  plus the `spend_ceiling` / `spend_diverging` topics), and the escalation is filed `by:
  conductor` while the decision record is `by: queue`.
- **Idea ordering (SPEC-0423).** `nw queue after` was added for queued campaigns; a single
  `nw queue add IDEA-N` also honors the idea's `:AFTER:`. The live `seed-children` and
  `nw queue after --add` were left for rollout, not run against the live store.
- **Launch freshness (SPEC-0424).** `AskUserQuestion` is not allowed for stewards under
  `dontAsk` (denied by the real binary), so stewards ask in plain text. Freshness judges the
  newest `source: launch` record. The relaunch request is an event plus a mail; it never stops
  a session.
- **Escalation timeout (SPEC-0425).** The `depends_on: SPEC-0422` edge needed no wait: topic-less
  records never time out and cost / spend topics are human-only. Records without an `auto`
  block (pre-change) never time out. Reversal is allowed for `human` and `steward:<P>` only,
  once. Note attribution adds `note_actor` in `cli_campaign.py`.

**Test plan as executed.** The epic's single `tests/test_selfheal_e2e.py` was not written;
each child carries its own real-fixture e2e instead (`tests/test_redmain_e2e.py`,
`tests/test_cost_policy_e2e.py`, `tests/test_idea_ordering_e2e.py`, the guard replay of the
real incident commits in SPEC-0421, the real-binary run in SPEC-0424, the real-process run in
SPEC-0425), which covers steps 1 to 6 of the plan at finer grain. Real-binary smoke items are
recorded in SPEC-0420 (live check pending a real red episode) and SPEC-0424 (AC5). Regression:
affected tests and `spec_lint` per child; the full suite ran at each queue close gate.
Live state at close (2026-11-04): 0 open escalations; the Meta-Tools sentinel is green at
`f14d0da8`.

**Post-close monitoring (not AC gates).** The Test plan's 7-day live observation cannot be
true at close, and the ACs above are checked on the evidence of the children's tests and real
verification. Compare the following with the audit's 3 days and note results in a follow-up
idea if any target is missed:
- sentinel escalations per red episode (target <= 1), red hours per episode, and that no
  non-fix campaign merges while red (`nw queue sentinel status`, `nw queue events --type
  close_held`);
- the next campaign close gate log shows `+8 guards` in its `affected_tests:` line;
- cost escalations (target: none below 3x);
- escalations with a recommendation and a non-human-only topic that waited over 30 min
  (target 0);
- `by: human` notes the human did not write (target 0);
- the human relaunches the conductor and stewards once so `launch_stale` clears.

## Follow-ups

- IDEA-942: `/tmp` isolation for test runs.
- IDEA-944: SPEC-0353 deny rules containing a literal `*`.
- IDEA-945: the `default` permission mode.

## Definition of done

- [x] All child specs `done` or `superseded` (the linter gates this).
- [x] Integration test plan executed; `uv run pytest` green.
- [x] Acceptance criteria met; epic body reflects what shipped.
- [x] `docs/LEDGER.md` regenerated.

## Open questions

_None._ Every judgment call is recorded in the child specs' Decision and Design sections.
