"""SPEC-0344: remote persona jobs with bundle write-back. Offline: the worker is SPEC-0338's
loopback (`test_remote_gates.Remote`, a fake `ssh` running `nw worker ssh-command` against a
second data dir) with `FakeRunner` selected on the worker (`NW_AGENTIC_RUNNER=fake`)."""
import datetime as dt
import json
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright.agentic import events as EV
from nightwright.agentic import queue as Q
from nightwright.agentic import remote_jobs as RJ
from nightwright.agentic import remote_personas as RPS
from nightwright.agentic import runner as R
from nightwright.agentic import store as S
from nightwright.agentic import worker as W
from nightwright.agentic import workers as WK

from test_remote_gates import CID, Remote, World, _git, _script
from test_remote_gates import _short_runtime_dir  # noqa: F401  (autouse fixture)
from waits import join, until

INFO = {"protocol": "nw.job.v1", "features": ["async", "persona"], "git": "git version 2"}
VERDICT = {"summary": "did it"}

# commits c.txt; leaves a modified tracked file (a.txt) and a new untracked one (new.txt)
WRITER = [{"edits": [{"path": "c.txt", "write": "c\n"}, {"path": "a.txt", "append": "more\n"},
                     {"path": "new.txt", "write": "new\n"}],
           "shell": ["git add c.txt && git commit -q -m 'add c'"],
           "verdict": VERDICT, "cost_usd": 0.5}]


def _write_script(path: Path, entries) -> Path:
    path.write_text(json.dumps(entries))
    return path


def _world(tmp_path, name="w") -> World:
    d = tmp_path / name
    d.mkdir()
    w = World(d)
    st = S.read_state(w.cfg, CID)
    st["state"] = "IMPLEMENTING"
    S.write_state(w.cfg, CID, st)
    return w


def _remote(w: World, worker_script: Path, *, kinds=("gate", "persona"), progress="0.2"):
    r = Remote(w)
    ssh = _script(w.tmp / "fake-ssh-persona",
                  f"export NW_AGENTIC_RUNNER=fake NW_AGENTIC_FAKE_SCRIPT={worker_script} "
                  f"NW_WORKER_PROGRESS_S={progress}\nexec {r.ssh} \"$@\"")
    r.register(ssh=ssh, info=INFO, kinds=list(kinds))
    return r


def _req(w: World, run_id="001-implementer", persona="implementer", read_only=False, **kw):
    wt = w.repo
    fields = dict(
        persona=persona, prompt="do it", system_append="sys", cwd=wt, model="sonnet",
        effort=None, max_budget_usd=1.0, timeout_s=600, permission_mode="bypassPermissions",
        read_only=read_only, allowed_tools=(f"Bash({wt}/tools/check.sh*)", "Bash(uv run pytest*)"),
        add_dirs=(wt,), json_schema={"type": "object", "required": ["summary"]},
        env={"NW_CAMPAIGN": CID, "NW_SPEC_ID": "SPEC-0900", "NW_BASE": "main",
             "NW_PERSONA_RUN": "1", "NW_WORKTREE": str(wt), "NW_DATA_DIR": w.cfg["data_dir"],
             "NW_CONFIG_DIR": w.cfg["config_dir"], "TMPDIR": str(w.tmp / "t"),
             "TEMP": str(w.tmp / "t"), "TMP": str(w.tmp / "t")},
        run_dir=S.runs_dir(w.cfg, CID) / run_id)
    fields.update(kw)
    return R.RunRequest(**fields)


def _start(w: World, req):
    """The `run.start` the queue's `_prepare_run` writes."""
    S.append_event(w.cfg, CID, "run.start", {
        "persona": req.persona, "run_id": req.run_dir.name, "model": req.model,
        "effort": None, "max_budget_usd": req.max_budget_usd, "timeout_s": req.timeout_s,
        "permission_mode": req.permission_mode, "state": "IMPLEMENTING",
        "prompt_sha256": "0" * 64,
        "step": {"kind": "implement", "personas": []}})
    st = S.read_state(w.cfg, CID)
    st["counters"]["runs"] = int(req.run_dir.name[:3])
    S.write_state(w.cfg, CID, st)


