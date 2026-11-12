"""SPEC-0556: resumable implementer and reviewer sessions — the registry's validity rules, the
request shaping (continuation vs fresh prompt with the findings history), placement on the
recorded host, the resume-failure fallback, the personas that never resume, and the queue path
with a FakeRunner: loop-2 implementer and reviewers resumed, a non-resumable backend running the
same flow fresh, a failed resume falling back once in the same step, and flag off unchanged."""
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from test_agentic_collator import (ADV, CONF, IN_BUG, MARSHAL, _drive, _Recording, adv,
                                   collator, marshal)
from test_agentic_e2e import added, impl_done, make_cfg, make_repo, review, script
from nightwright.agentic import campaign_export as CE
from nightwright.agentic import collator as CO
from nightwright.agentic import runner as R
from nightwright.agentic import sessions as SS
from nightwright.agentic import store as S
from nightwright.agentic import queue as Q

pytestmark = pytest.mark.integration

ON = {"packet": True, "collator": True, "sessions": True}


# ---- pure ---------------------------------------------------------------------------------------

def _state(**over):
    st = {"id": "C1", "lean": dict(ON), "repo": {"worktree": "/wt"}, "counters": {"loops": 1}}
    st.update(over)
    return st


def _entry(**over):
    e = {"backend": "claude", "session_id": "s-1", "host": "local", "model": "claude-sonnet-5-5",
         "tier": None, "worktree": "/wt", "campaign": "C1", "context_tokens": 1000,
         "session_cost_usd": 0.4}
    e.update(over)
    return e


def _check(state, **over):
    kw = {"backend": "claude", "model": "claude-sonnet-5-5", "tier": None}
    kw.update(over)
    return SS.check({}, state, "implementer", **kw)


def test_registry_entry_valid_only_for_the_same_backend_model_tier_worktree_campaign():
    st = _state(sessions={"implementer": _entry()})
    assert _check(st) == (st["sessions"]["implementer"], None)
    assert _check(st, backend="cursor")[1] == "backend_not_resumable"
    assert _check(st, model="claude-haiku-5-5")[1] == "model_changed"
    assert _check(st, tier="judge")[1] == "tier_changed"
    assert _check(_state(sessions={"implementer": _entry(worktree="/other")}))[1] == \
        "worktree_changed"
    assert _check(_state(sessions={"implementer": _entry(campaign="C2")}))[1] == \
        "campaign_changed"
    assert _check(_state())[1] == "no_session"
    # a session is never shared across personas
    assert SS.check({}, st, "test_reviewer", backend="claude", model="claude-sonnet-5-5",
                    tier=None)[1] == "no_session"


def test_context_threshold_default_and_config():
    big = _state(sessions={"implementer": _entry(context_tokens=SS.DEFAULT_MAX_TOKENS)})
    assert _check(big)[1] == "context_limit"
    cfg = {"agentic": {"lean_session_max_tokens": 5000}}
    st = _state(sessions={"implementer": _entry(context_tokens=6000)})
    assert SS.check(cfg, st, "implementer", backend="claude", model="claude-sonnet-5-5",
                    tier=None)[1] == "context_limit"
    assert SS.max_tokens({"agentic": {"lean_session_max_tokens": True}}) == SS.DEFAULT_MAX_TOKENS


def test_context_tokens_last_call_else_summed():
    usage = {"input_tokens": 6, "cache_read_input_tokens": 89701,
             "iterations": [{"input_tokens": 4, "cache_read_input_tokens": 45003,
                             "cache_creation_input_tokens": 477}]}
    assert SS.context_tokens(usage) == 45484
    assert SS.context_tokens({"input_tokens": 10, "cache_read_input_tokens": 90}) == 100
    assert SS.context_tokens(None) == 0


@pytest.mark.parametrize("persona", ["gate_triager", "review_triager", "divergence_triager",
                                     "review_marshal", "findings_collator", "spec_writer",
                                     "spec_reviewer", "conflict_resolver"])
def test_other_personas_never_keep_a_session(persona):
    assert not SS.managed(_state(), persona)


def test_managed_needs_all_three_flags_and_skips_the_deep_close_pass():
    assert SS.managed(_state(), "implementer") and SS.managed(_state(), ADV)
    for lean in ({}, {"sessions": True}, {"sessions": True, "packet": True},
                 {"packet": True, "collator": True}):
        assert not SS.managed(_state(lean=lean), "implementer")
    deep = _state(deep_close={"status": "pending"})
    assert not SS.managed(deep, ADV) and SS.managed(deep, CONF)


