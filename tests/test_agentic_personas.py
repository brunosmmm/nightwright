"""SPEC-0174: persona prompt assembly, verdict schemas, build_request, hidden `campaign prompt`."""
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright.agentic import personas as PS
from nightwright.agentic import store as S
from nightwright.agentic.profiles import resolve_profile
from nightwright.cli import cli
from nightwright.agentic import schemas as SC

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
        "repo": {"root": str(tmp_path / "repo"), "base": "main", "branch": "nw/x",
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
                       "allowed_tools": ["Bash(uv run nw *)"]}}


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
        if name == "spec_reviewer":       # no campaign/worktree: SPEC-0188 tests cover it
            continue
        if name == "review_triager":      # its own context (SPEC-0206): spec + triage_input
            continue
        if name == "spec_writer":         # no gates or previous runs (SPEC-0215)
            continue
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


def test_implementer_prompt_targeted_tests(tmp_path):
    ctx = PS.build_context(_cfg(tmp_path), _state(tmp_path), _project_cfg())
    p = " ".join(PS.build_prompt("implementer", ctx).split())   # unwrap the hard line breaks
    assert ("run targeted tests only: the test files you touched plus the tests of directly "
            "related modules") in p
    assert "Run the full test suite at most once, at the end, or skip it" in p
    assert ("gating runs the full suite after you return and the triager sends back any "
            "failures") in p
    assert ("The gate's full-suite run counts as the no-regressions check, so skipping your own "
            "full run does not block `status: done`") in p
    assert "say in the `### Test record` that the full suite was left to gating" in p
    # cheap gates: generic instruction, spec-lint only as the "for nightwright itself" example
    assert "The cheap gates among the project's gate commands" in p
    assert "(listed in the repo's `.nw/project.yaml` and allowed for you; for nightwright itself: spec-lint)" in p
    step5 = p.split("5. ")[1].split(" 6. ")[0]
    assert step5.count("spec-lint") == 1 and "for nightwright itself: spec-lint" in step5


def test_render_leaves_unknown_braces_alone():
    assert PS.render("a {{x}} {y} {{z}}", {"x": "1"}) == "a 1 {y} (none)"


# ---- build_request -----------------------------------------------------------------------

def test_build_request_implementer(tmp_path):
    cfg, st = _cfg(tmp_path), _state(tmp_path)
    req = PS.build_request(cfg, resolve_profile(cfg), _project_cfg(), st, "implementer",
                           run_dir=tmp_path / "runs" / "001-implementer")
    assert req.model == "sonnet" and req.effort == "high" and req.max_budget_usd == 4.0
    assert req.permission_mode == "bypassPermissions" and not req.read_only
    assert req.allowed_tools == ("Bash(uv run nw *)", "Bash(uv run pytest -q -x*)",
                                 "Bash(python3 tools/spec_lint.py*)")
    assert req.cwd == Path(st["repo"]["worktree"]) and req.add_dirs == (req.cwd,)
    assert req.timeout_s == 1800 and req.json_schema is PS.IMPLEMENTER_SCHEMA
    tmp = str(S.campaign_dir(cfg, st["id"]) / "tmp" / "run-001-implementer")    # SPEC-0253
    assert req.env == {"NW_CAMPAIGN": st["id"], "NW_DATA_DIR": cfg["data_dir"],
                       "NW_CONFIG_DIR": cfg["config_dir"], "NW_SPEC_ID": "SPEC-0001",
                       "NW_WORKTREE": st["repo"]["worktree"], "NW_BASE": "main",
                       "NW_PERSONA_RUN": "1", "NW_PERSONA": "implementer",   # SPEC-0425
                       "TMPDIR": tmp, "TEMP": tmp, "TMP": tmp}
    assert req.system_append.startswith("## nw queue invariants")
    assert req.prompt.endswith(req.system_append + "\n")


def test_build_request_sets_persona_flag(tmp_path):
    cfg, st = _cfg(tmp_path), _state(tmp_path)
    req = PS.build_request(cfg, resolve_profile(cfg), _project_cfg(), st, "implementer",
                           run_dir=tmp_path / "runs" / "001-implementer")
    assert req.env["NW_PERSONA_RUN"] == "1"
    assert req.env["NW_PERSONA"] == "implementer"           # SPEC-0425: note attribution


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


