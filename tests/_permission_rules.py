"""Claude Code permission-rule matching for tests (SPEC-0550; SPEC-0551 reuses it).

`decide(command, allow, deny)` follows the documented semantics for `Bash(<pattern>)` rules:
`*` matches any characters including spaces, the whole command must match, and a deny rule
beats an allow rule. It returns "deny", "allow", or "ask" (nothing matched). The real binary
is the arbiter; this keeps the static half of a test cheap."""
from __future__ import annotations

import re


def _pattern(rule: str):
    if not (rule.startswith("Bash(") and rule.endswith(")")):
        return None
    inner = rule[len("Bash("):-1]
    return re.compile("".join(".*" if c == "*" else re.escape(c) for c in inner), re.S)


def matches(command: str, rules) -> bool:
    for rule in rules:
        pat = _pattern(rule)
        if pat is not None and pat.fullmatch(command):
            return True
    return False


def decide(command: str, allow, deny) -> str:
    if matches(command, deny):
        return "deny"
    if matches(command, allow):
        return "allow"
    return "ask"
