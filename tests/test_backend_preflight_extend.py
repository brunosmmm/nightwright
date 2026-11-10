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

pytestmark = pytest.mark.fast


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
    _worker_can_dispatch(monkeypatch)
    Q.tick(cfg)
    st2 = S.read_state(cfg, cid)
    assert st2["state"] == "SPEC_REVIEW"
    assert st2["counters"].get("spec_review_retries", 0) == 0
    assert PG.read_holds(cfg).get(cid, {}).get("held_by") == "backend_unavailable"
    starts = [e for e in S.read_ledger(cfg, cid, types=["run.start"]) if e.get("persona") == "spec_reviewer"]
    assert not starts


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
    row = Q._apply_spec_review(ctx, {**st, "counters": counters}, {}, counters, res, {}, [], lambda *a, **k: ())
    assert counters["spec_review_retries"] == 0
    assert row["to"] == "SPEC_REVIEW" and row["reason"] == "interrupted"
    (rd / "launch.json").write_text("{}")
    assert not isinstance(Q._preflight_hold(ctx, st, req, CursorRunner()), PreflightHeldRunner)
    (rd / "launch.json").unlink()
    (rd / "proc.json").write_text("{}")
    assert not isinstance(Q._preflight_hold(ctx, st, req, CursorRunner()), PreflightHeldRunner)
    _pi_check_fail_openai(monkeypatch)
    rd2 = tmp_path / "c1" / "runs" / "002-spec_reviewer"
    rd2.mkdir(parents=True)
    req2 = RunRequest(
        persona="spec_reviewer", prompt="p", system_append="", cwd=tmp_path,
        model="openai/x", effort="high", max_budget_usd=1.0, timeout_s=10,
        permission_mode="default", read_only=True, allowed_tools=(), add_dirs=(),
        json_schema={}, env={}, run_dir=rd2, pi_provider="openai")
    ctx.runner = PiRunner()
    counters = {"spec_review_retries": 0}
    res = Q._do_spec_review(ctx, st, Q.Step("spec_review", persona="spec_reviewer"), "002-x", req2, 1.0)
    assert res.interrupted and res.run_result.cost_usd == 0.0
    row = Q._apply_spec_review(ctx, {**st, "counters": counters}, {}, counters, res, {}, [], lambda *a, **k: ())
    assert counters["spec_review_retries"] == 0
    assert row["to"] == "SPEC_REVIEW" and row["reason"] == "interrupted"
    (rd2 / "proc.json").write_text("{}")
    assert not isinstance(Q._preflight_hold(ctx, st, req2, PiRunner()), PreflightHeldRunner)


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
    row = Q._apply_spec_review(ctx, {**st, "counters": counters}, {}, counters, res, {}, [], lambda *a, **k: ())
    assert counters["spec_review_retries"] == 0
    assert row["to"] == "SPEC_REVIEW" and row["reason"] == "interrupted"

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
    bare = {k: v for k, v in env2.items()
            if k not in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "HOME")}
    bare["HOME"] = str(tmp_path / "no_adc")
    (tmp_path / "no_adc").mkdir()
    assert BP.check("pi", environ=bare, providers=["google-vertex"]) == (
        "no credential for provider google-vertex")
    env2["GOOGLE_API_KEY"] = "g"
    assert BP.check("pi", environ=env2, providers=["google-vertex"]) is None
    env2.pop("GOOGLE_API_KEY")
    gemini_only = {k: v for k, v in env2.items()
                   if k not in ("GOOGLE_APPLICATION_CREDENTIALS", "GEMINI_API_KEY")}
    gemini_only["HOME"] = str(tmp_path / "gemini_home")
    (tmp_path / "gemini_home").mkdir()
    assert BP.check("pi", environ=gemini_only, providers=["google"]) == (
        "no credential for provider google")
    gemini_only["GEMINI_API_KEY"] = "g"
    assert BP.check("pi", environ=gemini_only, providers=["google"]) is None
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
    passwd = bare_home / "real_secret"
    passwd.write_text("x")
    escape = {"PATH": env2["PATH"], "HOME": str(bare_home),
              "ANTHROPIC_IDENTITY_TOKEN_FILE": "~/../../real_secret"}
    assert BP.check("pi", environ=escape, providers=["anthropic"]) == (
        "no credential for provider anthropic")
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
    monkeypatch.setattr("nightwright.agentic.remote_personas.can_dispatch_step",
                        lambda c, s, stp, **k: True)
    assert PG.Gate(cfg, ["c1"]).admit(st, step) is step
    assert PG.Gate(cfg, ["c1"]).backend_fault(st, ("implementer",), remote_ok=False) is not None
    monkeypatch.setattr("nightwright.agentic.remote_personas.persona_workers",
                        lambda c, s: [{"worker": "w", "name": "w", "enabled": True, "kinds": ["persona"]}])
    monkeypatch.setattr("nightwright.agentic.remote_personas.can_dispatch_step",
                        lambda c, s, stp, **k: False)
    g2 = PG.Gate(cfg, ["c1"])
    assert g2.admit(st, step) is None
    assert g2.holds["c1"]["held_by"] == "backend_unavailable"


def test_implementer_not_remotable_holds_despite_worker(tmp_path, monkeypatch):
    from test_agentic_e2e import _git, added, impl_done, make_cfg, make_repo, review, script
    from test_runner_selfheal import _no_key
    from nightwright.agentic import remote_personas as RPS

    _no_key(monkeypatch)
    repo = make_repo(tmp_path)
    py = repo / ".nw" / "project.yaml"
    py.write_text(py.read_text().replace(
        "gates:\n",
        "gates:\n"
        "  - {name: spec-lint, cmd: 'python3 tools/spec_lint.py --write-ledger >/dev/null"
        " && python3 tools/spec_lint.py', timeout_s: 120, required: true}\n",
        1))
    _git(repo, "add", ".nw/project.yaml")
    _git(repo, "commit", "-q", "-m", "spec-lint gate")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), review()]))
    cfg["agentic"]["runner"] = "cursor"
    monkeypatch.delenv("NW_AGENTIC_RUNNER", raising=False)
    worker = {"name": "box", "enabled": True, "kinds": ["persona"],
              "info": {"features": ["async"]}}
    monkeypatch.setattr(RPS, "persona_workers", lambda c, s: [worker])
    monkeypatch.setattr(RPS, "acquire_slot",
                        lambda c, w: (os.open(os.devnull, os.O_RDONLY), 0))
    monkeypatch.setattr(RPS, "busy", lambda c, w, mine=None: 0)
    monkeypatch.setattr(RPS, "persona_slots", lambda w: 1)
    cid = added(cfg, monkeypatch)
    for _ in range(3):
        Q.tick(cfg)
        if PG.read_holds(cfg).get(cid, {}).get("held_by") == "backend_unavailable":
            break
    assert S.read_state(cfg, cid)["state"] == "IMPLEMENTING"
    assert PG.read_holds(cfg).get(cid, {}).get("held_by") == "backend_unavailable"
    assert not S.read_ledger(cfg, cid, types=["run.start"])


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


def test_admit_pause_and_cap_beat_backend_when_pi_passes(tmp_path, monkeypatch):
    _pi_ok_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "k-test-value")
    _pi_check_fail_openai(monkeypatch)
    monkeypatch.setattr(BP, "spawn_backend", lambda c, s, p: ("pi", ["openai"]))
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi", "workers": {"max_personas": 1}}}
    st = {"id": "c1", "state": "IMPLEMENTING"}
    step = Q.Step("implement", persona="implementer")
    PG.note_usage(cfg, now=1_000.0)
    g = PG.Gate(cfg, ["c1"], now=1_100.0)
    assert g.admit(st, step) is None
    assert g.holds["c1"]["held_by"] == "usage_pause"
    PG._write_json(PG.usage_path(cfg), {})
    PG._write_json(PG.rate_limit_path(cfg), {})
    monkeypatch.setattr(PG, "live_persona_runs", lambda *a, **k: 1)
    g = PG.Gate(cfg, ["c1"])
    assert g.admit(st, step) is None
    assert g.holds["c1"]["held_by"] == "backend_unavailable"


