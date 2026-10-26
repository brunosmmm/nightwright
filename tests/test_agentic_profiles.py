"""SPEC-0171: workflow profiles — builtins in code, user deltas deep-merged over them."""
import pytest

from wt.agentic import profiles as P
from wt.config import DEFAULT_CONFIG

PERSONA_KEYS = {"model", "effort", "max_budget_usd", "permission_mode", "read_only"}
PROFILE_KEYS = {"slots", "loop_max", "campaign_cap_usd", "run_timeout_s", "personas"}


def test_builtins_are_complete():
    for name, prof in P.builtin_profiles().items():
        assert PROFILE_KEYS <= set(prof), name
        for persona, settings in prof["personas"].items():
            assert PERSONA_KEYS <= set(settings), (name, persona)
    assert P.builtin_profiles()["balanced"]["personas"]["gate_triager"]["read_only"] is True


def test_default_config_has_agentic_block():
    a = DEFAULT_CONFIG["agentic"]
    assert a["default_profile"] == "balanced"
    assert a["runner"] == "claude" and a["profiles"] == {}
    assert a["lease_ttl_s"] > 0 and a["poll_interval_s"] > 0


def test_resolve_default_is_balanced():
    prof = P.resolve_profile({"agentic": dict(DEFAULT_CONFIG["agentic"])})
    assert prof["name"] == "balanced"
    meta = {"name", "description", "sources"}
    assert {k: v for k, v in prof.items() if k not in meta} == P.builtin_profiles()["balanced"]
    assert prof["name"] == "balanced" and prof["sources"] == ["builtin:balanced.yaml"]
    # a resolved profile is a copy — mutating it must not leak into the builtin
    prof["personas"]["implementer"]["model"] = "x"
    assert P.builtin_profiles()["balanced"]["personas"]["implementer"]["model"] == "sonnet"


def test_user_delta_keeps_every_other_builtin_key():
    cfg = {"agentic": {"profiles": {"balanced": {
        "personas": {"implementer": {"max_budget_usd": 6.0}}}}}}
    prof = P.resolve_profile(cfg, "balanced")
    assert prof["personas"]["implementer"]["max_budget_usd"] == 6.0
    assert prof["personas"]["implementer"]["model"] == "sonnet"       # untouched sibling key
    assert prof["personas"]["gate_triager"] == P.builtin_profiles()["balanced"]["personas"]["gate_triager"]
    assert prof["slots"] == 2 and prof["loop_max"] == 3


def test_new_user_profile_merges_over_balanced():
    cfg = {"agentic": {"profiles": {"mine": {"slots": 1, "campaign_cap_usd": 2.0}}}}
    prof = P.resolve_profile(cfg, "mine")
    assert prof["name"] == "mine" and prof["slots"] == 1 and prof["campaign_cap_usd"] == 2.0
    assert prof["personas"] == P.builtin_profiles()["balanced"]["personas"]
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
                              "followups": True, "followups_nits": False,
                              "followups_max": 10}
    assert P.review_params(prof) == (score, DEFAULT_WEIGHTS)
    assert P.review_params({}) == (80, DEFAULT_WEIGHTS)
    prof["review"]["weights"]["major"] = 1                      # a copy, not the builtin
    assert P.builtin_profiles()[name]["review"]["weights"]["major"] == 15


def test_review_user_delta_merges_partial_weights():
    cfg = {"agentic": {"profiles": {"balanced": {"review": {"weights": {"nit": 0},
                                                            "pass_score": 60}}}}}
    prof = P.resolve_profile(cfg, "balanced")
    assert prof["review"] == {"pass_score": 60, "weights": {**DEFAULT_WEIGHTS, "nit": 0},
                              "triager": True, "followups": True, "followups_nits": False,
                              "followups_max": 10}
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
    ({"followups_max": True}, "review.followups_max"),
    ({"followups_max": -1}, "review.followups_max"),
    ({"followups_max": "10"}, "review.followups_max"),
    ({"followups_max": 2.5}, "review.followups_max"),
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