def _git_repo_with_change(tmp_path):
    """A repo at the state's worktree: `main` plus a branch commit touching src and tests."""
    import subprocess
    wt = tmp_path / "worktree"
    wt.mkdir(exist_ok=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1", "PATH": __import__("os").environ["PATH"]}

    def git(*args):
        subprocess.run(["git", "-C", str(wt), *args], check=True, capture_output=True, env=env)
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-q", "--allow-empty", "-m", "base")
    git("checkout", "-q", "-b", "nw/x")
    (wt / "src").mkdir()
    (wt / "src" / "a.py").write_text("print('hi')\n")
    (wt / "tests").mkdir()
    (wt / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    git("add", "-A")
    git("commit", "-q", "-m", "change")


def test_build_request_conformance_reviewer(tmp_path):
    _git_repo_with_change(tmp_path)
    cfg, st = _cfg(tmp_path), _state(tmp_path, state="REVIEWING")
    req = PS.build_request(cfg, resolve_profile(cfg), _project_cfg(), st, "conformance_reviewer",
                           run_dir=tmp_path / "r")
    assert req.read_only and req.allowed_tools == () and req.permission_mode == "dontAsk"
    assert req.model == "opus" and req.max_budget_usd == 1.5
    assert req.json_schema is PS.CONFORMANCE_REVIEWER_SCHEMA
    assert "+print('hi')" in req.prompt and "src/a.py" in req.prompt
    assert "- tests/test_a.py" in req.prompt and "- src/a.py" not in req.prompt
    assert "Use {braces} freely." in req.prompt and "### gate `pytest`" in req.prompt
    assert "{{" not in req.prompt
    again = PS.build_request(cfg, resolve_profile(cfg), _project_cfg(), st, "conformance_reviewer",
                             run_dir=tmp_path / "r")
    assert again.prompt == req.prompt                       # deterministic
    for name, model, budget in (("frugal", "sonnet", 0.6), ("thorough", "opus", 1.5)):
        prof = resolve_profile(cfg, name)
        r = PS.build_request(cfg, prof, _project_cfg(), st, "conformance_reviewer",
                             run_dir=tmp_path / "r")
        assert (r.model, r.max_budget_usd, r.read_only) == (model, budget, True)


def test_conformance_reviewer_schema_and_diff_truncation(tmp_path):
    from nightwright.agentic.runner import _conforms
    ok = {"verdict": "pass", "score": 90, "findings": [{"severity": "nit", "title": "t",
                                            "suggested_fix": "rename it"}],
          "ac_assessment": [{"ac": "a", "met": True, "evidence": "e"}], "fix_brief": ""}
    assert _conforms(ok, PS.CONFORMANCE_REVIEWER_SCHEMA)
    assert not _conforms({**ok, "verdict": "maybe"}, PS.CONFORMANCE_REVIEWER_SCHEMA)
    assert not _conforms({**ok, "findings": [{"severity": "huge", "title": "t",
                                                "suggested_fix": "x"}]},
                         PS.CONFORMANCE_REVIEWER_SCHEMA)
    assert not _conforms({**ok, "findings": [{"severity": "nit", "title": "t"}]},
                         PS.CONFORMANCE_REVIEWER_SCHEMA)
    assert not _conforms({k: v for k, v in ok.items() if k != "score"},
                         PS.CONFORMANCE_REVIEWER_SCHEMA)
    assert PS.PERSONAS["conformance_reviewer"].read_only
    from nightwright.agentic import gitops
    _git_repo_with_change(tmp_path)
    cut = gitops.diff_text(tmp_path / "worktree", "main", max_lines=3)
    assert cut.splitlines()[:3] == gitops.diff_text(tmp_path / "worktree", "main").splitlines()[:3]
    assert "diff truncated" in cut.splitlines()[-1]


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
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["campaign", "prompt", st["id"], "implementer"])
    assert r.exit_code == 0, r.output
    assert "Use {braces} freely." in r.output and "implementer run 001: partial — half" in r.output
    r2 = CliRunner().invoke(cli, ["campaign", "prompt", st["id"], "reviewer"])
    assert r2.exit_code != 0
    r3 = CliRunner().invoke(cli, ["campaign", "prompt", "nope", "implementer"])
    assert r3.exit_code != 0 and "no campaign" in r3.output
    assert "prompt" not in CliRunner().invoke(cli, ["campaign", "--help"]).output.split("Commands:")[1]


def test_cli_campaign_prompt_resolves_the_projects_profile_layer(tmp_path, monkeypatch):
    cfg, st = _cfg(tmp_path), _state(tmp_path)
    S.create_campaign(cfg, st)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    d = tmp_path / "repo" / ".nw" / "profiles"
    d.mkdir(parents=True)
    f = d / "balanced.yaml"
    f.write_text("schema: nw.profile.v1\nslots: 0\n", encoding="utf-8")
    r = CliRunner().invoke(cli, ["campaign", "prompt", st["id"], "implementer"])
    assert r.exit_code != 0 and f"{f}: slots" in r.output
    f.write_text("schema: nw.profile.v1\nslots: 3\n", encoding="utf-8")
    assert CliRunner().invoke(cli, ["campaign", "prompt", st["id"], "implementer"]).exit_code == 0


def test_reviewer_personas_get_the_review_context(tmp_path, monkeypatch):
    """SPEC-0201: `build_request` gives the review context to a persona listed in
    REVIEWER_PERSONAS and to no other."""
    seen = {}
    monkeypatch.setattr(PS, "build_context", lambda *a, **kw: seen.update(review=kw["review"])
                        or {})
    monkeypatch.setattr(PS, "build_prompt", lambda p, ctx: "")
    monkeypatch.setattr(PS, "render", lambda text, ctx: "")
    cfg = {"agentic": {}}
    monkeypatch.setattr(PS, "REVIEWER_PERSONAS",
                        ("conformance_reviewer", "test_reviewer", "gate_triager"))
    cfg = {"data_dir": str(tmp_path / "data"),
           "agentic": {"profiles": {"balanced": {"reviewers": ["conformance_reviewer"]}}}}
    for persona, want in (("conformance_reviewer", True), ("gate_triager", True),
                          ("implementer", False)):
        PS.build_request(cfg, resolve_profile(cfg), {}, {"id": "c", "repo": {}}, persona,
                         run_dir=tmp_path)
        assert seen["review"] is want, persona


# ---- SPEC-0205: test reviewer ------------------------------------------------------------

def test_test_reviewer_registered_and_template():
    p = PS.PERSONAS["test_reviewer"]
    assert p.read_only and p.schema is PS.TEST_REVIEWER_SCHEMA
    assert "test_reviewer" in PS.REVIEWER_PERSONAS and "tests_touched" in PS.PLACEHOLDERS
    t = p.template
    assert "{{tests_touched}}" in t and "{{diff}}" in t
    assert "must_fix" in t and "removed" in t and "weakened" in t     # regression rule
    assert "`major`" in t and "`minor`" in t                          # severity mapping
    assert set(PS._PLACEHOLDER_RE.findall(t)) <= set(PS.PLACEHOLDERS)


def test_test_reviewer_schema_keys_and_conformance():
    from nightwright.agentic.runner import _conforms
    S_ = PS.TEST_REVIEWER_SCHEMA
    assert set(S_["required"]) == {"verdict", "score", "findings", "coverage_assessment",
                                   "fix_brief"}
    assert set(S_["properties"]) == set(PS.CONFORMANCE_REVIEWER_SCHEMA["properties"]) \
        - {"ac_assessment"} | {"coverage_assessment"}
    item = S_["properties"]["findings"]["items"]                    # SPEC-0295: not extended here
    assert item["required"] == ["severity", "title"]
    assert set(PS.CONFORMANCE_REVIEWER_SCHEMA["properties"]["findings"]["items"]["properties"]) \
        - set(item["properties"]) == {"suggested_fix", "acceptance"}
    assert S_["properties"]["coverage_assessment"]["items"]["required"] == ["ac", "covered"]
    ok = {"verdict": "pass", "score": 90, "findings": [{"severity": "nit", "title": "t"}],
          "coverage_assessment": [{"ac": "a", "covered": True, "tests": ["t::x"]}],
          "fix_brief": ""}
    assert _conforms(ok, S_)
    assert not _conforms({k: v for k, v in ok.items() if k != "coverage_assessment"}, S_)
    assert not _conforms({**ok, "coverage_assessment": [{"ac": "a"}]}, S_)
    assert not _conforms({**ok, "findings": [{"severity": "huge", "title": "t"}]}, S_)


def test_build_request_test_reviewer(tmp_path):
    _git_repo_with_change(tmp_path)
    cfg, st = _cfg(tmp_path), _state(tmp_path, state="REVIEWING")
    req = PS.build_request(cfg, resolve_profile(cfg), _project_cfg(), st, "test_reviewer",
                           run_dir=tmp_path / "r")
    assert req.read_only and req.allowed_tools == () and req.permission_mode == "dontAsk"
    assert req.json_schema is PS.TEST_REVIEWER_SCHEMA
    assert '"path": "tests/test_a.py"' in req.prompt and '"test_a"' in req.prompt
    assert "+print('hi')" in req.prompt and "{{" not in req.prompt
    again = PS.build_request(cfg, resolve_profile(cfg), _project_cfg(), st, "test_reviewer",
                             run_dir=tmp_path / "r")
    assert again.prompt == req.prompt
    other = PS.build_request(cfg, resolve_profile(cfg), _project_cfg(), st,
                             "conformance_reviewer", run_dir=tmp_path / "r")
    assert '"changed"' not in other.prompt                       # only this persona gets it


def test_tests_touched_only_for_the_test_reviewer(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(PS, "tests_touched", lambda wt, base: calls.append(base) or [])
    st = _state(tmp_path)
    assert PS.build_context({}, st, review=True)["tests_touched"] is None
    assert calls == []
    assert PS.build_context({}, st, review=True, with_tests=True)["tests_touched"] is None
    assert calls == ["main"]                         # an empty list renders as `(none)`
    assert "(none)" in PS.render("{{tests_touched}}", {"tests_touched": None})


class _Repo:
    """A tiny git repo: `commit(files)` on main, then `branch()` and `commit(files)` again."""

    def __init__(self, path):
        import os
        import subprocess
        self.path, self._sp = path, subprocess
        path.mkdir(parents=True, exist_ok=True)
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        self.git("init", "-q", "-b", "main")

    def git(self, *args):
        self._sp.run(["git", "-C", str(self.path), *args], check=True, env=self.env,
                     capture_output=True)

    def write(self, files):
        for rel, text in files.items():
            f = self.path / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)

    def commit(self, files, remove=(), mv=()):
        for a, b in mv:
            (self.path / b).parent.mkdir(parents=True, exist_ok=True)
            self.git("mv", a, b)
        for rel in remove:
            self.git("rm", "-q", rel)
        self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "c")

    def branch(self):
        self.git("checkout", "-q", "-b", "feature")


