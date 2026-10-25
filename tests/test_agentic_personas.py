"""SPEC-0174: persona prompt assembly, verdict schemas, build_request, hidden `campaign prompt`."""
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from wt.agentic import personas as PS
from wt.agentic import store as S
from wt.agentic.profiles import resolve_profile
from wt.cli import cli
from wt.agentic import schemas as SC

def _ev(cfg, cid, etype, payload=None, **kw):
    """append_event with the schema's required keys defaulted (SPEC-0192 fixtures)."""
    return S.append_event(cfg, cid, etype, SC.with_defaults(etype, payload), **kw)

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir(exist_ok=True)
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(tmp_path / "data"),
        "config_dir": str(tmp_path / "cfg"),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"),
        "agentic": {"profiles": {}, "default_profile": "balanced"},
    }


def _state(tmp_path, **over):
    wt = tmp_path / "worktree"
    (wt / "docs" / "specs").mkdir(parents=True, exist_ok=True)
    (wt / "docs" / "specs" / "0001-x.md").write_text(
        "---\nid: SPEC-0001\nstatus: accepted\n---\n\n## Decision\n\nUse {braces} freely.\n")
    st = {
        "id": "SPEC-0001-20261026-ab12", "kind": "implement", "spec_id": "SPEC-0001",
        "project": "Meta-Tools", "profile": "balanced", "state": "IMPLEMENTING",
        "repo": {"root": str(tmp_path / "repo"), "base": "main", "branch": "wt/x",
                 "worktree": str(wt)},
        "spec": {"rel_path": "docs/specs/0001-x.md", "internal": True},
        "counters": {"loops": 1, "runs": 2},
        "cost": {"total_usd": 0.5, "cap_usd": 10.0},
        "last_gate": {"seq": 1, "ok": False, "results": [
            {"name": "pytest", "required": True, "ok": False, "exit_code": 1,
             "tail": "\n".join(f"line {i}" for i in range(80))},
            {"name": "lint", "required": False, "ok": True, "exit_code": 0, "tail": ""}]},
        "pending_brief": {"source": "gate_triager", "fix_brief": "fix the import",
                          "must_fix": [{"gate": "pytest", "title": "ImportError",
                                        "files": ["src/a.py"]}],
                          "human_notes": ["prefer the small fix"]},
    }
    st.update(over)
    return st


def _project_cfg():
    return {"gates": [{"name": "pytest", "cmd": "uv run pytest -q -x"},
                      {"name": "lint", "cmd": "python3 tools/spec_lint.py"}],
            "agents": {"context_files": ["AGENTS.md", "docs/specs/SCHEMA.md"],
                       "permission_mode": "bypassPermissions",
                       "allowed_tools": ["Bash(uv run wt *)"]}}


# ---- prompt ------------------------------------------------------------------------------

def test_build_prompt_is_deterministic_and_complete(tmp_path):
    ctx = PS.build_context(_cfg(tmp_path), _state(tmp_path), _project_cfg(), loop_max=3,
                           previous=[{"persona": "implementer", "run_id": "001",
                                      "verdict": {"status": "partial", "summary": "half"}}])
    a = PS.build_prompt("implementer", ctx)
    b = PS.build_prompt("implementer", PS.build_context(_cfg(tmp_path), _state(tmp_path),
                                                        _project_cfg(), loop_max=3,
                                                        previous=[{"persona": "implementer",
                                                                   "run_id": "001",
                                                                   "verdict": {"status": "partial",
                                                                               "summary": "half"}}]))
    assert a == b
    assert "Use {braces} freely." in a                 # spec text, braces intact
    assert str(tmp_path / "worktree") in a
    assert "loop 1 of 3" in a
    assert "- AGENTS.md\n- docs/specs/SCHEMA.md" in a
    assert "### gate `pytest` — FAILED (required, exit 1)" in a
    assert "line 79" in a and "line 19" not in a       # tail bounded to 60 lines
    assert "(optional, exit 0)" in a
    assert "Brief: fix the import" in a and "MUST FIX [pytest]: ImportError (src/a.py)" in a
    assert "Human note: prefer the small fix" in a
    assert "implementer run 001: partial — half" in a
    assert a.rstrip().endswith("nothing else\n  you print is read.")
    assert "{{" not in a


def test_missing_values_render_as_none(tmp_path):
    st = _state(tmp_path, last_gate=None, pending_brief=None, counters={})
    st["spec"]["rel_path"] = "docs/specs/missing.md"
    ctx = PS.build_context(_cfg(tmp_path), st, None)
    p = PS.build_prompt("gate_triager", ctx)
    assert "{{" not in p
    assert p.count("(none)") >= 4          # context files, gates, brief, previous, spec text