def _ctx(w: World, local_script: Path):
    ctx = Q._Ctx(w.cfg, runner=R.FakeRunner(local_script))
    ctx._projects[w.state["project"]] = w.project
    return ctx


def _snap(repo: Path) -> dict:
    head = _git(repo, "rev-parse", "HEAD")
    return {"head_tree": _git(repo, "rev-parse", "HEAD^{tree}"),
            "status": _git(repo, "status", "--porcelain"), "diff": _git(repo, "diff"),
            "new": (repo / "new.txt").read_text() if (repo / "new.txt").exists() else None,
            "head": head}


# ---- write-back equality ---------------------------------------------------------------------

def test_write_back_equals_a_local_run(tmp_path):
    from nightwright.agentic import gitops
    script = _write_script(tmp_path / "writer.json", WRITER)
    remote_w, local_w = _world(tmp_path, "remote"), _world(tmp_path, "local")
    _remote(remote_w, script)
    base = _git(remote_w.repo, "rev-parse", "HEAD")
    st = S.read_state(remote_w.cfg, CID)
    rr = RPS.dispatch(remote_w.cfg, st, "implement", _req(remote_w))
    assert isinstance(rr, R.RemoteRunner)
    got = rr.run(_req(remote_w))
    assert got.worker == "box" and got.verdict == VERDICT and got.verdict_ok
    want = R.FakeRunner(script).run(_req(local_w))
    assert want.verdict == got.verdict
    a, b = _snap(remote_w.repo), _snap(local_w.repo)
    assert a["head"] != base                     # the fast-forward brought the commit
    assert {k: v for k, v in a.items() if k != "head"} == \
        {k: v for k, v in b.items() if k != "head"}
    assert "?? new.txt" in a["status"] and "M a.txt" in a["status"]
    ca = gitops.commit_all(remote_w.repo, "m")
    cb = gitops.commit_all(local_w.repo, "m")
    assert ca and cb
    assert _git(remote_w.repo, "rev-parse", "HEAD^{tree}") == \
        _git(local_w.repo, "rev-parse", "HEAD^{tree}")
    run = json.loads((_req(remote_w).run_dir / "run.json").read_text())
    assert run["worker"] == "box"
    assert json.loads((_req(local_w).run_dir / "run.json").read_text())["worker"] is None
    assert not RJ.live(remote_w.cfg, CID)                      # the record went
    assert not RPS._refs_of(remote_w.repo, RPS.job_id_for(CID, "001-implementer"))


# ---- read-only reviewer, progress ------------------------------------------------------------

def _norm_request(doc, tree):
    doc = dict(doc)
    for k in ("cwd", "started_at"):
        doc.pop(k, None)
    return doc


def test_read_only_reviewer_same_verdict_files_and_progress(tmp_path):
    entries = [{"verdict": {"summary": "lgtm"}, "cost_usd": 0.1, "sleep_s": 1.0,
                "transcript": '{"type": "result"}\n'}]
    script = _write_script(tmp_path / "rev.json", entries)
    remote_w, local_w = _world(tmp_path, "remote"), _world(tmp_path, "local")
    _remote(remote_w, script)
    (remote_w.repo / "a.txt").write_text("dirty tracked\n")     # read-only: no clean rule
    (local_w.repo / "a.txt").write_text("dirty tracked\n")
    st = S.read_state(remote_w.cfg, CID)
    req = _req(remote_w, "001-reviewer", persona="reviewer", read_only=True)
    rr = RPS.dispatch(remote_w.cfg, st, "review", req)
    assert isinstance(rr, R.RemoteRunner)
    got = rr.run(req)
    want = R.FakeRunner(script).run(_req(local_w, "001-reviewer", persona="reviewer",
                                         read_only=True))
    assert (got.verdict, got.verdict_ok) == (want.verdict, want.verdict_ok) == \
        ({"summary": "lgtm"}, True)
    rd, ld = req.run_dir, _req(local_w, "001-reviewer").run_dir
    for name in ("result.json", "stream.jsonl", "request.json"):
        assert (rd / name).is_file(), name
    ra = json.loads((rd / "request.json").read_text())
    la = json.loads((ld / "request.json").read_text())
    assert _norm_request(ra, None) == _norm_request(la, None)
    prog = json.loads((rd / "progress.json").read_text())
    assert set(prog) == {"ts", "transcript_bytes", "last_line_ts"}
    assert _git(remote_w.repo, "status", "--porcelain") == "M a.txt"   # untouched


