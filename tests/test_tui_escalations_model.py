"""SPEC-0250: the escalations inbox's view model (no Textual). Records are made through
`escalations.plan_escalate` / `apply_escalate` with the notifier set to `none`."""
import json

import pytest

from test_agentic_escalations import (AC4, CID, RV, _answer, _camp, _cfg, _esc, _parked,
                                      _reviewed)
from wt.agentic import escalations as ESC
from wt.agentic import mailbox as MB
from wt.agentic import store as S
from wt.tui import agentic_model as AM


def cfg_(tmp_path):
    return _cfg(tmp_path, notify="none")


def _stamp(cfg, esc_id, created):
    rec = ESC.read(cfg, esc_id)
    rec["created"] = created
    ESC._write(cfg, rec)


def test_load_escalations_orders_open_first_then_newest(tmp_path):
    cfg = cfg_(tmp_path)
    assert AM.load_escalations(cfg) == []
    for i in range(1, 5):
        _esc(cfg, cid=None, question=f"q{i}\nsecond line", options=[], recommend=None)
        _stamp(cfg, f"ESC-000{i}", f"2026-10-25T1{i}:00:00+00:00")
    ESC.withdraw(cfg, "ESC-0004", "no", "human")
    ESC.apply_answer(cfg, ESC.plan_answer(cfg, "ESC-0001", text="t", by="human"))
    rows = AM.load_escalations(cfg)
    assert [(r.id, r.status) for r in rows] == [
        ("ESC-0003", "open"), ("ESC-0002", "open"), ("ESC-0004", "withdrawn"),
        ("ESC-0001", "answered")]
    assert rows[0].question == "q3" and rows[0].options_n == 0 and rows[0].age_s > 0


def test_escalation_text_for_an_open_record_shows_every_field(tmp_path):
    cfg = cfg_(tmp_path)
    _parked(cfg)
    _esc(cfg, question="ship it?\nreally", options=["yes", "no"], recommend=2,
         facts=["gate red", "two loops"])
    text = AM.escalation_text(cfg, ESC.read(cfg, "ESC-0001")).plain
    assert text.splitlines()[0].startswith("ESC-0001  open  ")
    for want in (f"campaign {CID}  {CID[:9]}  PARKED", "ship it?\nreally", "  1. yes\n",
                 "  2. no (recommended)", "facts:", "  - gate red", "  - two loops",
                 "park: loop_exhausted → IMPLEMENTING (immediate)", "notified via none ok",
                 "reminded never", "override (CLI):",
                 'wt escalations answer ESC-0001 --waive <finding-id> --attest <ac> '
                 '--reason "<why>" --yes'):
        assert want in text, want
    assert "  1. yes (recommended)" not in text


def test_answered_and_withdrawn_records_show_the_outcome(tmp_path):
    cfg = cfg_(tmp_path)
    _parked(cfg)
    _esc(cfg)
    _answer(cfg, choose="2", note="because")
    a = AM.escalation_text(cfg, ESC.read(cfg, "ESC-0001")).plain
    rec = ESC.read(cfg, "ESC-0001")
    assert f"answered by human at {rec['answered_at']}" in a
    assert ESC.answer_summary(rec) == "chose 2 (no) — note: because" and ESC.answer_summary(rec) in a
    assert "override (CLI)" not in a
    _esc(cfg, cid=None, question="loose")
    ESC.withdraw(cfg, "ESC-0002", "not needed", "human")
    w = AM.escalation_text(cfg, ESC.read(cfg, "ESC-0002")).plain
    assert "no campaign" in w and "withdrawn by human: not needed" in w
    assert "override (CLI)" not in w and "park:" not in w


def test_campaignless_and_gone_campaign_records(tmp_path):
    cfg = cfg_(tmp_path)
    _esc(cfg, cid=None, question="loose", options=[], recommend=None)
    t = AM.escalation_text(cfg, ESC.read(cfg, "ESC-0001")).plain
    assert "no campaign" in t and "override (CLI)" not in t and "  1." not in t
    _parked(cfg)
    _esc(cfg)
    import shutil
    shutil.rmtree(S.campaign_dir(cfg, CID))
    t = AM.escalation_text(cfg, ESC.read(cfg, "ESC-0002")).plain
    assert f"campaign {CID}  {CID[:9]}  (campaign gone)" in t
    assert "override (CLI):" in t            # still a template, no ids