def test_templates_and_schemas(tmp_path):
    for name, persona in PS.PERSONAS.items():
        text = persona.template
        for ph in ("{{campaign}}", "{{spec_id}}", "{{worktree}}", "{{spec_text}}",
                   "{{gate_results}}", "{{pending_brief}}", "{{previous_verdicts}}"):
            assert ph in text, (name, ph)
    assert PS.IMPLEMENTER_SCHEMA["required"] == ["status", "summary"]
    assert PS.IMPLEMENTER_SCHEMA["properties"]["status"]["enum"] == ["done", "partial", "blocked"]
    assert PS.GATE_TRIAGER_SCHEMA["required"] == ["disposition", "fix_brief"]
    assert PS.GATE_TRIAGER_SCHEMA["properties"]["disposition"]["enum"] == [
        "fix", "retry_gate", "waive", "escalate"]
    assert PS.PERSONAS["gate_triager"].read_only and not PS.PERSONAS["implementer"].read_only
    with pytest.raises(ValueError, match="unknown persona"):
        PS.get_persona("reviewer")


def test_personas_state_spec_change_guard_rules():
    impl = PS.PERSONAS["implementer"].template
    for needle in ("### As shipped", "### Test record", "`status:`", "`updated:`",
                   "checkbox ticks", "## Clock Log", "refuse the merge"):
        assert needle in impl, needle
    tri = PS.PERSONAS["gate_triager"].template
    assert "revert\n  ONLY the flagged hunks" in tri
    assert "Never instruct a whole-file revert" in tri
    assert "never \"the rules conflict\"" in tri


def test_render_leaves_unknown_braces_alone():
    assert PS.render("a {{x}} {y} {{z}}", {"x": "1"}) == "a 1 {y} (none)"


# ---- build_request -----------------------------------------------------------------------

def test_build_request_implementer(tmp_path):
    cfg, st = _cfg(tmp_path), _state(tmp_path)
    req = PS.build_request(cfg, resolve_profile(cfg), _project_cfg(), st, "implementer",
                           run_dir=tmp_path / "runs" / "001-implementer")
    assert req.model == "sonnet" and req.effort == "high" and req.max_budget_usd == 4.0
    assert req.permission_mode == "bypassPermissions" and not req.read_only
    assert req.allowed_tools == ("Bash(uv run wt *)", "Bash(uv run pytest -q -x*)",
                                 "Bash(python3 tools/spec_lint.py*)")
    assert req.cwd == Path(st["repo"]["worktree"]) and req.add_dirs == (req.cwd,)
    assert req.timeout_s == 1800 and req.json_schema is PS.IMPLEMENTER_SCHEMA
    assert req.env == {"WT_CAMPAIGN": st["id"], "WT_DATA_DIR": cfg["data_dir"],
                       "WT_CONFIG_DIR": cfg["config_dir"], "WT_SPEC_ID": "SPEC-0001",
                       "WT_WORKTREE": st["repo"]["worktree"], "WT_BASE": "main"}
    assert req.system_append.startswith("## wt queue invariants")
    assert req.prompt.endswith(req.system_append + "\n")


def test_build_request_project_allowlist_override(tmp_path):
    cfg, st = _cfg(tmp_path), _state(tmp_path)
    pc = _project_cfg()
    pc["agents"]["permission_mode"] = "dontAsk"
    req = PS.build_request(cfg, resolve_profile(cfg), pc, st, "implementer",
                           run_dir=tmp_path / "r")
    assert req.permission_mode == "dontAsk"


def test_build_request_triager_is_read_only(tmp_path):
    cfg, st = _cfg(tmp_path), _state(tmp_path)
    req = PS.build_request(cfg, resolve_profile(cfg), _project_cfg(), st, "gate_triager",
                           run_dir=tmp_path / "r")
    assert req.read_only and req.allowed_tools == () and req.max_budget_usd == 0.5
    assert req.permission_mode == "dontAsk" and req.json_schema is PS.GATE_TRIAGER_SCHEMA
    assert req.effort == "medium"


def test_build_request_unknown_persona(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError):
        PS.build_request(cfg, resolve_profile(cfg), {}, _state(tmp_path), "reviewer",
                         run_dir=tmp_path / "r")


# ---- hidden CLI --------------------------------------------------------------------------

def test_cli_campaign_prompt(tmp_path, monkeypatch):
    cfg, st = _cfg(tmp_path), _state(tmp_path)
    S.create_campaign(cfg, st)
    _ev(cfg, st["id"], "run.end", {"persona": "implementer", "run_id": "001",
                                             "verdict": {"status": "partial", "summary": "half"}})
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["campaign", "prompt", st["id"], "implementer"])
    assert r.exit_code == 0, r.output
    assert "Use {braces} freely." in r.output and "implementer run 001: partial — half" in r.output
    r2 = CliRunner().invoke(cli, ["campaign", "prompt", st["id"], "reviewer"])
    assert r2.exit_code != 0
    r3 = CliRunner().invoke(cli, ["campaign", "prompt", "nope", "implementer"])
    assert r3.exit_code != 0 and "no campaign" in r3.output
    assert "prompt" not in CliRunner().invoke(cli, ["campaign", "--help"]).output.split("Commands:")[1]
