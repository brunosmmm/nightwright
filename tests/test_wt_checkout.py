"""SPEC-0166: one wt-checkout resolver (config `wt_checkout` > module walk > None), and every
caller fails loudly with the fix hint instead of silently returning nothing.

Installs use a tmp --dest — never the real ~/.claude/skills."""
from pathlib import Path

import pytest
from click.testing import CliRunner

from wt import rules as R
from wt import skills as SK
from wt import specs as S
from wt.cli import cli

ROOT = Path(__file__).resolve().parent.parent


def _fake_checkout(tmp_path, *, skills=("wt-demo",)):
    root = tmp_path / "wt-checkout"
    (root / "src" / "wt").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'wt'\n")
    (root / "docs" / "specs").mkdir(parents=True)
    for name in skills:
        (root / "skills" / name).mkdir(parents=True)
        (root / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
    return root


def _no_walk(monkeypatch):
    """Simulate a non-editable install: the module-relative walk finds nothing."""
    monkeypatch.setattr(R, "discover_wt_checkout", lambda: None)


# ---- resolver ------------------------------------------------------------------------

def test_module_walk_is_the_default():
    assert R.wt_checkout({}) == ROOT


def test_config_override_wins(tmp_path):
    fake = _fake_checkout(tmp_path)
    assert R.wt_checkout({"wt_checkout": str(fake)}) == fake


def test_config_override_expands_user(tmp_path, monkeypatch):
    fake = _fake_checkout(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert R.wt_checkout({"wt_checkout": "~/wt-checkout"}) == fake


def test_invalid_override_raises_not_falls_back(tmp_path):
    with pytest.raises(ValueError, match="not a wt checkout"):
        R.wt_checkout({"wt_checkout": str(tmp_path)})


def test_none_when_unresolvable(monkeypatch):
    _no_walk(monkeypatch)
    assert R.wt_checkout({}) is None
    assert R.wt_checkout(None) is None


def test_no_parents_anchoring_outside_resolver():
    for name in ("skills.py", "specs.py", "rules.py"):
        text = (ROOT / "src" / "wt" / name).read_text()
        assert "parents[2]" not in text, name


# ---- Meta-Tools research --------------------------------------------------------------

def test_meta_tools_research_uses_override(tmp_path, monkeypatch):
    _no_walk(monkeypatch)
    fake = _fake_checkout(tmp_path)
    ctx = R.resolve_research_context({"wt_checkout": str(fake)}, "Meta-Tools")
    assert ctx == {"root": str(fake), "source": "internal"}


def test_meta_tools_note_carries_hint(monkeypatch):
    _no_walk(monkeypatch)
    ctx = R.resolve_research_context({}, "Meta-Tools")
    assert ctx["root"] is None
    assert "--editable" in ctx["note"] and "wt_checkout" in ctx["note"]


# ---- internal specs dir ---------------------------------------------------------------

def test_specs_dir_uses_wt_checkout_when_unset(tmp_path):
    fake = _fake_checkout(tmp_path)
    assert S._specs_dir({"wt_checkout": str(fake)}) == fake / "docs" / "specs"


def test_specs_dir_explicit_beats_wt_checkout(tmp_path):
    fake = _fake_checkout(tmp_path)
    want = tmp_path / "elsewhere"
    assert S._specs_dir({"specs_dir": str(want), "wt_checkout": str(fake)}) == want


def test_specs_dir_error_carries_hint(monkeypatch):
    monkeypatch.setattr(S, "_package_specs_dir", lambda: None)
    with pytest.raises(ValueError, match="wt_checkout"):
        S._specs_dir({})


# ---- skills ---------------------------------------------------------------------------

def test_install_and_status_from_explicit_source(tmp_path):
    fake = _fake_checkout(tmp_path)
    dest = tmp_path / "dest"
    assert SK.install(dest=dest, source=fake / "skills") == [("wt-demo", "linked")]
    assert SK.status(dest=dest, source=fake / "skills")[0]["state"] == "linked"


def test_repo_skills_dir_none_when_unresolvable(monkeypatch):
    _no_walk(monkeypatch)
    assert SK.repo_skills_dir({}) is None
    assert SK.repo_skills() == []


@pytest.mark.parametrize("args", [["skills", "install"], ["skills", "list"]])
def test_cli_skills_fail_loudly_without_checkout(tmp_path, monkeypatch, args):
    _no_walk(monkeypatch)
    monkeypatch.setattr("wt.cli.load_config", lambda: {})
    dest = tmp_path / "dest"
    r = CliRunner().invoke(cli, args + ["--dest", str(dest)])
    assert r.exit_code != 0
    assert "--editable" in r.output and "wt_checkout" in r.output
    assert not dest.exists() or not any(dest.iterdir())


def test_cli_skills_install_via_override_on_non_editable(tmp_path, monkeypatch):
    _no_walk(monkeypatch)
    fake = _fake_checkout(tmp_path)
    monkeypatch.setattr("wt.cli.load_config", lambda: {"wt_checkout": str(fake)})
    dest = tmp_path / "dest"
    r = CliRunner().invoke(cli, ["skills", "install", "--dest", str(dest)])
    assert r.exit_code == 0, r.output
    assert (dest / "wt-demo").resolve() == (fake / "skills" / "wt-demo").resolve()


def test_cli_skills_invalid_override_errors(tmp_path, monkeypatch):
    monkeypatch.setattr("wt.cli.load_config", lambda: {"wt_checkout": str(tmp_path)})
    r = CliRunner().invoke(cli, ["skills", "list", "--dest", str(tmp_path / "d")])
    assert r.exit_code != 0
    assert "not a wt checkout" in r.output
