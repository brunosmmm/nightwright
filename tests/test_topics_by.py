"""nw topics --by facet axis (SPEC-0028)."""
from pathlib import Path
from zoneinfo import ZoneInfo

from click.testing import CliRunner

from nightwright.cli import cli
import pytest

pytestmark = pytest.mark.fast


def _cfg(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    return {
        "config_dir": str(config),
        "data_dir": str(data),
        "work_root": str(work),
        "_work": str(work),
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "org_files": [],
        "project_axis": "bucket",
    }


def _patch(monkeypatch, cfg, day_topic, history=None):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    monkeypatch.setattr("nightwright.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "nightwright.report.build",
        lambda *a, **k: (day_topic, {}, {}, {}),
    )
    monkeypatch.setattr("nightwright.report.read_history", lambda _cfg: history or [])
    # Skip RepoResolver entirely — topics only needs build's day_topic
    monkeypatch.setattr("nightwright.report.RepoResolver", lambda _cfg: object())


def test_topics_by_merges_inherited_hours(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    Path(cfg["config_dir"], "mappings.yaml").write_text(
        "example-ats:\n  bucket: Example\n", encoding="utf-8"
    )
    _patch(
        monkeypatch,
        cfg,
        {"2026-07-01": {
            "example-ats": 3600.0,
            "example-ats:device-health": 1800.0,
            "scratch": 900.0,
            "MEETINGS": 100.0,
        }},
    )
    r = CliRunner().invoke(cli, ["topics", "--by", "bucket"])
    assert r.exit_code == 0, r.output
    assert "by bucket" in r.output
    assert "Example" in r.output
    assert "1.50" in r.output  # (3600+1800)/3600
    assert "0.25" in r.output  # scratch
    assert "example-ats:device-health" not in r.output
    # no facets column header in --by mode
    assert "facets" not in r.output.lower()


def test_topics_by_and_unmapped_errors(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, cfg, {})
    r = CliRunner().invoke(cli, ["topics", "--by", "bucket", "--unmapped"])
    assert r.exit_code != 0
    assert "--by and --unmapped" in r.output


def test_topics_without_by_keeps_facets_column(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    Path(cfg["config_dir"], "mappings.yaml").write_text(
        "example-ats:\n  bucket: Example\n", encoding="utf-8"
    )
    _patch(monkeypatch, cfg, {"2026-07-01": {"example-ats": 3600.0}})
    r = CliRunner().invoke(cli, ["topics"])
    assert r.exit_code == 0, r.output
    assert "base topic" in r.output.lower() or "example-ats" in r.output
    assert "bucket" in r.output  # facet shown
    assert "by bucket" not in r.output


def test_topics_help_documents_by():
    r = CliRunner().invoke(cli, ["topics", "--help"])
    assert r.exit_code == 0
    assert "--by" in r.output