def test_gate_cache_keeps_pi_error_after_worker_escape(tmp_path, monkeypatch):
    from nightwright.agentic import remote_personas as RPS

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
    worker = {"name": "w", "enabled": True, "kinds": ["persona"], "persona_slots": 1}
    monkeypatch.setattr(RPS, "persona_workers", lambda c, s: [worker])
    monkeypatch.setattr(RPS, "ineligible", lambda *a, **k: None)
    monkeypatch.setattr(RPS, "acquire_slot", lambda c, w: (7, 0))
    monkeypatch.setattr(RPS, "busy", lambda *a, **k: 0)
    monkeypatch.setattr(PG, "order", lambda c, s, names: names)
    monkeypatch.setattr(PG, "failing", lambda *a, **k: False)
    from types import SimpleNamespace
    monkeypatch.setattr(RPS, "_probe_request",
                        lambda c, s, p, k: SimpleNamespace(persona=p))
    personas = ("claude_reviewer", "adversarial_reviewer")
    step = Q.Step("review", personas=personas)
    assert g.backend_fault(st, personas, remote_ok=True, step=step) is None
    err = g.backend_fault(st, personas, remote_ok=False)
    assert err == "no credential for provider openai"
    assert g._backend[("pi", ("anthropic",))] is None
    assert g._backend[("pi", ("openai",))] == err
    gate_rows = [r for r in journal(cfg, "backend_preflight_failed") if r.get("where") == "gate"]
    assert len(gate_rows) == 1

    monkeypatch.setattr(RPS, "persona_workers", lambda c, s: (_ for _ in ()).throw(RuntimeError("nope")))
    g2 = PG.Gate(cfg, ["c1"])
    g2._backend = dict(g._backend)
    assert g2.backend_fault(st, personas, remote_ok=True, step=step) == err


def test_pi_check_each_provider_in_list(tmp_path, monkeypatch):
    _pi_ok_env(monkeypatch)
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "ANTHROPIC_API_KEY": "k"}
    assert BP.check("pi", environ=env, providers=["anthropic", "openai"]) == (
        "no credential for provider openai")


def test_preflight_hold_passing_pi_leaves_runner(tmp_path, monkeypatch):
    _pi_ok_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "k-test-value")
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"}}
    rd = tmp_path / "runs" / "001-implementer"
    rd.mkdir(parents=True)
    req = RunRequest(
        persona="implementer", prompt="p", system_append="", cwd=tmp_path,
        model="openai/x", effort="high", max_budget_usd=1.0, timeout_s=10,
        permission_mode="default", read_only=False, allowed_tools=(), add_dirs=(),
        json_schema={}, env={}, run_dir=rd, pi_provider="openai")
    out = Q._preflight_hold(SimpleNamespace(cfg=cfg), {"id": "c1"}, req, PiRunner())
    assert isinstance(out, PiRunner)


def test_gate_holds_persona_less_pi_implement(tmp_path, monkeypatch):
    _pi_ok_env(monkeypatch)
    _pi_check_fail_openai(monkeypatch)
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"}}
    st = {"id": "c1", "state": "IMPLEMENTING"}
    step = Q.Step("implement")
    g = PG.Gate(cfg, ["c1"])
    assert g.admit(st, step) is None
    assert g.holds["c1"]["held_by"] == "backend_unavailable"
    assert g.backend_fault(st, ()) is not None


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
    BP.reset_noted()
    held = Q._preflight_hold(SimpleNamespace(cfg=cfg), {"id": "c1"}, req, PiRunner())
    assert isinstance(held, PreflightHeldRunner)
    rr = held.run(req)
    assert rr.interrupted and rr.cost_usd == 0.0
    open_ = [r for r in ESC.list_all(cfg, "open") if r.get("kind") == "backend_unavailable"]
    assert len(open_) == 1 and open_[0]["facts"][0] == "backend: pi"
    assert [r for r in journal(cfg, "backend_preflight_failed") if r.get("where") == "spawn"]
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


def test_pi_credential_env_map_still_escalates(tmp_path):
    cfg = {"data_dir": str(tmp_path / "d"),
           "agentic": {"credentials": {"OPENAI_API_KEY": "env"}}}
    BP.reset_noted()
    BP.note_fault(cfg, "pi", "no credential for provider openai", where="gate")
    open_ = [r for r in ESC.list_all(cfg, "open") if r.get("kind") == "backend_unavailable"]
    assert len(open_) == 1 and open_[0]["facts"][0] == "backend: pi"


def test_startup_check_pi_empty_providers_still_checks_node(tmp_path, monkeypatch):
    import io
    from nightwright.agentic import runner as RR
    BP.reset_noted()
    cfg = _cursor_cfg(tmp_path)
    cfg["agentic"]["runner"] = "pi"
    monkeypatch.setattr(RR, "pi_needed", lambda cfg, campaigns=None: True)
    monkeypatch.setattr(RR, "cursor_needed", lambda cfg: False)
    monkeypatch.setattr("nightwright.agentic.pi_map.probe_providers", lambda cfg, campaigns=None: [])
    monkeypatch.setattr(RP, "_resolve_binary", lambda name, environ=None: None)
    err = io.StringIO()
    BP.startup_check(cfg, err=err)
    assert "node not found on PATH" in err.getvalue()
    assert [r for r in journal(cfg, "backend_preflight_failed") if r.get("backend") == "pi"]


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
        "    model: haiku\n    effort: high\n")
    proj_root = tmp_path / "proj_badpi"
    (proj_root / ".nw" / "pi_map.yaml").parent.mkdir(parents=True)
    (proj_root / ".nw" / "pi_map.yaml").write_text(
        "schema: nw.pi_map.v1\naliases:\n  sonnet:\n    high: "
        "{provider: anthropic, id: claude, thinking: high}\n")
    monkeypatch.setattr(PJ, "project_config", lambda c, p: {"_root": str(proj_root), "project": p})
    st = S.read_state(cfg, cid)
    st["profile"] = "badpi"
    st.setdefault("repo", {})["root"] = str(repo_root)
    S.write_state(cfg, cid, st)
    cfg["agentic"]["runner"] = "pi"
    monkeypatch.setattr(RP, "_resolve_binary", lambda name, environ=None: None)

    def bad_map(*a, **kw):
        raise ValueError("no Pi mapping for haiku/high")

    monkeypatch.setattr("nightwright.agentic.runner.map_pi_model", bad_map)
    PG._write_json(PG.holds_path(cfg), {})
    Q.tick(cfg)
    assert PG.read_holds(cfg).get(cid, {}).get("held_by") != "backend_unavailable"
    st = S.read_state(cfg, cid)
    assert st.get("park", {}).get("reason") == "spec_review_exhausted"
    assert "no Pi mapping for haiku/high" in (st.get("park") or {}).get("detail", "")


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
    st = S.read_state(cfg, cid)
    fault = PG.Gate(cfg, [cid]).backend_fault(st, ("spec_reviewer",), remote_ok=False)
    assert fault == "no credential for provider openai"
    Q.tick(cfg)
    assert PG.read_holds(cfg).get(cid, {}).get("held_by") == "backend_unavailable"
    starts = [e for e in S.read_ledger(cfg, cid, types=["run.start"]) if e.get("persona") == "spec_reviewer"]
    assert not starts


def test_project_model_set_binds_the_pi_provider(tmp_path, monkeypatch):
    cfg, cid = _proj_pi_hold_fixture(
        tmp_path, monkeypatch, model_set="projpi", pi_map_provider="anthropic")
    st = S.read_state(cfg, cid)
    fault = PG.Gate(cfg, [cid]).backend_fault(st, ("spec_reviewer",), remote_ok=False)
    assert fault == "no credential for provider openai"
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


def test_review_fanout_does_not_keep_unused_slot(tmp_path, monkeypatch):
    from nightwright.agentic import remote_personas as RPS

    _pi_ok_env(monkeypatch)
    _pi_check_fail_openai(monkeypatch)
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"}}
    monkeypatch.setattr(BP, "spawn_backend", lambda c, s, p: ("pi", ["openai"]))
    worker = {"name": "w", "enabled": True, "kinds": ["persona"], "persona_slots": 1}
    acquire_calls = []

    def acquire(c, w):
        acquire_calls.append(1)
        if len(acquire_calls) > 1:
            return None
        return (os.open(os.devnull, os.O_RDONLY), 0)

    monkeypatch.setattr(RPS, "persona_workers", lambda c, s: [worker])
    monkeypatch.setattr(RPS, "ineligible", lambda *a, **k: None)
    monkeypatch.setattr(RPS, "acquire_slot", acquire)
    monkeypatch.setattr(RPS, "busy", lambda *a, **k: 0)
    monkeypatch.setattr(PG, "order", lambda c, s, names: names)
    monkeypatch.setattr(PG, "failing", lambda *a, **k: False)
    monkeypatch.setattr(RPS, "_probe_request",
                        lambda c, s, p, k: SimpleNamespace(persona=p))
    review_st = {"id": "c_review", "state": "REVIEWING", "project": "Meta-Tools", "profile": "balanced"}
    impl_st = {"id": "c_impl", "state": "IMPLEMENTING", "project": "Meta-Tools", "profile": "balanced"}
    g = PG.Gate(cfg, ["c_review", "c_impl"])
    review_step = Q.Step("review", personas=("conformance_reviewer", "adversarial_reviewer"))
    impl_step = Q.Step("implement", persona="implementer")
    assert g.admit(review_st, review_step) is None
    assert g.holds["c_review"]["held_by"] == "backend_unavailable"
    assert not g._slot_reservations
    assert len(acquire_calls) == 2
    acquire_calls.clear()
    assert g.admit(impl_st, impl_step) is impl_step
    assert len(acquire_calls) == 1


