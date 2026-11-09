"""SPEC-0361 / SPEC-0363: the herdr reporter — best-effort client (timeout, breaker, gating), argv builders,
`nw herdr watch` tokens / notifications / cleanup against a fake `herdr` on PATH, pane
placement recorded by the followers, import isolation and `HERDR_*` stripping for persona runs.

The real herdr is never reached: an autouse fixture drops every ambient `HERDR_*` and puts only
the fake's directory (plus the interpreter's) on PATH."""
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright.agentic import conductor_cost as CC
from nightwright.agentic import events as E
from nightwright.agentic import herdr as H
from nightwright.agentic import herdr_watch as HW
from nightwright.agentic import mailbox as M
from nightwright.agentic import runproc as RP
from nightwright.cli import cli
import waits
from test_agentic_events import _campaign, _cfg, _ev, _set_state

pytestmark = pytest.mark.integration

FAKE_SRC = Path(__file__).parent / "fixtures" / "fake_herdr.py"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A fake `herdr` (argv -> one JSON line in the log) and a herdr-like env."""
    for k in [k for k in os.environ if k.startswith("HERDR_")]:
        monkeypatch.delenv(k)
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    exe = bindir / "herdr"
    exe.write_text(f"#!{sys.executable}\n" + FAKE_SRC.read_text())
    exe.chmod(0o755)
    log = tmp_path / "herdr.log"
    monkeypatch.setenv("FAKE_HERDR_LOG", str(log))
    monkeypatch.setenv("FAKE_HERDR_MODE", "ok")
    monkeypatch.setenv("FAKE_HERDR_STATE", str(tmp_path / "herdr-state.json"))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{Path(sys.executable).parent}")
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "ws-self")
    monkeypatch.setenv("HERDR_PANE_ID", "pane-self")

    class F:
        path = log
        bin = bindir

        @staticmethod
        def calls():
            return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    return F


@pytest.fixture(autouse=True)
def _no_ambient_herdr(monkeypatch):
    for k in [k for k in os.environ if k.startswith("HERDR_")]:
        monkeypatch.delenv(k)


def _opt(args, flag):
    return args[args.index(flag) + 1]


def _tokens(args):
    toks = {}
    for i, a in enumerate(args):
        if a == "--token":
            name, _, val = args[i + 1].partition("=")
            toks[name] = val
        elif a == "--clear-token":
            toks[args[i + 1]] = None
    return toks


# ---- client --------------------------------------------------------------------------------

def test_gated_off_spawns_nothing(fake, monkeypatch):
    monkeypatch.delenv("HERDR_ENV")
    c = H.Client()
    assert H.unavailable() and "HERDR_ENV" in H.unavailable()
    assert c.call(["notification", "show", "x"]) is False
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))      # no herdr on PATH
    assert "PATH" in H.unavailable()
    assert c.call(["notification", "show", "x"]) is False
    assert c.calls == 0 and not fake.path.exists()


def test_ok_fail_and_hang(fake, monkeypatch):
    c = H.Client(timeout_s=0.5)
    assert H.TIMEOUT_S == 2.0 and H.Client().timeout_s == 2.0
    assert c.call(["pane", "report-metadata", "p1"]) is True
    assert fake.calls() == [["pane", "report-metadata", "p1"]]
    monkeypatch.setenv("FAKE_HERDR_MODE", "fail")
    assert c.call(["x"]) is False
    monkeypatch.setenv("FAKE_HERDR_MODE", "hang")
    t0 = time.monotonic()
    assert c.call(["y"]) is False                      # timed out, not raised
    assert time.monotonic() - t0 < 10


def test_module_call_never_raises(fake, monkeypatch):
    monkeypatch.setattr(H, "_default", None)
    assert H.call(["a"]) is True
    monkeypatch.setattr(H.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    assert H.call(["b"]) is False


def test_breaker_opens_after_five_and_probes_after_cooldown(fake, monkeypatch):
    monkeypatch.setenv("FAKE_HERDR_MODE", "fail")
    now = [100.0]
    err = io.StringIO()
    c = H.Client(clock=lambda: now[0], err=err)
    for _ in range(5):
        assert c.call(["f"]) is False
    assert c.calls == 5 and c.breaker_open()
    assert err.getvalue().count("\n") == 1 and "pausing herdr calls for 60s" in err.getvalue()
    assert c.call(["f"]) is False and c.calls == 5           # open: nothing spawned
    now[0] += 61
    monkeypatch.setenv("FAKE_HERDR_MODE", "ok")
    assert c.call(["probe"]) is True and c.calls == 6 and not c.breaker_open()
    assert len(fake.calls()) == 6


def test_breaker_reopens_on_failed_probe(fake, monkeypatch):
    monkeypatch.setenv("FAKE_HERDR_MODE", "fail")
    now = [0.0]
    c = H.Client(clock=lambda: now[0], err=io.StringIO())
    for _ in range(5):
        c.call(["f"])
    now[0] += 61
    assert c.call(["probe"]) is False and c.breaker_open() and c.calls == 6


def test_seq_strictly_increasing_and_epoch_seeded():
    c = H.Client()
    seqs = [c.next_seq() for _ in range(50)]
    assert all(b > a for a, b in zip(seqs, seqs[1:]))
    assert seqs[0] >= time.time_ns() // 1_000_000 - 5000
    assert H.Client().next_seq() > seqs[0] - 1              # a new client never goes back


def test_argv_builders():
    a = H.metadata_args("pane", "p9", {"nw_role": "conductor", "nw_cost": ""}, seq=7, ttl_ms=30000)
    assert a[:3] == ["pane", "report-metadata", "p9"]
    assert _opt(a, "--source") == "nw" and _opt(a, "--seq") == "7" and _opt(a, "--ttl-ms") == "30000"
    assert _tokens(a) == {"nw_role": "conductor", "nw_cost": None}
    c = H.clear_args("workspace", "w1", ["nw_queue", "nw_attn"], seq=8)
    assert c[:3] == ["workspace", "report-metadata", "w1"] and _tokens(c) == {"nw_queue": None, "nw_attn": None}
    assert H.notify_args("t", "b", "done") == ["notification", "show", "t", "--body", "b",
                                               "--sound", "done"]
    with pytest.raises(ValueError):
        H.notify_args("t", "b", "loud")
    with pytest.raises(ValueError):
        H.metadata_args("tab", "x", {}, seq=1, ttl_ms=1)
    d = H.metadata_args("pane", "w7:p2", {}, seq=9, ttl_ms=30000, display_agent="claude · x")
    assert d[:3] == ["pane", "report-metadata", "w7:p2"] and _opt(d, "--display-agent") == "claude · x"
    assert "--token" not in d and "--clear-token" not in d
    cd = H.clear_args("pane", "w7:p2", [], seq=10, clear_display_agent=True)
    assert cd[:3] == ["pane", "report-metadata", "w7:p2"] and "--clear-display-agent" in cd
    with pytest.raises(ValueError):                     # herdr has no workspace display agent
        H.metadata_args("workspace", "w1", {}, seq=1, ttl_ms=1, display_agent="x")
    with pytest.raises(ValueError):
        H.clear_args("workspace", "w1", [], seq=1, clear_display_agent=True)


# ---- placement ------------------------------------------------------------------------------

def _herdr_env(pane, ws="ws-1", sid=None):
    return {"HERDR_ENV": "1", "HERDR_PANE_ID": pane, "HERDR_WORKSPACE_ID": ws,
            "HERDR_TAB_ID": "t1", **({"CLAUDE_CODE_SESSION_ID": sid} if sid else {})}


def test_record_herdr_pane(tmp_path):
    cfg = _cfg(tmp_path)
    assert CC.record_herdr_pane(cfg, "conductor", {}) is None
    assert CC.record_herdr_pane(cfg, "conductor", {"HERDR_ENV": "0", "HERDR_PANE_ID": "p"}) is None
    assert CC.herdr_panes(cfg) == {}
    rec = CC.record_herdr_pane(cfg, "conductor", _herdr_env("p1", sid="s1"))
    assert rec["pane_id"] == "p1" and rec["workspace_id"] == "ws-1" and rec["session_id"] == "s1"
    assert rec["pid"] == os.getpid() and CC.placement_live(rec)
    CC.record_herdr_pane(cfg, "conductor", _herdr_env("p2"))
    CC.record_herdr_pane(cfg, "steward:Meta-Tools", _herdr_env("p3"))
    assert {r: p["pane_id"] for r, p in CC.herdr_panes(cfg).items()} == {
        "conductor": "p2", "steward:Meta-Tools": "p3"}
    assert CC.sessions(cfg, "conductor") == []              # the session registry is untouched


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_placement_liveness():
    assert not CC.placement_live({"pid": _dead_pid()})
    assert not CC.placement_live({"pid": None})
    assert CC.placement_live({"pid": os.getpid(), "start_ticks": CC._start_ticks(os.getpid())})
    assert not CC.placement_live({"pid": os.getpid(), "start_ticks": -1})   # pid reused


def test_followers_record_their_pane(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    for k, v in _herdr_env("pane-c", sid="sess-c").items():
        monkeypatch.setenv(k, v)
    E.follow(cfg, "conductor", poll_s=0, out=io.StringIO(), stop=lambda: True, code_root=None)
    assert CC.herdr_panes(cfg)["conductor"]["pane_id"] == "pane-c"
    monkeypatch.setattr("nightwright.rules.projects_payload",
                        lambda cfg: {"projects": [{"name": "Meta-Tools"}]})
    monkeypatch.setenv("HERDR_PANE_ID", "pane-s")
    M.follow(cfg, "steward:Meta-Tools", out=io.StringIO(), poll_s=0, stop=lambda: True,
             code_root=None)
    assert CC.herdr_panes(cfg)["steward:Meta-Tools"]["pane_id"] == "pane-s"
    monkeypatch.delenv("HERDR_ENV")
    monkeypatch.setenv("HERDR_PANE_ID", "pane-x")
    E.follow(cfg, "conductor", poll_s=0, out=io.StringIO(), stop=lambda: True, code_root=None)
    assert CC.herdr_panes(cfg)["conductor"]["pane_id"] == "pane-c"   # not in herdr: unchanged


# ---- watcher: tokens ------------------------------------------------------------------------

def _queue(cfg):
    a = _campaign(cfg, "SPEC-0901-20261101-aaaa", state="IMPLEMENTING")
    _set_state(cfg, a, cost={"total_usd": 1.2, "cap_usd": 5.0},
               counters={"loops": 2, "runs": 3})
    _campaign(cfg, "SPEC-0902-20261101-bbbb", state="QUEUED")
    p = _campaign(cfg, "SPEC-0903-20261101-cccc", state="PARKED")
    _set_state(cfg, p, park={"reason": "cost_cap", "detail": "d", "resume_to": "IMPLEMENTING"})
    return a


def _place(cfg, reader, pane, ws, pid=None):
    data = CC.herdr_panes(cfg)
    pid = pid or os.getpid()
    data[reader] = {"pane_id": pane, "workspace_id": ws, "session_id": None, "pid": pid,
                    "start_ticks": CC._start_ticks(pid)}
    CC.conductor_dir(cfg).mkdir(parents=True, exist_ok=True)
    (CC.conductor_dir(cfg) / "herdr_panes.json").write_text(json.dumps(data))


def _metadata(calls):
    return {(c[0], c[2]): c for c in calls if len(c) > 2 and c[1] == "report-metadata"}


def _queue_two_projects(cfg):
    """Meta-Tools: one implementing (+ queued + parked); Other: one reviewing, its own cost."""
    _queue(cfg)
    o = _campaign(cfg, "SPEC-0950-20261101-ffff", state="REVIEWING")
    _set_state(cfg, o, project="Other", cost={"total_usd": 7.5, "cap_usd": 20.0},
               counters={"loops": 4, "runs": 5})


def test_push_targets_only_nw_panes_and_workspaces(fake, tmp_path):
    cfg = _cfg(tmp_path)
    _queue_two_projects(cfg)
    _place(cfg, "conductor", "pane-cond", "ws-a")
    _place(cfg, "steward:Meta-Tools", "pane-stew", "ws-a")             # the conductor's workspace
    _place(cfg, "steward:Other", "pane-other", "ws-b")
    _place(cfg, "steward:Dead", "pane-dead", "ws-dead", pid=_dead_pid())
    _place(cfg, "mailbox:human", "pane-mbox", "ws-c")                  # not a reader we report to
    w = HW.Watcher(cfg, H.Client(), interval_s=10)                     # own env: ws-self/pane-self
    assert w.push() == 5
    md = _metadata(fake.calls())
    assert set(md) == {("workspace", "ws-a"), ("workspace", "ws-b"), ("pane", "pane-cond"),
                       ("pane", "pane-stew"), ("pane", "pane-other")}  # never self / dead / mbox
    for args in md.values():
        assert _opt(args, "--source") == "nw" and _opt(args, "--ttl-ms") == "30000"
    # panes: a display agent only, no tokens
    for key in [k for k in md if k[0] == "pane"]:
        assert "--token" not in md[key] and "--clear-token" not in md[key]
    assert _opt(md[("pane", "pane-cond")], "--display-agent") == "claude · conductor · 1 undecided"
    stew = _opt(md[("pane", "pane-stew")], "--display-agent")
    assert stew.startswith("claude · steward:Meta-Tools · SPEC-0901 loop 2") and stew.endswith(" · $1.20/$5")
    assert _opt(md[("pane", "pane-other")], "--display-agent") == \
        "claude · steward:Other · SPEC-0950 loop 4 · $7.50/$20"
    # conductor's workspace: queue totals
    ws = _tokens(md[("workspace", "ws-a")])
    assert ws == {"nw_queue": "2 active · 1 queued · 1 parked", "nw_attn": "1 undecided",
                  "nw_cost": "$8.70"}
    # the other steward's workspace: its project only, attention cleared
    other = _tokens(md[("workspace", "ws-b")])
    assert other == {"nw_queue": "Other: 1 active · 0 queued · 0 parked", "nw_attn": None,
                     "nw_cost": "$7.50"}
    seqs = [int(_opt(c, "--seq")) for c in fake.calls()]
    w2 = HW.Watcher(cfg, H.Client(), interval_s=10)                     # a restarted watcher
    w2.push()
    seqs2 = [int(_opt(c, "--seq")) for c in fake.calls()[len(seqs):]]
    allseqs = seqs + seqs2
    assert all(b > a for a, b in zip(allseqs, allseqs[1:]))


def test_stewards_sharing_a_workspace_get_only_their_projects(fake, tmp_path):
    cfg = _cfg(tmp_path)
    _queue_two_projects(cfg)
    _set_state(cfg, _campaign(cfg, "SPEC-0960-20261101-gggg", state="QUEUED"), project="Third")
    _place(cfg, "steward:Other", "pane-o", "ws-b")
    _place(cfg, "steward:Meta-Tools", "pane-m", "ws-b")
    HW.Watcher(cfg, H.Client(), interval_s=10).push()
    md = _metadata(fake.calls())
    assert set(md) == {("workspace", "ws-b"), ("pane", "pane-o"), ("pane", "pane-m")}
    assert _tokens(md[("workspace", "ws-b")])["nw_queue"] == \
        "Meta-Tools, Other: 2 active · 1 queued · 1 parked"            # Third is not here


def test_no_conductor_no_totals_anywhere(fake, tmp_path):
    cfg = _cfg(tmp_path)
    _queue(cfg)
    HW.Watcher(cfg, H.Client(), interval_s=10).push()
    assert fake.calls() == []                                          # not even ws-self


def test_conductor_pane_and_workspace_overrides(fake, tmp_path):
    cfg = _cfg(tmp_path)
    _place(cfg, "conductor", "pane-cond", "ws-a")
    HW.Watcher(cfg, H.Client(), interval_s=4, conductor_pane="pane-X", workspace="ws-X").push()
    md = _metadata(fake.calls())
    assert set(md) == {("workspace", "ws-X"), ("pane", "pane-X")}
    assert all(_opt(a, "--ttl-ms") == "12000" for a in md.values())
    n = len(fake.calls())
    HW.Watcher(cfg, H.Client(), conductor_pane="w9:p3").push()        # no placement for it
    assert set(_metadata(fake.calls()[n:])) == {("workspace", "w9"), ("pane", "w9:p3")}
    n = len(fake.calls())
    HW.Watcher(cfg, H.Client(), conductor_pane="pane-cond").push()    # placement: its workspace
    assert set(_metadata(fake.calls()[n:])) == {("workspace", "ws-a"), ("pane", "pane-cond")}


def test_display_strings():
    snap = {"items": [], "active_states": {"IMPLEMENTING", "REVIEWING"}, "escalations": 0,
            "undecided": 0}
    assert HW.steward_display(snap, "P") == "claude · steward:P · idle"
    assert HW.conductor_display(snap) == "claude · conductor"
    assert HW.conductor_display({**snap, "escalations": 2}, "$5.24") == \
        "claude · conductor · $5.24 · 2 need you"
    assert HW.conductor_display({**snap, "escalations": 1, "undecided": 3}) == \
        "claude · conductor · 1 need you · 3 undecided"
    one = {"project": "Meta-Tools", "state": "IMPLEMENTING", "label": "SPEC-0353",
           "loops": "16/30", "cost_usd": 40.95, "cap_usd": 115.0}
    assert HW.steward_display({**snap, "items": [one]}, "Meta-Tools") == \
        "claude · steward:Meta-Tools · SPEC-0353 loop 16 · $40.95/$115"
    assert HW.steward_display({**snap, "items": [{**one, "cap_usd": None}]}, "Meta-Tools") == \
        "claude · steward:Meta-Tools · SPEC-0353 loop 16 · $40.95"
    snap["items"] = [{"project": "P", "state": "IMPLEMENTING", "cost_usd": 1, "cap_usd": 5},
                     {"project": "P", "state": "REVIEWING", "cost_usd": 2, "cap_usd": 5},
                     {"project": "P", "state": "QUEUED", "cost_usd": 4, "cap_usd": 5},
                     {"project": "Q", "state": "REVIEWING", "cost_usd": 9, "cap_usd": 9}]
    assert HW.steward_display(snap, "P") == "claude · steward:P · 2 active · $3/$10"
    long = {**one, "project": "A" * 40, "label": "L" * 40}
    d = HW.steward_display({**snap, "items": [long]}, "A" * 40)
    assert len(d) == HW.DISPLAY_MAX and d.endswith("…") and ("A" * 23 + "…") in d
    assert HW._money(0) == "$0" and HW._money(115) == "$115" and HW._money(1.2) == "$1.20"


def test_attn_parts():
    assert HW.Watcher.attn({"escalations": 0, "undecided": 0}) == ""
    assert HW.Watcher.attn({"escalations": 2, "undecided": 1}) == "2 esc · 1 undecided"


# ---- watcher: notifications -----------------------------------------------------------------

def _notes(calls):
    return [(c[2], _opt(c, "--sound")) for c in calls if c[:2] == ["notification", "show"]]


def _drive(cfg):
    """The production projection: ledger entries -> events.sync -> the stream."""
    cid = _campaign(cfg, "SPEC-0910-20261101-dddd", state="IMPLEMENTING")
    _ev(cfg, cid, "run.end", {"persona": "implementer", "run_id": "001", "is_error": True,
                              "timed_out": False, "result_tail": "SyntaxError in foo.py"})
    _ev(cfg, cid, "run.end", {"persona": "implementer", "run_id": "002", "is_error": True,
                              "timed_out": False, "result_tail": "You've hit your session limit"})
    _ev(cfg, cid, "park", {"reason": "cost_cap", "detail": "d", "resume_to": "IMPLEMENTING"})
    _ev(cfg, cid, "park", {"reason": "main_dirty", "detail": "d", "resume_to": "CLOSING"})
    E.append(cfg, "escalation", campaign=cid, spec_id="SPEC-0910",
             data={"escalation_id": "ESC-0001", "question": "ship it?", "options": ["y", "n"]})
    m = _campaign(cfg, "SPEC-0911-20261101-eeee", state="CLOSING")
    _set_state(cfg, m, state="MERGED", close={"mode": "merge", "merged_sha": "deadbeefcafe"})
    _ev(cfg, m, "transition", {"from": "CLOSING", "to": "MERGED", "reason": "closed"})


def test_notifications_once_per_event_are_silent(fake, tmp_path):
    cfg = _cfg(tmp_path)
    w = HW.Watcher(cfg, H.Client(), interval_s=10)
    w.run(once=True)                                     # creates the cursor at the end
    _drive(cfg)
    w.run(once=True)
    notes = _notes(fake.calls())
    assert sorted(notes) == sorted([("nw: run failed", "none"), ("nw: parked", "none"),
                                    ("nw: needs you", "none"), ("nw: merged", "none")])
    before = len(fake.calls())
    HW.Watcher(cfg, H.Client(), interval_s=10).run(once=True)        # a restart
    assert _notes(fake.calls()[before:]) == []


def test_new_cursor_does_not_replay_history(fake, tmp_path):
    cfg = _cfg(tmp_path)
    _drive(cfg)
    E.sync(cfg)
    HW.Watcher(cfg, H.Client(), interval_s=10).run(once=True)
    assert _notes(fake.calls()) == []


def test_notification_policy():
    n = HW.notification_for
    assert n({"type": "run_failed", "data": {"transient": True}}) is None
    assert n({"type": "parked", "data": {"reason": "human_decision"}}) is None
    assert n({"type": "parked", "data": {"reason": "gate_failed"}})[2] == "none"
    assert n({"type": "admitted", "data": {}}) is None
    assert n({"type": "merged", "spec_id": "SPEC-1", "data": {"merged_sha": "abc"}}) == \
        ("nw: merged", "SPEC-1 abc", "none")


# ---- watcher: lifecycle ---------------------------------------------------------------------

def test_clean_exit_clears_once_does_not(fake, tmp_path):
    cfg = _cfg(tmp_path)
    _place(cfg, "conductor", "pane-cond", "ws-a")
    _place(cfg, "steward:Other", "pane-o", "ws-b")
    HW.Watcher(cfg, H.Client(), interval_s=10).run(once=True)
    assert not [c for c in fake.calls() if "--clear-display-agent" in c]
    assert all("--clear-token" not in c or "--token" in c for c in fake.calls())
    n = len(fake.calls())
    HW.Watcher(cfg, H.Client(), interval_s=10).run(stop=lambda: True, sleep=lambda s: None)
    tail = fake.calls()[n:]
    clears = [c for c in tail if "--token" not in c and "--display-agent" not in c
              and len(c) > 1 and c[1] == "report-metadata"]
    assert {(c[0], c[2]) for c in clears} == {("workspace", "ws-a"), ("workspace", "ws-b"),
                                               ("pane", "pane-cond"), ("pane", "pane-o")}
    for c in clears:
        if c[0] == "pane":
            assert "--clear-display-agent" in c and "--clear-token" not in c
        else:
            assert set(_tokens(c)) == set(HW.WS_TOKENS) and "--clear-display-agent" not in c


def test_one_watcher_at_a_time(fake, tmp_path):
    cfg = _cfg(tmp_path)
    with E.reader_lock(cfg, HW.READER):
        with pytest.raises(ValueError):
            HW.Watcher(cfg, H.Client()).run(once=True)


def test_hanging_herdr_is_bounded_and_trips_breaker(fake, tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    for i in range(6):
        _place(cfg, f"steward:P{i}", f"pane-{i}", "ws-a")
    monkeypatch.setenv("FAKE_HERDR_MODE", "hang")
    c = H.Client(timeout_s=0.3)
    t0 = time.monotonic()
    HW.Watcher(cfg, c, interval_s=10).push()            # 7 targets, breaker after 5
    assert time.monotonic() - t0 < 5 * 0.3 + 10
    assert c.calls == 5 and c.breaker_open()
    assert "pausing herdr calls" in capsys.readouterr().err


def test_watch_loop_pushes_and_stops(fake, tmp_path):
    cfg = _cfg(tmp_path)
    _place(cfg, "conductor", "pane-cond", "ws-a")
    stop = threading.Event()
    w = HW.Watcher(cfg, H.Client(), interval_s=1)
    t = threading.Thread(target=w.run, kwargs={"stop": stop.is_set})
    t.start()
    try:
        assert waits.until(lambda: len(_metadata(fake.calls())) >= 1)
    finally:
        stop.set()
        waits.join(t)
    assert any("--clear-display-agent" in c for c in fake.calls())


# ---- CLI -------------------------------------------------------------------------------------

def test_cli_noop_outside_herdr_and_without_binary(fake, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    monkeypatch.delenv("HERDR_ENV")
    r = CliRunner().invoke(cli, ["herdr", "watch", "--once"])
    assert r.exit_code == 0 and r.output.count("\n") == 1 and "nothing to do" in r.output
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))
    r = CliRunner().invoke(cli, ["herdr", "watch", "--once"])
    assert r.exit_code == 0 and "not on PATH" in r.output and r.output.count("\n") == 1
    assert not fake.path.exists()


def test_cli_once_with_fake(fake, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["herdr", "watch", "--once", "--interval", "5",
                                 "--conductor-pane", "w7:p2"])
    assert r.exit_code == 0, r.output
    md = _metadata(fake.calls())
    assert set(md) == {("workspace", "w7"), ("pane", "w7:p2")}
    assert _opt(md[("workspace", "w7")], "--ttl-ms") == "15000"
    assert _opt(md[("pane", "w7:p2")], "--display-agent") == "claude · conductor"


# ---- isolation and persona env -------------------------------------------------------------

def test_queue_runner_runproc_never_import_herdr():
    code = ("import sys, nightwright.cli, nightwright.agentic.queue, nightwright.agentic.runner, "
            "nightwright.agentic.runproc, nightwright.agentic.events, nightwright.agentic.mailbox;"
            "print(sorted(m for m in sys.modules if m.startswith('nightwright.agentic.herdr')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, timeout=120).stdout.strip()
    assert out == "[]"


def test_child_env_strips_herdr():
    env = RP.child_env({"EXTRA": "1"}, environ={"HERDR_ENV": "1", "HERDR_PANE_ID": "p",
                                                 "HERDR_SOCKET_PATH": "/s", "KEEP": "k"})
    assert env == {"KEEP": "k", "EXTRA": "1"}


def test_runproc_wrapper_child_sees_no_herdr(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    dump = "import json,os;print(json.dumps(dict(os.environ)))"
    (run_dir / "launch.json").write_text(json.dumps({
        "argv": [sys.executable, "-c", dump], "cwd": str(tmp_path), "env": {"EXTRA": "1"}}))
    (run_dir / "request.json").write_text(json.dumps({"timeout_s": 60}))
    (run_dir / "prompt.md").write_text("x")
    env = {**os.environ, "HERDR_ENV": "1", "HERDR_PANE_ID": "conductor-pane",
           "HERDR_SOCKET_PATH": "/tmp/x.sock", "HERDR_WORKSPACE_ID": "w", "HERDR_TAB_ID": "t"}
    subprocess.run([sys.executable, "-m", "nightwright.agentic.runproc", str(run_dir)], env=env,
                   check=True, timeout=60)
    got = json.loads((run_dir / "result.json").read_text())
    assert got["EXTRA"] == "1"
    assert not [k for k in got if k.startswith("HERDR_")]


def test_fake_rejects_options_before_target_and_breaker_names_the_error(fake):
    """herdr 0.9.5 rejects `report-metadata --source nw ID` (exit 2); the breaker line must carry
    herdr's own stderr so a misbehaving call is diagnosable."""
    err = io.StringIO()
    c = H.Client(err=err, fail_max=2)
    bad = ["pane", "report-metadata", "--source", "nw", "p1"]
    assert c.call(bad) is False and c.call(bad) is False
    assert "unknown option: nw" in err.getvalue() and "pane report-metadata" in err.getvalue()
    assert H.Client().call(H.metadata_args("pane", "p1", {"nw_role": "x"}, seq=1, ttl_ms=1000))


