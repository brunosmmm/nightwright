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

@pytest.mark.parametrize("name,want", [
    ("balanced", ["conformance_reviewer", "test_reviewer", "adversarial_reviewer"]),
    ("thorough", ["conformance_reviewer", "test_reviewer", "adversarial_reviewer"]),
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
                              "triager": name != "frugal",
                              "followups": True, "followups_nits": False}
    assert P.review_params(prof) == (score, DEFAULT_WEIGHTS)
    assert P.review_params({}) == (80, DEFAULT_WEIGHTS)
    prof["review"]["weights"]["major"] = 1                      # a copy, not the builtin
    assert P.BUILTIN_PROFILES[name]["review"]["weights"]["major"] == 15


def test_review_user_delta_merges_partial_weights():
    cfg = {"agentic": {"profiles": {"balanced": {"review": {"weights": {"nit": 0},
                                                            "pass_score": 60}}}}}
    prof = P.resolve_profile(cfg, "balanced")
    assert prof["review"] == {"pass_score": 60, "weights": {**DEFAULT_WEIGHTS, "nit": 0},
                              "triager": True, "followups": True, "followups_nits": False}
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
    ({"followups": 1}, "review.followups"),
    ({"followups": "yes"}, "review.followups"),
    ({"followups_nits": None}, "review.followups_nits"),
    ({"followups_nits": 0}, "review.followups_nits"),
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


def test_followup_keys_default_validate_and_accept(tmp_path):                      # SPEC-0207 AC2
    for name in ("balanced", "thorough", "frugal"):
        prof = P.resolve_profile({"agentic": {"profiles": {}}}, name)
        assert P.followup_params(prof) == (True, False)
    assert P.followup_params({}) == (True, False)                  # hand-built profile
    assert P.followup_params({"review": {"pass_score": 50}}) == (True, False)
    cfg = {"agentic": {"profiles": {"balanced": {"review": {"followups": False,
                                                            "followups_nits": True}}}}}
    assert P.followup_params(P.resolve_profile(cfg, "balanced")) == (False, True)


def test_docs_describe_the_followup_filer():                                       # SPEC-0207 AC8
    from pathlib import Path
    doc = (Path(__file__).resolve().parent.parent / "docs/product/capabilities/agentic.md"
           ).read_text(encoding="utf-8")
    for needle in ("wtfu:", "followups_nits", "review_carry", "`followup` event", "adopted",
                   "unfiled"):
        assert needle in doc


# ---- SPEC-0202: adversarial reviewer in the profiles ------------------------------------------

def _in_canonical_order(reviewers):
    return reviewers == [r for r in P.REVIEWER_ORDER if r in reviewers]


@pytest.mark.parametrize("name,model,budget", [("balanced", "opus", 1.5), ("thorough", "opus", 2.5)])
def test_default_reviewers_include_adversarial_last(name, model, budget):
    prof = P.resolve_profile({}, name)
    assert {"conformance_reviewer", "adversarial_reviewer"} <= set(prof["reviewers"])
    assert _in_canonical_order(prof["reviewers"]) and prof["reviewers"][-1] == "adversarial_reviewer"
    s = prof["personas"]["adversarial_reviewer"]
    assert (s["model"], s["effort"], s["max_budget_usd"]) == (model, "high", budget)
    assert s["read_only"] is True and s["permission_mode"] == "dontAsk"


def test_frugal_has_settings_but_does_not_list_adversarial():
    prof = P.resolve_profile({}, "frugal")
    assert "adversarial_reviewer" not in prof["reviewers"]
    s = prof["personas"]["adversarial_reviewer"]
    assert (s["model"], s["effort"], s["max_budget_usd"]) == ("sonnet", "medium", 0.6)


def test_frugal_user_delta_can_opt_in_to_adversarial():
    cfg = {"agentic": {"profiles": {"frugal": {
        "reviewers": ["conformance_reviewer", "adversarial_reviewer"]}}}}
    prof = P.resolve_profile(cfg, "frugal")
    assert P.reviewers_of(prof) == ["conformance_reviewer", "adversarial_reviewer"]


def test_reviewer_order_is_canonical():
    assert P.REVIEWER_ORDER == ("conformance_reviewer", "test_reviewer", "adversarial_reviewer")


def test_capability_page_lists_reviewers_and_categories():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "docs" / "product" / "capabilities"
            / "agentic.md").read_text()
    from wt.agentic import personas as PS
    for needle in ("adversarial_reviewer", "conformance_reviewer", *PS.ADVERSARIAL_CATEGORIES):
        assert needle in text, needle