def _touched(repo):
    return {e["path"]: e for e in PS.tests_touched(repo.path, "main")}


BASE_TESTS = (
    "def test_keep():\n    assert 1\n\n"
    "def test_edit():\n    x = 1\n    assert x\n    assert x == 1\n\n"
    "@pytest.mark.skip\ndef test_deco():\n    pass\n\n"
    "class TestBox:\n    def test_in(self):\n        assert 1\n\n"
    "    class TestInner:\n        def test_deep(self):\n            assert 1\n\n"
    "    def test_untouched(self):\n        assert 2\n\n"
    "def test_gap_one():\n    pass\n\n\n\n"
    "async def test_async():\n    pass\n\n"
    "def test_gone():\n    pass\n")


def test_tests_touched_changed_removed_and_nested(tmp_path):
    r = _Repo(tmp_path / "r")
    r.commit({"tests/test_m.py": BASE_TESTS, "src/mod.py": "x = 1\n",
              "tests/conftest.py": "A = 1\n", "tests/data.json": "{}\n"})
    r.branch()
    new = BASE_TESTS.replace("    assert x == 1\n", "") \
        .replace("@pytest.mark.skip\ndef test_deco", "@pytest.mark.skipif(1)\ndef test_deco") \
        .replace("        assert 1\n\n    class TestInner", "        assert 11\n\n    class TestInner") \
        .replace("        def test_deep(self):\n            assert 1", "        def test_deep(self):\n            assert 3") \
        .replace("def test_gone():\n    pass\n", "")
    r.commit({"tests/test_m.py": new, "src/mod.py": "x = 2\n", "tests/conftest.py": "A = 2\n",
              "tests/data.json": '{"a": 1}\n'})
    got = _touched(r)
    assert set(got) == {"tests/test_m.py"}                   # src, conftest, json skipped
    e = got["tests/test_m.py"]
    assert e["status"] == "M" and e["removed"] == ["test_gone"]
    assert "test_edit" in e["changed"]                       # deleted assertion
    assert "test_deco" in e["changed"]                       # decorator edit
    assert "TestBox::test_in" in e["changed"]
    assert "TestBox.TestInner::test_deep" in e["changed"]
    assert "test_keep" not in e["changed"] and "TestBox::test_untouched" not in e["changed"]
    assert "test_async" not in e["changed"]
    assert e["changed"] == sorted(e["changed"])


def test_tests_touched_deleted_added_renamed(tmp_path):
    r = _Repo(tmp_path / "r")
    body = "def test_a():\n    assert 1\n\ndef test_b():\n    assert 2\n"
    r.commit({"tests/test_del.py": "def test_z():\n    assert 9\n" * 3 + body,
              "tests/test_ren.py": body + "# ren\n", "tests/test_ren2.py": body,
              "tests/helper.py": "def test_h():\n    pass\n", "docs/notes.txt": "n\n"})
    r.branch()
    renamed87 = "def test_a():\n    assert 1\n\ndef test_b():\n    assert 2\n\ndef test_c():\n    pass\n"
    r.commit({"tests/test_new.py": "def test_n():\n    pass\n",
              "tests/test_ren2_moved.py": renamed87,
              "tests/sub/test_deeper.py": "def test_d():\n    pass\n"},
             remove=["tests/test_del.py"],
             mv=[("tests/test_ren.py", "tests/test_ren_moved.py"),
                 ("tests/test_ren2.py", "tests/test_ren2_moved.py")])
    got = _touched(r)
    assert got["tests/test_del.py"] == {"path": "tests/test_del.py", "status": "D", "changed": [],
                                        "removed": ["test_a", "test_b", "test_z"]}
    assert got["tests/test_new.py"]["status"] == "A"
    assert got["tests/test_new.py"]["changed"] == ["test_n"]
    assert got["tests/test_ren_moved.py"] == {"path": "tests/test_ren_moved.py", "status": "R",
                                              "changed": [], "removed": []}   # R100
    ren = got["tests/test_ren2_moved.py"]
    assert ren["status"] == "R" and ren["changed"] == ["test_c"] and ren["removed"] == []
    assert "docs/notes.txt" not in got


