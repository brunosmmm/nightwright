"""SPEC-0429: answering escalations from the dashboard over real sockets. Escalations are opened
through the real CLI (`nw queue escalate --yes`); every web answer is twin-checked against `nw
escalations answer --yes` on a copy of the same data dir. SPEC-0428's write checks (CSRF,
same-origin, JSON) are exercised here too, on the routes they protect."""
import copy
import datetime as dt
import hashlib
import http.client
import json
import re
import shutil
import threading

import pytest
import yaml
from click.testing import CliRunner

import waits
from fixtures import dashboard_data as F
from nightwright.agentic import escalations as ESC
from nightwright.agentic import store as S
from nightwright.agentic.dashboard import answers as ANS
from nightwright.agentic.dashboard import server as DS
from nightwright.agentic.dashboard import snapshot as SN
from nightwright.cli import cli

pytestmark = pytest.mark.fast

CH, TX, OV, WD, AU, RS = (f"SPEC-09{n}-20261104-a00{n % 10}" for n in (61, 62, 63, 64, 65, 66))
AGENTIC = {"lease_ttl_s": 120, "escalations": {"notify": "none", "auto_apply_after_s": 1}}
TS = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?[-+]\d\d:\d\d")


def cfg_for(root):
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "config.yaml").write_text(yaml.safe_dump({"agentic": AGENTIC}))
    cfg = F.make_cfg(root / "data", root / "config")
    cfg["agentic"] = copy.deepcopy(AGENTIC)
    return cfg


