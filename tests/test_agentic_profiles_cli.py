"""SPEC-0238: `nw profiles list|show|check` over a temp config dir and project root."""
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright.agentic import profiles as P
from nightwright.cli import cli

pytestmark = pytest.mark.fast

SCHEMA = "schema: nw.profile.v1\n"
DEFS = Path(P.__file__).resolve().parent / "profile_defs"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """(cfg, user dir, project root); the project `Proj` is registered and the cwd is elsewhere."""
    cfg = {"config_dir": str(tmp_path / "cfg"), "agentic": {"profiles": {}}}
    user = tmp_path / "cfg" / "profiles"
    proj = tmp_path / "proj"
    user.mkdir(parents=True)
    (proj / ".nw" / "profiles").mkdir(parents=True)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    monkeypatch.setattr("nightwright.console.console._width", 400)   # COLUMNS is read once, at import
    monkeypatch.setattr("nightwright.agentic.project.project_root", lambda c, name: proj.resolve())
    monkeypatch.setattr("nightwright.agentic.project.find_project_for_cwd", lambda c, cwd=None: None)
    return cfg, user, proj


def _run(*args):
    return CliRunner().invoke(cli, ["profiles", *args], env={"COLUMNS": "400"})


def _put(directory, name, text):
    (Path(directory) / f"{name}.yaml").write_text(text, encoding="utf-8")


def test_list_table_and_json_with_a_broken_profile(env):
    cfg, user, proj = env
    _put(user, "broken", SCHEMA + "bogus: 1\n")
    cfg["agentic"]["profiles"]["balanced"] = {"slots": 6}
    r = _run("list")
    assert r.exit_code == 0, r.output                       # broken profiles do not fail a list
    assert "balanced *" in r.output and "frugal" in r.output and "thorough" in r.output
    assert "broken.yaml: bogus: unknown key" in r.output and "config" in r.output
    assert "sonnet/high" in r.output and "opus/high" in r.output
    r = _run("list", "--json")
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["schema"] == "nw.profiles.v1" and d["project"] is None and d["default"] == "balanced"
    by = {p["name"]: p for p in d["profiles"]}
    assert set(by) == {"balanced", "broken", "frugal", "thorough"}
    bal = by["balanced"]
    assert bal["ok"] and bal["error"] is None and bal["scope"] == "config" and bal["slots"] == 6
    assert bal["sources"] == ["builtin:balanced.yaml", "config.yaml:agentic.profiles.balanced"]
    assert bal["reviewers"] == ["conformance_reviewer", "test_reviewer", "adversarial_reviewer"]
    assert by["frugal"]["scope"] == "builtin" and by["frugal"]["loop_max"] == 2
    assert not by["broken"]["ok"] and "bogus: unknown key" in by["broken"]["error"]
    assert by["broken"]["scope"] == "user" and by["broken"]["sources"] == []
    assert set(bal) == {"name", "scope", "sources", "ok", "error", "slots", "loop_max",
                        "campaign_cap_usd", "reviewers", "description"}


def test_show_text_and_json(env):
    cfg, user, proj = env
    cfg["agentic"]["profiles"]["balanced"] = {"slots": 6, "personas": {"implementer": {"model": "opus"}}}
    r = _run("show", "balanced")
    assert r.exit_code == 0, r.output
    rows = {line.split("│")[1].strip(): [c.strip() for c in line.split("│")[2:4]]
            for line in r.output.splitlines() if line.startswith("│") and line.count("│") >= 4}
    assert rows["slots"] == ["6", "config.yaml"]
    assert rows["personas.implementer.model"] == ["opus", "config.yaml"]
    assert rows["personas.implementer.effort"] == ["high", "builtin"]
    assert rows["review.triager"] == ["true", "builtin"]
    assert "sources (lowest first): builtin → config.yaml" in r.output
    r = _run("show", "balanced", "--json")
    d = json.loads(r.output)
    assert d["schema"] == "nw.profile.show.v1" and d["name"] == "balanced" and d["project"] is None
    assert d["profile"]["slots"] == 6 and d["profile"]["name"] == "balanced"
    assert d["provenance"]["slots"] == "config.yaml:agentic.profiles.balanced"
    assert d["provenance"]["loop_max"] == "builtin:balanced.yaml"
    assert d["sources"] == d["profile"]["sources"]
    assert set(d["provenance"]) == set(
        k for k in P.profile_provenance(cfg, "balanced"))