# ---- write-back failures ---------------------------------------------------------------------

LOCAL_SNAP = [{"pre_shell": ["git rev-parse HEAD > ../snap-head",
                             "git status --porcelain > ../snap-status"],
               "verdict": VERDICT, "cost_usd": 0.0}]


def _failure_case(tmp_path, monkeypatch, hook, worker_entries=WRITER, clean_stub=False):
    script = _write_script(tmp_path / "writer.json", worker_entries)
    local = _write_script(tmp_path / "local.json", LOCAL_SNAP)
    w = _world(tmp_path)
    _remote(w, script)
    base = _git(w.repo, "rev-parse", "HEAD")
    monkeypatch.setattr(RPS, "_before_writeback", lambda wt, bundle: hook(w, bundle))
    if clean_stub:
        monkeypatch.setattr(RPS, "_worktree_clean", lambda wt: True)
    req = _req(w)
    _start(w, req)
    ctx = _ctx(w, local)
    st = S.read_state(w.cfg, CID)
    res = Q._do_persona(ctx, st, Q.Step("implement", persona="implementer"), "001-implementer",
                        req, 1.0)
    return w, base, res, st


def _check_failure(w, res, st, reason):
    ends = w.ledger(["run.end"])
    assert [e["run_id"] for e in ends] == ["001-implementer"]
    end = ends[0]
    assert end["is_error"] and end["writeback_failed"] and end["worker"] == "box"
    assert end["result_tail"].startswith(f"write-back: {reason}")
    kind, data = EV._project_ledger_event(end, st, EV.settings(w.cfg), False)
    assert kind == "run_failed" and data["transient"] is True and data["worker"] == "box"
    assert S.read_state(w.cfg, CID)["remote_excluded"] == ["box"]
    starts = w.ledger(["run.start"])
    assert [e["run_id"] for e in starts] == ["001-implementer", "002-implementer"]
    assert starts[1]["retry_of"] == "001-implementer"
    assert res.run_id == "002-implementer" and res.run_result.worker is None
    assert res.run_result.verdict == VERDICT
    assert not w.ledger(["park"])                               # no autopark
    rec = json.loads((S.runs_dir(w.cfg, CID) / "001-implementer" / "run.json").read_text())
    assert rec["writeback_failed"] and rec["worker"] == "box"
    return rec


def _local_saw(w):
    return ((w.tmp / "snap-head").read_text().strip(),
            (w.tmp / "snap-status").read_text().rstrip("\n"))


def test_write_back_failure_head_moved(tmp_path, monkeypatch):
    moved = {}

    def hook(w, bundle):
        moved["sha"] = __import__("test_remote_gates")._commit(w.repo, "x.txt", "x\n")
    w, base, res, st = _failure_case(tmp_path, monkeypatch, hook)
    _check_failure(w, res, st, "head moved")
    assert _local_saw(w) == (moved["sha"], "")                  # untouched


def test_write_back_failure_dirty_worktree(tmp_path, monkeypatch):
    w, base, res, st = _failure_case(
        tmp_path, monkeypatch, lambda w, b: (w.repo / "late.txt").write_text("late\n"))
    _check_failure(w, res, st, "worktree dirty")
    assert _local_saw(w) == (base, "?? late.txt")