def test_place_keeps_a_resume_only_on_the_recorded_host():
    req = NS(resume_session="s-1", persist_session=True, session_host="box",
             fresh_prompt="fresh", prompt="cont", resume_cost_base=0.1, session_dir=None)
    req = R.RunRequest(**{**_base_req(), **vars(req)})
    assert SS.place(req, "box", "claude") is req
    moved = SS.place(req, None, "claude")
    assert (moved.resume_session, moved.prompt, moved.persist_session) == (None, "fresh", True)
    assert moved.resume_cost_base is None and moved.fresh_prompt is None
    pi_remote = SS.place(replace(req, session_host="local"), "box", "pi")
    assert not pi_remote.persist_session and pi_remote.resume_session is None
    plain = R.RunRequest(**_base_req())
    assert SS.place(plain, "box", "claude") is plain


def _base_req():
    return dict(persona="implementer", prompt="p", system_append="", cwd=Path("/wt"), model="m",
                effort=None, max_budget_usd=1.0, timeout_s=60, permission_mode="dontAsk",
                read_only=False, allowed_tools=(), add_dirs=(), json_schema={}, env={},
                run_dir=Path("/r/001-implementer"))


def _rr(**over):
    base = dict(exit_code=0, timed_out=False, interrupted=False, duration_s=1.0, is_error=False,
                cost_usd=0.1, session_id="s-1", verdict={"status": "done"}, verdict_ok=True)
    base.update(over)
    return R.RunResult(**base)


def test_resume_failed():
    req = R.RunRequest(**_base_req(), resume_session="s-1")
    assert not SS.resume_failed(req, _rr())
    assert SS.resume_failed(req, _rr(is_error=True, verdict=None, verdict_ok=False))
    assert SS.resume_failed(req, _rr(verdict_ok=False))
    assert SS.resume_failed(req, _rr(session_id="other"))     # an old worker ran it fresh
    assert not SS.resume_failed(req, _rr(is_error=True, timed_out=True))
    assert not SS.resume_failed(req, _rr(is_error=True, interrupted=True))
    assert not SS.resume_failed(R.RunRequest(**_base_req()), _rr(is_error=True))


def _recorded(tmp_path, doc, **rr):
    d = tmp_path / "001-implementer"
    d.mkdir(exist_ok=True)
    (d / "request.json").write_text(json.dumps(doc))
    return _rr(raw_path=d / "result.json", **rr)


def test_record_writes_replaces_and_drops(tmp_path):
    st = _state()
    doc = {"model": "m", "persist_session": True, "session_backend": "claude"}
    marks = SS.record(st, "implementer", "001-implementer",
                      _recorded(tmp_path, doc, usage={"input_tokens": 5}), interrupted=False)
    e = st["sessions"]["implementer"]
    assert marks == {"resumed": False}
    assert (e["backend"], e["session_id"], e["host"], e["model"], e["first_loop"],
            e["context_tokens"], e["session_cost_usd"]) == ("claude", "s-1", "local", "m", 2, 5, 0.1)
    # a resumed run keeps first_loop and stores the cumulative session cost
    st["counters"]["loops"] = 2
    doc2 = {**doc, "resume_session": "s-1"}
    SS.record(st, "implementer", "002-implementer",
              _recorded(tmp_path, doc2, session_cost_usd=0.3, worker="box"))
    e = st["sessions"]["implementer"]
    assert (e["first_loop"], e["last_loop"], e["session_cost_usd"], e["host"]) == (2, 3, 0.3, "box")
    # a failed run (or a different session) drops the entry
    SS.record(st, "implementer", "003-implementer", _recorded(tmp_path, doc2, session_id="x"))
    assert "implementer" not in st["sessions"]
    SS.record(st, "implementer", "001-implementer", _recorded(tmp_path, doc))
    SS.record(st, "implementer", "004-implementer",
              _recorded(tmp_path, doc, verdict_ok=False, is_error=True))
    assert "implementer" not in st["sessions"]
    # flag off / a run without a session: nothing
    off = _state(lean={})
    assert SS.record(off, "implementer", "001", _recorded(tmp_path, doc)) == {}
    assert "sessions" not in off
    assert SS.record(_state(), "implementer", "001", _recorded(tmp_path, {"model": "m"})) == {}


# ---- through the queue (FakeRunner) ---------------------------------------------------------------

IN_REC = [{"id": f"{ADV}#0", "merged": [], "tie": "diff:feature.txt", "scope": "in",
           "reason": "defect in changed lines", "severity": "must_fix"}]


def _setup(tmp_path, lean=ON):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []), profile={"reviewers": [CONF, ADV]})
    if lean is not None:
        cfg["agentic"]["lean"] = dict(lean)
    return repo, cfg


def _scripts(impl=None):
    return {"implementer": impl or [impl_done(), impl_done(content="better\n")],
            MARSHAL: [marshal(), marshal()], CONF: [review(), review()],
            ADV: [adv("fix", [IN_BUG]), adv("pass")], CO.NAME: [collator(IN_REC)]}


