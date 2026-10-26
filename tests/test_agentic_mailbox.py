"""SPEC-0183: file-based mailbox between conductor and stewards."""
import io
import json
import threading
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from wt.agentic import events as E
from wt.agentic import mailbox as M
from wt.agentic.cli_mailbox import mailbox_group


def _cfg(tmp_path):
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "mappings.yaml").write_text("{}\n")
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "cfg"),
            "outbox_targets": {"Demo": {"repo_path": str(tmp_path)}},
            "project_axis": "bucket", "specs_dir": str(tmp_path / "specs")}


def test_address_validation(tmp_path):
    cfg = _cfg(tmp_path)
    assert M.validate_address(cfg, "conductor") == "conductor"
    assert M.validate_address(cfg, "human") == "human"
    assert M.validate_address(cfg, "steward:demo") == "steward:Demo"
    assert M.validate_address(cfg, "steward:meta-tools") == "steward:Meta-Tools"
    with pytest.raises(ValueError, match="unknown steward project"):
        M.validate_address(cfg, "steward:Nope")
    with pytest.raises(ValueError, match="unknown address"):
        M.validate_address(cfg, "bob")


def test_send_envelope_and_defaults(tmp_path):
    cfg = _cfg(tmp_path)
    q = M.send(cfg, to="steward:Demo", frm="conductor", text="why?", kind="question",
               campaign="C1")
    assert q["schema"] == "wt.message.v1" and q["id"] == "steward:Demo:1" and q["seq"] == 1
    assert q["needs_reply"] is True and q["campaign"] == "C1"
    i = M.send(cfg, to="steward:Demo", frm="conductor", text="fyi")
    assert i["needs_reply"] is False and i["seq"] == 2
    with pytest.raises(ValueError):
        M.send(cfg, to="steward:Demo", frm="conductor", text="x", kind="bogus")


def test_concurrent_senders_get_distinct_increasing_ids(tmp_path):
    cfg = _cfg(tmp_path)

    def worker(n):
        for i in range(25):
            M.send(cfg, to="human", frm="conductor", text=f"{n}-{i}")

    ts = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    seqs = [m["seq"] for m in M.read_inbox(cfg, "human")]
    assert seqs == list(range(1, 151))


def test_conductor_mail_is_mirrored_into_events(tmp_path):
    cfg = _cfg(tmp_path)
    m = M.send(cfg, to="conductor", frm="steward:Demo", text="please resume", kind="request")
    evs = E.read_since(cfg, 0, types=["message"])
    assert len(evs) == 1
    assert evs[0]["data"]["msg_id"] == m["id"] and evs[0]["by"] == "steward:Demo"
    M.send(cfg, to="human", frm="conductor", text="not mirrored")
    assert len(E.read_since(cfg, 0, types=["message"])) == 1


def test_follow_resumes_after_cursor(tmp_path):
    cfg = _cfg(tmp_path)
    M.send(cfg, to="conductor", frm="human", text="old one")
    out = io.StringIO()
    # new reader starts at the current end: nothing printed
    assert M.follow(cfg, "conductor", out=out, poll_s=0, stop=lambda: True) == 0
    M.send(cfg, to="conductor", frm="human", text="a")
    M.send(cfg, to="conductor", frm="human", text="b")
    assert M.follow(cfg, "conductor", out=out, poll_s=0, max_events=1) == 1
    M.send(cfg, to="conductor", frm="human", text="c")
    out2 = io.StringIO()
    assert M.follow(cfg, "conductor", out=out2, poll_s=0, stop=lambda: True) == 2
    texts = [json.loads(ln)["text"] for ln in (out.getvalue() + out2.getvalue()).splitlines()]
    assert texts == ["a", "b", "c"]


def test_follow_from_start_and_single_follower(tmp_path):
    cfg = _cfg(tmp_path)
    M.send(cfg, to="human", frm="conductor", text="first")
    out = io.StringIO()
    assert M.follow(cfg, "human", out=out, poll_s=0, from_start=True, stop=lambda: True) == 1
    with E.reader_lock(cfg, "mailbox:human"):
        with pytest.raises(ValueError, match="already followed"):
            M.follow(cfg, "human", out=io.StringIO(), poll_s=0, stop=lambda: True)


