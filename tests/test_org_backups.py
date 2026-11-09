"""Org backup retention (SPEC-0271): per-basename keep-last + one-per-day selection, prune on
every backup write (that file only, best-effort), `nw backups prune` dry run / --yes, and the
`org_backups` config fallback."""
import datetime as dt
import json
import os
import re
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import org_write as W
from nightwright.cli import cli
from nightwright.config import load_config
from nightwright.org import load_tasks

pytestmark = pytest.mark.fast


def _name(base, when):
    return f"{base}.{when.strftime('%Y%m%dT%H%M%S')}.bak"


def test_select_keeps_last_n_and_one_per_day():
    today = dt.date(2026, 10, 27)
    names = []
    for base in ("ideas.org", "other.org"):
        for back in range(20):                       # 20 days, 3 backups a day
            day = dt.datetime.combine(today - dt.timedelta(days=back), dt.time(9))
            names += [_name(base, day + dt.timedelta(hours=h)) for h in (0, 1, 2)]
    prunes = W.select_backup_prunes(names, keep_last=3, keep_days=5, today=today)
    kept = set(names) - set(prunes)
    expected = set()
    for base in ("ideas.org", "other.org"):
        # newest 3 = all of today's; plus the newest (11:00) of each of the 4 days before
        expected |= {_name(base, dt.datetime.combine(today, dt.time(9 + h))) for h in (0, 1, 2)}
        expected |= {_name(base, dt.datetime.combine(today - dt.timedelta(days=b), dt.time(11)))
                     for b in range(1, 5)}
    assert kept == expected
    assert len(prunes) == len(names) - 14


def test_select_ignores_foreign_files():
    today = dt.date(2026, 10, 27)
    names = ["notes.txt", "ideas.org.bak", "ideas.org.2026T1.bak",
             _name("ideas.org", dt.datetime(2026, 1, 1)), _name("ideas.org", dt.datetime(2026, 1, 2))]
    prunes = W.select_backup_prunes(names, keep_last=0, keep_days=0, today=today)
    assert sorted(prunes) == sorted(names[3:])


def test_select_skips_unparseable_timestamp():
    # matches BACKUP_RE but strptime rejects it: skipped, never selected, never raises
    names = ["ideas.org.20261399T999999.bak", _name("ideas.org", dt.datetime(2026, 1, 1))]
    assert W.select_backup_prunes(names, keep_last=0, keep_days=0,
                                  today=dt.date(2026, 10, 27)) == names[1:]


def test_select_huge_keep_days_keeps_one_per_day():
    today = dt.date(2026, 10, 27)
    names = [_name("ideas.org", dt.datetime(2000, 1, 1, h)) for h in (1, 2)]
    names.append(_name("ideas.org", dt.datetime(2026, 10, 27, 3)))
    prunes = W.select_backup_prunes(names, keep_last=0, keep_days=10**9, today=today)
    assert prunes == [names[0]]                      # no OverflowError; older same-day copy goes


