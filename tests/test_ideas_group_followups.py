"""SPEC-0239: `nw ideas group-followups` — the one-off migration of the SPEC-0207 single
`nwfu:` ideas into one grouped idea per spec."""
import json
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import explore as EX
from nightwright import org as ORG
from nightwright import org_write as OW
from nightwright.agentic import followups as FU
from nightwright.cli import cli

pytestmark = pytest.mark.fast

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
    monkeypatch.setattr("nightwright.cli.load_config", lambda: c)
    return c


def single(cfg, key, spec, title, **kw):
    _, _, idea_id = OW.add_idea(cfg, f"nwfu:{key} [{spec}] test_reviewer/AC 1: {title} - detail",
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
    return [t for t in tasks(cfg).values() if "nwfug:" in t.heading and not t.is_done]


def test_dry_run_text_and_json_write_nothing(cfg):
    ids, closed = seed(cfg)
    before = open(cfg["org_ideas_file"]).read()
    r = run()
    assert f"{S1}  3 singles (IDEA-001, IDEA-002, IDEA-003)" in r.output and "new" in r.output
    assert f"{S2}  1 singles (IDEA-004)" in r.output and "re-run with --yes" in r.output
    payload = as_json(run("--json").output)
    assert payload["schema"] == "nw.ideas.group_followups.v1" and payload["written"] is False
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
    assert g1.heading == f"nwfug:legacy-{S1} [{S1}] review follow-ups (2 findings)"
    assert g1.project == "Meta-Tools" and g1.epic == "SPEC-0169"   # lowest-numbered single's
    assert g1.properties["KIND"] == "improvement"
    assert g2.heading.endswith("(1 finding)") and g2.project != "Meta-Tools" and g2.epic is None   # none inherited
    lines = EX.read_idea_enrichment(cfg, g1)["summary"].splitlines()
    assert lines == [f"- [ ] nwfu:{K1} test_reviewer/AC 1: first - detail (was IDEA-001)",
                     f"- [ ] nwfu:{K2} test_reviewer/AC 1: second - detail (was IDEA-002)"]
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
    EX.set_summary(cfg, other, f"- [ ] nwfu:{K2} minor x: y")
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
    EX.set_summary(cfg, other, f"- [ ] nwfu:{K1} minor x: y")
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
    EX.set_summary(cfg, closed, f"- [ ] nwfu:{K1} minor x: y (was IDEA-900)")
    EX.close_idea(cfg, closed, "drop", reason="done")
    if archive:
        OW.archive_idea(cfg, tasks(cfg)[closed])
    single(cfg, K1, S1, "again")                              # the old loop filed another single
    plan = as_json(run("--json").output)
    assert plan["groups"][0]["target"] == "new" and len(plan["groups"][0]["lines"]) == 1
    run("--yes")
    [fresh] = groups(cfg)
    assert fresh.properties["ID"] != closed and f"nwfug:legacy-{S1}" in fresh.heading
    assert FU.summary_keys(EX.read_idea_enrichment(cfg, fresh)["summary"]) == [K1]
    t = [x for x in tasks(cfg).values() if x.heading.startswith("nwfu:")]
    assert [x.properties["FOLDED_INTO"] for x in t] == [fresh.properties["ID"]]
    assert as_json(run("--yes", "--json").output)["groups"] == []


def test_dry_run_names_an_existing_open_legacy_group_as_the_target(cfg):
    _, _, legacy = OW.add_idea(cfg, FU.group_title(f"legacy-{S1}", S1, 1), kind="improvement")
    EX.set_summary(cfg, legacy, f"- [ ] nwfu:{K2} minor x: y")
    single(cfg, K1, S1, "new one")
    g = as_json(run("--json").output)["groups"][0]
    assert g["target"] == legacy and len(g["lines"]) == 1
    assert "group_id" not in g


# ---- SPEC-0555: collation-time filing of out-of-scope findings ---------------------------------

def _oos_campaign(cfg):
    from nightwright.agentic import store as S
    cid = "SPEC-0555-20261112-test"
    S.campaign_dir(cfg, cid).mkdir(parents=True)
    return {"id": cid, "spec_id": "SPEC-0555", "project": "Meta-Tools", "spec": {}, "repo": {},
            "last_review": {}, "review_carry": []}


def _oos(title, loop=1, sev="major", files=("src/old.py",)):
    from nightwright.agentic import collator as CO
    item = {"id": "adversarial_reviewer#0", "persona": "adversarial_reviewer", "severity": sev,
            "title": title, "detail": "d", "files": list(files), "ac": None, "category": "bug"}
    rec = {"id": item["id"], "merged": [], "scope": "out", "tie": "none", "reason": "untouched"}
    return CO.followup_entries("SPEC-0555", "SPEC-0555-20261112-test", loop, [rec], [item])


def _append(cfg, cid, events):
    from nightwright.agentic import store as S
    for etype, payload in events:
        S.append_event(cfg, cid, etype, payload)


def test_out_of_scope_filed_into_the_group_once_across_loops_and_close(cfg):
    from nightwright.agentic import store as S
    st = _oos_campaign(cfg)
    prof = {"review": {"followups": True, "followups_max": 10}}
    ev1, s1 = FU.file_out_of_scope(cfg, st, prof, _oos("old parser"))
    _append(cfg, st["id"], ev1)
    [g] = groups(cfg)
    assert g.heading == f"nwfug:{st['id']} [SPEC-0555] review follow-ups (1 finding)"
    body = EX.read_idea_enrichment(cfg, g)["summary"]
    assert "old parser" in body and f"{st['id']} loop 1, tie none" in body
    assert [e[1]["origin"] for e in ev1 if e[0] == "followup"] == ["out_of_scope"]
    # loop 2: the same finding again plus a new one -> only the new one gets a line
    ev2, s2 = FU.file_out_of_scope(cfg, st, prof, _oos("old parser", loop=2)
                                   + _oos("old cache", loop=2))
    _append(cfg, st["id"], ev2)
    [g] = groups(cfg)
    assert "(2 findings)" in g.heading
    assert [f["title"] for f in s2["filed"]] == ["old parser", "old cache"]
    assert [e[1]["title"] for e in ev2 if e[0] == "followup"] == ["old cache"]
    # close: the same key as an advisory finding is skipped (its followup event is on the ledger)
    st["last_review"] = {"advisory": [{"severity": "major", "title": "old parser",
                                       "files": ["src/old.py"], "personas": ["test_reviewer"]}]}
    ev3, s3 = FU.file_followups(cfg, st, prof)
    assert [e for e in ev3 if e[0] == "followup"] == [] and "(2 findings)" in groups(cfg)[0].heading
    assert len(S.read_ledger(cfg, st["id"], types=["followup"])) == 2


def test_out_of_scope_respects_followups_off_and_never_raises(cfg, monkeypatch):
    st = _oos_campaign(cfg)
    ev, s = FU.file_out_of_scope(cfg, st, {"review": {"followups": False}}, _oos("x"))
    assert ev == [] and s["filed"] == [] and groups(cfg) == []
    monkeypatch.setattr(OW, "add_idea", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    ev, s = FU.file_out_of_scope(cfg, st, {"review": {}}, _oos("y"))
    assert s["unfiled"] and any("not filed" in e[1]["text"] for e in ev if e[0] == "note")
