"""SPEC-0330: the wt -> nightwright rename. Hard cut for every user-facing name; legacy `wt.`
schema ids, `wtfu:` markers, `wt(<cid>):` commits and `owner = "wt"` sheet columns stay
READABLE. Skill / allow-rule cleanup and the migration command run on tmp paths only — never
the real ~/.claude, ~/.config/wt or ~/.local/share/wt."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright import agent_permissions as AP
from nightwright import legacy as LG
from nightwright import migrate as MG
from nightwright import paths
from nightwright import skills as SK
from nightwright.cli import cli

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent


# ---- hard cut ------------------------------------------------------------------------------

def test_console_script_is_nw_only():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'name = "nightwright"' in text
    assert 'nw = "nightwright.cli:cli"' in text
    assert not re.search(r"^wt\s*=", text, re.M)
    assert 'packages = ["src/nightwright"]' in text


def test_no_wt_env_fallback(monkeypatch, tmp_path):
    for var in ("NW_CONFIG_DIR", "NW_DATA_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WT_CONFIG_DIR", str(tmp_path / "legacy-cfg"))
    monkeypatch.setenv("WT_DATA_DIR", str(tmp_path / "legacy-data"))
    assert paths.config_dir() == str(tmp_path / ".config" / "nightwright")
    assert paths.data_dir() == str(tmp_path / ".local" / "share" / "nightwright")
    monkeypatch.setenv("NW_DATA_DIR", str(tmp_path / "d"))
    assert paths.data_dir() == str(tmp_path / "d")


def test_skills_are_nw_prefixed():
    names = [p.name for p in (ROOT / "skills").iterdir() if (p / "SKILL.md").is_file()]
    assert names and all(n.startswith("nw-") for n in names)
    for n in names:
        fm = (ROOT / "skills" / n / "SKILL.md").read_text().split("---")[1]
        assert re.search(rf"^name:\s*{re.escape(n)}\s*$", fm, re.M)


def test_project_config_dir_is_dot_nw():
    from nightwright.agentic import project as PJ
    assert str(PJ.CONFIG_RELPATH) == ".nw/project.yaml"
    assert (ROOT / ".nw" / "project.yaml").is_file() and not (ROOT / ".wt").exists()
    assert PJ.worktree_root({}, Path("/r/repo"), "P") == Path("/r/.nw-worktrees/P")


# ---- legacy readers ------------------------------------------------------------------------

def test_legacy_schema_helper():
    assert LG.legacy_schema("nw.event.v1") == "wt.event.v1"
    assert LG.schema_ok("wt.ledger.event.v1", "nw.ledger.event.v1")
    assert LG.schema_ok("nw.ledger.event.v1", "nw.ledger.event.v1")
    assert not LG.schema_ok("xx.ledger.event.v1", "nw.ledger.event.v1")


def test_ledger_and_run_records_accept_legacy_ids():
    from nightwright.agentic import schemas as SC
    assert SC.LEDGER_SCHEMA.startswith("nw.")
    rec = {k: "x" for k in SC.ENVELOPE}
    rec.update(seq=1, schema="wt.ledger.event.v1", type="note", text="t")
    assert SC.ledger_problems(rec) == []
    rec["schema"] = "zz.ledger.event.v1"
    assert any("schema" in p for p in SC.ledger_problems(rec))
    assert SC.record_problems({"schema": "wt.run.v1"}, (), SC.RUN_SCHEMA) == []
    assert SC.record_problems({"schema": "nw.run.v1"}, (), SC.RUN_SCHEMA) == []


def test_project_yaml_accepts_legacy_schema():
    from nightwright.agentic import project as PJ
    for sid in ("nw.project.v1", "wt.project.v1"):
        PJ.validate({"schema": sid, "project": "P", "base": "main", "gates": []})
    with pytest.raises(ValueError):
        PJ.validate({"schema": "xx.project.v1", "project": "P", "base": "main", "gates": []})


def test_spec_reservation_store_reads_legacy_schema(tmp_path):
    from nightwright import spec_numbers as SN
    store = tmp_path / "spec-reservations.json"
    store.write_text(json.dumps({"schema": "wt.spec.reservations.store.v1", "reservations": []}))
    assert SN._load({"data_dir": str(tmp_path)}) == []


def test_followup_markers_read_legacy(tmp_path):
    from nightwright.agentic import followups as FU
    assert FU.MARKER == "nwfu:" and FU.GROUP_MARKER == "nwfug:"
    assert FU.legacy_marker("nwfug:C1") == "wtfug:C1" and FU.legacy_marker("nwfu:ab") == "wtfu:ab"
    summary = "- [ ] wtfu:0123456789ab major x\n- [ ] nwfu:ba9876543210 minor y\n"
    assert FU.summary_keys(summary) == ["0123456789ab", "ba9876543210"]
    assert FU._SINGLE_RE.match("wtfu:0123456789ab [SPEC-0001] t")
    ideas = tmp_path / "ideas.org"
    ideas.write_text("* IDEA wtfug:C1 [SPEC-0001] review follow-ups (1 findings)\n"
                     ":PROPERTIES:\n:ID: IDEA-001\n:END:\n")
    cfg = {"org_ideas_file": str(ideas), "data_dir": str(tmp_path),
           "specs_dir": str(tmp_path / "specs")}
    assert FU.find_marker(cfg, "nwfug:C1", open_only=True) == "IDEA-001"


def test_journey_strips_legacy_and_new_commit_prefix():
    from nightwright.agentic import journey as J
    assert J._COMMIT_PREFIX.sub("", "wt(C1): implementer run abc") == "implementer run abc"
    assert J._COMMIT_PREFIX.sub("", "nw(C1): implementer run abc") == "implementer run abc"


def test_sheet_manifest_owner_wt_is_read_as_nw():
    from nightwright import sheet_schemes as SS
    m = SS._parse_manifest({"name": "x", "id_column": "ID", "columns": [
        {"header": "ID", "source": "id", "owner": "wt"},
        {"header": "Note", "source": "note", "owner": "sheet"}]})
    assert [c.header for c in m.nw_owned()] == ["ID"]


# ---- skills install cleanup (tmp dest only) ------------------------------------------------

def _repo_skills(tmp_path):
    src = tmp_path / "repo" / "skills"
    for n in ("nw-orient", "nw-capture"):
        (src / n).mkdir(parents=True)
        (src / n / "SKILL.md").write_text(f"---\nname: {n}\ndescription: d\n---\n")
    return src


def test_remove_legacy_skills_only_ours(tmp_path):
    src = _repo_skills(tmp_path)
    dest = tmp_path / "claude-skills"
    dest.mkdir()
    (dest / "wt-orient").symlink_to(tmp_path / "old-checkout" / "skills" / "wt-orient")  # dangling
    old_copy = dest / "wt-capture"
    old_copy.mkdir()
    (old_copy / ".wt-skill.json").write_text("{}")
    foreign = tmp_path / "elsewhere" / "wt-related"
    foreign.mkdir(parents=True)
    (dest / "wt-related").symlink_to(foreign)            # not a skill this repo ships
    (dest / "wt-mine").mkdir()                            # unrelated user dir
    removed = SK.remove_legacy(dest=dest, source=src)
    assert sorted(removed) == ["wt-capture", "wt-orient"]
    assert (dest / "wt-related").is_symlink() and (dest / "wt-mine").is_dir()
    assert SK.remove_legacy(dest=dest, source=src) == []


def test_remove_legacy_keeps_foreign_wt_named_symlink(tmp_path):
    src = _repo_skills(tmp_path)
    dest = tmp_path / "d"
    dest.mkdir()
    other = tmp_path / "other-tool" / "orient"
    other.mkdir(parents=True)
    (dest / "wt-orient").symlink_to(other)
    assert SK.remove_legacy(dest=dest, source=src) == []
    assert (dest / "wt-orient").is_symlink()


def test_cli_skills_install_removes_legacy_links_and_rules(tmp_path, monkeypatch):
    dest = tmp_path / "skills-dest"
    dest.mkdir()
    (dest / "wt-orient").symlink_to(tmp_path / "gone" / "skills" / "wt-orient")
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": [
        "Bash(ls)", "Bash(wt queue *)", "Bash(WT_MAILBOX_ADDR=* wt mailbox *)",
        "Bash(wt something-else *)"]}}))
    r = CliRunner().invoke(cli, ["skills", "install", "--dest", str(dest),
                                 "--settings", str(settings)])
    assert r.exit_code == 0, r.output
    assert not (dest / "wt-orient").is_symlink() and (dest / "nw-orient").is_symlink()
    allow = json.loads(settings.read_text())["permissions"]["allow"]
    assert "Bash(wt queue *)" not in allow and "Bash(WT_MAILBOX_ADDR=* wt mailbox *)" not in allow
    assert "Bash(ls)" in allow and "Bash(wt something-else *)" in allow   # not ours: kept
    assert all(rule in allow for rule in AP.ALLOW_RULES)
    assert "removed" in r.output


def test_permissions_sync_removes_only_legacy(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"permissions": {"allow": list(AP.LEGACY_RULES) + ["Bash(x)"]}}))
    res = AP.sync(p)
    assert sorted(res["removed"]) == sorted(AP.LEGACY_RULES)
    allow = json.loads(p.read_text())["permissions"]["allow"]
    assert allow == ["Bash(x)"] + list(AP.ALLOW_RULES)
    before = p.read_bytes()
    assert AP.sync(p) == {"added": [], "removed": [], "hook": False} and p.read_bytes() == before
    assert all(r.startswith(("Bash(nw ", "Bash(NW_MAILBOX_ADDR=* nw ")) for r in AP.ALLOW_RULES)


# ---- nw migrate-from-wt (tmp dirs only) ----------------------------------------------------

OLD_CO, NEW_CO = "/home/u/work/wt", "/home/u/work/nightwright"


def _legacy_install(tmp_path):
    home = tmp_path / "home"
    cfg = home / ".config" / "wt"
    data = home / ".local" / "share" / "wt"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text(
        "# my config\n"
        f"wt_checkout: {OLD_CO}\n"
        f"data_dir: {data}\n"
        "outbox_targets:\n"
        "  acme:\n"
        "    repo_path: /home/u/work/wt-other   # not the checkout\n"
        "    scheme: wt-native\n"
        f"agentic:\n  worktrees_root: /home/u/work/.wt-worktrees\n")
    (cfg / "config.yaml.bak.1").write_text(f"wt_checkout: {OLD_CO}\n")
    camp = data / "agentic" / "campaigns" / "C1"
    camp.mkdir(parents=True)
    state = {"schema": "wt.campaign.v1", "id": "C1", "repo": {
        "root": OLD_CO, "branch": "wt/C1",
        "worktree": "/home/u/work/.wt-worktrees/Meta-Tools/C1"},
        "last_gate": {"results": [{"cwd": "/home/u/work/.wt-worktrees/Meta-Tools/C1",
                                   "tail": f"failed in {OLD_CO}/src"}]},
        "other": {"root": "/home/u/work/wt-rename"}}
    (camp / "state.json").write_text(json.dumps(state))
    ledger = '{"schema": "wt.ledger.event.v1", "worktree": "/home/u/work/.wt-worktrees/x"}\n'
    (camp / "ledger.jsonl").write_text(ledger)
    (data / "spec-reservations.json").write_text(json.dumps({
        "schema": "wt.spec.reservations.store.v1",
        "reservations": [{"number": 7, "key": f"{OLD_CO}/.git::docs/specs"},
                         {"number": 8, "key": "/home/u/work/other/.git::docs/specs"}]}))
    return home, cfg, data, ledger


def _env(monkeypatch, home):
    for var in ("NW_CONFIG_DIR", "NW_DATA_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))


def _run(*args):
    return CliRunner().invoke(cli, ["migrate-from-wt", "--old-checkout", OLD_CO,
                                    "--new-checkout", NEW_CO, *args])


def test_migrate_dry_run_changes_nothing(tmp_path, monkeypatch):
    home, cfg, data, _ = _legacy_install(tmp_path)
    _env(monkeypatch, home)
    before = sorted(str(p) for p in home.rglob("*"))
    r = _run()
    assert r.exit_code == 0, r.output
    assert "would move" in r.output and "dry run" in r.output
    assert sorted(str(p) for p in home.rglob("*")) == before
    assert not (home / ".config" / "nightwright").exists()


def test_migrate_yes_moves_and_rewrites(tmp_path, monkeypatch):
    home, cfg, data, ledger = _legacy_install(tmp_path)
    _env(monkeypatch, home)
    r = _run("--yes")
    assert r.exit_code == 0, r.output
    ncfg, ndata = home / ".config" / "nightwright", home / ".local" / "share" / "nightwright"
    assert ncfg.is_dir() and ndata.is_dir() and not cfg.exists() and not data.exists()
    text = (ncfg / "config.yaml").read_text()
    assert text.startswith("# my config\n")
    assert f"nw_checkout: {NEW_CO}\n" in text and "wt_checkout" not in text
    assert f"data_dir: {ndata}\n" in text
    assert "repo_path: /home/u/work/wt-other   # not the checkout" in text
    assert "scheme: nw-native" in text and "/home/u/work/.nw-worktrees" in text
    assert (ncfg / "config.yaml.bak.1").read_text() == f"wt_checkout: {OLD_CO}\n"  # backups kept
    st = json.loads((ndata / "agentic" / "campaigns" / "C1" / "state.json").read_text())
    assert st["repo"] == {"root": NEW_CO, "branch": "wt/C1",
                          "worktree": "/home/u/work/.nw-worktrees/Meta-Tools/C1"}
    assert st["last_gate"]["results"][0]["cwd"] == "/home/u/work/.nw-worktrees/Meta-Tools/C1"
    assert st["last_gate"]["results"][0]["tail"] == f"failed in {OLD_CO}/src"   # free text kept
    assert st["other"]["root"] == "/home/u/work/wt-rename"                       # boundary
    assert st["schema"] == "wt.campaign.v1"                                     # format kept
    assert (ndata / "agentic" / "campaigns" / "C1" / "ledger.jsonl").read_text() == ledger
    res = json.loads((ndata / "spec-reservations.json").read_text())["reservations"]
    assert [x["key"] for x in res] == [f"{NEW_CO}/.git::docs/specs",
                                      "/home/u/work/other/.git::docs/specs"]
    again = _run("--yes")
    assert again.exit_code == 0 and "nothing to migrate" in again.output   # idempotent


def test_migrate_refuses_nonempty_target(tmp_path, monkeypatch):
    home, cfg, data, _ = _legacy_install(tmp_path)
    _env(monkeypatch, home)
    target = home / ".local" / "share" / "nightwright"
    target.mkdir(parents=True)
    (target / "history.jsonl").write_text("{}\n")
    r = _run("--yes")
    assert r.exit_code != 0 and "refused, nothing changed" in r.output
    assert cfg.is_dir() and data.is_dir() and not (home / ".config" / "nightwright").exists()


def test_migrate_empty_target_is_replaced(tmp_path, monkeypatch):
    home, cfg, data, _ = _legacy_install(tmp_path)
    _env(monkeypatch, home)
    (home / ".config" / "nightwright").mkdir(parents=True)
    r = _run("--yes")
    assert r.exit_code == 0, r.output
    assert (home / ".config" / "nightwright" / "config.yaml").is_file()


def test_migrate_never_touches_dot_claude(tmp_path, monkeypatch):
    home, cfg, data, _ = _legacy_install(tmp_path)
    claude = home / ".claude" / "skills"
    claude.mkdir(parents=True)
    (claude / "wt-orient").symlink_to(Path(OLD_CO) / "skills" / "wt-orient")
    settings = home / ".claude" / "settings.json"
    settings.write_text('{"permissions": {"allow": ["Bash(wt queue *)"]}}')
    _env(monkeypatch, home)
    assert _run("--yes").exit_code == 0
    assert (claude / "wt-orient").is_symlink()
    assert settings.read_text() == '{"permissions": {"allow": ["Bash(wt queue *)"]}}'


def test_migrate_json_plan(tmp_path, monkeypatch):
    home, *_ = _legacy_install(tmp_path)
    _env(monkeypatch, home)
    r = _run("--json")
    doc = json.loads(r.output)
    assert doc["schema"] == "nw.migrate_from_wt.v1" and doc["applied"] is False
    assert len(doc["moves"]) == 2 and any(x["file"].endswith("state.json") for x in doc["rewrites"])
