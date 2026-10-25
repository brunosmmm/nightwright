---
id: SPEC-0231
title: "Followers exit with code_changed on new wt code; conductor and steward re-arm and re-read skills"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, events, mailbox, conductor, steward, skills]
source_idea: IDEA-088
depends_on: [SPEC-0182, SPEC-0183, SPEC-0194]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md),
promoted from `IDEA-088`.

The runner already follows merged code: between ticks `queue.run(..., reexec=True)` compares
the wt checkout's HEAD with the sha it started on (`queue.code_changes`, `_CODE_PATHS =
("src/wt", "pyproject.toml")`) and re-execs
([SPEC-0194](./0194-runner-follows-merged-code-re-exec-between-ticks.md)). Every other `wt` CLI call is a fresh
process. Two long-lived things still keep stale code:

1. **The followers** that the conductor's and stewards' Monitors run:
   `wt queue events --follow --for conductor` (`events.follow`,
   [SPEC-0182](./0182-event-driven-wt-conduct-monitor-armed-conductor.md)) and
   `wt mailbox follow --to steward:<P>` (`mailbox.follow`,
   [SPEC-0183](./0183-file-based-mailbox-between-conductor-and-stewards.md)). Both loop forever in one process. After
   SPEC-0212 merged, the conductor's follower kept running the old `sync` (no pings, no new
   watchdog checks) until the conductor re-armed it by hand.
2. **The sessions.** `/wt-conduct` and `/wt-steward` are read once, when the skill is
   invoked. A merged change to `skills/wt-conduct/SKILL.md` (a new decision-table row) does
   not reach the live conductor until someone tells it to re-read the skill.

The skills live in the same checkout (`skills/<name>/SKILL.md`, symlinked into
`~/.claude/skills` by `wt skills install`), so one HEAD comparison covers both.

## Goals / Non-goals

**Goals**
- G1. Both followers record the code sha at start and, when the wt checkout's HEAD moves
  with changes under `src/wt`, `pyproject.toml` or `skills/`, print one final `code_changed`
  line and exit 0. No event or message is lost or repeated: the cursor already points past
  the last printed line.
- G2. The `code_changed` payload names `from_sha`, `to_sha`, the changed `paths` and
  `skills_changed` (skill directory names), so a session knows whether to re-read a skill.
- G3. `wt-conduct` and `wt-steward` re-arm their Monitor at once on `code_changed`, and
  re-invoke their own skill (and `/wt-orient`) when it is listed in `skills_changed`.

**Non-goals**
- Hot reload inside a process (`importlib.reload`). Exiting and re-arming is simpler and
  matches the runner's re-exec.
- Watching uncommitted edits or non-checkout installs (a wheel in a venv). As with SPEC-0194,
  only a git checkout's HEAD is watched. Without a checkout the watch is off.
- Re-execing the follower itself. The Monitor owner (the session) re-arms. A re-exec would
  hide the event from the session, which must re-read changed skills.
- Changing the runner's own re-exec (SPEC-0194/0213) or its `_CODE_PATHS`.

## Decision

Move the code-change helpers (`code_checkout`, `_git_out`, `code_changes`) from `queue.py`
into a new module `src/wt/agentic/codewatch.py`, and re-export them from `queue` so existing
callers and test patches keep working. `code_changes` gains a `paths` argument (default
`_CODE_PATHS`). Add a `codewatch.Watcher` class used by both followers. It checks at most
every `code_check_s` (default 10 s) over `FOLLOWER_PATHS = ("src/wt", "pyproject.toml",
"skills")`. When the change touches those paths, the follower prints a synthetic
`code_changed` envelope (not appended to `events.jsonl`) and returns. Both skills get a
`code_changed` row.

## Design

### `codewatch.py`

```python
_CODE_PATHS = ("src/wt", "pyproject.toml")              # runner (SPEC-0194), unchanged
FOLLOWER_PATHS = _CODE_PATHS + ("skills",)

def code_checkout() -> Path | None            # moved verbatim (parents[3] is still the root)
def _git_out(root, *args) -> str | None       # moved verbatim
def code_changes(root, since, paths=_CODE_PATHS) -> tuple[str | None, list[str]]

class Watcher:
    def __init__(self, root, check_s): ...    # root None => disabled; records HEAD at start
    def poll(self, now_monotonic) -> dict | None
