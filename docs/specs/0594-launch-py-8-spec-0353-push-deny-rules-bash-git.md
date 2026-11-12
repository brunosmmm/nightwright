---
id: SPEC-0594
title: "launch.py: the 8 SPEC-0353 `nw/x:<dst>` push deny rules must not end in the legacy `:*` prefix form"
status: done
owner: user
created: 2026-11-12
updated: 2026-11-12
kind: feature
source_idea: IDEA-944
tags: [agentic, conductor, steward, permissions]
depends_on: [SPEC-0353, SPEC-0424]
---

## Context

[SPEC-0353](./0353-conductor-launch-steward-launch-allowedtools-do.md) (done, so not rewritten)
gave the conductor and steward launches a push guard in `src/nightwright/agentic/launch.py`
(`_PUSH_DENY`, `_push_deny()`). It was extended after review with four suffixes meant to deny a
push of an allowed `nw/*` / `wt/*` branch to any other destination (`git push origin
nw/x:release`, `… HEAD:nw/x:release`, a tag, a partly qualified `heads/main`):

```
* nw/*:*     * wt/*:*     *:nw/*:*     *:wt/*:*
```

Each is generated for the `git` and `git -C *` prefixes, so there are 8 rules, for example
`Bash(git push * nw/*:*)` and `Bash(git -C * push *:wt/*:*)`.

The real-binary check in [SPEC-0424](./0424-launch-freshness-and-steward-dontask.md) (AC5,
`claude` 2.1.318) found that Claude Code warns at startup that these 8 rules are read with a
literal `*` and suggests `Bash(git push * nw/*)`. The cause is the legacy prefix syntax:
`Bash(<prefix>:*)`, such as `Bash(git:*)`, means "starts with `<prefix>`", and the parser
recognises it by a pattern that **ends in `:*`**. All 8 rules end in `:*` because the `:` is a
literal refspec separator that we followed with the `*` wildcard. They are therefore parsed as the prefix
`git push * nw/*` (or `git -C * push *:wt/*`) in which the `*` characters are literal. Such a
rule matches no real command.

Consequence: the guard the rules were added for does not exist at runtime. `git push origin
nw/x:release` is matched by the allow rule `Bash(git push origin nw/*)` (the wildcard swallows
`:release`), and the only deny rules that still fire are the ones for specific destinations
(`*:main`, `*:master`, `*heads/main*`, `*refs/heads/main`, …). A push of an allowed branch to
any other destination (`nw/x:release`, `HEAD:nw/x:v1.0`) runs without a prompt in both auto and
`dontAsk` modes. Pushes to `main` / `master` are still denied by the other rules, so this is a
narrowing of the intended guard, not an opening to the base branch.

`_PUSH_DENY` is also the only place in `launch.py` that puts a `:` before a trailing wildcard;
no other default rule ends in `:*`.

## Goals / Non-goals

**Goals**
- None of the default `allowed_tools` / `disallowed_tools` rules (conductor and steward) ends in
  `:*`, so Claude Code never reads a default rule as legacy-prefix syntax, and the startup
  warning "reads its * literally" does not appear for a launch built from the defaults.
- The `nw/x:<dst>` guard works as SPEC-0353 intended: a push whose `nw/*` or `wt/*` ref is
  followed by `:<anything>` is denied (`git push origin nw/x:release`, `git push origin
  HEAD:nw/x:release`, `git -C /w push origin wt/x:v1`), in both prefix forms.
- The legitimate pushes stay allowed: `git push origin nw/x`, `git push --force-with-lease=r:sha
  origin HEAD:nw/x`, and their `git -C <dir>` and `wt/*` forms.
- A unit test fails if any default rule ends in `:*` again, and the fix is proven once against the
  real `claude` binary (SPEC-0424 AC5 style), because a fake matcher cannot see the warning.

**Non-goals**
- No change to the allowed push set, the rebase set, the steward guard, or the matcher used by
  the launch tests beyond what the new rules need.
- No `--permission-mode` change and no change to who can push where (still `nw/*` / `wt/*`
  only, never `main`).
- The `PERMISSION_MODES` `default` / `manual` finding from SPEC-0424 is a separate idea.
- Already running sessions are not changed: rules are fixed at launch (relaunch, see Rollout).

## Decision