# ---- run panes (SPEC-0364) ---------------------------------------------------------------------

from nightwright.agentic import herdr_runpanes as RPN
from nightwright.agentic import store as S
from nightwright.agentic import tail as TL
from rich.console import Console

TOOL_SAMPLE = Path(__file__).parent / "fixtures" / "stream_json" / "read_tool_schema.jsonl"


def _fstate(tmp_path):
    return json.loads((tmp_path / "herdr-state.json").read_text())


def _run(cfg, cid, name="001-implementer", *, persona="implementer", budget=5.0,
         lines=None):
    """A run dir whose wrapper is this (live) test process: `run_status` == running."""
    rd = S.runs_dir(cfg, cid) / name
    rd.mkdir(parents=True)
    RP.write_json_atomic(rd / "request.json", {"persona": persona, "max_budget_usd": budget,
                                               "model": "haiku"})
    src = TOOL_SAMPLE.read_text().splitlines()
    (rd / "stream.jsonl").write_text("".join(x + "\n" for x in (src if lines is None
                                                                 else src[:lines])))
    RP.write_json_atomic(rd / "proc.json", {"wrapper_pid": os.getpid(),
                                            "wrapper_start_ticks": None})
    return rd


def _end(rd):
    RP.write_json_atomic(rd / "exit.json", {"exit_code": 0})


