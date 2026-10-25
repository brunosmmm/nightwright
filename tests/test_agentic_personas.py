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
        if name == "spec_reviewer":       # no campaign/worktree: SPEC-0188 tests cover it
            continue
        if name == "review_triager":      # its own context (SPEC-0206): spec + triage_input
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
    git("checkout", "-q", "-b", "wt/x")
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
    from wt.agentic.runner import _conforms
    ok = {"verdict": "pass", "score": 90, "findings": [{"severity": "nit", "title": "t"}],
          "ac_assessment": [{"ac": "a", "met": True, "evidence": "e"}], "fix_brief": ""}
    assert _conforms(ok, PS.CONFORMANCE_REVIEWER_SCHEMA)
    assert not _conforms({**ok, "verdict": "maybe"}, PS.CONFORMANCE_REVIEWER_SCHEMA)
    assert not _conforms({**ok, "findings": [{"severity": "huge", "title": "t"}]},
                         PS.CONFORMANCE_REVIEWER_SCHEMA)
    assert not _conforms({k: v for k, v in ok.items() if k != "score"},
                         PS.CONFORMANCE_REVIEWER_SCHEMA)
    assert PS.PERSONAS["conformance_reviewer"].read_only
    from wt.agentic import gitops
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
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["campaign", "prompt", st["id"], "implementer"])
    assert r.exit_code == 0, r.output
    assert "Use {braces} freely." in r.output and "implementer run 001: partial — half" in r.output
    r2 = CliRunner().invoke(cli, ["campaign", "prompt", st["id"], "reviewer"])
    assert r2.exit_code != 0
    r3 = CliRunner().invoke(cli, ["campaign", "prompt", "nope", "implementer"])
    assert r3.exit_code != 0 and "no campaign" in r3.output
    assert "prompt" not in CliRunner().invoke(cli, ["campaign", "--help"]).output.split("Commands:")[1]


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
    from wt.agentic.runner import _conforms
    S_ = PS.TEST_REVIEWER_SCHEMA
    assert set(S_["required"]) == {"verdict", "score", "findings", "coverage_assessment",
                                   "fix_brief"}
    assert set(S_["properties"]) == set(PS.CONFORMANCE_REVIEWER_SCHEMA["properties"]) \
        - {"ac_assessment"} | {"coverage_assessment"}
    assert S_["properties"]["findings"] == PS.CONFORMANCE_REVIEWER_SCHEMA["properties"]["findings"]
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
    from wt.agentic.runner import _conforms
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
