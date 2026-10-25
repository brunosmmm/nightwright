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


# ---- SPEC-0201: reviewers --------------------------------------------------------------------

@pytest.mark.parametrize("name,want", [("balanced", ["conformance_reviewer", "test_reviewer"]),
                                       ("thorough", ["conformance_reviewer", "test_reviewer"]),
                                       ("frugal", ["conformance_reviewer"])])
def test_reviewers_default_lists(name, want):
    got = P.resolve_profile({}, name)["reviewers"]
    assert got == want
    order = [p for p in P.REVIEWER_ORDER if p in got]
    assert got == order                                  # canonical order (SPEC-0205/0202)
    if "test_reviewer" in got:
        assert got.index("test_reviewer") == got.index("conformance_reviewer") + 1


@pytest.mark.parametrize("name,model,effort,budget", [("balanced", "opus", "high", 1.0),
                                                      ("thorough", "opus", "high", 1.0),
                                                      ("frugal", "sonnet", "medium", 0.5)])
def test_test_reviewer_settings(name, model, effort, budget):
    s = P.resolve_profile({}, name)["personas"]["test_reviewer"]
    assert (s["model"], s["effort"], s["max_budget_usd"]) == (model, effort, budget)
    assert s["read_only"] is True and s["permission_mode"] == "dontAsk"


def test_test_reviewer_frugal_opt_in_by_user_delta():
    cfg = {"agentic": {"profiles": {"frugal": {
        "reviewers": ["conformance_reviewer", "test_reviewer"]}}}}
    prof = P.resolve_profile(cfg, "frugal")
    assert prof["reviewers"] == ["conformance_reviewer", "test_reviewer"]
    assert prof["personas"]["test_reviewer"]["max_budget_usd"] == 0.5


def test_docs_list_the_test_reviewer():
    from pathlib import Path
    doc = Path(__file__).resolve().parent.parent / "docs/product/capabilities/agentic.md"
    assert "test_reviewer" in doc.read_text(encoding="utf-8")


def test_reviewers_of_falls_back_to_conformance():
    assert P.reviewers_of({}) == ["conformance_reviewer"]
    assert P.reviewers_of({"reviewers": []}) == ["conformance_reviewer"]
    assert P.reviewers_of({"reviewers": ["x"]}) == ["x"]


@pytest.mark.parametrize("reviewers,needle", [
    (["nobody"], "nobody"),
    (["conformance_reviewer", "conformance_reviewer"], "twice"),
    (["implementer"], "implementer"),          # not read-only, not a reviewer persona
    (["gate_triager"], "gate_triager"),        # read-only but not in REVIEWER_PERSONAS
])
def test_reviewers_rejects_bad_entries(reviewers, needle):
    cfg = {"agentic": {"profiles": {"balanced": {"reviewers": reviewers}}}}
    with pytest.raises(ValueError) as e:
        P.resolve_profile(cfg, "balanced")
    assert needle in str(e.value) and "balanced" in str(e.value)


def test_reviewers_rejects_non_read_only_and_missing_settings(monkeypatch):
    from wt.agentic import personas as PS
    monkeypatch.setitem(PS.PERSONAS, "extra", PS.PERSONAS["conformance_reviewer"])
    monkeypatch.setattr(PS, "REVIEWER_PERSONAS", ("conformance_reviewer", "extra"))
    cfg = {"agentic": {"profiles": {"balanced": {"reviewers": ["extra"]}}}}
    with pytest.raises(ValueError, match="extra.*no persona settings"):
        P.resolve_profile(cfg, "balanced")
    cfg["agentic"]["profiles"]["balanced"]["personas"] = {"extra": {"read_only": False}}
    with pytest.raises(ValueError, match="extra.*read_only"):
        P.resolve_profile(cfg, "balanced")


def test_reviewers_user_delta_replaces_the_list(monkeypatch):
    from wt.agentic import personas as PS
    monkeypatch.setitem(PS.PERSONAS, "extra", PS.PERSONAS["conformance_reviewer"])
    monkeypatch.setattr(PS, "REVIEWER_PERSONAS", ("conformance_reviewer", "extra"))
    extra = {"model": "sonnet", "effort": "low", "max_budget_usd": 1.0, "read_only": True}
    cfg = {"agentic": {"profiles": {"balanced": {"reviewers": ["extra"],
                                                 "personas": {"extra": extra}}}}}
    assert P.resolve_profile(cfg, "balanced")["reviewers"] == ["extra"]


# ---- review: block (SPEC-0203) -------------------------------------------------------------

DEFAULT_WEIGHTS = {"must_fix": 100, "major": 15, "minor": 4, "nit": 1}


