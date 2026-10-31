"""`nw add` — capture work items (SPEC-0010): headline building, state validation, options
round-trip via load_tasks, capture-file creation + backup, and CLI."""
import glob
import os
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import org_write as W
from nightwright.cli import cli
from nightwright.org import filter_tasks, load_tasks


def _cfg(tmp_path, capture="inbox.org"):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    return {"org_files": [str(org)], "org_todo_keywords": ["TODO", "|", "DONE"],
            "timezone": "America/New_York", "_tz": ZoneInfo("America/New_York"),
            "data_dir": str(data), "org_capture_file": str(org / capture)}


def _find(cfg, sub):
    return next(t for t in load_tasks(cfg) if sub in t.heading)


def test_add_creates_capture_file_and_task(tmp_path):
    cfg = _cfg(tmp_path)
    assert not os.path.exists(cfg["org_capture_file"])
    path, headline = W.add_task(cfg, "write the docs")
    assert os.path.exists(path)
    assert headline == "* TODO write the docs"          # default state = first active keyword
    t = _find(cfg, "write the docs")
    assert t.state == "TODO" and not t.is_done


def test_add_honors_all_options_and_reparses(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_task(cfg, "fix flaky test", state="TODO", tags=("ci", "flaky"),
               priority="A", scheduled="2026-07-20", deadline="2026-07-25")
    t = _find(cfg, "fix flaky test")
    assert t.priority == "A"
    assert {"ci", "flaky"} <= t.tags
    assert t.scheduled.isoformat() == "2026-07-20"
    assert t.deadline.isoformat() == "2026-07-25"


def test_jira_key_in_text_becomes_topic_key(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_task(cfg, "DEMO-100 wire up the thing")
    keyed = filter_tasks(load_tasks(cfg), has_key=True)
    assert any(t.topic_key == "DEMO-100" for t in keyed)


def test_default_state_from_target_files_keywords(tmp_path):
    cfg = _cfg(tmp_path, capture="agenda.org")
    # a file with its own #+TODO whose first active keyword is not "TODO"
    open(cfg["org_capture_file"], "w").write(
        "#+TODO: TRIAGE INPROGRESS | DONE\n\n* DONE seed\n")
    _, headline = W.add_task(cfg, "new item")
    assert headline == "* TRIAGE new item"


def test_invalid_state_raises_and_no_write(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_task(cfg, "seed")                              # create the file
    before = open(cfg["org_capture_file"]).read()
    with pytest.raises(ValueError, match="invalid state"):
        W.add_task(cfg, "bad", state="BOGUS")
    assert open(cfg["org_capture_file"]).read() == before


def test_append_preserves_existing_and_backs_up(tmp_path):
    cfg = _cfg(tmp_path)
    open(cfg["org_capture_file"], "w").write("#+TITLE: Inbox\n\n* TODO existing\n")
    W.add_task(cfg, "appended")
    lines = open(cfg["org_capture_file"]).read().splitlines()
    assert lines[:3] == ["#+TITLE: Inbox", "", "* TODO existing"]   # prefix untouched
    assert lines[-1] == "* TODO appended"
    backups = glob.glob(os.path.join(cfg["data_dir"], "org-backups", "inbox.org.*.bak"))
    assert len(backups) == 1                             # existing file was backed up


def test_new_file_makes_no_backup(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_task(cfg, "first ever")                        # file did not exist
    backups = glob.glob(os.path.join(cfg["data_dir"], "org-backups", "*.bak"))
    assert backups == []


def test_cli_add(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["add", "DEMO-100", "do", "the", "thing", "--priority", "B"])
    assert r.exit_code == 0, r.output
    assert "DEMO-100" in r.output
    assert _find(cfg, "DEMO-100").priority == "B"
    r2 = CliRunner().invoke(cli, ["add", "x", "--state", "NOPE"])
    assert r2.exit_code != 0 and "invalid state" in r2.output