def test_tests_touched_rename_across_test_boundary(tmp_path):
    r = _Repo(tmp_path / "r")
    r.commit({"tests/test_x.py": "def test_x():\n    pass\n",
              "tools/check.py": "def test_y():\n    pass\n"})
    r.branch()
    r.commit({}, mv=[("tests/test_x.py", "tools/x_helper.py"),
                     ("tools/check.py", "tests/test_check.py")])
    got = _touched(r)
    assert got["tools/x_helper.py"] == {"path": "tools/x_helper.py", "status": "R",
                                        "changed": [], "removed": ["test_x"]}
    assert got["tests/test_check.py"]["changed"] == ["test_y"]


def test_tests_touched_syntax_error_falls_back_to_regex(tmp_path):
    r = _Repo(tmp_path / "r")
    base = "def test_a():\n    assert 1\n\ndef test_b():\n    assert 2\n\ndef test_c():\n    pass\n"
    r.commit({"tests/test_s.py": base})
    r.branch()
    r.commit({"tests/test_s.py": base.replace("def test_b():\n    assert 2",
                                              "def test_b(:\n    assert 2") + "\ndef test_d():\n"})
    e = _touched(r)["tests/test_s.py"]
    assert "test_b" in e["changed"] and "test_d" in e["changed"]
    assert "test_a" not in e["changed"] and "test_c" not in e["changed"]
    assert e["removed"] == []
    r.commit({"tests/test_s.py": "def test_a(:\n"})        # base side parses, new side does not
    e = _touched(r)["tests/test_s.py"]
    assert e["removed"] == ["test_b", "test_c"]


def test_tests_touched_caps_long_lists(tmp_path):
    r = _Repo(tmp_path / "r")
    r.commit({"tests/test_big.py": "".join(f"def test_{i:03d}():\n    pass\n\n"
                                            for i in range(250))})
    r.branch()
    r.commit({}, remove=["tests/test_big.py"])
    e = _touched(r)["tests/test_big.py"]
    assert len(e["removed"]) == 201 and e["removed"][-1] == "\u2026 50 more"
    assert e["removed"][0] == "test_000"


def test_tests_touched_git_error_is_empty(tmp_path):
    assert PS.tests_touched(tmp_path / "missing", "main") == []
    r = _Repo(tmp_path / "r")
    r.commit({"a.txt": "a\n"})
    assert PS.tests_touched(r.path, "nope") == []


def test_tests_touched_is_deterministic_json(tmp_path):
    _git_repo_with_change(tmp_path)
    ctx = PS.build_context({}, _state(tmp_path), review=True, with_tests=True)
    again = PS.build_context({}, _state(tmp_path), review=True, with_tests=True)
    assert ctx["tests_touched"] == again["tests_touched"]
    assert json.loads(ctx["tests_touched"]) == [
        {"changed": ["test_a"], "path": "tests/test_a.py", "removed": [], "status": "A"}]


def test_test_reviewer_prompt_lists_changed_and_removed(tmp_path):
    r = _Repo(tmp_path / "worktree")
    r.commit({"tests/test_k.py": "def test_kept():\n    assert 1\n\ndef test_dropped():\n    pass\n"})
    r.branch()
    r.commit({"tests/test_k.py": "def test_kept():\n    assert 2\n"})
    st = _state(tmp_path)
    ctx = PS.build_context({}, st, review=True, with_tests=True)
    prompt = PS.build_prompt("test_reviewer", ctx)
    assert PS.build_prompt("test_reviewer", ctx) == prompt
    assert '"changed": [\n      "test_kept"\n    ]' in prompt
    assert '"removed": [\n      "test_dropped"\n    ]' in prompt


# ---- SPEC-0206: review triager -----------------------------------------------------------

def test_review_triager_registered_and_template():
    p = PS.PERSONAS["review_triager"]
    assert p.read_only and p.schema is PS.REVIEW_TRIAGER_SCHEMA
    assert "review_triager" not in PS.REVIEWER_PERSONAS
    assert "triage_input" in PS.PLACEHOLDERS
    t = p.template
    assert "{{triage_input}}" in t and "{{spec_text}}" in t
    for word in ("subjective", "out of scope", "not a defect", "reason", "waive"):
        assert word in t
    assert set(PS._PLACEHOLDER_RE.findall(t)) <= set(PS.PLACEHOLDERS)


def test_review_triager_schema_keys():
    from nightwright.agentic.runner import _conforms
    S_ = PS.REVIEW_TRIAGER_SCHEMA
    assert S_["required"] == ["waive", "note"] and set(S_["properties"]) == {"waive", "note"}
    assert S_["properties"]["waive"]["items"]["required"] == ["id", "reason"]
    ok = {"waive": [{"id": "conformance_reviewer#0", "reason": "taste"}], "note": "n"}
    assert _conforms(ok, S_) and _conforms({"waive": [], "note": ""}, S_)
    assert not _conforms({"waive": []}, S_) and not _conforms({"note": "n"}, S_)
    assert not _conforms({"waive": [{"id": "x"}], "note": "n"}, S_)
    assert not _conforms({"waive": [{"reason": "r"}], "note": "n"}, S_)


def test_build_request_review_triager(tmp_path):
    cfg, st = _cfg(tmp_path), _state(tmp_path, state="REVIEWING")
    extra = {"triage_input": '{"waivable": [{"id": "conformance_reviewer#2"}]}'}
    req = PS.build_request(cfg, resolve_profile(cfg), _project_cfg(), st, "review_triager",
                           run_dir=tmp_path / "r", extra=extra)
    assert req.read_only and req.allowed_tools == () and req.permission_mode == "dontAsk"
    assert req.json_schema is PS.REVIEW_TRIAGER_SCHEMA and req.model == "sonnet"
    assert req.max_budget_usd == 0.5
    assert "conformance_reviewer#2" in req.prompt and "{{" not in req.prompt
    assert "Use {braces} freely." in req.prompt                     # the spec text
    assert "```diff" not in req.prompt                              # no review context
    again = PS.build_request(cfg, resolve_profile(cfg), _project_cfg(), st, "review_triager",
                             run_dir=tmp_path / "r", extra=extra)
    assert again.prompt == req.prompt
    bare = PS.build_request(cfg, resolve_profile(cfg), _project_cfg(), st, "review_triager",
                            run_dir=tmp_path / "r")
    assert "conformance_reviewer#2" not in bare.prompt
    frugal = PS.build_request(cfg, resolve_profile(cfg, "frugal"), _project_cfg(), st,
                              "review_triager", run_dir=tmp_path / "r", extra=extra)
    assert (frugal.model, frugal.max_budget_usd) == ("haiku", 0.25)


