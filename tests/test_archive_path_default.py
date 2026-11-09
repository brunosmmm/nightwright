"""SPEC-0168: the ideas archive defaults to `ideas-archive.org` next to `org_ideas_file`, so a
config that relocates the ideas file can never archive into someone's real archive.

Goes through the real `load_config()` (tmp HOME / NW_CONFIG_DIR / NW_DATA_DIR) — the bug lived
in the DEFAULT_CONFIG merge, which hand-built cfg dicts never exercise."""
import os
from pathlib import Path

import pytest

from nightwright import explore as EX
from nightwright import org_write as W
from nightwright.config import load_config
from nightwright.org import invalidate_tasks_cache, load_tasks

pytestmark = pytest.mark.fast


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NW_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("NW_DATA_DIR", str(tmp_path / "data"))
    return home, cfg_dir


def test_default_install_path_unchanged(env):
    home, _ = env
    cfg = load_config()
    assert W.ideas_archive_path(cfg) == str(home / "work" / "org" / "ai" / "ideas-archive.org")


def test_relocated_ideas_file_archives_next_to_it(env, tmp_path):
    home, cfg_dir = env
    org = tmp_path / "elsewhere"
    org.mkdir()
    specs = tmp_path / "specs"
    (cfg_dir / "config.yaml").write_text(
        f"org_files: [{org}]\norg_ideas_file: {org}/ideas.org\n"
        f"org_capture_file: {org}/inbox.org\nspecs_dir: {specs}\n")
    cfg = load_config()
    assert W.ideas_archive_path(cfg) == str(org / "ideas-archive.org")

    W.add_idea(cfg, "finished here")
    EX.set_summary(cfg, "IDEA-001", "done")
    EX.set_idea_state(cfg, "IDEA-001", "SHIPPED")      # SPEC-0073 auto-archives
    invalidate_tasks_cache()
    idea = next(t for t in load_tasks(cfg) if t.properties.get("ID") == "IDEA-001")
    assert idea.file == str(org / "ideas-archive.org")
    assert "finished here" in (org / "ideas-archive.org").read_text()
    assert not (home / "work").exists()                # nothing leaked to the old default


def test_explicit_archive_key_wins(env, tmp_path):
    _, cfg_dir = env
    org = tmp_path / "elsewhere"
    arch = tmp_path / "archives" / "mine.org"
    (cfg_dir / "config.yaml").write_text(
        f"org_ideas_file: {org}/ideas.org\norg_ideas_archive_file: {arch}\n")
    assert W.ideas_archive_path(load_config()) == str(arch)