def test_write_back_failure_stash_conflict_rolls_back(tmp_path, monkeypatch):
    w, base, res, st = _failure_case(
        tmp_path, monkeypatch, lambda w, b: (w.repo / "a.txt").write_text("human edit\n"),
        clean_stub=True)
    rec = _check_failure(w, res, st, "stash conflict")
    head, status = _local_saw(w)
    assert head == base and status == " M a.txt"                # prior status, no c/new.txt
    job = RPS.job_id_for(CID, "001-implementer")
    assert sorted(rec["saved_refs"]) == [f"refs/nw/jobs/{job}/head", f"refs/nw/jobs/{job}/wip"]
    for ref in rec["saved_refs"]:
        _git(w.repo, "rev-parse", "--verify", ref)              # kept for inspection
    assert w.ledger(["run.end"])[0]["saved_refs"] == rec["saved_refs"]


def test_write_back_failure_corrupt_bundle(tmp_path, monkeypatch):
    import base64

    def hook(w, bundle):
        bundle["bundle_b64"] = base64.b64encode(b"not a bundle").decode()
    w, base, res, st = _failure_case(tmp_path, monkeypatch, hook)
    _check_failure(w, res, st, "corrupt bundle")
    assert _local_saw(w) == (base, "")


# ---- restart / adoption ----------------------------------------------------------------------

def _detached_job(tmp_path):
    entries = [dict(WRITER[0], sleep_s=1.5)]
    script = _write_script(tmp_path / "writer.json", entries)
    local = _write_script(tmp_path / "local.json", [])
    w = _world(tmp_path)
    _remote(w, script)
    req = _req(w)
    _start(w, req)
    ctx = _ctx(w, local)
    st = S.read_state(w.cfg, CID)
    hooks = R.RunHooks(threading.Lock(), req.run_dir)
    out = {}
    t = threading.Thread(target=lambda: out.update(res=Q._do_persona(
        ctx, st, Q.Step("implement", persona="implementer"), "001-implementer", req, 1.0,
        hooks)))
    t.start()
    job = RPS.job_id_for(CID, "001-implementer")
    until(lambda: (RJ.read(w.cfg, job) or {}).get("accepted_at"))
    hooks.detach.set()
    join(t)
    assert out["res"].detached
    assert RJ.read(w.cfg, job)["state"] == "detached"
    return w, ctx, req, job


def _adopt(w, ctx, req):
    st = S.read_state(w.cfg, CID)
    hooks = R.RunHooks(threading.Lock(), req.run_dir, phase="wait")
    return Q._do_persona(ctx, st, Q.Step("implement", persona="implementer"),
                         "001-implementer", req, 1.0, hooks, {"exited_before_start": False})


def test_restart_adopts_and_writes_back_once(tmp_path):
    w, ctx, req, job = _detached_job(tmp_path)
    base = _git(w.repo, "rev-parse", "HEAD")
    assert RJ.read(w.cfg, job)["base_head"] == base and RJ.read(w.cfg, job)["worker"] == "box"
    res = _adopt(w, ctx, req)
    assert res.run_result.worker == "box" and res.commit_sha
    log = _git(w.repo, "log", "--format=%s", f"{base}..HEAD").splitlines()
    assert log == [res.commit_message.splitlines()[0], "add c"]  # fast-forward once, then commit
    assert (w.repo / "new.txt").read_text() == "new\n"
    assert RJ.read(w.cfg, job) is None


def test_adopted_record_already_applied_is_not_applied_again(tmp_path):
    w, ctx, req, job = _detached_job(tmp_path)
    base = _git(w.repo, "rev-parse", "HEAD")
    RJ.update(w.cfg, job, writeback="applied")      # a crash after write-back, before the end
    res = _adopt(w, ctx, req)
    assert res.run_result.worker == "box" and not res.run_result.writeback_failed
    assert _git(w.repo, "rev-parse", "HEAD") == base and not (w.repo / "c.txt").exists()


# ---- eligibility -----------------------------------------------------------------------------

@pytest.fixture
def ew(tmp_path):
    w = _world(tmp_path)
    _remote(w, _write_script(tmp_path / "s.json", WRITER))
    return w