||||||| parent of 03ed8e5 (wt(SPEC-0219-20261025-e1d5): implementer run 002 — SPEC-0219: spec sizing and tone now pick the campaign profile. The revie)




# ---- sizing and tone pick the profile (SPEC-0219) ----------------------------------------------

MATRIX = {
    "cost-effective": ("frugal", "frugal", "balanced", "balanced"),
    "high-end": ("balanced", "balanced", "balanced", "thorough"),
    "critical": ("thorough", "thorough", "thorough", "thorough"),
}
SIZED_REVIEW = {"verdict": "approve", "size": "m", "tone": "high-end", "rationale": "r"}


def _cfg(**agentic):
    return {"agentic": {"default_profile": "balanced", **agentic}}


@pytest.mark.parametrize("tone", sorted(MATRIX))
def test_profile_matrix_every_cell(tone):
    assert P.PROFILE_MATRIX[tone] == dict(zip(("xs", "s", "m", "l"), MATRIX[tone]))
    for size, want in zip(("xs", "s", "m", "l"), MATRIX[tone]):
        pick = P.select_profile(_cfg(), review={"verdict": "approve", "size": size, "tone": tone})
        assert pick["profile"] == want and pick["source"] == "review"
        assert (pick["size"], pick["tone"]) == (size, tone)
        assert pick["reason"] == f"size {size}, tone {tone} → {want}"
    assert all("xl" not in row for row in P.PROFILE_MATRIX.values())


def test_select_profile_precedence_and_reasons():
    cfg = _cfg(default_profile="frugal")
    flag = P.select_profile(cfg, flag="thorough", review=SIZED_REVIEW)
    assert flag == {"profile": "thorough", "source": "flag", "reason": "--profile thorough",
                    "size": "m", "tone": "high-end"}
    nothing = P.select_profile(cfg, flag="thorough")
    assert nothing["size"] is None and nothing["tone"] is None
    old = P.select_profile(cfg, review={"verdict": "approve"})
    assert (old["profile"], old["source"]) == ("frugal", "default")
    assert old["reason"] == "review predates sizing (no size/tone)"
    forced = P.select_profile(cfg, review=None)
    assert (forced["profile"], forced["source"]) == ("frugal", "default")
    assert forced["reason"] == "no approving review (--force)"
    xl = P.select_profile(cfg, review={"verdict": "approve", "size": "xl", "tone": "critical"})
    assert xl["source"] == "default" and xl["size"] == "xl"


def test_matrix_config_overrides_one_cell():
    cfg = _cfg(profile_matrix={"high-end": {"m": "thorough"}})
    assert P.select_profile(cfg, review=SIZED_REVIEW)["profile"] == "thorough"
    assert P.select_profile(cfg, review={**SIZED_REVIEW, "size": "s"})["profile"] == "balanced"
    assert P.PROFILE_MATRIX["high-end"]["m"] == "balanced"          # builtin untouched
    custom = _cfg(profiles={"mine": {}}, profile_matrix={"high-end": {"m": "mine"}})
    assert P.select_profile(custom, review=SIZED_REVIEW)["profile"] == "mine"


@pytest.mark.parametrize("matrix,needle", [
    ({"fancy": {"m": "frugal"}}, "profile_matrix: unknown tone 'fancy'"),
    ({"high-end": {"huge": "frugal"}}, "profile_matrix[high-end][huge]"),
    ({"high-end": {"m": "nope"}}, "profile_matrix[high-end][m]: unknown profile 'nope'"),
    ({"critical": {"xl": "thorough"}}, "profile_matrix[critical][xl]"),
])
def test_matrix_config_errors_name_the_cell(matrix, needle):
    with pytest.raises(ValueError) as e:
        P.select_profile(_cfg(profile_matrix=matrix), review=SIZED_REVIEW)
    assert needle in str(e.value)