```

- `Watcher(root, check_s)`: with `root` None (not a checkout) or `check_s <= 0`, `poll`
  always returns None. Otherwise it records `start_sha = _git_out(root, "rev-parse",
  "HEAD")`. If that fails, the watcher is disabled too.
- `poll(now)` returns None until `check_s` has passed since the last check. Then it calls
  `code_changes(root, sha, FOLLOWER_PATHS)`:
  - No HEAD change: returns None.
  - HEAD moved but only other paths changed (docs, specs, tests): the baseline moves to the
    new HEAD and `poll` returns None, as the runner does for docs-only commits.
  - Watched paths changed: returns
    `{"from_sha", "to_sha", "paths": changed[:50], "skills_changed": sorted({p.split("/")[1]
    for p in changed if p.startswith("skills/") and p.count("/") >= 2})}`.
- `queue.py` keeps `from .codewatch import _CODE_PATHS, code_checkout, _git_out,
  code_changes`. `queue.run` keeps calling `code_changes(watch_root, code_sha)` with the
  default paths, so the runner's behaviour is unchanged. Choice: a new module, because
  `events.py` cannot import `queue.py` at module load (queue imports events), and
  `mailbox.py` imports `events`.

### Followers

- `events.follow(cfg, reader, ..., code_root=_AUTO, code_check_s=None)` and
  `mailbox.follow(cfg, addr, ..., code_root=_AUTO, code_check_s=None)`:
  - `code_root=_AUTO` resolves to `codewatch.code_checkout()`. `None` disables the watch.
    Tests pass a temporary git repo.
  - `code_check_s` defaults to `events.settings(cfg)["code_check_s"]`. The new `DEFAULTS`
    key is `"code_check_s": 10`, so `agentic.events.code_check_s: 0` turns the watch off.
- The watcher is created after the reader lock is taken. It is polled once per loop
  iteration, **after** the batch of lines has been printed and the cursor advanced, and before
  the `stop()` check and the sleep. So every event or message already read is delivered and
  cursored before the exit, and nothing after it is consumed.
- On a change, the follower writes one line and returns the count printed. The return value
  stays an `int`. The CLI exits 0.
  - JSON mode: the envelope `{"schema": "wt.event.v1", "id": null, "ts": now, "type":
    "code_changed", "campaign": null, "spec_id": null, "project": null, "by": "follower",
    "data": {"reader", "from_sha", "to_sha", "paths", "skills_changed"}}`. The mailbox
    follower prints the same envelope; a session tells it apart from a message by `"type"`.
  - Text mode: `code_changed <from7>..<to7> skills=<a,b|-> (re-arm)`.
- The line is **not** appended to `events.jsonl`. Each follower detects the change on its
  own. A mailbox follower has no event stream. A persisted event would also wake readers
  that never ran stale code (`wt queue events --tail`). `id: null` keeps it out of cursor
  arithmetic.
- `EVENT_TYPES["code_changed"] = ("from_sha", "to_sha", "paths", "skills_changed")`
  registers the type, and the follower builds the payload through `validate`.
  `events.summary_line` renders an `id` of None as `#-`.
- CLI: `wt queue events --follow` and `wt mailbox follow` gain `--no-code-watch`, which
  passes `code_root=None`, for a human who wants a follower that never exits on a merge.
  Their help text says that the follower exits with `code_changed` when wt's code changes.

### Skills

- `skills/wt-conduct/SKILL.md`:
  - Step 4 (arm the Monitor) gains: "the follower exits after a `code_changed` line when wt's
    code changes; that is not an error".
  - New decision-table row `code_changed`: "re-arm the Monitor now, with the same command.
    If `skills_changed` has `wt-conduct`, re-invoke `/wt-conduct` (re-read it) before
    handling the next event; if it has `wt-orient`, re-read `/wt-orient` first. If the paths
    touch `src/wt/agentic/`, check the runner re-exec'd (the existing `merged` row). No
    report to the human."
- `skills/wt-steward/SKILL.md`: the same instruction in its Monitor step (step 3), with
  `wt-steward` in place of `wt-conduct`.
- `skills/wt-orient/SKILL.md` is unchanged. The re-read rule lives with the Monitor owners.

## Alternatives considered

- **Persist `code_changed` on the event stream (one writer, deduped by `to_sha`).**
  Rejected. The mailbox followers would still need their own detection, and a re-armed
  follower with an older cursor would read the event and exit again. A synthetic, per-follower
  line has none of these problems.