# ---- SPEC-0202: adversarial reviewer -------------------------------------------------------

ADV = "adversarial_reviewer"


def _adv_ok(**finding):
    f = {"severity": "major", "category": "bug", "title": "t"}
    f.update(finding)
    return {"verdict": "fix", "score": 40, "findings": [f], "fix_brief": "fix it"}


def test_adversarial_template_checklist_severity_and_trigger_rule():
    text = PS.PERSONAS[ADV].template
    for needle in ("shell=True", "unvalidated paths", "tempfile", "secrets or tokens",
                   "read-modify-write", "time-of-check", "swallowed", "non-atomic",
                   "destructive git", "unbounded", "A finding without a trigger is at most",
                   "`must_fix`", "`major`", "`minor`", "`nit`", "{{changed_files}}", "{{diff}}",
                   "{{spec_text}}", "{{gate_results}}", "{{previous_verdicts}}",
                   "{{pending_brief}}"):
        assert needle in text, needle
    for cat in PS.ADVERSARIAL_CATEGORIES:
        assert f"`{cat}`" in text, cat
    assert PS.PERSONAS[ADV].read_only and ADV in PS.REVIEWER_PERSONAS


def test_adversarial_schema_shape():
    s = PS.ADVERSARIAL_REVIEWER_SCHEMA
    assert s["required"] == ["verdict", "score", "findings", "fix_brief"]
    item = s["properties"]["findings"]["items"]
    assert item["required"] == ["severity", "category", "title"]
    assert item["properties"]["category"]["enum"] == [
        "bug", "logic", "security", "injection", "path_traversal", "subprocess", "secrets",
        "concurrency", "error_handling", "data_loss", "resource_leak", "other"]
    assert item["properties"]["trigger"] == {"type": "string"}
    assert item["properties"]["lines"] == {"type": "array", "items": {"type": "string"}}


def test_adversarial_verdict_ok_needs_a_known_category():
    from nightwright.agentic.runner import _conforms
    S_ = PS.ADVERSARIAL_REVIEWER_SCHEMA
    assert _conforms(_adv_ok(trigger="when x", lines=["a.py:3"], files=["a.py"]), S_)
    assert _conforms(_adv_ok(category="path_traversal"), S_)
    bad = _adv_ok()
    del bad["findings"][0]["category"]
    assert not _conforms(bad, S_)
    assert not _conforms(_adv_ok(category="vibes"), S_)
    assert not _conforms({k: v for k, v in _adv_ok().items() if k != "fix_brief"}, S_)
    assert not _conforms(_adv_ok(lines="a.py:3"), S_)


def test_build_request_adversarial_reviewer(tmp_path):
    _git_repo_with_change(tmp_path)
    cfg, st = _cfg(tmp_path), _state(tmp_path, state="REVIEWING")
    req = PS.build_request(cfg, resolve_profile(cfg), _project_cfg(), st, ADV,
                           run_dir=tmp_path / "r")
    assert req.read_only and req.allowed_tools == () and req.permission_mode == "dontAsk"
    assert req.model == "opus" and req.max_budget_usd == 1.5 and req.effort == "high"
    assert req.json_schema is PS.ADVERSARIAL_REVIEWER_SCHEMA
    assert "+print('hi')" in req.prompt and "{{" not in req.prompt
    assert "Use {braces} freely." in req.prompt
    assert "src/a.py\ntests/test_a.py" in req.prompt              # changed_files, sorted
    again = PS.build_request(cfg, resolve_profile(cfg), _project_cfg(), st, ADV,
                             run_dir=tmp_path / "r")
    assert again.prompt == req.prompt
    for name, model, effort, budget in (("frugal", "sonnet", "medium", 0.6),
                                        ("thorough", "opus", "high", 2.5)):
        r = PS.build_request(cfg, resolve_profile(cfg, name), _project_cfg(), st, ADV,
                             run_dir=tmp_path / "r")
        assert (r.model, r.effort, r.max_budget_usd, r.read_only) == (model, effort, budget, True)


def test_conformance_template_ignores_changed_files(tmp_path):
    assert "{{changed_files}}" not in PS.PERSONAS["conformance_reviewer"].template
    assert "changed_files" in PS.PLACEHOLDERS


def _changed_ctx(tmp_path, monkeypatch, paths):
    monkeypatch.setattr(PS.git, "diff_text", lambda *a, **kw: "d")
    monkeypatch.setattr(PS.git, "changed_files", lambda *a, **kw: paths)
    return PS.build_context(_cfg(tmp_path), _state(tmp_path), None, review=True)


def test_changed_files_sorted_and_plain(tmp_path, monkeypatch):
    ctx = _changed_ctx(tmp_path, monkeypatch, ["z.py", "tests/t.py", "a/b.py"])
    assert ctx["changed_files"] == "a/b.py\ntests/t.py\nz.py"
    assert ctx["test_files"] == ["tests/t.py"]
    assert PS.render("{{changed_files}}", ctx) == "a/b.py\ntests/t.py\nz.py"


def test_changed_files_caps_at_300(tmp_path, monkeypatch):
    names = [f"f{i:04d}.py" for i in range(305)]
    out = _changed_ctx(tmp_path, monkeypatch, list(reversed(names)))["changed_files"].split("\n")
    assert out[:300] == names[:300] and out[300] == "… 5 more" and len(out) == 301
    exact = _changed_ctx(tmp_path, monkeypatch, names[:300])["changed_files"].split("\n")
    assert len(exact) == 300 and "more" not in exact[-1]


def test_changed_files_none_when_empty_or_git_error(tmp_path, monkeypatch):
    assert _changed_ctx(tmp_path, monkeypatch, [])["changed_files"] == "(none)"

    def boom(*a, **kw):
        raise PS.git.GitError(["git", "diff"], 1, "nope")
    monkeypatch.setattr(PS.git, "changed_files", boom)
    ctx = PS.build_context(_cfg(tmp_path), _state(tmp_path), None, review=True)
    assert PS.render("{{changed_files}}", ctx) == "(none)"
    assert ctx["diff"] is None and ctx["test_files"] is None      # all three stay unset
    assert PS.build_context(_cfg(tmp_path), _state(tmp_path), None)["changed_files"] is None


