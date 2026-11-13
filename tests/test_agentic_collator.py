"""SPEC-0555: the findings collator — the pure guards (`collator.collate`), the annotation the
triage reads, and the queue path with a FakeRunner: only in-scope findings reach the
implementer, out-of-scope ones land in the campaign's grouped follow-up idea during the loop
(once, not again at close), a collator failure falls back to today's routing, and with the flag
off (or on a campaign pinned before the flag existed) nothing changes."""
import json

import pytest

from test_agentic_e2e import added, impl_done, make_cfg, make_repo, review, script, _PerPersona
from nightwright import explore as EX
from nightwright import org as ORG
from nightwright.agentic import campaign_export as CE
from nightwright.agentic import collator as CO
from nightwright.agentic import packet as PK
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S

CONF, ADV = "conformance_reviewer", "adversarial_reviewer"
MARSHAL = "review_marshal"


# ---- pure: guards ----------------------------------------------------------------------------

def item(i, sev="major", title=None, files=(), ac=None, persona=ADV, **kw):
    return {"id": f"{persona}#{i}", "persona": persona, "severity": sev,
            "title": title or f"finding {i}", "detail": f"detail {i}", "files": list(files),
            "ac": ac, "category": None, **kw}


def rec(i, scope="out", tie="none", sev="major", merged=(), persona=ADV, reason="r"):
    return {"id": f"{persona}#{i}", "merged": list(merged), "tie": tie, "scope": scope,
            "reason": reason, "severity": sev}


def collate(recs, items, **kw):
    kw.setdefault("changed", {"src/new.py"})
    kw.setdefault("ac_ids", {"AC1", "AC2"})
    return CO.collate({"collated": recs}, items, **kw)


pytestmark = pytest.mark.fast


def test_out_of_scope_kept_when_no_guard_applies():
    out, stats = collate([rec(0)], [item(0, files=["src/old.py"])])
    assert [(r["id"], r["scope"], r["tie"]) for r in out] == [(f"{ADV}#0", "out", "none")]
    assert stats == {"dropped": 0, "severity_reverted": 0, "guarded": 0, "unknown": 0}


@pytest.mark.parametrize("it,why", [
    (item(0, files=["src/new.py"]), "cites a file this campaign changed"),
    (item(0, files=["./src/new.py:12-14"]), "cites a file this campaign changed"),
    (item(0, sev="must_fix", files=["src/old.py"]), "must_fix"),
    (item(0, files=["src/old.py"], ac="the parser handles empty input"), "on an unmet AC"),
])
def test_guards_force_in(it, why):
    out, stats = collate([rec(0, sev=it["severity"])], [it],
                         unmet=["The parser handles empty input"])
    assert out[0]["scope"] == "in" and out[0]["guard"] == why and stats["guarded"] == 1


def test_basis_outside_delta_major_is_never_scoped_out():
    """SPEC-0596: a major the delta rule kept (no tie, no changed file) stays in scope even
    when the collator says `out`; `inputs` carries the marker from the stored verdict."""
    verdicts = {ADV: {"findings": [{"severity": "major", "title": "MAC reflection",
                                    "files": ["src/old.py"], "basis_outside_delta": True}]}}
    [it] = CO.inputs(verdicts)
    assert it["basis_outside_delta"] is True
    out, stats = collate([rec(0)], [it])
    assert out[0]["scope"] == "in" and out[0]["guard"] == "basis_outside_delta"
    assert stats["guarded"] == 1


def test_a_tie_to_an_ac_decision_or_design_is_in_scope():
    items = [item(0), item(1), item(2)]
    out, _ = collate([rec(0, tie="AC2"), rec(1, tie="design"), rec(2, tie="diff:src/new.py")],
                     items)
    assert [r["scope"] for r in out] == ["in", "in", "in"]
    assert [r["guard"] for r in out] == ["tied to AC2", "tied to design", "tied to diff:src/new.py"]


def test_malformed_ties_become_none():
    out, _ = collate([rec(0, tie="AC9"), rec(1, tie="diff:src/old.py"), rec(2, tie="whatever")],
                     [item(0), item(1), item(2)])
    assert [r["tie"] for r in out] == ["none", "none", "none"]
    assert all(r["scope"] == "out" for r in out)


