---
id: SPEC-0353
title: "conductor-launch / steward-launch permission rules cover lease-pinned PR-rebase pushes and the steward rebase set"
status: done
owner: user
created: 2026-11-01
updated: 2026-11-01
kind: feature
source_idea: IDEA-639
tags: [agentic, conductor, steward, permissions]
depends_on: [SPEC-0339]
---

## Context

[SPEC-0339](./0339-conductor-launch-session-scoped-permission-rules.md) (done, so not rewritten)
gave `nw queue conductor-launch` / `steward-launch` session-scoped `--allowedTools` /
`--disallowedTools` defaults (`DEFAULTS` in `src/nightwright/agentic/launch.py`). The allow
list only contains the literal pushes `git push --force-with-lease origin nw/*` and
`git push origin nw/*` (and the `wt/*` pair). That does not match the pushes a steward
actually runs when it rebases a PR branch or resolves a conflict:

```
git -C <worktree> push --force-with-lease=<ref>:<sha> origin HEAD:nw/<branch>
```

which differs in three ways: `git -C <dir>` before `push`, a lease pinned with
`=<ref>:<sha>`, and a `HEAD:<ref>` refspec. Claude Code matches `Bash(...)` rules on the
command string with `*` as a wildcard, so none of those spellings match; the push falls
through to the permission prompt, and in auto mode it is blocked. This blocked the
comfort-assistant steward on PRs 9 and 10. A session cannot edit its own settings, so the
steward could not unblock itself; only a relaunch with wider rules can.

Separately the steward has no allow rules for the rest of a rebase / conflict-resolution
cycle (`fetch`, `rebase`, `add`, `commit`, `checkout --ours/--theirs`), so each step prompts.

The deny list also has gaps that matter once the allow list widens: `Bash(git push --force *)`
does not cover `git -C <dir> push --force …`, and `Bash(git push * main)` does not cover a
lease-pinned refspec such as `HEAD:refs/heads/main` or `nw/x:main`.

## Goals / Non-goals

**Goals**
- The conductor and steward allow lists cover lease-pinned force pushes to `nw/*` and `wt/*`
  in every spelling above (plain `git` or `git -C <dir>`; `--force-with-lease` or
  `--force-with-lease=<ref>:<sha>`; refspec `<branch>` or `HEAD:<branch>`), and the plain
  non-force pushes in the same spellings.
- Pushes to `main` / `master` (any refspec spelling) and bare `--force` / `-f` stay denied,
  with and without `git -C <dir>`.
- Stewards additionally get the local rebase / conflict-resolution rule set.
- A unit test proves, against a Claude Code-style matcher, that the real commands from the
  incident are allowed and the dangerous variants are denied.
- The behaviour is documented in the launch capability doc.

**Non-goals**
- No `--dangerously-skip-permissions` and no `--permission-mode` change.
- No push permission for any branch outside `nw/*` / `wt/*`, no `gh pr merge`.
- No read-only git rules (`status`, `diff`, `log`) as defaults; the human keeps those in
  global settings, as for read-only `gh` in SPEC-0339.
- Already running stewards are not changed: rules are fixed at launch (see Rollout).
- No emulation of Claude Code's full permission engine in production code.

## Decision

Keep the SPEC-0339 mechanism (code-held `DEFAULTS`, rules passed as launch flags, a
configured list replaces the default wholesale). Widen the default `allowed_tools` push rules
to the `git`/`git -C *` × lease form × refspec form product for `nw/*` and `wt/*`, widen
`disallowed_tools` to the same two command prefixes, to refspec spellings of `main` /
`master` and to every trailing-argument shape the wildcard allows can swallow, add a
steward guard of explicit denies (shell-executing and destructive git forms), and add a new
`steward_allowed_tools` default (rebase / conflict-resolution rules) that is appended to
`allowed_tools` for the steward role only.

## Design

All in `src/nightwright/agentic/launch.py`; rule syntax is the space-wildcard form from
SPEC-0339.