def _ideas_setup(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    ideas = org / "ideas.org"
    ideas.write_text("#+TODO: TODO | DONE\n* TODO a thing\n")
    data_dir = tmp_path / "data"
    (data_dir / "org-backups").mkdir(parents=True)
    cfg = {"org_files": [str(org)], "org_todo_keywords": ["TODO", "|", "DONE"],
           "timezone": "America/New_York", "_tz": ZoneInfo("America/New_York"),
           "data_dir": str(data_dir)}
    return cfg, ideas, data_dir / "org-backups"


def _seed(bdir, base, n, day=None, body="old\n"):
    start = dt.datetime.combine(day or dt.date.today(), dt.time(0))
    for i in range(n):
        (bdir / _name(base, start + dt.timedelta(seconds=i))).write_text(body)


def _task(cfg):
    return next(t for t in load_tasks(cfg) if "a thing" in t.heading)


def test_write_prunes_that_files_backups(tmp_path):
    cfg, ideas, bdir = _ideas_setup(tmp_path)
    _seed(bdir, "ideas.org", 60)
    _seed(bdir, "other.org", 60)
    W.set_property(cfg, _task(cfg), "FOO", "bar")
    assert len(list(bdir.glob("ideas.org.*.bak"))) == 50     # same-day seeds: exactly keep_last
    assert len(list(bdir.glob("other.org.*.bak"))) == 60     # another file's backups untouched
    assert ":FOO: bar" in ideas.read_text()


def test_prune_failure_does_not_fail_write(tmp_path, monkeypatch):
    cfg, ideas, bdir = _ideas_setup(tmp_path)
    _seed(bdir, "ideas.org", 60)

    def boom(path):
        raise OSError("nope")
    monkeypatch.setattr(W.os, "remove", boom)
    W.set_property(cfg, _task(cfg), "FOO", "bar")
    assert ":FOO: bar" in ideas.read_text()
    assert len(list(bdir.glob("ideas.org.*.bak"))) >= 60      # nothing deleted
    assert not list(ideas.parent.glob(".nw-org-*"))          # no temp file left: atomic replace


def test_prune_listdir_failure_does_not_fail_write(tmp_path, monkeypatch):
    cfg, ideas, _ = _ideas_setup(tmp_path)

    def boom(path):
        raise OSError("nope")
    monkeypatch.setattr(W.os, "listdir", boom)
    W.set_property(cfg, _task(cfg), "FOO", "bar")
    assert ":FOO: bar" in ideas.read_text()


def test_huge_keep_days_does_not_fail_write(tmp_path):
    cfg, ideas, bdir = _ideas_setup(tmp_path)
    cfg["org_backups"] = {"keep_last": 2, "keep_days": 1000000}
    _seed(bdir, "ideas.org", 5, day=dt.date.today() - dt.timedelta(days=100))
    W.set_property(cfg, _task(cfg), "FOO", "bar")
    assert ":FOO: bar" in ideas.read_text()
    # the prune still ran: newest 2 (today's backup + newest seed); older seeds share a day
    assert len(list(bdir.glob("ideas.org.*.bak"))) == 2


def test_non_oserror_prune_does_not_fail_write(tmp_path, monkeypatch):
    cfg, ideas, _ = _ideas_setup(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("bug in retention")
    monkeypatch.setattr(W, "select_backup_prunes", boom)
    W.set_property(cfg, _task(cfg), "FOO", "bar")
    assert ":FOO: bar" in ideas.read_text()


def _cli(monkeypatch, data_dir, config_dir):
    """Run `nw` against a real load_config() of tmp dirs (patched in directly so a leaked
    `nightwright.cli.load_config` patch from another test can't redirect it)."""
    monkeypatch.setenv("NW_DATA_DIR", str(data_dir))
    monkeypatch.setenv("NW_CONFIG_DIR", str(config_dir))
    cfg = load_config()
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    return lambda args: CliRunner().invoke(cli, args)


def test_cli_prune_dry_run_then_yes(tmp_path, monkeypatch):
    data_dir, config_dir = tmp_path / "data", tmp_path / "config"
    bdir = data_dir / "org-backups"
    bdir.mkdir(parents=True)
    config_dir.mkdir()
    old = dt.date.today() - dt.timedelta(days=100)
    _seed(bdir, "ideas.org", 70, day=old)
    _seed(bdir, "ideas-archive.org", 3, day=old)
    (bdir / "notes.txt").write_text("keep me\n")
    run = _cli(monkeypatch, data_dir, config_dir)
    before = sorted(os.listdir(bdir))
    r = run(["backups", "prune"])
    assert r.exit_code == 0, r.output
    assert "would delete 20 of 73 backups" in r.output
    assert "kept 53" in r.output
    assert sorted(os.listdir(bdir)) == before
    r = run(["backups", "prune", "--json"])
    assert r.exit_code == 0, r.output
    assert '"bytes": 80' in r.output                          # 20 files x 4 bytes
    r = run(["backups", "prune", "--yes", "--keep-last", "1", "--keep-days", "0"])
    assert r.exit_code == 0, r.output
    assert "deleted 71 of 73 backups" in r.output
    start = dt.datetime.combine(old, dt.time(0))
    assert sorted(os.listdir(bdir)) == sorted([           # the newest per basename + foreign file
        _name("ideas.org", start + dt.timedelta(seconds=69)),
        _name("ideas-archive.org", start + dt.timedelta(seconds=2)), "notes.txt"])


def test_cli_prune_honours_config(tmp_path, monkeypatch):
    data_dir, config_dir = tmp_path / "data", tmp_path / "config"
    bdir = data_dir / "org-backups"
    bdir.mkdir(parents=True)
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("org_backups:\n  keep_last: 5\n")
    _seed(bdir, "ideas.org", 8, day=dt.date.today() - dt.timedelta(days=100))
    r = _cli(monkeypatch, data_dir, config_dir)(["backups", "prune"])
    assert r.exit_code == 0, r.output
    assert "would delete 3 of 8 backups" in r.output


def test_retention_config_fallback():
    assert W._retention({"org_backups": {"keep_last": "x", "keep_days": -1}}) == (50, 14)
    assert W._retention({"org_backups": {"keep_last": True, "keep_days": 1.5}}) == (50, 14)
    assert W._retention({}) == (50, 14)
    assert W._retention({"org_backups": "junk"}) == (50, 14)
    assert W._retention({"org_backups": {"keep_last": 0, "keep_days": 0}}) == (0, 0)
    assert W._retention({"org_backups": {"keep_last": 7}}) == (7, 14)


def _cli_dirs(tmp_path, make_backups=True):
    data_dir, config_dir = tmp_path / "data", tmp_path / "config"
    bdir = data_dir / "org-backups"
    (bdir if make_backups else data_dir).mkdir(parents=True)
    config_dir.mkdir()
    return data_dir, config_dir, bdir


def test_cli_keep_days_override_takes_effect(tmp_path, monkeypatch):
    data_dir, config_dir, bdir = _cli_dirs(tmp_path)
    today = dt.date.today()
    for back in range(5):                            # 3 a day for the last 5 days
        _seed(bdir, "ideas.org", 3, day=today - dt.timedelta(days=back))
    run = _cli(monkeypatch, data_dir, config_dir)
    r = run(["backups", "prune"])                    # defaults (50, 14): everything is kept
    assert r.exit_code == 0, r.output
    assert "would delete 0 of 15 backups" in r.output
    r = run(["backups", "prune", "--yes", "--keep-last", "1", "--keep-days", "2"])
    assert r.exit_code == 0, r.output
    assert "deleted 13 of 15 backups" in r.output
    newest = lambda day: _name("ideas.org", dt.datetime.combine(day, dt.time(0, 0, 2)))  # noqa: E731
    assert sorted(os.listdir(bdir)) == sorted([newest(today),
                                               newest(today - dt.timedelta(days=1))])


def test_cli_yes_reports_failed_deletes(tmp_path, monkeypatch):
    data_dir, config_dir, bdir = _cli_dirs(tmp_path)
    old = dt.date.today() - dt.timedelta(days=100)
    _seed(bdir, "ideas.org", 5, day=old, body="x" * 250_000)
    run = _cli(monkeypatch, data_dir, config_dir)
    r = run(["backups", "prune", "--keep-last", "1", "--keep-days", "0"])
    assert r.exit_code == 0, r.output
    assert "would delete 4 of 5 backups (1.0 MB); kept 1" in r.output
    start = dt.datetime.combine(old, dt.time(0))
    stuck = {_name("ideas.org", start + dt.timedelta(seconds=i)) for i in (0, 1)}
    real_remove = os.remove

    def flaky(path):
        if os.path.basename(path) in stuck:
            raise OSError("read-only")
        real_remove(path)
    monkeypatch.setattr(W.os, "remove", flaky)
    r = run(["backups", "prune", "--yes", "--keep-last", "1", "--keep-days", "0"])
    assert r.exit_code == 0, r.output
    assert "deleted 2 of 5 backups (0.5 MB); kept 3" in r.output
    assert "2 delete(s) failed" in r.output
    assert "     2  ideas.org" in r.output             # per-basename table: actual deletes only
    assert len(os.listdir(bdir)) == 3


def test_cli_prune_without_backup_dir(tmp_path, monkeypatch):
    data_dir, config_dir, bdir = _cli_dirs(tmp_path, make_backups=False)
    r = _cli(monkeypatch, data_dir, config_dir)(["backups", "prune"])
    assert r.exit_code == 0, r.output
    assert "would delete 0 of 0 backups (0.0 MB); kept 0" in r.output
    assert not bdir.exists()


def test_cli_prune_huge_keep_days(tmp_path, monkeypatch):
    data_dir, config_dir, bdir = _cli_dirs(tmp_path)
    _seed(bdir, "ideas.org", 3, day=dt.date.today() - dt.timedelta(days=100))
    r = _cli(monkeypatch, data_dir, config_dir)(
        ["backups", "prune", "--keep-last", "0", "--keep-days", "1000000"])
    assert r.exit_code == 0, r.output
    assert "would delete 2 of 3 backups" in r.output


# --- SPEC-0285: persona refusal + prune-failure marker ---------------------------------------

MARKER = ".prune-error"


def _marker(bdir):
    return json.loads((bdir / MARKER).read_text())


def test_persona_run_cannot_prune_backups(tmp_path, monkeypatch):
    data_dir, config_dir, bdir = _cli_dirs(tmp_path)
    _seed(bdir, "ideas.org", 3, day=dt.date.today() - dt.timedelta(days=100))
    run = _cli(monkeypatch, data_dir, config_dir)
    before = sorted(os.listdir(bdir))
    monkeypatch.setenv("NW_PERSONA_RUN", "1")
    locked = []
    real_lock = W.org_write_lock

    def spy_lock(cfg):
        locked.append(1)
        return real_lock(cfg)
    monkeypatch.setattr(W, "org_write_lock", spy_lock)
    r = run(["backups", "prune", "--keep-last", "0", "--keep-days", "0", "--yes"])
    assert r.exit_code == 1, r.output
    assert not locked                                # refused before taking the org write lock
    assert "refusing to write the org from a campaign agent" in r.output
    assert "Traceback" not in r.output
    assert sorted(os.listdir(bdir)) == before
    r = run(["backups", "prune", "--keep-last", "0", "--keep-days", "0"])
    assert r.exit_code == 0, r.output
    assert sorted(os.listdir(bdir)) == before


def test_failed_auto_prune_leaves_marker(tmp_path, monkeypatch):
    cfg, ideas, bdir = _ideas_setup(tmp_path)

    def boom(*a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr(W, "prune_backups", boom)
    W.set_property(cfg, _task(cfg), "FOO", "bar")
    assert ":FOO: bar" in ideas.read_text()
    m = _marker(bdir)
    assert m["basename"] == "ideas.org"
    assert "PermissionError" in m["error"] and m["at"]


def test_partial_delete_failure_leaves_marker(tmp_path, monkeypatch):
    cfg, ideas, bdir = _ideas_setup(tmp_path)
    _seed(bdir, "ideas.org", 60)
    (bdir / MARKER).write_text(json.dumps({"at": "x", "basename": "ideas.org", "error": "old"}))

    real_remove = os.remove

    def boom(path):
        if str(path).endswith(".bak"):
            raise PermissionError("denied")
        real_remove(path)             # a wrongful marker clear stays visible
    monkeypatch.setattr(W.os, "remove", boom)
    W.set_property(cfg, _task(cfg), "FOO", "bar")
    assert ":FOO: bar" in ideas.read_text()
    m = _marker(bdir)
    assert m["basename"] == "ideas.org"
    assert re.fullmatch(r"\d+ delete\(s\) failed", m["error"])
    assert m["error"] != "old"


def test_prune_cmd_reports_and_clears_marker(tmp_path, monkeypatch):
    data_dir, config_dir, bdir = _cli_dirs(tmp_path)
    run = _cli(monkeypatch, data_dir, config_dir)
    marker = {"at": "2026-10-27T10:00:00", "basename": "tasks.org", "error": "boom"}
    (bdir / MARKER).write_text(json.dumps(marker))
    r = run(["backups", "prune", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["last_auto_prune_error"] == marker
    r = run(["backups", "prune"])
    warning = "last automatic prune failed at 2026-10-27T10:00:00 (tasks.org): boom"
    assert warning in r.stderr and warning not in r.stdout
    # a successful automatic prune for another file leaves the marker
    (tmp_path / "other").mkdir()
    cfg, ideas, ibdir = _ideas_setup(tmp_path / "other")
    (ibdir / MARKER).write_text(json.dumps(marker))
    W.set_property(cfg, _task(cfg), "FOO", "bar")
    assert _marker(ibdir) == marker
    # a successful prune of the same file clears it
    (ibdir / MARKER).write_text(json.dumps({**marker, "basename": "ideas.org"}))
    W.set_property(cfg, _task(cfg), "FOO", "qux")
    assert not (ibdir / MARKER).exists()
    # --yes shows the warning in that run, removes the marker, the next run shows nothing
    r = run(["backups", "prune", "--yes"])
    assert "last automatic prune failed" in r.output
    assert not (bdir / MARKER).exists()
    r = run(["backups", "prune", "--json"])
    assert json.loads(r.output)["last_auto_prune_error"] is None
    assert "last automatic prune failed" not in run(["backups", "prune"]).output


def test_marker_not_a_backup(tmp_path, monkeypatch):
    data_dir, config_dir, bdir = _cli_dirs(tmp_path)
    _seed(bdir, "ideas.org", 3, day=dt.date.today() - dt.timedelta(days=100))
    (bdir / MARKER).write_text(json.dumps({"at": "x", "basename": "ideas.org", "error": "e"}))
    r = _cli(monkeypatch, data_dir, config_dir)(
        ["backups", "prune", "--keep-last", "0", "--keep-days", "0", "--json"])
    res = json.loads(r.output)
    assert MARKER not in res["delete"] and res["scanned"] == 3 and len(res["delete"]) == 3


def test_persona_run_refuses_before_backup_copy(tmp_path, monkeypatch):
    """`_atomic_backup_write` itself refuses before it copies (the `org_writer` wrapper would
    otherwise mask a misplaced refusal)."""
    cfg, ideas, bdir = _ideas_setup(tmp_path)
    monkeypatch.setenv("NW_PERSONA_RUN", "1")
    with pytest.raises(W.PersonaOrgWriteRefused):
        W._atomic_backup_write(cfg, str(ideas), "changed\n")
    assert os.listdir(bdir) == []
    assert ideas.read_text() == "#+TODO: TODO | DONE\n* TODO a thing\n"


def _failing_bak_remove(monkeypatch):
    real_remove = os.remove

    def boom(path):
        if str(path).endswith(".bak"):
            raise PermissionError("denied")
        real_remove(path)
    monkeypatch.setattr(W.os, "remove", boom)


def test_yes_prune_with_failed_deletes_keeps_marker(tmp_path, monkeypatch):
    data_dir, config_dir, bdir = _cli_dirs(tmp_path)
    _seed(bdir, "ideas.org", 3, day=dt.date.today() - dt.timedelta(days=100))
    marker = {"at": "2026-10-27T10:00:00", "basename": "ideas.org", "error": "boom"}
    (bdir / MARKER).write_text(json.dumps(marker))
    _failing_bak_remove(monkeypatch)
    r = _cli(monkeypatch, data_dir, config_dir)(
        ["backups", "prune", "--keep-last", "0", "--keep-days", "0", "--yes"])
    assert r.exit_code == 0, r.output
    assert "delete(s) failed" in r.output
    assert _marker(bdir) == marker


def test_yes_prune_json_carries_marker_it_removes(tmp_path, monkeypatch):
    data_dir, config_dir, bdir = _cli_dirs(tmp_path)
    marker = {"at": "2026-10-27T10:00:00", "basename": "tasks.org", "error": "boom"}
    (bdir / MARKER).write_text(json.dumps(marker))
    r = _cli(monkeypatch, data_dir, config_dir)(["backups", "prune", "--yes", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["last_auto_prune_error"] == marker
    assert not (bdir / MARKER).exists()


def test_corrupt_marker_is_ignored_then_removed_by_all_files_prune(tmp_path, monkeypatch):
    cfg, ideas, bdir = _ideas_setup(tmp_path)
    (bdir / MARKER).write_text("{not json")
    W.set_property(cfg, _task(cfg), "FOO", "bar")    # per-basename clear leaves a corrupt marker
    assert (bdir / MARKER).read_text() == "{not json"
    data_dir, config_dir = tmp_path / "data", tmp_path / "config"
    run = _cli(monkeypatch, data_dir, config_dir)
    r = run(["backups", "prune"])
    assert r.exit_code == 0, r.output
    assert "last automatic prune failed" not in r.output
    assert json.loads(run(["backups", "prune", "--json"]).output)["last_auto_prune_error"] is None
    r = run(["backups", "prune", "--yes"])
    assert r.exit_code == 0, r.output
    assert not (bdir / MARKER).exists()


def test_failed_marker_write_does_not_fail_org_write(tmp_path, monkeypatch):
    cfg, ideas, bdir = _ideas_setup(tmp_path)
    old = {"at": "x", "basename": "ideas.org", "error": "old"}
    (bdir / MARKER).write_text(json.dumps(old))
    monkeypatch.setattr(W, "prune_backups", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    real_mkstemp = W.tempfile.mkstemp

    def mkstemp(*a, **k):
        if k.get("prefix") == ".prune-error-":
            raise OSError("no space")
        return real_mkstemp(*a, **k)
    monkeypatch.setattr(W.tempfile, "mkstemp", mkstemp)
    W.set_property(cfg, _task(cfg), "FOO", "bar")
    assert ":FOO: bar" in ideas.read_text()
    assert _marker(bdir) == old                      # the failed write left the old marker intact


def test_yes_prune_holds_org_write_lock(tmp_path, monkeypatch):
    data_dir, config_dir, bdir = _cli_dirs(tmp_path)
    depths = []
    real = W.prune_backups

    def spy(*a, **k):
        depths.append((k["dry_run"], getattr(W._ORG_LOCK_STATE, "depth", 0)))
        return real(*a, **k)
    monkeypatch.setattr(W, "prune_backups", spy)
    run = _cli(monkeypatch, data_dir, config_dir)
    assert run(["backups", "prune", "--yes"]).exit_code == 0
    assert run(["backups", "prune"]).exit_code == 0
    assert depths == [(False, 1), (True, 0)]