def test_render_pending_brief_bracket_prefers_gate_then_ac_then_category():
    def brief(**item):
        return PS.render_pending_brief({"source": "review", "must_fix": [dict(title="T", **item)]})
    assert "- MUST FIX [path_traversal]: T" in brief(category="path_traversal")
    assert "- MUST FIX [AC1]: T" in brief(ac="AC1", category="bug")
    assert "- MUST FIX [pytest]: T" in brief(gate="pytest", ac="AC1", category="bug")
    assert "- MUST FIX: T" in brief()
    out = brief(category="path_traversal", files=["a.py"], detail="x\ntrigger: when ../")
    assert "[path_traversal]: T (a.py) — x\ntrigger: when ../" in out


def test_spec_reviewer_schema_requires_size_tone_rationale():          # SPEC-0219
    sch = PS.SPEC_REVIEWER_SCHEMA
    assert {"size", "tone", "rationale"} <= set(sch["required"])
    props = sch["properties"]
    assert props["size"]["enum"] == ["xs", "s", "m", "l", "xl"]
    assert props["tone"]["enum"] == ["cost-effective", "high-end", "critical"]
    assert props["rationale"]["type"] == "string"
    t = PS.PERSONAS["spec_reviewer"].template
    for word in ("xs", "xl", "cost-effective", "high-end", "critical"):
        assert word in t


# ---- conflict_resolver (SPEC-0221) ---------------------------------------------------------

def test_conflict_resolver_registered_write_capable_with_own_invariant():
    p = PS.PERSONAS["conflict_resolver"]
    assert not p.read_only and p.invariant is PS.RESOLVER_INVARIANT
    s = PS.CONFLICT_RESOLVER_SCHEMA
    assert s["required"] == ["status", "summary", "resolutions"]
    assert s["properties"]["status"]["enum"] == ["resolved", "gave_up"]
    item = s["properties"]["resolutions"]["items"]
    assert item["required"] == ["path", "strategy", "how"]
    assert item["properties"]["strategy"]["enum"] == ["both", "ours", "theirs", "regenerated",
                                                     "rewritten"]
    assert s["properties"]["questions"]["items"] == {"type": "string"}
    inv = PS.RESOLVER_INVARIANT
    assert "Do not commit" not in inv
    allowed_part = inv.split("Forbidden git:")[0]               # SPEC-0252
    assert "`commit` (never `--amend`)" in allowed_part
    assert "the runner commits any tracked or staged changes" in inv
    assert "extra commit must not revert a campaign commit" in inv
    for allowed in ("rebase {{rebase_target}}", "rebase --continue", "rebase --abort", "add",
                    "checkout --ours/--theirs -- <path>", "status", "diff", "log", "show"):
        assert allowed in inv, allowed
    assert "`rebase --skip` (only for a commit you then list in `dropped_commits`)" \
        in allowed_part                                           # SPEC-0259
    assert "dropped_commits" in inv and "Never use this to skip a conflict" in inv
    for forbidden in ("push", "merge", "reset", "commit --amend", "switch", "checkout <branch>",
                      "branch -f", "worktree", "stash"):
        assert forbidden in inv.split("Forbidden git:")[1], forbidden
    text = p.template
    for ph in ("{{rebase_target}}", "{{conflicts}}", "{{campaign_diff}}", "{{base_diff}}",
               "{{landed}}", "{{regenerate}}", "{{spec_rel_path}}", "{{spec_text}}"):
        assert ph in text, ph
    for word in ("**ours**", "**theirs**", "gave_up", "Never hand-edit", "refuses the merge",
                 "Clock Log"):
        assert word in text or word.replace("**", "") in text, word


def test_conflict_resolver_dropped_commits_schema_and_prompt():          # SPEC-0259
    import jsonschema
    s = PS.CONFLICT_RESOLVER_SCHEMA
    assert "dropped_commits" not in s["required"]
    base = {"status": "resolved", "summary": "s", "resolutions": []}

    def ok(dropped):
        return jsonschema.Draft202012Validator(s).is_valid({**base, "dropped_commits": dropped})
    assert jsonschema.Draft202012Validator(s).is_valid(base)
    assert ok([{"sha": "abc1234", "reason": "fixed upstream another way"}])
    assert not ok([{"sha": "abc1234"}]) and not ok([{"reason": "r"}])
    assert not ok([{"sha": "", "reason": "r"}]) and not ok([{"sha": "abc", "reason": ""}])
    text = PS.PERSONAS["conflict_resolver"].template
    assert "git rebase --skip" in text and "only for such a commit" in text
    assert '"dropped_commits"' not in text and "`dropped_commits`" in text


def test_conflict_resolver_build_request_follows_implementer_rules(tmp_path):
    cfg, st = _cfg(tmp_path), _state(tmp_path)
    st["closing"] = {"attempts": 0, "resolve": {"target_sha": "abc123", "conflicts": ["a.py"]}}
    req = PS.build_request(cfg, resolve_profile(cfg), _project_cfg(), st, "conflict_resolver",
                           run_dir=tmp_path / "runs" / "001-conflict_resolver")
    assert req.model == "sonnet" and req.effort == "high" and req.max_budget_usd == 2.0
    assert req.permission_mode == "bypassPermissions" and not req.read_only
    assert req.allowed_tools == ("Bash(uv run nw *)", "Bash(uv run pytest -q -x*)",
                                 "Bash(python3 tools/spec_lint.py*)")
    assert req.env["NW_REBASE_TARGET"] == "abc123"
    assert req.json_schema is PS.CONFLICT_RESOLVER_SCHEMA
    assert req.system_append.startswith("## nw queue invariants")
    assert "rebase abc123" in req.system_append and "{{" not in req.prompt
    assert req.prompt.endswith(req.system_append + "\n")


def test_build_request_carries_profile_and_sources(tmp_path):                    # SPEC-0238
    cfg, st = _cfg(tmp_path), _state(tmp_path)
    prof = resolve_profile(cfg)
    req = PS.build_request(cfg, prof, _project_cfg(), st, "implementer",
                           run_dir=tmp_path / "runs" / "001-implementer")
    assert req.profile == "balanced" and req.profile_sources == ("builtin:balanced.yaml",)
    hand = PS.build_request(cfg, {k: v for k, v in prof.items() if k != "sources"},
                            _project_cfg(), st, "implementer", run_dir=tmp_path / "r2")
    assert hand.profile_sources == ()


