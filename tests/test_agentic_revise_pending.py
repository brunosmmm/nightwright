"""SPEC-0496: the conductor ping lists specs stalled on a `revise` review."""
from nightwright.agentic import events as E
from nightwright.agentic import quiet as QT
from nightwright.agentic import spec_review as SR
from nightwright.agentic import store


def _row(spec_id, verdict, current=True):
    return {"spec_id": spec_id, "verdict": verdict, "reviewed_at": "t", "counts": {},
            "matches_current": current}


def test_revise_pending_items(monkeypatch):
    rows = [_row("SPEC-1", "revise"), _row("SPEC-2", "approve"), _row("SPEC-3", "revise"),
            _row("SPEC-4", "revise", current=False), _row("SPEC-5", "revise", current=None),
            _row("SPEC-6", "revise")]
    monkeypatch.setattr(SR, "all_latest_reviews", lambda cfg: rows)
    monkeypatch.setattr(store, "list_campaigns", lambda cfg: [
        {"spec_id": "SPEC-3", "state": "QUEUED"}, {"spec_id": "SPEC-6", "state": "ABANDONED"}])
    items = E.revise_pending_items({})
    assert [(i["spec_id"], i["stale"]) for i in items] == [
        ("SPEC-1", False), ("SPEC-4", True), ("SPEC-6", False)]


def test_ping_fingerprint_revise_pending():
    fp = QT.ping_fingerprint({"revise_pending": [{"spec_id": "SPEC-1"}]})
    assert fp == [["revise_pending", "SPEC-1"]]