def nw(cfg, monkeypatch, *args):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    monkeypatch.delenv("NW_MAILBOX_ADDR", raising=False)
    r = CliRunner().invoke(cli, list(args))
    assert r.exit_code == 0, r.output
    return r.output


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Six IMPLEMENTING campaigns, each escalated through the real CLI (so each is PARKED
    `human_decision`): ESC-0001 choose, -0002 text, -0003 override (stored review), -0004
    withdrawn, -0005 auto-applied (SPEC-0425), -0006 a spare."""
    cfg = cfg_for(tmp_path / "a")
    (tmp_path / "a" / "data").mkdir(parents=True)
    now = dt.datetime.now().astimezone()
    for cid in (CH, TX, OV, WD, AU, RS):
        F._campaign(cfg, cid, "IMPLEMENTING", now,
                    **({"last_review": F.review()} if cid == OV else {}))
    esc = ["queue", "escalate", "--yes"]
    nw(cfg, monkeypatch, *esc, CH, "--question", "Ship?", "--option", "resume", "--option",
       "abandon", "--recommend", "1", "--topic", "scope", "--fact", "loop 3/3")
    nw(cfg, monkeypatch, *esc, TX, "--question", "Which API?", "--option", "REST", "--topic",
       "product")
    nw(cfg, monkeypatch, *esc, OV, "--question", "Waive?", "--option", "resume", "--topic",
       "scope")
    nw(cfg, monkeypatch, *esc, WD, "--question", "Withdraw me", "--option", "a", "--topic",
       "scope")
    nw(cfg, monkeypatch, *esc, AU, "--question", "Retry?", "--option", "retry", "--option",
       "park", "--recommend", "1", "--topic", "recovery")
    nw(cfg, monkeypatch, *esc, RS, "--question", "Spare?", "--option", "x", "--topic", "scope")
    nw(cfg, monkeypatch, "escalations", "withdraw", "ESC-0004", "--reason", "moot", "--yes")
    assert waits.until(lambda: ESC._until_s(ESC.read(cfg, "ESC-0005")["auto"]["due_at"],
                                            dt.datetime.now().astimezone()) <= 0)
    nw(cfg, monkeypatch, "escalations", "answer", "ESC-0005", "--auto", "--by", "conductor",
       "--yes")
    twin_root = tmp_path / "b"
    shutil.copytree(tmp_path / "a" / "data", twin_root / "data")
    twin = cfg_for(twin_root)
    srv = Srv(cfg)
    yield {"cfg": cfg, "twin": twin, "srv": srv, "mp": monkeypatch}
    srv.close()


class Srv:
    def __init__(self, cfg):
        self.cfg = cfg
        self.srv = DS.make_server(cfg, "127.0.0.1:0",
                                  cache=SN.SnapshotCache(cfg, refresh_s=0.05, max_stale_s=3600))
        self.port = self.srv.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.t = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05},
                                  daemon=True)
        self.t.start()
        self.csrf = re.search(r'name="nw-csrf" content="([^"]+)"',
                              self.req("GET", "/")[2].decode()).group(1)

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            data = None if body is None else (body if isinstance(body, bytes)
                                              else json.dumps(body).encode())
            c.request(method, path, body=data,
                      headers={"Host": f"127.0.0.1:{self.port}", **(headers or {})})
            r = c.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            c.close()

    def post(self, path, body, headers=None, *, csrf=True, origin=True, ctype=True):
        h = {}
        if ctype:
            h["Content-Type"] = "application/json"
        if origin:
            h["Origin"] = self.origin
        if csrf:
            h["X-NW-CSRF"] = self.csrf
        h.update(headers or {})
        st, hd, raw = self.req("POST", path, body, h)
        return st, json.loads(raw) if raw else None

    def answer(self, esc, body):
        st, plan = self.post(f"/api/escalations/{esc}/plan", body)
        assert st == 200, plan
        st, out = self.post(f"/api/escalations/{esc}/answer",
                            {**body, "plan_token": plan["plan_token"]})
        return st, out, plan

    def audit(self):
        p = S.agentic_dir(self.cfg) / "dashboard" / "audit.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def close(self):
        self.srv.shutdown()
        waits.join(self.t, 10)
        self.srv.server_close()


def tree(cfg):
    root = S.agentic_dir(cfg).parent
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()
            and "agentic/dashboard" not in str(p.relative_to(root))}


def mask(o):
    if isinstance(o, dict):
        return {k: mask(v) for k, v in o.items() if k not in ("seq", "id")}
    if isinstance(o, list):
        return [mask(x) for x in o]
    return TS.sub("<ts>", o) if isinstance(o, str) else o


def written(cfg, esc, cid):
    """What an answer writes: the record, the ledger `human_decision` + `note`, the overrides
    and the `escalation_answered` event of this escalation."""
    rec = ESC.read(cfg, esc)
    led = [e for e in S.read_ledger(cfg, cid) if e["type"] in ("human_decision", "note")]
    ov = S.read_state(cfg, cid).get("human_overrides")
    evp = S.agentic_dir(cfg) / "events.jsonl"
    evs = [json.loads(x) for x in evp.read_text().splitlines()]
    ev = [e for e in evs if e["type"] == "escalation_answered"
          and e["data"]["escalation_id"] == esc]
    return mask({"record": rec, "ledger": led, "overrides": ov, "events": ev})


@pytest.mark.parametrize("esc,cid,body,cli_args", [
    ("ESC-0001", CH, {"form": "choose", "choose": 1}, ["--choose", "1"]),
    ("ESC-0002", TX, {"form": "text", "text": "Go with REST.", "note": "from the phone"},
     ["--text", "Go with REST.", "--note", "from the phone"]),
    ("ESC-0003", OV, {"form": "override", "waive": ["conformance_reviewer#0"],
                      "attest": [F.UNMET_AC], "reason": "accepted risk"},
     ["--waive", "conformance_reviewer#0", "--attest", F.UNMET_AC, "--reason", "accepted risk"]),
    ("ESC-0005", AU, {"form": "choose", "choose": 2}, ["--choose", "2"]),   # reverses the auto
])
def test_web_answer_writes_exactly_what_the_cli_writes(world, esc, cid, body, cli_args):
    s = world["srv"]
    st, out, plan = s.answer(esc, body)
    assert st == 200, out
    assert out["record"]["status"] == "answered" and out["record"]["answered_by"] == "human"
    assert plan["plan"]["summary"] == out["summary"]
    nw(world["twin"], world["mp"], "escalations", "answer", esc, *cli_args, "--yes")
    web, cli_ = written(world["cfg"], esc, cid), written(world["twin"], esc, cid)
    assert web == cli_
    assert len(web["events"]) == (2 if esc == "ESC-0005" else 1)    # 0005: auto + reversal
    if esc == "ESC-0005":
        assert web["record"]["answer_history"] and web["events"][-1]["data"]["reverses_auto"]
        assert "Reverses the auto-applied answer" in plan["effect"]
        assert plan["plan"]["reverses_summary"].startswith("chose 1 (retry)")
    if esc == "ESC-0003":
        assert S.read_state(world["cfg"], OV)["human_overrides"]["waive"][0]["id"] == \
            "conformance_reviewer#0"


def test_conflicts_are_409_and_write_nothing(world):
    s, cfg = world["srv"], world["cfg"]
    st, out, plan = s.answer("ESC-0001", {"form": "choose", "choose": 1})
    assert st == 200
    before = tree(cfg)
    st, b = s.post("/api/escalations/ESC-0001/plan", {"form": "choose", "choose": 2})
    assert st == 409 and b["status"] == "answered" and b["by"] == "human" and b["at"]
    st, b = s.post("/api/escalations/ESC-0001/answer",
                   {"form": "choose", "choose": 1, "plan_token": plan["plan_token"]})
    assert st == 409 and b["status"] == "answered"
    st, b = s.post("/api/escalations/ESC-0004/plan", {"form": "choose", "choose": 1})
    assert st == 409 and b["status"] == "withdrawn" and b["by"] == "conductor"
    # the campaign left the park by another route: no longer applicable
    stt = S.read_state(cfg, RS)
    stt.update(state="IMPLEMENTING", park=None)
    S.write_state(cfg, RS, stt)
    before = tree(cfg)
    st, b = s.post("/api/escalations/ESC-0006/plan", {"form": "choose", "choose": 1})
    assert st == 409 and b["status"] == "not_applicable" and b["campaign_state"] == "IMPLEMENTING"
    # a second reversal of an auto answer
    assert s.answer("ESC-0005", {"form": "choose", "choose": 2})[0] == 200
    before = tree(cfg)
    st, b = s.post("/api/escalations/ESC-0005/plan", {"form": "choose", "choose": 1})
    assert st == 409 and b["status"] == "answered"
    assert tree(cfg) == before
    st, b = s.post("/api/escalations/ESC-0099/plan", {"form": "choose", "choose": 1})
    assert st == 404


def test_a_plan_made_before_another_answer_is_a_409_not_a_write(world):
    s, cfg = world["srv"], world["cfg"]
    st, plan = s.post("/api/escalations/ESC-0001/plan", {"form": "choose", "choose": 1})
    nw(world["cfg"], world["mp"], "escalations", "answer", "ESC-0001", "--choose", "2", "--yes")
    before = tree(cfg)
    st, b = s.post("/api/escalations/ESC-0001/answer",
                   {"form": "choose", "choose": 1, "plan_token": plan["plan_token"]})
    assert st == 409 and b["status"] == "answered"
    assert ESC.read(cfg, "ESC-0001")["answer"]["choice"] == 2
    assert tree(cfg) == before


def test_plan_token_and_form_errors_are_refused_and_write_nothing(world, monkeypatch):
    s, cfg = world["srv"], world["cfg"]
    before = tree(cfg)
    body = {"form": "choose", "choose": 1}
    st, plan = s.post("/api/escalations/ESC-0001/plan", body)
    assert st == 200 and tree(cfg) == before                       # planning writes nothing
    tok = plan["plan_token"]
    cases = [
        ({**body}, 400),                                            # no token
        ({**body, "plan_token": "garbage"}, 400),
        ({"form": "choose", "choose": 2, "plan_token": tok}, 400),  # token for another body
        ({**body, "note": "n", "plan_token": tok}, 400),
    ]
    for b, want in cases:
        assert s.post("/api/escalations/ESC-0001/answer", b)[0] == want, b
    st, b = s.post("/api/escalations/ESC-0002/answer", {**body, "plan_token": tok})
    assert st == 400, b                                             # another record
    monkeypatch.setattr(ANS, "PLAN_TTL_S", -1.0)
    st, plan2 = s.post("/api/escalations/ESC-0001/plan", body)
    st, b = s.post("/api/escalations/ESC-0001/answer", {**body, "plan_token": plan2["plan_token"]})
    assert st == 409 and b["status"] == "plan_expired"
    for b in ({"form": "choose", "choose": 9}, {"form": "override", "waive": ["nope#0"],
                                                 "reason": "r"},
              {"form": "override", "waive": ["conformance_reviewer#0"]},
              {"form": "choose", "choose": 1, "text": "x"}, {"form": "vote"}, [1, 2],
              {"form": "text", "text": "   "}):
        st, out = s.post("/api/escalations/ESC-0001/plan" if "waive" not in str(b)
                         else "/api/escalations/ESC-0003/plan", b)
        assert st == 400 and out["error"], (b, out)
    assert tree(cfg) == before


def test_write_checks_refuse_and_audit_without_writing(world):
    s, cfg = world["srv"], world["cfg"]
    before = tree(cfg)
    p, body = "/api/escalations/ESC-0001/plan", {"form": "choose", "choose": 1}
    assert s.post(p, body, csrf=False)[1]["reason"] == "bad_csrf"
    assert s.post(p, body, {"X-NW-CSRF": s.csrf[:-2] + "xx"})[1]["reason"] == "bad_csrf"
    other = DS.A.Csrf().mint("local")                               # another process's secret
    assert s.post(p, body, {"X-NW-CSRF": other})[0] == 403
    assert s.post(p, body, {"Origin": "http://evil.example"})[1]["reason"] == "bad_origin"
    assert s.post(p, body, origin=False)[1]["reason"] == "no_origin"
    assert s.post(p, body, {"Content-Type": "text/plain"})[0] == 415
    assert s.post(p, body, origin=False, headers={"Referer": "http://evil.example/"})[0] == 403
    assert s.post(p, body, origin=False, headers={"Referer": s.origin + "/"})[0] == 200
    assert s.req("GET", p)[0] == 405
    assert tree(cfg) == before
    reasons = [a.get("reason") for a in s.audit() if a["action"] == "refused"]
    assert reasons.count("bad_csrf") == 3 and "bad_origin" in reasons and "no_origin" in reasons


def test_every_plan_and_answer_is_audited_with_the_identity(world):
    s = world["srv"]
    s.answer("ESC-0001", {"form": "choose", "choose": 1})
    s.post("/api/escalations/ESC-0001/plan", {"form": "choose", "choose": 1})
    rows = [(a["action"], a["esc_id"], a["form"], a["result"], a["login"], a["node"])
            for a in s.audit() if a["action"] in ("plan", "answer")]
    assert rows == [("plan", "ESC-0001", "choose", "ok", "local", "local"),
                    ("answer", "ESC-0001", "choose", "ok", "local", "local"),
                    ("plan", "ESC-0001", "choose", "409", "local", "local")]
    ok = [a for a in s.audit() if a["action"] == "answer"][0]
    assert ok["summary"] == "chose 1 (resume)" and ok["node_ip"] == "127.0.0.1"


def test_the_inbox_lists_auto_applied_answers_for_reversal(world):
    s = world["srv"]
    doc = json.loads(s.req("GET", "/api/snapshot")[2])
    auto = doc["inbox"]["auto_applied"]
    assert [e["id"] for e in auto] == ["ESC-0005"]
    assert auto[0]["reversible"] and auto[0]["answerable"]
    assert auto[0]["auto_answer"].startswith("chose 1 (retry)")
    assert all(v.startswith("Reverses the auto-applied answer") for v in auto[0]["effects"].values())
    assert "ESC-0005" not in [e["id"] for e in doc["inbox"]["escalations"]]
    assert s.answer("ESC-0005", {"form": "choose", "choose": 2})[0] == 200
    assert waits.until(lambda: json.loads(s.req("GET", "/api/snapshot")[2])["inbox"]
                       ["auto_applied"] == [])
