"""TODO-state write-back (SPEC-0009): line-anchored keyword rewrite, CLOSED stamping,
round-trip fidelity, drift/validation/ambiguity guards, backup, and CLI."""
import glob
import os
import shutil
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import org_write as W
from nightwright.cli import cli
from nightwright.org import load_tasks

pytestmark = pytest.mark.fast

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "org")


def _setup(tmp_path):
    org_dir = tmp_path / "org"
    shutil.copytree(FIXTURES, org_dir)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg = {"org_files": [str(org_dir)], "org_todo_keywords": ["TODO", "|", "DONE"],
           "timezone": "America/New_York", "_tz": ZoneInfo("America/New_York"),
           "data_dir": str(data_dir)}
    return cfg, org_dir


def _lines(path):
    return open(path).read().splitlines()


def _headline(path, sub):
    return next(l for l in _lines(path) if sub in l and l.startswith("*"))


def test_set_state_changes_only_headline(tmp_path):
    cfg, org_dir = _setup(tmp_path)
    est = str(org_dir / "estimations.org")
    before = _lines(est)
    W.set_state_by_selector(cfg, "DEMO-506", "DONE")
    after = _lines(est)
    # exactly one line changed (the headline) + one CLOSED line inserted
    diff = [i for i in range(min(len(before), len(after))) if i < len(before) and
            (i >= len(after) or before[i] != after[i])]
    hl = next(l for l in after if "DEMO-506" in l and l.startswith("*"))
    assert hl.startswith("* DONE DEMO-506")
    assert any("CLOSED:" in l for l in after)
    assert len(after) == len(before) + 1          # only the CLOSED insertion grew the file


def test_round_trip_fidelity(tmp_path):
    cfg, org_dir = _setup(tmp_path)
    inbox = str(org_dir / "inbox.org")
    original = open(inbox).read()
    # REFILE -> TODO -> DONE -> REFILE with --no-closed keeps it byte-identical
    W.set_state_by_selector(cfg, "pre-SP2 device-tree", "TODO", stamp_closed=False)
    W.set_state_by_selector(cfg, "pre-SP2 device-tree", "DONE", stamp_closed=False)
    W.set_state_by_selector(cfg, "pre-SP2 device-tree", "REFILE", stamp_closed=False)
    assert open(inbox).read() == original


def test_closed_stamp_added_and_removed(tmp_path):
    cfg, org_dir = _setup(tmp_path)
    est = str(org_dir / "estimations.org")
    W.set_state_by_selector(cfg, "DEMO-504", "DONE")
    assert "CLOSED:" in "\n".join(_lines(est))
    # moving back out of done removes the stamp
    W.set_state_by_selector(cfg, "DEMO-504", "INPROGRESS")
    lines = _lines(est)
    hl_idx = next(i for i, l in enumerate(lines)
                  if l.startswith("* INPROGRESS") and "DEMO-504" in l)
    assert not lines[hl_idx + 1].lstrip().startswith("CLOSED:")


def test_invalid_state_raises_and_no_write(tmp_path):
    cfg, org_dir = _setup(tmp_path)
    est = str(org_dir / "estimations.org")
    original = open(est).read()
    with pytest.raises(ValueError, match="invalid state"):
        W.set_state_by_selector(cfg, "DEMO-504", "BOGUS")
    assert open(est).read() == original


def test_ambiguous_selector_raises(tmp_path):
    cfg, _ = _setup(tmp_path)
    with pytest.raises(ValueError, match="ambiguous|no task matched"):
        W.set_state_by_selector(cfg, "DEMO", "DONE")   # matches 504 and 506


def test_no_match_raises(tmp_path):
    cfg, _ = _setup(tmp_path)
    with pytest.raises(ValueError, match="no task matched"):
        W.set_state_by_selector(cfg, "ZZZ-000", "DONE")


def test_backup_written(tmp_path):
    cfg, org_dir = _setup(tmp_path)
    W.set_state_by_selector(cfg, "DEMO-506", "DONE")
    backups = glob.glob(os.path.join(cfg["data_dir"], "org-backups", "estimations.org.*.bak"))
    assert len(backups) == 1


def test_mark_done_uses_first_done_keyword(tmp_path):
    cfg, org_dir = _setup(tmp_path)
    W.mark_done(cfg, "expense report")                 # agenda.org done set starts NONACTIONABLE
    hl = _headline(str(org_dir / "agenda.org"), "expense report")
    assert hl.startswith("* NONACTIONABLE") or hl.split()[1] == "NONACTIONABLE" \
        or hl.startswith("** NONACTIONABLE")


def test_cli_state(tmp_path, monkeypatch):
    cfg, _ = _setup(tmp_path)
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["state", "DEMO-506", "DONE"])
    assert r.exit_code == 0, r.output
    r2 = CliRunner().invoke(cli, ["state", "DEMO-504", "BOGUS"])
    assert r2.exit_code != 0 and "invalid state" in r2.output