Rewrite the four `:*`-ending push deny suffixes so that they no longer end in the two
characters `:*`, while keeping their meaning "ref, then `:`, then anything". Use a doubled
trailing wildcard, `:**`: `* nw/*:**`, `* wt/*:**`, `*:nw/*:**`, `*:wt/*:**`. `*` matches any
characters including none, so `**` matches the same strings as `*`, but the pattern ends in `**`,
not in `:*`, so the legacy-prefix parser does not apply. The suffix constants are shared by both
prefixes, so one change fixes all 8 rules. A real-binary probe is the acceptance gate for the
spelling. Claude Code's `Bash()` pattern language documents only `*` as a wildcard; nothing
shows that `?` is a single-character wildcard, so no `?` spelling (`:?*`, `:*?`) is part of this
spec and the launch tests' matchers (`_permitted`, `_hits`: `*` becomes `.*`, everything else,
`?` included, is literal) are not extended. If `claude` still warns or does not deny with `:**`,
the implementer stops, does not ship a different spelling, records the probe output in the
spec's As shipped section and sends the spec back for revision (a `?` fallback would first need
its own real-binary proof that `?` is a wildcard, plus a declared matcher extension and tests).
If the probe passes, the implementer records the `claude` version and the evidence in As shipped.

## Design

- **Rule change** in `src/nightwright/agentic/launch.py`, the `_PUSH_DENY += (...)` block: replace
  `"* nw/*:*"`, `"* wt/*:*"`, `"*:nw/*:*"`, `"*:wt/*:*"` with
  `"* nw/*:**"`, `"* wt/*:**"`, `"*:nw/*:**"`, `"*:wt/*:**"`. `_push_deny()` still prefixes each
  with `git push ` and `git -C * push `, so the 8 rules become `Bash(git push * nw/*:**)` and
  siblings. The rule count (32 `_PUSH_DENY` suffixes times 2 prefixes, plus the `git -C * push *` rule, so
  the 65 deny rules the count assertion at `tests/test_agentic_launch.py:680` expects) does not
  change, so the count assertions in `tests/test_agentic_launch.py` are unaffected.
- **Why `**` and not an enumerated allow-list inversion.** The deny stays a denylist
  (SPEC-0353's recorded residual risk). Replacing it with fewer, allow-shaped rules would need a
  different matcher model and is out of scope.
- **Does the deny still match what it must?** With `*` = `.*`: `git push origin nw/x:release`
  matches `git push * nw/*:**` (`origin`, `x`, `release`); `git push origin HEAD:nw/x:v1`
  matches `git push *:nw/*:**`. A legitimate lease push `git push --force-with-lease=r:sha origin
  HEAD:nw/x` contains `:` before `nw/` only (after `HEAD`), has no `:` after the `nw/x` ref, and
  is not matched by `* nw/*:**` (needs a space before `nw/`, but the ref follows `HEAD:`), nor by
  `*:nw/*:**` (needs a second `:` after `nw/`). It stays allowed. `git push
  --force-with-lease=r:sha origin nw/x` is also not matched by `* nw/*:**`: the `:` is before
  `origin`, not after `nw/`.
- **Guard test (new).** `test_no_default_rule_ends_in_legacy_prefix_form` in
  `tests/test_agentic_launch.py`: for every list key in `L.DEFAULTS` that holds `Bash(...)` rules
  (`allowed_tools`, `disallowed_tools`, `steward_allowed_tools`, `conductor_disallowed_tools`,
  and any other rule list present), assert that no rule's pattern (the text inside `Bash(` and the
  final `)`) ends in `:*`. It also asserts that the effective lists from `build_plan` for the
  conductor and a steward carry no such rule, so a configured override that copies a default is
  caught only if it copies the broken form (a user-configured list is replaced wholesale and is
  not checked).
- **Behaviour tests.** The existing parametrized cases that cite the old strings
  (`"Bash(git push * nw/*:*)"`, `"Bash(git push *:nw/*:*)"`, `"Bash(git push * wt/*:*)"`,
  `"Bash(git push *:wt/*:*)"`) are updated to the new strings. They keep proving, with the
  `_permitted` matcher (`*` → `.*`), that the named deny rule is the one that stops the command
  (the allow rule matches it first). One new test covers the `git -C <dir>` spelling of each.
- **Real-binary check** (manual, run once by the implementer; not a CI test): with `claude`
  >= 2.1.318, `--setting-sources project`, scratch data / config dirs and a scratch repo, take the
  argv from `nw queue steward-launch T --dry-run --json` and
  - (a) start `claude -p` with those flags before and after the change: before shows the
    "reads its * literally" warning for these 8 rules, after shows none for any default rule;
  - (b) ask it to run `git push origin nw/x:release` against a scratch bare remote: after the
    change the call is in `permission_denials` and no ref is created on the remote; and `git
    push origin nw/x` still succeeds.
  The result (claude version, spelling, evidence) goes into the spec's As shipped section.