- **Push rule generation.** A module helper builds the push allow list from small tuples so the
  list is not hand-copied 24 times:
  - prefixes: `git`, `git -C *`
  - branch namespaces: `nw/*`, `wt/*`
  - refs per namespace: `<ns>` and `HEAD:<ns>`
  - flag forms: none (plain push), `--force-with-lease`, `--force-with-lease=*`

  Each rule is `Bash(<prefix> push [<flag> ]origin <ref>)`, e.g.
  `Bash(git -C * push --force-with-lease=* origin HEAD:nw/*)`. The original four SPEC-0339
  literals are members of the product, so existing behaviour is preserved. The `gh pr create`,
  `gh pr edit`, `gh pr comment` allows are unchanged.
- **Deny list (push guard).** Allowed push rules end in a wildcard, so every argument that
  can follow the ref is guarded by a deny rule. A second helper generates, for both prefixes
  (`git`, `git -C *`), these `push …` suffixes (25 per prefix, 50 in total), each as
  `Bash(<prefix> push …)`:
  - main/master as the last or a non-last token: `* main`, `* master`, `* main *`, `* master *`;
  - main/master as a refspec destination: `*:main`, `*:master`, `*:main *`, `*:master *`,
    `*refs/heads/main`, `*refs/heads/master`, `*refs/heads/main *`, `*refs/heads/master *`;
  - the `+` force refspec: `*+*` (subsumes `+main` / `+master`);
  - bare force flags anywhere: `--force *`, `-f *`, `--force`, `-f`, `* --force`,
    `* --force *`, `* -f`, `* -f *`;
  - extra tokens after a namespaced ref (an extra refspec or flag): `* nw/* *`,
    `* wt/* *`, `*:nw/* *`, `*:wt/* *`.

  `Bash(gh pr merge *)` is kept. Deny rules take precedence over allow rules in Claude Code,
  so a lease-pinned push to `main` is denied even though the `--force-with-lease=*` allow
  exists. Legitimate pushes still pass: `--force-with-lease=…` is not matched by the force
  rules (they need a space after `--force`), `* nw/* *` needs a literal space before `nw/`
  and a further token after the ref, and `*:nw/* *` needs a token after `HEAD:nw/<branch>`.
  The old single-prefix `HEAD:main` / `HEAD:master` rules are subsumed and dropped.
  Denied by these rules: `git push origin nw/x +main`, `git push origin nw/x main --verbose`,
  `git push origin nw/x --force`, `git -C /w push origin HEAD:nw/x -f`,
  `git push origin nw/x feature/y`.
- **Steward rebase set** (`DEFAULTS["steward_allowed_tools"]`), for `git` and `git -C *`:
  `fetch *`, `rebase *`, `rebase --continue`, `rebase --abort`, `add *`, `commit *`,
  `restore *`, `checkout --ours *`, `checkout --theirs *`. With `*` matching spaces these
  allow rules are leaky on their own (`git -C * add *` matches `git -C x branch -D main add y`,
  `git rebase *` matches `git rebase -x '<shell>'`, `git fetch *` matches
  `--upload-pack=<cmd>`), so the set is only safe together with the steward guard below.
- **Steward guard** (in `DEFAULTS["disallowed_tools"]`, applied to both roles; the conductor
  never runs these). Because a prompt is not a safety net in auto mode, destructive and
  shell-executing git forms are denied explicitly. Each rule exists in two spellings, `git <x>`
  (subcommand first) and `git * <x>` (anything between, which covers `git -C <dir> <x>`):
  - shell execution: `* --exec*`, `* -x *`, `*--upload-pack*`, `*--receive-pack*`, and the
    config-injection flags `-c *` / `* -c *` (lowercase; `-C` is a different, allowed flag);
  - history/branch destruction: `reset --hard*`, `branch -D*`, `branch -d*`,
    `branch --delete*`, `clean *`, `clean`;
  - the swallow guard against `-C *` hiding a push to another ref behind a steward keyword:
    `git * push * <kw>` and `git * push * <kw> *` for `<kw>` in `fetch`, `rebase`, `add`,
    `commit`, `restore`, `checkout`.

  Residual risk, recorded rather than hidden: the guard is a denylist, so an exotic shape that
  is both allowed by a wildcard and absent from the deny families still runs. The shapes
  enumerated here are the ones reachable through the `-C *` and `--force-with-lease=*`
  wildcards; a surprise is fixed by adding a deny rule and relaunching.
