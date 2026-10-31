"""SPEC-0197: piped human output never hard-wraps text lines."""
import io

import click
from click.testing import CliRunner
from rich.console import Console
from rich.table import Table

from test_agentic_e2e import added, make_cfg, make_repo, run_cli, script
from nightwright import render as R
from nightwright.agentic import mailbox as M
from nightwright.agentic.cli_mailbox import mailbox_group
from nightwright.console import NwConsole


def _words(n: int) -> str:
    """Space-separated text of exactly n characters (wraps if the console wraps)."""
    return " ".join(["word"] * n)[:n]


def _render(obj, *, terminal=False, **kw) -> str:
    buf = io.StringIO()
    NwConsole(file=buf, width=80, force_terminal=terminal, color_system=None).print(
        obj, highlight=False, **kw)
    return buf.getvalue()


def _one_line(out: str, *needles: str) -> None:
    lines = out.splitlines()
    assert len(lines) == 1, out
    for n in needles:
        assert n in lines[0]


def test_long_str_is_one_line():
    text = _words(200)
    _one_line(_render(text), text)


def test_tick_row_one_line():
    reason = _words(150)
    row = {"campaign": "C-1", "from": "QUEUED", "to": "OPENING", "action": "open", "reason": reason}
    _one_line(_render(R.tick_row(row)), reason)


def test_campaign_event_one_line():
    ev = {"seq": 3, "ts": "2026-10-25T10:00:00Z", "type": "note", "by": "human",
          "text": _words(200), "author": "human", "extra": _words(100)}
    out = _render(R.campaign_event(ev))
    _one_line(out, "text=", "extra=")


def test_message_one_line():
    m = {"id": "conductor:1", "kind": "question", "from": "conductor", "campaign": "C-1",
         "text": _words(300)}
    _one_line(_render(R.message(m)), _words(300))


def test_plan_one_line_per_key():
    value = _words(150)
    out = _render(R.plan("do it", {"alpha": value, "beta": "short"}))
    lines = out.splitlines()
    assert len(lines) == 3, out
    assert value in lines[1] and lines[2].strip() == "beta: short"


def test_terminal_still_wraps():
    out = _render(_words(200), terminal=True)
    assert len(out.splitlines()) > 1


def test_explicit_soft_wrap_false_is_respected():
    assert len(_render(_words(200), soft_wrap=False).splitlines()) > 1


def test_table_unchanged():
    def table():
        t = Table(title="t")
        t.add_column("a")
        t.add_column("b")
        t.add_row(_words(120), _words(90))
        return t

    for terminal in (False, True):
        kw = dict(width=80, force_terminal=terminal, color_system=None)
        mine, plain = io.StringIO(), io.StringIO()
        c1, c2 = NwConsole(file=mine, **kw), Console(file=plain, **kw)
        c1.print(table())
        c2.print(table())
        assert mine.getvalue() == plain.getvalue()
        assert len(mine.getvalue().splitlines()) > 5


def test_cli_campaign_log_note_one_line(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    cid = added(cfg, monkeypatch)
    long_note = _words(300)
    r = run_cli(cfg, monkeypatch, ["campaign", "note", cid, long_note])
    assert r.exit_code == 0, r.output
    r = run_cli(cfg, monkeypatch, ["campaign", "log", cid, "--type", "note"])
    assert r.exit_code == 0, r.output
    lines = [ln for ln in r.output.splitlines() if "text=" in ln]
    assert len(lines) == 1 and len(r.output.splitlines()) == 1, r.output
    assert len(lines[0]) > 80


def test_cli_mailbox_thread_message_one_line(tmp_path):
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "mappings.yaml").write_text("{}\n")
    cfg = {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "cfg"),
           "outbox_targets": {"Demo": {"repo_path": str(tmp_path)}},
           "project_axis": "bucket", "specs_dir": str(tmp_path / "specs")}
    text = _words(300)
    msg = M.send(cfg, to="steward:Demo", frm="conductor", text=text, kind="question")
    root = click.Group("root")
    root.add_command(mailbox_group)
    r = CliRunner().invoke(root, ["mailbox", "thread", msg["id"]], obj=cfg)
    assert r.exit_code == 0, r.output
    assert len(r.output.splitlines()) == 1 and text in r.output, r.output
