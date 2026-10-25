"""Entity association on capture (SPEC-0018): --project/--epic/--key on `wt idea`/`wt add`,
Task.epic + :PROJECT: override, known_projects/known_epics, permissive validation, promotion
parent-threading, `wt projects` CLI.

All specs scaffolding happens in a tmp specs dir (never docs/specs/) — see cfg["specs_dir"].
Mappings live under a tmp cfg["config_dir"] — never the real ~/.config/wt/mappings.yaml."""
import importlib.util
import pathlib
import shutil
from zoneinfo import ZoneInfo

import yaml
from click.testing import CliRunner

from wt import org_write as W
from wt import rules as RU
from wt import specs as S
from wt.cli import cli
from wt.org import load_tasks

ROOT = pathlib.Path(__file__).resolve().parent.parent
REAL_SPECS = ROOT / "docs" / "specs"
LINT = ROOT / "tools" / "spec_lint.py"


def _linter():
    spec = importlib.util.spec_from_file_location("spec_lint", LINT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cfg(tmp_path, mappings=None):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    for name in ("TEMPLATE.md", "TEMPLATE-epic.md"):
        shutil.copy(REAL_SPECS / name, specs_dir / name)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    if mappings:
        with open(config_dir / "mappings.yaml", "w") as f:
            yaml.safe_dump(mappings, f)
    return {"org_files": [str(org)], "org_todo_keywords": ["TODO", "|", "DONE"],
            "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
            "timezone": "America/New_York", "_tz": ZoneInfo("America/New_York"),
            "data_dir": str(data), "org_capture_file": str(org / "inbox.org"),
            "org_ideas_file": str(org / "ideas.org"), "specs_dir": str(specs_dir),
            "config_dir": str(config_dir), "project_axis": "bucket"}


def _find(cfg, sub):
    return next(t for t in load_tasks(cfg) if sub in t.heading)


def _write_epic(specs_dir, num, spec_id):
    text = (specs_dir / "TEMPLATE-epic.md").read_text()
    import re as _re
    text = _re.sub(r"^id: SPEC-NNNN$", f"id: {spec_id}", text, count=1, flags=_re.M)
    text = _re.sub(r"^title: .*$", "title: An epic", text, count=1, flags=_re.M)
    text = _re.sub(r"^status: draft$", "status: accepted", text, count=1, flags=_re.M)
    text = _re.sub(r"^created: YYYY-MM-DD$", "created: 2026-07-01", text, count=1, flags=_re.M)
    (specs_dir / f"{num}-an-epic.md").write_text(text)


# ---- rules.known_projects ----------------------------------------------------

def test_known_projects_empty_without_mappings(tmp_path):
    cfg = _cfg(tmp_path)
    assert RU.known_projects(cfg) == []


def test_known_projects_sorted_distinct_bucket_values(tmp_path):
    cfg = _cfg(tmp_path, mappings={
        "repoA": "Logging", "repoB": {"bucket": "Demo-ATS", "area": "x"},
        "repoC": "Logging",
    })
    assert RU.known_projects(cfg) == ["Demo-ATS", "Logging"]


# ---- specs.known_epics -------------------------------------------------------

def test_known_epics_empty_without_epic_specs(tmp_path):
    cfg = _cfg(tmp_path)
    assert S.known_epics(cfg) == []


def test_known_epics_lists_kind_epic_specs(tmp_path):
    cfg = _cfg(tmp_path)
    _write_epic(pathlib.Path(cfg["specs_dir"]), "0011", "SPEC-0011")
    assert S.known_epics(cfg) == ["SPEC-0011"]


# ---- add_task/add_idea association capture + re-parse -----------------------

def test_add_idea_captures_project_epic_key_and_reparses(tmp_path):
    cfg = _cfg(tmp_path, mappings={"repoA": "Logging"})
    _write_epic(pathlib.Path(cfg["specs_dir"]), "0011", "SPEC-0011")
    W.add_idea(cfg, "adaptive logging", project="Logging", epic="SPEC-0011", key="DEMO-100")
    t = _find(cfg, "adaptive logging")
    assert t.project == "Logging"
    assert t.epic == "SPEC-0011"
    assert t.topic_key == "DEMO-100"


def test_add_task_project_overrides_structural_project(tmp_path):
    cfg = _cfg(tmp_path, mappings={"repoA": "Logging"})
    W.add_task(cfg, "wire the dashboard", project="Demo-ATS")
    t = _find(cfg, "wire the dashboard")
    assert t.project == "Demo-ATS"


def test_add_task_without_associations_is_unaffected(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_task(cfg, "plain task")
    t = _find(cfg, "plain task")
    assert t.epic is None
    assert t.properties.get("PROJECT") is None


# ---- permissive validation ----------------------------------------------------

def test_unknown_project_warns_but_still_captures(tmp_path, capsys):
    cfg = _cfg(tmp_path, mappings={"repoA": "Logging"})
    W.add_task(cfg, "wire it up", project="Nonexistent")
    err = capsys.readouterr().err
    assert "unknown project" in err
    assert "Logging" in err
    t = _find(cfg, "wire it up")
    assert t.project == "Nonexistent"


def test_unknown_epic_warns_but_still_captures(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _write_epic(pathlib.Path(cfg["specs_dir"]), "0011", "SPEC-0011")
    W.add_idea(cfg, "some idea", epic="SPEC-9999")
    err = capsys.readouterr().err
    assert "unknown epic" in err
    assert "SPEC-0011" in err
    t = _find(cfg, "some idea")
    assert t.epic == "SPEC-9999"


# ---- promotion parent-threading ----------------------------------------------

def test_scaffold_from_idea_valid_epic_sets_parent(tmp_path):
    cfg = _cfg(tmp_path)
    _write_epic(pathlib.Path(cfg["specs_dir"]), "0011", "SPEC-0011")
    W.add_idea(cfg, "adaptive logging", epic="SPEC-0011")
    path, spec_id = S.scaffold_from_idea(cfg, "adaptive logging")
    text = path.read_text()
    assert "parent: SPEC-0011" in text

    mod = _linter()
    fm, body = mod.split_frontmatter(text)
    epic_fm, epic_body = mod.split_frontmatter(
        (pathlib.Path(cfg["specs_dir"]) / "0011-an-epic.md").read_text())
    specs = [
        {"path": path, "file": path.name, "num": spec_id.split("-")[1], "fm": fm,
         "raw_fm_ok": True, "sections": mod.sections(body), "id": fm.get("id", ""),
         "kind": fm.get("kind", "feature"), "status": fm.get("status", "")},
        {"path": pathlib.Path(cfg["specs_dir"]) / "0011-an-epic.md", "file": "0011-an-epic.md",
         "num": "0011", "fm": epic_fm, "raw_fm_ok": True, "sections": mod.sections(epic_body),
         "id": epic_fm.get("id", ""), "kind": epic_fm.get("kind", "feature"),
         "status": epic_fm.get("status", "")},
    ]
    problems = [p for p in mod.validate(specs) if not p.startswith("LEDGER.md")]
    assert problems == [], problems


def test_scaffold_from_idea_unresolvable_epic_warns_and_omits_parent(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "adaptive logging", epic="SPEC-9999")
    path, spec_id = S.scaffold_from_idea(cfg, "adaptive logging")
    text = path.read_text()
    assert "parent:" not in text
    err = capsys.readouterr().err
    assert "doesn't resolve" in err

    mod = _linter()
    fm, body = mod.split_frontmatter(text)
    specs = [{"path": path, "file": path.name, "num": spec_id.split("-")[1], "fm": fm,
             "raw_fm_ok": True, "sections": mod.sections(body), "id": fm.get("id", ""),
             "kind": fm.get("kind", "feature"), "status": fm.get("status", "")}]
    problems = [p for p in mod.validate(specs) if not p.startswith("LEDGER.md")]
    assert problems == [], problems


# ---- CLI ----------------------------------------------------------------------

def test_cli_idea_with_associations(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, mappings={"repoA": "Logging"})
    _write_epic(pathlib.Path(cfg["specs_dir"]), "0011", "SPEC-0011")
    import wt.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["idea", "adaptive logging", "--project", "Logging",
                                "--epic", "SPEC-0011", "--key", "DEMO-100"])
    assert r.exit_code == 0, r.output
    t = _find(cfg, "adaptive logging")
    assert t.project == "Logging" and t.epic == "SPEC-0011" and t.topic_key == "DEMO-100"


def test_cli_add_with_unknown_project_still_succeeds(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, mappings={"repoA": "Logging"})
    import wt.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["add", "wire the dashboard", "--project", "Demo-ATS"])
    assert r.exit_code == 0, r.output
    t = _find(cfg, "wire the dashboard")
    assert t.project == "Demo-ATS"


def test_cli_projects_lists_known_buckets(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, mappings={"repoA": "Logging", "repoB": "Demo-ATS"})
    import wt.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["projects"])
    assert r.exit_code == 0, r.output
    assert "Demo-ATS" in r.output
    assert "Logging" in r.output


def test_cli_projects_empty(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    import wt.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["projects"])
    assert r.exit_code == 0, r.output
    assert "no projects mapped" in r.output
    assert "Meta-Tools" in r.output          # SPEC-0189: the internal project is always listed


# ---- digest --by project groups associated captures --------------------------

def test_digest_by_project_groups_captured_project(tmp_path, monkeypatch):
    from wt import report as R
    from wt.console import console
    cfg = _cfg(tmp_path, mappings={"repoA": "Logging"})
    W.add_task(cfg, "DEMO-100 wire it up", project="Demo-ATS")
    monkeypatch.setattr(R, "_scope_topic_secs", lambda c, days: {"DEMO-100": 3600.0})
    prev = console._width
    console._width = 240
    try:
        with console.capture() as cap:
            R.digest(cfg, day="2026-07-18", by="project")
    finally:
        console._width = prev
    assert "Demo-ATS" in cap.get()