def test_review_fanout_checks_every_failing_provider(tmp_path, monkeypatch):
    from nightwright.agentic import remote_personas as RPS

    _pi_ok_env(monkeypatch)
    real_check = BP.check

    def check(kind, environ=None, *, providers=None):
        if kind == "pi" and providers and providers[0] == "openai":
            return "no credential for provider openai"
        if kind == "cursor":
            return "CURSOR_API_KEY is not set"
        return real_check(kind, environ=environ, providers=providers)

    monkeypatch.setattr(BP, "check", check)

    def spawn(c, s, p):
        if p == "conformance_reviewer":
            return ("pi", ["openai"])
        if p == "adversarial_reviewer":
            return ("cursor", [])
        return ("claude", [])

    monkeypatch.setattr(BP, "spawn_backend", spawn)
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"}}
    worker = {"name": "w", "enabled": True, "kinds": ["persona"], "persona_slots": 2}
    monkeypatch.setattr(RPS, "persona_workers", lambda c, s: [worker])
    monkeypatch.setattr(RPS, "ineligible", lambda *a, **k: None)
    monkeypatch.setattr(RPS, "acquire_slot", lambda c, w: (os.open(os.devnull, os.O_RDONLY), 0))
    monkeypatch.setattr(RPS, "busy", lambda *a, **k: 0)
    monkeypatch.setattr(PG, "order", lambda c, s, names: names)
    monkeypatch.setattr(PG, "failing", lambda *a, **k: False)
    monkeypatch.setattr(RPS, "_probe_request",
                        lambda c, s, p, k: SimpleNamespace(persona=p))

    def can_dispatch(c, s, kind, req, *, reservations=None):
        return req.persona == "conformance_reviewer"

    monkeypatch.setattr(RPS, "can_dispatch", can_dispatch)
    st = {"id": "c1", "state": "REVIEWING", "project": "Meta-Tools", "profile": "balanced"}
    step = Q.Step("review", personas=("conformance_reviewer", "adversarial_reviewer"))
    g = PG.Gate(cfg, ["c1"])
    assert g.admit(st, step) is None
    assert g.holds["c1"]["held_by"] == "backend_unavailable"
    assert not g._slot_reservations
    assert g.taken == 0


def test_review_fanout_holds_when_worker_dispatches_only_first_pi(tmp_path, monkeypatch):
    from nightwright.agentic import remote_personas as RPS

    _pi_ok_env(monkeypatch)
    _pi_check_fail_openai(monkeypatch)
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"}}

    def spawn(c, s, p):
        return ("pi", ["openai"]) if p == "adversarial_reviewer" else ("claude", [])

    monkeypatch.setattr(BP, "spawn_backend", spawn)
    st = {"id": "c1", "state": "REVIEWING", "project": "Meta-Tools", "profile": "balanced"}
    S.write_state(cfg, "c1", dict(st))
    worker = {"name": "w", "enabled": True, "kinds": ["persona"], "persona_slots": 1}
    calls = []

    def can_dispatch(c, s, kind, req, *, reservations=None):
        calls.append(req.persona)
        return req.persona == "claude"

    monkeypatch.setattr(RPS, "persona_workers", lambda c, s: [worker])
    monkeypatch.setattr(RPS, "ineligible", lambda *a, **k: None)
    monkeypatch.setattr(RPS, "_probe_request",
                        lambda c, s, p, k: SimpleNamespace(persona=p))
    monkeypatch.setattr(RPS, "can_dispatch", can_dispatch)
    step = Q.Step("review", personas=("claude", "adversarial_reviewer"))
    g = PG.Gate(cfg, ["c1"])
    assert g.admit(st, step) is None
    assert g.holds["c1"]["held_by"] == "backend_unavailable"
    assert "adversarial_reviewer" in calls


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
        row = Q._apply_spec_review(ctx, {**st, "counters": counters}, {}, counters, res, {}, [], lambda *a, **k: ())
        assert counters["spec_review_retries"] == 0
        assert row["to"] == "SPEC_REVIEW" and row["reason"] == "interrupted"


def test_spec_review_git_error_after_env_fault_stays_on_review(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "k-test-value")
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

    class Runner:
        detachable = False

        def run(self, req, hooks=None):
            return RunResult(1, False, False, 0.0, True, None,
                             result_text="CURSOR_API_KEY is not set")

    from nightwright.agentic import gitops as G
    real_rp = G.rev_parse
    calls = [0]

    def flaky_rev_parse(worktree):
        calls[0] += 1
        if calls[0] > 1:
            raise G.GitError(["rev-parse"], 1, "rev-parse failed")
        return real_rp(worktree)

    monkeypatch.setattr(G, "rev_parse", flaky_rev_parse)
    ctx = SimpleNamespace(cfg=cfg, runner=Runner(), stop=threading.Event(),
                          runner_for=lambda st, kind=None: Runner())
    (wt / "tracked-edit.txt").write_text("dirty")
    G.git(wt, "add", "tracked-edit.txt")
    res = Q._do_spec_review(ctx, st, Q.Step("spec_review"), "001-x", req, 1.0)
    assert res.interrupted and res.error is None
    assert calls[0] >= 2
    assert not (wt / "tracked-edit.txt").exists()
    counters = {"spec_review_retries": 0}
    row = Q._apply_spec_review(ctx, {**st, "counters": counters}, {}, counters, res, {}, [], lambda *a, **k: ())
    assert counters["spec_review_retries"] == 0 and row["to"] == "SPEC_REVIEW"


def test_do_spec_review_unknown_startup_fault_escalates(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "k-test-value")
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
    BP.reset_noted()

    class Runner:
        detachable = False

        def run(self, req, hooks=None):
            return RunResult(1, False, False, 0.0, True, None,
                             result_text="pi_exec.mjs missing at /nope/pi_exec.mjs")

    ctx = SimpleNamespace(cfg=cfg, runner=Runner(), stop=threading.Event(),
                          runner_for=lambda st, kind=None: Runner())
    res = Q._do_spec_review(ctx, st, Q.Step("spec_review"), "001-x", req, 1.0)
    assert res.interrupted
    run_rows = [r for r in journal(cfg, "backend_preflight_failed") if r.get("where") == "run"]
    assert len(run_rows) == 1


def test_spec_review_precheck_before_backend_hold(tmp_path, monkeypatch):
    from test_agentic_spec_review_loop import begin, reviewed
    from fanout_support import _git
    _no_key(monkeypatch)
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [reviewed()])
    cfg["agentic"]["runner"] = "cursor"
    st = S.read_state(cfg, cid)
    wt = Path(st["repo"]["worktree"])
    (wt / "dirty-tracked.txt").write_text("x")
    _git(wt, "add", "dirty-tracked.txt")
    Q.tick(cfg)
    assert PG.read_holds(cfg).get(cid, {}).get("held_by") != "backend_unavailable"
    assert S.read_state(cfg, cid).get("state") == "PARKED"
    assert S.read_state(cfg, cid).get("park", {}).get("reason") == "spec_review_exhausted"


