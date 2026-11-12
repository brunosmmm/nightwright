"""SPEC-0554 through the queue with a FakeRunner: the packet pre-step of every review loop, the
loop-2 delta review (delta diff, own prior findings only, basis enforcement), the fallbacks
(marshal failure -> `ac_map: null`, packet build failure -> today's prompt) and the export
columns. Flag off: no marshal run, no packet file, no new run.start keys."""
import json

import pytest

from test_agentic_e2e import added, impl_done, make_cfg, make_repo, review, script, _PerPersona
from nightwright.agentic import campaign_export as CE
from nightwright.agentic import packet as PK
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S

pytestmark = pytest.mark.integration

CONF, ADV = "conformance_reviewer", "adversarial_reviewer"
MARSHAL = "review_marshal"


class _Recording(_PerPersona):
    def __init__(self, scripts):
        super().__init__(scripts)
        self.reqs = []

    def run(self, req, *, hooks=None):
        with self.lock:
            self.reqs.append(req)
        return super().run(req, hooks=hooks)


def marshal(ac_map=None, malformed=False):
    return {"persona": MARSHAL, "malformed": malformed, "cost_usd": 0.01, "verdict": {
        "ac_map": ac_map if ac_map is not None else [
            {"ac": "AC1", "hunks": ["feature.txt:1"], "tests": [], "coverage": "partial"}]}}


def adv(verdict="pass", findings=(), **kw):
    v = {"verdict": verdict, "score": 92 if verdict == "pass" else 30,
         "findings": list(findings), "fix_brief": "" if verdict == "pass" else "fix it"}
    v.update(kw)
    return {"persona": ADV, "verdict": v, "cost_usd": 0.3}


def _setup(tmp_path, *, packet=True):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []), profile={"reviewers": [CONF, ADV]})
    if packet:
        cfg["agentic"]["lean"] = {"packet": True}
    return repo, cfg


def _drive(cfg, cid, runner, ticks=30):
    for _ in range(ticks):
        Q.tick(cfg, runner=runner)
        if S.read_state(cfg, cid)["state"] in ("MERGED", "PARKED"):
            break
    return S.read_state(cfg, cid)


ADV_BUG = {"severity": "must_fix", "category": "bug", "title": "feature text is wrong",
           "detail": "says hello", "files": ["feature.txt"]}
ADV_OLD_CODE = {"severity": "major", "category": "bug", "title": "pyproject name is odd",
                "files": ["pyproject.toml"]}