def test_show_unknown_and_invalid_exit_1(env):
    cfg, user, proj = env
    r = _run("show", "nope")
    assert r.exit_code == 1 and "unknown profile 'nope'; known: balanced, frugal, thorough" in r.output
    _put(user, "broken", SCHEMA + "bogus: 1\n")
    r = _run("show", "broken", "--json")
    assert r.exit_code == 1 and "bogus: unknown key" in r.output


def test_check_exit_codes_and_json(env):
    cfg, user, proj = env
    r = _run("check")
    assert r.exit_code == 0, r.output
    assert "✓ balanced" in r.output and "✓ thorough" in r.output
    assert json.loads(_run("check", "--json").output) == {
        "schema": "nw.profiles.check.v1", "ok": True,
        "results": [{"name": n, "ok": True, "error": None}
                    for n in ("balanced", "frugal", "thorough")]}
    _put(user, "broken", SCHEMA + "personas: {implementer: {model: gpt}}\n")
    r = _run("check")
    assert r.exit_code == 1
    assert "✗ broken:" in r.output and "personas.implementer.model" in r.output
    assert "✓ balanced" in r.output
    r = _run("check", "--json")
    d = json.loads(r.output)
    assert r.exit_code == 1 and d["ok"] is False
    bad = [x for x in d["results"] if not x["ok"]]
    assert [x["name"] for x in bad] == ["broken"] and "personas.implementer.model" in bad[0]["error"]


def test_check_resolves_the_default_even_when_unknown(env):
    cfg, user, proj = env
    cfg["agentic"]["default_profile"] = "ghost"
    r = _run("check", "--json")
    d = json.loads(r.output)
    assert r.exit_code == 1 and d["results"][-1]["name"] == "ghost"
    assert "unknown profile 'ghost'" in d["results"][-1]["error"]


def test_project_selected_by_flag(env):
    cfg, user, proj = env
    _put(proj / ".nw" / "profiles", "projonly", (DEFS / "frugal.yaml").read_text())
    _put(proj / ".nw" / "profiles", "balanced", SCHEMA + "slots: 9\n")
    r = _run("list", "--json")                              # no project: no project layer
    assert "projonly" not in {p["name"] for p in json.loads(r.output)["profiles"]}
    r = _run("list", "--project", "Proj", "--json")
    d = json.loads(r.output)
    assert d["project"] == "Proj"
    by = {p["name"]: p for p in d["profiles"]}
    assert by["projonly"]["scope"] == "project" and by["balanced"]["slots"] == 9
    r = _run("show", "balanced", "--project", "Proj")
    assert r.exit_code == 0 and "Proj:.nw/profiles/balanced.yaml" in r.output
    d = json.loads(_run("show", "balanced", "--project", "Proj", "--json").output)
    assert d["provenance"]["slots"] == str(proj / ".nw" / "profiles" / "balanced.yaml")
    assert _run("check", "--project", "Proj").exit_code == 0


def test_project_selected_by_cwd(env, monkeypatch):
    cfg, user, proj = env
    _put(proj / ".nw" / "profiles", "projonly", (DEFS / "frugal.yaml").read_text())
    monkeypatch.setattr("nightwright.agentic.project.find_project_for_cwd", lambda c, cwd=None: "Proj")
    r = _run("list", "--json")
    d = json.loads(r.output)
    assert d["project"] == "Proj" and "projonly" in {p["name"] for p in d["profiles"]}
    assert "project Proj" in _run("list").output


def test_unknown_project_flag_is_an_error(env, monkeypatch):
    def boom(c, name):
        raise ValueError(f"cannot resolve a repo for project {name!r}: nope")
    monkeypatch.setattr("nightwright.agentic.project.project_root", boom)
    r = _run("list", "--project", "Nope")
    assert r.exit_code == 1 and "cannot resolve a repo for project 'Nope'" in r.output
