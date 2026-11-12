"""SPEC-0557: no persona run above effort `high` on any backend. The clamp helper, every persona
x backend x profile x model set x tier, explicit Cursor ids, Pi thinking, the `null` identity,
the user's real profile shape, and `effort_clamped_from` on the run record, ledger and export."""
import dataclasses
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from nightwright.agentic import effort_ceiling as EC
from nightwright.agentic import personas as PS
from nightwright.agentic import runner as R
from nightwright.agentic.cursor_map import load_sdk_models, sdk_selection
from nightwright.agentic.profiles import EFFORTS, known_profiles, resolve_profile

pytestmark = pytest.mark.fast

ABOVE = ("xhigh", "max", "extra-high")

# The user's ~/.config/nightwright shape (2026-11-11): the `cursor` profile file, and the
# `balanced` / `thorough` overrides in config.yaml `agentic.profiles`.
USER_CURSOR_PROFILE = yaml.safe_load("""
schema: nw.profile.v1
extends: balanced
description: "composer implements, grok reviews; no gpt-5.6-sol"
slots: 4
loop_max: 3
campaign_cap_usd: 15
run_timeout_s: 2700
personas:
  implementer: {model: sonnet, effort: high, cursor_model: composer-2.5}
  conflict_resolver: {model: sonnet, effort: high, cursor_model: composer-2.5}
  gate_triager: {model: sonnet, effort: medium, cursor_model: grok-4.7-medium}
  conformance_reviewer: {model: sonnet, effort: xhigh, cursor_model: grok-4.7-xhigh}
  test_reviewer: {model: sonnet, effort: xhigh, cursor_model: grok-4.7-xhigh}
  adversarial_reviewer: {model: sonnet, effort: xhigh, cursor_model: grok-4.7-xhigh}
  spec_reviewer: {model: sonnet, effort: xhigh, cursor_model: grok-4.7-xhigh}
  spec_writer: {model: sonnet, effort: high, cursor_model: grok-4.7-high}
  review_triager: {model: sonnet, effort: medium, cursor_model: grok-4.7-medium}
""")
USER_CONFIG_PROFILES = yaml.safe_load("""
balanced:
  slots: 4
  campaign_cap_usd: 15
  run_timeout_s: 2700
  personas:
    implementer: {cursor_model: composer-2.5}
    conflict_resolver: {cursor_model: composer-2.5}
    gate_triager: {cursor_model: grok-4.7-medium}
    conformance_reviewer: {cursor_model: grok-4.7-xhigh}
    test_reviewer: {cursor_model: grok-4.7-xhigh}
    adversarial_reviewer: {cursor_model: grok-4.7-xhigh}
    spec_reviewer: {model: sonnet, effort: xhigh, max_budget_usd: 1.5, cursor_model: grok-4.7-xhigh}
    spec_writer: {cursor_model: grok-4.7-high}
    review_triager: {cursor_model: grok-4.7-medium}
thorough:
  personas:
    implementer: {cursor_model: composer-2.5}
    conflict_resolver: {cursor_model: composer-2.5}
    gate_triager: {cursor_model: grok-4.7-high}
    conformance_reviewer: {cursor_model: grok-4.7-xhigh}
    test_reviewer: {cursor_model: grok-4.7-xhigh}
    adversarial_reviewer: {cursor_model: grok-4.7-xhigh}
    spec_reviewer: {cursor_model: grok-4.7-xhigh}
    spec_writer: {cursor_model: grok-4.7-high}
    review_triager: {cursor_model: grok-4.7-medium}
""")


# ---- the helper ------------------------------------------------------------------------------

@pytest.mark.parametrize("effort,limit,want", [
    ("xhigh", "high", ("high", "xhigh")), ("max", "high", ("high", "max")),
    ("high", "high", ("high", None)), ("medium", "high", ("medium", None)),
    ("low", "high", ("low", None)), ("xhigh", None, ("xhigh", None)),
    (None, "high", (None, None)), ("weird", "high", ("weird", None)),
    ("max", "xhigh", ("xhigh", "max")), ("xhigh", "medium", ("medium", "xhigh")),
])
def test_clamp_effort(effort, limit, want):
    assert EC.clamp_effort(effort, limit) == want


