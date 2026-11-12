"""SPEC-0553: persona role tiers, the per-backend tier map (overlays, Cursor effort and Pi
provider/id checks), the lean flags, and the tier binding in `personas.build_request`
(override precedence, flag-off identity, the deep close pass)."""
import dataclasses
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from nightwright.agentic import persona_defs as PD
from nightwright.agentic import persona_registry as PR
from nightwright.agentic import personas as PS
from nightwright.agentic import tiers as T
from nightwright.agentic.profiles import resolve_profile

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent
DEFS = ROOT / "src" / "nightwright" / "agentic" / "persona_defs"
EPIC_TIERS = {"spec_writer": "judge", "spec_reviewer": "judge", "implementer": "judge",
              "conflict_resolver": "judge", "conformance_reviewer": "judge",
              "test_reviewer": "judge", "adversarial_reviewer": "judge",
              "divergence_triager": "judge", "gate_triager": "marshal",
              "review_triager": "marshal",
              "review_marshal": "marshal"}                       # SPEC-0554


# ---- persona tiers ---------------------------------------------------------------------------

def test_every_builtin_declares_the_epic_tier():
    assert {n: p.tier for n, p in PS.PERSONAS.items()} == EPIC_TIERS
    for path in sorted(DEFS.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        assert doc["tier"] == EPIC_TIERS[doc["name"]], path.name


@pytest.mark.parametrize("tier,msg", [(None, "tier: required"), ("huge", "tier: must be one of")])
def test_builtin_definition_with_missing_or_unknown_tier_fails_naming_the_file(tier, msg):
    doc = yaml.safe_load((DEFS / "gate_triager.yaml").read_text())
    doc.pop("tier")
    if tier is not None:
        doc["tier"] = tier
    errs = PR.validate_persona_doc(doc, origin="builtin:persona_defs/gate_triager.yaml",
                                   layer="builtin", stem="gate_triager", known=PS.KNOWN)
    assert len(errs) == 1 and errs[0].startswith("builtin:persona_defs/gate_triager.yaml: ")
    assert msg in errs[0]


def test_builtin_loader_raises_naming_the_file(tmp_path, monkeypatch):
    for path in DEFS.glob("*.yaml"):
        text = path.read_text()
        if path.stem == "review_triager":
            text = text.replace("tier: marshal", "tier: cheap")
        (tmp_path / path.name).write_text(text)
    monkeypatch.setattr(PR, "_DEFS_DIR", tmp_path)
    with pytest.raises(ValueError, match=r"review_triager\.yaml: tier: must be one of"):
        PR.load_builtin(PS.KNOWN)


def _declared(directory, name, extra=""):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text("Review {{spec_id}}.\n")
    (directory / f"{name}.yaml").write_text(
        f"schema: nw.persona.v1\nname: {name}\nrole: reviewer\ntemplate: {name}.md\n{extra}")


def test_declared_persona_tier_defaults_to_judge_and_is_validated(tmp_path):
    user = tmp_path / "user"
    _declared(user, "plain_reviewer")
    _declared(user, "cheap_reviewer", "tier: marshal\n")
    _declared(user, "bad_reviewer", "tier: gold\n")
    problems = PD.validate_persona_defs(None, user_dir=user)
    assert problems == ["user:bad_reviewer.yaml: tier: must be one of marshal, judge, deep, "
                        "got 'gold'"]
    reg = PD.load_registry(None, user_dir=user)
    assert reg["plain_reviewer"].tier == "judge" and reg["cheap_reviewer"].tier == "marshal"
    assert "bad_reviewer" not in reg


def test_personas_validate_cli_fails_on_a_bad_tier(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from nightwright.cli import cli
    cfg_dir = tmp_path / "cfg"
    _declared(cfg_dir / "personas", "bad_reviewer", "tier: 7\n")
    cfg = {"config_dir": str(cfg_dir), "data_dir": str(tmp_path / "data"), "agentic": {}}
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(cli, ["personas", "validate", "--json"])
    assert r.exit_code == 1, r.output
    assert "bad_reviewer.yaml: tier: must be one of" in r.output


# ---- the tier map ----------------------------------------------------------------------------

def test_builtin_tier_map_rows():
    m = T.load_tier_map({})
    assert m["claude"] == {"marshal": {"model": "claude-haiku-5-5", "effort": "medium"},
                           "judge": {"model": "claude-sonnet-5-5", "effort": "high"},
                           "deep": {"model": "claude-opus-5-5", "effort": "high"}}
    assert T.tier_model({}, "cursor", "deep") == {"model": "gpt-5.6-sol", "effort": "high"}
    assert T.tier_model({}, "pi", "marshal") == {"model": "openai/gpt-5.6-luna",
                                                 "effort": "medium"}
    assert T.tier_model({}, "fake", "judge")["model"] == "claude-sonnet-5-5"   # claude row
    with pytest.raises(ValueError, match="unknown tier"):
        T.tier_model({}, "claude", "mega")


def test_tier_map_overlays_lowest_first(tmp_path):
    cfg_dir, root = tmp_path / "cfg", tmp_path / "proj"
    cfg_dir.mkdir()
    (root / ".nw").mkdir(parents=True)
    (cfg_dir / "tier_map.yaml").write_text(
        "schema: nw.tier_map.v1\nclaude: {judge: {model: claude-opus-5-5}}\n"
        "pi: {judge: {model: anthropic/claude-sonnet-5-5}}\n")
    cfg = {"config_dir": str(cfg_dir),
           "agentic": {"tiers": {"claude": {"judge": {"effort": "low"}},
                                 "cursor": {"marshal": {"model": "grok-4.6"}}}}}
    assert T.tier_model(cfg, "claude", "judge") == {"model": "claude-opus-5-5", "effort": "low"}
    assert T.tier_model(cfg, "pi", "judge")["model"] == "anthropic/claude-sonnet-5-5"
    assert T.tier_model(cfg, "cursor", "marshal") == {"model": "grok-4.6", "effort": "low"}
    (root / ".nw" / "tier_map.yaml").write_text(
        "schema: nw.tier_map.v1\nclaude: {judge: {model: claude-haiku-5-5}}\n")
    assert T.tier_model(cfg, "claude", "judge", project_root=root) == {
        "model": "claude-haiku-5-5", "effort": "low"}
    assert T.tier_model(cfg, "claude", "judge")["model"] == "claude-opus-5-5"


@pytest.mark.parametrize("doc,needle", [
    ({"cursor": {"judge": {"model": "grok-4.7", "effort": "max"}}},
     "cursor.judge.effort: grok-4.7 takes low, medium, high, xhigh"),
    ({"cursor": {"judge": {"model": "claude-sonnet-5-5"}}}, "must be a Cursor id"),
    ({"pi": {"deep": {"model": "gpt-6.1-terra"}}}, "pi needs provider/id"),
    ({"pi": {"deep": {"model": "openai/x/y"}}}, "pi needs provider/id"),
    ({"claude": {"judge": {"model": "gpt-5"}}}, "must be a claude-* id"),
    ({"claude": {"judge": {"model": "claude-x", "effort": "turbo"}}}, "effort: must be one of"),
    ({"claude": {"huge": {"model": "claude-x"}}}, "unknown tier"),
    ({"gemini": {"judge": {"model": "g"}}}, "unknown backend"),
    ({"claude": {"judge": {"model": "claude-x", "temp": 1}}}, "unknown key"),
])
def test_tier_map_rejects_bad_rows(doc, needle):
    problems = T.validate_tier_map_doc(doc, origin="x", in_file=False)
    assert any(needle in p for p in problems), problems
    with pytest.raises(ValueError, match="invalid tier map"):
        T.load_tier_map({"agentic": {"tiers": doc}})


def test_tier_map_file_needs_its_schema(tmp_path):
    (tmp_path / "tier_map.yaml").write_text("claude: {judge: {model: claude-x}}\n")
    with pytest.raises(ValueError, match="schema: must be 'nw.tier_map.v1'"):
        T.load_tier_map({"config_dir": str(tmp_path)})


def test_builtin_cursor_rows_take_their_effort_parameter():
    from nightwright.agentic.cursor_map import sdk_selection
    from nightwright.agentic.model_sets import cursor_id
    rows = T.load_tier_map({})["cursor"]
    sels = {t: sdk_selection(cursor_id(r["model"], r["effort"])) for t, r in rows.items()}
    assert sels["marshal"]["id"] == "grok-4.7"
    assert {"id": "reasoning_effort", "value": "low"} in sels["marshal"]["params"]
    assert {"id": "reasoning", "value": "high"} in sels["deep"]["params"]


# ---- flags -----------------------------------------------------------------------------------

def test_effective_lean_defaults_overlay_and_dependencies():
    from nightwright.config import DEFAULT_CONFIG
    off = {"tiers": False, "packet": False, "collator": False, "sessions": False}
    assert T.effective_lean({"agentic": dict(DEFAULT_CONFIG["agentic"])}, "Meta-Tools") == (off, [])
    cfg = {"agentic": {"lean": {"tiers": True}, "project_lean": {"P": {"tiers": False,
                                                                       "sessions": True}}}}
    assert T.effective_lean(cfg, "Q")[0] == dict(off, tiers=True)
    flags, why = T.effective_lean(cfg, "P")
    assert flags == off and why == ["sessions off: needs packet and collator"]
    flags, why = T.effective_lean({"agentic": {"lean": {"packet": True, "collator": True,
                                                        "tiers": "yes"}}})
    assert flags == dict(off, packet=True, collator=True) and why == []   # only literal true


# ---- build_request ---------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("NW_AGENTIC_RUNNER", raising=False)
    cfg = {"config_dir": str(tmp_path / "cfg"), "data_dir": str(tmp_path / "data"),
           "agentic": {"runner": "claude", "profiles": {}, "default_profile": "balanced"}}
    (tmp_path / "cfg" / "personas").mkdir(parents=True)
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


def _req(env, persona, *, lean=None, state_extra=None, cfg_extra=None, profile=None):
    cfg, root, state, tmp = env
    cfg = {**cfg, "agentic": {**cfg["agentic"], **(cfg_extra or {})}}
    st = dict(state, **(state_extra or {}))
    if lean is not None:
        st["lean"] = lean
    prof = resolve_profile(cfg, profile, project_root=root)
    return PS.build_request(cfg, prof, {"_root": str(root)}, st, persona,
                            run_dir=tmp / "runs" / f"001-{persona}")


ON = {"tiers": True, "packet": False, "collator": False, "sessions": False}


def _fields(req):
    return {f.name: getattr(req, f.name) for f in dataclasses.fields(req)
            if f.name not in ("env", "run_dir")}


@pytest.mark.parametrize("backend,profile", [
    ({"runner": "claude"}, None), ({"runner": "cursor"}, None), ({"runner": "pi"}, None),
    ({"model_set": "grok"}, None), ({"model_set": "gpt"}, None), ({"runner": "claude"}, "frugal"),
    ({"runner": "cursor"}, "thorough")], ids=lambda v: str(v))
@pytest.mark.parametrize("persona", sorted(PS.PERSONAS))
def test_flag_off_is_byte_identical(env, persona, backend, profile):
    """Every persona, every backend, a model set and a non-default profile: an absent
    `state.lean` and a pinned all-off one give today's request, field for field."""
    today = _fields(_req(env, persona, cfg_extra=backend, profile=profile))
    for lean in ({}, dict(ON, tiers=False)):
        assert _fields(_req(env, persona, lean=lean, cfg_extra=backend,
                            profile=profile)) == today
    assert today["tier"] is None


def test_flag_off_request_is_todays_model(env):
    assert _req(env, "conformance_reviewer").model == "opus"
    assert _req(env, "implementer").model == "sonnet"


@pytest.mark.parametrize("persona,model,effort,tier", [
    ("conformance_reviewer", "claude-sonnet-5-5", "high", "judge"),
    ("adversarial_reviewer", "claude-sonnet-5-5", "high", "judge"),
    ("implementer", "claude-sonnet-5-5", "high", "judge"),
    ("gate_triager", "claude-haiku-5-5", "medium", "marshal"),
    ("review_triager", "claude-haiku-5-5", "medium", "marshal"),
])
def test_claude_runs_take_the_tier_model(env, persona, model, effort, tier):
    req = _req(env, persona, lean=ON)
    assert (req.model, req.effort, req.tier, req.model_alias, req.runner) == (
        model, effort, tier, None, None)
    off = _req(env, persona)
    assert (req.max_budget_usd, req.permission_mode, req.timeout_s) == (
        off.max_budget_usd, off.permission_mode, off.timeout_s)      # still the profile's


def test_no_persona_runs_deep_at_default(env):
    for persona in PS.PERSONAS:
        assert "opus" not in _req(env, persona, lean=ON).model


def test_explicit_profile_model_overrides_the_tier(env):
    over = {"profiles": {"balanced": {"personas": {"conformance_reviewer": {"model": "opus"}}}}}
    req = _req(env, "conformance_reviewer", lean=ON, cfg_extra=over)
    assert (req.model, req.tier) == ("opus", "judge")
    assert _req(env, "test_reviewer", lean=ON, cfg_extra=over).model == "claude-sonnet-5-5"


def test_project_profile_model_overrides_the_tier(env):
    cfg, root, state, tmp = env
    (root / ".nw" / "profiles").mkdir()
    (root / ".nw" / "profiles" / "balanced.yaml").write_text(
        "schema: nw.profile.v1\npersonas:\n  gate_triager: {model: claude-opus-5-5}\n")
    assert _req(env, "gate_triager", lean=ON).model == "claude-opus-5-5"


def test_deep_close_pass_is_deep_even_over_an_override(env):
    over = {"profiles": {"balanced": {"personas": {"adversarial_reviewer": {"model": "haiku"}}}}}
    pending = {"deep_close": {"status": "pending", "loop": 0}}
    req = _req(env, "adversarial_reviewer", lean=ON, cfg_extra=over, state_extra=pending)
    assert (req.model, req.effort, req.tier) == ("claude-opus-5-5", "high", "deep")
    assert _req(env, "conformance_reviewer", lean=ON, state_extra=pending).tier == "judge"
    done = {"deep_close": {"status": "done", "loop": 0}}
    assert _req(env, "adversarial_reviewer", lean=ON, state_extra=done).model == "claude-sonnet-5-5"


def test_cursor_backend_maps_the_tier_and_keeps_cursor_model_override(env):
    cursor = {"runner": "cursor"}
    req = _req(env, "conformance_reviewer", lean=ON, cfg_extra=cursor)
    assert (req.model, req.tier, req.model_alias) == ("grok-4.7-high", "judge", "opus")
    assert _req(env, "gate_triager", lean=ON, cfg_extra=cursor).model == "grok-4.7-low"
    pinned = {**cursor, "profiles": {"balanced": {"personas": {
        "implementer": {"cursor_model": "composer-2.5"}}}}}
    assert _req(env, "implementer", lean=ON, cfg_extra=pinned).model == "composer-2.5"


def test_pi_backend_maps_provider_id_and_thinking(env):
    req = _req(env, "test_reviewer", lean=ON, cfg_extra={"runner": "pi"})
    assert (req.pi_provider, req.pi_id, req.pi_thinking, req.model, req.tier) == (
        "openai", "gpt-5.6-sol", "high", "openai/gpt-5.6-sol", "judge")
    req = _req(env, "review_triager", lean=ON, cfg_extra={"runner": "pi"})
    assert (req.pi_id, req.pi_thinking) == ("gpt-5.6-luna", "medium")


def test_model_set_per_persona_model_overrides_set_model_does_not(env):
    grok = {"model_set": "grok"}
    impl = _req(env, "implementer", lean=ON, cfg_extra=grok)
    assert (impl.model, impl.runner) == ("composer-2.5", "cursor")            # per-persona
    rev = _req(env, "conformance_reviewer", lean=ON, cfg_extra=grok)
    assert (rev.model, rev.runner, rev.tier) == ("grok-4.7-high", "cursor", "judge")
    tri = _req(env, "gate_triager", lean=ON, cfg_extra=grok)
    assert tri.model == "grok-4.7-low"
    gpt = _req(env, "test_reviewer", lean=ON, cfg_extra={"model_set": "gpt"})
    assert (gpt.pi_id, gpt.runner) == ("gpt-5.6-luna", "pi")                  # set's own pick
    mixed = _req(env, "implementer", lean=ON, cfg_extra={"model_set": "mixed"})
    assert (mixed.model, mixed.runner) == ("claude-sonnet-5-5", "claude")


def test_spec_writer_is_told_not_to_write_size_or_tone(env):
    assert PS.LEAN_SPEC_WRITER_NOTE in _req(env, "spec_writer", lean=ON).prompt
    assert PS.LEAN_SPEC_WRITER_NOTE not in _req(env, "spec_writer").prompt
    assert PS.LEAN_SPEC_WRITER_NOTE not in _req(env, "implementer", lean=ON).prompt


def test_run_record_and_request_carry_the_tier(env, tmp_path):
    from nightwright.agentic import runner as R
    req = _req(env, "gate_triager", lean=ON)
    req.run_dir.mkdir(parents=True)
    res = R.RunResult(exit_code=0, timed_out=False, interrupted=False, duration_s=0.1,
                      is_error=False, cost_usd=0.0, verdict=None)
    R.write_run_record(req, res, "2026-11-11T00:00:00")
    import json
    assert json.loads((req.run_dir / "run.json").read_text())["tier"] == "marshal"
    assert R._request_doc(req, "t")["tier"] == "marshal"
    off = _req(env, "gate_triager")
    off.run_dir.mkdir(parents=True, exist_ok=True)
    R.write_run_record(off, res, "t")
    assert "tier" not in json.loads((off.run_dir / "run.json").read_text())
    assert "tier" not in R._request_doc(off, "t")
    job = R.request_to_job(req, req.cwd)
    assert R.request_from_job(job, req.cwd, req.run_dir).tier == "marshal"