def test_dropped_finding_restored_in_and_every_input_once():
    items = [item(0), item(1, ac="AC1"), item(2)]
    out, stats = collate([rec(0, scope="in", tie="AC1", merged=[f"{ADV}#2", f"{ADV}#0"])], items)
    ids = [i for r in out for i in [r["id"], *r["merged"]]]
    assert sorted(ids) == sorted(i["id"] for i in items) and len(ids) == len(set(ids))
    restored = out[-1]
    assert (restored["id"], restored["scope"], restored["reason"], restored["tie"]) == (
        f"{ADV}#1", "in", "collator_dropped", "AC1")
    assert stats["dropped"] == 1


def test_severity_change_is_reverted_and_merge_takes_the_highest_input():
    items = [item(0, sev="minor"), item(1, sev="major", persona=CONF)]
    out, stats = collate([rec(0, scope="in", tie="AC1", sev="nit", merged=[f"{CONF}#1"])], items)
    [r] = out
    assert r["severity"] == "major" and r["severity_reverted"] == "nit"
    assert r["persona"] == [ADV, CONF] and stats["severity_reverted"] == 1


def test_text_fields_are_the_reviewers_verbatim():
    it = item(0, title="Exact  Title, kept", files=["src/old.py"])
    v = {"collated": [dict(rec(0), title="Reworded", detail="summary", files=["x"])]}
    [r] = CO.collate(v, [it], changed=set(), ac_ids=set())[0]
    assert (r["title"], r["detail"], r["files"]) == ("Exact  Title, kept", "detail 0",
                                                     ["src/old.py"])


def test_unknown_ids_and_duplicates_are_ignored_and_bad_verdict_raises():
    out, stats = collate([rec(7), rec(0, scope="in", tie="AC1"), rec(0)], [item(0)])
    assert len(out) == 1 and out[0]["scope"] == "in" and stats["unknown"] == 2
    with pytest.raises(ValueError):
        CO.collate({"nope": []}, [item(0)])
    with pytest.raises(ValueError):           # an empty list for a non-empty input: failed run
        CO.collate({"collated": []}, [item(0)])
    assert CO.collate({"collated": []}, []) == ([], {"dropped": 0, "severity_reverted": 0,
                                                    "guarded": 0, "unknown": 0})


def test_inputs_skip_demoted_and_keep_triage_ids():
    verdicts = {ADV: {"findings": [{"severity": "major", "title": "a"},
                                   {"severity": "minor", "title": "b", "demoted": True},
                                   {"severity": "weird", "title": "c"}]}}
    assert [(i["id"], i["severity"]) for i in CO.inputs(verdicts)] == [
        (f"{ADV}#0", "major"), (f"{ADV}#2", "minor")]


def test_annotate_and_scoped_flag_off_is_identity():
    verdicts = {ADV: {"run": "005-a", "findings": [{"severity": "major", "title": "a"}]}}
    state = {"last_review": {"verdicts": verdicts, "collation": {
        "runs": {ADV: "005-a"}, "status": "done", "scopes": {f"{ADV}#0": ["out", "none"]}}}}
    assert CO.scoped(state, verdicts) is verdicts                    # flag off
    for lean in ({"collator": True}, {"packet": True}):              # dependency unmet / off
        assert CO.scoped(dict(state, lean=lean), verdicts) is verdicts
    on = dict(state, lean={"packet": True, "collator": True})
    got = CO.scoped(on, verdicts)
    assert got[ADV]["findings"][0]["scope"] == "out" and "scope" not in verdicts[ADV]["findings"][0]
    stale = {ADV: dict(verdicts[ADV], run="009-a")}                  # another verdict set
    assert CO.scoped(on, stale) is stale


def test_followup_entries_name_campaign_loop_tie():
    items = [item(0, title="old", files=["src/old.py"]), item(1, persona=CONF, title="old2")]
    recs = [dict(rec(0, merged=[f"{CONF}#1"]), title="old", files=["src/old.py"])]
    es = CO.followup_entries("SPEC-0001", "C1", 2, recs, items)
    assert [e["title"] for e in es] == ["old", "old2"] and {e["origin"] for e in es} == {"out_of_scope"}
    assert es[0]["detail"].startswith("out of scope (C1 loop 2, tie none): r")


# ---- through the queue (FakeRunner) ------------------------------------------------------------

class _Recording(_PerPersona):
    def __init__(self, scripts):
        super().__init__(scripts)
        self.reqs = []

    def run(self, req, *, hooks=None):
        with self.lock:
            self.reqs.append(req)
        return super().run(req, hooks=hooks)