- **Docs.** `docs/product/capabilities/agentic.md` (launch section) gets one sentence: default
  rules never end in `:*`, because Claude Code reads that as the legacy prefix form; a
  configured rule list that ends in `:*` is read as a prefix with a literal `*`.

## Alternatives considered

- **Delete the four suffixes and rely on `* nw/* *`.** The existing `* nw/* *` rule needs a token
  after the ref, so `git push origin nw/x:release` (the destination is part of the same token)
  would stay allowed. Rejected: it removes the guard instead of repairing it.
- **Follow the warning's suggestion, `Bash(git push * nw/*)`.** That pattern is identical to
  the allow rule and would deny every legitimate push. Rejected.
- **Escape the colon (`\:`).** Escaping is only documented for `*` / `\`; the behaviour of an
  escaped `:` is unverified, and the same real-binary probe is needed anyway. `:**` is plain
  wildcard semantics and needs no escape.
- **A `?` spelling (`:?*` / `:*?`) as the spelling or as a fallback.** Claude Code documents
  only `*` as a wildcard, so a `?` rule is unproven, and the test matchers treat `?` as a literal,
  so every deny case would stop matching and the allow cases would prove nothing. Rejected: it
  would need its own real-binary proof and a matcher extension that this spec does not scope.

## Acceptance criteria

- [x] No rule in any `L.DEFAULTS` rule list, and none in the effective `allowed_tools` /
      `disallowed_tools` of a conductor or steward `build_plan`, ends in `:*`.
- [x] The four suffixes are `* nw/*:**`, `* wt/*:**`, `*:nw/*:**`, `*:wt/*:**`, each present for
      both `git push` and `git -C * push`: 8 rules, and no rule contains a `?`.
- [x] With the `_permitted` matcher, `git push origin nw/x:release`, `git push origin
      HEAD:nw/x:release`, `git push origin wt/x:v1`, `git push origin HEAD:wt/x:v1` and their
      `git -C /w push …` forms are denied, and the named deny rule is the new one.
- [x] With the same matcher, `git push origin nw/x`, `git push --force-with-lease=r:sha origin
      HEAD:nw/x`, `git -C /w push origin wt/x` and `git -C /w push --force-with-lease=r:sha origin
      HEAD:wt/x` are still allowed.
- [x] Pushes to `main` / `master` (including `nw/x:main`, `HEAD:refs/heads/main`) stay denied, and the
      count of `push` allow rules (24) and the unique-rule assertions are unchanged.
- [x] Real binary: a `claude` 2.1.318+ session started with the steward plan's flags prints no
      "reads its * literally" warning for any default rule, denies `git push origin nw/x:release`
      (listed in `permission_denials`, no remote ref created) and allows `git push origin nw/x`.
      If any of the three fails, the spec is not shipped with another spelling: the failure is
      recorded in As shipped and the spec is revised.
- [x] `docs/product/capabilities/agentic.md` states the no-trailing-`:*` rule.

## Test plan

- **Automated tests** (extend the existing `tests/test_agentic_launch.py`, which already carries
  its one module-level marker `pytestmark = pytest.mark.fast`; no new test module and no second
  tier marker):
  - `test_no_default_rule_ends_in_legacy_prefix_form`: walks `L.DEFAULTS` and the conductor /
    steward `build_plan` effective lists; fails on any `Bash(...:*)` rule.
  - updated parametrized deny cases for the four new suffixes (named deny rule is the one that
    stops the command), plus new `git -C /w push …` cases for each suffix;
  - a parametrized allow case for the lease-pinned and plain pushes in both prefix forms, to prove
    the legitimate pushes stay allowed, and an assertion that `main` / `master` destinations stay
    denied (`nw/x:main`, `HEAD:nw/x:refs/heads/main`).
  - the test matchers `_permitted` / `_hits` are not changed (`?` stays a literal there), and the
    parametrized cases cite exactly the `:**` strings the rules carry, so the behaviour tests
    run against the shipped spelling;
  - the existing rule-count and legacy-literal tests (`test_push_rule_count_and_legacy_literals`)
    keep passing unchanged.
  - Run with `python3 tools/affected_tests.py --run uv run pytest -q`, plus
    `uv run pytest -q tests/test_agentic_launch.py tests/test_conductor_git_guard.py
    tests/test_claude_settings_allowlist.py`; `python3 tools/test_tiers.py --check` exits 0
    (`tools/test_tiers.py` exists and no module gains or loses a marker).
- **Manual verification** (real binary, once, see Design): the (a) warning check and (b) denial /
  allow check against a scratch bare remote, using `claude` >= 2.1.318, with the version and the
  outcome recorded in As shipped. A fake cannot show the warning, so this is not optional
  (memory: validate integrations against the real binary). If the `:**` probe fails, stop and
  record it; no `?` spelling is tried inside this spec.
- **Regression guard:** the existing SPEC-0353 / SPEC-0382 / SPEC-0424 launch tests and the
  `python3 tools/spec_lint.py` run stay green; `test_conductor_git_guard.py` and
  `test_claude_settings_allowlist.py` (which scan the same rule families) are run explicitly.

## Rollout / migration

1. Land the rule change, the tests and the doc sentence in one change.
2. Run the real-binary check and record it in the spec.
3. Running conductor and steward sessions keep the old, ineffective rules until relaunched
   (`nw queue conductor-launch --resume` / `steward-launch <project> --resume`); `nw doctor`'s
   `agentic.launch_fresh` check (SPEC-0424) already reports them stale once the launch hash
   changes, so no extra migration is needed.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass (`python3 tools/affected_tests.py --run uv run
      pytest -q`); real-binary steps performed and noted in the spec.
- [x] `python3 tools/test_tiers.py --check` exits 0.
- [x] No regressions.
- [x] Spec body updated to match what shipped (spelling used, claude version, evidence).
- [x] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`) and
      `python3 tools/spec_lint.py` exits 0.

## Open questions

- None.

### Test record (2026-11-12)

- `uv run pytest -q tests/test_agentic_launch.py tests/test_conductor_git_guard.py
  tests/test_claude_settings_allowlist.py`: pass (new tests: `test_nw_ref_with_destination_is_denied`,
  `test_legitimate_pushes_stay_allowed_with_colon_suffix_rules`,
  `test_no_default_rule_ends_in_legacy_prefix_form`; `test_each_guard_family_is_what_stops_the_command`
  cases updated to `:**` plus four `git -C` cases).
- `python3 tools/affected_tests.py --run uv run pytest -q`: 2922 passed. `python3 tools/test_tiers.py
  --check` exits 0; `python3 tools/spec_lint.py` exits 0 (ledger regenerated). The full suite is left
  to gating.

### As shipped (2026-11-12)

- Spelling shipped: `:**` (`* nw/*:**`, `* wt/*:**`, `*:nw/*:**`, `*:wt/*:**`), exactly as Design;
  `_push_deny()` still yields 65 rules, 8 of them the new ones, none with a `?`, none ending in `:*`.
- One test outside the named files changed: the launch-rules snapshot hash in
  `test_verb_rules_snapshot_and_two_projects_launch_the_same_rules` (it hashes `disallowed_tools`)
  moved from `a7c908c1fafd7b35` to `696d5e0d73993c23`, the expected effect of the 8 respelled rules.
- Real-binary probe, `claude` 2.1.322 (>= 2.1.318), scratch repo + bare remote, argv lists from
  `uv run nw queue steward-launch Meta-Tools --dry-run --json`, `--setting-sources project`,
  `--permission-mode dontAsk`; the model was told to run exactly the quoted command:
  - (b) `git push origin nw/x:release` and `git push origin nw/x:v1.0`: both listed in
    `permission_denials` with the exact command, and no `release` / `v1.0` ref appeared on the
    remote; `git push origin nw/x` ran and created `refs/heads/nw/x`;
    `git push --force-with-lease=nw/x:abc123 origin HEAD:nw/x` passed permissions (it failed in git
    on the fake sha, no denial). With the previous `:*` rules (same probe, before the change)
    `nw/x:release` ran and pushed to the remote. A single-rule probe showed `Bash(git push *
    nw/*:**)` and `…:v*` deny while `:?*`, `\:*` and `:* *` do not, so `:**` needed no fallback.
  - (a) `claude -p` prints no rule warning in either state; the diagnostic is `claude doctor`
    ("Permission rules that may not work as written"). Before: `Bash(git push * nw/*:*)` "mixes *
    with the trailing :* prefix syntax, so it is matched as a literal prefix". After, for all 8
    rules: an advisory "has a :* that is not at the end, so it is matched as a * wildcard ... It
    already matches as a * wildcard". The literal-prefix warning is gone; this different advisory
    (it states the rule works) is still listed by `claude doctor`, so AC6's "no literal-`*`
    warning" holds as worded but the doctor output is not fully clean. See the follow-up idea.
