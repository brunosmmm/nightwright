"""SPEC-0415: the campaign-history fixture, its capture tool and `tests/history_scale.copy_fixture`.

Two kinds of input: the committed fixture (`tests/fixtures/agentic_history/`) and a synthetic source
data dir built here by `build_source` (about 6 small campaigns, one per scenario predicate, with a
planted home path, email, secrets, host name, an oversized stream and a runner log with lines
inside and outside the campaigns' intervals). The tests never read the developer's real data dir."""
from __future__ import annotations

import ast
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nightwright.agentic import campaign_export as CE
from nightwright.agentic import history as H
from nightwright.agentic import queue as Q
from nightwright.agentic import schemas as SC
from nightwright.agentic import store as S
import history_scale as HS
from fixtures import dashboard_data as DD

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "capture_history_fixture.py"
MIB3 = 3 * 1024 * 1024
TZ = dt.timezone(dt.timedelta(hours=-4))
T0 = dt.datetime(2026, 10, 1, 10, 0, 0, tzinfo=TZ)

MULTI = "SPEC-0001-20261001-aaaa"       # merged_multi_loop (+ the derived HANDED_OFF base)
ABANDONED = "SPEC-0002-20261001-bbbb"
LEGACY = "SPEC-0003-20261001-cccc"      # wt schema, no stream, result.json only
ESCAL = "SPEC-0004-20261001-dddd"       # answered human_decision
FLAKY = "SPEC-0005-20261001-eeee"       # flaky_recorded + waive
LIVE = "SPEC-0006-20261001-ffff"        # not a candidate
DERIVED = "SPEC-0001-20261001-hdof"
SECRETS = ["sk-abcdefghijklmnop12345", "ghp_abcdefghijklmnopqrstuvwx", "alice@corp.example",
           "abcdefghijklmnopqrstuvwxyz0123", "zzz-env-token", "buildbox.lan", "/home/alice"]


def ts(minutes=0, seconds=0):
    return (T0 + dt.timedelta(minutes=minutes, seconds=seconds)).isoformat(timespec="seconds")


def jl(path: Path, objs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(o) + "\n" for o in objs))


def ev(cid, seq, minute, typ, **kw):
    return {"schema": "nw.ledger.event.v1", "ts": ts(minute), "seq": seq, "campaign": cid,
            "type": typ, "by": "tick", **kw}


def make_campaign(agentic: Path, cid, state, events, runs, schema="nw.campaign.v1", extra=None):
    d = agentic / "campaigns" / cid
    st = {"schema": schema, "id": cid, "kind": "implement", "spec_id": cid[:9],
          "project": "Meta-Tools", "state": state, "prev_state": "CLOSING",
          "created_at": ts(0), "repo": {"root": "/home/alice/work/repo", "base": "main",
                                        "branch": f"nw/{cid}"},
          "counters": {"loops": 1, "runs": len(runs)}, "cost": {"total_usd": 1.0},
          "close": {"mode": "merge", "merged_sha": "abc123", "pr_url": None},
          "updated_at": ts(30), **(extra or {})}
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps(st, indent=2) + "\n")
    (d / "spec_at_open.md").write_text("---\nid: X\n---\nbody for alice@corp.example\n")
    jl(d / "ledger.jsonl", events)
    for rid, files in runs.items():
        rd = d / "runs" / rid
        rd.mkdir(parents=True)
        for name, content in files.items():
            if name == "stream.jsonl":
                jl(rd / name, content)
            elif isinstance(content, (dict, list)):
                (rd / name).write_text(json.dumps(content, indent=2) + "\n")
            else:
                (rd / name).write_text(content)
        (rd / "prompt.md").write_text("not copied\n")
    return d


def run_files(stream_lines=3, **kw):
    stream = [{"type": "system", "n": i, "text": f"line {i}"} for i in range(stream_lines)]
    return {"run.json": {"schema": "nw.run.v1", "run_id": "x", "cost_usd": 0.5, "verdict": {},
                         "launch": {"env": {"TOKEN": "zzz-env-token"}},
                         "Authorization": "Bearer abcdefghijklmnopqrstuvwxyz0123", **kw},
            "result.json": {"summary": "ok"}, "exit.json": {"exit_code": 0},
            "stderr.log": "err\n" * 3, "stream.jsonl": stream}


