"""SPEC-0239: `wt ideas group-followups` — the one-off migration of the SPEC-0207 single
`wtfu:` ideas into one grouped idea per spec."""
import json
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from wt import explore as EX
from wt import org as ORG
from wt import org_write as OW
from wt.agentic import followups as FU
from wt.cli import cli

K1, K2, K3, K4, K5 = ("1" * 12, "2" * 12, "3" * 12, "4" * 12, "5" * 12)
S1, S2 = "SPEC-0207", "SPEC-0100"
CLOSED_MSG = "merged into grouped review follow-ups"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    org = tmp_path / "org"
    org.mkdir()
    c = {
        "org_files": [str(org)], "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "timezone": "America/New_York", "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "cfg"),
        "org_capture_file": str(org / "inbox.org"), "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"), "agentic": {},
    }
    monkeypatch.setattr("wt.cli.load_config", lambda: c)
    return c


def single(cfg, key, spec, title, **kw):
    _, _, idea_id = OW.add_idea(cfg, f"wtfu:{key} [{spec}] test_reviewer/AC 1: {title} - detail",
                                kind="improvement", **kw)
    return idea_id


def seed(cfg):
    """IDEA-001..: S1 has three singles (001 and 003 share K1; 001 has project+epic), S2 one,
    and one closed S1 single that must stay untouched."""
    ids = [single(cfg, K1, S1, "first", project="Meta-Tools", epic="SPEC-0169"),
           single(cfg, K2, S1, "second"), single(cfg, K1, S1, "first again"),
           single(cfg, K3, S2, "other spec")]
    closed = single(cfg, K4, S1, "already closed")
    EX.close_idea(cfg, closed, "drop", reason="not now")
    return ids, closed


def tasks(cfg):
    ORG.invalidate_tasks_cache()
    return {t.properties["ID"]: t for t in ORG.load_tasks(cfg) if t.is_idea}


def run(*args):
    r = CliRunner().invoke(cli, ["ideas", "group-followups", *args])
    assert r.exit_code == 0, r.output
    return r


def as_json(out):
    return json.loads(out[out.index("{"):])               # skip warnings echoed before it


def groups(cfg):
    return [t for t in tasks(cfg).values() if "wtfug:" in t.heading and not t.is_done]


def test_dry_run_text_and_json_write_nothing(cfg):
    ids, closed = seed(cfg)
    before = open(cfg["org_ideas_file"]).read()
    r = run()
    assert f"{S1}  3 singles (IDEA-001, IDEA-002, IDEA-003)" in r.output and "new" in r.output
    assert f"{S2}  1 singles (IDEA-004)" in r.output and "re-run with --yes" in r.output
    payload = as_json(run("--json").output)
    assert payload["schema"] == "wt.ideas.group_followups.v1" and payload["written"] is False
    assert [(g["spec_id"], g["singles"], g["target"], len(g["lines"]))
            for g in payload["groups"]] == [
        (S2, ["IDEA-004"], "new", 1), (S1, ["IDEA-001", "IDEA-002", "IDEA-003"], "new", 2)]
    assert open(cfg["org_ideas_file"]).read() == before


def test_yes_creates_one_group_per_spec_and_folds_the_singles(cfg):
    ids, closed = seed(cfg)
    payload = as_json(run("--yes", "--json").output)
    assert payload["written"] is True
    t = tasks(cfg)
    g1 = [x for x in groups(cfg) if S1 in x.heading][0]
    g2 = [x for x in groups(cfg) if S2 in x.heading][0]
    assert g1.heading == f"wtfug:legacy-{S1} [{S1}] review follow-ups (2 findings)"
    assert g1.project == "Meta-Tools" and g1.epic == "SPEC-0169"   # lowest-numbered single's
    assert g1.properties["KIND"] == "improvement"
    assert g2.heading.endswith("(1 finding)") and g2.project != "Meta-Tools" and g2.epic is None   # none inherited
    lines = EX.read_idea_enrichment(cfg, g1)["summary"].splitlines()
    assert lines == [f"- [ ] wtfu:{K1} test_reviewer/AC 1: first - detail (was IDEA-001)",
                     f"- [ ] wtfu:{K2} test_reviewer/AC 1: second - detail (was IDEA-002)"]
    for sid in ("IDEA-001", "IDEA-002", "IDEA-003"):
        s = t[sid]
        assert s.state == "DROPPED" and s.properties["CLOSE_REASON"] == CLOSED_MSG
        assert s.properties["FOLDED_INTO"] == g1.properties["ID"]
    assert t["IDEA-004"].properties["FOLDED_INTO"] == g2.properties["ID"]
    assert t[closed].properties["CLOSE_REASON"] == "not now" and "FOLDED_INTO" not in t[closed].properties
    assert {g["group_id"] for g in payload["groups"]} == {g1.properties["ID"], g2.properties["ID"]}