def test_check_pi_swallows_auth_recursion(tmp_path, monkeypatch):
    monkeypatch.setattr(BP, "_sdk_on_walk", lambda script, environ=None: True)
    agent = tmp_path / "agent"
    agent.mkdir(parents=True)
    env = {"HOME": str(tmp_path), "PI_CODING_AGENT_DIR": str(agent),
           "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    nested = b"[" * 5000 + b"]" * 5000
    (agent / "auth.json").write_bytes(nested)
    assert BP.check("pi", environ=env, providers=["openai"]) == "no credential for provider openai"


def test_note_fault_path_credential_does_not_suppress_pi_escalation(tmp_path):
    BP.reset_noted()
    cfg = {"data_dir": str(tmp_path / "d"),
           "agentic": {"credentials": {"PATH": "/usr/bin:/bin"}}}
    BP.note_fault(cfg, "pi", "node not found on PATH", where="spawn")
    open_ = [r for r in ESC.list_all(cfg, "open") if r.get("kind") == "backend_unavailable"]
    assert len(open_) == 1


def test_note_fault_mapped_openai_does_not_suppress_node_escalation(tmp_path):
    BP.reset_noted()
    cfg = {"data_dir": str(tmp_path / "d"),
           "agentic": {"credentials": {"OPENAI_API_KEY": "pass:openai/key"}}}
    BP.note_fault(cfg, "pi", "node not found on PATH", where="spawn")
    open_ = [r for r in ESC.list_all(cfg, "open") if r.get("kind") == "backend_unavailable"]
    assert len(open_) == 1
    BP.note_fault(cfg, "pi", "pi-coding-agent is not installed", where="start")
    assert len([r for r in ESC.list_all(cfg, "open") if r.get("kind") == "backend_unavailable"]) == 1


def test_spec_review_runner_raises_during_stop_parks_exhausted(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "k-test-value")
    cfg = _cursor_cfg(tmp_path)
    wt = make_repo(tmp_path)
    rd = tmp_path / "c1" / "runs" / "001-spec_reviewer"
    rd.mkdir(parents=True)
    req = RunRequest(
        persona="spec_reviewer", prompt="p", system_append="", cwd=tmp_path,
        model="m", effort=None, max_budget_usd=1.0, timeout_s=10,
        permission_mode="default", read_only=True, allowed_tools=(), add_dirs=(),
        json_schema={}, env={}, run_dir=rd)
    st = {"id": "c1", "state": "SPEC_REVIEW", "repo": {"worktree": str(wt), "branch": "main"},
          "spec": {"rel_path": "docs/specs/x.md", "id": "SPEC-1"}, "counters": {},
          "project": "Meta-Tools", "profile": "balanced"}
    (wt / "docs/specs").mkdir(parents=True, exist_ok=True)
    (wt / "docs/specs/x.md").write_text("---\nid: SPEC-1\n---\n")
    S.write_state(cfg, "c1", dict(st))

    class Runner:
        detachable = False

        def run(self, req, hooks=None):
            raise RuntimeError("boom")

    stop = threading.Event()
    stop.set()
    ctx = SimpleNamespace(cfg=cfg, runner=Runner(), stop=stop,
                          runner_for=lambda s, kind=None: Runner(),
                          profile=lambda s: {})
    res = Q._do_spec_review(ctx, st, Q.Step("spec_review", persona="spec_reviewer"), "001-x", req, 1.0)
    assert res.error and "boom" in res.error
    assert res.interrupted and res.run_result is None
    row = Q._apply(ctx, S.read_state(cfg, "c1"), res)
    st2 = S.read_state(cfg, "c1")
    assert st2.get("state") == "PARKED"
    assert st2.get("park", {}).get("reason") == "spec_review_exhausted"
    assert "boom" in st2.get("park", {}).get("detail", "")
    assert st2.get("counters", {}).get("spec_review_retries", 0) == 0
    assert row.get("reason") == "spec_review_exhausted"


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
    ver.write_bytes("\ufeff\ufeff0.50.0\n".encode("utf-8"))
    assert BP.check("pi", environ=env) is None
    ver.write_bytes("0.50.0\ufeff\n".encode("utf-8"))
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


def test_review_fanout_spawn_skip_at_cap_one_does_not_block_impl(tmp_path, monkeypatch):
    _pi_ok_env(monkeypatch)
    _pi_check_fail_openai(monkeypatch)

    def spawn(c, s, p):
        if p == "conformance_reviewer":
            raise ValueError("unknown model set")
        if p == "adversarial_reviewer":
            return ("pi", ["openai"])
        return ("claude", [])

    monkeypatch.setattr(BP, "spawn_backend", spawn)
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi", "workers": {"max_personas": 1}}}
    review_st = {"id": "c_review", "state": "REVIEWING", "project": "Meta-Tools", "profile": "balanced"}
    impl_st = {"id": "c_impl", "state": "IMPLEMENTING", "project": "Meta-Tools", "profile": "balanced"}
    g = PG.Gate(cfg, ["c_review", "c_impl"])
    review_step = Q.Step("review", personas=("conformance_reviewer", "adversarial_reviewer"))
    impl_step = Q.Step("implement", persona="implementer")
    assert g.admit(review_st, review_step) is None
    assert g.holds["c_review"]["held_by"] == "backend_unavailable"
    assert g.taken == 0
    assert g.admit(impl_st, impl_step) is impl_step
    assert g.holds.get("c_impl", {}).get("held_by") != "persona_cap"


def test_review_fanout_spawn_skip_prepare_hold_frees_persona_cap(tmp_path, monkeypatch):
    from nightwright.agentic import remote_personas as RPS

    _pi_ok_env(monkeypatch)
    _pi_check_fail_openai(monkeypatch)

    def spawn(c, s, p):
        if p == "conformance_reviewer":
            raise ValueError("unknown model set")
        return ("pi", ["openai"])

    monkeypatch.setattr(BP, "spawn_backend", spawn)
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi", "workers": {"max_personas": 2}}}
    worker = {"name": "w", "enabled": True, "kinds": ["persona"], "persona_slots": 2}
    monkeypatch.setattr(RPS, "persona_workers", lambda c, s: [worker])
    monkeypatch.setattr(RPS, "ineligible", lambda *a, **k: None)
    monkeypatch.setattr(RPS, "acquire_slot", lambda c, w: (os.open(os.devnull, os.O_RDONLY), 0))
    monkeypatch.setattr(RPS, "busy", lambda *a, **k: 0)
    monkeypatch.setattr(PG, "order", lambda c, s, names: names)
    monkeypatch.setattr(PG, "failing", lambda *a, **k: False)
    monkeypatch.setattr(RPS, "_probe_request",
                        lambda c, s, p, k: SimpleNamespace(persona=p))
    monkeypatch.setattr(RPS, "can_dispatch", lambda *a, **k: True)
    review_st = {"id": "c_review", "state": "REVIEWING", "project": "Meta-Tools", "profile": "balanced"}
    impl_st = {"id": "c_impl", "state": "IMPLEMENTING", "project": "Meta-Tools", "profile": "balanced"}
    g = PG.Gate(cfg, ["c_review", "c_impl"])
    review_step = Q.Step("review", personas=("conformance_reviewer", "adversarial_reviewer"))
    impl_step = Q.Step("implement", persona="implementer")
    admitted = g.admit(review_st, review_step)
    assert admitted is review_step
    assert g.taken == 2
    profile = {"reviewers": ["conformance_reviewer", "adversarial_reviewer"]}
    ctx = SimpleNamespace(cfg=cfg, stop=threading.Event(), profile=lambda s: profile,
                          project=lambda s: {"_root": str(tmp_path)})
    report = Q.TickReport()
    assert Q._prepare_reviews(ctx, g, review_st, review_step, profile, [], report) is None
    assert g.holds["c_review"]["held_by"] == "backend_unavailable"
    assert g.taken == 0
    assert g.admit(impl_st, impl_step) is impl_step
    assert g.holds.get("c_impl", {}).get("held_by") != "persona_cap"


def test_backend_fault_oserror_returns_none(tmp_path, monkeypatch):
    def boom(cfg, profile, **kw):
        raise OSError("nope")

    monkeypatch.setattr("nightwright.agentic.profiles.resolve_profile", boom)
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "cursor"}}
    st = {"id": "c1", "state": "SPEC_REVIEW", "project": "Meta-Tools", "profile": "balanced"}
    assert PG.Gate(cfg, ["c1"]).backend_fault(st, ("spec_reviewer",)) is None


def test_writeback_rerun_pi_preflight_interrupts(tmp_path, monkeypatch):
    from nightwright.agentic import remote_personas as RPS

    monkeypatch.setattr(BP, "check", lambda kind, environ=None, *, providers=None: (
        "node not found on PATH" if kind == "pi" else None))
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"}}
    (tmp_path / "d" / "campaigns").mkdir(parents=True)
    cid = "c1"
    rd = S.runs_dir(cfg, cid) / "001-implementer"
    rd.mkdir(parents=True)
    st = {"id": cid, "state": "IMPLEMENTING", "counters": {"runs": 1}, "cost": {},
          "repo": {"worktree": str(tmp_path)}}
    S.write_state(cfg, cid, st)
    S.append_event(cfg, cid, "run.start", {
        "persona": "implementer", "run_id": "001-implementer", "model": "openai/x",
        "effort": "high", "max_budget_usd": 1.0, "timeout_s": 10,
        "permission_mode": "default", "prompt_sha256": "0" * 64,
        "state": "IMPLEMENTING", "step": {"kind": "implement", "personas": []},
        "profile": "balanced"})
    req = RunRequest(
        persona="implementer", prompt="p", system_append="", cwd=str(tmp_path),
        model="openai/x", effort="high", max_budget_usd=1.0, timeout_s=10,
        permission_mode="default", read_only=False, allowed_tools=(), add_dirs=(),
        json_schema={}, env={}, run_dir=rd, pi_provider="openai", runner="pi")
    failed = RunResult(1, False, False, 0.1, True, None, result_text="write-back: missing",
                       writeback_failed=True)

    class Remote:
        detachable = True

        def run(self, req, hooks=None):
            return failed

    monkeypatch.setattr(RPS, "dispatch", lambda *a, **k: Remote())
    ctx = SimpleNamespace(cfg=cfg, runner=PiRunner(), stop=threading.Event(),
                          runner_for=lambda s, kind=None: PiRunner(),
                          profile=lambda s: {}, project=lambda s: {"_root": str(tmp_path)})
    res = Q._do_persona(ctx, st, Q.Step("implement", persona="implementer"),
                        "001-implementer", req, 1.0)
    assert res.interrupted and res.run_result.interrupted and res.run_result.cost_usd == 0.0
    row = Q._apply_step(ctx, S.read_state(cfg, cid), res)
    assert row["to"] == "IMPLEMENTING"


