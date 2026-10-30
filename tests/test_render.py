"""SPEC-0185: `wt.render` — styled renderers keep the plain words of the existing helpers;
warnings go to stderr; code blocks are byte-identical when piped."""
import io

import pytest
from rich.console import Console
from rich.syntax import Syntax

from wt import render as R
from wt.agentic import events as E
from wt.agentic import mailbox as M
from wt.agentic import queue as Q

CAMPAIGN_EVENTS = [
    {"seq": 3, "ts": "2026-10-25T13:47:00-04:00", "campaign": "c", "type": "transition",
     "by": "tick", "from": "OPENING", "to": "IMPLEMENTING", "reason": "opened"},
    {"seq": 4, "ts": "t", "campaign": "c", "type": "run.start", "by": "tick",
     "persona": "implementer", "run_id": "001-implementer", "model": "sonnet"},
    {"seq": 5, "ts": "t", "type": "run.end", "by": "tick", "verdict_ok": True,
     "result_tail": "x" * 200},
    {"seq": 9, "ts": "t", "type": "gate", "by": "tick", "ok": True, "gate_seq": 1},
    {"seq": 10, "ts": "t", "type": "gate", "by": "tick", "ok": False, "gate_seq": 2},
    {"seq": 11, "ts": "t", "type": "park", "by": "tick", "reason": "rate_limited",
     "resume_to": "IMPLEMENTING"},
    {"seq": 12, "ts": "t", "type": "note", "by": "agent:conductor", "text": "hello [b]x[/b]"},
    {"seq": 13, "type": "mystery"},
]


def _old_event_line(ev):
    """The pre-SPEC-0185 `cli_campaign._event_line`, kept here as the plain-text contract."""
    skip = {"ts", "seq", "campaign", "type", "by"}
    bits = []
    for k, v in ev.items():
        if k in skip:
            continue
        txt = v if isinstance(v, str) else str(v)
        if len(txt) > 60:
            txt = txt[:57] + "..."
        bits.append(f"{k}={txt}")
    return (f"{ev.get('seq', '?'):>4}  {ev.get('ts', '')}  {ev.get('type', '?'):<12} "
            f"{ev.get('by', ''):<16} {' '.join(bits)}")


@pytest.mark.parametrize("ev", CAMPAIGN_EVENTS)
def test_campaign_event_plain_matches(ev):
    assert R.campaign_event(ev).plain == _old_event_line(ev)


@pytest.mark.parametrize("ev", [
    {"id": 7, "type": "park", "campaign": "c1", "data": {"reason": "rate_limited", "x": None}},
    {"id": 8, "type": "runner_dead", "campaign": None, "data": {}},
    {"id": 9, "type": "merged", "campaign": "c2", "data": {"sha": "abc", "empty": []}},
])
def test_queue_event_plain_matches(ev):
    assert R.queue_event(ev).plain == E.summary_line(ev)


@pytest.mark.parametrize("m", [
    {"id": "a:1", "kind": "question", "from": "conductor", "campaign": "c", "re": None,
     "text": "two\nlines [red]x[/]"},
    {"id": "a:2", "kind": "answer", "from": "steward:P", "campaign": None, "re": "a:1",
     "text": "ok"},
    {"id": "a:3", "kind": "weird", "from": "x", "text": "t"},
])
def test_message_plain_matches(m):
    assert R.message(m).plain == M.summary_line(m)


@pytest.mark.parametrize("row", [
    {"campaign": "c", "from": "QUEUED", "to": "OPENING", "action": "admit", "reason": "slot"},
    {"campaign": "c", "from": "IMPLEMENTING", "to": "GATING", "action": "run",
     "reason": "implemented", "run_id": "001-implementer", "cost_usd": 0.5},
])
def test_tick_row_plain_matches(row):
    assert R.tick_row(row).plain == Q.tick_row_text(row)


def test_every_state_has_a_style():
    for st in Q.STATES:
        assert R.state_style(st), st


def test_project_style_is_stable_and_distinguishes():
    assert R.project_style("Meta-Tools") == R.project_style("Meta-Tools")
    assert R.project_style(None) == "dim"
    assert len({R.project_style(p) for p in ("Meta-Tools", "comfort-assistant", "a", "b")}) > 1


def test_plan_keeps_key_colon_value_lines():
    c = Console(file=io.StringIO(), width=100)
    c.print(R.plan("queue SPEC-1", {"campaign": "c1", "empty": "", "cap_usd": 10.0}))
    assert c.file.getvalue().splitlines() == ["plan  queue SPEC-1", "  campaign: c1",
                                              "  cap_usd: 10.0"]


def test_warn_and_note_go_to_stderr(capsys):
    R.warn("  ! unknown project 'x' [not markup]")
    R.note("running …")
    out, err = capsys.readouterr()
    assert out == ""
    assert "  ! unknown project 'x' [not markup]" in err and "running …" in err


def test_print_code_plain_when_piped():
    buf = io.StringIO()
    c = Console(file=buf, width=20, force_terminal=False)
    text = "root: /a/very/long/path/that/would/wrap/at/twenty/columns\nx: 1\n"
    R.print_code(text, "yaml", console=c, title="/some/path.yaml")
    assert buf.getvalue() == "# /some/path.yaml\n" + text


def test_print_code_syntax_on_terminal(monkeypatch):
    seen = []
    c = Console(file=io.StringIO(), force_terminal=True, width=80)
    monkeypatch.setattr(c, "print", lambda obj, **kw: seen.append(obj))
    R.print_code("a: 1\n", "yaml", console=c)
    assert any(isinstance(o, Syntax) for o in seen)


def test_campaign_event_idea_reconciled():
    ev = {"seq": 7, "ts": "t", "type": "idea_reconciled", "by": "tick", "spec_id": "SPEC-0257",
          "idea_id": "IDEA-067", "from": "INCUBATE", "to": "PROMOTED"}
    assert R.campaign_event(ev).plain.endswith("idea IDEA-067: INCUBATE -> PROMOTED")


def test_campaign_event_outbound_pulled():                                       # SPEC-0297
    ev = {"seq": 8, "ts": "t", "type": "outbound_pulled", "by": "tick", "spec_id": "ACME-0001",
          "status": "done", "outbox_path": "/o/acme/ACME-0001-x.md", "idea_id": "IDEA-038",
          "from": "EXPORTED", "to": "SHIPPED"}
    assert R.campaign_event(ev).plain.endswith(
        "outbox ACME-0001 → done; idea IDEA-038: EXPORTED -> SHIPPED")