def test_two_loops_packet_per_loop_and_delta_review(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch)
    # untouched code, no valid basis; a minor, so the delta rule demotes it (SPEC-0596 keeps a
    # major blocking: see test_outside_delta_major_stays_blocking)
    loop2_new = dict(ADV_OLD_CODE, severity="minor", basis="initial")
    runner = _Recording({
        "implementer": [impl_done(), impl_done(content="better\n")],
        MARSHAL: [marshal(), marshal()],
        CONF: [review(), review()],
        ADV: [adv("fix", [ADV_BUG]),
              adv("fix", [loop2_new], prior_status=[])]})
    # loop 2: the adversarial reviewer marks its loop-1 finding fixed (filled in below)
    st = S.read_state(cfg, cid)

    orig = runner._next_entry

    def next_entry(req):
        e = orig(req)
        if req.persona == ADV and "delta review" in req.prompt:
            doc = json.loads((S.campaign_dir(cfg, cid) / "packet-loop-2.json").read_text())
            fid = next(f["id"] for f in doc["findings"] if f["persona"] == ADV)
            e = json.loads(json.dumps(e))
            e["verdict"]["prior_status"] = [{"id": fid, "status": "fixed",
                                             "evidence": "feature.txt now says better"}]
        return e
    runner._next_entry = next_entry
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    cdir = S.campaign_dir(cfg, cid)
    p1 = json.loads((cdir / "packet-loop-1.json").read_text())
    p2 = json.loads((cdir / "packet-loop-2.json").read_text())
    assert PK.validate(p1) == [] and PK.validate(p2) == []
    assert p1["mode"] == "full" and p1["delta"] is None and p1["findings"] == []
    assert p1["acs"] == [{"id": "AC1", "text": "feature.txt exists"}]
    assert p1["ac_map"] == [{"ac": "AC1", "hunks": ["feature.txt:1"], "tests": [],
                             "coverage": "partial"}]
    assert {s["path"] for s in p1["diff"]["stat"]} >= {"feature.txt"}
    assert (cdir / p1["diff"]["path"]).read_text().startswith("diff --git")
    assert p2["mode"] == "delta" and p2["prev_head"] == p1["head"]
    assert [s["path"] for s in p2["delta"]["stat"]] == ["feature.txt"]
    assert "+better" in (cdir / p2["delta"]["path"]).read_text()
    [prior] = p2["findings"]
    assert (prior["persona"], prior["status"], prior["loop"]) == (ADV, "open", 1)
    assert st["review_heads"] == {"1": p1["head"], "2": p2["head"]}
    # reviewer prompts: loop 1 full, loop 2 delta with the delta and only its own findings
    advs = [r for r in runner.reqs if r.persona == ADV]
    confs = [r for r in runner.reqs if r.persona == CONF]
    assert "review mode: `full`" in advs[0].prompt and "packet-loop-1.json" in advs[0].prompt
    assert "review mode: `delta`" in advs[1].prompt and "+better" in advs[1].prompt
    assert "feature text is wrong" in advs[1].prompt
    assert "feature text is wrong" not in confs[1].prompt.split("Your open findings")[1]
    assert "prior_status" in advs[1].json_schema["properties"]
    # the marshal ran before the reviewers of each loop, at the head of the loop
    order = [e["persona"] for e in S.read_ledger(cfg, cid, types=["run.start"])]
    assert order.index(MARSHAL) < order.index(CONF)
    # the unsupported loop-2 finding was demoted (never blocked); the fixed prior stopped counting
    outcomes = S.read_ledger(cfg, cid, types=["review_outcome"])
    assert [(o["loop"], o["outcome"], o["demoted"]) for o in outcomes] == [
        (0, "fix", 0), (1, "pass", 1)]
    assert any(f["title"] == "pyproject name is odd" for f in st["last_review"]["advisory"])
    runs = CE.export_tables(cfg)["runs"]
    by = {(r["persona"], r["loop"]): r for r in runs}
    assert by[(MARSHAL, 1)]["packet"] is True and by[(ADV, 2)]["packet"] is True
    assert by[("implementer", 1)]["packet"] is False
    assert CE.export_tables(cfg)["campaigns"][0]["demoted_findings"] == 1
    assert CE.export_tables(cfg)["campaigns"][0]["outside_delta_findings"] == 0
    assert [o["outside_delta"] for o in outcomes] == [0, 0]


def test_outside_delta_major_stays_blocking(tmp_path, monkeypatch):
    """SPEC-0596: a loop-2 `major` on untouched code with no valid basis is not demoted: it
    stays blocking (another fix loop), is marked `basis_outside_delta`, counted as
    `outside_delta` in `review_outcome` and exported as `outside_delta_findings`."""
    repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch)
    # raised by the conformance reviewer with a `fix` verdict, so the SPEC-0468 adversarial-major
    # relief (conformance passed) does not apply and only the delta rule decides
    loop2_new = {"severity": "major", "title": "pyproject name is odd",
                 "files": ["pyproject.toml"], "basis": "initial",
                 "suggested_fix": "rename the project"}
    met = [{"ac": "feature.txt exists", "met": True, "evidence": "feature.txt"}]
    runner = _Recording({
        "implementer": [impl_done(), impl_done(content="better\n"), impl_done(content="c\n")],
        MARSHAL: [marshal(), marshal(), marshal()],
        CONF: [review(), review("fix", findings=[loop2_new], fix_brief="rename it",
                                         ac_assessment=met), review()],
        ADV: [adv("fix", [ADV_BUG]), adv("pass"), adv("pass")]})
    orig = runner._next_entry
    seen = []

    def next_entry(req):
        e = orig(req)
        if req.persona in (ADV, CONF) and "delta review" in req.prompt:
            seen.append((req.persona, req.prompt))
            n = 2 + sum(1 for p, _ in seen[:-1] if p == req.persona)
            doc = json.loads((S.campaign_dir(cfg, cid) / f"packet-loop-{n}.json").read_text())
            e = json.loads(json.dumps(e))
            e["verdict"]["prior_status"] = [
                {"id": x["id"], "status": "fixed", "evidence": "ok"}
                for x in doc["findings"] if x["persona"] == req.persona and x["status"] == "open"
                and (req.persona == ADV or n == 3)]
        return e
    runner._next_entry = next_entry
    st = _drive(cfg, cid, runner, ticks=45)
    assert st["state"] == "MERGED", st.get("park")
    outcomes = S.read_ledger(cfg, cid, types=["review_outcome"])
    assert [(o["outcome"], o["demoted"], o["outside_delta"]) for o in outcomes] == [
        ("fix", 0, 0), ("fix", 0, 1), ("pass", 0, 0)]
    # loop 3's packet carries it as an open, non-advisory finding (it was blocking)
    p3 = json.loads((S.campaign_dir(cfg, cid) / "packet-loop-3.json").read_text())
    odd = next(x for x in p3["findings"] if x["title"] == "pyproject name is odd")
    assert odd["status"] == "open" and not odd.get("advisory")
    conf_prompts = [pr for p, pr in seen if p == CONF]
    assert "pyproject name is odd" in conf_prompts[1].split("Your open findings")[1]
    camp = CE.export_tables(cfg)["campaigns"][0]
    assert (camp["demoted_findings"], camp["outside_delta_findings"]) == (0, 1)


