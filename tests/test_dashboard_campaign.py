"""SPEC-0430: the campaign drilldown (`GET /api/campaigns/<cid>`) and the live run tail (`GET
/api/campaigns/<cid>/tail`) over real sockets, against `nw campaign log` / `nw campaign tail`."""
import hashlib
import http.client
import json
import socket
import subprocess
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

import waits
from fixtures.dashboard_data import ACTIVE, seed
from nightwright.agentic import store as S
from nightwright.agentic.dashboard import campaign as CP
from nightwright.agentic.dashboard import server as DS
from nightwright.cli import cli

FIX = Path(__file__).parent / "fixtures" / "stream_json" / "ok_text.jsonl"


@pytest.fixture
def world(tmp_path):
    fx = seed(tmp_path)
    cfg = fx["cfg"]
    cfg["agentic"]["dashboard"] = {"tail_poll_s": 0.05}
    runs = S.runs_dir(cfg, ACTIVE)
    for name in ("001-implementer", "002-reviewer"):
        (runs / name).mkdir(parents=True)
        (runs / name / "request.json").write_text(json.dumps({"persona": name[4:],
                                                              "model": "opus",
                                                              "max_budget_usd": 5}))
    (runs / "001-implementer" / "exit.json").write_text("{}")
    sleeper = subprocess.Popen(["sleep", "300"])
    (runs / "002-reviewer" / "proc.json").write_text(json.dumps({"wrapper_pid": sleeper.pid}))
    (runs / "002-reviewer" / "stream.jsonl").write_text("")
    S.append_event(cfg, ACTIVE, "note", {"text": "hello <b>x</b>\x1b[31m", "author": "t"}, by="t")
    srv = DS.make_server(cfg, "127.0.0.1:0")
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield {"cfg": cfg, "srv": srv, "port": srv.server_address[1], "run": runs / "002-reviewer",
           "fx": fx}
    sleeper.kill()
    sleeper.wait()
    srv.shutdown()
    waits.join(t, 10)
    srv.server_close()


def get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        c.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def test_drilldown_matches_the_cli_views(world, monkeypatch):
    st, body = get(world["port"], f"/api/campaigns/{ACTIVE}")
    assert st == 200
    d = json.loads(body)
    assert d["schema"] == "nw.dashboard.campaign.v1" and d["campaign"] == ACTIVE
    assert d["state"]["state"] == "IMPLEMENTING" and d["state"]["cap_usd"] == 10.0
    assert d["current_run"] == "002-reviewer" and d["running"] is True
    assert [r["run"] for r in d["runs"]] == ["002-reviewer", "001-implementer"]
    monkeypatch.setattr("nightwright.cli.load_config", lambda: world["cfg"])
    r = CliRunner().invoke(cli, ["campaign", "log", ACTIVE, "--json"])
    events = json.loads(r.output)["events"]
    from nightwright import render as R
    assert [x["text"] for x in d["timeline"]] == [CP.clean(R.campaign_event(e).plain)
                                                  for e in events[-CP.TIMELINE_N:]]
    assert "\x1b" not in d["timeline"][-1]["text"] and "<b>x</b>" in d["timeline"][-1]["text"]
    # SPEC-0433: the whole ledger as the story plus its raw text per seq
    from nightwright.agentic import timeline as TLN
    assert d["story"]["schema"] == TLN.SCHEMA and d["story"]["events"] == len(events)
    assert d["raw"] == {str(e["seq"]): CP.clean(R.campaign_event(e).plain) for e in events}
    lines = [ln for _, _, ln in TLN.iter_lines(d["story"])]
    note = [ln for ln in lines if ln["kind"] == "note"][-1]
    assert note["text"] == "Note (t): hello <b>x</b>[31m" and "\x1b" not in body.decode()
    assert {s for ln in lines for s in ln["seqs"]} | {
        t["seq"] for lp in d["story"]["loops"] for g in lp["groups"] for t in g["transitions"]
    } == {e["seq"] for e in events}


def test_a_story_failure_fails_alone(world, monkeypatch):
    def boom(*a, **k):
        raise ValueError("no story")
    monkeypatch.setattr(CP.STORY, "story", boom)
    d = CP.build(world["cfg"], ACTIVE)
    assert d["story"] == {"error": "ValueError: no story"} == d["raw"]
    assert d["timeline"] and d["runs"] and d["state"]["state"] == "IMPLEMENTING"


@pytest.mark.parametrize("cid", ["nope-123", "..", "a/b", "%2e%2e", "x" * 200])
def test_unknown_or_invalid_campaigns_are_404(world, cid):
    assert get(world["port"], f"/api/campaigns/{cid}")[0] == 404
    assert get(world["port"], f"/api/campaigns/{cid}/tail")[0] == 404


def test_tail_streams_appended_lines_then_ends_and_frees_its_slot(world):
    port, rd = world["port"], world["run"]
    s = socket.create_connection(("127.0.0.1", port), timeout=waits.WAIT_S)
    s.sendall(f"GET /api/campaigns/{ACTIVE}/tail HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n"
              .encode())
    buf = [b""]
    done = []

    def read():
        while True:
            try:
                d = s.recv(65536)
            except OSError:
                d = b""
            if not d:
                done.append(True)
                return
            buf[0] += d

    threading.Thread(target=read, daemon=True).start()
    assert waits.until(lambda: b"event: start" in buf[0])
    assert buf[0].startswith(b"HTTP/1.1 200") and b"text/event-stream" in buf[0]
    assert waits.until(lambda: world["srv"].client_count() == 1)
    src = FIX.read_text().splitlines()
    with open(rd / "stream.jsonl", "a") as f:
        f.write("\n".join(src[:-1]) + "\n")
    assert waits.until(lambda: b"event: lines" in buf[0])
    with open(rd / "stream.jsonl", "a") as f:
        f.write(src[-1] + "\n")
    (rd / "exit.json").write_text("{}")
    assert waits.until(lambda: done), "the tail did not end after the run ended"
    assert b"event: end" in buf[0]
    lines = []
    for block in buf[0].split(b"\n\n"):
        if b"event: lines" in block:
            lines += json.loads(block.split(b"data: ", 1)[1])["lines"]
    from nightwright.agentic import tail as TL
    r = TL.Renderer(budget_usd=5, price_lookup=TL._price_lookup(world["cfg"]))
    want = [t.plain for ln in src for t in r.feed_line(ln)]
    assert lines == want and any(x.startswith("■ result") for x in lines)
    assert waits.until(lambda: world["srv"].client_count() == 0)


def test_serving_drilldowns_and_tails_writes_nothing(world):
    root = Path(world["cfg"]["data_dir"])

    def tree():
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*")
                if p.is_file() and "agentic/dashboard" not in str(p)}
    (world["run"] / "exit.json").write_text("{}")
    before = tree()
    for _ in range(3):
        assert get(world["port"], f"/api/campaigns/{ACTIVE}")[0] == 200
        assert get(world["port"], f"/api/campaigns/{ACTIVE}/tail?run=001")[0] == 200
    assert tree() == before
