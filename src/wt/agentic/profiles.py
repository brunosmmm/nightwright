"""Workflow profiles (SPEC-0171): which model / effort / budget each persona runs with, how
many loops and slots a campaign gets, and the campaign dollar cap.

Builtins live in code. `load_config` merges user config one level deep (config.py), so a user
`agentic:` block would replace `profiles` wholesale — `resolve_profile` therefore deep-merges
the user's delta over the builtin here, and users write only what differs."""
from __future__ import annotations

import copy

BUILTIN_PROFILES = {
    "balanced": {
        "slots": 2,
        "loop_max": 3,
        "campaign_cap_usd": 10.0,
        "run_timeout_s": 1800,
        "personas": {
            "implementer": {"model": "sonnet", "effort": "high", "max_budget_usd": 4.0,
                            "permission_mode": "bypassPermissions", "read_only": False},
            "gate_triager": {"model": "sonnet", "effort": "medium", "max_budget_usd": 0.5,
                             "permission_mode": "dontAsk", "read_only": True},
        },
    },
    "frugal": {
        "slots": 1,
        "loop_max": 2,
        "campaign_cap_usd": 3.0,
        "run_timeout_s": 1200,
        "personas": {
            "implementer": {"model": "sonnet", "effort": "medium", "max_budget_usd": 1.5,
                            "permission_mode": "bypassPermissions", "read_only": False},
            "gate_triager": {"model": "haiku", "effort": "medium", "max_budget_usd": 0.25,
                             "permission_mode": "dontAsk", "read_only": True},
        },
    },
    "thorough": {
        "slots": 3,
        "loop_max": 4,
        "campaign_cap_usd": 30.0,
        "run_timeout_s": 2700,
        "personas": {
            "implementer": {"model": "opus", "effort": "high", "max_budget_usd": 10.0,
                            "permission_mode": "bypassPermissions", "read_only": False},
            "gate_triager": {"model": "sonnet", "effort": "high", "max_budget_usd": 1.0,
                             "permission_mode": "dontAsk", "read_only": True},
        },
    },
}


def deep_merge(base, delta):
    """Recursive dict merge: mappings merge key by key, anything else from `delta` wins."""
    if not isinstance(base, dict) or not isinstance(delta, dict):
        return copy.deepcopy(delta)
    out = {k: copy.deepcopy(v) for k, v in base.items()}
    for k, v in delta.items():
        out[k] = deep_merge(out[k], v) if k in out else copy.deepcopy(v)
    return out


def _agentic(cfg):
    return (cfg or {}).get("agentic") or {}


def known_profiles(cfg):
    """Builtin names plus any user-defined ones, sorted."""
    return sorted(set(BUILTIN_PROFILES) | set((_agentic(cfg).get("profiles") or {}).keys()))


def resolve_profile(cfg, name=None):
    """The effective profile dict (plus `name`). `name` defaults to `agentic.default_profile`.
    A user delta merges over the builtin of the same name, or over `balanced` for a new name.
    Unknown names with no delta raise ValueError listing the known profiles."""
    agentic = _agentic(cfg)
    name = name or agentic.get("default_profile") or "balanced"
    deltas = agentic.get("profiles") or {}
    if name not in BUILTIN_PROFILES and name not in deltas:
        raise ValueError(f"unknown profile {name!r}; known: {', '.join(known_profiles(cfg))}")
    base = BUILTIN_PROFILES.get(name, BUILTIN_PROFILES["balanced"])
    profile = deep_merge(base, deltas.get(name) or {})
    profile["name"] = name
    return profile


def persona_settings(profile, persona):
    """`profile["personas"][persona]` or ValueError naming the persona and the profile."""
    personas = profile.get("personas") or {}
    if persona not in personas:
        raise ValueError(f"profile {profile.get('name')!r} has no persona {persona!r}; "
                         f"known: {', '.join(sorted(personas))}")
    return personas[persona]