def test_run_request_defaults_and_run_record(tmp_path):                          # SPEC-0238
    import json
    from nightwright.agentic import runner as RU
    run_dir = tmp_path / "camp" / "runs" / "001-implementer"
    run_dir.mkdir(parents=True)
    kw = dict(persona="implementer", prompt="p", system_append="s", cwd=tmp_path, model="sonnet",
              effort=None, max_budget_usd=1.0, timeout_s=10, permission_mode="dontAsk",
              read_only=True, allowed_tools=(), add_dirs=(), json_schema={}, env={},
              run_dir=run_dir)
    result = RU.RunResult(0, False, False, 1.0, False, 0.1)
    old = RU.RunRequest(**kw)                                    # existing constructions still work
    assert old.profile is None and old.profile_sources == ()
    RU.write_run_record(old, result, "t")
    rec = json.loads((run_dir / "run.json").read_text())
    assert rec["profile"] is None and rec["profile_sources"] == []
    new = RU.RunRequest(**kw, profile="x", profile_sources=("builtin:x.yaml", "config.yaml:y"))
    RU.write_run_record(new, result, "t")
    rec = json.loads((run_dir / "run.json").read_text())
    assert rec["profile"] == "x" and rec["profile_sources"] == ["builtin:x.yaml", "config.yaml:y"]


# ---- SPEC-0235: human overrides in the reviewer brief ------------------------------------

OVERRIDES = {"waive": [{"id": "conformance_reviewer#0", "title": "Wrong order",
                        "files": ["a.py"], "severity": "major", "reason": "intended"}],
             "attest": [{"ac": "AC 4 output is sorted", "reason": "checked by hand"}]}


def test_render_human_overrides_empty_and_populated():
    assert PS.render_human_overrides({}) == ""
    assert PS.render_human_overrides({"human_overrides": {"waive": [], "attest": []}}) == ""
    out = PS.render_human_overrides({"human_overrides": OVERRIDES})
    assert "do not raise them again, even reworded" in out
    assert "major Wrong order (a.py) — intended" in out
    assert "do not assess them as unmet" in out
    assert "AC 4 output is sorted — checked by hand" in out
    assert "human_overrides" in PS.PLACEHOLDERS


@pytest.mark.parametrize("persona", ["conformance_reviewer", "test_reviewer"])
def test_reviewer_prompts_carry_the_human_overrides(tmp_path, persona):
    _git_repo_with_change(tmp_path)
    cfg = _cfg(tmp_path)
    plain = PS.build_request(cfg, resolve_profile(cfg), _project_cfg(),
                             _state(tmp_path, state="REVIEWING"), persona, run_dir=tmp_path / "r")
    assert "Wrong order" not in plain.prompt and "Human overrides" not in plain.prompt
    st = dict(_state(tmp_path, state="REVIEWING"), human_overrides=OVERRIDES)
    req = PS.build_request(cfg, resolve_profile(cfg), _project_cfg(), st, persona,
                           run_dir=tmp_path / "r")
    assert "Wrong order" in req.prompt and "AC 4 output is sorted — checked by hand" in req.prompt
    assert "{{" not in req.prompt


def test_render_gate_results_summary_line():
    lg = {"results": [
        {"name": "pytest", "required": True, "ok": False, "exit_code": 1, "tail": "x",
         "summary": {"format": "pytest", "passed": 120, "failed": 3, "errors": 0, "skipped": 2,
                     "failing": ["a::b", "c::d"]}},
        {"name": "junit", "required": True, "ok": True, "exit_code": 0, "tail": "",
         "summary": {"format": "junit", "error": "no such file"}},
        {"name": "plain", "required": True, "ok": True, "exit_code": 0, "tail": ""}]}
    out = PS.render_gate_results(lg)
    assert "results: 3 failed, 120 passed, 2 skipped; failing: a::b, c::d" in out
    assert "could not parse junit results (no such file)" in out
    assert out.count("results:") == 2          # none for the gate without a summary


# ---- SPEC-0295: suggested_fix / acceptance on spec and conformance findings -----------------

@pytest.mark.parametrize("schema", [PS.SPEC_REVIEWER_SCHEMA, PS.CONFORMANCE_REVIEWER_SCHEMA])
def test_finding_schema_requires_suggested_fix(schema):
    item = schema["properties"]["findings"]["items"]
    assert item["required"] == ["severity", "title", "suggested_fix"]
    assert item["properties"]["suggested_fix"] == {"type": "string"}
    assert item["properties"]["acceptance"] == {"type": "string"}
    assert "suggested_fix" not in PS.TEST_REVIEWER_SCHEMA["properties"]["findings"]["items"][
        "properties"]
    assert "suggested_fix" not in PS.ADVERSARIAL_REVIEWER_SCHEMA["properties"]["findings"][
        "items"]["properties"]


def test_spec_reviewer_verdict_needs_a_suggested_fix_per_finding():
    from nightwright.agentic.runner import _conforms
    ok = {"verdict": "revise", "size": "s", "tone": "high-end", "rationale": "r",
          "findings": [{"severity": "nit", "title": "t", "suggested_fix": "do it"}]}
    assert _conforms(ok, PS.SPEC_REVIEWER_SCHEMA)
    bad = dict(ok, findings=[{"severity": "nit", "title": "t"}])
    assert not _conforms(bad, PS.SPEC_REVIEWER_SCHEMA)


def test_reviewer_and_writer_prompts_ask_for_the_fix():
    spec = " ".join(PS.PERSONAS["spec_reviewer"].template.split())
    assert '"suggested_fix": "...", "acceptance": "..."' in spec
    assert "including `minor` and `nit`, has a `suggested_fix`" in spec
    conf = " ".join(PS.PERSONAS["conformance_reviewer"].template.split())
    assert '"suggested_fix": "...", "acceptance": "..."' in conf
    assert "including `minor` and `nit`, has a `suggested_fix`" in conf
    writer = " ".join(PS.PERSONAS["spec_writer"].template.split())
    assert "`Fix:` line" in writer and "`Done when:` line" in writer


def test_finding_text_parts_and_omissions():
    f = {"detail": "why", "suggested_fix": "do X", "acceptance": "X holds"}
    assert PS.finding_text(f) == "why\nFix: do X\nDone when: X holds"
    assert PS.finding_text({"suggested_fix": " do X "}) == "Fix: do X"
    assert PS.finding_text({"detail": "why", "suggested_fix": "  ", "acceptance": ""}) == "why"
    assert PS.finding_text({"detail": 3, "suggested_fix": ["x"], "acceptance": None}) is None
    assert PS.finding_text({}) is None
    assert PS.finding_text({"detail": "why"}) == "why"
    assert PS.finding_text({"detail": "ignored", "suggested_fix": "f"}, "given") == "given\nFix: f"


