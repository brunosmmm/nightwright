"""SPEC-0415: the campaign-history fixture, its capture tool and `tests/history_scale.copy_fixture`.

Two kinds of input: the committed fixture (`tests/fixtures/agentic_history/`) and a synthetic source
data dir built here by `build_source` (about 6 small campaigns, one per scenario predicate, with a
planted home path, email, secrets, host name, an oversized stream and a runner log with lines
inside and outside the campaigns' intervals). The tests never read the developer's real data dir."""
from __future__ import annotations

import ast
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
from nightwright.agentic import queue as Q
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