def test_one_persona_slot_two_pi_campaigns_one_run_start(tmp_path, monkeypatch):
    from nightwright.agentic import remote_personas as RPS
    from nightwright.agentic.runner import RemoteRunner

    _pi_ok_env(monkeypatch)
    _pi_check_fail_openai(monkeypatch)
    monkeypatch.setattr(BP, "spawn_backend", lambda c, s, p: ("pi", ["openai"]))
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"}}
    worker = {"name": "w", "enabled": True, "kinds": ["persona"], "persona_slots": 1}
    acquire_calls = []
    slot_fd = os.open(os.devnull, os.O_RDONLY)

    def acquire(cfg, w):
        acquire_calls.append(1)
        if len(acquire_calls) > 1:
            return None
        return (slot_fd, 0)

    monkeypatch.setattr(RPS, "persona_workers", lambda c, s: [worker])
    monkeypatch.setattr(RPS, "ineligible", lambda *a, **k: None)
    monkeypatch.setattr(RPS, "acquire_slot", acquire)
    monkeypatch.setattr(RPS, "busy", lambda *a, **k: 0)
    monkeypatch.setattr(PG, "order", lambda c, s, names: names)
    monkeypatch.setattr(PG, "failing", lambda *a, **k: False)
    monkeypatch.setattr(RPS, "_probe_request",
                        lambda c, s, p, k: SimpleNamespace(persona=p))
    step = Q.Step("implement", persona="implementer")
    g = PG.Gate(cfg, ["c1", "c2"])
    st1, st2 = {"id": "c1", "state": "IMPLEMENTING"}, {"id": "c2", "state": "IMPLEMENTING"}
    assert g.admit(st1, step) is step
    key = ("c1", "implementer")
    assert key in g._slot_reservations
    assert g.admit(st2, step) is None
    assert g.holds["c2"]["held_by"] == "backend_unavailable"
    acquire_calls.clear()
    (tmp_path / "d" / "campaigns").mkdir(parents=True, exist_ok=True)
    rd = tmp_path / "d" / "campaigns" / "c1" / "runs" / "001-implementer"
    rd.mkdir(parents=True)
    req = RunRequest(
        persona="implementer", prompt="p", system_append="", cwd=str(tmp_path),
        model="openai/x", effort="high", max_budget_usd=1.0, timeout_s=10,
        permission_mode="default", read_only=False, allowed_tools=(), add_dirs=(),
        json_schema={}, env={}, run_dir=rd, pi_provider="openai")
    remote = RPS.dispatch(cfg, st1, "implement", req, reservations=g._slot_reservations)
    assert isinstance(remote, RemoteRunner)
    assert not acquire_calls
    assert key not in g._slot_reservations
    g.release_slot_reservations()


def test_dispatch_probe_without_reservation_writes_no_run_start(tmp_path, monkeypatch):
    from nightwright.agentic import remote_personas as RPS

    _pi_ok_env(monkeypatch)
    _pi_check_fail_openai(monkeypatch)
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"}}
    (tmp_path / "d" / "campaigns").mkdir(parents=True)
    cid = "c1"
    st = {"id": cid, "state": "IMPLEMENTING", "project": "Meta-Tools", "profile": "balanced"}
    S.write_state(cfg, cid, dict(st))
    worker = {"name": "w", "enabled": True, "kinds": ["persona"], "persona_slots": 1}
    monkeypatch.setattr(RPS, "persona_workers", lambda c, s: [worker])
    monkeypatch.setattr(RPS, "ineligible", lambda *a, **k: None)
    monkeypatch.setattr(RPS, "acquire_slot", lambda c, w: (os.open(os.devnull, os.O_RDONLY), 0))
    monkeypatch.setattr(RPS, "busy", lambda *a, **k: 0)
    monkeypatch.setattr(PG, "order", lambda c, s, names: names)
    monkeypatch.setattr(PG, "failing", lambda *a, **k: False)
    monkeypatch.setattr(RPS, "_probe_request",
                        lambda c, s, p, k: SimpleNamespace(persona=p))
    g = PG.Gate(cfg, [cid])
    step = Q.Step("implement", persona="implementer")
    assert g.admit(st, step) is step
    g._slot_reservations.clear()
    ctx = SimpleNamespace(cfg=cfg, stop=threading.Event(), profile=lambda s: {},
                          project=lambda s: {"_root": str(tmp_path)}, _slot_reservations=g._slot_reservations)
    from unittest.mock import patch
    with patch.object(Q, "_prepare_run", side_effect=AssertionError("run.start must not run")):
        pgate = g
        held = Q._hold_local_backend_before_prepare(pgate, st, "implementer", step)
        assert held
    assert not S.read_ledger(cfg, cid, types=["run.start"])


def test_rate_limit_beats_failing_pi_backend(tmp_path, monkeypatch):
    _pi_ok_env(monkeypatch)
    _pi_check_fail_openai(monkeypatch)
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"}}
    PG._write_json(PG.rate_limit_path(cfg),
                    {"until": "2099-01-01T00:00:00+00:00", "backoff_s": 900})
    st = {"id": "c1", "state": "IMPLEMENTING"}
    step = Q.Step("implement", persona="implementer")
    g = PG.Gate(cfg, ["c1"], now=1_700_000_000.0)
    assert g.admit(st, step) is None
    assert g.holds["c1"]["held_by"] == "rate_limit"


def test_spec_review_git_error_after_stop_parks(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "k-test-value")
    cfg = _cursor_cfg(tmp_path)
    wt = make_repo(tmp_path)
    rd = tmp_path / "c1" / "runs" / "001-spec_reviewer"
    rd.mkdir(parents=True)
    req = RunRequest(
        persona="spec_reviewer", prompt="p", system_append="", cwd=tmp_path,
        model="m", effort=None, max_budget_usd=1.0, timeout_s=10,
        permission_mode="default", read_only=True, allowed_tools=(), add_dirs=(),
        json_schema={}, env={}, run_dir=rd)
    st = {"id": "c1", "state": "SPEC_REVIEW", "repo": {"worktree": str(wt), "branch": "main"},
          "spec": {"rel_path": "docs/specs/x.md", "id": "SPEC-1"}, "counters": {},
          "project": "Meta-Tools", "profile": "balanced"}
    (wt / "docs/specs").mkdir(parents=True, exist_ok=True)
    (wt / "docs/specs/x.md").write_text("---\nid: SPEC-1\n---\n")
    S.write_state(cfg, "c1", dict(st))

    class Runner:
        detachable = False

        def run(self, req, hooks=None):
            return RunResult(1, False, True, 0.1, True, None, result_text="spec is unclear")

    from nightwright.agentic import gitops as G
    real_rp = G.rev_parse
    calls = [0]

    def flaky_rev_parse(worktree):
        calls[0] += 1
        if calls[0] > 1:
            raise G.GitError(["rev-parse"], 1, "rev-parse failed")
        return real_rp(worktree)

    monkeypatch.setattr(G, "rev_parse", flaky_rev_parse)
    stop = threading.Event()
    stop.set()
    ctx = SimpleNamespace(cfg=cfg, runner=Runner(), stop=stop,
                          runner_for=lambda s, kind=None: Runner(),
                          profile=lambda s: {})
    res = Q._do_spec_review(ctx, st, Q.Step("spec_review"), "001-x", req, 1.0)
    assert res.error and "rev-parse" in res.error
    row = Q._apply(ctx, S.read_state(cfg, "c1"), res)
    st2 = S.read_state(cfg, "c1")
    assert st2.get("state") == "PARKED"
    assert st2.get("park", {}).get("reason") == "spec_review_exhausted"
    assert row.get("reason") == "spec_review_exhausted"


def test_spec_review_git_error_after_preflight_hold_stays_on_review(tmp_path, monkeypatch):
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
    st = {"id": "c1", "state": "SPEC_REVIEW", "repo": {"worktree": str(wt)},
          "spec": {"rel_path": "docs/specs/x.md", "id": "SPEC-1"}}
    (wt / "docs/specs").mkdir(parents=True, exist_ok=True)
    (wt / "docs/specs/x.md").write_text("---\nid: SPEC-1\n---\n")

    from nightwright.agentic import gitops as G
    real_rp = G.rev_parse
    calls = [0]

    def flaky_rev_parse(worktree):
        calls[0] += 1
        if calls[0] > 1:
            raise G.GitError(["rev-parse"], 1, "rev-parse failed")
        return real_rp(worktree)

    monkeypatch.setattr(G, "rev_parse", flaky_rev_parse)
    (wt / "tracked-edit.txt").write_text("dirty")
    G.git(wt, "add", "tracked-edit.txt")
    ctx = SimpleNamespace(cfg=cfg, runner=CursorRunner(), stop=threading.Event())
    res = Q._do_spec_review(ctx, st, Q.Step("spec_review"), "001-x", req, 1.0)
    assert res.interrupted and res.error is None
    assert calls[0] >= 2
    assert not (wt / "tracked-edit.txt").exists()
    counters = {"spec_review_retries": 0}
    row = Q._apply_spec_review(ctx, {**st, "counters": counters}, {}, counters, res, {}, [], lambda *a, **k: ())
    assert counters["spec_review_retries"] == 0 and row["to"] == "SPEC_REVIEW"


