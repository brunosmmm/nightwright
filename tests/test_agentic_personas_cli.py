"""SPEC-0370: `nw personas list|show` over a temp config dir and project root."""
import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from nightwright.cli import cli

pytestmark = pytest.mark.fast


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = {"config_dir": str(tmp_path / "cfg")}
    proj = tmp_path / "proj"
    (proj / ".nw" / "personas").mkdir(parents=True)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    # Rich 15: size overrides require both dimensions (see tests/test_ideas.py).
    monkeypatch.setattr("nightwright.console.console._width", 400)
    monkeypatch.setattr("nightwright.console.console._height", 80)
    monkeypatch.setattr("nightwright.agentic.project.project_root", lambda c, name: proj.resolve())
    monkeypatch.setattr("nightwright.agentic.project.find_project_for_cwd", lambda c, cwd=None: None)
    return cfg, proj / ".nw" / "personas"


def _run(*args):
    return CliRunner().invoke(cli, ["personas", *args], env={"COLUMNS": "400"})


def _put(d, name, **over):
    (d / f"{name}.md").write_text("Review {{diff}}", encoding="utf-8")
    doc = {"schema": "nw.persona.v1", "name": name, "role": "reviewer",
           "template": f"{name}.md", "description": "security review", **over}
    (d / f"{name}.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def test_list_text_and_json(env):
    cfg, pdir = env
    _put(pdir, "security_reviewer")
    r = _run("list", "--project", "Proj")
    assert r.exit_code == 0, r.output
    assert "conformance_reviewer" in r.output and "security_reviewer" in r.output
    assert "builtin:persona_defs/conformance_reviewer.yaml" in r.output
    r = _run("list", "--project", "Proj", "--json")
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["schema"] == "nw.personas.v1"
    by = {p["name"]: p for p in d["personas"]}
    assert sum(1 for p in d["personas"] if p["scope"] == "builtin") == 10
    assert by["security_reviewer"]["scope"] == "project" and by["security_reviewer"]["reviewer"]
    assert by["conformance_reviewer"]["scope"] == "builtin"
    assert by["implementer"]["reviewer"] is False


def test_list_without_project_has_builtins_only(env):
    r = _run("list", "--json")
    assert r.exit_code == 0, r.output
    assert {p["scope"] for p in json.loads(r.output)["personas"]} == {"builtin"}


def test_show_text_and_json(env):
    r = _run("show", "conformance_reviewer")
    assert r.exit_code == 0, r.output
    assert "persona_defs/conformance_reviewer.yaml" in r.output
    assert "output_schema: conformance_reviewer" in r.output and "read_only: true" in r.output
    r = _run("show", "review_triager", "--json")
    d = json.loads(r.output)
    assert d["schema"] == "nw.persona.show.v1" and d["role"] == "system"
    assert d["adopt"] == {"step": "review", "personas": ["review_triager"]}
    assert d["invariant"] == "default" and d["order"] == 80 and d["problems"] == []
    assert d["template_path"].endswith("personas/review_triager.md")


def test_show_broken_definition_prints_problems(env):
    cfg, pdir = env
    _put(pdir, "security_reviewer", bogus=1)
    r = _run("show", "security_reviewer", "--project", "Proj")
    assert r.exit_code == 0, r.output
    assert "bogus: unknown key" in r.output and "problems:" in r.output
    d = json.loads(_run("show", "security_reviewer", "--project", "Proj", "--json").output)
    assert d["ok"] is False and any("bogus" in p for p in d["problems"])


def test_show_unknown_exits_nonzero_and_lists_known(env):
    r = _run("show", "nope")
    assert r.exit_code != 0
    assert "unknown persona 'nope'" in r.output and "implementer" in r.output


@pytest.mark.parametrize("over", [{"template": 123}, {"role": ["x"]}, {"role": 1},
                                  {"template": True}, {"template": "2026-11-02"}])
def test_non_string_values_in_a_broken_file_do_not_crash(env, over):
    cfg, pdir = env
    _put(pdir, "sec")
    doc = yaml.safe_load((pdir / "sec.yaml").read_text())
    doc.update(over)
    text = yaml.safe_dump(doc)
    if over.get("template") == "2026-11-02":
        text = text.replace("'2026-11-02'", "2026-11-02")      # a YAML date, not a string
    (pdir / "sec.yaml").write_text(text)
    for args in (("list",), ("list", "--json"), ("show", "sec"), ("show", "sec", "--json")):
        r = _run(*args, "--project", "Proj")
        assert r.exit_code == 0, (args, r.output, r.exception)
    d = json.loads(_run("show", "sec", "--project", "Proj", "--json").output)
    assert d["ok"] is False and d["problems"]


def test_user_file_named_like_a_builtin_never_displaces_it(env):
    cfg, pdir = env
    udir = Path(cfg["config_dir"]) / "personas"
    udir.mkdir(parents=True)
    _put(udir, "implementer")
    d = json.loads(_run("show", "implementer", "--json").output)
    assert d["scope"] == "builtin" and d["ok"] is True
    rows = [p for p in json.loads(_run("list", "--json").output)["personas"]
            if p["name"] == "implementer"]
    assert [(p["scope"], p["active"], p["ok"]) for p in rows] == [
        ("builtin", True, True), ("user", False, False)]
    assert "shadows builtin persona" in rows[1]["error"]
    assert "overridden" not in _run("list").output


def test_markup_in_user_values_is_printed_literally(env):
    cfg, pdir = env
    _put(pdir, "sec", description="all [/] clear [bold]x[/bold]")
    r = _run("list", "--project", "Proj")
    assert r.exit_code == 0, (r.output, r.exception)
    r = _run("show", "sec", "--project", "Proj")
    assert r.exit_code == 0, (r.output, r.exception)
    assert "all [/] clear [bold]x[/bold]" in r.output
    # a markup-like bad stem is escaped too
    (pdir / "x[bold]y.yaml").write_text("x: 1\n")
    r = _run("list", "--project", "Proj")
    assert r.exit_code == 0, (r.output, r.exception)