def test_override_block_lists_the_answerable_findings_and_skips_overridden_ones(tmp_path):
    cfg = cfg_(tmp_path)
    _reviewed(cfg)
    ids = [f["id"] for f in ESC.answerable_findings(cfg, S.read_state(cfg, CID))]
    assert f"{RV}#0" in ids and f"{RV}#ac0" in ids          # a real and a synthetic finding
    t = AM.escalation_text(cfg, ESC.read(cfg, "ESC-0001")).plain
    assert f"  {RV}#0 major Wrong order" in t and f"  {RV}#ac0 major" in t and AC4 in t
    _answer(cfg, waive=[f"{RV}#0"], reason="ok")
    _esc(cfg, question="again?")
    assert f"{RV}#0" not in [f["id"] for f in ESC.answerable_findings(cfg, S.read_state(cfg, CID))]
    t = AM.escalation_text(cfg, ESC.read(cfg, "ESC-0002")).plain
    assert f"{RV}#0 " not in t and f"{RV}#ac0 " in t
    with pytest.raises(ValueError, match="not a finding"):          # plan_answer agrees
        ESC.plan_answer(cfg, "ESC-0002", waive=[f"{RV}#0"], reason="r")


def test_override_block_caps_the_finding_ids_at_15(tmp_path):
    cfg = cfg_(tmp_path)
    findings = [{"severity": "minor", "title": f"nit {i}", "files": [f"f{i}.py"]}
                for i in range(20)]
    _camp(cfg, "PARKED", last_review={"verdicts": {RV: {
        "verdict": "fix", "score": 60, "fix_brief": "x", "loop": 0, "run": "1-r",
        "findings": findings}}},
          park={"reason": "loop_exhausted", "detail": "d", "since": S.now_iso(),
                "resume_to": "IMPLEMENTING"})
    _esc(cfg)
    n = len(ESC.answerable_findings(cfg, S.read_state(cfg, CID)))
    assert n > 15
    t = AM.escalation_text(cfg, ESC.read(cfg, "ESC-0001")).plain
    assert sum(1 for line in t.splitlines() if line.startswith(f"  {RV}#")) == 15
    assert f"  +{n - 15} more" in t


def test_plan_withdraw_and_apply_withdraw(tmp_path):
    cfg = cfg_(tmp_path)
    _parked(cfg)
    _esc(cfg)
    with pytest.raises(ValueError):
        ESC.plan_withdraw(cfg, "ESC-0001", reason="r", by="nobody")
    plan = ESC.plan_withdraw(cfg, "ESC-0001", reason="stale", by="human")
    assert ESC.read(cfg, "ESC-0001")["status"] == "open"            # pure
    ESC.withdraw(cfg, "ESC-0001", "someone else", "conductor")      # the record moves on
    with pytest.raises(ValueError, match="not open"):
        ESC.apply_withdraw(cfg, plan)
    with pytest.raises(ValueError, match="not open"):
        ESC.plan_withdraw(cfg, "ESC-0001", reason="r", by="human")
    _esc(cfg, question="next")
    rec = ESC.apply_withdraw(cfg, ESC.plan_withdraw(cfg, "ESC-0002", reason="r", by="human"))
    assert rec["status"] == "withdrawn" and rec["campaign_state"] == "PARKED"
    _esc(cfg, cid=None, question="loose")
    rec = ESC.apply_withdraw(cfg, ESC.plan_withdraw(cfg, "ESC-0003", reason="r", by="human"))
    assert rec["campaign_state"] is None


def test_the_escalation_mail_points_at_the_record(tmp_path):
    cfg = cfg_(tmp_path)
    _esc(cfg, cid=None, question="loose")
    MB.send(cfg, to="human", frm="conductor", kind="info", text="plain mail")
    msgs = AM.snapshot(cfg)
    roots = {m["text"].splitlines()[0]: m["id"] for m in msgs if m.get("to") == "human"}
    t = AM.thread_text(cfg, roots["loose"], msgs).plain
    assert "    answer in escalations: ESC-0001\n" in t
    assert "answer in escalations" not in AM.thread_text(cfg, roots["plain mail"], msgs).plain


def test_the_signature_changes_on_a_write_and_the_snapshot_opens_on_escalations(tmp_path):
    cfg = cfg_(tmp_path)
    s0 = AM.escalations_signature(cfg)
    assert s0 == () and AM.load_mailbox(cfg).inbox == "human"
    _esc(cfg, cid=None, question="loose")
    s1 = AM.escalations_signature(cfg)
    assert s1 != s0 and s1[0][0] == "ESC-0001.json"
    snap = AM.load_mailbox(cfg)
    assert snap.inbox == "escalations" and snap.threads == [] and len(snap.escalations) == 1
    ESC.withdraw(cfg, "ESC-0001", "r", "human")
    assert AM.escalations_signature(cfg) != s1
    assert AM.load_mailbox(cfg).inbox == "human"                  # none open any more
    assert AM.load_mailbox(cfg, "conductor").inbox == "conductor"
