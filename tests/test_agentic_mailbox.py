"""SPEC-0183: file-based mailbox between conductor and stewards."""
import io
import json
import os
import threading
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from nightwright.agentic import events as E
from nightwright.agentic import mailbox as M
from nightwright.agentic.cli_mailbox import mailbox_group

pytestmark = pytest.mark.fast


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
    assert q["schema"] == "nw.message.v1" and q["id"] == "steward:Demo:1" and q["seq"] == 1
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
    start = code_repo.head()

    def stop():
        calls["n"] += 1
        if calls["n"] == 1:
            # a message and a code commit land together: the message must still come first
            calls["m3"] = M.send(cfg, to="conductor", frm="human", text="late")
            code_repo.commit({"skills/nw-steward/SKILL.md": "new\n"})
        return False

    assert M.follow(cfg, "conductor", out=out, stop=stop, **kw) == 3
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [x.get("text") for x in lines[:3]] == ["a", "b", "late"]
    cc = lines[3]
    assert cc["data"]["from_sha"] == start and cc["data"]["to_sha"] == code_repo.head()
    assert cc["data"]["paths"] == ["skills/nw-steward/SKILL.md"]
    assert cc["type"] == "code_changed" and cc["id"] is None and cc["by"] == "follower"
    assert cc["data"]["reader"] == "mailbox:conductor"
    assert cc["data"]["skills_changed"] == ["nw-steward"]
    assert E.cursor_get(cfg, "mailbox:conductor") == calls["m3"]["seq"]
    # restart: only the new message, none repeated
    M.send(cfg, to="conductor", frm="human", text="c")
    out2 = io.StringIO()
    assert M.follow(cfg, "conductor", out=out2, max_events=1, **kw) == 1
    assert [json.loads(x)["text"] for x in out2.getvalue().splitlines()] == ["c"]


def test_follow_auto_root_and_config_key(tmp_path, code_repo, monkeypatch):
    cfg = _cfg(tmp_path)

    def run():
        out, calls = io.StringIO(), {"n": 0}

        def stop():
            calls["n"] += 1
            if calls["n"] == 1:
                code_repo.commit({"src/nightwright/x.py": f"x = {code_repo.head()}\n"})
            return calls["n"] >= 5

        M.follow(cfg, "human", out=out, poll_s=0.01, stop=stop)
        return out.getvalue()

    monkeypatch.setattr(E.CW, "code_checkout", lambda: code_repo.root)
    assert run() == ""                                   # default 10 s: not yet
    cfg["agentic"] = {"events": {"code_check_s": 0.01}}
    assert json.loads(run())["type"] == "code_changed"   # AUTO root + config key
    monkeypatch.setattr(E.CW, "code_checkout", lambda: None)
    assert run() == ""                                   # non-checkout install


def test_follow_code_root_none_keeps_running(tmp_path, code_repo):
    cfg = _cfg(tmp_path)
    out, calls = io.StringIO(), {"n": 0}

    def stop():
        calls["n"] += 1
        if calls["n"] == 1:
            code_repo.commit({"src/nightwright/x.py": "x = 2\n"})
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
    assert r.exit_code != 0 and "NW_MAILBOX_ADDR" in r.output
    r = _invoke(cfg, ["mailbox", "send", "--to", "steward:Demo", "--kind", "question",
                      "--json", "hello", "there"], env={"NW_MAILBOX_ADDR": "conductor"})
    assert r.exit_code == 0, r.output
    rec = json.loads(r.output)
    assert rec["text"] == "hello there" and rec["from"] == "conductor"
    r = _invoke(cfg, ["mailbox", "list", "--to", "steward:Demo", "--unanswered", "--json"])
    payload = json.loads(r.output)
    assert payload["schema"] == "nw.mailbox.list.v1" and len(payload["messages"]) == 1
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


# ---- SPEC-0431: campaign stamps ----------------------------------------------------------------

CA = "SPEC-0001-20261101-abcd"
CB = "SPEC-0002-20261102-0123"


