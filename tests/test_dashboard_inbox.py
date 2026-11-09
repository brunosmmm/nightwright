"""SPEC-0403: the decisions inbox of `nw.dashboard.v1`, built over the seeded inbox fixture."""
import re
from pathlib import Path

import pytest

from fixtures.dashboard_data import (DEC_QUESTION, DIRTY_ROOT, I_ANS, I_BAD, I_DEC, I_DIRTY,
                                     I_OFF, I_WD, LONG_DETAIL, UNMET_AC, seed_inbox)
from test_dashboard_readonly import tree
from nightwright.agentic import escalations as ESC
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S
from nightwright.agentic.dashboard import inbox as IB
from nightwright.agentic.dashboard import snapshot as SN

pytestmark = pytest.mark.fast

ENTRY_KEYS = {"id", "created", "by", "kind", "question", "question_text", "options",
              "recommendation", "facts", "campaign", "spec_id", "spec_title", "project", "park",
              "answerable", "pending", "why_not", "forms", "findings", "unmet_acs", "effects"}
SKILL = Path(__file__).resolve().parents[1] / "skills" / "nw-conduct" / "SKILL.md"


@pytest.fixture(scope="module")
def fx(tmp_path_factory):
    return seed_inbox(tmp_path_factory.mktemp("inbox"))


@pytest.fixture(scope="module")
def inbox(fx):
    return SN.build(fx["cfg"])["inbox"]


def by_id(inbox):
    return {e["id"]: e for e in inbox["escalations"]}


def test_entries_are_the_open_records_in_created_order_with_every_field(fx, inbox):
    escs = inbox["escalations"]
    assert [e["id"] for e in escs] == ["ESC-0004", "ESC-0001", "ESC-0002", "ESC-0003",
                                       "ESC-0005", "ESC-0006"]
    assert {r["id"] for r in ESC.list_all(fx["cfg"], "open")} == {e["id"] for e in escs}
    for e in escs:
        if e["id"] != "ESC-0005":
            assert set(e) == ENTRY_KEYS, (e["id"], set(e) ^ ENTRY_KEYS)
    e = by_id(inbox)["ESC-0001"]
    assert e["question"] == "Waive the review?" and e["question_text"] == DEC_QUESTION
    assert e["options"] == [{"n": 1, "label": "Waive and resume"}, {"n": 2, "label": "Abandon"}]
    assert e["recommendation"] == 1 and e["facts"] == ["loops: 3", "cost: $4.10"]
    assert (e["campaign"], e["spec_id"], e["project"], e["by"]) == (
        I_DEC, "SPEC-0911", "Meta-Tools", "conductor")
    assert e["park"] == {"reason": "human_decision", "escalated_from": "loop_exhausted",
                         "resume_to": "IMPLEMENTING",
                         "parked_at": S.read_state(fx["cfg"], I_DEC)["park"]["since"],
                         "detail": "ESC-0001: q"}
    c = by_id(inbox)["ESC-0004"]
    assert c["facts"] == ["one fact"] and c["options"] == [] and c["park"] is None
    assert by_id(inbox)["ESC-0002"]["park"] is None            # not PARKED yet


def test_findings_are_answerable_findings_projected_and_cut(fx, inbox):
    e = by_id(inbox)["ESC-0001"]
    st = S.read_state(fx["cfg"], I_DEC)
    raw = ESC.answerable_findings(fx["cfg"], st, "ESC-0001")
    synthetic = ESC._review_findings(fx["cfg"], st)[2]
    assert [f["id"] for f in e["findings"]] == [f["id"] for f in raw] == [
        "conformance_reviewer#0", "conformance_reviewer#1", "conformance_reviewer#ac0"]
    for got, f in zip(e["findings"], raw):
        assert set(got) == set(IB.FINDING_KEYS)
        assert got["synthetic"] is (f["id"] in synthetic)
        for k in ("personas", "severity", "files"):
            assert got[k] == f[k]
    long = e["findings"][1]["detail"]
    assert len(long) == IB.FINDING_TEXT_MAX + 1 and long.endswith("…")
    assert long[:-1] == LONG_DETAIL[:IB.FINDING_TEXT_MAX]
    assert e["findings"][2]["synthetic"] is True and e["findings"][2]["detail"] is None
    assert e["unmet_acs"] == ESC.unmet_acs(fx["cfg"], st, "ESC-0001") == [UNMET_AC]
    for i in ("ESC-0004", "ESC-0006"):
        assert by_id(inbox)[i]["findings"] == by_id(inbox)[i]["unmet_acs"] == []