def _dispatch(w, req=None, step="implement", state=None):
    rr = RPS.dispatch(w.cfg, state or S.read_state(w.cfg, CID), step, req or _req(w))
    if rr is not None:
        import os
        os.close(rr.slot)
    return rr


def test_eligible_baseline(ew):
    assert isinstance(_dispatch(ew), R.RemoteRunner)


@pytest.mark.parametrize("case", ["add_dir_outside", "tool_outside", "untracked", "modified",
                                  "conflict_resolver", "rebase", "excluded"])
def test_ineligible_runs_stay_local(ew, tmp_path, case):
    req, step, state = _req(ew), "implement", None
    if case == "add_dir_outside":
        req = _req(ew, add_dirs=(ew.repo, tmp_path / "elsewhere"))
    elif case == "tool_outside":
        req = _req(ew, allowed_tools=("Bash(/usr/local/bin/thing*)",))
    elif case == "untracked":
        (ew.repo / "u.txt").write_text("u\n")
    elif case == "modified":
        (ew.repo / "a.txt").write_text("changed\n")
    elif case == "conflict_resolver":
        req, step = _req(ew, persona="conflict_resolver"), "resolve"
    elif case == "rebase":
        (ew.repo / ".git" / "rebase-merge").mkdir()
    elif case == "excluded":
        state = dict(S.read_state(ew.cfg, CID), remote_excluded=["box"])
    assert _dispatch(ew, req, step, state) is None


def test_modified_tracked_file_is_fine_for_a_read_only_persona(ew):
    (ew.repo / "a.txt").write_text("changed\n")
    assert isinstance(_dispatch(ew, _req(ew, persona="reviewer", read_only=True)),
                      R.RemoteRunner)


def test_default_kinds_keep_persona_runs_local(tmp_path):
    w = _world(tmp_path)
    _remote(w, _write_script(tmp_path / "s.json", WRITER), kinds=["gate"])
    assert _dispatch(w) is None
    WK.path(w.cfg).unlink()
    assert _dispatch(w) is None


def test_persona_slots_are_a_pool(ew):
    st = S.read_state(ew.cfg, CID)
    first = RPS.dispatch(ew.cfg, st, "implement", _req(ew))
    try:
        assert RPS.dispatch(ew.cfg, st, "implement", _req(ew)) is None    # 1 slot, held
    finally:
        import os
        os.close(first.slot)
    assert (Path(ew.cfg["data_dir"]) / "agentic" / "gate-slots" / "w-box"
            / "persona-0.lock").exists()


# ---- serialization and env -------------------------------------------------------------------

def test_request_round_trip_re_anchors_paths(tmp_path):
    w = _world(tmp_path)
    req = _req(w)
    doc = R.request_to_job(req, w.repo)
    assert doc["allowed_tools"][0] == "Bash(${NW_TREE}/tools/check.sh*)"
    assert doc["cwd"] == "." and doc["add_dirs"] == ["."]
    assert set(doc["env"]) == {"NW_CAMPAIGN", "NW_SPEC_ID", "NW_BASE", "NW_PERSONA_RUN"}
    doc = json.loads(json.dumps(doc))
    tree = tmp_path / "wtree"
    back = R.request_from_job(doc, tree, tmp_path / "wrun",
                              env={"NW_WORKTREE": str(tree), "TMPDIR": "/w/tmp"})
    assert back.allowed_tools == (f"Bash({tree}/tools/check.sh*)", "Bash(uv run pytest*)")
    assert back.cwd == tree and back.add_dirs == (tree,)
    assert back.env["NW_WORKTREE"] == str(tree) and back.env["TMPDIR"] == "/w/tmp"
    assert back.env["NW_CAMPAIGN"] == CID and "NW_DATA_DIR" not in back.env
    same = ("persona", "prompt", "system_append", "model", "effort", "max_budget_usd",
            "timeout_s", "permission_mode", "read_only", "json_schema", "profile",
            "profile_sources")
    assert {k: getattr(back, k) for k in same} == {k: getattr(req, k) for k in same}