def _mk(cfg, cid):
    from nightwright.agentic import store as S
    S.create_campaign(cfg, {"id": cid, "spec_id": cid[:9], "project": "Demo", "kind": "spec",
                            "state": "IMPLEMENTING", "counters": {}})


def _send(cfg, *args):
    return _invoke(cfg, ["mailbox", "send", "--from", "steward:Demo", "--json", *args])


def test_send_campaign_from_envelope(tmp_path):
    cfg = _cfg(tmp_path)
    plain = M.send(cfg, to="conductor", frm="human", text="x")
    assert "campaign_from" not in plain
    stamped = M.send(cfg, to="conductor", frm="human", text="x", campaign="C1",
                     campaign_from="re")
    assert stamped["campaign_from"] == "re"
    before = M._inbox_path(cfg, "conductor").read_bytes()
    for camp, cf in (("C1", "txt"), ("C1", "event"), (None, "text")):
        with pytest.raises(ValueError):
            M.send(cfg, to="conductor", frm="human", text="y", campaign=camp, campaign_from=cf)
    assert M._inbox_path(cfg, "conductor").read_bytes() == before


def test_cli_send_text_inference_and_output(tmp_path):
    cfg = _cfg(tmp_path)
    _mk(cfg, CA)
    _mk(cfg, CB)
    rec = json.loads(_send(cfg, "--to", "conductor", f"about {CA}").output)
    assert rec["campaign"] == CA and rec["campaign_from"] == "text"
    ev = [e for e in E.read_since(cfg, 0) if e["type"] == "message"][-1]
    assert ev["campaign"] == CA and ev["spec_id"] == CA[:9] and ev["project"] == "Demo"
    r = _invoke(cfg, ["mailbox", "send", "--from", "steward:Demo", "--to", "conductor",
                      f"about {CA}"])
    assert r.exit_code == 0 and f"[{CA}, inferred from text]" in r.output
    r = _invoke(cfg, ["mailbox", "send", "--from", "steward:Demo", "--to", "conductor", "hi"])
    assert "inferred" not in r.output
    # explicit --campaign: stored as given, no campaign_from; ambiguous text: no stamp
    rec = json.loads(_send(cfg, "--to", "conductor", "--campaign", CB, f"about {CA}").output)
    assert rec["campaign"] == CB and "campaign_from" not in rec
    rec = json.loads(_send(cfg, "--to", "conductor", f"{CA} and {CB}").output)
    assert rec["campaign"] is None and "campaign_from" not in rec


def test_cli_send_empty_explicit_campaign_is_not_inferred(tmp_path):
    cfg = _cfg(tmp_path)
    _mk(cfg, CA)
    rec = json.loads(_send(cfg, "--to", "conductor", "--campaign", "", f"about {CA}").output)
    assert rec["campaign"] == "" and "campaign_from" not in rec
    n = len(M.read_inbox(cfg, "conductor"))
    r = _send(cfg, "--to", "conductor", "--campaign", "", "--no-campaign", "x")
    assert r.exit_code != 0 and "--no-campaign" in r.output
    assert len(M.read_inbox(cfg, "conductor")) == n


def test_cli_send_no_campaign_flag(tmp_path):
    cfg = _cfg(tmp_path)
    _mk(cfg, CA)
    rec = json.loads(_send(cfg, "--to", "conductor", "--no-campaign", f"about {CA}").output)
    assert rec["campaign"] is None and "campaign_from" not in rec
    n = len(M.read_inbox(cfg, "conductor"))
    r = _send(cfg, "--to", "conductor", "--no-campaign", "--campaign", CA, "x")
    assert r.exit_code != 0 and "--no-campaign" in r.output
    assert len(M.read_inbox(cfg, "conductor")) == n


