"""SPEC-0371: `nw personas list|show|validate` (declared personas) over a temp config dir and project root."""
import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from nightwright import completion as C
from nightwright.cli import cli

TEMPLATE = "Review {{spec_id}}:\n{{diff}}\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """(cfg, user personas dir, project personas dir); project `Proj` is registered."""
    cfg = {"config_dir": str(tmp_path / "cfg"), "agentic": {"profiles": {}}}
    user = tmp_path / "cfg" / "personas"
    proj = tmp_path / "proj"
    pdir = proj / ".nw" / "personas"
    user.mkdir(parents=True)
    pdir.mkdir(parents=True)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    monkeypatch.setattr("nightwright.console.console._width", 400)
    monkeypatch.setattr("nightwright.agentic.project.project_root", lambda c, name: proj.resolve())
    monkeypatch.setattr("nightwright.agentic.project.find_project_for_cwd",
                        lambda c, cwd=None: None)
    return cfg, user, pdir


def _run(*args):
    return CliRunner().invoke(cli, ["personas", *args], env={"COLUMNS": "400"})


def _put(directory, name, role="reviewer", template=TEMPLATE, extra=""):
    (directory / f"{name}.md").write_text(template, encoding="utf-8")
    (directory / f"{name}.yaml").write_text(
        f"schema: nw.persona.v1\nname: {name}\nrole: {role}\ntemplate: {name}.md\n{extra}",
        encoding="utf-8")


def test_validate_clean_exits_0_with_ok(env):
    cfg, user, pdir = env
    _put(pdir, "security_reviewer", extra="category: [crypto]\n")
    r = _run("validate", "--project", "Proj")
    assert r.exit_code == 0, r.output
    assert "ok" in r.output


def test_validate_broken_lists_problems_and_exits_1(env):
    cfg, user, pdir = env
    _put(pdir, "security_reviewer", template="{{idea_text}}\n")
    _put(user, "conformance_reviewer")
    r = _run("validate", "--project", "Proj")
    assert r.exit_code == 1, r.output
    assert "placeholder {{idea_text}} is out of role" in r.output
    assert "is a builtin persona; pick another name" in r.output


def test_validate_json(env):
    cfg, user, pdir = env
    _put(pdir, "good_reviewer")
    _put(pdir, "bad_reviewer", extra="read_only: false\n")
    r = _run("validate", "--project", "Proj", "--json")
    assert r.exit_code == 1
    d = json.loads(r.output)
    assert d["ok"] is False and len(d["problems"]) == 1
    assert "read_only: unknown key" in d["problems"][0]
    by = {p["name"]: p for p in d["personas"]}
    assert by["good_reviewer"]["ok"] and not by["bad_reviewer"]["ok"]
    assert by["good_reviewer"]["layer"] == "project" and by["good_reviewer"]["role"] == "reviewer"
    (pdir / "bad_reviewer.yaml").unlink()
    r = _run("validate", "--project", "Proj", "--json")
    assert r.exit_code == 0 and json.loads(r.output)["ok"] is True


def test_validate_without_project_checks_only_the_user_layer(env):
    cfg, user, pdir = env
    _put(pdir, "bad_reviewer", extra="read_only: false\n")
    assert _run("validate").exit_code == 0               # no project in scope: project layer unseen
    _put(user, "bad_user", extra="read_only: false\n")
    assert _run("validate").exit_code == 1


def test_list_and_show_render_layer_role_category_template_and_invalid(env):
    cfg, user, pdir = env
    _put(pdir, "security_reviewer", extra="category: [crypto, secrets]\n")
    _put(user, "my_impl", role="implementer", template="{{spec_text}}\n")
    _put(pdir, "broken_one", extra="bogus: 1\n")
    r = _run("list", "--project", "Proj")
    assert r.exit_code == 0, r.output                   # broken declarations do not fail a list
    out = r.output
    assert "conformance_reviewer" in out and "builtin" in out
    assert "security_reviewer" in out and "project" in out and "crypto, secrets" in out
    assert "my_impl" in out and "user" in out
    assert "broken_one" in out and "invalid" in out and "bogus: unknown key" in out
    r = _run("list", "--project", "Proj", "--json")
    by = {p["name"]: p for p in json.loads(r.output)["personas"]}
    assert by["security_reviewer"]["layer"] == "project" and by["security_reviewer"]["ok"]
    assert by["security_reviewer"]["read_only"] is True
    assert by["my_impl"]["read_only"] is False and by["my_impl"]["layer"] == "user"
    assert not by["broken_one"]["ok"]
    r = _run("show", "security_reviewer", "--project", "Proj")
    assert r.exit_code == 0, r.output
    for needle in ("project", "reviewer", "crypto, secrets", "security_reviewer.md", "ok"):
        assert needle in r.output
    r = _run("show", "broken_one", "--project", "Proj")
    assert "invalid" in r.output and "bogus: unknown key" in r.output
    assert _run("show", "implementer", "--project", "Proj").exit_code == 0
    assert _run("show", "nope", "--project", "Proj").exit_code != 0


def test_completion_offers_declared_names(env):
    cfg, user, pdir = env
    _put(pdir, "security_reviewer")
    ctx = click.Context(click.Command("show"), obj=cfg)
    ctx.params["project"] = "Proj"
    names = [i.value for i in C.complete_persona(ctx, None, "sec")]
    assert names == ["security_reviewer"]
    assert "conformance_reviewer" in [i.value for i in C.complete_persona(ctx, None, "conf")]


# ---- SPEC-0394: run settings, routing metadata and template sha256 ---------------------------

DECLARED = ("category: [key_handling, timing]\nmodel: opus\neffort: high\nmax_budget_usd: 2.0\n"
            "required: false\nwhen: {paths: ['src/crypto/**'], labels: [security]}\n")
DECLARED_FIELDS = ("settings", "required", "when", "template_sha256")


def test_show_and_list_json_carry_declared_fields_and_omit_them_for_builtins(env):
    import hashlib
    cfg, user, pdir = env
    _put(pdir, "security_reviewer", extra=DECLARED)
    _put(user, "my_impl", role="implementer", template="{{spec_text}}\n", extra="effort: low\n")
    sha = hashlib.sha256(TEMPLATE.encode()).hexdigest()
    r = _run("show", "security_reviewer", "--project", "Proj", "--json")
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["settings"] == {"model": "opus", "effort": "high", "max_budget_usd": 2.0}
    assert d["required"] is False
    assert d["when"] == {"paths": ["src/crypto/**"], "labels": ["security"]}
    assert d["template_sha256"] == sha
    r = _run("list", "--project", "Proj", "--json")
    by = {p["name"]: p for p in json.loads(r.output)["personas"]}
    assert {k: by["security_reviewer"][k] for k in DECLARED_FIELDS} == \
        {k: d[k] for k in DECLARED_FIELDS}
    assert by["my_impl"]["settings"] == {"effort": "low"} and "template_sha256" in by["my_impl"]
    assert "required" not in by["my_impl"] and "when" not in by["my_impl"]
    for name in ("conformance_reviewer", "implementer"):
        assert not set(DECLARED_FIELDS) & set(by[name]), name
        shown = json.loads(_run("show", name, "--project", "Proj", "--json").output)
        assert not set(DECLARED_FIELDS) & set(shown), name
    r = _run("show", "security_reviewer", "--project", "Proj")
    for needle in ("settings: model=opus, effort=high, max_budget_usd=2.0", "required: false",
                   "when: paths: src/crypto/**; labels: security", f"template_sha256: {sha}"):
        assert needle in r.output, r.output
    r = _run("show", "conformance_reviewer", "--project", "Proj")
    assert "template_sha256" not in r.output and "settings:" not in r.output


def test_validate_and_list_agree_on_new_key_problems(env):
    cfg, user, pdir = env
    _put(pdir, "bad_reviewer", extra="effort: extreme\nwhen: {files: x}\n")
    r = _run("validate", "--project", "Proj", "--json")
    assert r.exit_code == 1
    probs = json.loads(r.output)["problems"]
    assert [p.split(": ", 1)[1] for p in probs] == [
        "effort: must be one of low, medium, high, xhigh, max, got 'extreme'",
        "when.files: unknown key; allowed: labels, paths"]
    by = {p["name"]: p for p in json.loads(_run("list", "--project", "Proj", "--json").output)[
        "personas"]}
    assert not by["bad_reviewer"]["ok"]
    assert by["bad_reviewer"]["error"].split(": ", 1)[1] == probs[0].split(": ", 1)[1]
    shown = json.loads(_run("show", "bad_reviewer", "--project", "Proj", "--json").output)
    assert [p.split(": ", 1)[1] for p in shown["problems"]] == \
        [p.split(": ", 1)[1] for p in probs]
    assert "settings" not in shown                         # a broken entry carries no declaration
