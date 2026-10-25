"""Workflow profiles (SPEC-0171): which model / effort / budget each persona runs with, how
many loops and slots a campaign gets, and the campaign dollar cap.

Builtins live in code. `load_config` merges user config one level deep (config.py), so a user
`agentic:` block would replace `profiles` wholesale — `resolve_profile` therefore deep-merges
the user's delta over the builtin here, and users write only what differs."""
from __future__ import annotations

import copy

# Canonical reviewer order (SPEC-0205, SPEC-0202): a profile's default `reviewers` list follows it.
REVIEWER_ORDER = ("conformance_reviewer", "test_reviewer", "adversarial_reviewer")

# SPEC-0203: severity weights and the pass threshold of the review triage score.
SEVERITIES = ("must_fix", "major", "minor", "nit")
DEFAULT_WEIGHTS = {"must_fix": 100, "major": 15, "minor": 4, "nit": 1}
DEFAULT_PASS_SCORE = 80
REVIEW_KEYS = frozenset({"pass_score", "weights", "triager", "followups", "followups_nits"})

BUILTIN_PROFILES = {
    "balanced": {
        "review": {"pass_score": 80, "weights": dict(DEFAULT_WEIGHTS), "triager": True,
                   "followups": True, "followups_nits": False},
        "slots": 2,
        "loop_max": 3,
        "campaign_cap_usd": 10.0,
        "run_timeout_s": 1800,
        "reviewers": ["conformance_reviewer", "test_reviewer", "adversarial_reviewer"],
        "personas": {
            "implementer": {"model": "sonnet", "effort": "high", "max_budget_usd": 4.0,
                            "permission_mode": "bypassPermissions", "read_only": False},
            "gate_triager": {"model": "sonnet", "effort": "medium", "max_budget_usd": 0.5,
                             "permission_mode": "dontAsk", "read_only": True},
            "conformance_reviewer": {"model": "opus", "effort": "high", "max_budget_usd": 1.5,
                                     "permission_mode": "dontAsk", "read_only": True},
            "test_reviewer": {"model": "opus", "effort": "high", "max_budget_usd": 1.0,
                              "permission_mode": "dontAsk", "read_only": True},
            "adversarial_reviewer": {"model": "opus", "effort": "high", "max_budget_usd": 1.5,
                                     "permission_mode": "dontAsk", "read_only": True},
            "spec_reviewer": {"model": "sonnet", "effort": "high", "max_budget_usd": 0.8,
                              "permission_mode": "dontAsk", "read_only": True},
            "review_triager": {"model": "sonnet", "effort": "medium", "max_budget_usd": 0.5,
                               "permission_mode": "dontAsk", "read_only": True},
        },
    },
    "frugal": {
        "review": {"pass_score": 70, "weights": dict(DEFAULT_WEIGHTS), "triager": False,
                   "followups": True, "followups_nits": False},
        "slots": 1,
        "loop_max": 2,
        "campaign_cap_usd": 3.0,
        "run_timeout_s": 1200,
        "reviewers": ["conformance_reviewer"],
        "personas": {
            "implementer": {"model": "sonnet", "effort": "medium", "max_budget_usd": 1.5,
                            "permission_mode": "bypassPermissions", "read_only": False},
            "gate_triager": {"model": "haiku", "effort": "medium", "max_budget_usd": 0.25,
                             "permission_mode": "dontAsk", "read_only": True},
            "conformance_reviewer": {"model": "sonnet", "effort": "medium", "max_budget_usd": 0.6,
                                     "permission_mode": "dontAsk", "read_only": True},
            "test_reviewer": {"model": "sonnet", "effort": "medium", "max_budget_usd": 0.5,
                              "permission_mode": "dontAsk", "read_only": True},
            "adversarial_reviewer": {"model": "sonnet", "effort": "medium", "max_budget_usd": 0.6,
                                     "permission_mode": "dontAsk", "read_only": True},
            "spec_reviewer": {"model": "sonnet", "effort": "medium", "max_budget_usd": 0.4,
                              "permission_mode": "dontAsk", "read_only": True},
            "review_triager": {"model": "haiku", "effort": "medium", "max_budget_usd": 0.25,
                               "permission_mode": "dontAsk", "read_only": True},
        },
    },
    "thorough": {
        "review": {"pass_score": 90, "weights": dict(DEFAULT_WEIGHTS), "triager": True,
                   "followups": True, "followups_nits": False},
        "slots": 3,
        "loop_max": 4,
        "campaign_cap_usd": 30.0,
        "run_timeout_s": 2700,
        "reviewers": ["conformance_reviewer", "test_reviewer", "adversarial_reviewer"],
        "personas": {
            "implementer": {"model": "opus", "effort": "high", "max_budget_usd": 10.0,
                            "permission_mode": "bypassPermissions", "read_only": False},
            "gate_triager": {"model": "sonnet", "effort": "high", "max_budget_usd": 1.0,
                             "permission_mode": "dontAsk", "read_only": True},
            "conformance_reviewer": {"model": "opus", "effort": "high", "max_budget_usd": 1.5,
                                     "permission_mode": "dontAsk", "read_only": True},
            "test_reviewer": {"model": "opus", "effort": "high", "max_budget_usd": 1.0,
                              "permission_mode": "dontAsk", "read_only": True},
            "adversarial_reviewer": {"model": "opus", "effort": "high", "max_budget_usd": 2.5,
                                     "permission_mode": "dontAsk", "read_only": True},
            "spec_reviewer": {"model": "opus", "effort": "high", "max_budget_usd": 1.5,
                              "permission_mode": "dontAsk", "read_only": True},
            "review_triager": {"model": "sonnet", "effort": "medium", "max_budget_usd": 0.5,
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
    _check_reviewers(profile)
    _check_review(profile)
    return profile


# Spec sizing and tone pick the profile (SPEC-0219): tone -> size -> profile. `xl` is "too big
# for one campaign" (the review verdict is `split`), so it has no cell.
SIZES = ("xs", "s", "m", "l", "xl")
TONES = ("cost-effective", "high-end", "critical")
PROFILE_MATRIX = {
    "cost-effective": {"xs": "frugal", "s": "frugal", "m": "balanced", "l": "balanced"},
    "high-end": {"xs": "balanced", "s": "balanced", "m": "balanced", "l": "thorough"},
    "critical": {"xs": "thorough", "s": "thorough", "m": "thorough", "l": "thorough"},
}


def profile_matrix(cfg):
    """The builtin matrix with `agentic.profile_matrix` deep-merged over it. ValueError naming
    the cell for an unknown tone, size or profile, or for an `xl` cell."""
    delta = _agentic(cfg).get("profile_matrix") or {}
    matrix = deep_merge(PROFILE_MATRIX, delta)
    known = known_profiles(cfg)
    for tone, row in delta.items():
        if tone not in TONES:
            raise ValueError(f"profile_matrix: unknown tone {tone!r}; known: {', '.join(TONES)}")
        if not isinstance(row, dict):
            raise ValueError(f"profile_matrix[{tone}]: expected a mapping of size to profile")
        for size, name in row.items():
            if size == "xl":
                raise ValueError(f"profile_matrix[{tone}][xl]: xl has no profile "
                                 f"(too big for one campaign: split it)")
            if size not in SIZES:
                raise ValueError(f"profile_matrix[{tone}][{size}]: unknown size {size!r}; "
                                 f"known: {', '.join(s for s in SIZES if s != 'xl')}")
            if name not in known:
                raise ValueError(f"profile_matrix[{tone}][{size}]: unknown profile {name!r}; "
                                 f"known: {', '.join(known)}")
    return matrix


def select_profile(cfg, *, flag=None, review=None) -> dict:
    """Pick the campaign profile: `--profile` flag > the approving review's size and tone via
    the matrix > `agentic.default_profile`. Pure (no I/O beyond `cfg`). Returns
    `{profile, source: flag|review|default, reason, size, tone}`; size and tone are recorded
    whenever the review has valid ones, even when the flag wins."""
    matrix = profile_matrix(cfg)
    review = review or {}
    size, tone = review.get("size"), review.get("tone")
    sized = (size in SIZES and tone in TONES) and review.get("verdict", "approve") == "approve"
    if not sized:
        size = tone = None
    if flag:
        return {"profile": flag, "source": "flag", "reason": f"--profile {flag}",
                "size": size, "tone": tone}
    if sized and size != "xl":
        name = matrix[tone][size]
        return {"profile": name, "source": "review", "size": size, "tone": tone,
                "reason": f"size {size}, tone {tone} → {name}"}
    default = _agentic(cfg).get("default_profile") or "balanced"
    if not review:
        why = "no approving review (--force)"
    elif sized:
        why = f"size {size} has no profile (split recommended)"
    else:
        why = "review predates sizing (no size/tone)"
    return {"profile": default, "source": "default", "reason": why, "size": size, "tone": tone}


def reviewers_of(profile):
    """The effective reviewer personas for REVIEWING (SPEC-0201): `profile["reviewers"]`, or
    `["conformance_reviewer"]` when the key is absent or empty (SPEC-0187 behavior)."""
    return list(profile.get("reviewers") or []) or ["conformance_reviewer"]


def _check_reviewers(profile):
    """ValueError naming the persona and the profile for a `reviewers` entry that is unknown,
    duplicated, not a registered reviewer, not read-only, or has no settings in the profile."""
    from . import personas as PS      # personas imports this module
    name = profile.get("name")
    seen = set()
    for r in profile.get("reviewers") or []:
        if r in seen:
            raise ValueError(f"profile {name!r}: reviewer {r!r} is listed twice")
        seen.add(r)
        if r not in PS.PERSONAS:
            raise ValueError(f"profile {name!r}: unknown reviewer persona {r!r}")
        if r not in PS.REVIEWER_PERSONAS:
            raise ValueError(f"profile {name!r}: persona {r!r} is not a reviewer persona")
        settings = (profile.get("personas") or {}).get(r)
        if settings is None:
            raise ValueError(f"profile {name!r}: reviewer {r!r} has no persona settings")
        if not PS.PERSONAS[r].read_only or not settings.get("read_only", True):
            raise ValueError(f"profile {name!r}: reviewer {r!r} must be read_only")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_review(profile):
    """ValueError naming the key for a bad `review:` block (SPEC-0203): only `pass_score`
    (int 0..100), `weights` (exactly the four severities, ints >= 0; no bools), `triager`
    (bool, SPEC-0206) and the SPEC-0207 booleans `followups` / `followups_nits`."""
    name = profile.get("name")
    review = profile.get("review")
    if review is None:
        return
    if not isinstance(review, dict):
        raise ValueError(f"profile {name!r}: review must be a mapping")
    for key in review:
        if key not in REVIEW_KEYS:
            raise ValueError(f"profile {name!r}: unknown key review.{key}")
    if "pass_score" in review:
        ps = review["pass_score"]
        if not _is_int(ps) or not 0 <= ps <= 100:
            raise ValueError(f"profile {name!r}: review.pass_score must be an integer 0..100, "
                             f"got {ps!r}")
    if "triager" in review and not isinstance(review["triager"], bool):
        raise ValueError(f"profile {name!r}: review.triager must be true or false, "
                         f"got {review['triager']!r}")
    for key in ("followups", "followups_nits"):
        if key in review and not isinstance(review[key], bool):
            raise ValueError(f"profile {name!r}: review.{key} must be a boolean, "
                             f"got {review[key]!r}")
    if "weights" in review:
        weights = review["weights"]
        if not isinstance(weights, dict):
            raise ValueError(f"profile {name!r}: review.weights must be a mapping")
        for key in weights:
            if key not in SEVERITIES:
                raise ValueError(f"profile {name!r}: unknown key review.weights.{key}")
        for key in SEVERITIES:
            if key not in weights:
                raise ValueError(f"profile {name!r}: review.weights.{key} is missing")
            if not _is_int(weights[key]) or weights[key] < 0:
                raise ValueError(f"profile {name!r}: review.weights.{key} must be an integer "
                                 f">= 0, got {weights[key]!r}")


def review_params(profile) -> tuple:
    """`(pass_score, weights)` of `profile["review"]`; a hand-built profile without it (or
    with part of it) uses the `balanced` values."""
    review = (profile or {}).get("review") or {}
    ps = review.get("pass_score")
    weights = dict(DEFAULT_WEIGHTS)
    weights.update(review.get("weights") or {})
    return (DEFAULT_PASS_SCORE if ps is None else ps), weights


def triager_enabled(profile) -> bool:
    """Is the review triager (SPEC-0206) on? A hand-built profile without `review.triager`
    has it off."""
    review = (profile or {}).get("review")
    return isinstance(review, dict) and review.get("triager") is True


def followup_params(profile) -> tuple:
    """`(followups, followups_nits)` of `profile["review"]` (SPEC-0207); a hand-built profile
    without them behaves as the defaults (file follow-ups, no nits)."""
    review = (profile or {}).get("review") or {}
    return bool(review.get("followups", True)), bool(review.get("followups_nits", False))


def persona_settings(profile, persona):
    """`profile["personas"][persona]` or ValueError naming the persona and the profile."""
    personas = profile.get("personas") or {}
    if persona not in personas:
        raise ValueError(f"profile {profile.get('name')!r} has no persona {persona!r}; "
                         f"known: {', '.join(sorted(personas))}")
    return personas[persona]