def test_cli_send_re_inherits_campaign_from_sender_inbox(tmp_path):
    cfg = _cfg(tmp_path)
    _mk(cfg, CA)
    _mk(cfg, CB)
    q = M.send(cfg, to="conductor", frm="steward:Demo", text="question", kind="question",
               campaign=CA)
    # the conductor answers: the replied-to message sits in the sender's own inbox
    r = _invoke(cfg, ["mailbox", "send", "--from", "conductor", "--to", "steward:Demo",
                      "--kind", "answer", "--re", q["id"], "--json", "no ids here"])
    rec = json.loads(r.output)
    assert rec["campaign"] == CA and rec["campaign_from"] == "re"
    human = _invoke(cfg, ["mailbox", "send", "--from", "conductor", "--to", "steward:Demo",
                          "--kind", "answer", "--re", q["id"], "no ids here"])
    assert human.exit_code == 0
    assert f"[{CA}, inferred from re]" in " ".join(human.output.split())
    # --re outranks conflicting text
    rec = json.loads(_send(cfg, "--to", "conductor", "--re", q["id"], f"about {CB}").output)
    assert rec["campaign"] == CA and rec["campaign_from"] == "re"
    # explicit --campaign outranks --re
    rec = json.loads(_send(cfg, "--to", "conductor", "--re", q["id"], "--campaign", CB,
                           "x").output)
    assert rec["campaign"] == CB and "campaign_from" not in rec


def test_cli_send_no_campaign_flag_blocks_re_inheritance(tmp_path):
    cfg = _cfg(tmp_path)
    _mk(cfg, CA)
    q = M.send(cfg, to="conductor", frm="steward:Demo", text="question", kind="question",
               campaign=CA)
    rec = json.loads(_send(cfg, "--to", "conductor", "--re", q["id"], "--no-campaign",
                           "x").output)
    assert rec["campaign"] is None and "campaign_from" not in rec


def test_cli_send_re_with_unknown_campaign_falls_through(tmp_path):
    cfg = _cfg(tmp_path)
    _mk(cfg, CA)
    q = M.send(cfg, to="conductor", frm="steward:Demo", text="question", kind="question",
               campaign="SPEC-0123")
    r = _send(cfg, "--to", "conductor", "--re", q["id"], f"about {CA}")
    assert r.exit_code == 0, r.output
    rec = json.loads(r.output)
    assert rec["campaign"] == CA and rec["campaign_from"] == "text"
    rec = json.loads(_send(cfg, "--to", "conductor", "--re", q["id"], "plain").output)
    assert rec["campaign"] is None and "campaign_from" not in rec


def test_cli_send_re_with_non_string_campaign_falls_through(tmp_path):
    cfg = _cfg(tmp_path)
    _mk(cfg, CA)
    for bad in (123, ["x"], {"a": 1}):
        q = M.send(cfg, to="conductor", frm="steward:Demo", text="question", kind="question")
        path = M._inbox_path(cfg, "conductor")
        lines = path.read_text().splitlines()
        rec = json.loads(lines[-1])
        rec["campaign"] = bad                   # a hand-edited / foreign-writer line
        path.write_text("\n".join(lines[:-1] + [json.dumps(rec)]) + "\n")
        r = _send(cfg, "--to", "conductor", "--re", q["id"], f"about {CA}")
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        assert out["campaign"] == CA and out["campaign_from"] == "text"


def test_cli_send_re_campaign_with_non_object_state_falls_through(tmp_path):
    from nightwright.agentic import store as S
    cfg = _cfg(tmp_path)
    _mk(cfg, CA)
    _mk(cfg, CB)
    q = M.send(cfg, to="conductor", frm="steward:Demo", text="question", kind="question",
               campaign=CA)
    S.campaign_dir(cfg, CA).joinpath("state.json").write_text("[]\n")
    r = _send(cfg, "--to", "steward:Demo", "--re", q["id"], f"about {CB}")
    assert r.exit_code == 0, r.output
    rec = json.loads(r.output)
    assert rec["campaign"] == CB and rec["campaign_from"] == "text"


