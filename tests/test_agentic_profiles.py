"""SPEC-0171: workflow profiles — builtins in code, user deltas deep-merged over them."""
import pytest

from wt.agentic import profiles as P
from wt.config import DEFAULT_CONFIG

PERSONA_KEYS = {"model", "effort", "max_budget_usd", "permission_mode", "read_only"}
PROFILE_KEYS = {"slots", "loop_max", "campaign_cap_usd", "run_timeout_s", "personas"}


def test_builtins_are_complete():
    for name, prof in P.BUILTIN_PROFILES.items():
        assert PROFILE_KEYS <= set(prof), name
        for persona, settings in prof["personas"].items():
            assert PERSONA_KEYS <= set(settings), (name, persona)
    assert P.BUILTIN_PROFILES["balanced"]["personas"]["gate_triager"]["read_only"] is True


def test_default_config_has_agentic_block():
    a = DEFAULT_CONFIG["agentic"]
    assert a["default_profile"] == "balanced"
    assert a["runner"] == "claude" and a["profiles"] == {}
    assert a["lease_ttl_s"] > 0 and a["poll_interval_s"] > 0


def test_resolve_default_is_balanced():
    prof = P.resolve_profile({"agentic": dict(DEFAULT_CONFIG["agentic"])})
    assert prof["name"] == "balanced"
    assert prof == {**P.BUILTIN_PROFILES["balanced"], "name": "balanced"}
    # a resolved profile is a copy — mutating it must not leak into the builtin
    prof["personas"]["implementer"]["model"] = "x"
    assert P.BUILTIN_PROFILES["balanced"]["personas"]["implementer"]["model"] == "sonnet"


def test_user_delta_keeps_every_other_builtin_key():
    cfg = {"agentic": {"profiles": {"balanced": {
        "personas": {"implementer": {"max_budget_usd": 6.0}}}}}}
    prof = P.resolve_profile(cfg, "balanced")
    assert prof["personas"]["implementer"]["max_budget_usd"] == 6.0
    assert prof["personas"]["implementer"]["model"] == "sonnet"       # untouched sibling key
    assert prof["personas"]["gate_triager"] == P.BUILTIN_PROFILES["balanced"]["personas"]["gate_triager"]
    assert prof["slots"] == 2 and prof["loop_max"] == 3


def test_new_user_profile_merges_over_balanced():
    cfg = {"agentic": {"profiles": {"mine": {"slots": 1, "campaign_cap_usd": 2.0}}}}
    prof = P.resolve_profile(cfg, "mine")
    assert prof["name"] == "mine" and prof["slots"] == 1 and prof["campaign_cap_usd"] == 2.0
    assert prof["personas"] == P.BUILTIN_PROFILES["balanced"]["personas"]
    assert "mine" in P.known_profiles(cfg)


def test_default_profile_key_selects_name():
    cfg = {"agentic": {"default_profile": "frugal", "profiles": {}}}
    assert P.resolve_profile(cfg)["name"] == "frugal"


def test_unknown_profile_errors_with_known_list():
    with pytest.raises(ValueError, match="unknown profile 'nope'; known: balanced, frugal, thorough"):
        P.resolve_profile({"agentic": {"profiles": {}}}, "nope")


def test_persona_settings():
    prof = P.resolve_profile({}, "balanced")
    assert P.persona_settings(prof, "implementer")["permission_mode"] == "bypassPermissions"
    with pytest.raises(ValueError, match="no persona 'reviewer'"):
        P.persona_settings(prof, "reviewer")


def test_deep_merge_replaces_non_dicts():
    assert P.deep_merge({"a": {"b": 1, "c": [1]}, "d": 1}, {"a": {"c": [2]}, "e": 2}) == \
        {"a": {"b": 1, "c": [2]}, "d": 1, "e": 2}
