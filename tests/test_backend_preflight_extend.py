"""SPEC-0486: backend preflight for spec review and Pi."""
from __future__ import annotations

import json
import shutil
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
    monkeypatch.setattr(BP, "_sdk_on_walk", lambda script: True)


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
    res = Q._do_spec_review(ctx, st, Q.Step("spec_review"), "002-x", req, 1.0)
    assert res.interrupted and counters["spec_review_retries"] == 0


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
    monkeypatch.setattr(BP, "_sdk_on_walk", lambda script: False)
    assert BP.check("pi", environ=env) is None
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
    _pi_check_fail_openai(monkeypatch)
    monkeypatch.setattr(BP, "spawn_backend", lambda c, s, p: ("pi", ["openai"]))
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"}}
    st = {"id": "c1", "state": "IMPLEMENTING"}
    BP.reset_noted()
    g = PG.Gate(cfg, ["c1"])
    workers = [{"w": 1}]

    def pw(c, s):
        return workers

    monkeypatch.setattr("nightwright.agentic.remote_personas.persona_workers", pw)
    assert g.backend_fault(st, ("implementer",), remote_ok=True) is None
    err = g.backend_fault(st, ("implementer",), remote_ok=False)
    assert err == "no credential for provider openai"
    assert len(journal(cfg, "backend_preflight_failed")) == 1

    def boom(c, s):
        raise RuntimeError("nope")

    monkeypatch.setattr("nightwright.agentic.remote_personas.persona_workers", boom)
    g2 = PG.Gate(cfg, ["c1"])
    g2._backend = dict(g._backend)
    assert g2.backend_fault(st, ("implementer",), remote_ok=True) == err


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
    assert open_ and open_[0]["facts"][0] == "backend: pi"


def test_pi_credential_mapped_skips_backend_escalation(tmp_path, monkeypatch):
    cfg = {"data_dir": str(tmp_path / "d"),
           "agentic": {"credentials": {"OPENAI_API_KEY": "pass:openai/key"}}}
    BP.note_fault(cfg, "pi", "no credential for provider openai", where="spawn")
    assert not [r for r in ESC.list_all(cfg, "open") if r.get("kind") == "backend_unavailable"]


def test_startup_check_warns_for_pi(tmp_path, monkeypatch):
    import io
    from nightwright.agentic import runner as RR
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


def test_claude_spec_review_is_not_held(tmp_path, monkeypatch):
    from test_agentic_spec_review_loop import begin, reviewed
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [reviewed()])
    cfg["agentic"]["runner"] = "claude"
    Q.tick(cfg)
    assert PG.read_holds(cfg).get(cid, {}).get("held_by") != "backend_unavailable"


def test_unknown_model_still_retries(tmp_path, monkeypatch):
    cfg = _cursor_cfg(tmp_path)
    monkeypatch.setenv("CURSOR_API_KEY", "k-test-value")
    wt = make_repo(tmp_path)
    st = {"id": "c1", "state": "SPEC_REVIEW", "repo": {"worktree": str(wt)},
          "spec": {"rel_path": "docs/specs/x.md", "id": "SPEC-1"},
          "counters": {"spec_review_retries": 0}}
    (wt / "docs/specs").mkdir(parents=True, exist_ok=True)
    (wt / "docs/specs/x.md").write_text("---\nid: SPEC-1\n---\n")
    failed = RunResult(1, False, False, 0.1, True, None, result_text="unknown model openai/nope")
    res = Q.StepResult("c1", Q.Step("spec_review"), "SPEC_REVIEW", run_id="001-x", budget=1.0,
                       run_result=failed, spec_review={"sha": "x"})
    res.interrupted = False
    ctx = SimpleNamespace(cfg=cfg)
    counters = dict(st["counters"])
    Q._apply_spec_review(ctx, st, {}, counters, res, {}, [], lambda *a, **k: {})
    assert counters["spec_review_retries"] == 1


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
    rep = Q.tick(cfg, runner=R.FakeRunner(cfg["agentic"]["fake_script"]))
    starts = [e for e in S.read_ledger(cfg, cid, types=["run.start"]) if e.get("persona") == "spec_reviewer"]
    assert starts, (rep.steps, PG.read_holds(cfg))


def test_resolution_errors_do_not_abort_or_hold(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    cfg["agentic"]["model_set"] = "does-not-exist-set"
    cid = added(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    st["state"] = "SPEC_REVIEW"
    st.setdefault("spec", {})["rel_path"] = "docs/specs/x.md"
    S.write_state(cfg, cid, st)
    Q.tick(cfg)  # must not raise
    assert PG.read_holds(cfg).get(cid, {}).get("held_by") != "backend_unavailable"
    starts = S.read_ledger(cfg, cid, types=["run.start"])
    assert not [e for e in starts if e.get("persona") == "spec_reviewer"]
    st = S.read_state(cfg, cid)
    assert st.get("state") == "PARKED" or "spec_review" in str(st.get("park", {}))


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


def test_spawn_backend_binds_model_set_pi_provider(tmp_path, monkeypatch):
    proj_root = tmp_path / "project_main"
    (proj_root / ".nw" / "model_sets").mkdir(parents=True)
    (proj_root / ".nw" / "model_sets" / "projpi.yaml").write_text(
        "schema: nw.model_set.v1\nrunner: pi\nmodel: openai/gpt-4.1\n")
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"model_set": "projpi", "runner": "claude"}}
    state = {"id": "c1", "state": "SPEC_REVIEW", "project": "Meta-Tools", "profile": "balanced",
             "repo": {"root": str(tmp_path / "repo")}}
    monkeypatch.setattr(PJ, "project_config", lambda c, p: {"_root": proj_root})
    kind, providers = BP.spawn_backend(cfg, state, "spec_reviewer")
    assert kind == "pi" and providers == ["openai"]


def test_spawn_backend_reads_project_pi_map(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    proj_root = tmp_path / "project_main"
    for d in (repo_root / ".nw" / "profiles", proj_root / ".nw"):
        d.mkdir(parents=True)
    (repo_root / ".nw" / "profiles" / "projprof.yaml").write_text(
        "schema: nw.profile.v1\nextends: balanced\npersonas:\n  spec_reviewer:\n"
        "    model: haiku\n    effort: high\n")
    (proj_root / ".nw" / "pi_map.yaml").write_text(
        "schema: nw.pi_map.v1\naliases:\n  haiku:\n    high: "
        "{provider: openai, id: gpt-4.1, thinking: high}\n")
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "pi"}}
    state = {"id": "c1", "state": "SPEC_REVIEW", "project": "Meta-Tools",
             "profile": "projprof", "repo": {"root": str(repo_root)}}
    monkeypatch.setattr(PJ, "project_config", lambda c, p: {"_root": proj_root})
    monkeypatch.setattr(
        "nightwright.agentic.profiles.resolve_profile",
        lambda c, name, project_root=None: {
            "name": "projprof", "personas": {"spec_reviewer": {"model": "haiku", "effort": "high"}},
            "sources": []})
    kind, providers = BP.spawn_backend(cfg, state, "spec_reviewer")
    assert kind == "pi" and providers == ["openai"]


def test_backend_fault_oserror_returns_none(tmp_path, monkeypatch):
    def boom(cfg, profile, **kw):
        raise OSError("nope")

    monkeypatch.setattr("nightwright.agentic.profiles.resolve_profile", boom)
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "cursor"}}
    st = {"id": "c1", "state": "SPEC_REVIEW", "project": "Meta-Tools", "profile": "balanced"}
    assert PG.Gate(cfg, ["c1"]).backend_fault(st, ("spec_reviewer",)) is None
