"""SPEC-0486: backend preflight for spec review and Pi."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_agentic_e2e import added, make_cfg, make_repo, script
from test_agentic_spec_writer import in_review, start as spec_start
from test_runner_selfheal import _cursor_cfg, _no_key, journal
from nightwright.agentic import backend_preflight as BP
from nightwright.agentic import escalations as ESC
from nightwright.agentic import persona_gate as PG
from nightwright.agentic import project as PJ
from nightwright.agentic import queue as Q
from nightwright.agentic import remote_jobs as RJ
from nightwright.agentic import runproc as RP
from nightwright.agentic import store as S
from nightwright.agentic.runner import (
    ClaudeRunner, CursorRunner, FakeRunner, PiRunner, PreflightHeldRunner, RunRequest, RunResult,
)


def _pi_ok_env(monkeypatch):
    monkeypatch.setattr(BP, "_sdk_on_walk", lambda script, environ=None: True)


def _pi_check_fail_openai(monkeypatch):
    _real = BP.check

    def check(kind, environ=None, *, providers=None):
        if kind == "pi" and providers:
            return f"no credential for provider {providers[0]}"
        return _real(kind, environ=environ, providers=providers)

    monkeypatch.setattr(BP, "check", check)


def test_spec_review_hold_skips_prepare_and_spends_no_retry(tmp_path, monkeypatch):
    from test_agentic_spec_review_loop import begin, reviewed
    _no_key(monkeypatch)
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [reviewed()])
    cfg["agentic"]["runner"] = "cursor"
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "SPEC_REVIEW"
    assert st["counters"].get("spec_review_retries", 0) == 0
    assert PG.read_holds(cfg).get(cid, {}).get("held_by") == "backend_unavailable"
    starts = [e for e in S.read_ledger(cfg, cid, types=["run.start"]) if e.get("persona") == "spec_reviewer"]
    assert not starts
    monkeypatch.setattr("nightwright.agentic.remote_personas.persona_workers",
                        lambda c, s: [{"worker": "w"}])
    Q.tick(cfg)
    assert PG.read_holds(cfg).get(cid, {}).get("held_by") == "backend_unavailable"


def test_do_spec_review_preflight_hold_is_interrupted(tmp_path, monkeypatch):
    _no_key(monkeypatch)
    cfg = _cursor_cfg(tmp_path)
    wt = make_repo(tmp_path)
    rd = tmp_path / "c1" / "runs" / "001-spec_reviewer"
    rd.mkdir(parents=True)
    req = RunRequest(
        persona="spec_reviewer", prompt="p", system_append="", cwd=tmp_path,
        model="m", effort=None, max_budget_usd=1.0, timeout_s=10,
        permission_mode="default", read_only=True, allowed_tools=(), add_dirs=(),
        json_schema={}, env={}, run_dir=rd)
    ctx = SimpleNamespace(cfg=cfg, runner=CursorRunner(), stop=threading.Event())
    st = {"id": "c1", "state": "SPEC_REVIEW", "repo": {"worktree": str(wt)},
          "spec": {"rel_path": "docs/specs/x.md", "id": "SPEC-1"}}
    (wt / "docs/specs").mkdir(parents=True, exist_ok=True)
    (wt / "docs/specs/x.md").write_text("---\nid: SPEC-1\n---\n")
    res = Q._do_spec_review(ctx, st, Q.Step("spec_review", persona="spec_reviewer"), "001-x", req, 1.0)
    assert res.interrupted and res.run_result.cost_usd == 0.0
    counters = {"spec_review_retries": 0}
    row = Q._apply_spec_review(ctx, {**st, "counters": counters}, {}, counters, res, {}, [], lambda *a, **k: {})
    assert counters["spec_review_retries"] == 0
    (rd / "launch.json").write_text("{}")
    assert not isinstance(Q._preflight_hold(ctx, st, req, CursorRunner()), PreflightHeldRunner)
    (rd / "launch.json").unlink()
    (rd / "proc.json").write_text("{}")
    assert not isinstance(Q._preflight_hold(ctx, st, req, CursorRunner()), PreflightHeldRunner)


def test_do_spec_review_converts_startup_fault(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "k-test-value")
    cfg = _cursor_cfg(tmp_path)
    wt = make_repo(tmp_path)

    class Runner:
        detachable = False

        def run(self, req, hooks=None):
            return RunResult(1, False, False, 0.1, True, None,
                             result_text="CURSOR_API_KEY is not set")

    ctx = SimpleNamespace(cfg=cfg, runner=Runner(), stop=threading.Event(),
                          runner_for=lambda st, kind=None: Runner())
    req = SimpleNamespace(run_dir=tmp_path / "r", persona="spec_reviewer", runner=None,
                          pi_provider=None)
    st = {"id": "c1", "state": "SPEC_REVIEW", "repo": {"worktree": str(wt)},
          "spec": {"rel_path": "docs/specs/x.md", "id": "SPEC-1"}}
    (wt / "docs/specs").mkdir(parents=True, exist_ok=True)
    (wt / "docs/specs/x.md").write_text("---\nid: SPEC-1\n---\n")
    res = Q._do_spec_review(ctx, st, Q.Step("spec_review"), "001-x", req, 1.0)
    assert res.interrupted and res.run_result.cost_usd == 0.0 and ctx.stop.is_set() is False
    counters = {"spec_review_retries": 0}
    Q._apply_spec_review(ctx, {**st, "counters": counters}, {}, counters, res, {}, [], lambda *a, **k: {})
    assert counters["spec_review_retries"] == 0

    class PiRunnerStub:
        detachable = False

        def run(self, req, hooks=None):
            return RunResult(1, False, False, 0.1, True, None,
                             result_text="no credential for provider openai")

    ctx.runner = PiRunnerStub()
    ctx.runner_for = lambda st, kind=None: PiRunnerStub()
    res = Q._do_spec_review(ctx, st, Q.Step("spec_review"), "002-x", req, 1.0)
    assert res.interrupted and counters["spec_review_retries"] == 0
    assert "openai" in (BP.env_fault(SimpleNamespace(
        is_error=True, interrupted=False, result_text="no credential for provider openai")) or "")


def test_do_spec_review_mapped_credential_skips_escalation(tmp_path, monkeypatch):
    cfg = {"data_dir": str(tmp_path / "d"),
           "agentic": {"credentials": {"OPENAI_API_KEY": "pass:openai/key"}}}
    wt = make_repo(tmp_path)

    class PiRunnerStub:
        detachable = False

        def run(self, req, hooks=None):
            return RunResult(1, False, False, 0.1, True, None,
                             result_text="no credential for provider openai")

    ctx = SimpleNamespace(cfg=cfg, runner=PiRunnerStub(), stop=threading.Event(),
                          runner_for=lambda st, kind=None: PiRunnerStub())
    req = SimpleNamespace(run_dir=tmp_path / "r", persona="spec_reviewer", runner=None,
                          pi_provider="openai")
    st = {"id": "c1", "state": "SPEC_REVIEW", "repo": {"worktree": str(wt)},
          "spec": {"rel_path": "docs/specs/x.md", "id": "SPEC-1"}}
    (wt / "docs/specs").mkdir(parents=True, exist_ok=True)
    (wt / "docs/specs/x.md").write_text("---\nid: SPEC-1\n---\n")
    BP.reset_noted()
    res = Q._do_spec_review(ctx, st, Q.Step("spec_review"), "001-x", req, 1.0)
    assert res.interrupted
    assert not [r for r in ESC.list_all(cfg, "open") if r.get("kind") == "backend_unavailable"]


def test_pi_real_script_has_no_stub_package_json(tmp_path, monkeypatch):
    stub = (Path(__file__).resolve().parent.parent / "src/nightwright/agentic"
            / "node_modules/@earendil-works/pi-coding-agent/package.json")
    assert not stub.is_file()
    env = {"HOME": str(tmp_path), "NODE_PATH": "", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    assert BP.check("pi", environ=env) == "pi-coding-agent is not installed"


def test_pi_sdk_found_via_node_path(tmp_path, monkeypatch):
    calls = []

    def no_subprocess(*a, **k):
        calls.append(1)
        raise AssertionError("subprocess")

    monkeypatch.setattr("subprocess.run", no_subprocess)
    leaf = tmp_path / "np_leaf"
    leaf.mkdir()
    sdk = tmp_path / "node_modules/@earendil-works/pi-coding-agent/package.json"
    sdk.parent.mkdir(parents=True)
    sdk.write_text("{}")
    env = {"HOME": str(tmp_path), "NODE_PATH": str(leaf),
           "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    monkeypatch.setattr(BP, "_sdk_managed", lambda environ: False)
    assert BP.check("pi", environ=env) is None
    assert not calls


def test_env_fault_preserves_provider_and_path():
    cred = SimpleNamespace(is_error=True, interrupted=False,
                           result_text="no credential for provider openai")
    assert "openai" in (BP.env_fault(cred) or "")
    miss = SimpleNamespace(is_error=True, interrupted=False,
                           result_text="pi_exec.mjs missing at /tmp/pi_exec.mjs")
    assert "/tmp/pi_exec.mjs" in (BP.env_fault(miss) or "")
    assert BP.fault_kind(miss.result_text) == "pi"
    sdk = SimpleNamespace(is_error=True, interrupted=False,
                          result_text="cursor-sdk is not installed")
    assert BP.env_fault(sdk) == "cursor-sdk is not installed"
    assert BP.fault_kind(sdk.result_text) == "cursor"
    node = SimpleNamespace(is_error=True, interrupted=False, result_text="node not found on PATH")
    assert BP.env_fault(node) == "node not found on PATH"
    assert BP.fault_kind(node.result_text) == "pi"
    assert BP.env_fault(SimpleNamespace(is_error=True, interrupted=False,
                                        result_text="unknown model x")) is None


def test_pi_escalation_two_options(tmp_path, monkeypatch):
    BP.reset_noted()
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"}}
    err = "node not found on PATH"
    BP.note_fault(cfg, "pi", err, where="spawn")
    open_ = [r for r in ESC.list_all(cfg, "open") if r.get("kind") == "backend_unavailable"]
    assert len(open_) == 1
    assert err in open_[0]["question"]
    assert len(open_[0]["options"]) == 2
    assert "Node" in open_[0]["options"][0]["label"]
    assert "pi-coding-agent" in open_[0]["options"][0]["label"]
    assert open_[0]["options"][1]["label"].startswith("switch agentic.runner")


def test_pi_check_node_script_sdk_and_credential(tmp_path, monkeypatch):
    calls = []

    def no_subprocess(*a, **k):
        calls.append(1)
        raise AssertionError("subprocess")

    monkeypatch.setattr("subprocess.run", no_subprocess)
    env = {"PATH": "/empty", "HOME": str(tmp_path)}
    real_rb = RP._resolve_binary
    monkeypatch.setattr(RP, "_resolve_binary", lambda name, environ=None: None)
    assert BP.check("pi", environ=env) == "node not found on PATH"
    monkeypatch.setattr(RP, "_resolve_binary", real_rb)
    env["PATH"] = str(tmp_path)
    (tmp_path / "node").write_text("#!/bin/sh\n")
    (tmp_path / "node").chmod(0o755)
    monkeypatch.setattr(BP, "_pi_script", lambda: tmp_path / "missing.mjs")
    assert BP.check("pi", environ=env) == f"pi_exec.mjs missing at {tmp_path / 'missing.mjs'}"
    script = tmp_path / "pi_exec.mjs"
    script.write_text("//")
    monkeypatch.setattr(BP, "_pi_script", lambda: script)
    assert BP.check("pi", environ=env) == "pi-coding-agent is not installed"
    sdk = script.parent / "node_modules/@earendil-works/pi-coding-agent/package.json"
    sdk.parent.mkdir(parents=True)
    sdk.write_text("{}")
    assert BP.check("pi", environ=env) is None
    assert BP.check("pi", environ=env, providers=["openai"]) == "no credential for provider openai"
    env["OPENAI_API_KEY"] = "secret-key-here"
    assert BP.check("pi", environ=env, providers=["openai"]) is None
    env.pop("OPENAI_API_KEY")
    agent = tmp_path / "agent"
    agent.mkdir(parents=True)
    env["PI_CODING_AGENT_DIR"] = str(agent)
    auth = agent / "auth.json"
    auth.write_text(json.dumps({"openai": {"type": "api"}}))
    assert BP.check("pi", environ=env, providers=["openai"]) is None
    env["HOME"] = str(tmp_path)
    adc = tmp_path / ".config/gcloud/application_default_credentials.json"
    adc.parent.mkdir(parents=True)
    adc.write_text("{}")
    assert BP.check("pi", environ=env, providers=["google"]) is None
    # managed install with trailing newline
    rel = agent / "install" / "releases" / "0.50.0" / "node_modules/@earendil-works/pi-coding-agent"
    (rel / "dist").mkdir(parents=True)
    (rel / "dist" / "index.js").write_text("//")
    (agent / "install" / "current-version").write_text("0.50.0\n")
    monkeypatch.setattr(BP, "_sdk_on_walk", lambda script, environ=None: False)
    assert BP.check("pi", environ=env) is None
    env.pop("OPENAI_API_KEY", None)
    auth.write_text("{}")
    assert BP.check("pi", environ=env, providers=["openai"]) == "no credential for provider openai"
    auth.write_text("not-json")
    assert BP.check("pi", environ=env, providers=["openai"]) == "no credential for provider openai"
    env["ANTHROPIC_API_KEY"] = "k"
    assert BP.check("pi", environ=env, providers=["anthropic"]) is None
    env.pop("ANTHROPIC_API_KEY")
    auth.write_text(json.dumps({"anthropic": {"type": "api"}}))
    assert BP.check("pi", environ=env, providers=["anthropic"]) is None
    env["AWS_ACCESS_KEY_ID"] = "k"
    assert BP.check("pi", environ=env, providers=["amazon-bedrock"]) is None
    env.pop("AWS_ACCESS_KEY_ID")
    assert BP.check("pi", environ=env, providers=["bedrock"]) == "no credential for provider bedrock"
    auth.write_text(json.dumps({"bedrock": {"type": "api"}}))
    assert BP.check("pi", environ=env, providers=["bedrock"]) is None
    auth.write_text(json.dumps({"customprov": {"type": "api"}}))
    assert BP.check("pi", environ=env, providers=["customprov"]) is None
    monkeypatch.setattr(BP, "_sdk_on_walk", lambda script, environ=None: True)
    env2 = dict(env)
    env2.pop("PI_CODING_AGENT_DIR", None)
    auth2 = tmp_path / ".pi" / "agent" / "auth.json"
    auth2.parent.mkdir(parents=True)
    auth2.write_text(json.dumps({"openai": {"type": "api"}}))
    assert BP.check("pi", environ=env2, providers=["openai"]) is None
    env2.pop("OPENAI_API_KEY", None)
    auth2.write_text(json.dumps({"openai": {}}))
    assert BP.check("pi", environ=env2, providers=["openai"]) == "no credential for provider openai"
    env2["ANTHROPIC_AUTH_TOKEN"] = "tok"
    assert BP.check("pi", environ=env2, providers=["anthropic"]) is None
    env2.pop("ANTHROPIC_AUTH_TOKEN")
    tok = tmp_path / "anthropic.id"
    tok.write_text("x")
    env2["ANTHROPIC_IDENTITY_TOKEN_FILE"] = str(tok)
    assert BP.check("pi", environ=env2, providers=["anthropic"]) is None
    env2["GOOGLE_API_KEY"] = "g"
    assert BP.check("pi", environ=env2, providers=["google-vertex"]) is None
    env2.pop("GOOGLE_API_KEY")
    env2["GEMINI_API_KEY"] = "g"
    assert BP.check("pi", environ=env2, providers=["google"]) is None
    env2.pop("GEMINI_API_KEY")
    credf = tmp_path / "adc.json"
    credf.write_text("{}")
    env2["GOOGLE_APPLICATION_CREDENTIALS"] = str(credf)
    assert BP.check("pi", environ=env2, providers=["google"]) is None
    web = tmp_path / "web.token"
    web.write_text("t")
    env2["AWS_WEB_IDENTITY_TOKEN_FILE"] = str(web)
    assert BP.check("pi", environ=env2, providers=["amazon-bedrock"]) is None
    no_home = {k: v for k, v in env2.items() if k != "HOME"}
    monkeypatch.setattr(BP, "_home", lambda environ: tmp_path)
    assert BP.check("pi", environ=no_home, providers=["openai"]) == "no credential for provider openai"
    bare_home = tmp_path / "bare_home"
    bare_home.mkdir()
    monkeypatch.setattr(BP, "_home", lambda environ: bare_home)
    bad_tilde = {"PATH": env2["PATH"], "HOME": str(bare_home),
                 "GOOGLE_APPLICATION_CREDENTIALS": "~//etc/passwd"}
    assert BP.check("pi", environ=bad_tilde, providers=["google"]) == "no credential for provider google"
    assert not calls


def test_pi_gate_holds_unless_a_worker_can_take_it(tmp_path, monkeypatch):
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"}}
    _pi_ok_env(monkeypatch)
    _pi_check_fail_openai(monkeypatch)
    monkeypatch.setattr(BP, "spawn_backend", lambda c, s, p: ("pi", ["openai"]))
    st = {"id": "c1", "state": "IMPLEMENTING"}
    g = PG.Gate(cfg, ["c1"])
    step = Q.Step("implement", persona="implementer")
    assert g.admit(st, step) is None
    assert g.holds["c1"]["held_by"] == "backend_unavailable"
    monkeypatch.setattr("nightwright.agentic.remote_personas.persona_workers",
                        lambda c, s: [{"worker": "w"}])
    assert PG.Gate(cfg, ["c1"]).admit(st, step) is step
    assert PG.Gate(cfg, ["c1"]).backend_fault(st, ("implementer",), remote_ok=False) is not None


def test_passing_pi_check_leaves_backend_fault_none(tmp_path, monkeypatch):
    _pi_ok_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "k-test-value")
    monkeypatch.setattr(BP, "spawn_backend", lambda c, s, p: ("pi", ["openai"]))
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi", "workers": {"max_personas": 1}}}
    st = {"id": "c1", "state": "IMPLEMENTING"}
    g = PG.Gate(cfg, ["c1"])
    assert g.backend_fault(st, ("implementer",)) is None
    step = Q.Step("implement", persona="implementer")
    PG.note_usage(cfg, now=1_000.0)
    g = PG.Gate(cfg, ["c1"], now=1_100.0)
    assert g.admit(st, step) is None
    assert g.holds["c1"]["held_by"] == "usage_pause"
    PG._write_json(PG.usage_path(cfg), {})
    PG._write_json(PG.rate_limit_path(cfg), {"until": "2099-01-01T00:00:00+00:00", "backoff_s": 900})
    g = PG.Gate(cfg, ["c1"])
    assert g.admit(st, step) is None
    assert g.holds["c1"]["held_by"] == "rate_limit"
    PG._write_json(PG.rate_limit_path(cfg), {})
    cfg["agentic"]["workers"] = {"max_personas": 1}
    monkeypatch.setattr(PG, "live_persona_runs", lambda *a, **k: 1)
    g = PG.Gate(cfg, ["c1"])
    assert g.admit(st, step) is None
    assert g.holds["c1"]["held_by"] == "persona_cap"


def test_gate_cache_keeps_pi_error_after_worker_escape(tmp_path, monkeypatch):
    _pi_ok_env(monkeypatch)
    real_check = BP.check

    def check(kind, environ=None, *, providers=None):
        if kind == "pi" and providers:
            name = providers[0]
            if name == "openai":
                return f"no credential for provider {name}"
            return None
        return real_check(kind, environ=environ, providers=providers)

    monkeypatch.setattr(BP, "check", check)

    def spawn(c, s, p):
        return ("pi", ["openai"]) if p == "adversarial_reviewer" else ("pi", ["anthropic"])

    monkeypatch.setattr(BP, "spawn_backend", spawn)
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"}}
    st = {"id": "c1", "state": "IMPLEMENTING"}
    BP.reset_noted()
    g = PG.Gate(cfg, ["c1"])
    workers = [{"w": 1}]

    def pw(c, s):
        return workers

    monkeypatch.setattr("nightwright.agentic.remote_personas.persona_workers", pw)
    personas = ("claude_reviewer", "adversarial_reviewer")
    assert g.backend_fault(st, personas, remote_ok=True) is None
    err = g.backend_fault(st, personas, remote_ok=False)
    assert err == "no credential for provider openai"
    assert g._backend[("pi", ("anthropic",))] is None
    assert g._backend[("pi", ("openai",))] == err
    gate_rows = [r for r in journal(cfg, "backend_preflight_failed") if r.get("where") == "gate"]
    assert len(gate_rows) == 1

    def boom(c, s):
        raise RuntimeError("nope")

    monkeypatch.setattr("nightwright.agentic.remote_personas.persona_workers", boom)
    g2 = PG.Gate(cfg, ["c1"])
    g2._backend = dict(g._backend)
    assert g2.backend_fault(st, personas, remote_ok=True) == err


def test_preflight_hold_wraps_pi_and_not_claude(tmp_path, monkeypatch):
    _pi_ok_env(monkeypatch)
    _pi_check_fail_openai(monkeypatch)
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"}}
    rd = tmp_path / "runs" / "001-implementer"
    rd.mkdir(parents=True)
    req = RunRequest(
        persona="implementer", prompt="p", system_append="", cwd=tmp_path,
        model="openai/x", effort="high", max_budget_usd=1.0, timeout_s=10,
        permission_mode="default", read_only=False, allowed_tools=(), add_dirs=(),
        json_schema={}, env={}, run_dir=rd, pi_provider="openai")
    held = Q._preflight_hold(SimpleNamespace(cfg=cfg), {"id": "c1"}, req, PiRunner())
    assert isinstance(held, PreflightHeldRunner)
    rr = held.run(req)
    assert rr.interrupted and rr.cost_usd == 0.0
    cl = Q._preflight_hold(SimpleNamespace(cfg=cfg), {"id": "c1"}, req, ClaudeRunner())
    assert isinstance(cl, ClaudeRunner)
    fake_path = tmp_path / "fake.json"
    fake_path.write_text("[]")
    fk = Q._preflight_hold(SimpleNamespace(cfg=cfg), {"id": "c1"}, req, FakeRunner(fake_path))
    assert isinstance(fk, FakeRunner)


def test_do_persona_converts_pi_startup_fault(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    cfg = {"data_dir": str(tmp_path / "d")}
    failed = RunResult(1, False, False, 0.1, True, None,
                       result_text="pi-coding-agent is not installed")

    class Fake:
        detachable = False

        def run(self, req, hooks=None):
            return failed

    ctx = SimpleNamespace(cfg=cfg, runner=Fake(), stop=threading.Event())
    req = SimpleNamespace(run_dir=tmp_path / "r", persona="implementer", runner=None)
    res = Q._do_persona(ctx, {"id": "c1", "state": "IMPLEMENTING"},
                        Q.Step("implement", persona="implementer"), "001-x", req, 1.0)
    assert res.interrupted and res.run_result.cost_usd == 0.0
    open_ = [r for r in ESC.list_all(cfg, "open") if r.get("kind") == "backend_unavailable"]
    assert len(open_) == 1 and open_[0]["facts"][0] == "backend: pi"


def test_pi_credential_mapped_skips_backend_escalation(tmp_path, monkeypatch):
    cfg = {"data_dir": str(tmp_path / "d"),
           "agentic": {"credentials": {"OPENAI_API_KEY": "pass:openai/key"}}}
    BP.note_fault(cfg, "pi", "no credential for provider openai", where="spawn")
    assert not [r for r in ESC.list_all(cfg, "open") if r.get("kind") == "backend_unavailable"]


def test_startup_check_warns_for_pi(tmp_path, monkeypatch):
    import io
    from nightwright.agentic import runner as RR
    BP.reset_noted()
    _no_key(monkeypatch)
    cfg = _cursor_cfg(tmp_path)
    cfg["agentic"]["runner"] = "pi"
    _pi_ok_env(monkeypatch)
    _pi_check_fail_openai(monkeypatch)
    monkeypatch.setattr(RR, "pi_needed", lambda cfg, campaigns=None: True)
    monkeypatch.setattr(RR, "cursor_needed", lambda cfg: True)
    err = io.StringIO()
    BP.startup_check(cfg, err=err)
    lines = err.getvalue().splitlines()
    assert len(lines) >= 2
    assert all(l.startswith("WARNING:") for l in lines)
    kinds = {r["backend"] for r in journal(cfg, "backend_preflight_failed")}
    assert kinds == {"cursor", "pi"}
    assert all(r.get("where") == "start" for r in journal(cfg, "backend_preflight_failed"))
    open_ = [r for r in ESC.list_all(cfg, "open") if r.get("kind") == "backend_unavailable"]
    facts = {r["facts"][0] for r in open_}
    assert facts == {"backend: cursor", "backend: pi"}


def test_claude_spec_review_is_not_held(tmp_path, monkeypatch):
    from test_agentic_spec_review_loop import begin, reviewed
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [reviewed()])
    cfg["agentic"]["runner"] = "claude"
    Q.tick(cfg)
    assert PG.read_holds(cfg).get(cid, {}).get("held_by") != "backend_unavailable"


def _spec_review_ordinary_failure_retries(tmp_path, monkeypatch, result_text):
    cfg = _cursor_cfg(tmp_path)
    monkeypatch.setenv("CURSOR_API_KEY", "k-test-value")
    wt = make_repo(tmp_path)
    rd = tmp_path / "c1" / "runs" / "001-spec_reviewer"
    rd.mkdir(parents=True)
    req = RunRequest(
        persona="spec_reviewer", prompt="p", system_append="", cwd=tmp_path,
        model="m", effort=None, max_budget_usd=1.0, timeout_s=10,
        permission_mode="default", read_only=True, allowed_tools=(), add_dirs=(),
        json_schema={}, env={}, run_dir=rd)
    st = {"id": "c1", "state": "SPEC_REVIEW", "repo": {"worktree": str(wt)},
          "spec": {"rel_path": "docs/specs/x.md", "id": "SPEC-1"}}
    (wt / "docs/specs").mkdir(parents=True, exist_ok=True)
    (wt / "docs/specs/x.md").write_text("---\nid: SPEC-1\n---\n")

    class Runner:
        detachable = False

        def run(self, req, hooks=None):
            return RunResult(1, False, False, 0.1, True, None, result_text=result_text)

    ctx = SimpleNamespace(cfg=cfg, runner=Runner(), stop=threading.Event(),
                          runner_for=lambda st, kind=None: Runner())
    res = Q._do_spec_review(ctx, st, Q.Step("spec_review", persona="spec_reviewer"), "001-x", req, 1.0)
    assert BP.env_fault(res.run_result) is None
    assert res.interrupted is False and ctx.stop.is_set() is False
    counters = {"spec_review_retries": 0}
    next_state = []

    def finish(state, *a, **k):
        next_state.append(state)

    Q._apply_spec_review(ctx, {**st, "counters": counters}, {}, counters, res, {}, [], finish)
    assert counters["spec_review_retries"] == 1
    assert next_state == ["SPEC_REVIEW"]


def test_request_json_failure_still_retries(tmp_path, monkeypatch):
    _spec_review_ordinary_failure_retries(tmp_path, monkeypatch, "request.json write failed")


def test_unknown_model_still_retries(tmp_path, monkeypatch):
    _spec_review_ordinary_failure_retries(tmp_path, monkeypatch, "unknown model openai/nope")


def test_fault_kind_pi_path():
    assert BP.fault_kind("pi_exec.mjs missing at /work/cursor/pi_exec.mjs") == "pi"


def test_nw_conduct_skill_mentions_pi_backend():
    text = (Path(__file__).resolve().parent.parent / "skills/nw-conduct/SKILL.md").read_text()
    assert "coding-agent package" in text or "Pi backend" in text


def test_spec_review_stays_ungated_for_backoff_and_cap(tmp_path, monkeypatch):
    from fanout_support import _git, make_cfg, make_repo, script
    repo = make_repo(tmp_path)
    v = {"verdict": "approve", "size": "s", "tone": "cost-effective", "rationale": "ok",
         "findings": [], "suggested_splits": []}
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [{"persona": "spec_reviewer", "verdict": v}]))
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    cid = added(cfg, monkeypatch)
    from nightwright.agentic import runner as R
    Q.tick(cfg, runner=R.FakeRunner(cfg["agentic"]["fake_script"]))
    st = S.read_state(cfg, cid)
    wt = Path(st["repo"]["worktree"])
    spec = wt / st["spec"]["rel_path"]
    spec.write_text(spec.read_text().replace("status: accepted", "status: draft"))
    _git(wt, "commit", "-qam", "draft")
    st.update(state="SPEC_REVIEW", kind="idea")
    S.write_state(cfg, cid, st)
    PG._write_json(PG.rate_limit_path(cfg), {"until": "2099-01-01T00:00:00+00:00", "backoff_s": 900})
    cfg.setdefault("agentic", {})["workers"] = {"max_personas": 1}
    RJ.create(cfg, job_id="j-other", campaign="other", worker="box", attempt=1, seq=0,
              submitted_at=0.0, kind="persona", deadline_at=9e9)
    RJ.update(cfg, "j-other", run_id="001-implementer", persona="implementer")
    PG.note_usage(cfg, now=1_000.0)
    monkeypatch.setattr(PG, "_now_epoch", lambda now=None: 1_100.0 if now is None else float(now))
    rep = Q.tick(cfg, runner=R.FakeRunner(cfg["agentic"]["fake_script"]))
    starts = [e for e in S.read_ledger(cfg, cid, types=["run.start"]) if e.get("persona") == "spec_reviewer"]
    assert starts, (rep.steps, PG.read_holds(cfg))


def test_resolution_errors_do_not_abort_or_hold(tmp_path, monkeypatch):
    from test_agentic_spec_review_loop import reviewed
    from test_agentic_spec_writer import wrote
    cfg, repo, iid, cid = wrote(tmp_path, monkeypatch, [reviewed()])
    cfg["agentic"]["model_set"] = "does-not-exist-set"
    Q.tick(cfg)
    assert PG.read_holds(cfg).get(cid, {}).get("held_by") != "backend_unavailable"
    starts = S.read_ledger(cfg, cid, types=["run.start"])
    assert not [e for e in starts if e.get("persona") == "spec_reviewer"]
    st = S.read_state(cfg, cid)
    assert st.get("state") == "PARKED"
    assert st.get("park", {}).get("reason") == "spec_review_exhausted"
    assert "unknown model set" in (st.get("park") or {}).get("detail", "")
    cfg["agentic"].pop("model_set", None)
    st = S.read_state(cfg, cid)
    st["state"] = "SPEC_REVIEW"
    st.pop("park", None)
    S.write_state(cfg, cid, st)
    repo_root = tmp_path / "host_repo"
    repo_root.mkdir()
    (repo_root / ".nw" / "profiles").mkdir(parents=True)
    (repo_root / ".nw" / "profiles" / "badpi.yaml").write_text(
        "schema: nw.profile.v1\nextends: balanced\npersonas:\n  spec_reviewer:\n"
        "    model: nope-model\n    effort: high\n")
    st = S.read_state(cfg, cid)
    st["profile"] = "badpi"
    st.setdefault("repo", {})["root"] = str(repo_root)
    S.write_state(cfg, cid, st)
    cfg["agentic"]["runner"] = "pi"
    monkeypatch.setattr(RP, "_resolve_binary", lambda name, environ=None: None)
    Q.tick(cfg)
    assert PG.read_holds(cfg).get(cid, {}).get("held_by") != "backend_unavailable"


def test_declared_persona_uses_declared_settings_alias(tmp_path, monkeypatch):
    user = tmp_path / "cfg" / "personas"
    root = tmp_path / "proj"
    user.mkdir(parents=True)
    (root / ".nw" / "personas").mkdir(parents=True)
    from test_agentic_persona_defs import _put
    _put(user, "custom_impl", role="implementer", template="{{spec_text}}\n",
         extra="model: haiku\neffort: high\n")
    pi_map = root / ".nw" / "pi_map.yaml"
    pi_map.parent.mkdir(parents=True, exist_ok=True)
    pi_map.write_text("schema: nw.pi_map.v1\naliases:\n  haiku:\n    high: "
                      "{provider: openai, id: gpt-4.1, thinking: high}\n  sonnet:\n    high: "
                      "{provider: anthropic, id: claude, thinking: high}\n")
    _pi_ok_env(monkeypatch)
    _pi_check_fail_openai(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k-test-value")
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"},
           "config_dir": str(tmp_path / "cfg")}
    st = {"id": "c1", "state": "IMPLEMENTING", "project": "Meta-Tools",
          "profile": "balanced", "repo": {"root": str(tmp_path / "repo")}}
    monkeypatch.setattr(PJ, "project_config", lambda c, p: {"_root": root})
    err = PG.Gate(cfg, ["c1"]).backend_fault(st, ("custom_impl",))
    assert err == "no credential for provider openai"


def _proj_pi_hold_fixture(tmp_path, monkeypatch, *, model_set=None, pi_map_provider="openai"):
    from test_agentic_spec_review_loop import reviewed
    from test_agentic_spec_writer import wrote
    cfg, repo, iid, cid = wrote(tmp_path, monkeypatch, [reviewed()])
    repo_root = tmp_path / "host_repo"
    proj_root = tmp_path / "project_main"
    (repo_root / ".nw" / "profiles").mkdir(parents=True)
    (repo_root / ".nw" / "profiles" / "projprof.yaml").write_text(
        "schema: nw.profile.v1\nextends: balanced\npersonas:\n  spec_reviewer:\n"
        "    model: haiku\n    effort: high\n")
    (proj_root / ".nw" / "pi_map.yaml").parent.mkdir(parents=True, exist_ok=True)
    if pi_map_provider == "anthropic":
        haiku_row = "{provider: anthropic, id: claude, thinking: high}\n"
    else:
        haiku_row = "{provider: openai, id: gpt-4.1, thinking: high}\n"
    (proj_root / ".nw" / "pi_map.yaml").write_text(
        "schema: nw.pi_map.v1\naliases:\n  haiku:\n    high: " + haiku_row)
    if model_set:
        (proj_root / ".nw" / "model_sets").mkdir(parents=True, exist_ok=True)
        (proj_root / ".nw" / "model_sets" / f"{model_set}.yaml").write_text(
            "schema: nw.model_set.v1\nrunner: pi\nmodel: openai/gpt-4.1\n")
    monkeypatch.setattr(PJ, "project_config", lambda c, p: {"_root": str(proj_root), "project": p})
    st = S.read_state(cfg, cid)
    st["profile"] = "projprof"
    st.setdefault("repo", {})["root"] = str(repo_root)
    S.write_state(cfg, cid, st)
    cfg["agentic"]["runner"] = "pi"
    if model_set:
        cfg["agentic"]["model_set"] = model_set
    _pi_ok_env(monkeypatch)
    pi_home = tmp_path / "pi_home"
    pi_home.mkdir()
    monkeypatch.setenv("HOME", str(pi_home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home / "agent"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k-test-value")
    return cfg, cid


def test_spec_review_holds_on_project_pi_map(tmp_path, monkeypatch):
    cfg, cid = _proj_pi_hold_fixture(tmp_path, monkeypatch)
    Q.tick(cfg)
    assert PG.read_holds(cfg).get(cid, {}).get("held_by") == "backend_unavailable"
    starts = [e for e in S.read_ledger(cfg, cid, types=["run.start"]) if e.get("persona") == "spec_reviewer"]
    assert not starts


def test_project_model_set_binds_the_pi_provider(tmp_path, monkeypatch):
    cfg, cid = _proj_pi_hold_fixture(
        tmp_path, monkeypatch, model_set="projpi", pi_map_provider="anthropic")
    Q.tick(cfg)
    assert PG.read_holds(cfg).get(cid, {}).get("held_by") == "backend_unavailable"
    starts = [e for e in S.read_ledger(cfg, cid, types=["run.start"]) if e.get("persona") == "spec_reviewer"]
    assert not starts


def test_pi_gate_holds_review_fanout_persona(tmp_path, monkeypatch):
    _pi_ok_env(monkeypatch)
    _pi_check_fail_openai(monkeypatch)
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"}}

    def spawn(c, s, p):
        return ("pi", ["openai"]) if p == "adversarial_reviewer" else ("claude", [])

    monkeypatch.setattr(BP, "spawn_backend", spawn)
    st = {"id": "c1", "state": "IMPLEMENTING", "project": "Meta-Tools"}
    g = PG.Gate(cfg, ["c1"])
    step = Q.Step("review", persona="claude", personas=("claude", "adversarial_reviewer"))
    assert g.admit(st, step) is None
    assert g.holds["c1"]["held_by"] == "backend_unavailable"


def test_spec_review_env_fault_cursor_sdk_and_node(tmp_path, monkeypatch):
    cfg = _cursor_cfg(tmp_path)
    wt = make_repo(tmp_path)
    rd = tmp_path / "c1" / "runs" / "001-spec_reviewer"
    rd.mkdir(parents=True)
    req = RunRequest(
        persona="spec_reviewer", prompt="p", system_append="", cwd=tmp_path,
        model="m", effort=None, max_budget_usd=1.0, timeout_s=10,
        permission_mode="default", read_only=True, allowed_tools=(), add_dirs=(),
        json_schema={}, env={}, run_dir=rd)
    st = {"id": "c1", "state": "SPEC_REVIEW", "repo": {"worktree": str(wt)},
          "spec": {"rel_path": "docs/specs/x.md", "id": "SPEC-1"}}
    (wt / "docs/specs").mkdir(parents=True, exist_ok=True)
    (wt / "docs/specs/x.md").write_text("---\nid: SPEC-1\n---\n")

    for text in ("cursor-sdk is not installed", "node not found on PATH"):
        class Runner:
            detachable = False

            def run(self, req, hooks=None):
                return RunResult(1, False, False, 0.0, True, None, result_text=text)

        ctx = SimpleNamespace(cfg=cfg, runner=Runner(), stop=threading.Event(),
                              runner_for=lambda st, kind=None: Runner())
        res = Q._do_spec_review(ctx, st, Q.Step("spec_review"), "001-x", req, 1.0)
        assert res.interrupted and res.run_result.cost_usd == 0.0
        counters = {"spec_review_retries": 0}
        Q._apply_spec_review(ctx, {**st, "counters": counters}, {}, counters, res, {}, [], lambda *a, **k: {})
        assert counters["spec_review_retries"] == 0


def test_note_fault_path_credential_does_not_suppress_pi_escalation(tmp_path):
    BP.reset_noted()
    cfg = {"data_dir": str(tmp_path / "d"),
           "agentic": {"credentials": {"PATH": "/usr/bin:/bin"}}}
    BP.note_fault(cfg, "pi", "node not found on PATH", where="spawn")
    open_ = [r for r in ESC.list_all(cfg, "open") if r.get("kind") == "backend_unavailable"]
    assert len(open_) == 1


def test_pi_managed_version_bom_and_invalid_utf8(tmp_path, monkeypatch):
    monkeypatch.setattr(BP, "_sdk_on_walk", lambda script, environ=None: False)
    agent = tmp_path / "agent"
    rel = agent / "install" / "releases" / "0.50.0" / "node_modules/@earendil-works/pi-coding-agent"
    (rel / "dist").mkdir(parents=True)
    (rel / "dist" / "index.js").write_text("//")
    ver = agent / "install" / "current-version"
    env = {"HOME": str(tmp_path), "PI_CODING_AGENT_DIR": str(agent),
           "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    ver.write_bytes("\ufeff0.50.0\n".encode("utf-8"))
    assert BP.check("pi", environ=env) is None
    ver.write_bytes(b"\xff\xfe")
    assert BP.check("pi", environ=env) == "pi-coding-agent is not installed"


def test_backend_fault_skips_broken_persona_checks_rest(tmp_path, monkeypatch):
    _pi_ok_env(monkeypatch)
    _pi_check_fail_openai(monkeypatch)

    def spawn(c, s, p):
        if p == "broken_reviewer":
            raise ValueError("unknown model set")
        return ("pi", ["openai"])

    monkeypatch.setattr(BP, "spawn_backend", spawn)
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"}}
    st = {"id": "c1", "state": "IMPLEMENTING"}
    err = PG.Gate(cfg, ["c1"]).backend_fault(st, ("broken_reviewer", "adversarial_reviewer"))
    assert err == "no credential for provider openai"


def test_backend_fault_oserror_returns_none(tmp_path, monkeypatch):
    def boom(cfg, profile, **kw):
        raise OSError("nope")

    monkeypatch.setattr("nightwright.agentic.profiles.resolve_profile", boom)
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "cursor"}}
    st = {"id": "c1", "state": "SPEC_REVIEW", "project": "Meta-Tools", "profile": "balanced"}
    assert PG.Gate(cfg, ["c1"]).backend_fault(st, ("spec_reviewer",)) is None