- **Exit only on `src/wt` changes; poll skills separately (mtime of SKILL.md).** Rejected.
  The skills sit in the same checkout, so one `git diff --name-only` covers both, and a
  skill-only merge must also wake the session.
- **Exit non-zero.** Rejected. The Monitor treats the exit as the end of the stream either
  way, and a non-zero code reads as a crash in logs and to humans.
- **Check every poll (2 s).** Rejected. That is a `git rev-parse` every 2 s per follower,
  forever. 10 s is quick enough for a merge, and `code_check_s` tunes it.

## Acceptance criteria

- [ ] `codewatch.py` holds `code_checkout`, `_git_out`, `code_changes(root, since,
      paths=_CODE_PATHS)` and `Watcher`. `queue.code_checkout` / `queue.code_changes` still
      resolve, and the runner's re-exec tests pass unchanged.
- [ ] `Watcher.poll` returns None before `code_check_s` has elapsed, with no HEAD change, and
      for a commit touching only non-watched paths (and moves its baseline). It returns
      `{from_sha, to_sha, paths, skills_changed}` for a commit under `src/wt`,
      `pyproject.toml` or `skills/`. `skills_changed` lists the skill directory names.
- [ ] `events.follow` with a watched repo prints all pending events, then exactly one
      `code_changed` envelope (`id` null, `by: follower`, the payload above) after a code
      commit, and returns. The reader's cursor equals the id of the last real event.
      Restarting the follower delivers the next event and does not repeat any.
- [ ] `mailbox.follow` behaves the same way: pending messages first, then the `code_changed`
      line, cursor at the last message's seq, and no lost or repeated messages on restart.
- [ ] `code_root=None`, `--no-code-watch`, `agentic.events.code_check_s: 0` and a
      non-checkout install each disable the watch, and the follower keeps running.
- [ ] `EVENT_TYPES` has `code_changed`, the text mode prints `code_changed <from>..<to>
      skills=…`, and `summary_line` handles `id: null`.
- [ ] `wt-conduct` and `wt-steward` skills carry the `code_changed` rule (re-arm; re-invoke own
      skill / `wt-orient` when listed in `skills_changed`).
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_codewatch.py` (new), using a temporary git repo fixture with
    `src/wt/x.py`, `skills/wt-conduct/SKILL.md` and `docs/a.md`:
    - `Watcher` with a fake monotonic clock: no change ⇒ None;
    - a docs-only commit ⇒ None, and the baseline moves (a later code commit reports only
      the code paths);
    - a `src/wt` commit ⇒ payload with an empty `skills_changed`;
    - a `skills/wt-conduct/SKILL.md` commit ⇒ `skills_changed == ["wt-conduct"]`;
    - `root=None`, `check_s=0` and a non-git dir ⇒ disabled.
  - `tests/test_agentic_events.py`:
    - `follow(..., code_root=repo, code_check_s=0.01, poll_s=0.01, out=buf)`: two events
      are appended, a code commit lands from a `stop`-style hook between iterations, then
      `follow` returns. `buf` has the two events and then one `code_changed` line, and
      `cursor_get` equals the second event's id;
    - a third event appended after the exit is delivered by a second `follow` call, which
      also stops after `max_events=1`;
    - `code_root=None` keeps looping until `stop()`;
    - `summary_line` for `id: None`.
  - `tests/test_agentic_mailbox.py`: the same exit-and-cursor test for `mailbox.follow`.
  - CLI (`CliRunner`, monkeypatched `follow`): `wt queue events --follow --for x
    --no-code-watch` in `tests/test_agentic_events.py` and `wt mailbox follow --to human
    --no-code-watch` in `tests/test_agentic_mailbox.py` pass `code_root=None`.
  - `tests/test_skills.py`: `wt-conduct` and `wt-steward` mention `code_changed` and
    `skills_changed`.
- **Manual verification:** arm `uv run wt mailbox follow --to human --text` in a worktree
  checkout, then commit a one-line change under `skills/` in that worktree. Within about 10 s
  the follower prints `code_changed … skills=…` and exits 0. Record the output in the spec.
- **Regression guard:** full `uv run pytest`, including the SPEC-0194 re-exec tests that patch
  `queue.code_checkout` / `queue.code_changes`.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

None for data. The first merge of this spec is itself a code change. The followers running
at that moment are the old code, so they do not exit, and the conductor re-arms by hand one
last time. From then on every merge makes the followers exit and the sessions re-arm by
themselves.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