def test_cli_send_re_oserror_falls_through_to_text(tmp_path, monkeypatch):
    from nightwright.agentic import store as S
    cfg = _cfg(tmp_path)
    _mk(cfg, CA)
    _mk(cfg, CB)
    q = M.send(cfg, to="conductor", frm="steward:Demo", text="question", kind="question",
               campaign=CA)
    real = S.read_state

    def flaky(cfg, cid):
        if cid == CA:
            raise OSError("unreadable")
        return real(cfg, cid)

    monkeypatch.setattr(S, "read_state", flaky)
    r = _send(cfg, "--to", "conductor", "--re", q["id"], f"about {CB}")
    assert r.exit_code == 0, r.output
    rec = json.loads(r.output)
    assert rec["campaign"] == CB and rec["campaign_from"] == "text"


def test_cli_send_re_campaign_outside_the_store_is_not_inherited(tmp_path):
    from nightwright.agentic import store as S
    cfg = _cfg(tmp_path)
    _mk(cfg, CB)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "state.json").write_text(
        json.dumps({"id": "evil", "spec_id": "SPEC-9", "project": "P"}) + "\n")
    root = S.campaigns_dir(cfg)
    root.mkdir(parents=True, exist_ok=True)
    (root / "LINKED").symlink_to(outside, target_is_directory=True)
    escaped = os.path.relpath(outside, root)
    assert escaped.startswith("..")
    for bad in (str(outside), escaped, "LINKED"):
        q = M.send(cfg, to="steward:Demo", frm="conductor", text="question", kind="question",
                   campaign=bad)
        before = sorted(p.name for p in outside.iterdir())
        r = _send(cfg, "--to", "conductor", "--re", q["id"], f"about {CB}")
        assert r.exit_code == 0, r.output
        rec = json.loads(r.output)
        assert rec["campaign"] == CB and rec["campaign_from"] == "text"
        assert sorted(p.name for p in outside.iterdir()) == before
    assert sorted(p.name for p in outside.iterdir()) == ["state.json"]


def test_cli_send_unknown_re_falls_through_to_text(tmp_path):
    cfg = _cfg(tmp_path)
    _mk(cfg, CA)
    r = _send(cfg, "--to", "conductor", "--re", "conductor:99", f"about {CA}")
    assert r.exit_code == 0, r.output
    rec = json.loads(r.output)
    assert rec["campaign"] == CA and rec["campaign_from"] == "text"
    rec = json.loads(_send(cfg, "--to", "conductor", "--re", "nope:1", "plain").output)
    assert rec["campaign"] is None


def test_find_message_streams_only_the_inbox_named_by_the_id(tmp_path):
    cfg = _cfg(tmp_path)
    q = M.send(cfg, to="steward:Demo", frm="conductor", text="why?", kind="question",
               campaign="C1")
    assert q["id"] == "steward:Demo:1"          # address itself contains a colon
    (M.mailbox_dir(cfg) / "other.jsonl").mkdir()  # a full-history scan would try to open this
    found = M.find_message(cfg, q["id"])
    assert found["id"] == q["id"] and found["campaign"] == "C1"
    assert M.find_message(cfg, "steward:Demo:99") is None
    assert M.find_message(cfg, "nope") is None
    assert M.find_message(cfg, None) is None
    # a seq past the interpreter's int digit limit must miss, not raise
    assert M.find_message(cfg, "steward:Demo:" + "9" * 5000) is None
    assert M.find_message(cfg, "steward:Demo:" + "1" * (M._REPLY_SEQ_MAX_DIGITS + 1)) is None


def test_cli_send_is_append_only(tmp_path):
    cfg = _cfg(tmp_path)
    _mk(cfg, CA)
    _send(cfg, "--to", "conductor", "first")
    before = M._inbox_path(cfg, "conductor").read_bytes()
    _send(cfg, "--to", "conductor", f"second {CA}")
    assert M._inbox_path(cfg, "conductor").read_bytes().startswith(before)