def test_env_allowlist(tmp_path):
    w = _world(tmp_path)
    with pytest.raises(R.NotRemotable, match="SECRET"):
        R.request_to_job(_req(w, env={"NW_CAMPAIGN": CID, "SECRET": "x"}), w.repo)
    doc = R.request_to_job(_req(w), w.repo)
    doc["env"]["SECRET"] = "x"
    with pytest.raises(ValueError, match="SECRET"):
        R.request_from_job(doc, tmp_path, tmp_path)
    h = {"schema": W.JOB_SCHEMA, "kind": "persona", "job_id": "j1", "repo_key": "k",
         "campaign": CID, "spec_id": "S", "commit": "a" * 40,
         "base": {"name": "main", "sha": "b" * 40}, "bundle_bytes": 0, "env": {},
         "payload": {"run_id": "001-implementer", "persona": "implementer", "request": doc,
                     "json_schema": {}, "read_only": False, "timeout_s": 60,
                     "max_budget_usd": 1.0}}
    with pytest.raises(W.WorkerError, match="SECRET"):
        W.validate_header(h)
    del doc["env"]["SECRET"]
    W.validate_header(h)


# ---- detectors -------------------------------------------------------------------------------

def test_open_remote_run_fires_run_overdue_not_campaign_silent(tmp_path):
    w, ctx, req, job = _detached_job(tmp_path)
    now = dt.datetime.now().astimezone() + dt.timedelta(seconds=600 + 3600)
    kinds = {k for _key, k, cid, _d in EV.watchdog_checks(w.cfg, now=now) if cid == CID}
    assert "run_overdue" in kinds and "campaign_silent" not in kinds
    _adopt(w, ctx, req)                                         # let the job finish


# ---- nw workers set / check --auth -----------------------------------------------------------

def test_workers_set(tmp_path, monkeypatch):
    from nightwright.cli import cli
    w = _world(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: w.cfg)
    _remote(w, _write_script(tmp_path / "s.json", WRITER), kinds=["gate"])
    run = CliRunner().invoke(cli, ["workers", "set", "box", "--kinds", "gate,persona",
                                   "--persona-slots", "3"])
    assert run.exit_code == 0, run.output
    rec = WK.load(w.cfg)[0]
    assert rec["kinds"] == ["gate", "persona"] and RPS.persona_slots(rec) == 3
    bad = CliRunner().invoke(cli, ["workers", "set", "box", "--kinds", "bogus"])
    assert bad.exit_code != 0


@pytest.mark.parametrize("working", [True, False])
def test_workers_check_auth_with_fake_claude(tmp_path, working, monkeypatch):
    from nightwright.cli import cli
    w = _world(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: w.cfg)
    r = _remote(w, _write_script(tmp_path / "s.json", WRITER))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if working:
        _script(bindir / "claude", 'case "$1" in --version) echo "2.0.0 (Claude Code)";; '
                                   '*) echo \'{"type":"result","is_error":false,"result":"ok"}\';; esac')
    else:
        _script(bindir / "claude", "echo 'not logged in' >&2; exit 1")
    ssh = _script(tmp_path / "fake-ssh-claude", f'export PATH={bindir}:$PATH\nexec {r.ssh} "$@"')
    r.register(ssh=ssh, info=INFO, kinds=["gate", "persona"])
    monkeypatch.setattr(WK, "check", lambda cfg, rec: {"name": rec["name"], "ok": True,
                                                       "errors": [], "warnings": [],
                                                       "info": INFO})
    run = CliRunner().invoke(cli, ["workers", "check", "box", "--auth", "--json"])
    out = json.loads(run.output)
    assert out[0]["ok"] is working and (run.exit_code == 0) is working
    if not working:
        assert "claude" in out[0]["errors"][0]


def test_ssh_grammar_allows_claude_check():
    assert W.check_ssh_command("nw worker claude-check --auth") == ["claude-check", "--auth"]
    with pytest.raises(ValueError):
        W.check_ssh_command("nw worker claude-check --other")