def test_followups_max_validates_defaults_and_builtins():                        # SPEC-0239 AC1
    assert P.followup_cap({}) == 10 and P.followup_cap({"review": {"pass_score": 50}}) == 10
    for good in (0, 1, 25):
        cfg = {"agentic": {"profiles": {"balanced": {"review": {"followups_max": good}}}}}
        assert P.followup_cap(P.resolve_profile(cfg, "balanced")) == good
    for name in ("balanced", "thorough", "frugal"):
        prof = P.resolve_profile({"agentic": {"profiles": {}}}, name)
        assert P.followup_cap(prof) == 10
        assert P.followup_params(prof) == (True, False)           # nits stay off


def test_docs_describe_the_followup_filer():                                       # SPEC-0207 AC8
    from pathlib import Path
    doc = (Path(__file__).resolve().parent.parent / "docs/product/capabilities/agentic.md"
           ).read_text(encoding="utf-8")
    for needle in ("wtfu:", "followups_nits", "review_carry", "`followup` event", "adopted",
                   "unfiled", "wtfug:", "review.followups_max", "wt ideas group-followups"):
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


@pytest.mark.parametrize("name,model,effort,budget", [
    ("frugal", "sonnet", "medium", 1.0), ("balanced", "sonnet", "high", 2.0),
    ("thorough", "opus", "high", 4.0)])
def test_builtin_conflict_resolver_settings(name, model, effort, budget):         # SPEC-0221
    s = P.persona_settings(P.builtin_profiles()[name], "conflict_resolver")
    assert (s["model"], s["effort"], s["max_budget_usd"]) == (model, effort, budget)
    assert s["read_only"] is False and s["permission_mode"] == "bypassPermissions"
    assert budget < P.persona_settings(P.builtin_profiles()[name], "implementer")["max_budget_usd"]


# ---- SPEC-0237: YAML layers ------------------------------------------------------------------

import json
import os
import re
from pathlib import Path

DEFS = Path(P.__file__).resolve().parent / "profile_defs"
SCHEMA = "schema: wt.profile.v1\n"


def _layers(tmp_path):
    """(cfg, user dir, project root): a temp config dir and a temp project main checkout."""
    cfg = {"config_dir": str(tmp_path / "cfg"), "agentic": {"profiles": {}}}
    user = tmp_path / "cfg" / "profiles"
    proj = tmp_path / "proj"
    user.mkdir(parents=True, exist_ok=True)
    (proj / ".wt" / "profiles").mkdir(parents=True, exist_ok=True)
    return cfg, user, proj


def _put(directory, name, text, suffix=".yaml"):
    p = Path(directory) / f"{name}{suffix}"
    p.write_text(text, encoding="utf-8")
    return str(p)


def _pdir(proj):
    return proj / ".wt" / "profiles"


def test_builtin_files_match_the_golden_snapshot():
    golden = json.loads((Path(__file__).parent / "data" / "builtin_profiles_v1.json").read_text())
    assert P.builtin_profiles() == golden
    for name in golden:
        assert (DEFS / f"{name}.yaml").read_text().startswith("schema: wt.profile.v1\n")
    got = P.builtin_profiles()
    got["balanced"]["personas"]["implementer"]["model"] = "x"          # copies, not shared
    assert P.builtin_profiles()["balanced"]["personas"]["implementer"]["model"] == "sonnet"
    needle = "BUILTIN" + "_PROFILES"                      # not spelled out, so this file is clean
    assert not hasattr(P, needle)
    root = Path(__file__).resolve().parent.parent
    stray = [str(f.relative_to(root)) for d in ("src", "tests") for f in (root / d).rglob("*.py")
             if needle in f.read_text(encoding="utf-8")]
    assert stray == []