def test_open_prior_finding_still_blocks(tmp_path, monkeypatch):
    """A prior finding the reviewer omits stays open and keeps the loop going."""
    repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch)
    runner = _Recording({
        "implementer": [impl_done(), impl_done(content="b\n"), impl_done(content="c\n")],
        MARSHAL: [marshal(), marshal(), marshal()],
        CONF: [review(), review(), review()],
        ADV: [adv("fix", [ADV_BUG]), adv("pass"), adv("pass", prior_status=[])]})
    orig = runner._next_entry
    seen = []

    def next_entry(req):
        e = orig(req)
        if req.persona == ADV and "delta review" in req.prompt:
            seen.append(req.prompt)
            if len(seen) == 2:      # loop 3: mark it fixed
                doc = json.loads((S.campaign_dir(cfg, cid) / "packet-loop-3.json").read_text())
                fid = next(f["id"] for f in doc["findings"] if f["persona"] == ADV)
                e = json.loads(json.dumps(e))
                e["verdict"]["prior_status"] = [{"id": fid, "status": "fixed", "evidence": "ok"}]
        return e
    runner._next_entry = next_entry
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    outcomes = [o["outcome"] for o in S.read_ledger(cfg, cid, types=["review_outcome"])]
    assert outcomes == ["fix", "fix", "pass"]
    assert "feature text is wrong" in seen[1]                  # still listed in loop 3


def test_marshal_failure_leaves_null_map_and_reviews_run(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch)
    runner = _Recording({"implementer": [impl_done()], MARSHAL: [marshal(malformed=True)],
                         CONF: [review()], ADV: [adv()]})
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    doc = json.loads((S.campaign_dir(cfg, cid) / "packet-loop-1.json").read_text())
    assert doc["ac_map"] is None and "malformed" in doc["ac_map_error"]
    assert PK.validate(doc) == []
    evs = S.read_ledger(cfg, cid, types=["review_packet"])
    assert evs[-1]["ac_map"] is False
    assert "packet-loop-1.json" in next(r for r in runner.reqs if r.persona == CONF).prompt


def test_packet_build_failure_falls_back_to_todays_prompt(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch)

    def boom(cfg, state):
        raise RuntimeError("no packet today")
    monkeypatch.setattr(PK, "build", boom)
    runner = _Recording({"implementer": [impl_done()], CONF: [review()], ADV: [adv()]})
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    assert st["packet"]["ok"] is False and "no packet today" in st["packet"]["error"]
    assert MARSHAL not in runner.calls
    conf = next(r for r in runner.reqs if r.persona == CONF)
    assert "Review packet" not in conf.prompt and "prior_status" not in conf.json_schema["properties"]
    runs = {r["persona"]: r for r in CE.export_tables(cfg)["runs"]}
    assert runs[CONF]["packet"] is False and runs[CONF]["loop"] == 1


def test_flag_off_no_marshal_no_packet(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, packet=False)
    cid = added(cfg, monkeypatch)
    runner = _Recording({"implementer": [impl_done()], CONF: [review()], ADV: [adv()]})
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    assert MARSHAL not in runner.calls
    assert not list(S.campaign_dir(cfg, cid).glob("packet-loop-*"))
    assert not any(k in st for k in ("packet", "packet_prev", "review_heads"))
    for e in S.read_ledger(cfg, cid, types=["run.start"]):
        assert "loop" not in e and "packet" not in e
    assert not S.read_ledger(cfg, cid, types=["review_packet"])
    assert "demoted" not in S.read_ledger(cfg, cid, types=["review_outcome"])[0]
    assert "outside_delta" not in S.read_ledger(cfg, cid, types=["review_outcome"])[0]
    camp = CE.export_tables(cfg)["campaigns"][0]
    assert camp["demoted_findings"] == 0 and camp["outside_delta_findings"] == 0