def test_a_single_already_in_an_open_campaign_group_gets_no_line(cfg):
    ids, _ = seed(cfg)
    _, _, other = OW.add_idea(cfg, FU.group_title("SPEC-0207-20261025-bbbb", S1, 1),
                              kind="improvement")
    EX.set_summary(cfg, other, f"- [ ] wtfu:{K2} minor x: y")
    run("--yes")
    t = tasks(cfg)
    legacy = [x for x in groups(cfg) if "legacy-" in x.heading and S1 in x.heading][0]
    assert FU.summary_keys(EX.read_idea_enrichment(cfg, legacy)["summary"]) == [K1]
    assert t["IDEA-002"].properties["FOLDED_INTO"] == other
    assert t["IDEA-001"].properties["FOLDED_INTO"] == legacy.properties["ID"]
    assert "(1 finding)" in legacy.heading


def test_everything_listed_elsewhere_creates_no_legacy_group(cfg):
    single(cfg, K1, S1, "only")
    _, _, other = OW.add_idea(cfg, FU.group_title("SPEC-0207-20261025-bbbb", S1, 1),
                              kind="improvement")
    EX.set_summary(cfg, other, f"- [ ] wtfu:{K1} minor x: y")
    run("--yes")
    assert [x.properties["ID"] for x in groups(cfg)] == [other]
    assert tasks(cfg)["IDEA-001"].properties["FOLDED_INTO"] == other


def test_second_yes_run_is_a_no_op(cfg):
    seed(cfg)
    run("--yes")
    before = open(cfg["org_ideas_file"]).read()
    r = run("--yes")
    assert "no open single" in r.output and open(cfg["org_ideas_file"]).read() == before
    assert as_json(run("--yes", "--json").output)["groups"] == []


def test_a_crash_between_the_group_and_the_closes_is_adopted(cfg, monkeypatch):
    seed(cfg)
    real = EX.close_idea
    calls = []

    def flaky(*a, **k):
        calls.append(a[1])
        if len(calls) == 2:
            raise OSError("boom")
        return real(*a, **k)

    monkeypatch.setattr(EX, "close_idea", flaky)
    with pytest.raises(OSError):
        FU.group_legacy(cfg, apply=True)
    monkeypatch.setattr(EX, "close_idea", real)
    assert len(groups(cfg)) == 2                              # both groups exist, closes unfinished
    assert tasks(cfg)["IDEA-001"].state == "IDEA"
    report = FU.group_legacy(cfg, apply=True)
    t = tasks(cfg)
    assert all(t[i].state == "DROPPED" for i in ("IDEA-001", "IDEA-002", "IDEA-003", "IDEA-004"))
    gs = groups(cfg)
    assert len(gs) == 2 and len({g.heading for g in gs}) == 2   # adopted, not duplicated
    for g in gs:
        keys = FU.summary_keys(EX.read_idea_enrichment(cfg, g)["summary"])
        assert len(keys) == len(set(keys))
        assert f"({len(keys)} finding" in g.heading
    assert t["IDEA-002"].properties["FOLDED_INTO"] == t["IDEA-001"].properties["FOLDED_INTO"]
    assert report["written"] is True


@pytest.mark.parametrize("archive", [False, True])
def test_a_closed_legacy_group_is_not_the_target(cfg, archive):
    old = FU.group_title(f"legacy-{S1}", S1, 1)
    _, _, closed = OW.add_idea(cfg, old, kind="improvement")
    EX.set_summary(cfg, closed, f"- [ ] wtfu:{K1} minor x: y (was IDEA-900)")
    EX.close_idea(cfg, closed, "drop", reason="done")
    if archive:
        OW.archive_idea(cfg, tasks(cfg)[closed])
    single(cfg, K1, S1, "again")                              # the old loop filed another single
    plan = as_json(run("--json").output)
    assert plan["groups"][0]["target"] == "new" and len(plan["groups"][0]["lines"]) == 1
    run("--yes")
    [fresh] = groups(cfg)
    assert fresh.properties["ID"] != closed and f"wtfug:legacy-{S1}" in fresh.heading
    assert FU.summary_keys(EX.read_idea_enrichment(cfg, fresh)["summary"]) == [K1]
    t = [x for x in tasks(cfg).values() if x.heading.startswith("wtfu:")]
    assert [x.properties["FOLDED_INTO"] for x in t] == [fresh.properties["ID"]]
    assert as_json(run("--yes", "--json").output)["groups"] == []


def test_dry_run_names_an_existing_open_legacy_group_as_the_target(cfg):
    _, _, legacy = OW.add_idea(cfg, FU.group_title(f"legacy-{S1}", S1, 1), kind="improvement")
    EX.set_summary(cfg, legacy, f"- [ ] wtfu:{K2} minor x: y")
    single(cfg, K1, S1, "new one")
    g = as_json(run("--json").output)["groups"][0]
    assert g["target"] == legacy and len(g["lines"]) == 1
    assert "group_id" not in g