def test_pi_slot_handoff_through_tick(tmp_path, monkeypatch):
    from test_agentic_e2e import added, impl_done, make_cfg, make_repo, review, script
    from nightwright.agentic import remote_personas as RPS
    from nightwright.agentic.runner import RunResult

    _pi_ok_env(monkeypatch)
    _pi_check_fail_openai(monkeypatch)
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), review()]))
    cfg["agentic"]["runner"] = "pi"
    cfg["agentic"].pop("fake_script", None)
    worker = {"name": "w", "enabled": True, "kinds": ["persona"], "persona_slots": 1,
              "ssh": "-", "info": {"features": ["async"]}}
    acquire_calls = []
    slot_fd = os.open(os.devnull, os.O_RDONLY)
    release_seen = []

    def acquire(c, w):
        acquire_calls.append(1)
        return (slot_fd, 0) if len(acquire_calls) == 1 else None

    class Remote:
        detachable = True

        def run(self, req, hooks=None):
            return RunResult(0, False, True, 0.5, False, {"type": "result", "subtype": "success"},
                             result_text="ok")

    dispatch_acquires = []

    def dispatch(cfg, state, step_kind, req, **kw):
        reservations = kw.get("reservations") or {}
        key = (state["id"], req.persona)
        assert reservations.get(key) is not None
        dispatch_acquires.append(len(acquire_calls))
        return Remote()

    def release(reservations):
        release_seen.append(dict(reservations or {}))
        RPS.release_reservations(reservations)

    monkeypatch.setattr(RPS, "persona_workers", lambda c, s: [worker])
    monkeypatch.setattr(RPS, "ineligible", lambda *a, **k: None)
    monkeypatch.setattr(RPS, "acquire_slot", acquire)
    monkeypatch.setattr(RPS, "busy", lambda *a, **k: 0)
    monkeypatch.setattr(PG, "order", lambda c, s, names: names)
    monkeypatch.setattr(PG, "failing", lambda *a, **k: False)
    monkeypatch.setattr(RPS, "dispatch", dispatch)
    monkeypatch.setattr(PG.Gate, "release_slot_reservations",
                        lambda self: release(self._slot_reservations))
    cid = added(cfg, monkeypatch)
    starts = []
    for _ in range(12):
        Q.tick(cfg)
        starts = S.read_ledger(cfg, cid, types=["run.start"])
        if starts:
            break
    hold = PG.read_holds(cfg).get(cid, {}).get("held_by")
    assert len(starts) == 1, f"held_by={hold!r} state={S.read_state(cfg, cid).get('state')}"
    assert len(acquire_calls) == 1
    assert dispatch_acquires == [1]
    assert release_seen and not release_seen[-1]


def test_spec_review_git_error_on_ordinary_failure_parks(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "k-test-value")
    cfg = _cursor_cfg(tmp_path)
    wt = make_repo(tmp_path)
    rd = tmp_path / "c1" / "runs" / "001-spec_reviewer"
    rd.mkdir(parents=True)
    req = RunRequest(
        persona="spec_reviewer", prompt="p", system_append="", cwd=tmp_path,
        model="m", effort=None, max_budget_usd=1.0, timeout_s=10,
        permission_mode="default", read_only=True, allowed_tools=(), add_dirs=(),
        json_schema={}, env={}, run_dir=rd)
    st = {"id": "c1", "state": "SPEC_REVIEW", "repo": {"worktree": str(wt), "branch": "main"},
          "spec": {"rel_path": "docs/specs/x.md", "id": "SPEC-1"}, "counters": {},
          "project": "Meta-Tools", "profile": "balanced"}
    (wt / "docs/specs").mkdir(parents=True, exist_ok=True)
    (wt / "docs/specs/x.md").write_text("---\nid: SPEC-1\n---\n")
    S.write_state(cfg, "c1", dict(st))

    class Runner:
        detachable = False

        def run(self, req, hooks=None):
            return RunResult(1, False, False, 0.1, True, None, result_text="spec is unclear")

    from nightwright.agentic import gitops as G
    real_rp = G.rev_parse
    calls = [0]

    def flaky_rev_parse(worktree):
        calls[0] += 1
        if calls[0] > 1:
            raise G.GitError(["rev-parse"], 1, "rev-parse failed")
        return real_rp(worktree)

    monkeypatch.setattr(G, "rev_parse", flaky_rev_parse)
    ctx = SimpleNamespace(cfg=cfg, runner=Runner(), stop=threading.Event(),
                          runner_for=lambda s, kind=None: Runner(),
                          profile=lambda s: {})
    res = Q._do_spec_review(ctx, st, Q.Step("spec_review"), "001-x", req, 1.0)
    assert res.error and "rev-parse" in res.error
    row = Q._apply(ctx, S.read_state(cfg, "c1"), res)
    st2 = S.read_state(cfg, "c1")
    assert st2.get("state") == "PARKED"
    assert st2.get("park", {}).get("reason") == "spec_review_exhausted"


def _worker_can_dispatch(monkeypatch):
    from nightwright.agentic import remote_personas as RPS

    worker = {"name": "w", "enabled": True, "kinds": ["persona"], "persona_slots": 1}
    monkeypatch.setattr(RPS, "persona_workers", lambda c, s: [worker])
    monkeypatch.setattr(RPS, "ineligible", lambda *a, **k: None)
    monkeypatch.setattr(RPS, "acquire_slot", lambda c, w: (os.open(os.devnull, os.O_RDONLY), 0))
    monkeypatch.setattr(RPS, "busy", lambda *a, **k: 0)
    monkeypatch.setattr(PG, "order", lambda c, s, names: names)
    monkeypatch.setattr(PG, "failing", lambda *a, **k: False)
    monkeypatch.setattr(RPS, "_probe_request",
                        lambda c, s, p, k: SimpleNamespace(persona=p, run_dir=Path("/tmp/x")))


def test_write_spec_held_backend_unavailable_despite_worker(tmp_path, monkeypatch):
    from test_agentic_spec_writer import start as spec_start

    _no_key(monkeypatch)
    cfg, repo, iid, cid = spec_start(tmp_path, monkeypatch, lambda i: [])
    cfg["agentic"]["runner"] = "cursor"
    _worker_can_dispatch(monkeypatch)
    Q.tick(cfg)
    assert PG.read_holds(cfg).get(cid, {}).get("held_by") == "backend_unavailable"
    starts = [e for e in S.read_ledger(cfg, cid, types=["run.start"])
              if e.get("persona") == "spec_writer"]
    assert not starts


def test_write_spec_backend_hold_does_not_burn_persona_cap(tmp_path, monkeypatch):
    from test_agentic_spec_writer import start as spec_start

    _no_key(monkeypatch)
    cfg, repo, iid, cid1 = spec_start(tmp_path, monkeypatch, lambda i: [])
    cfg["agentic"]["runner"] = "cursor"
    cfg.setdefault("agentic", {}).setdefault("workers", {})["max_personas"] = 1
    _worker_can_dispatch(monkeypatch)
    cid2 = added(cfg, monkeypatch)
    Q.tick(cfg)
    assert S.read_state(cfg, cid2)["state"] == "IMPLEMENTING"
    assert PG.read_holds(cfg).get(cid1, {}).get("held_by") == "backend_unavailable"
    assert not [e for e in S.read_ledger(cfg, cid1, types=["run.start"])
                if e.get("persona") == "spec_writer"]
    hold2 = PG.read_holds(cfg).get(cid2, {}).get("held_by")
    assert hold2 != "persona_cap"


