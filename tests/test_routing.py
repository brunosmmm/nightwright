"""Project-aware promotion routing (SPEC-0020): promote_idea dispatches --from-idea to an
internal SPEC-NNNN or an outbound <PROJ>-NNNN based on the idea's :PROJECT: association (or
--target/--internal overrides), and errors (without writing anything or mutating the idea) when
the project has no configured outbound target.

All scaffolding happens in tmp specs/outbox dirs (never docs/specs/ or docs/outbox/)."""
import pathlib
import shutil

import pytest
from click.testing import CliRunner
from zoneinfo import ZoneInfo

from nightwright import org_write as W
from nightwright import specs as S
from nightwright.cli import cli
from nightwright.org import load_tasks

pytestmark = pytest.mark.fast

ROOT = pathlib.Path(__file__).resolve().parent.parent
REAL_SPECS = ROOT / "docs" / "specs"


def _cfg(tmp_path, outbox_targets=None):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    for name in ("TEMPLATE.md", "TEMPLATE-epic.md"):
        shutil.copy(REAL_SPECS / name, specs_dir / name)
    outbox = tmp_path / "outbox"
    return {"org_files": [str(org)], "org_todo_keywords": ["TODO", "|", "DONE"],
            "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
            "timezone": "America/New_York", "_tz": ZoneInfo("America/New_York"),
            "data_dir": str(data), "org_capture_file": str(org / "inbox.org"),
            "org_ideas_file": str(org / "ideas.org"), "specs_dir": str(specs_dir),
            "outbox_dir": str(outbox), "outbox_targets": outbox_targets or {},
            "config_dir": str(data)}  # for load_mappings (known_projects) permissive check


def _seed_idea(cfg, heading="instant review agent", project=None):
    W.add_idea(cfg, heading, project=project)
    return _find(cfg, heading)


def _find(cfg, sub):
    return next(t for t in load_tasks(cfg) if sub in t.heading)


# ---- promote_idea: outbound route ----------------------------------------------------

def test_promote_idea_with_configured_project_goes_outbound(tmp_path):
    cfg = _cfg(tmp_path, outbox_targets={"AI-workstreams": {"repo_path": "/tmp/ai-repo"}})
    _seed_idea(cfg, "adversarial reviewer", project="AI-workstreams")

    path, spec_id, kind = S.promote_idea(cfg, "adversarial reviewer")

    assert kind == "outbound"
    assert spec_id.startswith("AIWORKSTREAMS-")
    assert path.exists()
    assert (tmp_path / "outbox").exists()
    assert not list((tmp_path / "specs").glob("[0-9]*.md"))  # nothing landed internally

    idea = _find(cfg, "adversarial reviewer")
    assert idea.state == "SPECCED"
    assert idea.properties.get("SPEC") == spec_id


def test_promote_idea_target_override_goes_outbound(tmp_path):
    cfg = _cfg(tmp_path, outbox_targets={"acme": {"repo_path": "/tmp/acme-repo"}})
    _seed_idea(cfg, "adversarial reviewer")  # no :PROJECT:

    path, spec_id, kind = S.promote_idea(cfg, "adversarial reviewer", target="acme")

    assert kind == "outbound"
    assert spec_id.startswith("ACME-")


# ---- promote_idea: internal route ----------------------------------------------------

def test_promote_idea_with_no_project_goes_internal(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_idea(cfg, "instant review agent")

    path, spec_id, kind = S.promote_idea(cfg, "instant review agent")

    assert kind == "internal"
    assert spec_id == "SPEC-0001"
    assert path.parent == tmp_path / "specs"

    idea = _find(cfg, "instant review agent")
    assert idea.state == "SPECCED"
    assert idea.properties.get("SPEC") == "SPEC-0001"


def test_promote_idea_internal_flag_overrides_configured_project(tmp_path):
    cfg = _cfg(tmp_path, outbox_targets={"AI-workstreams": {"repo_path": "/tmp/ai-repo"}})
    _seed_idea(cfg, "adversarial reviewer", project="AI-workstreams")

    path, spec_id, kind = S.promote_idea(cfg, "adversarial reviewer", internal=True)

    assert kind == "internal"
    assert spec_id.startswith("SPEC-")


# ---- promote_idea: error route (unconfigured project) --------------------------------

def test_promote_idea_unconfigured_project_errors_without_mutation(tmp_path):
    cfg = _cfg(tmp_path)  # no outbox_targets at all
    _seed_idea(cfg, "adversarial reviewer", project="AI-workstreams")

    with pytest.raises(ValueError, match="AI-workstreams"):
        S.promote_idea(cfg, "adversarial reviewer")

    # nothing written anywhere
    assert not list((tmp_path / "specs").glob("[0-9]*.md"))
    assert not (tmp_path / "outbox").exists()

    # idea left untouched
    idea = _find(cfg, "adversarial reviewer")
    assert idea.state == "IDEA"
    assert idea.properties.get("SPEC") is None


# ---- promote_idea: already-promoted guard ---------------------------------------------

def test_promote_idea_already_promoted_requires_force(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_idea(cfg, "instant review agent")
    S.promote_idea(cfg, "instant review agent")

    with pytest.raises(ValueError, match="already promoted"):
        S.promote_idea(cfg, "instant review agent")

    path, spec_id, kind = S.promote_idea(cfg, "instant review agent", force=True)
    assert kind == "internal"


# ---- CLI --------------------------------------------------------------------------

def test_cli_spec_new_from_idea_routes_outbound(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, outbox_targets={"AI-workstreams": {"repo_path": "/tmp/ai-repo"}})
    _seed_idea(cfg, "adversarial reviewer", project="AI-workstreams")
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)

    r = CliRunner().invoke(cli, ["spec", "new", "--from-idea", "adversarial reviewer"])
    assert r.exit_code == 0, r.output
    assert "AIWORKSTREAMS-" in r.output
    assert "outbound" in r.output
    assert "/tmp/ai-repo" in r.output


def test_cli_spec_new_from_idea_routes_internal(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed_idea(cfg, "instant review agent")
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)

    r = CliRunner().invoke(cli, ["spec", "new", "--from-idea", "instant review agent"])
    assert r.exit_code == 0, r.output
    assert "SPEC-0001" in r.output
    assert "internal" in r.output


def test_cli_spec_new_internal_flag(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, outbox_targets={"AI-workstreams": {"repo_path": "/tmp/ai-repo"}})
    _seed_idea(cfg, "adversarial reviewer", project="AI-workstreams")
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)

    r = CliRunner().invoke(cli, ["spec", "new", "--from-idea", "adversarial reviewer",
                                "--internal"])
    assert r.exit_code == 0, r.output
    assert "SPEC-" in r.output
    assert "internal" in r.output


def test_cli_spec_new_unconfigured_project_errors(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)  # no outbox_targets
    _seed_idea(cfg, "adversarial reviewer", project="AI-workstreams")
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)

    r = CliRunner().invoke(cli, ["spec", "new", "--from-idea", "adversarial reviewer"])
    assert r.exit_code != 0
    assert "AI-workstreams" in r.output

    assert not list((tmp_path / "specs").glob("[0-9]*.md"))
    idea = _find(cfg, "adversarial reviewer")
    assert idea.state == "IDEA"

