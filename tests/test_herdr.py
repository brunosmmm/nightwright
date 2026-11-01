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

FAKE = r'''#!{py}
import json, os, sys, time
mode = os.environ.get("FAKE_HERDR_MODE", "ok")
with open(os.environ["FAKE_HERDR_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
# real herdr 0.9.5 parser: report-metadata's positional id must precede every option, and
# each kind accepts only its own options (workspaces have no --display-agent)
if sys.argv[2:3] == ["report-metadata"]:
    if len(sys.argv) < 4 or sys.argv[3].startswith("-"):
        sys.stderr.write("unknown option: nw\n")
        sys.exit(2)
    flags = {"--source": 1, "--seq": 1, "--ttl-ms": 1, "--token": 1, "--clear-token": 1}
    if sys.argv[1] == "pane":
        flags.update({"--display-agent": 1, "--clear-display-agent": 0, "--title": 1,
                      "--clear-title": 0, "--state-label": 1, "--clear-state-labels": 0,
                      "--agent": 1, "--applies-to-source": 1})
    i = 4
    while i < len(sys.argv):
        if sys.argv[i] not in flags:
            sys.stderr.write("unknown option: " + sys.argv[i] + "\n")
            sys.exit(2)
        i += 1 + flags[sys.argv[i]]
if mode == "hang":
    time.sleep(30)
sys.exit(1 if mode == "fail" else 0)
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A fake `herdr` (argv -> one JSON line in the log) and a herdr-like env."""
    for k in [k for k in os.environ if k.startswith("HERDR_")]:
        monkeypatch.delenv(k)
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    exe = bindir / "herdr"
    exe.write_text(FAKE.replace("{py}", sys.executable))
    exe.chmod(0o755)
    log = tmp_path / "herdr.log"
    monkeypatch.setenv("FAKE_HERDR_LOG", str(log))
    monkeypatch.setenv("FAKE_HERDR_MODE", "ok")
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


def test_notifications_once_per_event_with_sounds(fake, tmp_path):
    cfg = _cfg(tmp_path)
    w = HW.Watcher(cfg, H.Client(), interval_s=10)
    w.run(once=True)                                     # creates the cursor at the end
    _drive(cfg)
    w.run(once=True)
    notes = _notes(fake.calls())
    assert sorted(notes) == sorted([("nw: run failed", "request"), ("nw: parked", "request"),
                                    ("nw: needs you", "request"), ("nw: merged", "done")])
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
    assert n({"type": "parked", "data": {"reason": "gate_failed"}})[2] == "request"
    assert n({"type": "admitted", "data": {}}) is None
    assert n({"type": "merged", "spec_id": "SPEC-1", "data": {"merged_sha": "abc"}}) == \
        ("nw: merged", "SPEC-1 abc", "done")


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