def test_user_and_project_files_add_complete_profiles(tmp_path):
    cfg, user, proj = _layers(tmp_path)
    body = (DEFS / "frugal.yaml").read_text()
    u = _put(user, "cheap", body)
    pr = _put(_pdir(proj), "projonly", body.replace("slots: 1", "slots: 4"))
    assert P.resolve_profile(cfg, "cheap")["sources"] == [u]
    assert P.resolve_profile(cfg, "cheap")["slots"] == 1
    assert "projonly" not in P.known_profiles(cfg)                       # no project root: no layer
    got = P.resolve_profile(cfg, "projonly", project_root=proj)
    assert got["slots"] == 4 and got["sources"] == [pr]
    assert {"cheap", "projonly", "balanced"} <= set(P.known_profiles(cfg, project_root=proj))


def test_precedence_project_over_config_over_user_over_builtin(tmp_path):
    cfg, user, proj = _layers(tmp_path)
    u = _put(user, "balanced", SCHEMA + "slots: 5\nloop_max: 5\nrun_timeout_s: 500\n"
             "campaign_cap_usd: 11.0\n")
    cfg["agentic"]["profiles"]["balanced"] = {"slots": 6, "loop_max": 6, "run_timeout_s": 600}
    pr = _put(_pdir(proj), "balanced", SCHEMA + "slots: 7\nrun_timeout_s: 700\n")
    got = P.resolve_profile(cfg, "balanced", project_root=proj)
    assert (got["slots"], got["loop_max"], got["run_timeout_s"]) == (7, 6, 700)
    assert got["campaign_cap_usd"] == 11.0                               # only the user layer set it
    assert got["personas"] == P.builtin_profiles()["balanced"]["personas"]
    assert got["sources"] == ["builtin:balanced.yaml", u, "config.yaml:agentic.profiles.balanced",
                              pr]
    no_proj = P.resolve_profile(cfg, "balanced")
    assert (no_proj["slots"], no_proj["sources"][-1]) == (
        6, "config.yaml:agentic.profiles.balanced")


def test_config_only_new_name_merges_over_balanced_and_effective_balanced(tmp_path):
    cfg, user, proj = _layers(tmp_path)
    cfg["agentic"]["profiles"]["mine"] = {"campaign_cap_usd": 2.0}
    got = P.resolve_profile(cfg, "mine")
    assert got["campaign_cap_usd"] == 2.0 and got["sources"] == [
        "builtin:balanced.yaml", "config.yaml:agentic.profiles.mine"]
    _put(user, "balanced", SCHEMA + "slots: 9\n")
    assert P.resolve_profile(cfg, "mine")["slots"] == 9


def test_campaign_cap_null_means_no_cap(tmp_path):
    cfg, user, proj = _layers(tmp_path)
    cfg["agentic"]["profiles"]["balanced"] = {"campaign_cap_usd": None}
    assert P.resolve_profile(cfg, "balanced")["campaign_cap_usd"] is None
    cfg["agentic"]["profiles"] = {}
    _put(user, "nocap", (DEFS / "frugal.yaml").read_text().replace(
        "campaign_cap_usd:", "campaign_cap_usd: null  #", 1))
    got = P.resolve_profile(cfg, "nocap")
    assert got["campaign_cap_usd"] is None and got["sources"] == [str(user / "nocap.yaml")]


def test_sources_list_each_origin_once_in_layer_order(tmp_path):
    """A chain through all four layers (own-name `extends` at each) lists each origin once; a
    chain is linear and a cycle raises, so no origin can repeat."""
    cfg, user, proj = _layers(tmp_path)
    u = _put(user, "balanced", SCHEMA + "slots: 3\n")
    cfg["agentic"]["profiles"]["balanced"] = {"extends": "balanced", "loop_max": 2}
    pr = _put(_pdir(proj), "balanced", SCHEMA + "extends: balanced\nrun_timeout_s: 9\n")
    got = P.resolve_profile(cfg, "balanced", project_root=proj)
    assert got["sources"] == ["builtin:balanced.yaml", u, "config.yaml:agentic.profiles.balanced",
                              pr]
    chain = P.profile_chain(cfg, "balanced", project_root=proj)
    assert [o for o, _ in chain] == got["sources"]
    tail = _put(_pdir(proj), "tail", SCHEMA + "extends: balanced\n")
    assert P.resolve_profile(cfg, "tail", project_root=proj)["sources"] == got["sources"] + [tail]


