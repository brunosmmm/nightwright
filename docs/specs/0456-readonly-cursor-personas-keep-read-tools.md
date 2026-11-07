---
id: SPEC-0456
title: Read-only Cursor personas keep the file-inspection tools
status: done
owner: user
created: 2026-11-07
updated: 2026-11-07
kind: feature
tags: [agentic, cursor]
depends_on: [SPEC-0442]
---

## Context

SPEC-0442 started a read-only Cursor persona with `tools=[]`. The SDK defines an
empty list as no built-in tools. Writers pass `tools=None`, which keeps the
default toolset. Claude reviewers still get `--restricted`, so they can read and
cannot write.

The reviewer prompt says the persona has read-only tools. On campaign IDEA-967,
run `012-conformance_reviewer` spent about 22 minutes and 114,721 reasoning
tokens, called no tool, and finished with a sentence of intention instead of a
verdict. IDEA-1025.

## Goals / Non-goals

**Goals**
- A read-only Cursor persona can read and search the worktree.
- It cannot edit files or run a shell.
- A write persona still gets the SDK's default toolset.

**Non-goals**
- Changing Claude's `--restricted` launch.
- Giving reviewers WebSearch, Task, or Shell.
- Rewriting SPEC-0442. That spec still governs the SDK child. This one replaces
  its empty-tool rule.

## Decision

A read-only run passes `tools=["Read", "Grep", "Glob", "LS"]`. Those are the
file tools the SDK marks read-only. Write, StrReplace, Delete, and Shell stay
off. A write run still passes `tools=None`.

An allowlist is the restriction. Denying write names would leave any new write
tool available. Shell stays off because a shell can change the tree.

## Design

`agent_tools` in `src/nightwright/agentic/cursor_exec.py` returns that list when
`read_only` is set, and `None` otherwise. `_default_create` passes the return
value as `AgentOptions.tools`.

## Alternatives considered

- **`tools=[]`.** This is the bug. The SDK treats it as no tools.
- **`disallowed_tools` for Write, StrReplace, Delete, and Shell.** A tool the
  list forgets stays available.
- **`mode: plan`.** The SDK mode is plan or agent. Plan is not a read-only
  reviewer.

## Acceptance criteria

- [x] A read-only create passes `Read`, `Grep`, `Glob`, and `LS`, and no other tool.
- [x] A write create passes `tools=None`.
- [x] The product description no longer says read-only personas run with no tools.

## Test plan

- **Automated tests:** `tests/test_cursor_sdk_exec.py` covers both create paths
  with a fake SDK module.
- **Manual verification:** none. The next read-only persona spawn picks this up.
  A run already in progress keeps the empty tool list.
- **Regression guard:** the rest of `tests/test_cursor_sdk_exec.py`.

## Rollout / migration

None. Restart is not required for the queue process. The next persona child
imports the new module.

## As shipped

`agent_tools(True)` returns `Read`, `Grep`, `Glob`, and `LS`. `agent_tools(False)` returns `None`. `_default_create` passes that value through. `tests/test_cursor_sdk_exec.py`: 14 passed.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed.
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` regenerated.

## Open questions

None. The tool names are the SDK's read-only file tools.