def test_do_write_spec_converts_startup_fault(tmp_path, monkeypatch):
    from nightwright.agentic import gitops as git
    from nightwright.agentic import spec_write as SW
    from nightwright.agentic.gitops import GitError

    wt = make_repo(tmp_path)
    cfg = _cursor_cfg(tmp_path)
    monkeypatch.setenv("CURSOR_API_KEY", "k-test-value")
    branch = git.current_branch(wt)
    head = git.rev_parse(wt)
    st = {
        "id": "c1", "state": "SPEC_WRITING",
        "repo": {"worktree": str(wt), "branch": branch},
        "spec_write": {"mode": "write", "number": 2, "path": "docs/specs/0002-x.md",
                       "head_before": head, "untracked_before": []},
    }
    monkeypatch.setattr(SW, "rollback", lambda *a, **k: None)
    (wt / "docs/specs").mkdir(parents=True, exist_ok=True)
    (wt / "docs/specs/0002-x.md").write_text("---\nid: SPEC-0002\n---\n")

    def fail_text(text):
        class Runner:
            detachable = False

            def run(self, req, hooks=None):
                return RunResult(1, False, False, 0.1, True, None, result_text=text)

        return Runner

    ctx = SimpleNamespace(cfg=cfg, runner=fail_text("no credential for provider openai")(),
                          stop=threading.Event(),
                          runner_for=lambda s, kind=None: fail_text("no credential for provider openai")(),
                          project=lambda s: {})
    req = SimpleNamespace(run_dir=tmp_path / "r", persona="spec_writer", pi_provider=None,
                          runner=None)
    BP.reset_noted()
    res = Q._do_write_spec(ctx, st, Q.Step("write_spec", persona="spec_writer"), "001-x", req, 1.0)
    assert res.interrupted and res.run_result.cost_usd == 0.0 and res.error is None
    counters = {"spec_write_retries": 0}
    row = {}
    Q._apply_spec_write(ctx, {**st, "counters": counters}, counters, res, row, [], lambda *a, **k: ())
    assert counters["spec_write_retries"] == 0
    assert row["to"] == "SPEC_WRITING" and row["reason"] == "interrupted"
    open_ = [r for r in ESC.list_all(cfg, "open") if r.get("kind") == "backend_unavailable"]
    assert len(open_) == 1 and open_[0]["facts"][0] == "backend: pi"

    ctx.runner = fail_text("pi-coding-agent is not installed")()
    ctx.runner_for = lambda s, kind=None: fail_text("pi-coding-agent is not installed")()
    BP.reset_noted()
    res = Q._do_write_spec(ctx, st, Q.Step("write_spec", persona="spec_writer"), "002-x", req, 1.0)
    assert res.interrupted and counters["spec_write_retries"] == 0

    ctx.runner = fail_text("unknown model openai/nope")()
    ctx.runner_for = lambda s, kind=None: fail_text("unknown model openai/nope")()
    counters["spec_write_retries"] = 0
    res = Q._do_write_spec(ctx, st, Q.Step("write_spec", persona="spec_writer"), "003-x", req, 1.0)
    assert not res.interrupted
    assert res.write["problems"]
    row = {}
    Q._apply_spec_write(ctx, {**st, "counters": counters}, counters, res, row, [], lambda *a, **k: ())
    assert counters["spec_write_retries"] == 1

    ctx.runner = fail_text("no credential for provider openai")()
    ctx.runner_for = lambda s, kind=None: fail_text("no credential for provider openai")()
    monkeypatch.setattr(SW, "rollback",
                        lambda *a, **k: (_ for _ in ()).throw(GitError(["git"], 1, "rollback")))
    counters = {"spec_write_retries": 0}

    def run(self, req, hooks=None):
        return RunResult(1, False, False, 0.1, True, None,
                         result_text="no credential for provider openai")

    ctx.runner = type("R", (), {"detachable": False, "run": run})()
    ctx.runner_for = lambda s, kind=None: ctx.runner
    res = Q._do_write_spec(ctx, st, Q.Step("write_spec", persona="spec_writer"), "004-x", req, 1.0)
    assert res.interrupted and res.error is None
    assert res.write["rollback_failed"]
    row = {}
    park = {}
    Q._apply_spec_write(
        ctx, {**st, "counters": counters}, counters, res, row, [],
        lambda to, reason, **kw: (row.update({"to": to, "reason": reason}),
                                  park.update(kw.get("park") or {})))
    assert counters["spec_write_retries"] == 0
    assert row["to"] == "PARKED" and row["reason"] == "spec_write_failed"
    assert "rollback" in (park.get("detail") or "").lower()

    preflight_text = (
        "the run was not started: the pi backend fails its preflight "
        "(no credential for provider openai)")
    counters = {"spec_write_retries": 0}
    row = {}
    park = {}

    def preflight_run(self, req, hooks=None):
        return RunResult(-1, False, True, 0.0, True, 0.0, None, result_text=preflight_text)

    ctx.runner = type("R", (), {"detachable": False, "run": preflight_run})()
    ctx.runner_for = lambda s, kind=None: ctx.runner
    res = Q._do_write_spec(ctx, st, Q.Step("write_spec", persona="spec_writer"), "005-x", req, 1.0)
    assert res.error is None and res.interrupted and res.write["rollback_failed"]
    Q._apply_spec_write(
        ctx, {**st, "counters": counters}, counters, res, row, [],
        lambda to, reason, **kw: (row.update({"to": to, "reason": reason}),
                                  park.update(kw.get("park") or {})))
    assert counters["spec_write_retries"] == 0
    assert row["to"] == "PARKED" and row["reason"] == "spec_write_failed"
    assert "rollback" in (park.get("detail") or "").lower()

    from test_agentic_spec_writer import start as spec_start

    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    _no_key(monkeypatch)
    p2 = tmp_path / "p2"
    p2.mkdir()
    cfg2, repo2, iid2, cid2 = spec_start(p2, monkeypatch, lambda i: [])
    cfg2["agentic"]["runner"] = "cursor"
    st2 = S.read_state(cfg2, cid2)
    st2["state"] = "PARKED"
    st2["park"] = {"reason": "spec_write_failed", "resume_to": "SPEC_WRITING",
                   "detail": park.get("detail") or "rollback"}
    S.write_state(cfg2, cid2, st2)
    Q.tick(cfg2)
    starts = [e for e in S.read_ledger(cfg2, cid2, types=["run.start"])
              if e.get("persona") == "spec_writer"]
    assert not starts


def test_do_resolve_converts_startup_fault(tmp_path, monkeypatch):
    from nightwright.agentic import gitops as git

    wt = make_repo(tmp_path)
    cfg = _cursor_cfg(tmp_path)
    monkeypatch.setattr(git, "restore_branch", lambda *a, **k: None)

    def fail_text(text):
        class Runner:
            detachable = False

            def run(self, req, hooks=None):
                return RunResult(1, False, False, 0.1, True, None, result_text=text)

        return Runner

    ctx = SimpleNamespace(cfg=cfg, runner=fail_text("no credential for provider openai")(),
                          stop=threading.Event(),
                          runner_for=lambda s, kind=None: fail_text("no credential for provider openai")(),
                          project=lambda s: {})
    req = SimpleNamespace(run_dir=tmp_path / "r", persona="conflict_resolver", runner=None)
    st = {
        "id": "c1", "state": "CLOSING",
        "closing": {"resolve": {"pre_head": "deadbeef", "target_sha": "abc", "fork": False},
                    "resolve_attempts": 0, "resolve_failed": {"n": 0}},
        "repo": {"worktree": str(wt), "branch": git.current_branch(wt)},
    }
    BP.reset_noted()
    res = Q._do_resolve(ctx, st, Q.Step("resolve", persona="conflict_resolver"), "003-x", req, 1.0)
    assert res.interrupted and res.run_result.cost_usd == 0.0 and res.error is None
    state = dict(st)
    row = {}
    events = []
    Q._apply(ctx, state, res)
    assert state["closing"].get("resolve_attempts", 0) == 0
    assert int((state["closing"].get("resolve_failed") or {}).get("n") or 0) == 0
    open_ = [r for r in ESC.list_all(cfg, "open") if r.get("kind") == "backend_unavailable"]
    assert open_[0]["facts"][0] == "backend: pi"

    ctx.runner = fail_text("unknown model openai/nope")()
    ctx.runner_for = lambda s, kind=None: fail_text("unknown model openai/nope")()
    st2 = dict(st)
    res = Q._do_resolve(ctx, st2, Q.Step("resolve", persona="conflict_resolver"), "004-x", req, 1.0)
    assert not res.interrupted
    counters = {}
    row = {}
    events = []
    finish = lambda *a, **k: row.update({"to": a[0], "reason": a[1]})
    Q._apply_resolve(st2, counters, res, res.run_result, row, events, finish)
    assert st2["closing"]["resolve_attempts"] == 1
    assert st2["closing"]["resolve_failed"]["n"] == 1

    from nightwright.agentic.gitops import GitError

    preflight_text = (
        "the run was not started: the pi backend fails its preflight "
        "(no credential for provider openai)")
    restore_calls = []

    def fail_restore(*a, **k):
        restore_calls.append(1)
        if len(restore_calls) > 1:
            raise GitError(["git"], 1, "restore")
        return None

    monkeypatch.setattr(git, "restore_branch", fail_restore)

    def preflight_run(self, req, hooks=None):
        return RunResult(-1, False, True, 0.0, True, 0.0, None, result_text=preflight_text)

    ctx.runner = type("R", (), {"detachable": False, "run": preflight_run})()
    ctx.runner_for = lambda s, kind=None: ctx.runner
    st3 = dict(st)
    res = Q._do_resolve(ctx, st3, Q.Step("resolve", persona="conflict_resolver"), "005-x", req, 1.0)
    assert res.error is None and res.interrupted
    state = dict(st3)
    Q._apply(ctx, state, res)
    assert state["closing"].get("resolve_attempts", 0) == 0
    assert int((state["closing"].get("resolve_failed") or {}).get("n") or 0) == 0
    assert state.get("state") == "CLOSING"


