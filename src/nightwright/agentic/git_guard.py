"""`nw-conductor-git-guard` (SPEC-0550): a PreToolUse hook on `Bash` that denies raw `git` / `cp`
writes into a MAIN checkout for a registered conductor session, and the `main-dirty-apply` verb
without `--trivial-only`.

A light script on purpose: it runs on every Bash call of every session on the machine, so this
module imports only json, os, sys, pathlib and `nightwright.paths` (never click, yaml, rich, the
store or the config), reads the conductor session registry itself, and returns silently unless
the session matches. The tokenizer and the git calls live in `git_guard_parse`, imported only
after the match. Always exits 0; a decision is the PreToolUse JSON on stdout."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .. import paths


def _registry_path() -> Path:
    return Path(paths.data_dir()) / "agentic" / "conductor" / "sessions.jsonl"


def _is_conductor(session_id) -> bool:
    """Mirrors `conductor_cost.sessions(cfg, "conductor")`: a line counts when it is a JSON
    object with a truthy `session_id` and `reader == "conductor"`; bad lines and a final line
    without a newline are skipped; a missing or unreadable file is no match."""
    if not session_id or not isinstance(session_id, str):
        return False
    try:
        with _registry_path().open("rb") as stream:
            while True:
                raw = stream.readline()
                if not raw or not raw.endswith(b"\n"):
                    return False
                try:
                    rec = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError):
                    continue
                if (isinstance(rec, dict) and rec.get("reader") == "conductor"
                        and rec.get("session_id") == session_id):
                    return True
    except OSError:
        return False


def _deny(reason: str) -> None:
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))
    sys.stdout.write("\n")


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            return
        command = (payload.get("tool_input") or {}).get("command")
        if not command or not isinstance(command, str) or not _is_conductor(
                payload.get("session_id")):
            return
    except BaseException:           # fail open: never block a human's session (SPEC-0382)
        return
    try:
        from . import git_guard_parse
        reason = git_guard_parse.decide(command, payload.get("cwd"), os.environ)
    except BaseException:           # a conductor session: fail closed
        reason = "conductor-git-guard: cannot verify internal error (SPEC-0550)"
    if reason:
        _deny(reason)


if __name__ == "__main__":
    main()
