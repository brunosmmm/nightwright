"""Claude Code permission rules for nightwright's own agent sessions (SPEC-0320).

The conductor and the stewards are long-lived Claude Code sessions that drive the queue with
`nw` commands. Under Claude Code's auto mode, a command the classifier sees as outward-facing
(e.g. `nw queue add` for a campaign that opens a GitHub PR) is refused unless an explicit allow
rule covers it, and Claude Code never lets an agent write its own permission settings. So the
rules are provisioned by the human-run install step: `nw skills install` merges ALLOW_RULES into
`permissions.allow` of the user settings file (~/.claude/settings.json), keeping every other key
and entry. User scope, because stewards run in other projects' repositories too.

Tests MUST pass a tmp `path` — never read or write the real ~/.claude/settings.json."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

_GROUPS = ("queue", "mailbox", "campaign", "escalations", "idea", "ideas", "spec", "hub", "next",
           "projects")
# The conductor and stewards address the mailbox with `NW_MAILBOX_ADDR=<addr> nw …`.
_ADDRESSED = ("queue", "mailbox", "campaign", "escalations", "idea", "spec")

ALLOW_RULES = tuple([f"Bash(nw {g} *)" for g in _GROUPS]
                    + [f"Bash(NW_MAILBOX_ADDR=* nw {g} *)" for g in _ADDRESSED])

# SPEC-0330: the exact rules the pre-rename `wt skills install` added. `nw skills install`
# removes these (and only these) so the settings file carries no dead `wt` grants.
LEGACY_RULES = tuple([f"Bash(wt {g} *)" for g in _GROUPS]
                     + [f"Bash(WT_MAILBOX_ADDR=* wt {g} *)" for g in _ADDRESSED])


# SPEC-0382: a PreToolUse hook that denies `AskUserQuestion` for a registered conductor session
# (covers a conductor started by hand, which `conductor-launch`'s --disallowedTools does not)
HOOK_EVENT = "PreToolUse"
HOOK_MATCHER = "AskUserQuestion"
# `|| true`: the guard denies through a JSON decision on stdout and exits 0; any other exit must not
# block, but click exits 2 (Claude Code's "block") for a usage error such as `No such command
# 'conductor-guard'` from an older `nw` on PATH, so the shell maps every failure to 0
HOOK_COMMAND = "nw queue conductor-guard || true"
HOOK_ENTRY = {"matcher": HOOK_MATCHER, "hooks": [{"type": "command", "command": HOOK_COMMAND}]}

# SPEC-0550: the cwd-aware git guard, a light console script (not the click CLI: it runs on every
# Bash call of every session). `|| true` for the same reason as above: only the hook's JSON
# decision may block, and a shell exit 127 (script not on PATH) must not.
GIT_GUARD_EXECUTABLE = "nw-conductor-git-guard"
GIT_GUARD_COMMAND = f"{GIT_GUARD_EXECUTABLE} || true"
GIT_GUARD_ENTRY = {"matcher": "Bash",
                   "hooks": [{"type": "command", "command": GIT_GUARD_COMMAND}]}


def default_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _load(path: Path) -> dict:
    """The settings document, {} when the file is absent. ValueError (never a rewrite) when it
    is not a JSON object or `permissions.allow` is not a list."""
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: not valid JSON ({e}); fix it before installing permissions")
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a JSON object")
    allow = (doc.get("permissions") or {}).get("allow", [])
    if not isinstance(doc.get("permissions", {}), dict) or not isinstance(allow, list):
        raise ValueError(f"{path}: permissions.allow must be a list")
    hooks = doc.get("hooks")
    hooks = {} if hooks is None else hooks      # `"hooks": null` is treated as {}
    if not isinstance(hooks, dict) or not isinstance(hooks.get(HOOK_EVENT, []), list):
        raise ValueError(f"{path}: hooks.{HOOK_EVENT} must be a list")
    return doc


def _has_hook(doc, command=HOOK_COMMAND) -> bool:
    """True when some PreToolUse entry already runs `command`."""
    for entry in (doc.get("hooks") or {}).get(HOOK_EVENT, []):
        for h in (entry.get("hooks") or []) if isinstance(entry, dict) else []:
            if isinstance(h, dict) and h.get("command") == command:
                return True
    return False


def hook_missing(path=None) -> bool:
    """True when the conductor-guard hook is not yet in the settings file."""
    return not _has_hook(_load(Path(path or default_settings_path())))


def git_guard_hook_missing(path=None) -> bool:
    """True when the SPEC-0550 git-guard hook is not yet in the settings file."""
    return not _has_hook(_load(Path(path or default_settings_path())), GIT_GUARD_COMMAND)


def git_guard_executable(env=None):
    """Path of the `nw-conductor-git-guard` console script on `env`'s PATH (default: the
    process's), else None. The settings command ends in `|| true`, so a script that is not on
    the PATH Claude Code runs hooks with would never run and never complain."""
    path = (os.environ if env is None else env).get("PATH")
    return shutil.which(GIT_GUARD_EXECUTABLE, path=path)


def missing(path=None) -> list[str]:
    """ALLOW_RULES not yet present in the settings file, in ALLOW_RULES order."""
    path = Path(path or default_settings_path())
    present = set((_load(path).get("permissions") or {}).get("allow", []))
    return [r for r in ALLOW_RULES if r not in present]


def legacy_present(path=None) -> list[str]:
    """LEGACY_RULES (pre-rename `wt` grants) still present in the settings file."""
    path = Path(path or default_settings_path())
    present = set((_load(path).get("permissions") or {}).get("allow", []))
    return [r for r in LEGACY_RULES if r in present]


def install(path=None) -> list[str]:
    """`sync()`, returning only the added rules."""
    return sync(path)["added"]


def sync(path=None) -> dict:
    """Merge the missing ALLOW_RULES into `permissions.allow` and drop every LEGACY_RULES entry
    (SPEC-0330), and add the conductor-guard PreToolUse hook (SPEC-0382) unless present; returns
    {"added": [...], "removed": [...], "hook": bool (added now), "git_guard_hook": bool (the
    SPEC-0550 Bash git-guard entry, added now)}. When there is nothing to do the
    file is left untouched. Other keys and entries are kept as they are; the write is atomic."""
    path = Path(path or default_settings_path())
    doc = _load(path)
    added = missing(path)
    legacy = set(LEGACY_RULES)
    current = list((doc.get("permissions") or {}).get("allow", []))
    removed = [r for r in current if r in legacy]
    hook = not _has_hook(doc)
    git_guard = not _has_hook(doc, GIT_GUARD_COMMAND)
    if not added and not removed and not hook and not git_guard:
        return {"added": [], "removed": [], "hook": False, "git_guard_hook": False}
    if added or removed:
        perms = doc.setdefault("permissions", {})
        perms["allow"] = [r for r in current if r not in legacy] + added
    for wanted, entry in ((hook, HOOK_ENTRY), (git_guard, GIT_GUARD_ENTRY)):
        if wanted:
            if doc.get("hooks") is None:
                doc["hooks"] = {}
            doc["hooks"].setdefault(HOOK_EVENT, []).append(json.loads(json.dumps(entry)))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return {"added": added, "removed": removed, "hook": hook, "git_guard_hook": git_guard}