def test_ceiling_default_explicit_and_null():
    from nightwright.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["agentic"]["effort_ceiling"] == "high"
    assert EC.ceiling({}) == "high" and EC.ceiling({"agentic": {}}) == "high"
    assert EC.ceiling({"agentic": {"effort_ceiling": "xhigh"}}) == "xhigh"
    assert EC.ceiling({"agentic": {"effort_ceiling": None}}) is None
    with pytest.raises(ValueError, match="effort_ceiling"):
        EC.ceiling({"agentic": {"effort_ceiling": "ultra"}})


@pytest.mark.parametrize("mid,want", [
    ("grok-4.7-xhigh", "grok-4.7-high"), ("gpt-5.6-sol-xhigh", "gpt-5.6-sol-high"),
    ("gpt-5.4-extra-high", "gpt-5.4-high"), ("some-model-max", "some-model-high"),
    ("gpt-5.6-sol-xhigh-fast", "gpt-5.6-sol-high-fast"),
    ("grok-4.7-high", "grok-4.7-high"), ("grok-4.7-low", "grok-4.7-low"),
    ("composer-2.5", "composer-2.5"), ("grok-4.7", "grok-4.7"),
])
def test_clamp_cursor_id(mid, want):
    assert EC.clamp_cursor_id(mid, "high") == want
    assert EC.clamp_cursor_id(mid, None) == mid


def test_every_clamped_catalog_id_is_a_legal_high_variant():
    """The `-high` rewrite of every catalog id with an xhigh/max variant is a model the SDK
    accepts: its effort parameter lists `high`."""
    for mid, spec in load_sdk_models().items():
        efforts = (spec or {}).get("efforts") or []
        if not any(e in efforts for e in ABOVE):
            continue
        assert "high" in efforts, mid
        for word in (e for e in efforts if e in ABOVE):
            sel = sdk_selection(EC.clamp_cursor_id(f"{mid}-{word}", "high"))
            assert sel["id"] == mid
            assert {p["value"] for p in sel["params"] if p["id"] != "fast"} == {"high"}


# ---- build_request across the matrix --------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("NW_AGENTIC_RUNNER", raising=False)
    cfg_dir = tmp_path / "cfg"
    (cfg_dir / "personas").mkdir(parents=True)
    (cfg_dir / "profiles").mkdir()
    (cfg_dir / "profiles" / "cursor.yaml").write_text(yaml.safe_dump(USER_CURSOR_PROFILE))
    cfg = {"config_dir": str(cfg_dir), "data_dir": str(tmp_path / "data"),
           "agentic": {"runner": "claude", "profiles": USER_CONFIG_PROFILES,
                       "default_profile": "balanced"}}
    root = tmp_path / "main"
    (root / ".nw").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    genv = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1", "PATH": os.environ["PATH"]}
    for args in (("init", "-q", "-b", "main"), ("commit", "-q", "--allow-empty", "-m", "b")):
        subprocess.run(["git", "-C", str(wt), *args], check=True, capture_output=True, env=genv)
    state = {"id": "SPEC-0001-20261111-ab12", "kind": "implement", "spec_id": "SPEC-0001",
             "project": "NoSuchProject", "state": "REVIEWING",
             "repo": {"root": str(root), "base": "main", "branch": "main", "worktree": str(wt)},
             "spec": {"rel_path": "docs/specs/0001-x.md"}, "counters": {"loops": 0}}
    return cfg, root, state, tmp_path


def _req(env, persona, *, agentic=None, profile=None, lean=None):
    cfg, root, state, tmp = env
    cfg = {**cfg, "agentic": {**cfg["agentic"], **(agentic or {})}}
    st = dict(state)
    if lean:
        st["lean"] = {"tiers": True, "packet": False, "collator": False, "sessions": False}
    prof = resolve_profile(cfg, profile, project_root=root)
    return PS.build_request(cfg, prof, {"_root": str(root)}, st, persona,
                            run_dir=tmp / "runs" / f"001-{persona}")