def test_description_is_top_most(tmp_path):
    cfg, user, proj = _layers(tmp_path)
    assert P.resolve_profile(cfg, "frugal")["description"].startswith("cheap")
    _put(user, "frugal", SCHEMA + "description: mine\n")
    assert P.resolve_profile(cfg, "frugal")["description"] == "mine"


def test_extends_other_name_builds_on_the_effective_target(tmp_path):
    cfg, user, proj = _layers(tmp_path)
    _put(user, "frugal", SCHEMA + "slots: 3\n")                          # effective frugal
    f = _put(_pdir(proj), "mini", SCHEMA + "extends: frugal\nloop_max: 1\n")
    got = P.resolve_profile(cfg, "mini", project_root=proj)
    assert (got["slots"], got["loop_max"], got["run_timeout_s"]) == (3, 1, 1200)
    assert got["sources"] == ["builtin:frugal.yaml", str(user / "frugal.yaml"), f]


def test_extends_own_name_builds_on_lower_layers(tmp_path):
    cfg, user, proj = _layers(tmp_path)
    _put(user, "frugal", SCHEMA + "slots: 3\nloop_max: 3\n")
    _put(_pdir(proj), "frugal", SCHEMA + "extends: frugal\nloop_max: 4\n")
    got = P.resolve_profile(cfg, "frugal", project_root=proj)
    assert (got["slots"], got["loop_max"]) == (3, 4)
    f = _put(user, "orphan", SCHEMA + "extends: orphan\nslots: 1\n")
    with pytest.raises(ValueError, match="unknown profile 'orphan'.*" + str(f).replace(".", r"\.")):
        P.resolve_profile(cfg, "orphan")


def test_extends_cycle_and_unknown_target(tmp_path):
    cfg, user, proj = _layers(tmp_path)
    _put(user, "a", SCHEMA + "extends: b\n")
    _put(user, "b", SCHEMA + "extends: a\n")
    with pytest.raises(ValueError, match="profile extends cycle: a → b → a"):
        P.resolve_profile(cfg, "a")
    f = _put(user, "c", SCHEMA + "extends: nope\n")
    with pytest.raises(ValueError) as e:
        P.resolve_profile(cfg, "c")
    assert "unknown profile 'nope'" in str(e.value) and f in str(e.value)


def _bad(tmp_path, text, name="bad", suffix=".yaml"):
    cfg, user, proj = _layers(tmp_path)
    f = _put(user, name, text, suffix)
    with pytest.raises(ValueError) as e:
        P.resolve_profile(cfg, name)
    return f, str(e.value)


