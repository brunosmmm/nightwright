"""Org backup retention (SPEC-0271): per-basename keep-last + one-per-day selection, prune on
every backup write (that file only, best-effort), `wt backups prune` dry run / --yes, and the
`org_backups` config fallback."""
import datetime as dt
import os
from zoneinfo import ZoneInfo

from click.testing import CliRunner

from wt import org_write as W
from wt.cli import cli
from wt.config import load_config
from wt.org import load_tasks


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


def _seed(bdir, base, n, day=None):
    start = dt.datetime.combine(day or dt.date.today(), dt.time(0))
    for i in range(n):
        (bdir / _name(base, start + dt.timedelta(seconds=i))).write_text("old\n")


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
    assert not list(ideas.parent.glob(".wt-org-*"))          # no temp file left: atomic replace


def test_prune_listdir_failure_does_not_fail_write(tmp_path, monkeypatch):
    cfg, ideas, _ = _ideas_setup(tmp_path)

    def boom(path):
        raise OSError("nope")
    monkeypatch.setattr(W.os, "listdir", boom)
    W.set_property(cfg, _task(cfg), "FOO", "bar")
    assert ":FOO: bar" in ideas.read_text()


def _cli(monkeypatch, data_dir, config_dir):
    """Run `wt` against a real load_config() of tmp dirs (patched in directly so a leaked
    `wt.cli.load_config` patch from another test can't redirect it)."""
    monkeypatch.setenv("WT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("WT_CONFIG_DIR", str(config_dir))
    cfg = load_config()
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
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