def _cursor_effort_words(model):
    """Effort words an SDK run would send for this Cursor id."""
    return {p["value"] for p in sdk_selection(model)["params"] if p["id"] != "fast"}


def _assert_under_ceiling(req):
    assert req.effort not in ABOVE, (req.persona, req.effort)
    assert req.pi_thinking not in ABOVE, (req.persona, req.pi_thinking)
    if "--effort" in R.build_argv(req):
        assert R.build_argv(req)[R.build_argv(req).index("--effort") + 1] not in ABOVE
    if req.model.startswith(("grok", "gpt", "composer")):           # a Cursor id
        assert not _cursor_effort_words(req.model) & set(ABOVE), (req.persona, req.model)
        assert not req.model.endswith(ABOVE), (req.persona, req.model)


BACKENDS = [{"runner": "claude"}, {"runner": "cursor"}, {"runner": "pi"},
            {"runner": "claude", "model_set": "claude"}, {"runner": "claude", "model_set": "grok"},
            {"runner": "claude", "model_set": "gpt"}, {"runner": "claude", "model_set": "mixed"}]


@pytest.mark.parametrize("lean", [False, True])
@pytest.mark.parametrize("backend", BACKENDS, ids=lambda b: "-".join(b.values()))
def test_no_persona_above_high_for_every_profile(env, backend, lean):
    cfg = env[0]
    names = sorted(known_profiles(cfg))
    assert {"balanced", "frugal", "thorough", "cursor"} <= set(names)
    for name in names:
        for persona in PS.PERSONAS:
            _assert_under_ceiling(_req(env, persona, agentic=backend, profile=name, lean=lean))


def test_unclamped_baseline_really_has_xhigh(env):
    """Guards the matrix above: the user's profile does ask for xhigh and `-xhigh` ids."""
    ag = {"effort_ceiling": None, "runner": "cursor"}
    r = _req(env, "conformance_reviewer", agentic=ag, profile="cursor")
    assert (r.effort, r.model) == ("xhigh", "grok-4.7-xhigh")
    r = _req(env, "spec_reviewer", agentic={"effort_ceiling": None}, profile="balanced")
    assert (r.effort, r.model) == ("xhigh", "sonnet")


def test_user_profile_shape_claude_cursor_pi(env):
    claude = _req(env, "spec_reviewer", profile="balanced")           # model sonnet, effort xhigh
    assert (claude.model, claude.effort, claude.effort_clamped_from) == ("sonnet", "high", "xhigh")
    assert R.build_argv(claude)[R.build_argv(claude).index("--effort") + 1] == "high"
    for persona in ("conformance_reviewer", "test_reviewer", "adversarial_reviewer"):
        cur = _req(env, persona, agentic={"runner": "cursor"}, profile="cursor")
        assert (cur.model, cur.effort, cur.effort_clamped_from) == ("grok-4.7-high", "high", "xhigh")
        assert _cursor_effort_words(cur.model) == {"high"}
        pi = _req(env, persona, agentic={"runner": "pi"}, profile="cursor")
        assert (pi.effort, pi.pi_thinking, pi.effort_clamped_from) == ("high", "high", "xhigh")
    cur = _req(env, "spec_reviewer", agentic={"runner": "cursor"}, profile="thorough")
    assert cur.model == "grok-4.7-high" and cur.effort == "high"
    low = _req(env, "gate_triager", agentic={"runner": "cursor"}, profile="cursor")
    assert (low.model, low.effort, low.effort_clamped_from) == ("grok-4.7-medium", "medium", None)


def test_explicit_cursor_ids_rewritten_to_high(env):
    for explicit, want, was in (("grok-4.7-xhigh", "grok-4.7-high", "xhigh"),
                                ("gpt-5.6-sol-xhigh", "gpt-5.6-sol-high", "xhigh"),
                                ("gpt-5.6-sol-max", "gpt-5.6-sol-high", "max")):
        prof = {"personas": {"adversarial_reviewer": {"cursor_model": explicit, "effort": "high"}}}
        req = _req(env, "adversarial_reviewer", profile="balanced",
                   agentic={"runner": "cursor", "profiles": {
                       "balanced": prof, "thorough": {}}})
        assert req.model == want, explicit
        assert req.effort == "high" and req.effort_clamped_from == was