def test_follow_exits_with_code_changed(tmp_path, code_repo):
    cfg = _cfg(tmp_path)
    out, calls = io.StringIO(), {"n": 0}
    kw = dict(poll_s=0.01, code_root=code_repo.root, code_check_s=0.01)
    assert M.follow(cfg, "conductor", out=out, stop=lambda: True, **kw) == 0   # new reader
    M.send(cfg, to="conductor", frm="human", text="a")
    m2 = M.send(cfg, to="conductor", frm="human", text="b")
    out = io.StringIO()

    def stop():
        calls["n"] += 1
        if calls["n"] == 1:
            code_repo.commit({"skills/wt-steward/SKILL.md": "new\n"})
        return False

    assert M.follow(cfg, "conductor", out=out, stop=stop, **kw) == 2
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [x.get("text") for x in lines[:2]] == ["a", "b"]
    cc = lines[2]
    assert cc["type"] == "code_changed" and cc["id"] is None and cc["by"] == "follower"
    assert cc["data"]["reader"] == "mailbox:conductor"
    assert cc["data"]["skills_changed"] == ["wt-steward"]
    assert E.cursor_get(cfg, "mailbox:conductor") == m2["seq"]
    # restart: only the new message, none repeated
    M.send(cfg, to="conductor", frm="human", text="c")
    out2 = io.StringIO()
    assert M.follow(cfg, "conductor", out=out2, max_events=1, **kw) == 1
    assert [json.loads(x)["text"] for x in out2.getvalue().splitlines()] == ["c"]


def test_follow_code_root_none_keeps_running(tmp_path, code_repo):
    cfg = _cfg(tmp_path)
    out, calls = io.StringIO(), {"n": 0}

    def stop():
        calls["n"] += 1
        if calls["n"] == 1:
            code_repo.commit({"src/wt/x.py": "x = 2\n"})
        return calls["n"] >= 4

    assert M.follow(cfg, "human", out=out, poll_s=0, code_root=None, code_check_s=0.01,
                    stop=stop) == 0
    assert calls["n"] == 4 and out.getvalue() == ""


def test_unanswered_and_thread(tmp_path):
    cfg = _cfg(tmp_path)
    q = M.send(cfg, to="steward:Demo", frm="conductor", text="resume?", kind="question")
    q2 = M.send(cfg, to="steward:Demo", frm="conductor", text="other?", kind="question")
    assert [m["id"] for m in M.unanswered(cfg, "steward:Demo")] == [q["id"], q2["id"]]
    a = M.send(cfg, to="conductor", frm="steward:Demo", text="yes", kind="answer", re=q["id"])
    assert [m["id"] for m in M.unanswered(cfg, "steward:Demo")] == [q2["id"]]
    t = M.thread(cfg, q["id"])
    assert [m["id"] for m in t] == [q["id"], a["id"]]
    with pytest.raises(ValueError):
        M.thread(cfg, "nope:1")


def _invoke(cfg, args, env=None):
    root = click.Group("root")
    root.add_command(mailbox_group)
    return CliRunner().invoke(root, args, obj=cfg, env=env or {})


def test_cli_send_list_thread(tmp_path):
    cfg = _cfg(tmp_path)
    r = _invoke(cfg, ["mailbox", "send", "--to", "steward:Demo", "--kind", "question",
                      "hello", "there"])
    assert r.exit_code != 0 and "WT_MAILBOX_ADDR" in r.output
    r = _invoke(cfg, ["mailbox", "send", "--to", "steward:Demo", "--kind", "question",
                      "--json", "hello", "there"], env={"WT_MAILBOX_ADDR": "conductor"})
    assert r.exit_code == 0, r.output
    rec = json.loads(r.output)
    assert rec["text"] == "hello there" and rec["from"] == "conductor"
    r = _invoke(cfg, ["mailbox", "list", "--to", "steward:Demo", "--unanswered", "--json"])
    payload = json.loads(r.output)
    assert payload["schema"] == "wt.mailbox.list.v1" and len(payload["messages"]) == 1
    r = _invoke(cfg, ["mailbox", "send", "--to", "conductor", "--from", "steward:Demo",
                      "--kind", "answer", "--re", rec["id"], "ok"])
    assert r.exit_code == 0, r.output
    r = _invoke(cfg, ["mailbox", "thread", rec["id"], "--json"])
    assert [m["text"] for m in json.loads(r.output)["messages"]] == ["hello there", "ok"]
    r = _invoke(cfg, ["mailbox", "list", "--to", "steward:Demo"])
    assert r.exit_code == 0 and "hello there" in r.output
    r = _invoke(cfg, ["mailbox", "send", "--to", "steward:Nope", "--from", "conductor", "x"])
    assert r.exit_code != 0 and "unknown steward project" in r.output


def test_cli_follow_no_code_watch(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    seen = []
    monkeypatch.setattr(M, "follow", lambda *a, **kw: seen.append(kw) or 0)
    assert _invoke(cfg, ["mailbox", "follow", "--to", "human"]).exit_code == 0
    assert "code_root" not in seen[-1]
    r = _invoke(cfg, ["mailbox", "follow", "--to", "human", "--no-code-watch"])
    assert r.exit_code == 0, r.output
    assert seen[-1]["code_root"] is None