@pytest.mark.parametrize("text,path", [
    (SCHEMA + "extends: balanced\nbogus: 1\n", "bogus"),
    (SCHEMA + "extends: balanced\npersonas: {nobody: {model: opus}}\n", "personas.nobody"),
    (SCHEMA + "extends: balanced\npersonas: {implementer: {modle: opus}}\n",
     "personas.implementer.modle"),
    (SCHEMA + "extends: balanced\npersonas: {implementer: {model: gpt4}}\n",
     "personas.implementer.model"),
    (SCHEMA + "extends: balanced\npersonas: {implementer: {model: 3}}\n",
     "personas.implementer.model"),
    (SCHEMA + "extends: balanced\npersonas: {implementer: {effort: huge}}\n",
     "personas.implementer.effort"),
    (SCHEMA + "extends: balanced\npersonas: {implementer: {permission_mode: yolo}}\n",
     "personas.implementer.permission_mode"),
    (SCHEMA + "extends: balanced\npersonas: {implementer: {max_budget_usd: 0}}\n",
     "personas.implementer.max_budget_usd"),
    (SCHEMA + "extends: balanced\npersonas: {implementer: {max_budget_usd: true}}\n",
     "personas.implementer.max_budget_usd"),
    (SCHEMA + "extends: balanced\npersonas: {implementer: {read_only: 1}}\n",
     "personas.implementer.read_only"),
    (SCHEMA + "extends: balanced\npersonas: {implementer: opus}\n", "personas.implementer"),
    (SCHEMA + "extends: balanced\npersonas: [implementer]\n", "personas"),
    (SCHEMA + "extends: balanced\nslots: true\n", "slots"),
    (SCHEMA + "extends: balanced\nslots: 0\n", "slots"),
    (SCHEMA + "extends: balanced\nloop_max: 1.5\n", "loop_max"),
    (SCHEMA + "extends: balanced\nrun_timeout_s: '60'\n", "run_timeout_s"),
    (SCHEMA + "extends: balanced\ncampaign_cap_usd: 0\n", "campaign_cap_usd"),
    (SCHEMA + "extends: balanced\ncampaign_cap_usd: yes\n", "campaign_cap_usd"),
    (SCHEMA + "extends: balanced\nreviewers: conformance_reviewer\n", "reviewers"),
    (SCHEMA + "extends: balanced\nextends: 5\n", "extends"),
    (SCHEMA + "extends: balanced\ndescription: [1]\n", "description"),
    (SCHEMA + "extends: balanced\nreview: {pass_score: 101}\n", "review.pass_score"),
    (SCHEMA + "extends: balanced\nreview: {weights: {nit: -1}}\n", "review.weights.nit"),
    ("extends: balanced\nslots: 1\n", "schema"),
    ("schema: wt.profile.v9\nextends: balanced\n", "schema"),
    ("- a\n- b\n", "<root>"),
])
def test_validation_rejects_with_origin_and_key_path(tmp_path, text, path):
    f, msg = _bad(tmp_path, text)
    assert f"{f}: {path}: " in msg


def test_validation_yaml_error_and_incomplete_profile(tmp_path):
    f, msg = _bad(tmp_path, "slots: [1\n")
    assert msg.startswith(f"{f}: ")
    f, msg = _bad(tmp_path, SCHEMA + "slots: 1\n", name="lonely")
    assert msg == (f"profile lonely ({f}): missing loop_max "
                   "(add it or `extends: <profile>`)")

def test_incomplete_personas_are_reported(tmp_path):
    cfg, user, proj = _layers(tmp_path)
    lines = (DEFS / "frugal.yaml").read_text().splitlines()
    _put(user, "nopersona", "\n".join(lines[:lines.index("  review_triager:")]) + "\n")
    with pytest.raises(ValueError, match=r"missing personas\.review_triager"):
        P.resolve_profile(cfg, "nopersona")


def test_bad_file_names_and_yml_files(tmp_path):
    cfg, user, proj = _layers(tmp_path)
    f1 = _put(user, "Bad_Name", SCHEMA + "extends: balanced\n")
    f2 = _put(user, "legacy", SCHEMA + "extends: balanced\n", suffix=".yml")
    _put(user, "notes", "ignored", suffix=".txt")
    assert {"Bad_Name", "legacy"} <= set(P.known_profiles(cfg))
    assert "notes" not in P.known_profiles(cfg)
    with pytest.raises(ValueError, match=re.escape(f"{f1}: <file>: profile name")):
        P.resolve_profile(cfg, "Bad_Name")
    with pytest.raises(ValueError, match=re.escape(f"{f2}: <file>: rename to .yaml")):
        P.resolve_profile(cfg, "legacy")


def test_yaml_and_yml_pair_is_a_conflict(tmp_path):
    cfg, user, proj = _layers(tmp_path)
    _put(user, "dup", SCHEMA + "extends: balanced\nslots: 1\n")
    f2 = _put(user, "dup", SCHEMA + "extends: balanced\nslots: 2\n", suffix=".yml")
    with pytest.raises(ValueError, match=re.escape(f"{f2}: <file>: conflicts with dup.yaml")):
        P.resolve_profile(cfg, "dup")


def test_unreadable_profile_directory_raises_value_error(tmp_path):
    cfg, user, proj = _layers(tmp_path)
    d = _pdir(proj)
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0)
    try:
        if os.access(d, os.R_OK):
            pytest.skip("directory permissions are not enforced (root)")
        with pytest.raises(ValueError, match=re.escape(f"{d}: <dir>:")):
            P.resolve_profile(cfg, "balanced", project_root=proj)
    finally:
        d.chmod(0o755)