def test_check_pi_probe_exception_is_not_passing(tmp_path, monkeypatch):
    _pi_ok_env(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("probe broke")

    monkeypatch.setattr(BP, "_check_pi", boom)
    with pytest.raises(RuntimeError, match="probe broke"):
        BP.check("pi")
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"}}
    st = {"id": "c1", "state": "IMPLEMENTING"}
    monkeypatch.setattr(BP, "spawn_backend", lambda c, s, p: ("pi", ["openai"]))
    assert PG.Gate(cfg, ["c1"]).backend_fault(st, ("implementer",)) == "pi backend preflight failed"


def test_auth_json_oversize_counts_as_missing_credential(tmp_path, monkeypatch):
    _pi_ok_env(monkeypatch)
    agent = tmp_path / "agent"
    agent.mkdir(parents=True)
    env = {"HOME": str(tmp_path), "PI_CODING_AGENT_DIR": str(agent),
           "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    (agent / "auth.json").write_bytes(b"x" * (64 * 1024 + 1))
    assert BP.check("pi", environ=env, providers=["openai"]) == "no credential for provider openai"


def test_resolve_held_backend_unavailable_despite_worker(tmp_path, monkeypatch):
    _no_key(monkeypatch)
    repo = make_repo(tmp_path)
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "cursor"}}
    (tmp_path / "d" / "campaigns").mkdir(parents=True, exist_ok=True)
    st = {"id": "c1", "state": "CLOSING", "project": "Meta-Tools", "profile": "balanced",
          "closing": {"resolve": {"pre_head": "deadbeef"}},
          "repo": {"worktree": str(repo), "root": str(repo)}}
    S.write_state(cfg, "c1", dict(st))
    _worker_can_dispatch(monkeypatch)
    Q.tick(cfg)
    assert PG.read_holds(cfg).get("c1", {}).get("held_by") == "backend_unavailable"
    starts = [e for e in S.read_ledger(cfg, "c1", types=["run.start"])
              if e.get("persona") == "conflict_resolver"]
    assert not starts


def _track_closes(monkeypatch):
    closed = []
    real_close = os.close

    def track(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(os, "close", track)
    return closed


def test_can_dispatch_closes_slot_when_worker_full(tmp_path, monkeypatch):
    from nightwright.agentic import remote_personas as RPS

    closed = _track_closes(monkeypatch)
    slot_fd = os.open(os.devnull, os.O_RDONLY)
    cfg = {"data_dir": str(tmp_path / "d")}
    worker = {"name": "w", "enabled": True, "kinds": ["persona"], "persona_slots": 1}
    monkeypatch.setattr(RPS, "persona_workers", lambda c, s: [worker])
    monkeypatch.setattr(RPS, "ineligible", lambda *a, **k: None)
    monkeypatch.setattr(RPS, "acquire_slot", lambda c, w: (slot_fd, 0))
    monkeypatch.setattr(RPS, "busy", lambda *a, **k: 1)
    req = SimpleNamespace(persona="implementer", run_dir=tmp_path / "r")
    st = {"id": "c1", "state": "IMPLEMENTING", "repo": {"worktree": str(tmp_path)}}
    assert RPS.can_dispatch(cfg, st, "implement", req) is False
    assert slot_fd in closed


def test_can_dispatch_skips_acquire_without_workers(tmp_path, monkeypatch):
    from nightwright.agentic import remote_personas as RPS

    acquires = []
    monkeypatch.setattr(RPS, "persona_workers", lambda c, s: [])
    monkeypatch.setattr(RPS, "acquire_slot", lambda *a, **k: acquires.append(1) or (3, 0))
    req = SimpleNamespace(persona="implementer", run_dir=tmp_path / "r")
    st = {"id": "c1", "state": "IMPLEMENTING"}
    assert RPS.can_dispatch({"data_dir": str(tmp_path)}, st, "implement", req) is False
    assert not acquires


def test_can_dispatch_skips_acquire_when_ineligible(tmp_path, monkeypatch):
    from nightwright.agentic import remote_personas as RPS

    acquires = []
    worker = {"name": "w", "enabled": True, "kinds": ["persona"], "persona_slots": 1}
    monkeypatch.setattr(RPS, "persona_workers", lambda c, s: [worker])
    monkeypatch.setattr(RPS, "ineligible", lambda *a, **k: {"reason": "dirty"})
    monkeypatch.setattr(RPS, "acquire_slot", lambda *a, **k: acquires.append(1) or (3, 0))
    req = SimpleNamespace(persona="implementer", run_dir=tmp_path / "r")
    st = {"id": "c1", "state": "IMPLEMENTING"}
    assert RPS.can_dispatch({"data_dir": str(tmp_path)}, st, "implement", req) is False
    assert not acquires


def test_dispatch_held_runner_closes_reserved_slot_fd(tmp_path, monkeypatch):
    from nightwright.agentic import remote_personas as RPS

    closed = _track_closes(monkeypatch)
    slot_fd = os.open(os.devnull, os.O_RDONLY)
    cfg = {"data_dir": str(tmp_path / "d")}
    PG._write_json(PG.rate_limit_path(cfg),
                   {"until": "2099-01-01T00:00:00+00:00", "backoff_s": 900})
    req = RunRequest(
        persona="implementer", prompt="p", system_append="", cwd=str(tmp_path),
        model="m", effort=None, max_budget_usd=1.0, timeout_s=10,
        permission_mode="default", read_only=False, allowed_tools=(), add_dirs=(),
        json_schema={}, env={}, run_dir=tmp_path / "r")
    st = {"id": "c1", "state": "IMPLEMENTING"}
    reservations = {("c1", "implementer"): ("w", slot_fd, 0)}
    out = RPS.dispatch(cfg, st, "implement", req, local=PiRunner(),
                       reservations=reservations)
    assert isinstance(out, RPS.HeldRunner)
    assert slot_fd in closed
    assert not reservations


def test_backend_fault_probe_exception_releases_staged_slot_fd(tmp_path, monkeypatch):
    from nightwright.agentic import remote_personas as RPS

    closed = _track_closes(monkeypatch)
    _pi_ok_env(monkeypatch)
    real_check = BP.check

    def check(kind, environ=None, *, providers=None):
        if kind == "pi" and providers and providers[0] == "openai":
            return "no credential for provider openai"
        return real_check(kind, environ=environ, providers=providers)

    monkeypatch.setattr(BP, "check", check)

    def spawn(c, s, p):
        if p == "conformance_reviewer":
            return ("pi", ["openai"])
        if p == "adversarial_reviewer":
            return ("pi", ["anthropic"])
        return ("claude", [])

    monkeypatch.setattr(BP, "spawn_backend", spawn)
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"}}
    worker = {"name": "w", "enabled": True, "kinds": ["persona"], "persona_slots": 2}
    slot_fds = []

    def acquire(c, w):
        fd = os.open(os.devnull, os.O_RDONLY)
        slot_fds.append(fd)
        return (fd, len(slot_fds) - 1)

    calls = [0]

    def can_dispatch(c, s, kind, req, *, reservations=None):
        calls[0] += 1
        if calls[0] == 1:
            if reservations is not None:
                reservations[("c1", req.persona)] = (worker, slot_fds[-1], 0)
            return True
        raise RuntimeError("probe failed")

    monkeypatch.setattr(RPS, "persona_workers", lambda c, s: [worker])
    monkeypatch.setattr(RPS, "ineligible", lambda *a, **k: None)
    monkeypatch.setattr(RPS, "acquire_slot", acquire)
    monkeypatch.setattr(RPS, "busy", lambda *a, **k: 0)
    monkeypatch.setattr(PG, "order", lambda c, s, names: names)
    monkeypatch.setattr(PG, "failing", lambda *a, **k: False)
    monkeypatch.setattr(RPS, "_probe_request",
                        lambda c, s, p, k: SimpleNamespace(persona=p))
    monkeypatch.setattr(RPS, "can_dispatch", can_dispatch)
    repo = make_repo(tmp_path)
    st = {"id": "c1", "state": "REVIEWING", "project": "Meta-Tools", "profile": "balanced",
          "repo": {"worktree": str(repo), "root": str(repo)}}
    step = Q.Step("review", personas=("conformance_reviewer", "adversarial_reviewer"))
    g = PG.Gate(cfg, ["c1"])
    err = g.backend_fault(st, step.personas, remote_ok=True, step=step)
    assert err == "no credential for provider openai"
    assert all(fd in closed for fd in slot_fds)
    assert not g._slot_reservations