def _rp_watcher(cfg, now, **kw):
    # These tests read the fake's state after each push, so every call must finish: a load-
    # tolerant timeout instead of the client's 2 s (the timeout has its own tests above).
    return HW.Watcher(cfg, H.Client(timeout_s=waits.WAIT_S), interval_s=10, run_panes=True,
                      clock=lambda: now[0], **kw)


def _cmds(calls, group, cmd):
    return [c for c in calls if c[:2] == [group, cmd]]


def test_run_pane_argv_builders_put_the_id_first():
    assert H.tab_create_args("w7", "SPEC-0349 nw doctor", "/x") == [
        "tab", "create", "--workspace", "w7", "--label", "SPEC-0349 nw doctor", "--cwd", "/x",
        "--env", "fish_history=", "--env", "HISTFILE=/dev/null", "--no-focus"]
    assert H.pane_split_args("w7:p3", "down", "/x") == [
        "pane", "split", "w7:p3", "--direction", "down", "--cwd", "/x",
        "--env", "fish_history=", "--env", "HISTFILE=/dev/null", "--no-focus"]
    with pytest.raises(ValueError):
        H.pane_split_args("w7:p3", "left", "/x")
    assert H.pane_layout_args("p") == ["pane", "layout", "--pane", "p"]
    assert H.pane_process_info_args("p") == ["pane", "process-info", "--pane", "p"]
    assert H.pane_get_args("p") == ["pane", "get", "p"]
    assert H.pane_rename_args("p", "nw-run:C") == ["pane", "rename", "p", "nw-run:C"]
    assert H.pane_run_args("p", "echo hi") == ["pane", "run", "p", "echo hi"]
    assert H.pane_close_args("p") == ["pane", "close", "p"]
    assert H.tab_rename_args("w7:t2", "x (+1)") == ["tab", "rename", "w7:t2", "x (+1)"]
    assert H.report_agent_args("p", "working", seq=5) == [
        "pane", "report-agent", "p", "--source", "nw-run", "--agent", "nw-run", "--state",
        "working", "--seq", "5"]
    with pytest.raises(ValueError):
        H.report_agent_args("p", "done", seq=1)
    assert H.metadata_args("pane", "p", {}, seq=1, ttl_ms=10, source="nw-run",
                           display_agent="x")[:5] == ["pane", "report-metadata", "p", "--source",
                                                      "nw-run"]