def test_forms_and_effects(inbox):
    got = {i: e.get("forms") for i, e in by_id(inbox).items()}
    assert got == {"ESC-0001": ["choose", "text", "override"], "ESC-0002": ["choose", "text"],
                   "ESC-0003": ["text"], "ESC-0004": ["text"], "ESC-0005": None,
                   "ESC-0006": ["choose", "text"]}
    for i, e in by_id(inbox).items():
        if e.get("forms") is None:
            continue
        assert list(e["effects"]) == e["forms"]
        if e["kind"] == "worker_lost":
            assert set(e["effects"].values()) == {IB.EFFECTS_WORKER_LOST}
        else:
            assert e["effects"] == {f: IB.EFFECTS[f] for f in e["forms"]}
    # no override without a campaign, or with nothing to waive or attest
    assert IB.forms_of([{"n": 1}], False, [{"id": "x"}], ["ac"]) == ["choose", "text"]
    assert IB.forms_of([], True, [], []) == ["text"]
    assert IB.forms_of([], True, [], ["ac"]) == ["text", "override"]


def test_answerable_pending_and_why_not(inbox):
    got = {i: (e["answerable"], e["pending"], e["why_not"]) for i, e in by_id(inbox).items()}
    assert got == {"ESC-0001": (True, False, None),
                   "ESC-0002": (False, True, "not parked yet"),
                   "ESC-0003": (False, False, "campaign is IMPLEMENTING"),
                   "ESC-0004": (True, False, None),
                   "ESC-0005": (False, False, "campaign state unreadable"),
                   "ESC-0006": (True, False, None)}
    assert IB._status({"state": "RUNNING"}) == (False, False, "campaign is RUNNING")
    assert IB._status(None) == (True, False, None)


def _args(e, form):
    if form == "choose":
        return {"choose": str(e["options"][0]["n"])}
    if form == "text":
        return {"text": "go"}
    if e["findings"]:
        return {"waive": [e["findings"][0]["id"]], "reason": "checked"}
    return {"attest": [e["unmet_acs"][0]], "reason": "checked"}


def test_plan_answer_agrees_with_the_inbox(fx, inbox):
    cfg = fx["cfg"]
    for e in inbox["escalations"]:
        if e["answerable"]:
            for form in e["forms"]:
                plan = ESC.plan_answer(cfg, e["id"], **_args(e, form), by="human")
                assert plan["answer"]["kind"] == form
        else:
            with pytest.raises(ValueError):
                ESC.plan_answer(cfg, e["id"], text="go")
    # every offered finding id and unmet AC text is one `--waive` / `--attest` accepts
    e1 = by_id(inbox)["ESC-0001"]
    for f in e1["findings"]:
        ESC.plan_answer(cfg, "ESC-0001", waive=[f["id"]], reason="r")
    for a in e1["unmet_acs"]:
        ESC.plan_answer(cfg, "ESC-0001", attest=[a], reason="r")
    # the forms left out are refused too: no options, no campaign
    with pytest.raises(ValueError, match="no options"):
        ESC.plan_answer(cfg, "ESC-0004", choose="1")
    with pytest.raises(ValueError, match="no campaign"):
        ESC.plan_answer(cfg, "ESC-0006", attest=["x"], reason="r")