def test_render_pending_brief_shows_the_fix_lines():
    brief = {"source": "spec_reviewer", "must_fix": [
        {"title": "gap", "section": "Design",
         "detail": PS.finding_text({"detail": "why", "suggested_fix": "add X",
                                    "acceptance": "X is stated"})}]}
    text = PS.render_pending_brief(brief)
    assert "- MUST FIX [Design]: gap — why\nFix: add X\nDone when: X is stated" in text


def test_finding_detail_folds_trigger_then_the_fix():
    from nightwright.agentic import queue as Q
    f = {"detail": "d", "trigger": "t", "suggested_fix": "fix", "acceptance": "ok"}
    assert Q.finding_detail(f) == "d\ntrigger: t\nFix: fix\nDone when: ok"
    assert Q.finding_detail({"trigger": "t", "suggested_fix": "fix"}) == "trigger: t\nFix: fix"
    assert Q.finding_detail({"detail": "d"}) == "d" and Q.finding_detail({}) is None


def test_render_gate_results_retries_line():
    def att(*kinds_ok):
        return [{"attempt": 0, "kind": "full", "ok": False, "failing": ["a::b", "c::d"]}] + [
            {"attempt": i, "kind": k, "ok": ok, "failing": []}
            for i, (k, ok) in enumerate(kinds_ok, 1)]

    def render(ok, attempts, **extra):
        return PS.render_gate_results({"results": [
            {"name": "pytest", "required": True, "ok": ok, "exit_code": 0 if ok else 1,
             "tail": "x", "attempts": attempts, **extra}]})
    assert "retries: targeted rerun of 2 ids reproduced the failure" in render(
        False, att(("targeted", False)))
    out = render(True, att(("targeted", True), ("full", True)), flaky=["a::b"])
    assert "retries: targeted rerun of 2 ids passed, full rerun 1/1 passed (flaky: a::b)" in out
    assert "retries: targeted rerun passed but full rerun 1/1 failed (order-dependent" in render(
        False, att(("targeted", True), ("full", False)))
    assert "retries: full rerun 2/2 failed" in render(False, att(("full", False), ("full", False)))
    assert "retries:" not in render(False, [])
    assert "no full rerun left" in render(False, att(("targeted", True)))


def test_render_gate_results_failed_first_line():
    """SPEC-0358: the prelude is attempt -1 and never counts as a ladder retry."""
    def render(ok, attempts):
        return PS.render_gate_results({"results": [
            {"name": "pytest", "required": True, "ok": ok, "exit_code": 0 if ok else 1,
             "tail": "x", "attempts": attempts}]})
    ff = {"attempt": -1, "kind": "failed-first", "ok": False, "exit_code": 1, "failing": ["a::b"]}
    assert "failed-first: the previous round's 1 failing ids still fail" in render(False, [ff])
    main = {"attempt": 0, "kind": "full", "ok": True, "failing": []}
    assert render(True, [{**ff, "ok": True, "exit_code": 0}, main]).count("failed-first: previous failing ids passed") == 1
    assert "could not run (pytest exit 4)" in render(True, [{**ff, "exit_code": 4}, main])
    tgt = {"attempt": 1, "kind": "targeted", "ok": False, "failing": []}
    out = render(False, [{**ff, "ok": True, "exit_code": 0}, {**main, "ok": False,
                 "failing": ["a::b", "c::d"]}, tgt])
    assert "failed-first: previous failing ids passed; retries: targeted rerun of 2 ids reproduced" in out


def test_render_gate_results_retries_line_inconclusive_and_full_only():
    base = {"attempt": 0, "kind": "full", "ok": False, "failing": ["a::b", "c::d"]}

    def render(ok, *retries):
        attempts = [base] + [{"attempt": i, "failing": [], **a} for i, a in enumerate(retries, 1)]
        return PS.render_gate_results({"results": [
            {"name": "pytest", "required": True, "ok": ok, "exit_code": 0 if ok else 1,
             "tail": "x", "attempts": attempts}]})
    # a full-only ladder that ends in a pass
    out = render(True, {"kind": "full", "ok": False}, {"kind": "full", "ok": True})
    assert "retries: full rerun 1/2 passed" in out
    # a targeted rerun that was cut short did not reproduce anything
    for flag, word in (("timed_out", "timed out"), ("interrupted", "was interrupted")):
        out = render(False, {"kind": "targeted", "ok": False, flag: True})
        assert f"targeted rerun of 2 ids {word}" in out and "reproduced" not in out
    # pytest usage / no-tests exits: the ids never ran
    out = render(False, {"kind": "targeted", "ok": False, "exit_code": 4},
                 {"kind": "full", "ok": False})
    assert "could not run (pytest exit 4), full rerun 1/1 failed" in out
    assert "reproduced" not in out
    assert "could not run (pytest exit 5), no full rerun left" in render(
        False, {"kind": "targeted", "ok": False, "exit_code": 5})


# ---- SPEC-0293: known flaky tests in the gate-triager prompt ----------------------------------

def _failing_state(tmp_path):
    st = _state(tmp_path)
    st["last_gate"]["results"][0]["summary"] = {"failing": ["t::a", "t::fresh"]}
    return st


def test_triager_prompt_has_flaky_history(tmp_path):
    from nightwright.agentic import flaky
    cfg = _cfg(tmp_path)
    flaky.record(cfg, "Meta-Tools", ["t::a"], campaign="C1", source="rerun")
    flaky.record(cfg, "Meta-Tools", ["t::a", "t::other"], campaign="C2", source="triage_retry")
    p = PS.build_prompt("gate_triager", PS.build_context(cfg, _failing_state(tmp_path), _project_cfg()))
    assert "## Known flaky tests" in p
    assert "- t::a — flaked 2× in 2 campaigns" in p and "via rerun 1, triage retry 1" in p
    assert "t::fresh —" not in p and "t::other" not in p and "{{" not in p


def test_flaky_history_none_known(tmp_path):
    cfg = _cfg(tmp_path)
    st = _failing_state(tmp_path)
    p = PS.build_prompt("gate_triager", PS.build_context(cfg, st, _project_cfg()))
    assert "## Known flaky tests" in p and "(none known)" in p
    assert PS.render_flaky_history(cfg, dict(st, last_gate=None)) == "(none known)"