def build_source(root: Path, runner_in_a=40) -> Path:
    """A synthetic data dir `root/agentic/...`; returns `root`."""
    ag = root / "agentic"
    # MULTI: two loops (IMPLEMENTING twice), 2 gates, 2 findings, a cost; oversized stream/log
    mev = [ev(MULTI, 1, 0, "open"), ev(MULTI, 2, 1, "transition", **{"from": "QUEUED", "to": "IMPLEMENTING"}),
           ev(MULTI, 3, 5, "run.end", run_id="001-implementer", cost_usd=0.5),
           ev(MULTI, 4, 8, "gate", ok=False), ev(MULTI, 5, 9, "finding", severity="must_fix"),
           ev(MULTI, 6, 10, "transition", **{"from": "GATING", "to": "IMPLEMENTING"}),
           ev(MULTI, 7, 15, "gate", ok=True), ev(MULTI, 8, 16, "finding", severity="nit"),
           ev(MULTI, 9, 20, "close", mode="merge", merged_sha="abc123", pr_url=None),
           ev(MULTI, 10, 30, "transition", **{"from": "CLOSING", "to": "MERGED"})]
    big = [{"type": "assistant", "n": i, "text": "x"} for i in range(300)]
    big[290] = {"type": "assistant", "text": "y" * 10000}
    big[5] = {"type": "assistant", "text": "keys sk-abcdefghijklmnop12345 ghp_abcdefghijklmnopqrstuvwx "
                                           "mail alice@corp.example in /home/alice/work/repo/x.py"}
    files = run_files(stream_lines=0)
    files["stream.jsonl"] = big
    d = make_campaign(ag, MULTI, "MERGED", mev, {"001-implementer": files})
    (d / "gates").mkdir()
    (d / "gates" / "001-pytest.log").write_text("".join(f"log line {i} buildbox.lan\n" for i in range(500)))
    (d / "gates" / "001-pytest.json").write_text('{"ok": true}\n')
    make_campaign(ag, ABANDONED, "ABANDONED",
                  [ev(ABANDONED, 1, 20, "open"), ev(ABANDONED, 2, 25, "run.end", cost_usd=0.1),
                   ev(ABANDONED, 3, 40, "transition", **{"from": "PARKED", "to": "ABANDONED"})],
                  {"001-implementer": run_files()})
    make_campaign(ag, LEGACY, "MERGED",
                  [ev(LEGACY, 1, 50, "open"), ev(LEGACY, 2, 55, "transition", **{"to": "MERGED"})],
                  {"001-implementer": {"result.json": {"summary": "old"}}},
                  schema="wt.campaign.v1")
    make_campaign(ag, ESCAL, "MERGED",
                  [ev(ESCAL, 1, 60, "open"),
                   ev(ESCAL, 2, 61, "human_decision", escalation="ESC-0001", kind="choose"),
                   ev(ESCAL, 3, 65, "transition", **{"to": "MERGED"})],
                  {"001-implementer": run_files()})
    make_campaign(ag, FLAKY, "MERGED",
                  [ev(FLAKY, 1, 70, "open"), ev(FLAKY, 2, 71, "flaky_recorded", ids=["t::a"]),
                   ev(FLAKY, 3, 75, "transition", **{"to": "MERGED"})],
                  {"001-review_triager": run_files(verdict={"waive": [{"id": "r#0"}]})})
    make_campaign(ag, LIVE, "IMPLEMENTING", [ev(LIVE, 1, 80, "open")], {})
    # cross-campaign records
    jl(ag / "conductor" / "decisions.jsonl", [
        {"seq": 1, "campaign": MULTI, "rule": "r"}, {"seq": 2, "campaign": LIVE, "rule": "r"},
        {"seq": 3, "campaign": None, "rationale": f"see {MULTI}"}, {"seq": 4, "campaign": None}])
    for n, cid in ((1, ESCAL), (2, LIVE)):
        (ag / "escalations").mkdir(exist_ok=True)
        (ag / "escalations" / f"ESC-000{n}.json").write_text(json.dumps(
            {"id": f"ESC-000{n}", "campaign": cid, "status": "answered" if n == 1 else "open",
             "question": "from alice@corp.example?"}) + "\n")
    jl(ag / "mailbox" / "conductor.jsonl", [
        {"id": "conductor:1", "campaign": MULTI, "text": "a"}, {"id": "conductor:2", "campaign": LIVE, "text": "b"},
        {"id": "conductor:3", "campaign": None, "text": f"about {FLAKY}"}])
    jl(ag / "events.jsonl", [{"id": 1, "campaign": MULTI, "type": "t"}, {"id": 2, "campaign": LIVE, "type": "t"},
                             {"id": 3, "source": {"campaign": ESCAL}, "type": "t"}])
    # runner: lines in MULTI's interval (0-30), ABANDONED's (20-40, overlapping), and far outside
    rows = [{"ts": ts(0, i), "kind": "tick", "n": i} for i in range(runner_in_a)]
    rows += [{"ts": ts(25), "kind": "tick", "n": "overlap"}, {"ts": ts(500), "kind": "tick", "n": "outside"}]
    jl(ag / "runner.jsonl", sorted(rows, key=lambda r: r["ts"]))
    jl(ag / "spec-reviews.jsonl", [{"spec_id": MULTI[:9], "kind": "end"}, {"spec_id": "SPEC-9999"}])
    (ag / "reviews" / MULTI[:9]).mkdir(parents=True)
    (ag / "reviews" / MULTI[:9] / "r.json").write_text('{"ok": 1}\n')
    (ag / "flaky-tests.json").write_text(json.dumps({"projects": {"Meta-Tools": {
        "t::a": {"campaigns": [FLAKY, LIVE], "count": 2}, "t::b": {"campaigns": [LIVE], "count": 1}}}}))
    (ag / "sentinel").mkdir()
    (ag / "sentinel" / "Meta-Tools.last.json").write_text('{"ok": true}\n')
    (ag / "workers.json").write_text(json.dumps({"workers": [{"name": "box", "ssh": "nw@buildbox.lan"}]}))
    return root


def run_tool(*args, env=None, home=None):
    e = dict(os.environ)
    e.update(env or {})
    if home:
        e.update(HOME=str(home), USER="alice")
    e.pop("NW_DATA_DIR", None)
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, env=e)