def marshal():
    return {"persona": MARSHAL, "cost_usd": 0.01, "verdict": {"ac_map": []}}


def collator(recs, malformed=False):
    return {"persona": CO.NAME, "malformed": malformed, "cost_usd": 0.02,
            "verdict": {"collated": recs}}


def adv(verdict="pass", findings=(), **kw):
    v = {"verdict": verdict, "score": 92 if verdict == "pass" else 30,
         "findings": list(findings), "fix_brief": "" if verdict == "pass" else
         "fix feature.txt and rename pyproject"}
    v.update(kw)
    return {"persona": ADV, "verdict": v, "cost_usd": 0.3}


IN_BUG = {"severity": "must_fix", "category": "bug", "title": "feature text is wrong",
          "detail": "says hello", "files": ["feature.txt"]}
OUT_OLD = {"severity": "major", "category": "bug", "title": "pyproject name is odd",
           "detail": "the project name predates this campaign", "files": ["pyproject.toml"]}
COLLATE_1 = [{"id": f"{ADV}#0", "merged": [], "tie": "diff:feature.txt", "scope": "in",
              "reason": "defect in changed lines", "severity": "must_fix"},
             {"id": f"{ADV}#1", "merged": [], "tie": "none", "scope": "out",
              "reason": "pyproject.toml is not touched and no AC asks for it",
              "severity": "major"}]


def _setup(tmp_path, lean=True):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []), profile={"reviewers": [CONF, ADV]})
    if lean:
        cfg["agentic"]["lean"] = {"packet": True, "collator": True}
    return repo, cfg


def _drive(cfg, cid, runner, ticks=30, until=None):
    for _ in range(ticks):
        Q.tick(cfg, runner=runner)
        st = S.read_state(cfg, cid)
        if st["state"] in ("MERGED", "PARKED") or (until and until(st)):
            break
    return S.read_state(cfg, cid)


def _ideas(cfg):
    ORG.invalidate_tasks_cache()
    return [t for t in ORG.load_tasks(cfg) if t.is_idea]


def _mark_prior_fixed(runner, cfg, cid):
    orig = runner._next_entry

    def next_entry(req):
        e = orig(req)
        if req.persona == ADV and "delta review" in req.prompt:
            doc = json.loads((S.campaign_dir(cfg, cid) / "packet-loop-2.json").read_text())
            e = json.loads(json.dumps(e))
            e["verdict"]["prior_status"] = [
                {"id": f["id"], "status": "fixed", "evidence": "ok"} for f in doc["findings"]
                if f["persona"] == ADV and f["status"] == "open"]
        return e
    runner._next_entry = next_entry