def test_parks_are_needs_you_rows_without_an_open_escalation(fx, inbox):
    dp = Q.dashboard_payload(fx["cfg"])
    rows = {r["campaign"]: r for r in dp["needs_you"]}
    parks = {p["campaign"]: p for p in inbox["parks"]}
    assert set(rows) == {I_DEC, I_DIRTY, I_OFF, I_ANS, I_WD}
    assert set(parks) == {I_DIRTY, I_OFF, I_ANS, I_WD}             # I_DEC has ESC-0001
    for cid, p in parks.items():
        assert set(p) == set(rows[cid]) | {"inbox_state", "fix"}
        assert SN._strip({k: p[k] for k in rows[cid]}) == SN._strip(rows[cid])
    assert parks[I_DIRTY]["inbox_state"] == "needs_host"
    assert parks[I_DIRTY]["fix"] == (
        f"On the host: move the uncommitted changes in `{DIRTY_ROOT}` off main (commit them on "
        f"a branch), then `nw queue resume {I_DIRTY}`.")
    assert parks[I_OFF]["inbox_state"] == "needs_host"
    assert parks[I_OFF]["fix"] == (
        "On the host: move any work off the current branch of `the project repo`, switch it "
        f"back to its base branch, then `nw queue resume {I_OFF}`.")
    assert parks[I_ANS]["inbox_state"] == "awaiting_conductor"
    assert parks[I_ANS]["fix"] == IB.HOST_FIX["awaiting_conductor"]
    # latest by created: the withdrawn ESC-0009, not the older answered ESC-0008
    assert parks[I_WD]["inbox_state"] == "needs_decision"
    assert parks[I_WD]["fix"] == IB.HOST_FIX["needs_decision"].replace("<cid>", I_WD)


def test_park_entries_other_reason_and_missing_escalation(fx):
    rows = [{"campaign": "C-X", "park": {"reason": "spec_write_failed"}},
            {"campaign": "C-Y", "park": {"reason": "human_decision"}}]
    out = IB.park_entries(fx["cfg"], rows)
    assert [(p["inbox_state"], p["fix"]) for p in out] == [
        ("needs_host", "On the host: `nw campaign log C-X --tail 20`."),
        ("needs_decision", IB.HOST_FIX["needs_decision"].replace("<cid>", "C-Y"))]


def test_unreadable_state_is_one_entry_error(fx, inbox):
    e = by_id(inbox)["ESC-0005"]
    assert e["campaign"] == I_BAD and e["question"] == "Corrupt one?"
    assert e["error"].startswith("JSONDecodeError: ") and e["answerable"] is False
    assert {k for k in e} >= set(IB.SUMMARY_KEYS) | {"question", "error", "answerable"}
    assert len([x for x in inbox["escalations"] if "error" not in x]) == 5


def test_a_raising_entry_builder_fails_that_entry_only(fx, monkeypatch):
    real = ESC.answerable_findings

    def boom(cfg, st, esc_id=None):
        if esc_id == "ESC-0001":
            raise KeyError("verdicts")
        return real(cfg, st, esc_id)
    monkeypatch.setattr(ESC, "answerable_findings", boom)
    got = by_id(IB.build_inbox(fx["cfg"], []))
    assert got["ESC-0001"]["error"] == "KeyError: 'verdicts'"
    assert got["ESC-0001"]["answerable"] is False and got["ESC-0001"]["question"]
    assert got["ESC-0006"]["forms"] == ["choose", "text"]


def test_a_failing_payload_keeps_escalations(fx, monkeypatch):
    def boom(*a, **k):
        raise ValueError("bad")
    monkeypatch.setattr(Q, "dashboard_payload", boom)
    got = SN.build(fx["cfg"])["inbox"]
    assert got["parks"] == {"error": "ValueError: bad"}
    assert len(got["escalations"]) == 6


def test_build_inbox_writes_nothing(fx):
    before = tree(fx["data"])
    for _ in range(5):
        IB.build_inbox(fx["cfg"], Q.dashboard_payload(fx["cfg"])["needs_you"])
        SN.build(fx["cfg"])
    assert tree(fx["data"]) == before


def test_effects_match_nw_conduct_row():
    assert "REVIEWING" in IB.EFFECTS["override"]
    [row] = [ln for ln in SKILL.read_text(encoding="utf-8").splitlines()
             if ln.startswith("| `escalation_answered` |")]
    for word in ("--to REVIEWING", "`choose`", "`text`"):
        assert word in row, word
    assert re.search(r"SPEC-0342", IB.EFFECTS_WORKER_LOST)

