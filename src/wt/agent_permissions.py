"""Claude Code permission rules for wt's own agent sessions (SPEC-0320).

The conductor and the stewards are long-lived Claude Code sessions that drive the queue with
`wt` commands. Under Claude Code's auto mode, a command the classifier sees as outward-facing
(e.g. `wt queue add` for a campaign that opens a GitHub PR) is refused unless an explicit allow
rule covers it, and Claude Code never lets an agent write its own permission settings. So the
rules are provisioned by the human-run install step: `wt skills install` merges ALLOW_RULES into
`permissions.allow` of the user settings file (~/.claude/settings.json), keeping every other key
and entry. User scope, because stewards run in other projects' repositories too.

Tests MUST pass a tmp `path` — never read or write the real ~/.claude/settings.json."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

_GROUPS = ("queue", "mailbox", "campaign", "escalations", "idea", "ideas", "spec", "hub", "next",
           "projects")
# The conductor and stewards address the mailbox with `WT_MAILBOX_ADDR=<addr> wt …`.
_ADDRESSED = ("queue", "mailbox", "campaign", "escalations", "idea", "spec")

ALLOW_RULES = tuple([f"Bash(wt {g} *)" for g in _GROUPS]
                    + [f"Bash(WT_MAILBOX_ADDR=* wt {g} *)" for g in _ADDRESSED])


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
    return doc


def missing(path=None) -> list[str]:
    """ALLOW_RULES not yet present in the settings file, in ALLOW_RULES order."""
    path = Path(path or default_settings_path())
    present = set((_load(path).get("permissions") or {}).get("allow", []))
    return [r for r in ALLOW_RULES if r not in present]


def install(path=None) -> list[str]:
    """Merge the missing ALLOW_RULES into `permissions.allow` and return them; [] when all are
    present (the file is then left untouched). Other keys and entries are kept as they are; the
    write is atomic."""
    path = Path(path or default_settings_path())
    doc = _load(path)
    added = missing(path)
    if not added:
        return []
    perms = doc.setdefault("permissions", {})
    perms["allow"] = list(perms.get("allow", [])) + added
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
    return added