@pytest.mark.integration
def test_in_scope_reaches_implementer_out_of_scope_becomes_followup(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch)
    runner = _Recording({
        "implementer": [impl_done(), impl_done(content="better\n")],
        MARSHAL: [marshal(), marshal()], CONF: [review(), review()],
        ADV: [adv("fix", [IN_BUG, OUT_OLD]), adv("pass")],
        CO.NAME: [collator(COLLATE_1)]})
    _mark_prior_fixed(runner, cfg, cid)
    # stop as soon as loop 1 routed: the follow-up idea exists before the close
    st = _drive(cfg, cid, runner, until=lambda st: st["counters"].get("loops") == 1)
    assert st["state"] == "IMPLEMENTING" and st["counters"]["loops"] == 1
    [group] = _ideas(cfg)
    body = EX.read_idea_enrichment(cfg, group)["summary"]
    assert group.heading.startswith(f"nwfug:{cid} [SPEC-0001]")
    assert "pyproject name is odd" in body and "feature text is wrong" not in body
    assert f"{cid} loop 1, tie none" in body
    doc = json.loads((S.campaign_dir(cfg, cid) / "findings-loop-1.json").read_text())
    assert doc["schema"] == CO.SCHEMA_ID and doc["status"] == "done"
    assert [(r["id"], r["scope"], r["tie"]) for r in doc["collated"]] == [
        (f"{ADV}#0", "in", "diff:feature.txt"), (f"{ADV}#1", "out", "none")]
    assert [i["title"] for i in doc["inputs"]] == [IN_BUG["title"], OUT_OLD["title"]]
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    impl = [r for r in runner.reqs if r.persona == "implementer"][1].prompt
    assert "feature text is wrong" in impl and "[diff:feature.txt]" in impl
    assert "pyproject name is odd" not in impl and "rename pyproject" not in impl
    # the collator ran after the reviewers and before the implementer, at tier marshal
    order = [e["persona"] for e in S.read_ledger(cfg, cid, types=["run.start"])]
    assert order.index(ADV) < order.index(CO.NAME) < order.index("implementer", 3)
    coll = next(r for r in runner.reqs if r.persona == CO.NAME)
    assert coll.read_only and coll.model == "haiku" and coll.effort == "medium"
    assert f"{ADV}#1" in coll.prompt and "pyproject name is odd" in coll.prompt
    # filed once: the close found its key on the ledger and did not file it again
    [group] = _ideas(cfg)
    assert "(1 finding)" in group.heading
    fus = S.read_ledger(cfg, cid, types=["followup"])
    assert [(e["title"], e["origin"]) for e in fus] == [("pyproject name is odd", "out_of_scope")]
    # loop 2: the scoped-out finding was waived in the packet, so the reviewer did not carry it
    p2 = json.loads((S.campaign_dir(cfg, cid) / "packet-loop-2.json").read_text())
    old = next(f for f in p2["findings"] if f["title"] == OUT_OLD["title"])
    assert old["status"] == "waived" and "out of scope" in old["evidence"]
    outcomes = S.read_ledger(cfg, cid, types=["review_outcome"])
    assert [(o["outcome"], o["out_of_scope"]) for o in outcomes] == [("fix", 1), ("pass", 0)]
    tables = CE.export_tables(cfg)
    camp = tables["campaigns"][0]
    assert (camp["out_of_scope_findings"], camp["collator_fallbacks"]) == (1, 0)
    rows = {r["title"]: r for r in tables["findings"] if r["persona"] == ADV}
    assert (rows[OUT_OLD["title"]]["scope"], rows[OUT_OLD["title"]]["tie"]) == ("out", "none")
    assert rows[IN_BUG["title"]]["scope"] == "in"
    assert CO.NAME in {r["persona"] for r in tables["runs"]}


@pytest.mark.integration
def test_collator_cannot_scope_out_a_changed_file_must_fix(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch)
    bad = [dict(COLLATE_1[0], scope="out", tie="none", severity="minor"), COLLATE_1[1]]
    runner = _Recording({
        "implementer": [impl_done(), impl_done(content="better\n")],
        MARSHAL: [marshal(), marshal()], CONF: [review(), review()],
        ADV: [adv("fix", [IN_BUG, OUT_OLD]), adv("pass")], CO.NAME: [collator(bad)]})
    _mark_prior_fixed(runner, cfg, cid)
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    doc = json.loads((S.campaign_dir(cfg, cid) / "findings-loop-1.json").read_text())
    first = doc["collated"][0]
    assert (first["scope"], first["severity"], first["severity_reverted"]) == (
        "in", "must_fix", "minor")
    assert first["guard"] == "cites a file this campaign changed"
    impl = [r for r in runner.reqs if r.persona == "implementer"][1].prompt
    assert "feature text is wrong" in impl


@pytest.mark.integration
@pytest.mark.parametrize("how", ["malformed", "raises"])
def test_collator_failure_falls_back_to_todays_routing(tmp_path, monkeypatch, how):
    repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch)
    if how == "raises":
        def boom(*a, **k):
            raise RuntimeError("guard bug")
        monkeypatch.setattr(CO, "collate", boom)
    runner = _Recording({
        "implementer": [impl_done(), impl_done(content="better\n")],
        MARSHAL: [marshal(), marshal()], CONF: [review(), review()],
        ADV: [adv("fix", [IN_BUG, OUT_OLD]), adv("pass")],
        CO.NAME: [collator(COLLATE_1, malformed=how == "malformed"), collator([])]})
    _mark_prior_fixed(runner, cfg, cid)
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    fails = S.read_ledger(cfg, cid, types=["collator_failed"])
    assert len(fails) == 1 and fails[0]["loop"] == 1
    impl = [r for r in runner.reqs if r.persona == "implementer"][1].prompt
    assert "feature text is wrong" in impl and "pyproject name is odd" in impl   # all in scope
    doc = json.loads((S.campaign_dir(cfg, cid) / "findings-loop-1.json").read_text())
    assert doc["status"] == "failed" and {r["scope"] for r in doc["collated"]} == {"in"}
    assert runner.calls.count(CO.NAME) == 1                       # never retried, never parked
    assert not S.read_ledger(cfg, cid, types=["park"])
    assert CE.export_tables(cfg)["campaigns"][0]["collator_fallbacks"] == 1