def test_fake_models_herdr_parser_and_replies(fake):
    """The fake rejects options before the id (exit 2) like herdr 0.9.5 and answers with the
    JSON shapes nightwright reads."""
    c = H.Client()
    assert c.call_json(["pane", "get", "w1:p9"]) is None and "pane_not_found" in c.last_error
    assert not c.call(["pane", "split", "--direction", "right", "w1:p1"])
    assert "unknown option" in c.last_error
    res = c.call_json(H.tab_create_args("w1", "SPEC-0901 x", "/tmp"))
    assert res["tab"]["tab_id"] == "w1:t1" and res["root_pane"]["pane_id"] == "w1:p1"
    assert res["tab"]["label"] == "SPEC-0901 x" and res["root_pane"]["tab_id"] == "w1:t1"
    split = c.call_json(H.pane_split_args("w1:p1", "right", "/tmp"))
    assert split["pane"]["pane_id"] == "w1:p2" and split["pane"]["tab_id"] == "w1:t1"
    lay = c.call_json(H.pane_layout_args("w1:p1"))["layout"]
    assert [(p["pane_id"], p["rect"]["width"]) for p in lay["panes"]] == [("w1:p1", 60),
                                                                          ("w1:p2", 60)]
    assert c.call_json(["tab", "get", "w1:t1"])["tab"]["pane_count"] == 2
    assert c.call(H.pane_close_args("w1:p1")) and c.call(H.pane_close_args("w1:p2"))
    assert c.call_json(["tab", "rename", "w1:t1", "x"]) is None        # last pane closed the tab
    assert c.call_json(["tab", "get", "w1:t1"]) is None and "tab_not_found" in c.last_error


def test_call_json_returns_result_or_none(fake, monkeypatch):
    c = H.Client()
    assert c.call_json(["pane", "rename", "nope", "x"]) is None
    assert c.call_json(H.tab_create_args("w1", "L", "/"))["type"] == "tab_created"
    assert c.call_json(["notification", "show", "t"]) is None          # exit 0, no JSON
    monkeypatch.setenv("FAKE_HERDR_MODE", "fail")
    assert c.call_json(["pane", "get", "w1:p1"]) is None and c.fails == 1