- **Settings.** `steward_allowed_tools` is a new list key validated by the same rule as
  `allowed_tools` (non-list or empty/non-string entry falls back to the default; `[]` is
  valid and removes the set) and overridable at top level or in the `steward` sub-mapping.
  `build_plan` computes the effective allow list as `allowed_tools` for the conductor and
  `allowed_tools + steward_allowed_tools` (order kept, duplicates dropped) for a steward;
  `plan["allowed_tools"]`, the `--allowedTools` argv and the `--dry-run` / `--json` output all
  carry the effective list. A conductor ignores `steward_allowed_tools`.
- **Matcher assumption.** `*` matches any characters including spaces and `/`.
  The rule list therefore must not depend on `*` being restricted to one token. Because the
  `-C *` wildcard can swallow extra words, the `git -C *` deny rules are the guard against
  `git -C x push --force …` and `… HEAD:main`, and the steward guard is the guard against
  `git -C x branch -D main add y`. The test helper (Test plan) applies the same `*` → `.*`
  semantics so the tests exercise exactly this swallowing.
- **Docs.** `docs/product/capabilities/agentic.md` (launch section) lists the new default
  rule families, `steward_allowed_tools`, and the relaunch requirement.

## Alternatives considered

- **One catch-all `Bash(git push *)` allow** — the deny rules would then be the only
  guard, and any push spelling not denied (other remotes, deleting branches) would pass
  silently. Too broad.
- **Per-project settings / global settings for the pushes** — the human explicitly does not want
  these inherited by every session (SPEC-0339).
- **Tell stewards to avoid `git -C` and use the cwd** — fragile: the steward's cwd is the
  project root, not the campaign worktree, and a model's command spelling is not something
  nightwright controls.
- **Put the rebase set into the shared `allowed_tools`** — the conductor never rebases a
  worktree itself; least privilege keeps it steward-only.
- **Amend SPEC-0339 in place** — it is `done`; AGENTS.md requires a new spec for changed
  decisions. This spec extends rather than supersedes it (the mechanism is unchanged).

## Acceptance criteria

- [x] `DEFAULTS["allowed_tools"]` contains, for each of `nw/*` and `wt/*`, rules for plain,
      `--force-with-lease` and `--force-with-lease=*` pushes, each with prefix `git` and
      `git -C *` and with refs `<ns>` and `HEAD:<ns>` (24 push rules), plus the three
      unchanged `gh pr` rules; the four SPEC-0339 push literals are still present.
- [x] `DEFAULTS["disallowed_tools"]` contains the 25 push-guard rules per prefix (50 for
      `git` and `git -C *`: main/master incl. non-last position, refspec destinations,
      `*+*`, bare force flags anywhere, extra tokens after `nw/*` / `wt/*` refs), the steward
      guard rules (shell execution, `-c`, destructive, swallow guard) and `Bash(gh pr merge *)`.
- [x] Against the test matcher (deny wins over allow), these are allowed:
      `git push --force-with-lease origin nw/x`,
      `git -C /w/wt push --force-with-lease=nw/x:abc123 origin HEAD:nw/x`,
      `git -C /w/wt push origin HEAD:wt/x`, `git push origin wt/x`.
- [x] Against the same matcher these are denied (not allowed): `git push origin main`,
      `git push --force-with-lease=main:abc origin HEAD:main`,
      `git -C /w push --force-with-lease=nw/x:abc origin HEAD:refs/heads/master`,
      `git push --force origin nw/x`, `git -C /w push -f origin HEAD:nw/x`,
      `git push --force`, `git push origin feature/x`, `gh pr merge 9`, and the
      trailing-argument cases `git push origin nw/x +main`,
      `git push origin nw/x main --verbose`, `git push origin nw/x --force`,
      `git -C /w push origin HEAD:nw/x -f`, `git push origin nw/x feature/y`,
      `git push --force-with-lease=x --force origin nw/y`.
- [x] `DEFAULTS["steward_allowed_tools"]` holds the steward rebase set; `build_plan` for a
      steward includes it in `plan["allowed_tools"]` and the `--allowedTools` argv; for the
      conductor it does not.
- [x] `steward_allowed_tools` can be overridden or emptied at top level and under `steward`;
      a bad value falls back to the default; `allowed_tools: []` still removes the
      base list without removing the steward set (and vice versa).