@pytest.mark.integration
def test_no_findings_skips_the_collator_run(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch)
    runner = _Recording({"implementer": [impl_done()], MARSHAL: [marshal()],
                         CONF: [review()], ADV: [adv()]})
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    assert CO.NAME not in runner.calls
    doc = json.loads((S.campaign_dir(cfg, cid) / "findings-loop-1.json").read_text())
    assert doc["status"] == "skipped" and doc["collated"] == []


def _assert_untouched(cfg, cid, runner, st):
    assert CO.NAME not in runner.calls
    assert not list(S.campaign_dir(cfg, cid).glob("findings-loop-*"))
    assert "collation" not in (st.get("last_review") or {})
    assert not S.read_ledger(cfg, cid, types=["findings_collated"])
    assert not S.read_ledger(cfg, cid, types=["collator_failed"])
    for o in S.read_ledger(cfg, cid, types=["review_outcome"]):
        assert "out_of_scope" not in o
    impl = [r for r in runner.reqs if r.persona == "implementer"][1].prompt
    assert "feature text is wrong" in impl and "pyproject name is odd" in impl
    assert "rename pyproject" in impl                             # today's free-text brief
    assert not S.read_ledger(cfg, cid, types=["followup"]) and _ideas(cfg) == []


@pytest.mark.integration
@pytest.mark.parametrize("pinned", [None, {"packet": True}, {"tiers": True, "packet": True}])
def test_flag_off_or_pinned_before_the_flag_routes_as_today(tmp_path, monkeypatch, pinned):
    """No collator with the flag off, nor for a campaign created before the merge (its
    `state.lean` was pinned without `collator`), even when the live config turns it on."""
    repo, cfg = _setup(tmp_path, lean=False)
    if pinned:
        cfg["agentic"]["lean"] = dict(pinned)
    cid = added(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    if pinned:
        st["lean"] = dict(pinned)       # as a pre-merge open wrote it: no `collator` key
        S.write_state(cfg, cid, st)
    cfg["agentic"]["lean"] = {"packet": True, "collator": True}     # flipped after the open
    marshals = [marshal(), marshal()] if pinned else []
    runner = _Recording({
        "implementer": [impl_done(), impl_done(content="better\n")], MARSHAL: marshals,
        CONF: [review(), review()],
        ADV: [adv("fix", [IN_BUG, OUT_OLD]), adv("pass")]})
    orig = runner._next_entry

    def next_entry(req):                 # loop 2 passes in either review mode
        e = orig(req)
        if req.persona == ADV and "delta review" in req.prompt:
            doc = json.loads((S.campaign_dir(cfg, cid) / "packet-loop-2.json").read_text())
            e = json.loads(json.dumps(e))
            e["verdict"]["prior_status"] = [{"id": f["id"], "status": "fixed", "evidence": "x"}
                                            for f in doc["findings"] if f["persona"] == ADV]
        return e
    runner._next_entry = next_entry
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    _assert_untouched(cfg, cid, runner, st)
    camp = CE.export_tables(cfg)["campaigns"][0]
    assert (camp["out_of_scope_findings"], camp["collator_fallbacks"]) == (0, 0)
    assert all(r["scope"] is None and r["tie"] is None for r in CE.export_tables(cfg)["findings"])


def test_flag_off_route_reviews_never_calls_the_collator(monkeypatch):
    """With the flag off the collation helpers are never reached (patched to raise)."""
    def boom(*a, **k):
        raise AssertionError("collator code reached with the flag off")
    for name in ("inputs", "collate", "write", "followup_entries", "diff_files"):
        monkeypatch.setattr(CO, name, boom)
    state = {"state": "REVIEWING", "counters": {"loops": 0},
             "last_review": {"verdicts": {CONF: {"verdict": "pass", "findings": [], "loop": 0,
                                                 "run": "003-x"}}}}
    profile = {"reviewers": [CONF], "loop_max": 3, "review": {}}
    assert Q.plan_step(state, profile).persona != CO.NAME
    assert Q._scoped_verdicts(state, profile) == Q._valid_verdicts(state, profile)
    assert CO.out_findings(state) == [] and not CO.scoped_out(state)
    PK.remember_verdicts(dict(state))            # packet off: no-op