@pytest.mark.parametrize("name,score", [("balanced", 80), ("thorough", 90), ("frugal", 70)])
def test_review_defaults(name, score):
    prof = P.resolve_profile({"agentic": {"profiles": {}}}, name)
    assert prof["review"] == {"pass_score": score, "weights": DEFAULT_WEIGHTS,
                              "triager": name != "frugal"}
    assert P.review_params(prof) == (score, DEFAULT_WEIGHTS)
    assert P.review_params({}) == (80, DEFAULT_WEIGHTS)
    prof["review"]["weights"]["major"] = 1                      # a copy, not the builtin
    assert P.BUILTIN_PROFILES[name]["review"]["weights"]["major"] == 15


def test_review_user_delta_merges_partial_weights():
    cfg = {"agentic": {"profiles": {"balanced": {"review": {"weights": {"nit": 0},
                                                            "pass_score": 60}}}}}
    prof = P.resolve_profile(cfg, "balanced")
    assert prof["review"] == {"pass_score": 60, "weights": {**DEFAULT_WEIGHTS, "nit": 0},
                              "triager": True}
    new = P.resolve_profile({"agentic": {"profiles": {"mine": {"slots": 1}}}}, "mine")
    assert new["review"]["pass_score"] == 80


@pytest.mark.parametrize("review,needle", [
    ({"bogus": 1}, "review.bogus"),
    ({"pass_score": True}, "review.pass_score"),
    ({"pass_score": 101}, "review.pass_score"),
    ({"pass_score": -1}, "review.pass_score"),
    ({"pass_score": "80"}, "review.pass_score"),
    ({"pass_score": 80.5}, "review.pass_score"),
    ({"weights": {"extra": 1}}, "review.weights.extra"),
    ({"weights": {"major": True}}, "review.weights.major"),
    ({"weights": {"major": -1}}, "review.weights.major"),
    ({"weights": {"minor": 1.5}}, "review.weights.minor"),
    ({"weights": [1]}, "review.weights"),
])
def test_review_rejects_bad_values(review, needle):
    cfg = {"agentic": {"profiles": {"balanced": {"review": review}}}}
    with pytest.raises(ValueError, match=needle.replace(".", r"\.")):
        P.resolve_profile(cfg, "balanced")


def test_review_missing_weight_key_is_rejected():
    prof = {"name": "x", "review": {"weights": {"must_fix": 100}}}
    with pytest.raises(ValueError, match=r"review\.weights\.major"):
        P._check_review(prof)


def test_docs_describe_the_review_block():
    from pathlib import Path
    doc = (Path(__file__).resolve().parent.parent / "docs/product/capabilities/agentic.md"
           ).read_text(encoding="utf-8")
    for needle in ("pass_score", "advisory", "entry_run", "weights"):
        assert needle in doc


# ---- SPEC-0206: review triager ---------------------------------------------------------------

@pytest.mark.parametrize("name,on", [("balanced", True), ("thorough", True), ("frugal", False)])
def test_review_triager_defaults(name, on):
    prof = P.resolve_profile({}, name)
    assert prof["review"]["triager"] is on and P.triager_enabled(prof) is on


@pytest.mark.parametrize("name,model,effort,budget", [
    ("balanced", "sonnet", "medium", 0.5), ("thorough", "sonnet", "medium", 0.5),
    ("frugal", "haiku", "medium", 0.25)])
def test_review_triager_settings(name, model, effort, budget):
    s = P.persona_settings(P.resolve_profile({}, name), "review_triager")
    assert (s["model"], s["effort"], s["max_budget_usd"]) == (model, effort, budget)
    assert s["read_only"] is True and s["permission_mode"] == "dontAsk"


def test_review_triager_user_toggle_and_hand_built_profile():
    cfg = {"agentic": {"profiles": {"frugal": {"review": {"triager": True}},
                                    "balanced": {"review": {"triager": False}}}}}
    assert P.triager_enabled(P.resolve_profile(cfg, "frugal"))
    assert not P.triager_enabled(P.resolve_profile(cfg, "balanced"))
    assert not P.triager_enabled({}) and not P.triager_enabled({"review": {"pass_score": 80}})
    assert not P.triager_enabled({"review": {"triager": "yes"}})


@pytest.mark.parametrize("value", ["yes", 1, None, [True]])
def test_review_triager_rejects_non_bool(value):
    cfg = {"agentic": {"profiles": {"balanced": {"review": {"triager": value}}}}}
    with pytest.raises(ValueError, match=r"review\.triager"):
        P.resolve_profile(cfg, "balanced")


def test_docs_describe_the_review_triager():
    from pathlib import Path
    doc = (Path(__file__).resolve().parent.parent / "docs/product/capabilities/agentic.md"
           ).read_text(encoding="utf-8")
    for needle in ("review_triager", "review.triager", "last_review.triage"):
        assert needle in doc