def _prior_fixed(runner, cfg, cid):
    """Loop 2's adversarial reviewer marks its own prior findings fixed (either prompt)."""
    orig = runner._next_entry

    def next_entry(req):
        e = orig(req)
        if req.persona == ADV and "delta review" in req.prompt:
            doc = json.loads((S.campaign_dir(cfg, cid) / "packet-loop-2.json").read_text())
            e = json.loads(json.dumps(e))
            e["verdict"]["prior_status"] = [
                {"id": f["id"], "status": "fixed", "evidence": "ok"} for f in doc["findings"]
                if f["persona"] == ADV and f["status"] == "open"]
        return e
    runner._next_entry = next_entry


def _by(runner, persona):
    return [r for r in runner.reqs if r.persona == persona]


def test_loop_two_implementer_and_reviewers_resume_their_sessions(tmp_path, monkeypatch):
    _repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch)
    runner = _Recording(_scripts())
    _prior_fixed(runner, cfg, cid)
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    i1, i2 = _by(runner, "implementer")
    assert i1.persist_session and i1.resume_session is None
    assert i2.resume_session == f"fake-implementer-{i1.run_dir.name}"
    assert i2.prompt.startswith("# Continue: implementer")
    assert "feature text is wrong" in i2.prompt and "## Procedure" not in i2.prompt
    assert "## Procedure" in i2.fresh_prompt and "## Findings history" in i2.fresh_prompt
    assert "findings-loop-1.json" in i2.prompt
    for persona in (CONF, ADV):
        r1, r2 = _by(runner, persona)
        assert r1.persist_session and r1.resume_session is None
        assert r2.resume_session == f"fake-{persona}-{r1.run_dir.name}"
        assert r2.prompt.startswith(f"# Continue: {persona}") and "```diff" in r2.prompt
        assert "+better" in r2.prompt                  # the delta, not the whole change
        assert "delta review" in r2.prompt
    assert "feature text is wrong" in _by(runner, ADV)[1].prompt      # its own prior finding
    assert "feature text is wrong" not in _by(runner, CONF)[1].prompt
    for persona in (MARSHAL, CO.NAME):          # never kept, never resumed
        for r in _by(runner, persona):
            assert not r.persist_session and r.resume_session is None
    reg = st["sessions"]
    assert set(reg) == {"implementer", CONF, ADV}
    assert reg["implementer"]["first_loop"] == 1 and reg["implementer"]["last_loop"] == 2
    ends = {e["run_id"]: e for e in S.read_ledger(cfg, cid, types=["run.end"])}
    assert ends[i2.run_dir.name]["resumed"] is True and ends[i1.run_dir.name]["resumed"] is False
    assert "resumed" not in ends[_by(runner, MARSHAL)[0].run_dir.name]
    # the resumed run is charged its own cost, not the session's running total
    assert st["cost"]["by_persona"]["implementer"] == pytest.approx(1.0)
    rows = {r["run_id"]: r for r in CE.export_tables(cfg)["runs"]}
    assert rows[i2.run_dir.name]["resumed"] is True and rows[i1.run_dir.name]["resumed"] is False
    assert not any(r["session_fallback"] for r in rows.values())
    assert rows[i2.run_dir.name]["cost_usd"] == pytest.approx(0.5)


def test_a_non_resumable_backend_runs_the_same_flow_fresh(tmp_path, monkeypatch):
    monkeypatch.setitem(R.SESSION_RESUME, "fake", False)
    _repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch)
    runner = _Recording(_scripts())
    _prior_fixed(runner, cfg, cid)
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    assert all(not r.persist_session and r.resume_session is None for r in runner.reqs)
    i2 = _by(runner, "implementer")[1]
    assert "## Procedure" in i2.prompt and "## Findings history" in i2.prompt
    assert "findings-loop-1.json" in i2.prompt and "packet-loop-1.json" in i2.prompt
    assert "## Findings history" in _by(runner, ADV)[1].prompt
    assert "## Findings history" not in _by(runner, "implementer")[0].prompt   # loop 1
    assert "## Findings history" not in _by(runner, ADV)[0].prompt
    assert "sessions" not in st


