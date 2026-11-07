"""SPEC-0407: campaign detail popup view model (pure, no Textual)."""
import pytest

from rich.text import Text

from test_agentic_dashboard import _fixture
from nightwright import report as R
from nightwright.agentic import history as H
from nightwright.agentic import schemas as SC
from nightwright.tui import agentic_model as AM


_TRANSITION = frozenset({
    "open", "transition", "park", "resume", "priority", "close",
    "admitted_after_revision", "admitted_revise_pending",
})


def _timeline_kind(etype: str) -> str:
    if etype in ("run.start", "run.end"):
        return "run"
    return etype


def _expected_group(etype: str) -> str:
    if etype in _TRANSITION:
        return "transitions"
    if etype in ("run.start", "run.end", "cost"):
        return "runs"
    if etype == "gate":
        return "gates"
    if etype in ("finding", "followup"):
        return "findings"
    if etype in ("human_decision", "triage"):
        return "decisions"
    return "notes"


def _row(*, kind, seq=1):
    return {"kind": kind, "seq": seq, "loop": 0, "summary": kind}


def test_timeline_group_covers_event_keys_types_and_edge_cases():
    for etype in SC.EVENT_KEYS:
        assert AM.timeline_group(_row(kind=_timeline_kind(etype), seq=1)) == _expected_group(etype), etype
    assert AM.timeline_group(_row(kind="something.new")) == "notes"
    assert AM.timeline_group(_row(kind="run")) == "runs"
    assert AM.timeline_group(_row(kind="triage")) == "decisions"
    assert AM.timeline_group(_row(kind="human_decision")) == "decisions"
    assert AM.timeline_group({"kind": "mailbox", "seq": None, "ref": "rec:x"}) == "decisions"


def test_group_timeline_inserts_separators_on_loop_change():
    rows = [
        {"loop": 0, "kind": "open", "seq": 1},
        {"loop": 0, "kind": "note", "seq": 2},
        {"loop": 1, "kind": "gate", "seq": 3},
        {"loop": 1, "kind": "finding", "seq": 4},
    ]
    grouped = AM.group_timeline(rows)
    assert grouped[0]["kind"] == "open"
    assert grouped[1]["kind"] == "note"
    assert grouped[2]["separator"] and grouped[2]["loop"] == 1
    assert grouped[3]["kind"] == "gate"
    assert not any(g.get("separator") and g.get("loop") == 0 for g in grouped)


def test_resolve_focus_maps_tabs_run_record_and_unknown():
    history = {
        "runs": [{"run_id": "001-implementer"}],
        "records": [{"id": "rec-mail-1"}],
    }
    assert AM.resolve_focus(history, None) == ("overview", None)
    assert AM.resolve_focus(history, {"tab": "runs", "run": "001-implementer"}) == (
        "runs", "001-implementer")
    assert AM.resolve_focus(history, {"tab": "records", "record": "rec-mail-1"}) == (
        "records", "rec-mail-1")
    assert AM.resolve_focus(history, {"tab": "runs", "run": "missing"}) == ("overview", "not found")
    assert AM.resolve_focus(history, {"tab": "records", "record": "nope"}) == ("overview", "not found")
    assert AM.resolve_focus(history, {"tab": "timeline"}) == ("timeline", None)
    assert AM.resolve_focus(history, {"tab": "bogus"}) == ("overview", "not found")


def test_load_detail_header_lines_match_load_campaign(tmp_path):
    _, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    detail = AM.load_detail(cfg, cid)
    camp = AM.load_campaign(cfg, cid)
    assert [t.plain for t in detail.header_lines] == [t.plain for t in camp.header]


def test_markup_in_summary_and_header_stays_literal_in_text(tmp_path):
    markup = "[bold red]x[/] and [link=file:///etc]y[/link]"
    _, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    from test_agentic_dashboard import _ev
    _ev(cfg, cid, "note", {"text": markup})
    detail = AM.load_detail(cfg, cid)
    note = next(r for r in detail.history["timeline"] if markup in (r.get("summary") or ""))
    clipped = Text(R._clip(note["summary"], 200))
    assert clipped.plain == note["summary"] and "[bold red]" in clipped.plain
    header_plain = "\n".join(t.plain for t in detail.header_lines)
    assert markup not in header_plain