@pytest.mark.parametrize("effort", ["medium", "high"])
def test_explicit_cursor_id_lowered_records_clamped_from_without_effort_change(env, effort):
    prof = {"personas": {"conformance_reviewer": {"cursor_model": "grok-4.7-xhigh",
                                                  "effort": effort}}}
    req = _req(env, "conformance_reviewer", profile="balanced",
               agentic={"runner": "cursor", "profiles": {"balanced": prof, "thorough": {}}})
    assert (req.model, req.effort) == ("grok-4.7-high", effort)
    assert req.effort_clamped_from == "xhigh"
    assert R._request_doc(req, "t")["effort_clamped_from"] == "xhigh"
    nowhere = _req(env, "conformance_reviewer", profile="balanced",
                   agentic={"runner": "cursor", "profiles": {"balanced": {"personas": {
                       "conformance_reviewer": {"cursor_model": "grok-4.7-high",
                                                "effort": effort}}}, "thorough": {}}})
    assert nowhere.effort_clamped_from is None


def test_tier_overlay_effort_is_clamped(env, monkeypatch):
    from nightwright.agentic import tiers as T
    row = {"model": "claude-sonnet-5-5", "effort": "xhigh"}
    monkeypatch.setattr(T, "tier_model", lambda *a, **k: row)
    req = _req(env, "adversarial_reviewer", profile="balanced", lean=True)
    assert (req.effort, req.effort_clamped_from) == ("high", "xhigh")


def test_pi_thinking_clamped_from_pi_map_and_model_set(env):
    pi = _req(env, "implementer", agentic={"runner": "pi"}, profile="frugal")
    assert pi.pi_thinking in ("low", "medium", "high")
    custom = {"pi": {"aliases": {"sonnet": {"high": {"provider": "anthropic",
                                                     "id": "claude-sonnet-4-6",
                                                     "thinking": "xhigh"}}}}}
    req = _req(env, "implementer", agentic={"runner": "pi", **custom}, profile="balanced")
    assert req.pi_thinking == "high" and req.effort_clamped_from == "xhigh"


# ---- null ceiling is today's behaviour exactly ----------------------------------------------

@pytest.mark.parametrize("backend", [{"runner": "claude"}, {"runner": "cursor"}, {"runner": "pi"}])
def test_null_ceiling_is_identity(env, backend):
    cfg = env[0]
    for name in ("balanced", "thorough", "cursor"):
        prof = resolve_profile(cfg, name, project_root=env[1])
        for persona in PS.PERSONAS:
            req = _req(env, persona, agentic={**backend, "effort_ceiling": None}, profile=name)
            st = PS.persona_settings(prof, persona)
            alias, effort = str(st.get("model") or "sonnet"), st.get("effort")
            ag = {**cfg["agentic"], **backend}
            model, _ = R.mapped_model({**cfg, "agentic": ag}, alias, effort,
                                      cursor_model=st.get("cursor_model"))
            assert (req.model, req.effort) == (model, effort), (name, persona)
            assert req.effort_clamped_from is None
            if backend["runner"] == "pi":
                row = R.map_pi_model(alias, effort, cfg)
                assert req.pi_thinking == row["thinking"]


def test_default_config_clamps_the_same_request(env):
    on = _req(env, "conformance_reviewer", agentic={"runner": "cursor"}, profile="cursor")
    off = _req(env, "conformance_reviewer", agentic={"runner": "cursor", "effort_ceiling": None},
               profile="cursor")
    assert (on.effort, on.model) == ("high", "grok-4.7-high")
    assert (off.effort, off.model) == ("xhigh", "grok-4.7-xhigh")
    assert off.effort_clamped_from is None and on.effort_clamped_from == "xhigh"