def test_a_broken_file_breaks_only_its_own_profile(tmp_path):
    cfg, user, proj = _layers(tmp_path)
    _put(user, "broken", SCHEMA + "extends: balanced\nbogus: 1\n")
    _put(user, "fine", SCHEMA + "extends: balanced\nslots: 1\n")
    assert P.resolve_profile(cfg, "fine")["slots"] == 1
    assert P.resolve_profile(cfg, "frugal")["name"] == "frugal"
    assert P.resolve_profile(cfg)["name"] == "balanced"
    with pytest.raises(ValueError, match="broken.yaml: bogus"):
        P.resolve_profile(cfg, "broken")
    # a broken project delta of balanced breaks balanced there, not elsewhere
    _put(_pdir(proj), "balanced", SCHEMA + "slots: 0\n")
    with pytest.raises(ValueError, match="slots"):
        P.resolve_profile(cfg, "balanced", project_root=proj)
    assert P.resolve_profile(cfg, "balanced")["slots"] == 2


def test_config_delta_is_validated_too():
    cfg = {"agentic": {"profiles": {"balanced": {"personas": {"implementer": {"modle": "x"}}}}}}
    with pytest.raises(ValueError, match=r"config\.yaml:agentic\.profiles\.balanced: "
                                         r"personas\.implementer\.modle: unknown key"):
        P.resolve_profile(cfg, "balanced")
    cfg = {"agentic": {"profiles": {"balanced": {"schema": "wt.profile.v1", "slots": 4}}}}
    assert P.resolve_profile(cfg, "balanced")["slots"] == 4         # schema allowed, not required
    assert P.validate_profile_doc({"model": 1}, origin="o", in_file=False) == ["o: model: unknown key"]


def test_model_ids_are_accepted(tmp_path):
    cfg, user, proj = _layers(tmp_path)
    for model in ("claude-opus-5-5", "claude-sonnet-4-6[1m]", "haiku"):
        cfg["agentic"]["profiles"]["balanced"] = {"personas": {"implementer": {"model": model}}}
        assert P.resolve_profile(cfg, "balanced")["personas"]["implementer"]["model"] == model


def test_unknown_profile_lists_every_layer(tmp_path):
    cfg, user, proj = _layers(tmp_path)
    _put(user, "zeta", SCHEMA + "extends: balanced\n")
    _put(_pdir(proj), "alpha", SCHEMA + "extends: balanced\n")
    with pytest.raises(ValueError, match="unknown profile 'nope'; known: alpha, balanced, "
                                         "frugal, thorough"):
        P.resolve_profile(cfg, "nope", project_root=proj)


def test_profile_matrix_and_select_profile_see_the_project_layer(tmp_path):   # SPEC-0237 x 0219
    cfg, _user, proj = _layers(tmp_path)
    _put(_pdir(proj), "projonly", (DEFS / "frugal.yaml").read_text())
    cfg["agentic"]["profile_matrix"] = {"critical": {"xs": "projonly"}}
    with pytest.raises(ValueError, match="unknown profile 'projonly'"):
        P.profile_matrix(cfg)                                            # no project root
    review = {"verdict": "approve", "size": "xs", "tone": "critical"}
    pick = P.select_profile(cfg, review=review, project_root=proj)
    assert pick["profile"] == "projonly" and pick["source"] == "review"


def test_yaml_builtins_equal_the_last_python_builtin_profiles():         # resolver guard
    """Every key the retired Python builtin dict had (incl. SPEC-0202 adversarial_reviewer and
    reviewers, SPEC-0203/0206/0207 review keys) survives into the YAML builtins unchanged."""
    golden = json.loads((Path(__file__).parent / "data" / "builtin_profiles_v1.json").read_text())
    got = P.builtin_profiles()
    for name, prof in golden.items():
        for key, value in prof.items():
            assert got[name][key] == value, (name, key)
    assert golden["balanced"]["reviewers"][-1] == "adversarial_reviewer"