def test_a_failed_resume_falls_back_once_to_a_fresh_session(tmp_path, monkeypatch):
    _repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch)
    gone = impl_done(content="never\n", resume_error=True)
    runner = _Recording(_scripts([impl_done(), gone, impl_done(content="better\n")]))
    _prior_fixed(runner, cfg, cid)
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    assert not S.read_ledger(cfg, cid, types=["park"])
    i1, i2, i3 = _by(runner, "implementer")
    assert i2.resume_session and i3.resume_session is None and i3.persist_session
    assert i3.prompt == i2.fresh_prompt and "## Procedure" in i3.prompt
    [fb] = S.read_ledger(cfg, cid, types=["session_fallback"])
    assert (fb["persona"], fb["run_id"], fb["retry_run_id"], fb["session_id"]) == (
        "implementer", i2.run_dir.name, i3.run_dir.name, i2.resume_session)
    starts = {e["run_id"]: e for e in S.read_ledger(cfg, cid, types=["run.start"])}
    assert starts[i3.run_dir.name]["session_fallback"] is True
    assert starts[i3.run_dir.name]["retry_of"] == i2.run_dir.name
    assert "session_fallback" not in starts[i2.run_dir.name]
    # the fallback's fresh session is the one recorded now
    assert st["sessions"]["implementer"]["session_id"] == f"fake-implementer-{i3.run_dir.name}"
    rows = {r["run_id"]: r for r in CE.export_tables(cfg)["runs"]}
    assert rows[i3.run_dir.name]["session_fallback"] is True
    assert rows[i2.run_dir.name]["resumed"] is True


class _OnWorker:
    """A stand-in remote runner: the FakeRunner's run, reported as run on `name`."""
    detachable = False

    def __init__(self, inner, name):
        self.inner, self.worker = inner, {"name": name}

    def run(self, req, *, hooks=None):
        return replace(self.inner.run(req), worker=self.worker["name"])


@pytest.mark.parametrize("second, resumed", [("box", True), (None, False), ("other", False)])
def test_remote_runs_resume_only_on_the_recorded_host(tmp_path, monkeypatch, second, resumed):
    _repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch)
    runner = _Recording(_scripts())
    _prior_fixed(runner, cfg, cid)
    hosts = iter(["box", second])

    def dispatch(cfg, state, kind, req, **kw):
        if req.persona != "implementer":
            return None
        name = next(hosts)
        return _OnWorker(runner, name) if name else None
    monkeypatch.setattr(Q.RPS, "dispatch", dispatch)
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    i1, i2 = _by(runner, "implementer")
    assert bool(i2.resume_session) is resumed
    assert i2.prompt.startswith("# Continue") is resumed
    if not resumed:
        assert "## Procedure" in i2.prompt and "## Findings history" in i2.prompt
    assert not S.read_ledger(cfg, cid, types=["session_fallback"])
    want = second or "local"
    assert st["sessions"]["implementer"]["host"] == want


def test_remote_result_doc_carries_the_session_cost():
    from nightwright.agentic import remote_personas as RPS
    assert RPS._result_doc(_rr(session_cost_usd=0.7))["session_cost_usd"] == 0.7
    assert "session_cost_usd" not in RPS._result_doc(_rr())


@pytest.mark.parametrize("pinned", [None, {"packet": True, "collator": True},
                                    {"tiers": True, "packet": True, "collator": True}])
def test_flag_off_or_pinned_before_the_flag_is_unchanged(tmp_path, monkeypatch, pinned):
    """No session field on any request, no `sessions` state, no `resumed` mark, no request.json
    session keys, today's prompts; also for a campaign pinned before the merge (no `sessions`
    key in `state.lean`) when the live config is flipped on after the open."""
    _repo, cfg = _setup(tmp_path, lean=pinned)
    cid = added(cfg, monkeypatch)
    cfg["agentic"]["lean"] = {"tiers": True, "packet": True, "collator": True, "sessions": True}
    scripts = _scripts()
    if not pinned:
        scripts.pop(MARSHAL)
        scripts.pop(CO.NAME)
    runner = _Recording(scripts)
    _prior_fixed(runner, cfg, cid)
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    assert (st.get("lean") or {}).get("sessions") is not True
    for r in runner.reqs:
        assert not any(getattr(r, k) for k in R.SESSION_KEYS) and r.fresh_prompt is None
        assert "## Findings history" not in r.prompt and not r.prompt.startswith("# Continue")
        assert "--no-session-persistence" in R.build_argv(r)
        doc = json.loads((r.run_dir / "request.json").read_text())
        assert not set(R.SESSION_KEYS) & set(doc)
        assert not (r.run_dir / R.FRESH_PROMPT).exists()
    assert "sessions" not in st
    for e in S.read_ledger(cfg, cid, types=["run.end"]):
        assert "resumed" not in e
    assert not S.read_ledger(cfg, cid, types=["session_fallback"])


def test_flag_off_build_request_never_reaches_the_session_code(tmp_path, monkeypatch):
    """With the flag off the shaping is never called (patched to raise)."""
    def boom(*a, **k):
        raise AssertionError("session code reached with the flag off")
    monkeypatch.setattr(SS, "shape", boom)
    monkeypatch.setattr(SS, "place", boom)
    _repo, cfg = _setup(tmp_path, lean=None)
    cid = added(cfg, monkeypatch)
    runner = _Recording({"implementer": [impl_done()], CONF: [review()], ADV: [adv("pass")]})
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