def tree_hashes(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def read_jsonl(p: Path):
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("capture_history_fixture", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["capture_history_fixture"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def captured(tmp_path_factory):
    base = tmp_path_factory.mktemp("capture")
    src = build_source(base / "src")
    out = base / "out"
    home = base / "home" / "alice"
    home.mkdir(parents=True)
    r = run_tool("--src", str(src), "--out", str(out), home=home)
    assert r.returncode == 0, r.stderr
    return {"src": src, "out": out, "home": home, "stdout": r.stdout}


def manifest_of(out: Path):
    return json.loads((out / "MANIFEST.json").read_text())


# ---- the tool on a synthetic source ------------------------------------------------------------

def test_tool_imports_only_stdlib():
    tree = ast.parse(TOOL.read_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.level == 0:
            mods.add(n.module.split(".")[0])
    assert mods <= set(sys.stdlib_module_names), mods
    r = run_tool("--help")
    assert r.returncode == 0
    for flag in ("--src", "--out", "--cid", "--no-derive-handed-off", "--check",
                 "NW_FIXTURE_MAX_BYTES", "NW_FIXTURE_DENY_FILE"):
        assert flag in r.stdout


def test_capture_synthetic_source(captured):
    out = captured["out"]
    m = manifest_of(out)
    assert m["schema"] == "nw.history.fixture.v1"
    for key in ("merged_multi_loop", "abandoned", "handed_off", "human_escalation", "pre_stream",
                "flaky_and_triage", "legacy_schema", "no_run_json"):
        assert m["scenarios"][key] != "absent_in_source", key
    assert m["scenarios"]["merged_multi_loop"] == [MULTI]
    by = {c["cid"]: c for c in m["campaigns"]}
    assert LIVE not in by and len(by) <= 12
    d = by[DERIVED]
    assert (d["origin"], d["derived_from"], d["state"]) == ("derived", MULTI, "HANDED_OFF")
    st = json.loads((out / "agentic/campaigns" / DERIVED / "state.json").read_text())
    assert st["close"]["merged_sha"] is None and st["close"]["pr_url"] == "https://example.invalid/pr/1"
    assert st["close"]["mode"] == "pr"
    # ledger lines are never dropped
    for cid, c in by.items():
        if c["origin"] == "captured":
            src_n = len((captured["src"] / "agentic/campaigns" / cid / "ledger.jsonl").read_text().splitlines())
            assert len((out / "agentic/campaigns" / cid / "ledger.jsonl").read_text().splitlines()) == src_n
    assert not list(out.rglob("prompt.md"))
    # caps
    stream = (out / "agentic/campaigns" / MULTI / "runs/001-implementer/stream.jsonl").read_text().splitlines()
    assert len(stream) <= 120 and all(len(x.encode()) <= 4096 for x in stream)
    assert json.loads(stream[0])["n"] == 0             # the first line of the original is always kept
    assert any(json.loads(x).get("truncated") for x in stream)
    log = (out / "agentic/campaigns" / MULTI / "gates/001-pytest.log").read_text().splitlines()
    assert len(log) == 200 and "log line 499" in log[-1]


def test_derived_campaign_has_own_cid(captured):
    out = captured["out"]
    d = out / "agentic/campaigns" / DERIVED
    st = json.loads((d / "state.json").read_text())
    assert st["id"] == DERIVED == d.name
    for ln in (d / "ledger.jsonl").read_text().splitlines():
        e = json.loads(ln)
        assert e["campaign"] == DERIVED
    types = [json.loads(x) for x in (d / "ledger.jsonl").read_text().splitlines()]
    assert types[-1]["to"] == "HANDED_OFF" and [e for e in types if e["type"] == "close"][-1]["mode"] == "pr"
    for p in d.rglob("*"):
        if p.is_file():
            assert MULTI.encode() not in p.read_bytes(), p
    assert (d / "runs/001-implementer/run.json").is_file()


def test_runner_window_and_caps(tmp_path):
    src = build_source(tmp_path / "src", runner_in_a=700)
    out = tmp_path / "out"
    r = run_tool("--src", str(src), "--out", str(out))
    assert r.returncode == 0, r.stderr
    rows = read_jsonl(out / "agentic/runner.jsonl")
    ns = [x["n"] for x in rows]
    assert "outside" not in ns
    assert ns.count("overlap") == 1                     # inside two intervals, kept once
    # MULTI's interval holds 701 lines (700 + the overlap one); only the last 500 survive, and the
    # ABANDONED interval (20-40) holds just the overlap line
    assert len(rows) == 500
    assert [x for x in ns if x != "overlap"][0] == 700 - 499


def test_runner_per_interval_cap_with_two_busy_intervals(tmp_path):
    """Each interval keeps its own last 500; neither is starved by the other's lines."""
    src = build_source(tmp_path / "src", runner_in_a=0)
    rows = [{"ts": ts(2, i % 60), "kind": "tick", "n": f"m{i}"} for i in range(600)]      # MULTI only
    rows += [{"ts": ts(31 + (i % 8), i // 8 % 60), "kind": "tick", "n": f"a{i}"} for i in range(600)]
    jl(src / "agentic" / "runner.jsonl", sorted(rows, key=lambda r: r["ts"]))
    out = tmp_path / "out"
    r = run_tool("--src", str(src), "--out", str(out))
    assert r.returncode == 0, r.stderr
    ns = [x["n"] for x in read_jsonl(out / "agentic/runner.jsonl")]
    assert len([n for n in ns if n.startswith("m")]) == 500
    assert len([n for n in ns if n.startswith("a")]) == 500


def test_big_run_and_result_json_are_cut(tmp_path):
    src = build_source(tmp_path / "src")
    rd = src / "agentic/campaigns" / MULTI / "runs/001-implementer"
    run = json.loads((rd / "run.json").read_text())
    run["notes"] = ["n" * 20000] * 5
    run["cost_usd"] = 0.5
    (rd / "run.json").write_text(json.dumps(run, indent=2) + "\n")
    (rd / "result.json").write_text(json.dumps({"summary": "s" * 70000, "n": 7}) + "\n")
    assert (rd / "run.json").stat().st_size > 64 * 1024
    out = tmp_path / "out"
    r = run_tool("--src", str(src), "--out", str(out))
    assert r.returncode == 0, r.stderr
    for name in ("run.json", "result.json"):
        got = json.loads((out / "agentic/campaigns" / MULTI / "runs/001-implementer" / name).read_text())
        assert got["truncated_by_fixture"] is True, name
        assert all(len(x) <= 4096 for x in tool_strings(got)), name
    assert got["n"] == 7                                 # non-text values are untouched
    assert json.loads((out / "agentic/campaigns" / MULTI / "runs/001-implementer/run.json").read_text())["cost_usd"] == 0.5


def tool_strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, list):
        for x in v:
            yield from tool_strings(x)
    elif isinstance(v, dict):
        for x in v.values():
            yield from tool_strings(x)


def test_long_gate_log_lines_are_cut_to_1kib(tmp_path):
    src = build_source(tmp_path / "src")
    g = src / "agentic/campaigns" / MULTI / "gates"
    (g / "002-long.log").write_text("short\n" + "é" * 3000 + "\nlast\n")
    rd = src / "agentic/campaigns" / MULTI / "runs/001-implementer"
    (rd / "stderr.log").write_text("e" * 5000 + "\n")
    out = tmp_path / "out"
    r = run_tool("--src", str(src), "--out", str(out))
    assert r.returncode == 0, r.stderr
    base = out / "agentic/campaigns" / MULTI
    for p in (base / "gates/002-long.log", base / "runs/001-implementer/stderr.log"):
        lines = p.read_bytes().splitlines()
        assert lines and all(len(x) <= 1024 for x in lines), p
    assert (base / "gates/002-long.log").read_text().splitlines()[0] == "short"
    assert (base / "gates/002-long.log").read_text().splitlines()[-1] == "last"


def test_secrets_under_list_and_dict_values_are_redacted(tool, tmp_path):
    san = tool.Sanitizer()
    obj = {"tokens": ["plain-1", "plain-2"], "auth": {"cookie": {"a": "plain-3"}, "n": 4},
           "password": ["plain-4", {"x": "plain-5"}], "ok": ["fine"], "count_token": 3}
    got = san.value(obj)
    assert got["tokens"] == ["[REDACTED]"] * 2
    assert got["auth"]["cookie"] == {"a": "[REDACTED]"} and got["auth"]["n"] == 4
    assert got["password"] == ["[REDACTED]", {"x": "[REDACTED]"}]
    assert got["ok"] == ["fine"] and got["count_token"] == 3
    (tmp_path / "a.json").write_text(json.dumps(obj))
    assert {r for _, _, r in tool.scan(tmp_path, [])} >= {"secret_key"}
    (tmp_path / "a.json").write_text(json.dumps(got))
    assert tool.scan(tmp_path, []) == []


def test_capture_redacts_list_secret_values(tmp_path):
    src = build_source(tmp_path / "src")
    rd = src / "agentic/campaigns" / MULTI / "runs/001-implementer"
    run = json.loads((rd / "run.json").read_text())
    run["api_keys"] = ["plainvalue-one", "plainvalue-two"]
    (rd / "run.json").write_text(json.dumps(run, indent=2) + "\n")
    out = tmp_path / "out"
    r = run_tool("--src", str(src), "--out", str(out))
    assert r.returncode == 0, r.stderr
    blob = b"".join(p.read_bytes() for p in out.rglob("*") if p.is_file())
    assert b"plainvalue" not in blob


def test_missing_required_scenario_exits_2(tmp_path):
    src = build_source(tmp_path / "src")
    shutil.rmtree(src / "agentic/campaigns" / ABANDONED)
    out = tmp_path / "out"
    r = run_tool("--src", str(src), "--out", str(out))
    assert r.returncode == 2 and "abandoned" in r.stderr
    assert not out.exists()


def test_refuses_bad_src_and_out_inside_src(tmp_path):
    assert run_tool("--src", str(tmp_path), "--out", str(tmp_path / "o")).returncode == 2
    src = build_source(tmp_path / "src")
    assert run_tool("--src", str(src), "--out", str(src / "out")).returncode == 2


def test_size_budget_exits_3(captured, tmp_path):
    out = tmp_path / "out"
    r = run_tool("--src", str(captured["src"]), "--out", str(out), env={"NW_FIXTURE_MAX_BYTES": "1000"})
    assert r.returncode == 3 and "largest campaign" in r.stderr
    assert not out.exists()


def test_capture_refuses_to_delete_a_foreign_out(tmp_path):
    src = build_source(tmp_path / "src")
    out = tmp_path / "precious"
    out.mkdir()
    (out / "notes.txt").write_text("mine")
    r = run_tool("--src", str(src), "--out", str(out))
    assert r.returncode == 2 and "refusing" in r.stderr
    assert (out / "notes.txt").read_text() == "mine"
    (out / "MANIFEST.json").write_text('{"schema": "something.else"}')     # wrong schema: still refused
    assert run_tool("--src", str(src), "--out", str(out)).returncode == 2
    assert (out / "notes.txt").read_text() == "mine"
    (out / "MANIFEST.json").write_text("not json")
    assert run_tool("--src", str(src), "--out", str(out)).returncode == 2
    assert (out / "notes.txt").read_text() == "mine"
    f = tmp_path / "afile"
    f.write_text("x")
    assert run_tool("--src", str(src), "--out", str(f)).returncode == 2 and f.read_text() == "x"


def test_capture_replaces_an_empty_or_fixture_out(captured, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run_tool("--src", str(captured["src"]), "--out", str(empty)).returncode == 0
    assert (empty / "MANIFEST.json").is_file()
    stale = tmp_path / "stale"
    shutil.copytree(captured["out"], stale)
    (stale / "agentic/stale.txt").write_text("old")
    assert run_tool("--src", str(captured["src"]), "--out", str(stale)).returncode == 0
    assert not (stale / "agentic/stale.txt").exists()
    assert run_tool("--check", "--out", str(stale)).returncode == 0


def test_capture_refuses_src_inside_or_equal_out(tmp_path):
    out = tmp_path / "out"
    src = build_source(out / "src")
    (out / "MANIFEST.json").write_text(json.dumps({"schema": "nw.history.fixture.v1", "files": {}}))
    before = tree_hashes(src)
    r = run_tool("--src", str(src), "--out", str(out))
    assert r.returncode == 2 and "inside --out" in r.stderr
    assert tree_hashes(src) == before
    r = run_tool("--src", str(src), "--out", str(tmp_path))          # an ancestor of --src
    assert r.returncode == 2 and (src / "agentic/campaigns").is_dir()


def test_no_derive_flag(tmp_path):
    src = build_source(tmp_path / "src")
    out = tmp_path / "out"
    r = run_tool("--src", str(src), "--out", str(out), "--no-derive-handed-off")
    assert r.returncode == 0, r.stderr
    assert manifest_of(out)["scenarios"]["handed_off"] == "absent_in_source"


def test_sanitization_redacts_and_verifies(captured, tmp_path):
    out = captured["out"]
    blob = b"".join(p.read_bytes() for p in out.rglob("*") if p.is_file())
    for secret in SECRETS:
        assert secret.encode() not in blob, secret
    m = manifest_of(out)
    rules = m["sanitization"]["rules"]
    assert rules["home_path"] and rules["email"] and rules["secret"] and rules["env"] and rules["host"]
    assert b"worker-1" in blob and b"user@example.invalid" in blob and b"/home/user/" in blob
    run = json.loads((out / "agentic/campaigns" / MULTI / "runs/001-implementer/run.json").read_text())
    assert run["launch"]["env"] == {} and run["Authorization"] == "[REDACTED]"
    # a planted string no rule redacts, listed in the deny file, fails the scan
    (captured["src"] / "agentic/campaigns" / MULTI / "spec_at_open.md").write_text("CODENAME-HYDRA\n")
    deny = tmp_path / "deny.txt"
    deny.write_text("CODENAME-HYDRA\n")
    out2 = tmp_path / "out2"
    try:
        r = run_tool("--src", str(captured["src"]), "--out", str(out2), env={"NW_FIXTURE_DENY_FILE": str(deny)})
    finally:
        (captured["src"] / "agentic/campaigns" / MULTI / "spec_at_open.md").write_text("---\nid: X\n---\nbody\n")
    assert r.returncode == 4 and not out2.exists()
    assert not list(tmp_path.glob(".out2.tmp-*"))


def test_scan_function(tool, tmp_path):
    (tmp_path / "a.jsonl").write_text(json.dumps({"x": "ok /home/user/x", "env": {"A": "b"},
                                                   "t": "sk-abcdefghijklmnop12345"}) + "\n")
    (tmp_path / "b.json").write_text(json.dumps({"p": "/home/someone/z", "mail": "a@b.example"}))
    hits = {(r, rule) for r, _, rule in tool.scan(tmp_path, ["nope"])}
    assert {("a.jsonl", "env"), ("a.jsonl", "secret"), ("b.json", "home_path"), ("b.json", "email")} <= hits
    assert not [h for h in tool.scan(tmp_path, []) if h[0] == "a.jsonl" and h[2] == "home_path"]


def test_source_untouched_and_deterministic(tmp_path):
    src = build_source(tmp_path / "src")
    before = tree_hashes(src)
    r1 = run_tool("--src", str(src), "--out", str(tmp_path / "o1"))
    r2 = run_tool("--src", str(src), "--out", str(tmp_path / "o2"))
    assert r1.returncode == r2.returncode == 0
    assert tree_hashes(src) == before
    assert tree_hashes(tmp_path / "o1") == tree_hashes(tmp_path / "o2")


def test_check_detects_drift(captured, tmp_path):
    out = tmp_path / "out"
    shutil.copytree(captured["out"], out)
    assert run_tool("--check", "--out", str(out)).returncode == 0
    victim = out / "agentic/events.jsonl"
    original = victim.read_bytes()
    victim.write_bytes(original + b"\n")
    r = run_tool("--check", "--out", str(out))
    assert r.returncode == 1 and "agentic/events.jsonl" in r.stderr
    victim.write_bytes(original)
    extra = out / "agentic/extra.txt"
    extra.write_text("x")
    r = run_tool("--check", "--out", str(out))
    assert r.returncode == 1 and "agentic/extra.txt" in r.stderr
    extra.unlink()
    victim.unlink()
    r = run_tool("--check", "--out", str(out))
    assert r.returncode == 1 and "agentic/events.jsonl" in r.stderr


@pytest.mark.parametrize("text", ["", "not json", "[]", "null", '{"schema": "nw.history.fixture.v1"}',
                                  '{"schema": "nw.history.fixture.v1", "files": []}',
                                  '{"schema": "nw.history.fixture.v1", "files": {"a": 1}}',
                                  '{"schema": "other", "files": {}}'])
def test_check_malformed_manifest_exits_1(tmp_path, text):
    (tmp_path / "agentic").mkdir()
    (tmp_path / "agentic/x.txt").write_text("x")
    (tmp_path / "MANIFEST.json").write_text(text)
    r = run_tool("--check", "--out", str(tmp_path))
    assert r.returncode == 1 and "Traceback" not in r.stderr and "MANIFEST.json" in r.stderr


# ---- the committed fixture ---------------------------------------------------------------------

def fixture_manifest():
    return manifest_of(HS.FIXTURE_DIR)


def fixture_files():
    return [p for p in HS.FIXTURE_DIR.rglob("*") if p.is_file() and p.name != "MANIFEST.json"]


def test_committed_fixture_matches_manifest():
    r = run_tool("--check")
    assert r.returncode == 0, r.stderr
    m = fixture_manifest()
    assert m["totals"]["campaigns"] <= 12 and len(m["campaigns"]) <= 12
    assert sum(p.stat().st_size for p in fixture_files()) <= MIB3
    assert "tests/fixtures/agentic_history/** -text" in (ROOT / ".gitattributes").read_text().splitlines()


def test_committed_fixture_scenarios(tool):
    m = fixture_manifest()
    for key in ("merged_multi_loop", "abandoned", "handed_off", "human_escalation", "pre_stream",
                "flaky_and_triage"):
        assert isinstance(m["scenarios"][key], list) and m["scenarios"][key], key
    for key in ("legacy_schema", "no_run_json"):
        assert m["scenarios"][key]
    states = {c["state"] for c in m["campaigns"]}
    assert {"MERGED", "ABANDONED", "HANDED_OFF"} <= states
    agentic = HS.FIXTURE_DIR / "agentic"
    esc = {p.stem: json.loads(p.read_text()) for p in (agentic / "escalations").glob("ESC-*.json")}
    for key, cids in m["scenarios"].items():
        for cid in ([] if cids == "absent_in_source" else cids):
            c = tool.Camp(agentic / "campaigns" / cid)
            assert tool.predicates(c, esc)[key], (key, cid)
    for c in m["campaigns"]:
        d = agentic / "campaigns" / c["cid"]
        assert json.loads((d / "state.json").read_text())["id"] == c["cid"]
        if c["origin"] == "derived":
            st = json.loads((d / "state.json").read_text())
            assert c["derived_from"] and st["close"].get("merged_sha") is None
            for p in d.rglob("*"):
                if p.is_file():
                    assert c["derived_from"].encode() not in p.read_bytes(), p


def test_committed_fixture_sanitized(tool):
    deny = tool.load_deny()
    assert tool.scan(HS.FIXTURE_DIR, deny) == []


def test_committed_fixture_caps(tool):
    agentic = HS.FIXTURE_DIR / "agentic"
    for p in agentic.rglob("stream.jsonl"):
        lines = p.read_bytes().splitlines()
        assert len(lines) <= 120 and all(len(x) <= 4096 for x in lines), p
    for p in list(agentic.rglob("*.log")):
        lines = p.read_bytes().splitlines()
        assert len(lines) <= 200 and all(len(x) <= 1024 for x in lines), p
    m = fixture_manifest()
    # ledgers are never truncated
    for c in m["campaigns"]:
        n = len((agentic / "campaigns" / c["cid"] / "ledger.jsonl").read_text().splitlines())
        assert n == c["ledger_lines"]
    # runner.jsonl: only lines inside some chosen (captured) campaign's interval, <= 500 each
    ivs = [tool.Camp(agentic / "campaigns" / c["cid"]).interval() for c in m["campaigns"]
           if c["origin"] == "captured"]
    rows = read_jsonl(agentic / "runner.jsonl") if (agentic / "runner.jsonl").exists() else []
    stamps = [dt.datetime.fromisoformat(r["ts"]) for r in rows]
    assert all(any(a <= t <= b for a, b in ivs) for t in stamps)
    for a, b in ivs:
        assert sum(1 for t in stamps if a <= t <= b) <= 500, (a, b)
    assert len(rows) <= 500 * len(ivs)


def test_records_reference_fixture_campaigns():
    m = fixture_manifest()
    agentic = HS.FIXTURE_DIR / "agentic"
    real = {c["cid"] for c in m["campaigns"] if c["origin"] == "captured"}
    for p in [agentic / "conductor/decisions.jsonl", agentic / "events.jsonl",
              *(agentic / "mailbox").glob("*.jsonl")]:
        for r in read_jsonl(p) if p.exists() else []:
            cid = r.get("campaign") or (r.get("source") or {}).get("campaign")
            assert cid is None or cid in real, (p, cid)
    esc_files = {p.stem: json.loads(p.read_text()) for p in (agentic / "escalations").glob("ESC-*.json")}
    assert all(e["campaign"] in real for e in esc_files.values())
    import re
    for c in real:
        text = (agentic / "campaigns" / c / "ledger.jsonl").read_text()
        for eid in set(re.findall(r"ESC-\d{4}", text)):
            assert eid in esc_files, (c, eid)
    flaky = agentic / "flaky-tests.json"
    if flaky.exists():
        for tests in json.loads(flaky.read_text())["projects"].values():
            for ent in tests.values():
                assert set(ent["campaigns"]) <= real


# ---- copy_fixture and the readers ----------------------------------------------------------------

def test_copy_fixture(tmp_path):
    dst = HS.copy_fixture(tmp_path / "d")
    m = fixture_manifest()
    got = tree_hashes(dst)
    assert got == {k: v["sha256"] for k, v in m["files"].items()}
    committed = {p.stat().st_ino for p in fixture_files()}
    assert not any(p.stat().st_ino in committed for p in dst.rglob("*") if p.is_file())
    with pytest.raises(FileExistsError):
        HS.copy_fixture(dst)
    (tmp_path / "empty").mkdir()
    HS.copy_fixture(tmp_path / "empty")


def test_committed_fixture_is_a_data_dir(tmp_path):
    dst = HS.copy_fixture(tmp_path / "data")
    cfg = DD.make_cfg(dst, tmp_path / "cfg")
    m = fixture_manifest()
    cids = {c["cid"] for c in m["campaigns"]}
    assert {p.name for p in S.campaigns_dir(cfg).iterdir()} == cids
    for cid in cids:
        d = S.campaign_dir(cfg, cid)
        assert json.loads((d / "state.json").read_text())["id"] == cid
        for e in read_jsonl(d / "ledger.jsonl"):
            assert e["type"] and e["ts"]
    assert {s["id"] for s in S.list_campaigns(cfg, include_terminal=True)} == cids
    before = tree_hashes(dst)
    payload = Q.dashboard_payload(cfg)
    assert payload
    tables = CE.export_tables(cfg)
    assert {r["campaign"] if "campaign" in r else r.get("id") for r in tables["campaigns"]} >= cids
    assert tree_hashes(dst) == before
    assert {k: v["sha256"] for k, v in m["files"].items()} == {
        k: v for k, v in tree_hashes(HS.FIXTURE_DIR).items() if k != "MANIFEST.json"}


@pytest.mark.parametrize("user,home", [("nw", "/home/nw"), ("agentic", "/home/agentic"),
                                       ("campaigns", "/home/campaigns"), ("bruno", "/home/bruno")])
def test_check_is_independent_of_the_running_user(user, home):
    """`--check` of the committed fixture must not depend on $USER or $HOME (gate workers differ)."""
    r = run_tool("--check", env={"USER": user, "HOME": home})
    assert r.returncode == 0, r.stderr


def test_scan_user_segments_only_when_given(tool, tmp_path):
    (tmp_path / "a.txt").write_text("see /nw/thing and /home/user/x\n")
    assert tool.scan(tmp_path, []) == []
    assert ("a.txt", 1, "user_segment") in tool.scan(tmp_path, [], users=["nw"])
    assert tool.scan(tmp_path, [], users=["user", ""]) == []


# ---- JSONL lines split on '\n' only (U+2028 and friends stay inside the JSON string) -------------

LS = " "
PLANTED = ('{"type":"x","text":"a%sb","env":{"GH_TOKEN":"abc"},"authorization":"opaque-value"}\n' % LS)


def test_jsonl_lines_split_on_newline_only(tool):
    assert tool.jsonl_lines("a b c\x0b\x1d\u0085\nd\ne") == ["a b c\x0b\x1d\u0085\n", "d\n", "e"]
    assert tool.jsonl_lines("") == [] and tool.jsonl_lines("a\n") == ["a\n"]


def test_unicode_line_separator_does_not_bypass_redaction(tool, tmp_path):
    got = tool.san_jsonl(PLANTED.encode(), tool.Sanitizer()).decode()
    obj = json.loads(got)
    assert obj["env"] == {} and obj["authorization"] == "[REDACTED]" and LS in obj["text"]
    assert "opaque-value" not in got and "GH_TOKEN" not in got
    assert tool.cap_stream(PLANTED.encode(), tool.Sanitizer()).decode().count("\n") == 1
    (tmp_path / "x.jsonl").write_text(PLANTED, encoding="utf-8")
    assert {"env", "secret_key"} <= {r for _, _, r in tool.scan(tmp_path, [])}
    (tmp_path / "x.jsonl").write_text(got, encoding="utf-8")
    assert tool.scan(tmp_path, []) == []
    rows = tool.read_jsonl(tmp_path / "x.jsonl")
    assert len(rows) == 1 and rows[0][1] is not None


def test_capture_redacts_planted_line_separator_line(tmp_path):
    src = build_source(tmp_path / "src")
    sp = src / "agentic/campaigns" / MULTI / "runs/001-implementer/stream.jsonl"
    lines = sp.read_text(encoding="utf-8").splitlines(keepends=True)
    sp.write_text("".join(lines[:-1]) + PLANTED, encoding="utf-8")
    out = tmp_path / "out"
    r = run_tool("--src", str(src), "--out", str(out))
    assert r.returncode == 0, r.stderr
    blob = (out / "agentic/campaigns" / MULTI / "runs/001-implementer/stream.jsonl").read_text(encoding="utf-8")
    assert "opaque-value" not in blob and "GH_TOKEN" not in blob


def test_derive_handed_off_tolerates_non_json_ledger_line(tool):
    orig, new = "SPEC-1-20260101-aaaa", "SPEC-1-20260101-hdof"
    led = (f'{{"type":"open","campaign":"{orig}"}}\nnot json at all\n'
           f'{{"type":"transition","to":"MERGED","campaign":"{orig}"}}\n').encode()
    st = json.dumps({"id": orig, "state": "MERGED"}).encode()
    got = tool.derive_handed_off({"state.json": st, "ledger.jsonl": led}, orig, new)
    lines = got["ledger.jsonl"].decode().splitlines()
    assert lines[1] == "not json at all" and json.loads(lines[2])["to"] == "HANDED_OFF"
    assert orig.encode() not in got["ledger.jsonl"]


def test_recapture_replaces_out_and_leaves_no_side_dirs(tmp_path):
    src = build_source(tmp_path / "src")
    out = tmp_path / "out"
    assert run_tool("--src", str(src), "--out", str(out)).returncode == 0
    first = tree_hashes(out)
    assert run_tool("--src", str(src), "--out", str(out)).returncode == 0
    assert tree_hashes(out) == first
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out", "src"]


# ---- review fixes: --cid pin, IPv4 map, sanitize-then-cut, mail guard ---------------------------
ABANDONED2 = "SPEC-0007-20261001-1111"      # a second abandoned candidate, bigger than ABANDONED


def test_cid_pin_overrides_automatic_selection(tmp_path):
    src = build_source(tmp_path / "src")
    make_campaign(src / "agentic", ABANDONED2, "ABANDONED",
                  [ev(ABANDONED2, 1, 20, "open"), ev(ABANDONED2, 2, 25, "run.end", cost_usd=0.1),
                   ev(ABANDONED2, 3, 40, "transition", **{"from": "PARKED", "to": "ABANDONED"})],
                  {f"00{i}-implementer": run_files() for i in range(1, 4)})
    auto = tmp_path / "auto"
    r = run_tool("--src", str(src), "--out", str(auto))
    assert r.returncode == 0, r.stderr
    assert manifest_of(auto)["scenarios"]["abandoned"] == [ABANDONED]     # smaller one wins unpinned
    pinned = tmp_path / "pinned"
    r = run_tool("--src", str(src), "--out", str(pinned), "--cid", ABANDONED2)
    assert r.returncode == 0, r.stderr
    m = manifest_of(pinned)
    assert m["scenarios"]["abandoned"] == [ABANDONED2]
    cids = {c["cid"]: c for c in m["campaigns"]}
    assert ABANDONED2 in cids and "abandoned" in cids[ABANDONED2]["scenarios"]
    assert ABANDONED not in cids and not (pinned / "agentic/campaigns" / ABANDONED).exists()
    assert (pinned / "agentic/campaigns" / ABANDONED2 / "state.json").is_file()


def test_private_ipv4_is_rewritten_with_a_stable_map(tmp_path):
    src = build_source(tmp_path / "src")
    a, b = "192.168.1.9", "172.16.0.4"
    base = src / "agentic/campaigns"
    (base / ABANDONED / "spec_at_open.md").write_text(f"x {b} y {a}\n")
    (base / MULTI / "spec_at_open.md").write_text(f"{a} {a} {b} 10.1.2.3\n")
    out = tmp_path / "out"
    r = run_tool("--src", str(src), "--out", str(out))
    assert r.returncode == 0, r.stderr
    got = {cid: (out / "agentic/campaigns" / cid / "spec_at_open.md").read_text().split()
           for cid in (MULTI, ABANDONED)}
    blob = "".join(p.read_text(errors="replace") for p in out.rglob("*") if p.is_file())
    for ip in (a, b, "10.1.2.3"):
        assert ip not in blob, ip
    m_a, m_a2, m_b, m_c = got[MULTI]
    assert m_a == m_a2 and len({m_a, m_b, m_c}) == 3
    assert all(x.startswith("10.0.0.") for x in got[MULTI])
    assert got[ABANDONED] == ["x", m_b, "y", m_a]                  # same map across files
    assert manifest_of(out)["sanitization"]["rules"]["ipv4"] >= 6


def test_secret_straddling_the_json_cut_is_redacted_first(tmp_path):
    src = build_source(tmp_path / "src")
    rd = src / "agentic/campaigns" / MULTI / "runs/001-implementer"
    run = json.loads((rd / "run.json").read_text())
    run["notes"] = ["a" * 4080 + "ghp_" + "B" * 36, "c" * 4075 + " Bearer " + "D" * 30,
                    "e" * 4084 + "sk-" + "F" * 30, "pad" * 20000]
    (rd / "run.json").write_text(json.dumps(run, indent=2) + "\n")
    out = tmp_path / "out"
    r = run_tool("--src", str(src), "--out", str(out))
    assert r.returncode == 0, r.stderr
    got = json.loads((out / "agentic/campaigns" / MULTI / "runs/001-implementer/run.json").read_text())
    assert got["truncated_by_fixture"] is True
    blob = json.dumps(got)
    for part in ("ghp_", "BBBB", "DDDD", "sk-", "FFFF"):
        assert part not in blob, part


def _clean(tool, data: bytes):
    text = data.decode()
    assert all(m.group(1) == "user" for m in tool.PATH_RE.finditer(text)), text[-40:]
    assert all(m.group(0) == tool.PLACEHOLDER_EMAIL for m in tool.EMAIL_RE.finditer(text)), text[-40:]


def test_cut_cannot_leave_a_half_path_or_half_email(tool):
    san = tool.Sanitizer()
    for pad in range(1000, 1030):                                  # gate / stderr log lines (1 KiB)
        raw = ("x" * pad + " /home/alice/w user@corp.example\n").encode()
        _clean(tool, tool.cap_log(raw, san))
    for pad in range(1000, 1030):
        raw = ("x" * pad + " alice@corp.example /home/alice\n").encode()
        _clean(tool, tool.cap_log(raw, san))
    for pad in range(4070, 4100):                                  # oversized run / result values
        obj = {"a": "x" * pad + " /home/alice/w user@corp.example", "p": "y" * 70000}
        raw = json.dumps(obj).encode()
        out = tool.cap_big_json(tool.sanitize_file("run.json", raw, san))
        _clean(tool, out)
    for pad in range(480, 530):                                    # truncated stream lines (512 chars)
        line = {"type": "assistant", "text": "z" * pad + " /home/alice/w user@corp.example " + "q" * 5000}
        out = tool.cap_stream((json.dumps(line) + "\n").encode(), san)
        assert json.loads(out)["truncated"] is True
        _clean(tool, out)


def test_mail_about_a_chosen_cid_for_another_campaign_is_dropped(tmp_path):
    src = build_source(tmp_path / "src")
    with (src / "agentic/mailbox/conductor.jsonl").open("a") as f:
        f.write(json.dumps({"id": "conductor:9", "campaign": LIVE, "text": f"re {MULTI}"}) + "\n")
    out = tmp_path / "out"
    r = run_tool("--src", str(src), "--out", str(out))
    assert r.returncode == 0, r.stderr
    cids = {c["cid"] for c in manifest_of(out)["campaigns"]}
    mail = [o for p in (out / "agentic/mailbox").glob("*.jsonl") for o in read_jsonl(p)]
    assert mail and all(not o.get("campaign") or o["campaign"] in cids for o in mail)
    assert not any(LIVE in json.dumps(o) for o in mail)


# ---- SPEC-0406: derivation over the committed fixture -------------------------------------

HISTORY_NOW = dt.datetime(2026, 11, 4, tzinfo=dt.timezone.utc)


def history_cfg(tmp_path):
    data = HS.copy_fixture(tmp_path / "history-data")
    return DD.make_cfg(data, tmp_path / "history-cfg")


def test_history_fixture_documents_index_phases_and_records_validate(tmp_path):
    cfg = history_cfg(tmp_path)
    rows = H.history_index(cfg, now=HISTORY_NOW)
    assert len(rows) == 8
    records = H.history_records(cfg)
    assert all(not SC.record_problems_v1(rec) for rec in records)
    assert {rec["source"] for rec in records} == SC.RECORD_SOURCES
    assert SC.RECORD_KINDS == {rec["kind"] for rec in records}
    for row in rows:
        assert SC.index_problems(row) == []
        doc = H.campaign_history(cfg, row["campaign"], now=HISTORY_NOW)
        assert SC.history_problems(doc) == []
        assert {key: row[key] for key in SC.HISTORY_HEADER_KEYS} == doc["header"]
        assert row["dominant"] == doc["dominant"]
        assert sum(phase["wall_s"] for phase in doc["phases"]) == pytest.approx(
            doc["header"]["wall_s"], abs=1)
        for phase in doc["phases"]:
            assert sum(phase[key] for key in (
                "run_s", "gate_s", "gate_wait_s", "runner_down_s", "other_s"
            )) == phase["wall_s"]
        state = S.read_state(cfg, row["campaign"])
        events = S.read_ledger(cfg, row["campaign"])
        visits = H.phase_visits(
            state, events, H._parse_ts(doc["header"]["created_at"]),
            H._parse_ts(doc["header"]["closed_at"]) or HISTORY_NOW,
        )
        assert max((visit.loop for visit in visits), default=0) == doc["header"]["loops"]

    question = next(rec for rec in records if rec["source"] == "mailbox"
                    and rec["title"] == "question"
                    and rec["campaign"] == "SPEC-0394-20261102-ae5b")
    assert question["title"] == "question"
    assert question["status"] == "answered"
    queue_notes = [rec for rec in records if rec["kind"] == "note" and rec["agent"] == "queue"]
    assert queue_notes
    assert any(rec["kind"] == "note" and rec["agent"] == "conductor" for rec in records)

    gate_doc = H.campaign_history(cfg, "SPEC-0394-20261102-ae5b", now=HISTORY_NOW)
    first = next(gate for gate in gate_doc["gates"] if gate["gate_seq"] == 1)
    result = next(item for item in first["results"] if item["name"] == "pytest-affected")
    assert result["retries"] == 1
    assert all(item["log"].startswith("gates/")
               for gate in gate_doc["gates"] for item in gate["results"] if item["log"])


def test_history_fixture_export_agreement(tmp_path):
    cfg = history_cfg(tmp_path)
    tables = CE.export_tables(cfg, now=HISTORY_NOW)
    for row in tables["campaigns"]:
        state = S.read_state(cfg, row["campaign"])
        events = S.read_ledger(cfg, row["campaign"])
        opened = next((H._parse_ts(event.get("ts")) for event in events
                       if event.get("type") == "open"), H._parse_ts(state.get("created_at")))
        ended = next((H._parse_ts(event.get("ts")) for event in reversed(events)
                      if event.get("type") == "transition"
                      and event.get("to") in S.TERMINAL_STATES), None)
        end = ended if state.get("state") in S.TERMINAL_STATES else HISTORY_NOW
        assert row["time_in_state_s"] == H.time_in_state(state, events, opened, end)
        assert row["loops"] == H.campaign_history(
            cfg, row["campaign"], now=HISTORY_NOW)["header"]["loops"]


def test_history_fixture_reads_are_pure_and_import_is_ui_independent(tmp_path):
    cfg = history_cfg(tmp_path)
    root = Path(cfg["data_dir"])
    before = tree_hashes(root)
    cids = [row["campaign"] for row in H.history_index(cfg, now=HISTORY_NOW)]
    for cid in cids:
        H.campaign_history(cfg, cid, now=HISTORY_NOW)
        H.campaign_signature(cfg, cid)
    state = S.read_state(cfg, cids[0])
    events = S.read_ledger(cfg, cids[0])
    start = H._parse_ts(state.get("created_at"))
    H.is_fix_loop(events[0])
    H.count_loops(events)
    H.loop_max(cfg, state)
    H.time_in_state(state, events, start, HISTORY_NOW)
    H.phase_visits(state, events, start, HISTORY_NOW)
    assert H.history_records(cfg)
    H.filter_index(H.history_index(cfg, now=HISTORY_NOW), project=None, state="all", spec=None,
                   since=None, until=None, min_cost=None)
    assert tree_hashes(root) == before
    assert {path.name for path in tmp_path.iterdir()} == {"history-data"}
    code = (
        "import sys; import nightwright.agentic.history; "
        "assert 'textual' not in sys.modules; "
        "assert not any(x == 'nightwright.tui' or x.startswith('nightwright.tui.') "
        "for x in sys.modules); "
        "assert not any('dashboard' in x for x in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], check=True, env=os.environ.copy())


def test_history_bad_inputs_become_gaps(tmp_path):
    cfg = history_cfg(tmp_path)
    cid = "SPEC-0179-20261025-158c"
    root = Path(cfg["data_dir"]) / "agentic"
    campaign = root / "campaigns" / cid
    with (campaign / "ledger.jsonl").open("a") as stream:
        stream.write("{")
    (campaign / "runs" / "missing").mkdir()
    (root / "conductor" / "decisions.jsonl").write_text("{\n")
    (root / "runner.jsonl").unlink()
    state = json.loads((campaign / "state.json").read_text())
    state["state"] = "UNKNOWN_X"
    (campaign / "state.json").write_text(json.dumps(state))
    doc = H.campaign_history(cfg, cid, now=HISTORY_NOW)
    assert SC.history_problems(doc) == []
    assert any("ledger: line" in gap for gap in doc["gaps"])
    assert "run missing: no run.json" in doc["gaps"]
    assert "run missing: no run record" in doc["gaps"]
    assert "state UNKNOWN_X: no phase" in doc["gaps"]
    assert "decisions.jsonl: line 1 unparsable" in doc["gaps"]
    assert "runner.jsonl: missing" in doc["gaps"]


def test_history_schema_validators_reject_independent_contract_mutations(tmp_path):
    cfg = history_cfg(tmp_path)
    doc = H.campaign_history(cfg, "SPEC-0394-20261102-ae5b", now=HISTORY_NOW)
    row = next(item for item in H.history_index(cfg, now=HISTORY_NOW)
               if item["campaign"] == doc["campaign"])
    rec = next(item for item in doc["records"] if item["kind"] == "finding")

    def bad_history(change):
        altered = copy.deepcopy(doc)
        change(altered)
        assert SC.history_problems(altered)

    bad_history(lambda value: value.__setitem__("schema", "bad"))
    bad_history(lambda value: value["header"].pop("campaign"))
    bad_history(lambda value: value["phases"][0].pop("wall_s"))
    bad_history(lambda value: value["timeline"][0].pop("kind"))
    bad_history(lambda value: value["runs"][0].pop("status"))
    bad_history(lambda value: value["gates"][0]["results"][0].pop("retries"))
    bad_history(lambda value: value["records"][0].pop("links"))
    bad_history(lambda value: value["header"].__setitem__("outcome", "unknown"))
    bad_history(lambda value: value["phases"][0].__setitem__("phase", "unknown"))
    bad_history(lambda value: value["runs"][0].__setitem__("status", "unknown"))
    bad_history(lambda value: value["header"].__setitem__("wall_s", -1))
    bad_history(lambda value: value.__setitem__("timeline", {}))

    allowed = copy.deepcopy(doc)
    allowed["extra"] = True
    allowed["header"]["title"] = None
    assert SC.history_problems(allowed) == []

    broken_rec = copy.deepcopy(rec)
    broken_rec["schema"] = "bad"
    assert SC.record_problems_v1(broken_rec)
    broken_rec = copy.deepcopy(rec)
    broken_rec["links"]["gate_seq"] = -1
    assert SC.record_problems_v1(broken_rec)
    allowed_rec = copy.deepcopy(rec)
    allowed_rec["extra"] = True
    allowed_rec["text"] = None
    assert SC.record_problems_v1(allowed_rec) == []

    broken_index = copy.deepcopy(row)
    broken_index.pop("signature")
    assert SC.index_problems(broken_index)
    broken_index = copy.deepcopy(row)
    broken_index["phase_wall_s"]["queue"] = -1
    assert SC.index_problems(broken_index)
    allowed_index = copy.deepcopy(row)
    allowed_index["extra"] = True
    allowed_index["closed_at"] = None
    assert SC.index_problems(allowed_index) == []
