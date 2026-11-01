"""SPEC-0349: `nw doctor`, a read-only, offline install health check (`nw.doctor.v1`)."""
import datetime as dt
import json
import os
import socket
import subprocess
from pathlib import Path

import jsonschema
import pytest
from click.testing import CliRunner

from nightwright import completion as C
from nightwright import doctor as D
from nightwright import skills as SK
from nightwright.agentic import conductor_cost as CC
from nightwright.agentic import launch as L
from nightwright.agentic import lease as LS
from nightwright.agentic import profiles as PR
from nightwright.agentic import store
from nightwright.cli import cli
from nightwright.config import load_config

PRICE = {"input": 3.0, "output": 15.0}
GATES = ("gates:\n  - {name: lint, cmd: 'true', when: loop}\n"
         "  - {name: test, cmd: 'true', when: close}\n")
def project_yaml(name):
    return f"schema: nw.project.v1\nproject: {name}\n" + GATES


def _proc(cmd, code=0, out=""):
    return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")


class Tools:
    """A fake PATH: `which` answers from `paths`; `run` answers `--version` and `auth token`."""

    def __init__(self, names=("uv", "git", "claude", "gh", "nw"), token=True):
        self.paths = {n: f"/fake/bin/{n}" for n in names}
        self.token = token
        self.hang = set()
        self.calls = []

    def which(self, name):
        return self.paths.get(name)

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        name = Path(cmd[0]).name
        if name in self.hang:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        if cmd[1:] == ["--version"]:
            return _proc(cmd, out=f"{name} 1.0\nmore\n")
        if cmd[1:] == ["auth", "token"]:
            return _proc(cmd, 0 if self.token else 1, "tok" if self.token else "")
        raise AssertionError(f"unexpected command {cmd}")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temporary HOME / XDG / NW_* tree plus a minimal nightwright checkout with one skill."""
    h = tmp_path / "home"
    for sub in ("", ".config", ".local/share", ".cache"):
        (h / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(h / ".local/share"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(h / ".cache"))
    monkeypatch.setenv("NW_CONFIG_DIR", str(h / ".config/nightwright"))
    monkeypatch.setenv("NW_DATA_DIR", str(h / ".local/share/nightwright"))
    monkeypatch.setenv("NW_CACHE_DIR", str(h / ".cache/nightwright"))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    (h / ".config/nightwright").mkdir()
    co = tmp_path / "checkout"
    (co / "src" / "nightwright").mkdir(parents=True)
    (co / "pyproject.toml").write_text("[project]\nname='x'\n")
    (co / "skills" / "nw-demo").mkdir(parents=True)
    (co / "skills" / "nw-demo" / "SKILL.md").write_text("demo\n")
    return h


def write_config(home, extra="", *, timezone=True, prices=True):
    text = f"nw_checkout: {home.parent / 'checkout'}\n"
    text += f"org_ideas_file: {home / 'ideas.org'}\n"
    text += f"specs_dir: {home.parent / 'specs'}\n"          # SPEC-0079: never the live docs/specs
    if timezone:
        text += "timezone: Europe/Lisbon\n"
    if prices:
        text += "agentic:\n  conductor:\n    prices:\n      claude-: {input: 3.0, output: 15.0}\n"
    text += extra
    (home / ".config/nightwright/config.yaml").write_text(text)
    return load_config()


def by_id(checks):
    return {c["id"]: c for c in checks}


def status(checks, id):
    return by_id(checks)[id]["status"]


# ---- tools ---------------------------------------------------------------------------------

def test_tools_present_missing_and_hanging():
    t = Tools()
    c = D.check_tool("uv", which=t.which, run_cmd=t.run)
    assert c["status"] == "ok" and c["summary"] == "uv 1.0" and c["fix"] is None
    del t.paths["git"]
    c = D.check_tool("git", which=t.which, run_cmd=t.run)
    assert c["status"] == "fail" and "not on PATH" in c["summary"] and c["fix"]
    t.hang.add("claude")
    c = D.check_tool("claude", which=t.which, run_cmd=t.run)
    assert c["status"] == "fail" and "over 5 s" in c["summary"]


def test_tool_version_exit_nonzero_fails():
    c = D.check_tool("uv", which=lambda n: "/x/uv", run_cmd=lambda cmd, **k: _proc(cmd, 2))
    assert c["status"] == "fail" and "exited 2" in c["summary"]


def test_gh_missing_is_warn():
    t = Tools(names=())
    assert D.check_gh(which=t.which, run_cmd=t.run)["status"] == "warn"
    t = Tools()
    assert D.check_gh(which=t.which, run_cmd=t.run)["status"] == "ok"


def test_gh_auth_skip_ok_and_fail():
    t = Tools(token=False)
    assert D.check_gh_auth([], which=t.which, run_cmd=t.run)["status"] == "skip"
    assert t.calls == []
    c = D.check_gh_auth(["Foo"], which=t.which, run_cmd=t.run)
    assert c["status"] == "fail" and "Foo" in c["summary"] and c["fix"] == "gh auth login"
    assert t.calls == [["/fake/bin/gh", "auth", "token"]]
    assert D.check_gh_auth(["Foo"], which=Tools().which, run_cmd=Tools().run)["status"] == "ok"
    assert D.check_gh_auth(["Foo"], which=Tools(names=()).which, run_cmd=t.run)["status"] == "fail"


def test_gh_auth_through_run_follows_pr_policy(home, tmp_path):
    repo = tmp_path / "foo"
    (repo / ".nw").mkdir(parents=True)
    (repo / ".nw" / "project.yaml").write_text(project_yaml("Foo"))
    cfg = write_config(home, f"outbox_targets:\n  Foo: {{repo_path: {repo}}}\n")
    t = Tools(token=False)
    assert status(D.run(cfg, which=t.which, run_cmd=t.run), "tools.gh_auth") == "skip"
    (repo / ".nw" / "project.yaml").write_text(project_yaml("Foo")
                                              + "merge: {policy: pr}\n")
    assert status(D.run(cfg, which=t.which, run_cmd=t.run), "tools.gh_auth") == "fail"


# ---- install / config ----------------------------------------------------------------------

def test_nw_on_path_same_different_missing(tmp_path):
    nw = tmp_path / "bin" / "nw"
    nw.parent.mkdir()
    nw.write_text("")
    other = tmp_path / "other-nw"
    other.write_text("")
    c = D.check_nw_on_path(which=lambda n: str(nw), entry_points=[nw])
    assert c["status"] == "ok"
    c = D.check_nw_on_path(which=lambda n: str(other), entry_points=[nw])
    assert c["status"] == "warn" and str(other) in c["detail"] and str(nw) in c["detail"]
    assert D.check_nw_on_path(which=lambda n: None, entry_points=[nw])["status"] == "fail"


def test_checkout_resolved_unset_invalid(home, monkeypatch, tmp_path):
    from nightwright import rules
    c = D.check_checkout({"nw_checkout": str(tmp_path / "checkout")})
    assert c["status"] == "ok" and "nw_checkout" in c["detail"]
    c = D.check_checkout({})
    assert c["status"] == "ok" and "editable" in c["detail"]          # this test runs from a checkout
    monkeypatch.setattr(rules, "discover_nw_checkout", lambda: None)
    c = D.check_checkout({})
    assert c["status"] == "warn" and c["fix"] == rules.CHECKOUT_HINT
    c = D.check_checkout({"nw_checkout": str(tmp_path / "nope")})
    assert c["status"] == "fail" and "not a nightwright checkout" in c["detail"]


def test_config_parse_absent_valid_malformed(home):
    path = home / ".config/nightwright/config.yaml"
    assert D.check_config_parse(None, path=path)["status"] == "warn"
    path.write_text("timezone: UTC\n")
    assert D.check_config_parse(None, path=path)["status"] == "ok"
    c = D.check_config_parse("ScannerError: bad", path=path)
    assert c["status"] == "fail" and c["detail"] == "ScannerError: bad"


def test_config_bad_timezone_fails_through_cli(home):
    (home / ".config/nightwright/config.yaml").write_text("timezone: Mars/Olympus\n")
    r = CliRunner().invoke(cli, ["doctor", "--json"])
    assert r.exit_code == 1, r.output
    c = by_id(json.loads(r.output)["checks"])["config.parse"]
    assert c["status"] == "fail" and "Mars/Olympus" in c["detail"]


def test_timezone_set_absent_and_no_config(home):
    path = home / ".config/nightwright/config.yaml"
    path.write_text("timezone: UTC\n")
    assert D.check_timezone({}, path=path)["status"] == "ok"
    path.write_text("work_root: ~/w\n")
    c = D.check_timezone({"timezone": "America/New_York"}, path=path)
    assert c["status"] == "warn" and "America/New_York" in c["summary"]
    assert D.check_timezone(None, path=path)["status"] == "skip"


def test_org_ideas_file_present_missing_unreadable(tmp_path):
    p = tmp_path / "ideas.org"
    assert D.check_org_ideas_file({"org_ideas_file": str(p)})["status"] == "warn"
    p.write_text("* IDEA x\n")
    assert D.check_org_ideas_file({"org_ideas_file": str(p)})["status"] == "ok"
    assert D.check_org_ideas_file(None)["status"] == "skip"
    if os.geteuid() == 0:
        return                                                    # root reads anything
    p.chmod(0)
    try:
        assert D.check_org_ideas_file({"org_ideas_file": str(p)})["status"] == "fail"
    finally:
        p.chmod(0o644)


def test_data_dir_writable_absent_readonly(tmp_path):
    d = tmp_path / "data"
    assert D.check_data_dir(d)["status"] == "warn"
    d.mkdir()
    assert D.check_data_dir(d)["status"] == "ok"
    if os.geteuid() == 0:
        return
    d.chmod(0o500)
    try:
        assert D.check_data_dir(d)["status"] == "fail"
        assert D.check_data_dir(d / "sub")["status"] == "fail"    # nearest parent read-only
    finally:
        d.chmod(0o700)


# ---- skills / completion -------------------------------------------------------------------

def test_skills_linked_missing_stale(home, tmp_path):
    cfg = {"nw_checkout": str(tmp_path / "checkout")}
    dest = tmp_path / "skills-dest"
    c = D.check_skills(cfg, dest=dest)
    assert c["status"] == "warn" and "nw-demo: not installed" in c["detail"]
    assert c["fix"] == "nw skills install"
    SK.install(dest=dest, source=tmp_path / "checkout" / "skills")
    assert D.check_skills(cfg, dest=dest)["status"] == "ok"
    copy_dest = tmp_path / "copy-dest"
    SK.install(dest=copy_dest, mode="copy", source=tmp_path / "checkout" / "skills")
    assert D.check_skills(cfg, dest=copy_dest)["status"] == "ok"
    (tmp_path / "checkout" / "skills" / "nw-demo" / "SKILL.md").write_text("changed\n")
    c = D.check_skills(cfg, dest=copy_dest)
    assert c["status"] == "warn" and "copied (stale)" in c["detail"]


def test_skills_skip_without_checkout(monkeypatch):
    from nightwright import rules
    monkeypatch.setattr(rules, "discover_nw_checkout", lambda: None)
    assert D.check_skills({})["status"] == "skip"


def test_completion_matching_missing_stale_foreign_unknown(home):
    assert D.check_completion("/bin/tcsh")["status"] == "skip"
    assert D.check_completion("")["status"] == "skip"
    c = D.check_completion("/bin/bash")
    assert c["status"] == "warn" and c["fix"] == "nw completion install --shell bash"
    path = C.install("bash")
    assert D.check_completion("/usr/bin/bash")["status"] == "ok"
    path.write_text(C._MARKER + "old body\n")
    c = D.check_completion("/bin/bash")
    assert c["status"] == "warn" and "stale" in c["summary"]
    path.write_text("# someone else's\n")
    c = D.check_completion("/bin/bash")
    assert c["status"] == "warn" and "not nightwright" in c["summary"]


# ---- agentic -------------------------------------------------------------------------------

def _acfg(tmp_path, **agentic):
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "cfg"),
            "agentic": {"lease_ttl_s": 120, "profiles": {}, **agentic}}


def test_missing_prices_conductor_and_steward_override(tmp_path):
    cfg = _acfg(tmp_path, conductor={"prices": {"claude-sonnet-5-5": PRICE}})
    assert L.missing_prices(cfg) == []
    assert D.check_prices(cfg)["status"] == "ok"
    cfg["agentic"]["conductor"]["launch"] = {"steward": {"model": "claude-haiku-5-5"}}
    assert L.missing_prices(cfg) == [("steward", "claude-haiku-5-5")]
    c = D.check_prices(cfg)
    assert c["status"] == "fail" and "agentic.conductor.prices.claude-haiku-5-5" in c["fix"]
    assert L.missing_prices(_acfg(tmp_path)) == [("conductor", "claude-sonnet-5-5"),
                                                 ("steward", "claude-sonnet-5-5")]
    assert D.check_prices(None)["status"] == "skip"


def test_agentic_dirs(tmp_path):
    cfg = _acfg(tmp_path)
    assert D.check_agentic_dirs(cfg)["status"] == "ok"            # nearest parent is tmp_path
    if os.geteuid() == 0:
        return
    (tmp_path / "data").mkdir()
    (tmp_path / "data").chmod(0o500)
    try:
        assert D.check_agentic_dirs(cfg)["status"] == "fail"
    finally:
        (tmp_path / "data").chmod(0o700)


def _campaign(cfg, cid="SPEC-0001-x"):
    store.create_campaign(cfg, {"id": cid, "spec_id": "SPEC-0001", "state": "IMPLEMENTING"})


def test_runner_skip_ok_stale_and_unattended(tmp_path):
    cfg = _acfg(tmp_path)
    assert D.check_runner(cfg)["status"] == "skip"
    t0 = dt.datetime(2026, 10, 25, 12, 0, tzinfo=dt.timezone.utc)
    lease = LS.acquire(cfg, now=t0)
    try:
        assert D.check_runner(cfg, now=t0 + dt.timedelta(seconds=10))["status"] == "ok"
        c = D.check_runner(cfg, now=t0 + dt.timedelta(seconds=500))
        assert c["status"] == "warn" and "stale" in c["summary"]
    finally:
        lease.release()
    _campaign(cfg)
    c = D.check_runner(cfg)
    assert c["status"] == "warn" and c["fix"] == "nw queue run --detach"


def test_conductor_session_present_absent(tmp_path):
    cfg = _acfg(tmp_path)
    assert D.check_conductor(cfg)["status"] == "skip"
    _campaign(cfg)
    assert D.check_conductor(cfg)["status"] == "warn"
    CC.record_session(cfg, "conductor", env={"CLAUDE_CODE_SESSION_ID": "abc"})
    c = D.check_conductor(cfg)
    assert c["status"] == "ok" and "abc" in c["detail"]


def test_workers_absent_valid_unparseable(tmp_path):
    cfg = _acfg(tmp_path)
    assert D.check_workers(cfg)["status"] == "skip"
    p = tmp_path / "data" / "agentic" / "workers.json"
    p.parent.mkdir(parents=True)
    p.write_text("")
    assert D.check_workers(cfg)["status"] == "skip"
    p.write_text(json.dumps({"schema": "nw.workers.v1", "workers": [
        {"name": "box", "ssh": "box"}, {"name": "off", "ssh": "off", "enabled": False}]}))
    c = D.check_workers(cfg)
    assert c["status"] == "ok" and c["summary"] == "1/2 workers enabled"
    assert "nw workers check" in c["detail"]
    p.write_text("{not json")
    assert D.check_workers(cfg)["status"] == "fail"


def test_workers_without_ssh_dir_warn(tmp_path, monkeypatch):
    from nightwright.agentic import workers as W
    cfg = _acfg(tmp_path)
    p = tmp_path / "data" / "agentic" / "workers.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"schema": "nw.workers.v1", "workers": [{"name": "b", "ssh": "b"}]}))
    monkeypatch.setattr(W, "ssh_dir", lambda c: None)
    assert D.check_workers(cfg)["status"] == "warn"


# ---- projects ------------------------------------------------------------------------------

def test_project_config_valid_invalid_no_gates_absent_missing(home, tmp_path):
    repo = tmp_path / "foo"
    cfg = write_config(home, f"outbox_targets:\n  Foo: {{repo_path: {repo}}}\n")
    assert D.check_project_config(cfg, "Foo", repo)["status"] == "fail"          # root missing
    repo.mkdir()
    assert D.check_project_config(cfg, "Foo", repo)["status"] == "skip"
    assert D.check_project_profiles(cfg, "Foo", repo)["status"] == "skip"
    (repo / ".nw").mkdir()
    y = repo / ".nw" / "project.yaml"
    y.write_text(project_yaml("Foo"))
    c = D.check_project_config(cfg, "Foo", repo)
    assert c["status"] == "ok" and c["summary"] == "1 loop / 1 close gates"
    assert D.check_project_profiles(cfg, "Foo", repo)["status"] == "ok"
    y.write_text("schema: nw.project.v1\nproject: Foo\n")
    c = D.check_project_config(cfg, "Foo", repo)
    assert c["status"] == "warn" and "loop / close" in c["summary"]
    y.write_text("schema: nw.project.v1\nbogus: 1\n")
    c = D.check_project_config(cfg, "Foo", repo)
    assert c["status"] == "fail" and "bogus" in c["detail"]


def test_project_broken_profile_fails(home, tmp_path):
    repo = tmp_path / "foo"
    (repo / ".nw" / "profiles").mkdir(parents=True)
    (repo / ".nw" / "project.yaml").write_text(project_yaml("Foo"))
    (repo / ".nw" / "profiles" / "broken.yaml").write_text("schema: nw.profile.v1\nbogus: 1\n")
    cfg = write_config(home, f"outbox_targets:\n  Foo: {{repo_path: {repo}}}\n")
    c = D.check_project_profiles(cfg, "Foo", repo)
    assert c["status"] == "fail" and "broken" in c["detail"]
    assert c["fix"] == "nw profiles check --project Foo"


def test_profiles_check_all(tmp_path):
    cfg = {"config_dir": str(tmp_path / "cfg"), "agentic": {"profiles": {},
                                                           "default_profile": "ghost"}}
    res = PR.check_all(cfg)
    names = [r["name"] for r in res]
    assert {"balanced", "frugal", "thorough"} <= set(names) and names[-1] == "ghost"
    assert all(r["ok"] for r in res if r["name"] != "ghost")
    ghost = res[-1]
    assert ghost["ok"] is False and ghost["error"]


# ---- legacy / retention --------------------------------------------------------------------

def test_legacy_wt_dirs(home):
    assert D.check_legacy_wt()["status"] == "ok"
    (home / ".config" / "wt").mkdir()
    assert D.check_legacy_wt()["status"] == "ok"                       # empty is fine
    (home / ".config" / "wt" / "config.yaml").write_text("x: 1\n")
    c = D.check_legacy_wt()
    assert c["status"] == "warn" and c["fix"] == "nw migrate-from-wt"


def test_retention(tmp_path):
    settings = tmp_path / "settings.json"
    hist = tmp_path / "history.jsonl"
    today = dt.date(2026, 11, 1)
    hist.write_text(json.dumps({"day": "2026-09-01"}) + "\n")
    c = D.check_retention(hist, settings_path=settings, today=today)
    assert c["status"] == "warn" and "2026-09-01" in c["summary"]       # absent -> 30 days
    settings.write_text(json.dumps({"cleanupPeriodDays": 365}))
    assert D.check_retention(hist, settings_path=settings, today=today)["status"] == "ok"
    settings.write_text(json.dumps({"cleanupPeriodDays": 30}))
    hist.write_text(json.dumps({"day": "2026-10-30"}) + "\n")
    assert D.check_retention(hist, settings_path=settings, today=today)["status"] == "ok"
    hist.unlink()
    assert D.check_retention(hist, settings_path=settings, today=today)["status"] == "warn"


# ---- run / CLI -----------------------------------------------------------------------------

def _clean(home, tmp_path):
    """A correctly set-up tmp HOME: timezone + prices, ideas file, skills, completion."""
    cfg = write_config(home)
    (home / "ideas.org").write_text("* IDEA x\n")
    SK.install(source=tmp_path / "checkout" / "skills")                 # into tmp HOME/.claude
    C.install("bash")
    (home / ".claude" / "settings.json").write_text(json.dumps({"cleanupPeriodDays": 365}))
    return cfg


def test_clean_environment_has_no_findings(home, tmp_path):
    cfg = _clean(home, tmp_path)
    t = Tools()
    checks = D.run(cfg, which=t.which, run_cmd=t.run)
    allowed = {"install.nw_on_path", "data.dir"}       # fake nw path; data dir not created yet
    bad = {c["id"]: c["status"] for c in checks
           if c["status"] in ("warn", "fail") and c["id"] not in allowed}
    assert bad == {}
    assert D.payload(checks)["ok"] is True


def test_check_that_raises_becomes_fail(home, tmp_path, monkeypatch):
    cfg = _clean(home, tmp_path)
    monkeypatch.setattr(D, "check_legacy_wt", lambda: 1 / 0)
    t = Tools()
    checks = by_id(D.run(cfg, which=t.which, run_cmd=t.run))
    assert checks["legacy.wt_dirs"]["status"] == "fail"
    assert "ZeroDivisionError" in checks["legacy.wt_dirs"]["summary"]
    assert checks["retention.cleanup_period"]["status"] == "ok"        # later checks still ran


def _snapshot(root):
    return {str(p): p.read_bytes() for p in Path(root).rglob("*") if p.is_file()} | {
        str(p): None for p in Path(root).rglob("*") if not p.is_file()}


def test_read_only_and_offline(home, tmp_path, monkeypatch):
    repo = tmp_path / "foo"
    (repo / ".nw").mkdir(parents=True)
    (repo / ".nw" / "project.yaml").write_text(project_yaml("Foo")
                                              + "merge: {policy: pr}\n")
    _clean(home, tmp_path)
    cfg = write_config(home, f"outbox_targets:\n  Foo: {{repo_path: {repo}}}\n")
    _campaign(cfg)
    w = Path(cfg["data_dir"]) / "agentic" / "workers.json"
    w.write_text(json.dumps({"schema": "nw.workers.v1", "workers": [{"name": "b", "ssh": "b"}]}))
    before = _snapshot(tmp_path)

    def no_socket(*a, **k):
        raise AssertionError("doctor opened a socket")

    real_run = subprocess.run

    def guarded_run(cmd, *a, **k):
        argv = [str(x) for x in cmd]
        if Path(argv[0]).name == "ssh" or argv[1:3] == ["auth", "status"]:
            raise AssertionError(f"doctor ran {argv}")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(socket, "socket", no_socket)
    monkeypatch.setattr(subprocess, "run", guarded_run)
    t = Tools()
    checks = D.run(cfg, which=t.which, run_cmd=t.run)
    assert len(checks) > 20 and not any("crashed" in c["summary"] for c in checks)
    assert by_id(checks)["tools.gh_auth"]["status"] == "ok"
    assert all(c[1:] != ["auth", "status"] for c in t.calls)
    assert _snapshot(tmp_path) == before


def invoke(*args):
    return CliRunner().invoke(cli, ["doctor", *args])


def test_json_validates_against_schema(home, tmp_path):
    _clean(home, tmp_path)
    r = invoke("--json")
    doc = json.loads(r.output)
    jsonschema.validate(doc, D.DOCTOR_SCHEMA)
    assert doc["schema"] == "nw.doctor.v1"
    assert doc["ok"] == (doc["counts"]["fail"] == 0)
    for s in D.STATUSES:
        assert doc["counts"][s] == sum(1 for c in doc["checks"] if c["status"] == s)
    assert r.exit_code == (0 if doc["ok"] else 1)
    for c in doc["checks"]:
        if c["status"] in ("ok", "skip"):
            assert c["fix"] is None


def test_exit_codes_warn_only_then_missing_price(home, tmp_path, monkeypatch):
    t = Tools()
    monkeypatch.setattr(D.shutil, "which", t.which)
    monkeypatch.setattr(D, "_entry_points", lambda argv0=None: {Path("/fake/bin/nw")})
    real_run = subprocess.run
    monkeypatch.setattr(D.subprocess, "run", lambda cmd, *a, **k: (
        t.run(cmd, **k) if str(cmd[0]).startswith("/fake/") else real_run(cmd, *a, **k)))
    write_config(home, timezone=False)                                  # warns only
    r = invoke("--json")
    doc = json.loads(r.output)
    assert doc["counts"]["warn"] > 0 and doc["counts"]["fail"] == 0, doc
    assert r.exit_code == 0
    write_config(home, timezone=False, prices=False)
    r = invoke("--json")
    assert r.exit_code == 1
    assert by_id(json.loads(r.output)["checks"])["agentic.prices"]["status"] == "fail"


def test_malformed_config_reported_not_raised(home):
    (home / ".config/nightwright/config.yaml").write_text("timezone: [unclosed\n")
    r = invoke("--json")
    assert r.exit_code == 1 and (r.exception is None or isinstance(r.exception, SystemExit))
    doc = json.loads(r.output)
    checks = by_id(doc["checks"])
    assert checks["config.parse"]["status"] == "fail"
    assert "yaml" in checks["config.parse"]["detail"].lower()
    for id in ("config.timezone", "agentic.prices", "agentic.runner", "skills.installed"):
        assert checks[id]["status"] == "skip" and checks[id]["summary"] == D.NO_CONFIG
    r = invoke()
    assert r.exit_code == 1 and "Traceback" not in r.output
    assert "config.parse" in r.output
    other = CliRunner().invoke(cli, ["projects", "--json"])           # other commands still raise
    assert other.exit_code != 0 and other.exception is not None
    assert not isinstance(other.exception, SystemExit)


def test_project_option_limits_and_rejects_unknown(home, tmp_path):
    for name in ("Foo", "Bar"):
        (tmp_path / name / ".nw").mkdir(parents=True)
        (tmp_path / name / ".nw" / "project.yaml").write_text(project_yaml(name))
    write_config(home, "outbox_targets:\n"
                 f"  Foo: {{repo_path: {tmp_path / 'Foo'}}}\n"
                 f"  Bar: {{repo_path: {tmp_path / 'Bar'}}}\n")
    ids = [c["id"] for c in json.loads(invoke("--json").output)["checks"]]
    assert "project.Foo.config" in ids and "project.Bar.config" in ids
    ids = [c["id"] for c in json.loads(invoke("--json", "--project", "Foo").output)["checks"]]
    proj = [i for i in ids if i.startswith("project.")]
    assert proj == ["project.Foo.config", "project.Foo.profiles"]
    r = invoke("--project", "Nope")
    assert r.exit_code == 2 and "unknown project" in r.output


def test_human_output_groups_fix_hints_and_counts(home, tmp_path):
    write_config(home, timezone=False)
    r = CliRunner().invoke(cli, ["doctor"], env={"COLUMNS": "200"})
    out = r.output
    for group in ("tools", "install", "config", "data", "agentic", "legacy", "retention"):
        assert f"\n{group}" in "\n" + out
    assert "config.timezone" in out and "fix:" not in out
    last = out.strip().splitlines()[-1]
    assert last.endswith("skip") and " ok · " in last and " warn · " in last
    r = CliRunner().invoke(cli, ["doctor", "--fix-hints"], env={"COLUMNS": "200"})
    assert "fix: set `timezone:" in r.output


def test_help_lists_options():
    r = CliRunner().invoke(cli, ["doctor", "--help"])
    assert r.exit_code == 0
    for opt in ("--json", "--project", "--fix-hints"):
        assert opt in r.output