def test_request_roundtrips_effort_clamped_from(env, tmp_path):
    req = _req(env, "spec_reviewer", profile="balanced")
    assert req.effort_clamped_from == "xhigh"
    back = R.rebuild_request(_write_request_doc(req, tmp_path))
    assert back.effort_clamped_from == "xhigh"
    plain = _req(env, "implementer", profile="balanced")
    assert "effort_clamped_from" not in R._request_doc(plain, "t")


def _write_request_doc(req, tmp_path):
    d = tmp_path / "rd"
    d.mkdir(exist_ok=True)
    (d / "request.json").write_text(json.dumps(R._request_doc(req, "t")))
    return d


def test_run_record_carries_effort_clamped_from_only_when_lowered(env, tmp_path):
    for persona, want in (("spec_reviewer", "xhigh"), ("implementer", None)):
        req = dataclasses.replace(_req(env, persona, profile="balanced"),
                                  run_dir=tmp_path / "data" / "c" / "runs" / f"001-{persona}")
        req.run_dir.mkdir(parents=True)
        R.write_run_record(req, R.RunResult(0, False, False, 1.0, False, 0.1), "t")
        rec = json.loads((req.run_dir / "run.json").read_text())
        assert rec.get("effort_clamped_from") == want
        assert ("effort_clamped_from" in rec) is (want is not None)


# ---- through the queue: run.start and the export --------------------------------------------

def test_run_start_and_export_show_effort_clamped_from(tmp_path, monkeypatch):
    from test_agentic_e2e import (adv_review, added, impl_done, make_cfg, make_repo, review,
                                  script, _PerPersona)
    from nightwright.agentic import campaign_export as CE
    from nightwright.agentic import queue as Q
    from nightwright.agentic import store as S
    conf, adv = "conformance_reviewer", "adversarial_reviewer"
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []), profile={"reviewers": [conf, adv]})
    cfg["agentic"]["profiles"]["balanced"]["personas"] = {conf: {"effort": "xhigh"}}
    cid = added(cfg, monkeypatch)
    runner = _PerPersona({"implementer": [impl_done()], conf: [review()], adv: [adv_review()]})
    for _ in range(20):
        Q.tick(cfg, runner=runner)
        if S.read_state(cfg, cid)["state"] in ("MERGED", "PARKED"):
            break
    starts = {e["persona"]: e for e in S.read_ledger(cfg, cid, types=["run.start"])}
    assert starts[conf]["effort"] == "high" and starts[conf]["effort_clamped_from"] == "xhigh"
    assert "effort_clamped_from" not in starts["implementer"]
    runs = {r["persona"]: r for r in CE.export_tables(cfg)["runs"]}
    assert runs[conf]["effort_clamped_from"] == "xhigh"
    assert runs["implementer"]["effort_clamped_from"] is None
    assert "effort_clamped_from" in [c for c, _ in CE.RUN_COLUMNS]


def test_export_shows_effort_clamped_from_for_a_lowered_cursor_id(env, monkeypatch):
    """The user's reviewer shape (`cursor_model: grok-4.7-xhigh`, effort high): the request
    carries `effort_clamped_from`, the `run.start` payload is built from it, the export row
    shows it."""
    from nightwright.agentic import campaign_export as CE
    from nightwright.agentic import runs as RN
    req = _req(env, "adversarial_reviewer", profile="cursor", agentic={"runner": "cursor"})
    assert (req.model, req.effort, req.effort_clamped_from) == ("grok-4.7-high", "high", "xhigh")
    start = {"type": "run.start", "run_id": "001-adversarial_reviewer",
             "persona": req.persona, "model": req.model, "effort": req.effort,
             "effort_clamped_from": req.effort_clamped_from}
    monkeypatch.setattr(RN, "campaign_runs", lambda cfg, cid, events: [
        {"run_id": "001-adversarial_reviewer", "persona": req.persona, "model": req.model,
         "effort": req.effort}])
    rows = CE.run_rows({}, {"campaign": "c"}, events=[start])
    assert rows[0]["effort_clamped_from"] == "xhigh" and rows[0]["effort"] == "high"
