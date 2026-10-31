"""Filtered idea export + release archive (SPEC-0132/0133)."""
import tarfile
from pathlib import Path
from zoneinfo import ZoneInfo

from click.testing import CliRunner

from nightwright import idea_export as IE
from nightwright import org_write as W
from nightwright import release_archive as RA
from nightwright.cli import cli
from nightwright.org import load_tasks

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path, **overrides):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    (config / "mappings.yaml").write_text("topics: {}\n")
    base = {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "config_dir": str(config),
        "project_axis": "bucket",
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"),
    }
    base.update(overrides)
    return base


def test_export_filters_by_project(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "meta one", project="Meta-Tools")
    W.add_idea(cfg, "example one", project="Example")
    out = tmp_path / "slice.org"
    before = Path(cfg["org_ideas_file"]).read_text()
    ids = IE.export_idea_subtrees(cfg, str(out), project="Meta-Tools", all_done=True)
    after = Path(cfg["org_ideas_file"]).read_text()
    assert before == after
    assert ids == ["IDEA-001"]
    text = out.read_text()
    assert "meta one" in text
    assert "example one" not in text
    assert ":PROJECT: Meta-Tools" in text


def test_export_dry_run_no_write(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "meta", project="Meta-Tools")
    out = tmp_path / "nope.org"
    ids = IE.export_idea_subtrees(cfg, str(out), dry_run=True, project="Meta-Tools",
                                  all_done=True)
    assert ids == ["IDEA-001"]
    assert not out.exists()


def test_export_round_trip_load(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "round trip", project="Meta-Tools")
    out = tmp_path / "slice.org"
    IE.export_idea_subtrees(cfg, str(out), project="Meta-Tools", all_done=True)
    text = out.read_text()
    assert "* IDEA round trip" in text and ":ID: IDEA-001" in text
    # Point org_files at a *directory* that contains only the slice (not the source ideas.org).
    alone = tmp_path / "alone"
    alone.mkdir()
    slice2 = alone / "ideas.org"
    slice2.write_text(text)
    cfg2 = _cfg(alone)
    cfg2["org_files"] = [str(alone)]
    cfg2["org_ideas_file"] = str(slice2)
    tasks = [t for t in load_tasks(cfg2) if t.is_idea]
    assert len(tasks) == 1
    assert tasks[0].heading == "round trip"


def test_cli_ideas_export(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    W.add_idea(cfg, "cli export", project="Meta-Tools")
    out = tmp_path / "e.org"
    r = CliRunner().invoke(cli, ["ideas", "export", "--project", "Meta-Tools", "--all",
                                 "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert out.exists() and "cli export" in out.read_text()


def test_cli_ideas_list_still_works(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    W.add_idea(cfg, "still list")
    r = CliRunner().invoke(cli, ["ideas", "--json"])
    assert r.exit_code == 0, r.output
    assert "still list" in r.output


def test_release_archive_members(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "in slice", project="Meta-Tools")
    W.add_idea(cfg, "out of slice", project="Example")
    out = tmp_path / "rel.tar.gz"
    # Use this repo as git root; export still uses cfg's temp ideas
    path = RA.build_release_archive(cfg, str(out), project="Meta-Tools", all_done=True,
                                    cwd=str(ROOT))
    assert path == str(out)
    names = tarfile.open(out).getnames()
    assert any(n.endswith("ideas/ideas.org") for n in names)
    assert any("/src/nightwright/" in n or n.endswith("src/nightwright") for n in names) or any(
        "src/nightwright" in n for n in names)
    assert not any(".venv" in n for n in names)
    assert not any("example-console" in n for n in names)
    # Read the ideas slice from the tarball
    with tarfile.open(out) as tf:
        member = next(m for m in tf.getmembers() if m.name.endswith("ideas/ideas.org"))
        body = tf.extractfile(member).read().decode()
    assert "in slice" in body
    assert "out of slice" not in body