def test_run_panes_off_by_default_change_nothing(fake, tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0901-20261101-aaaa")
    _run(cfg, cid)
    _place(cfg, "conductor", "pane-cond", "ws-a")
    w = HW.Watcher(cfg, H.Client(), interval_s=10)
    assert w.run_panes is None
    w.run(once=True)
    assert {(c[0], c[1]) for c in fake.calls()} == {("workspace", "report-metadata"),
                                                    ("pane", "report-metadata")}
    assert not RPN.state_path(cfg).exists()


def _spec_at_open(cfg, cid, title):
    (S.campaign_dir(cfg, cid) / "spec_at_open.md").write_text(
        f"---\nid: {cid[:9]}\ntitle: {json.dumps(title)}\nstatus: accepted\n---\n\nbody\n")


def test_short_title_and_tab_label():
    assert RPN.short_title("`nw doctor`: read-only install health check with `nw.doctor.v1` "
                           "JSON") == "nw doctor"
    assert RPN.short_title("herdr run panes: one pane per active campaign") == "herdr run panes"
    assert RPN.short_title("**Bold** [linked](http://x) _thing_ — rest") == "Bold linked thing"
    assert RPN.short_title("a very long title without any clause break at all") == \
        "a very long title…"
    assert len(RPN.short_title("x" * 80)) == 24
    assert RPN.short_title("snake_case_name stays") == "snake_case_name stays"
    assert RPN.short_title("") == "" and RPN.short_title(None) == ""
    assert RPN.tab_label("SPEC-0349", "`nw doctor`: x") == "SPEC-0349 nw doctor"
    assert RPN.tab_label("merge:feat/x", "") == "merge:feat/x"


def test_title_from_spec_at_open_or_idea_heading(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0901-20261101-aaaa")
    assert RPN.title_of(cfg, cid) == ""
    _spec_at_open(cfg, cid, "`nw doctor`: health")
    assert RPN.title_of(cfg, cid) == "`nw doctor`: health"
    other = _campaign(cfg, "SPEC-0902-20261101-bbbb")
    assert RPN.title_of(cfg, other, {"idea": {"heading": "an idea: more"}}) == "an idea: more"
    (S.campaign_dir(cfg, other) / "spec_at_open.md").write_text("---\n: : bad\n---\n")
    assert RPN.title_of(cfg, other) == ""


def test_one_running_run_gets_its_own_named_tab(fake, tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0901-20261101-aaaa")
    _spec_at_open(cfg, cid, "`nw doctor`: read-only install health check")
    _set_state(cfg, cid, counters={"loops": 3, "runs": 1})
    _run(cfg, cid)
    _campaign(cfg, "SPEC-0902-20261101-bbbb", state="QUEUED")          # no run: no tab
    _place(cfg, "conductor", "w7:p2", "w7")
    now = [1000.0]
    _rp_watcher(cfg, now).run(once=True)
    calls = fake.calls()
    assert _cmds(calls, "tab", "create") == [["tab", "create", "--workspace", "w7", "--label",
                                              "SPEC-0901 nw doctor", "--cwd",
                                              os.path.expanduser("~"), "--env",
                                              "fish_history=", "--env", "HISTFILE=/dev/null",
                                              "--no-focus"]]
    pane = "w7:p1"
    assert ["pane", "rename", pane, f"nw-run:{cid}"] in calls
    (typed,) = _cmds(calls, "pane", "run")
    assert typed[2] == pane and typed[3].startswith("env NW_CONFIG_DIR=")
    assert typed[3].endswith(f"-m nightwright campaign tail {cid} --follow-campaign")
    (agent,) = _cmds(calls, "pane", "report-agent")
    assert agent[2] == pane and _opt(agent, "--state") == "working"
    assert _opt(agent, "--source") == "nw-run" and _opt(agent, "--agent") == "nw-run"
    md = _metadata(calls)[("pane", pane)]
    assert _opt(md, "--source") == "nw-run" and _opt(md, "--ttl-ms") == "30000"
    display = _opt(md, "--display-agent")
    assert display.startswith("nw run · implementer · loop 3 · $") and display.endswith("/$5")
    assert "SPEC-0901" not in display                       # the tab names the campaign
    assert not _cmds(calls, "pane", "split") and not _cmds(calls, "tab", "rename")
    st = _fstate(tmp_path)
    assert st["panes"][pane]["label"] == f"nw-run:{cid}" and st["panes"][pane]["agent_status"] == "working"
    assert not st["tabs"]["w7:t1"]["focus"]
    saved = json.loads(RPN.state_path(cfg).read_text())
    assert saved["schema"] == "nw.herdr.run_panes.v2"
    rec = saved["sessions"][""]["panes"][cid]
    assert rec["pane_id"] == pane and rec["tab_id"] == "w7:t1"
    assert rec["tab_label"] == "SPEC-0901 nw doctor"


def test_typed_command_runs_the_campaign_viewer(tmp_path):
    """What nw types into the pane really runs the viewer: it clears the screen, prints the
    fixture run's transcript and waits for the next run until it is signalled."""
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0901-20261101-aaaa")
    rd = _run(cfg, cid)
    _end(rd)
    cmd = RPN.tail_command(cfg, cid)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NW_", "HERDR_"))}
    proc = subprocess.Popen(["/bin/sh", "-c", "exec " + cmd], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env={**env, "COLUMNS": "200"})
    out = []
    reader = threading.Thread(target=lambda: out.extend(proc.stdout), daemon=True)
    reader.start()
    try:
        assert waits.until(lambda: "waiting for the next run" in "".join(out), timeout=120)
        assert proc.poll() is None                          # it stays: the pane's process
    finally:
        proc.terminate()
        proc.wait(timeout=30)
        waits.join(reader)
    text = "".join(out)
    assert proc.returncode == 130, proc.stderr.read()
    assert text.startswith(TL.CLEAR)
    assert "run 001-implementer · persona implementer" in text
    assert "→ Read" in text and "■ result: success" in text


def test_follow_campaign_switches_runs_by_itself(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0901-20261101-aaaa")
    rd1 = _run(cfg, cid)
    made = {}
    steps = [lambda: _end(rd1),
             lambda: _set_state(cfg, cid, state="REVIEWING"),
             lambda: made.setdefault("rd2", _run(cfg, cid, "002-conformance_reviewer",
                                                 persona="conformance_reviewer")),
             lambda: _end(made["rd2"]),
             lambda: _set_state(cfg, cid, state="PARKED", park={"reason": "cost_cap"})]
    left = list(steps)

    def sleep(_s):
        if left:
            left.pop(0)()
        else:
            left.append(None)                               # one quiet poll, then stop
    buf = io.StringIO()
    rc = TL.follow_campaign(cfg, cid, out=Console(file=buf, width=200), sleep=sleep,
                            stop=lambda: left == [None])
    text = buf.getvalue()
    assert rc == 0 and text.startswith(TL.CLEAR)
    i1 = text.index("run 001-implementer · persona implementer")
    e1 = text.index("■ run 001-implementer ended · waiting for the next run · campaign implementing")
    c1 = text.index("· campaign reviewing")
    i2 = text.index("run 002-conformance_reviewer · persona conformance_reviewer")
    e2 = text.index("■ run 002-conformance_reviewer ended · waiting for the next run · "
                    "campaign reviewing")
    c2 = text.index("· campaign parked: cost_cap")
    assert i1 < e1 < c1 < i2 < e2 < c2
    assert text.count("■ result: success") == 2


def test_follow_campaign_cli_and_restored_pane_shows_the_last_run(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0901-20261101-aaaa")
    _end(_run(cfg, cid))
    buf = io.StringIO()
    TL.follow_campaign(cfg, cid, out=Console(file=buf, width=200), sleep=lambda s: None,
                       stop=lambda: True, clear=False)
    text = buf.getvalue()
    assert not text.startswith(TL.CLEAR) and "■ result: success" in text
    assert "■ waiting for the next run · campaign implementing" in text
    with pytest.raises(ValueError):
        TL.follow_campaign(cfg, "SPEC-0999-nope", out=Console(file=io.StringIO()),
                           stop=lambda: True)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    monkeypatch.setattr(TL, "follow_campaign", lambda *a, **k: (_ for _ in ()).throw(
        KeyboardInterrupt))
    r = CliRunner().invoke(cli, ["campaign", "tail", cid, "--follow-campaign"])
    assert r.exit_code == 130, r.output


def test_second_campaign_gets_a_second_tab_never_a_split(fake, tmp_path):
    cfg = _cfg(tmp_path)
    cids = ["SPEC-0901-20261101-aaaa", "SPEC-0902-20261101-bbbb", "SPEC-0903-20261101-cccc"]
    for cid in cids:
        _campaign(cfg, cid)
        _run(cfg, cid)
    _spec_at_open(cfg, cids[0], "same: a")
    _spec_at_open(cfg, cids[1], "other: b")
    now = [1000.0]
    _rp_watcher(cfg, now).run(once=True)                     # no conductor: own workspace
    calls = fake.calls()
    creates = _cmds(calls, "tab", "create")
    assert [c[3] for c in creates] == ["ws-self"] * 3
    assert sorted(_opt(c, "--label") for c in creates) == ["SPEC-0901 same", "SPEC-0902 other",
                                                           "SPEC-0903"]
    assert all("--no-focus" in c for c in creates) and not _cmds(calls, "pane", "split")
    st = _fstate(tmp_path)
    assert sorted(len(t["panes"]) for t in st["tabs"].values()) == [1, 1, 1]
    assert not any(p.get("focus") for p in st["panes"].values())


def test_two_campaigns_with_the_same_label_get_distinct_tabs(fake, tmp_path):
    cfg = _cfg(tmp_path)
    a = _campaign(cfg, "SPEC-0901-20261101-aaaa")
    b = _campaign(cfg, "SPEC-0901-20261102-bbbb")
    for cid in (a, b):
        _run(cfg, cid)
        _spec_at_open(cfg, cid, "nw doctor: x")
    _rp_watcher(cfg, [1000.0]).run(once=True)
    labels = [_opt(c, "--label") for c in _cmds(fake.calls(), "tab", "create")]
    assert sorted(labels) == ["SPEC-0901 nw doctor", "SPEC-0901 nw doctor bbbb"] or \
        sorted(labels) == ["SPEC-0901 nw doctor", "SPEC-0901 nw doctor aaaa"]
    assert len(set(labels)) == 2


def test_tab_lives_while_a_run_runs_then_lingers_and_closes(fake, tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0901-20261101-aaaa")
    rd = _run(cfg, cid)
    now = [1000.0]
    w = _rp_watcher(cfg, now)
    assert w.run_panes.linger_s == 60                         # SPEC-0366 default
    with E.reader_lock(cfg, HW.READER):
        w.run_panes.load()
        w.push()
        pane = "ws-self:p1"
        w.push()                     # reported every push (a report while starting can drop)
        reports = _cmds(fake.calls(), "pane", "report-agent")
        assert [_opt(r, "--state") for r in reports] == ["working", "working"]
        assert int(_opt(reports[1], "--seq")) > int(_opt(reports[0], "--seq"))
        _end(rd)
        _set_state(cfg, cid, state="GATING")                  # still ACTIVE, no run
        w.push()
        assert _fstate(tmp_path)["panes"][pane]["agent_status"] == "idle"
        assert _opt(_cmds(fake.calls(), "pane", "report-metadata")[-1], "--display-agent") == \
            "nw run · gating"
        now[0] += 59
        w.push()
        assert pane in _fstate(tmp_path)["panes"]
        _set_state(cfg, cid, state="REVIEWING")
        rd2 = _run(cfg, cid, "002-conformance_reviewer", persona="conformance_reviewer")
        w.push()                                              # within the linger: same tab
        assert _fstate(tmp_path)["panes"][pane]["agent_status"] == "working"
        assert len(_cmds(fake.calls(), "pane", "run")) == 1   # the viewer switches itself
        assert len(_cmds(fake.calls(), "tab", "create")) == 1
        assert _opt(_cmds(fake.calls(), "pane", "report-metadata")[-1],
                    "--display-agent").startswith("nw run · conformance_reviewer")
        _end(rd2)
        _set_state(cfg, cid, state="CLOSING")
        w.push()
        now[0] += 60
        w.push()                                              # 60 s after the last run
        assert _cmds(fake.calls(), "pane", "close") == [["pane", "close", pane]]
        assert _fstate(tmp_path)["tabs"] == {}                # herdr closed the tab with it
        assert json.loads(RPN.state_path(cfg).read_text())["sessions"] == {}
        _set_state(cfg, cid, state="REVIEWING")
        _run(cfg, cid, "003-adversarial_reviewer", persona="adversarial_reviewer")
        w.push()                                              # later run: a new tab
    assert len(_cmds(fake.calls(), "tab", "create")) == 2


def test_parked_campaign_is_blocked_for_the_linger_then_closed(fake, tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0901-20261101-aaaa")
    rd = _run(cfg, cid)
    now = [1000.0]
    w = _rp_watcher(cfg, now, run_pane_linger=30)
    with E.reader_lock(cfg, HW.READER):
        w.push()
        _end(rd)
        _set_state(cfg, cid, state="PARKED", park={"reason": "cost_cap", "resume_to": "REVIEWING"})
        w.push()
        assert _fstate(tmp_path)["panes"]["ws-self:p1"]["agent_status"] == "blocked"
        assert _opt(_cmds(fake.calls(), "pane", "report-metadata")[-1], "--display-agent") == \
            "nw run · parked: cost_cap"
        now[0] += 30
        w.push()
    assert _fstate(tmp_path)["panes"] == {} and _fstate(tmp_path)["tabs"] == {}


def test_merged_campaign_with_a_live_run_keeps_its_tab_until_the_run_ends(fake, tmp_path):
    """The run, not the queue state, decides: a campaign out of queue_payload (MERGED) whose
    tab exists is still checked for running runs, then lingers."""
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0901-20261101-aaaa")
    rd = _run(cfg, cid)
    now = [1000.0]
    w = _rp_watcher(cfg, now)
    with E.reader_lock(cfg, HW.READER):
        w.push()
        _set_state(cfg, cid, state="MERGED")
        w.push()
        assert _fstate(tmp_path)["panes"]["ws-self:p1"]["agent_status"] == "working"
        _end(rd)
        w.push()
        assert _opt(_cmds(fake.calls(), "pane", "report-metadata")[-1], "--display-agent") == \
            "nw run · merged"
        now[0] += 60
        w.push()
    assert _fstate(tmp_path)["tabs"] == {}


def test_overflow_caps_at_six_tabs_and_evicts_lingering_first(fake, tmp_path):
    cfg = _cfg(tmp_path)
    cids = [f"SPEC-09{i:02d}-20261101-aaaa" for i in range(7)]
    rds = {}
    for cid in cids:
        _campaign(cfg, cid)
        rds[cid] = _run(cfg, cid)
    now = [1000.0]
    w = _rp_watcher(cfg, now)
    with E.reader_lock(cfg, HW.READER):
        w.push()
        st = _fstate(tmp_path)
        assert len(st["panes"]) == 6 and len(st["tabs"]) == 6
        rp = w.run_panes
        newest = max(rp.panes, key=lambda c: rp.panes[c]["order"])
        (waiting,) = set(cids) - set(rp.panes)
        assert st["tabs"][rp.panes[newest]["tab_id"]]["label"] == f"{newest[:9]} (+1)"
        assert sum(t["label"].endswith("(+1)") for t in st["tabs"].values()) == 1
        first = next(iter(rp.panes))
        _end(rds[first])                                      # its run ends: lingers
        now[0] += 1
        w.push()
    st = _fstate(tmp_path)
    assert len(st["panes"]) == 6
    assert sorted(t["label"] for t in st["tabs"].values()) == sorted(
        c[:9] for c in cids if c != first)
    labels = {p["label"] for p in st["panes"].values()}
    assert f"nw-run:{first}" not in labels and f"nw-run:{waiting}" in labels


def test_clean_exit_closes_once_adopts(fake, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0901-20261101-aaaa")
    _run(cfg, cid)
    now = [1000.0]
    _rp_watcher(cfg, now).run(once=True)
    _rp_watcher(cfg, now).run(once=True)                     # adopts: no second tab
    calls = fake.calls()
    assert len(_cmds(calls, "tab", "create")) == 1 and len(_cmds(calls, "pane", "run")) == 1
    assert ["pane", "get", "ws-self:p1"] in calls
    assert not _cmds(calls, "pane", "close")
    assert len(_cmds(calls, "pane", "report-agent")) == 2    # once per push, adopted too
    _rp_watcher(cfg, now).run(stop=lambda: True, sleep=lambda s: None)
    assert _cmds(fake.calls(), "pane", "close") == [["pane", "close", "ws-self:p1"]]
    assert _fstate(tmp_path)["panes"] == {} and _fstate(tmp_path)["tabs"] == {}


def _write_state(cfg, data):
    RPN.state_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    RPN.state_path(cfg).write_text(json.dumps(data))


def test_adoption_never_touches_what_is_not_ours(fake, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    c = H.Client()
    human = c.call_json(H.tab_create_args("ws-self", "human", "/"))["root_pane"]["pane_id"]
    other = c.call_json(H.pane_split_args(human, "right", "/"))["pane"]["pane_id"]
    c.call(H.pane_rename_args(other, "nw-run:SPEC-0902-20261101-bbbb"))   # right label, wrong tab
    _write_state(cfg, {"schema": RPN.SCHEMA, "sessions": {
        "": {"panes": {
            "SPEC-0901-20261101-aaaa": {"pane_id": human, "tab_id": "ws-self:t1"},  # label differs
            "SPEC-0902-20261101-bbbb": {"pane_id": other, "tab_id": "ws-self:t99"},  # tab differs
            "SPEC-0903-20261101-cccc": {"pane_id": "ws-self:p77", "tab_id": "ws-self:t1"}}},
        "/other.sock": {"panes": {"C": {"pane_id": human, "tab_id": "ws-self:t1"}}}}})
    n = len(fake.calls())
    now = [1000.0]
    _rp_watcher(cfg, now).run(stop=lambda: True, sleep=lambda s: None)
    after = fake.calls()[n:]
    assert {c[0] + " " + c[1] for c in after} <= {"pane get", "workspace report-metadata",
                                                  "pane report-metadata"}
    assert all(c[1] != "report-metadata" or c[2] not in (human, other) for c in after)
    saved = json.loads(RPN.state_path(cfg).read_text())["sessions"]
    assert "" not in saved and "/other.sock" in saved          # another socket: kept, untouched
    st = _fstate(tmp_path)
    assert human in st["panes"] and other in st["panes"]


def test_migration_closes_old_shared_tab_panes_and_opens_own_tabs(fake, tmp_path):
    """A SPEC-0364 layout (one `nw runs` tab, v1 state file): verified panes are closed, the
    shared tab goes with its last pane, and each still-running campaign gets its own tab. A v1
    pane whose label is not ours, and another socket's entry, are left alone."""
    cfg = _cfg(tmp_path)
    a, b, gone = "SPEC-0901-20261101-aaaa", "SPEC-0902-20261101-bbbb", "SPEC-0903-20261101-cccc"
    for cid in (a, b, gone):
        _campaign(cfg, cid)
    _run(cfg, a)
    _spec_at_open(cfg, a, "nw doctor: x")
    c = H.Client()
    res = c.call_json(H.tab_create_args("ws-self", "nw runs", "/"))   # made like 0364 did
    pa, tab = res["root_pane"]["pane_id"], res["tab"]["tab_id"]
    pb = c.call_json(H.pane_split_args(pa, "right", "/"))["pane"]["pane_id"]
    c.call(H.pane_rename_args(pa, f"nw-run:{a}"))
    c.call(H.pane_rename_args(pb, f"nw-run:{b}"))
    human = c.call_json(H.tab_create_args("ws-self", "mine", "/"))["root_pane"]["pane_id"]
    _write_state(cfg, {"schema": "nw.herdr.run_panes.v1", "sessions": {
        "": {"tab": tab, "tab_label": "nw runs", "workspace": "ws-self", "panes": {
            a: {"pane_id": pa, "run": "001-implementer", "state": "working", "ended_at": None},
            b: {"pane_id": pb, "run": "001-implementer", "state": "idle", "ended_at": 5.0},
            gone: {"pane_id": human, "run": "001"}},            # label not ours: untouched
            "dismissed": []},
        "/other.sock": {"tab": "x:t1", "panes": {"C": {"pane_id": "x:p1"}}}}})
    n = len(fake.calls())
    _rp_watcher(cfg, [1000.0]).run(once=True)
    after = fake.calls()[n:]
    assert sorted(c[2] for c in _cmds(after, "pane", "close")) == sorted([pa, pb])
    st = _fstate(tmp_path)
    assert tab not in st["tabs"] and human in st["panes"]       # shared tab gone, human's kept
    (create,) = _cmds(after, "tab", "create")
    assert _opt(create, "--label") == "SPEC-0901 nw doctor"
    saved = json.loads(RPN.state_path(cfg).read_text())
    assert saved["schema"] == RPN.SCHEMA
    assert set(saved["sessions"][""]["panes"]) == {a}
    assert not saved["sessions"][""]["panes"][a].get("legacy")
    assert saved["sessions"]["/other.sock"]["panes"]["C"]["legacy"] is True   # converted only


def test_migration_waits_when_herdr_is_unreachable(fake, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    a = _campaign(cfg, "SPEC-0901-20261101-aaaa")
    _write_state(cfg, {"schema": "nw.herdr.run_panes.v1", "sessions": {
        "": {"tab": "ws-self:t1", "panes": {a: {"pane_id": "ws-self:p1", "run": "001"}}}}})
    monkeypatch.setenv("FAKE_HERDR_MODE", "fail")
    _rp_watcher(cfg, [1000.0]).run(once=True)
    saved = json.loads(RPN.state_path(cfg).read_text())["sessions"][""]["panes"][a]
    assert saved["legacy"] is True and saved["tab_id"] == "ws-self:t1"
    assert not _cmds(fake.calls(), "pane", "close")


def test_fresh_shell_gets_the_viewer_typed_again(fake, tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0901-20261101-aaaa")
    _run(cfg, cid)
    now = [1000.0]
    _rp_watcher(cfg, now).run(once=True)
    st = _fstate(tmp_path)
    st["fresh_shell"] = ["ws-self:p1"]                      # herdr restored the pane
    (tmp_path / "herdr-state.json").write_text(json.dumps(st))
    _rp_watcher(cfg, now).run(once=True)
    typed = _cmds(fake.calls(), "pane", "run")
    assert len(typed) == 2 and typed[0] == typed[1]
    _rp_watcher(cfg, now).run(once=True)                   # its viewer runs now: not again
    assert len(_cmds(fake.calls(), "pane", "run")) == 2


def test_pane_closed_by_the_human_is_forgotten_not_recreated(fake, tmp_path, capsys):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0901-20261101-aaaa")
    rd = _run(cfg, cid)
    now = [1000.0]
    w = _rp_watcher(cfg, now)
    with E.reader_lock(cfg, HW.READER):
        w.push()
        assert H.Client().call(H.pane_close_args("ws-self:p1"))        # the human closes it
        w.push()
        w.push()
        assert w.run_panes.panes == {} and cid in w.run_panes.dismissed
        assert len(_cmds(fake.calls(), "tab", "create")) == 1
        _end(rd)                                            # its run ends: forgiven
        w.push()
        assert cid not in w.run_panes.dismissed
        _run(cfg, cid, "002-implementer")
        w.push()
    assert len(_cmds(fake.calls(), "tab", "create")) == 2
    assert not w.client.breaker_open() and "pausing" not in capsys.readouterr().err


def test_cli_run_panes_flag_and_config(fake, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0901-20261101-aaaa")
    _run(cfg, cid)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["herdr", "watch", "--once"])
    assert r.exit_code == 0, r.output
    assert not _cmds(fake.calls(), "tab", "create")
    r = CliRunner().invoke(cli, ["herdr", "watch", "--once", "--run-panes"])
    assert r.exit_code == 0, r.output
    assert len(_cmds(fake.calls(), "tab", "create")) == 1
    cfg["agentic"]["herdr"] = {"run_panes": True}
    r = CliRunner().invoke(cli, ["herdr", "watch", "--once", "--no-run-panes"])
    assert r.exit_code == 0 and len(_cmds(fake.calls(), "pane", "get")) == 0
    r = CliRunner().invoke(cli, ["herdr", "watch", "--once"])
    assert r.exit_code == 0 and len(_cmds(fake.calls(), "pane", "get")) == 1   # adopted


def test_linger_from_config_and_flag(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    assert RPN.linger_default(cfg) == 60
    cfg["agentic"]["herdr"] = {"run_pane_linger_s": 15}
    assert RPN.linger_default(cfg) == 15
    assert HW.Watcher(cfg, H.Client(), run_panes=True).run_panes.linger_s == 15
    assert HW.Watcher(cfg, H.Client(), run_panes=True,
                      run_pane_linger=5).run_panes.linger_s == 5
    cfg["agentic"]["herdr"] = {"run_pane_linger_s": "junk"}
    assert RPN.linger_default(cfg) == 60
    seen = {}

    class W:
        def __init__(self, *a, **k):
            seen.update(k)

        def run(self, **k):
            pass
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    monkeypatch.setattr(HW, "Watcher", W)
    monkeypatch.setattr(H, "unavailable", lambda env=None: None)
    CliRunner().invoke(cli, ["herdr", "watch", "--once", "--run-panes"])
    assert seen["run_pane_linger"] is None                  # -> config, else 60
    CliRunner().invoke(cli, ["herdr", "watch", "--once", "--run-panes", "--run-pane-linger", "7"])
    assert seen["run_pane_linger"] == 7


def test_run_display_strings():
    assert RPN.run_display(persona="implementer", loop="3", cost=1.2, budget=10) == \
        "nw run · implementer · loop 3 · $1.20/$10"
    assert RPN.run_display(persona="adversarial_reviewer", loop="2", cost=0.89, budget=2.5) == \
        "nw run · adversarial_reviewer · loop 2 · $0.89/$2.50"
    assert RPN.run_display(persona="implementer", budget=10) == \
        "nw run · implementer · budget $10"
    assert RPN.run_display(status=RPN.status_text("HANDED_OFF")) == "nw run · handed off"
    long = RPN.run_display(persona="y" * 40, loop="12", cost=100.5, budget=200)
    assert len(long) <= 64 and "…" in long
    assert RPN.agent_state(True, "PARKED") == "working"
    assert RPN.agent_state(False, "PARKED") == "blocked"
    assert [RPN.agent_state(False, s) for s in ("QUEUED", "WAITING", "MERGED", "GATING")] == \
        ["idle"] * 4


def _herdr_json(env, *args):
    out = subprocess.run(["herdr", *args], env=env, capture_output=True, text=True, timeout=10,
                         check=True).stdout
    return json.loads(out)


def _real_pane(env, pane):
    return _herdr_json(env, "agent", "get", pane)["result"]["agent"]


def _real_snapshot_entry(env, kind, ident):
    snap = _herdr_json(env, "api", "snapshot")["result"]["snapshot"]
    key = {"panes": "pane_id", "workspaces": "workspace_id"}[kind]
    return next(x for x in snap[kind] if x.get(key) == ident)


@pytest.mark.skipif(os.environ.get("NW_HERDR_CONTRACT") != "1",
                    reason="opt-in: NW_HERDR_CONTRACT=1 inside a real herdr pane")
def test_real_herdr_accepts_our_argv():
    """Contract check against the installed herdr (the fakes only model its parser). Writes only
    to the caller's own pane and workspace (TTL 60 s), reads the values back with `herdr agent
    get` / `herdr api snapshot`, clears them and reads back their absence (SPEC-0361/0363)."""
    env = {k: v for k, v in _REAL_ENV.items()}
    pane, ws = env.get("HERDR_PANE_ID"), env.get("HERDR_WORKSPACE_ID")
    assert env.get("HERDR_ENV") == "1" and pane and ws, "run inside a herdr pane"
    c = H.Client(env=env)
    snap = {"items": [{"project": "Contract", "state": "IMPLEMENTING", "label": "SPEC-0000",
                       "loops": "3/9", "cost_usd": 1.23, "cap_usd": 5.0}],
            "active_states": {"IMPLEMENTING"}}
    display = HW.steward_display(snap, "Contract")
    assert display == "claude · steward:Contract · SPEC-0000 loop 3 · $1.23/$5"
    try:
        assert c.call(H.metadata_args("pane", pane, {"nw_contract": "ok"}, seq=c.next_seq(),
                                      ttl_ms=60000, display_agent=display)), c.last_error
        assert c.call(H.metadata_args("workspace", ws, {"nw_contract": "ok-ws"},
                                      seq=c.next_seq(), ttl_ms=60000)), c.last_error
        assert _real_pane(env, pane).get("display_agent") == display
        assert _real_snapshot_entry(env, "panes", pane).get("display_agent") == display
        assert "ok-ws" in json.dumps(_real_snapshot_entry(env, "workspaces", ws))
        # herdr has no workspace display agent: the fake's model of the parser is right
        bad = H.Client(env=env)
        assert not bad.call(["workspace", "report-metadata", ws, "--source", "nw", "--seq",
                             str(bad.next_seq()), "--display-agent", "x"])
        assert "--display-agent" in (bad.last_error or "")
    finally:
        assert c.call(H.clear_args("pane", pane, ["nw_contract"], seq=c.next_seq(),
                                   clear_display_agent=True)), c.last_error
        assert c.call(H.clear_args("workspace", ws, ["nw_contract"], seq=c.next_seq())), \
            c.last_error
    assert "display_agent" not in _real_pane(env, pane)
    assert "nw_contract" not in json.dumps(_real_snapshot_entry(env, "panes", pane))
    assert "ok-ws" not in json.dumps(_real_snapshot_entry(env, "workspaces", ws))


_REAL_ENV = dict(os.environ)    # captured at import, before the autouse fixture strips HERDR_*


@pytest.mark.skipif(not os.environ.get("NW_HERDR_CONTRACT_SOCKET"),
                    reason="opt-in: NW_HERDR_CONTRACT_SOCKET=<socket of an isolated named herdr "
                           "session> (herdr --session nw-test-X server)")
def test_real_herdr_run_tabs_in_isolated_session(tmp_path):
    """SPEC-0366 contract: every run-tab argv against the real herdr, in an ISOLATED named
    session only (never the human's). Creates its own workspace there, drives `RunPanes`
    through create / overflow rename / states / a run ending and the next starting (the agent
    entry must never vanish) / adoption / close, reads each step back with `pane get`,
    `agent get`, `tab list`, `pane read`, closes the workspace."""
    sock = os.environ["NW_HERDR_CONTRACT_SOCKET"]
    human = {_REAL_ENV.get("HERDR_SOCKET_PATH"), os.path.expanduser("~/.config/herdr/herdr.sock")}
    assert sock not in human and "/sessions/" in sock, "refusing: not an isolated named session"
    env = {k: v for k, v in _REAL_ENV.items() if not k.startswith("HERDR_")}
    env.update(HERDR_ENV="1", HERDR_SOCKET_PATH=sock)
    ws = _herdr_json(env, "workspace", "create", "--label", "nw-contract", "--cwd", str(tmp_path),
                     "--no-focus")["result"]["workspace"]["workspace_id"]
    env["HERDR_WORKSPACE_ID"] = ws
    cfg = _cfg(tmp_path)
    cfg["config_dir"] = str(tmp_path / "config")
    cids = ["SPEC-0901-20261101-aaaa", "SPEC-0902-20261101-bbbb", "SPEC-0903-20261101-cccc"]
    rds = {}
    for cid in cids:
        _campaign(cfg, cid)
        _spec_at_open(cfg, cid, f"`thing {cid[7:9]}`: details")
        rds[cid] = _run(cfg, cid)

    def agent(pane):
        got = subprocess.run(["herdr", "agent", "get", pane], env=env, capture_output=True,
                             text=True, timeout=10)
        return json.loads(got.stdout)["result"]["agent"] if got.returncode == 0 else None

    def tabs():
        return {t["tab_id"]: t for t in
                _herdr_json(env, "tab", "list", "--workspace", ws)["result"]["tabs"]}

    def read(pane):
        return subprocess.run(["herdr", "pane", "read", pane, "--source", "recent",
                               "--lines", "200"], env=env, capture_output=True, text=True,
                              timeout=10).stdout
    try:
        w = HW.Watcher(cfg, H.Client(env=env), interval_s=10, env=env, run_panes=True,
                       run_pane_linger=600)
        w.run_panes.max_panes = 2
        with E.reader_lock(cfg, HW.READER):
            w.run_panes.load()
            w.push()
            rp = w.run_panes
            assert len(rp.panes) == 2 and set(rp.panes) <= set(cids), rp.client.last_error
            first, second = sorted(rp.panes, key=lambda c: rp.panes[c]["order"])
            p1, p2 = rp.panes[first]["pane_id"], rp.panes[second]["pane_id"]
            t1, t2 = rp.panes[first]["tab_id"], rp.panes[second]["tab_id"]
            assert t1 != t2
            assert tabs()[t1]["label"] == f"{first[:9]} thing {first[7:9]}"
            assert tabs()[t2]["label"] == f"{second[:9]} thing {second[7:9]} (+1)"
            assert not tabs()[t1]["focused"] and not tabs()[t2]["focused"]
            got = _herdr_json(env, "pane", "get", p1)["result"]["pane"]
            assert got["label"] == f"nw-run:{first}" and got["tab_id"] == t1
            a = agent(p1)
            assert a["agent"] == "nw-run" and a["agent_status"] == "working"
            assert a["display_agent"].startswith("nw run · implementer")
            subprocess.run(["herdr", "pane", "wait-output", p1, "--match", "result: success",
                            "--timeout", "30000"], env=env, check=True, timeout=40,
                           capture_output=True)
            text = read(p1)
            assert "→ Read" in text and "run 001-implementer · persona implementer" in text
            assert "campaign tail" not in text and "NW_DATA_DIR" not in text   # no typed line
            (third,) = set(cids) - {first, second}
            _end(rds[third])
            _set_state(cfg, third, state="QUEUED")            # the overflow clears
            w.push()
            assert tabs()[t2]["label"] == f"{second[:9]} thing {second[7:9]}"
            # run end -> parked -> next run: the agent entry must stay the whole time
            assert waits.until(lambda: (agent(p1) or {}).get("agent_status") == "working",
                               timeout=20, interval=0.5)
            _end(rds[first])
            _set_state(cfg, first, state="PARKED", park={"reason": "cost_cap"})
            subprocess.run(["herdr", "pane", "wait-output", p1, "--match",
                            "waiting for the next run", "--timeout", "30000"], env=env,
                           check=True, timeout=40, capture_output=True)
            w.push()
            samples = []
            for _ in range(6):
                samples.append(agent(p1))
                time.sleep(0.5)                               # sampling, not waiting
            assert all(s is not None for s in samples), samples
            assert samples[-1]["agent_status"] == "blocked"
            assert samples[-1]["display_agent"] == "nw run · parked: cost_cap"
            _set_state(cfg, first, state="REVIEWING")
            _run(cfg, first, "002-conformance_reviewer", persona="conformance_reviewer")
            w.push()
            subprocess.run(["herdr", "pane", "wait-output", p1, "--match",
                            "run 002-conformance_reviewer", "--timeout", "30000"], env=env,
                           check=True, timeout=40, capture_output=True)
            for _ in range(6):
                s = agent(p1)
                assert s is not None and s["agent_status"] == "working", s
                time.sleep(0.5)                               # sampling, not waiting
            assert agent(p1)["display_agent"].startswith("nw run · conformance_reviewer")
        w2 = HW.Watcher(cfg, H.Client(env=env), interval_s=10, env=env, run_panes=True)
        with E.reader_lock(cfg, HW.READER):
            w2.run_panes.load()                               # pane get + process-info
            assert set(w2.run_panes.panes) == {first, second}
            assert not any(p.get("retype") for p in w2.run_panes.panes.values())
            w2.run_panes.close_all()
        for p in (p1, p2):
            gone = subprocess.run(["herdr", "pane", "get", p], env=env, capture_output=True,
                                  text=True, timeout=10)
            assert gone.returncode == 1 and "pane_not_found" in gone.stderr
        assert t1 not in tabs() and t2 not in tabs()          # closed with their pane
    finally:
        subprocess.run(["herdr", "workspace", "close", ws], env=env, capture_output=True,
                       timeout=10)


def test_run_tab_shells_are_created_without_history(fake):
    """Every shell a run pane lives in is created with history-disabling env (fish: empty
    fish_history; bash/zsh: HISTFILE=/dev/null); the fake parses --env like herdr (KEY=VALUE,
    empty value allowed)."""
    assert H.NO_HISTORY_ENV == ("fish_history=", "HISTFILE=/dev/null")
    c = H.Client()
    assert c.call_json(["tab", "create", "--env", "fish_history=", "--env", "A=b"]) is not None
    assert c.call_json(["tab", "create", "--env", "novalue"]) is None
    assert "KEY=VALUE" in c.last_error
    for args in (H.tab_create_args("w1", "L", "/"), H.pane_split_args("w1:p1", "down", "/")):
        envs = [args[i + 1] for i, a in enumerate(args) if a == "--env"]
        assert envs == ["fish_history=", "HISTFILE=/dev/null"]