- [x] Against the test matcher with the steward effective lists, these are denied:
      `git -C /w branch -D main add x`, `git -C /w reset --hard HEAD~3 add x`,
      `git rebase -x "git push --force origin main" origin/main`,
      `git fetch --upload-pack=sh origin`, `git -C /w clean -fdx add x`,
      `git -C /w push origin feature/y add x`; and these are allowed:
      `git -C /w rebase origin/main`, `git -C /w rebase --continue`,
      `git -C /w add src/a.py`, `git -C /w commit -m msg`,
      `git -C /w checkout --theirs a.py`.
- [x] `--dry-run` text and `--json` show the effective list, `--resume` applies it, and the
      prompt is still the last argv element (the variadic-flag invariant from SPEC-0339).
- [x] The launch capability doc describes the widened rules, `steward_allowed_tools`, and that
      running stewards must be relaunched.
- [x] `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests** (`tests/test_agentic_launch.py`, extending the SPEC-0339 permission
  tests):
  - A small test-only helper `_permitted(command, allow, deny)` converts each
    `Bash(<pattern>)` rule to an anchored regex with `*` → `.*` (and everything else
    escaped), returns `matches(allow) and not matches(deny)`. The docstring notes it
    approximates Claude Code's matcher for these rule shapes.
  - `test_default_rules_allow_incident_pushes`: the allowed commands from the criteria,
    plus the exact incident command with a real-looking sha.
  - `test_default_rules_deny_dangerous_pushes`: the denied list, including `git -C` forms,
    refspec spellings of `main`/`master`, bare `--force`/`-f`, other branches, and the
    trailing-argument cases (`git push origin nw/x +main`,
    `git push origin nw/x main --verbose`, `git push origin nw/x --force`,
    `git -C /w push origin HEAD:nw/x -f`, `git push origin nw/x feature/y`,
    `git push --force-with-lease=x --force origin nw/y`).
  - `test_push_guard_rule_count`: 25 push-guard denies per prefix, no duplicates.
  - `test_push_rule_count_and_legacy_literals`: 24 push rules, SPEC-0339 literals present,
    no duplicates.
  - `test_steward_plan_includes_rebase_set` / `test_conductor_plan_excludes_rebase_set`:
    argv, `plan["allowed_tools"]` and JSON dry-run output; prompt last.
  - `test_steward_allowed_tools_overrides`: top-level, `steward` sub-mapping, `[]`, bad
    value fallback; independence from `allowed_tools: []`.
  - `test_steward_guard_denies_swallowed_and_exec_commands` (replaces the rule-string check):
    builds the steward plan's effective allow/deny lists and runs the commands from the
    steward acceptance criterion through `_permitted`, asserting the `-C *`-swallowing and
    `--exec` / `--upload-pack` commands are denied and the legitimate rebase commands stay
    allowed.
  - Existing `test_default_permission_rules`, `test_plan_has_permission_flags_and_prompt_stays_last`
    and `test_resume_applies_permission_rules` are updated to the new defaults and keep
    passing; run via `python3 tools/affected_tests.py --run uv run pytest -q` plus
    `tests/test_agentic_launch.py`.
- **Manual verification:** `uv run nw queue steward-launch <Project> --dry-run` prints the
  `git -C * push --force-with-lease=* origin HEAD:nw/*` rule and the rebase set;
  `uv run nw queue conductor-launch --dry-run --json | jq .allowed_tools` has the push rules
  but no `rebase`. After the human relaunches a steward, a lease-pinned `git -C` push of a
  `nw/*` branch runs without a prompt in auto mode and a push to `main` is still refused;
  the outcome is noted under "As shipped".
- **Regression guard:** the SPEC-0339 tests above; the full suite runs at the queue close
  gate. `uv run pytest tests/test_spec_lint.py` / `spec_lint` confirm spec and ledger.

### Test record (2026-11-01)

- `uv run pytest -q tests/test_agentic_launch.py`: 123 passed (new: `test_default_rules_allow_incident_pushes`,
  `test_default_rules_deny_dangerous_pushes`, `test_push_guard_rule_count`,
  `test_push_rule_count_and_legacy_literals`, `test_steward_plan_includes_rebase_set`,
  `test_conductor_plan_excludes_rebase_set`, `test_steward_allowed_tools_overrides`,
  `test_steward_guard_denies_swallowed_and_exec_commands`,
  `test_known_false_positive_commit_message_mentioning_push_main`; the SPEC-0339 permission tests
  updated to the new defaults).
- Manual: `nw queue steward-launch Meta-Tools --dry-run` prints `git -C * push --force-with-lease=* origin
  HEAD:nw/*` and `git -C * rebase *`; `nw queue conductor-launch --dry-run --json | jq .allowed_tools`
  has the push rules and no `rebase` rule.
- Not performed: the live relaunch of a steward in Claude Code (needs the human's session); the
  extra live checks (`... HEAD:nw/<b> --force`, `branch -D <scratch> add x` refused) are therefore
  still open, see As shipped.
- The full suite was left to the queue close gate.
- Review round 1 (2026-11-01): `uv run pytest -q tests/test_agentic_launch.py`: 175 passed. New:
  deny cases for `nw/x:heads/main`, `nw/x:release`, tags, escaped-space/second-remote lease values and
  `git -C … config|rm … "<allowed tail>"` for both roles; the steward swallow/exec test gained `config`,
  `rm`, `update-ref`; `test_each_guard_family_is_what_stops_the_command` pins, per deny family, that
  the command matches an allow rule and the named deny rule (including `-c`, `clean`, `branch -d`,
  `--receive-pack`, `*:nw/* *`, `*:wt/* *`, `* wt/* *`).

- Review round 2 (2026-11-01): `uv run pytest -q tests/test_agentic_launch.py`: 176 passed (new
  `test_steward_guard_denies_stuck_and_abbreviated_shell_exec_spellings`); `spec_lint` OK. Full suite left
  to the close gate.
- Review round 3 (2026-11-01): `uv run pytest -q tests/test_agentic_launch.py`: 214 passed (new
  swallow-family tests, extra guard-family pins); `spec_lint` OK. Full suite left to the close gate.

### As shipped

- Implemented as designed in `src/nightwright/agentic/launch.py` (`_push_allow`, `_push_deny`,
  `_steward_guard`, `LIST_KEYS`). `steward_allowed_tools` is validated through `LIST_KEYS` in `_good`
  but is not a `TOOL_FLAGS` key; only the merged list is emitted under `--allowedTools`.
- Steward guard shape: subcommand forms are generated as `git <x>` and `git * <x>`; flag forms as
  `git *<x>` (`--exec*`, ` -x *`, `--upload-pack*`, `--receive-pack*`); config injection as
  `git -c *` / `git * -c *`; swallow guard `git * push * <kw>` / `... <kw> *` for six keywords.
  The disallowed list is 81 rules (50 push guard, 30 steward guard, `gh pr merge`).
- Hardening after review round 1 (deviates from the counts in Design; the Design rules are all still
  present, extra rules were added): the push guard is now 32 rules per prefix plus `git -C * * push *`
  (65 total). Added per prefix: `* nw/*:*`, `* wt/*:*`, `*:nw/*:*`, `*:wt/*:*` (a `:<dst>` after a
  namespaced ref: `nw/x:heads/main`, `nw/x:release`, tags), `*heads/main*`, `*heads/master*` (partly
  qualified main/master) and `--force-with-lease=* * origin *` (a second token, e.g. another remote or
  `-o a\ origin nw/x`, hidden in the lease wildcard). `git -C * * push *` requires ` push ` to be the
  4th token, so `git -C <dir> <other-sub> … "x # push origin nw/y"` is denied (this also stops
  `-C /w -c … push`). The steward guard gained `git * <sub>` / `git * <sub> *` for `config`, `rm`,
  `update-ref`, `symbolic-ref`, `worktree`, `stash`, `remote`, `submodule`, `filter-branch`,
  `filter-repo`, `gc`, `reflog`, `bisect`, `difftool`, `mergetool`, `hook`, `maintenance`, `replace`
  and the flags `--config-env`, `--git-dir`, `--work-tree`; the disallowed list is now 135 rules (65 push guard + 69 steward guard + 1
  `gh pr merge`). `git -C * <verb> *` steward allows (`add`, `commit`, …) can still swallow a subcommand not
  in that list (`merge`, `cherry-pick`, `checkout -f`, `reset --soft/--mixed`, `tag`, …) with a
  quoted tail; those only touch the local worktree/repo and are accepted as residual risk, as is a
  `-C` directory containing a space. The guard assumes Claude Code treats `\`/quotes literally (the
  test matcher does); unconfirmed against the live matcher. New false positives: any `git -C`
  command with the words `config`, `rm`, `remote`, `stash`, … as separate arguments (a commit message)
  is denied; use `commit -F`.
- Known false positive, pinned by test: `git -C * push * main *` also denies a commit whose message
  contains ` push ... main <more>` (e.g. `-m "before push to main now"`). A message that ends in `main"`
  is not denied, since the closing quote stops the anchored `push * main`.
- Test matcher vs Claude Code: no difference observed yet because the live relaunch check is pending;
  a human relaunching a steward should confirm the refused/allowed commands listed in the manual
  verification and amend here if the real matcher differs.

- Hardening after review round 2 (spelling-independence of the shell-exec family): git's parse-options
  accepts a stuck short value (`-x'cmd'`, `-x\ cmd`) and any unambiguous long-option abbreviation, so
  `_GUARD_FLAGS` is now `--ex*`, ` -x*` (no trailing space), `--upl*`, `--rece*`, `--config-e*`,
  `--git-d*`, `--wo*` (shortest prefixes that do not hit `--update`, `--recurse-submodules` or
  `--continue`; `--up*` / `--rec*` were rejected for those false positives). `-x` behind other short
  flags (`rebase -ix'cmd'`, `-qx'cmd'`) is denied by `git [* ]rebase -*x*` and `git [* ]rebase * -*x*`
  (4 rules): a `rebase` with any dash option and an `x` later in the line is denied (new false
  positive, e.g. `rebase -r origin/next`). The disallowed list is now 139 rules (steward guard 73).
  Tests: `test_steward_guard_denies_stuck_and_abbreviated_shell_exec_spellings`; the guard-family pins
  use the new rule names.
- Residual risk (not guarded): word splitting through a tab, `$IFS` / `${IFS}` or `$(...)` inside the
  `-C *`-swallowed part of a command, and global options git matches exactly are denied only by their
  spelled prefix.

- Hardening after review round 3 (structural close of the `-C *` swallow hole): `git -C /w grep
  -O'<cmd>' -e . -- add .` matched `git -C * add *` and no deny rule, so it ran a shell command;
  `log|diff --output=<file>` overwrote files and `clone --template=` installed a hook. Naming more
  subcommands cannot close this, so for each keyword in `_GUARD_KEYWORDS` (fetch, rebase, add,
  commit, restore, checkout) the guard now has `git -C * * <kw> *` and `git -C * * <kw>`, like
  `git -C * * push *` for push: the allowed verb must be the 3rd token (right after `-C <dir>`);
  any `git -C <dir> <other-sub> … <kw> …` is denied whatever the other subcommand is.
  `rebase --continue|--abort`, `checkout --ours|--theirs`, `commit -m msg|-F file` stay allowed.
  Also denied by name: `git [* ]grep *-O*`, `--open-f*`, `--out*`, `--templ*` (flags). The disallowed
  list is now 156 rules. New false positive, pinned by test: a `-m` message containing ` add `,
  ` commit `, ` fetch `, ` rebase `, ` restore ` or ` checkout ` as separate words is denied; use
  `commit -F`. Tests: `test_steward_c_swallow_of_other_subcommands_is_denied` (every keyword tail),
  `test_steward_c_swallow_family_keeps_legit_commands_and_pins_false_positive`, extended
  `test_each_guard_family_is_what_stops_the_command`.

## Rollout / migration

1. Land the code, tests and doc change through the queue.
2. Permission flags are fixed when a Claude session starts, so already running conductor
   and steward sessions keep the old rules. The human relaunches stewards (and the conductor
   when convenient) with `nw queue steward-launch <Project>` / `conductor-launch`, using
   `--resume` to keep the conversation; the resumed session picks up the new rules.
3. A user config that sets `allowed_tools` / `disallowed_tools` replaces the defaults
   wholesale and does not get the widening; the doc notes this.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; manual steps performed and noted.
- [x] No regressions (full suite at the queue close gate).
- [x] Spec body updated to match what shipped (As shipped section).
- [x] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

- None.
