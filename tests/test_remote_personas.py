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
    # the worker's own request.json came back: its cwd is the worker's checkout
    assert ra["cwd"] != str(remote_w.repo) and Path(ra["cwd"]).name == "tree"
    assert la["cwd"] == str(local_w.repo)
    assert _norm_request(ra, None) == _norm_request(la, None)
    prog = json.loads((rd / "progress.json").read_text())
    assert set(prog) == {"ts", "transcript_bytes", "last_line_ts"}
    assert _git(remote_w.repo, "status", "--porcelain") == "M a.txt"   # untouched


# ---- write-back failures ---------------------------------------------------------------------

LOCAL_SNAP = [{"pre_shell": ["git rev-parse HEAD > ../snap-head",
                             "git status --porcelain > ../snap-status"],
               "verdict": VERDICT, "cost_usd": 0.0}]


def _failure_case(tmp_path, monkeypatch, hook, worker_entries=WRITER, clean_stub=False,
                  local_entries=LOCAL_SNAP):
    script = _write_script(tmp_path / "writer.json", worker_entries)
    local = _write_script(tmp_path / "local.json", local_entries)
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
    return w, base, res, st, ctx


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
    w, base, res, st, _ctx_ = _failure_case(tmp_path, monkeypatch, hook)
    _check_failure(w, res, st, "head moved")
    assert _local_saw(w) == (moved["sha"], "")                  # untouched


def test_write_back_failure_dirty_worktree(tmp_path, monkeypatch):
    w, base, res, st, _ctx_ = _failure_case(
        tmp_path, monkeypatch, lambda w, b: (w.repo / "late.txt").write_text("late\n"))
    _check_failure(w, res, st, "worktree dirty")
    assert _local_saw(w) == (base, "?? late.txt")


def test_write_back_failure_stash_conflict_rolls_back(tmp_path, monkeypatch):
    w, base, res, st, _ctx_ = _failure_case(
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


def test_rollback_cleans_non_ascii_and_colon_names(tmp_path):
    """`_rollback` after a partly applied `wip`: its added paths are removed even when a name
    is non-ASCII (C-quoted without `-z`) or starts `:` / holds `*` (pathspec magic)."""
    w = _world(tmp_path)
    repo = w.repo
    base = _git(repo, "rev-parse", "HEAD")
    head = __import__("test_remote_gates")._commit(repo, "c.txt", "c\n")
    odd = ["ünïcødé.txt", ":colon.txt", ":(glob)*.txt", "sub/:x.txt"]
    for n in odd:
        (repo / n).parent.mkdir(parents=True, exist_ok=True)
        (repo / n).write_text("odd\n")
    _git(repo, "add", "-A")
    wip = _git(repo, "stash", "create")
    _git(repo, "reset", "-q")                       # the stash's new files are in the tree
    (repo / "keep.txt").write_text("not the stash's\n")
    RPS._rollback(repo, base, head, wip, "")
    assert _git(repo, "rev-parse", "HEAD") == base
    assert _git(repo, "-c", "core.quotepath=false", "status", "--porcelain") == "?? keep.txt"
    assert not any((repo / n).exists() for n in odd)


def test_write_back_failure_corrupt_bundle(tmp_path, monkeypatch):
    import base64

    def hook(w, bundle):
        bundle["bundle_b64"] = base64.b64encode(b"not a bundle").decode()
    w, base, res, st, _ctx_ = _failure_case(tmp_path, monkeypatch, hook)
    _check_failure(w, res, st, "corrupt bundle")
    assert _local_saw(w) == (base, "")


# ---- restart / adoption ----------------------------------------------------------------------

def _detached_job(tmp_path, sleep_s=1.5):
    entries = [dict(WRITER[0], sleep_s=sleep_s)]
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
                                  "conflict_resolver", "rebase", "sequencer",
                                  "excluded"])
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
    elif case == "sequencer":
        (ew.repo / ".git" / "sequencer").mkdir()
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


# ---- adoption through the queue (queue._adopt) -----------------------------------------------

def _queue_adopt(w, local):
    """A new coordinator (non-detachable `FakeRunner`) runs its first-tick adoption."""
    ctx = _ctx(w, local)
    pool, report = Q.StepPool(), Q.TickReport()
    Q._adopt(w.cfg, ctx, pool, report)
    return ctx, pool, report


def test_queue_adopt_reattaches_a_live_persona_job_under_a_local_runner(tmp_path, monkeypatch):
    w, _old, req, job = _detached_job(tmp_path)
    seen = []
    real = Q._do_persona

    def spy(*a):
        seen.append(a[-1])                                      # `adopted`
        return real(*a)
    monkeypatch.setattr(Q, "_do_persona", spy)
    base = _git(w.repo, "rev-parse", "HEAD")
    ctx, pool, report = _queue_adopt(w, _write_script(tmp_path / "local2.json", []))
    try:
        assert [r["action"] for r in report.started] == ["adopt"]
        assert report.started[0]["run_ids"] == ["001-implementer"]
        step, fut, _hooks = pool.flights[CID].tasks[0]
        res = fut.result(timeout=60)
    finally:
        pool.shutdown()
    assert res.run_result.worker == "box" and res.commit_sha
    assert seen == [{"exited_before_start": False}]             # committed as adopted
    log = _git(w.repo, "log", "--format=%s", f"{base}..HEAD").splitlines()
    assert log == [res.commit_message.splitlines()[0], "add c"]  # written back once
    assert RJ.read(w.cfg, job) is None
    snap = pool.flights[CID].state
    Q._apply(ctx, snap, res)
    end, = w.ledger(["run.end"])
    assert end["run_id"] == "001-implementer" and end["worker"] == "box"


def test_queue_adopt_skips_open_runs_without_a_live_persona_record(tmp_path):
    w = _world(tmp_path)
    _remote(w, _write_script(tmp_path / "s.json", WRITER))
    _start(w, _req(w))                                          # an open run, no job record
    local = _write_script(tmp_path / "local.json", [])
    ctx, pool, report = _queue_adopt(w, local)                  # no live record at all
    assert not pool.flights and not report.started
    RJ.create(w.cfg, job_id=RPS.job_id_for(CID, "009-implementer"), campaign=CID, worker="box",
              attempt=1, seq=0, submitted_at=0.0, kind=RPS.KIND, commit="a" * 40,
              head="a" * 40, deadline_at=9e9)
    RJ.update(w.cfg, RPS.job_id_for(CID, "009-implementer"), run_id="009-implementer")
    ctx, pool, report = _queue_adopt(w, local)                  # a live record, another run
    assert not pool.flights and not report.started
    assert not w.ledger(["run.end"])                            # nor closed as stale


# ---- remote_excluded clears when the step completes ------------------------------------------

def test_remote_excluded_is_cleared_when_the_step_completes(tmp_path, monkeypatch):
    w, base, res, st, ctx = _failure_case(
        tmp_path, monkeypatch, lambda w, b: (w.repo / "late.txt").write_text("late\n"))
    assert S.read_state(w.cfg, CID)["remote_excluded"] == ["box"]
    Q._apply(ctx, st, res)
    assert "remote_excluded" not in S.read_state(w.cfg, CID)


def test_failed_write_back_cost_is_counted_once_when_the_rerun_completes(tmp_path, monkeypatch):
    w, base, res, st, ctx = _failure_case(
        tmp_path, monkeypatch, lambda w, b: (w.repo / "late.txt").write_text("late\n"),
        local_entries=[dict(LOCAL_SNAP[0], cost_usd=0.25)])
    failed, = w.ledger(["run.end"])
    assert S.read_state(w.cfg, CID)["cost"]["total_usd"] == failed["charged_usd"] == 0.5
    Q._apply(ctx, st, res)
    ends = w.ledger(["run.end"])
    assert [e["charged_usd"] for e in ends] == [0.5, 0.25]
    assert S.read_state(w.cfg, CID)["cost"]["total_usd"] == pytest.approx(0.75)
    assert [e["total_usd"] for e in w.ledger(["cost"])][-1] == pytest.approx(0.75)


# ---- lost job, pre-submit fallback, cancel ---------------------------------------------------

LOCAL_OK = [{"verdict": VERDICT, "cost_usd": 0.0}]


def _rr(w, req):
    rr = RPS.dispatch(w.cfg, S.read_state(w.cfg, CID), "implement", req,
                      local=R.FakeRunner(_write_script(w.tmp / "local-ok.json", LOCAL_OK)))
    assert isinstance(rr, R.RemoteRunner)
    return rr


def test_lost_job_is_cancelled_counted_and_runs_locally(tmp_path, monkeypatch):
    from nightwright.agentic import remote_gates as RG
    # the worker's FakeRunner raises (persona mismatch): the job ends in `error`
    w = _world(tmp_path)
    _remote(w, _write_script(tmp_path / "s.json", [dict(WRITER[0], persona="reviewer")]))
    cancels = []
    real = RG._cancel
    monkeypatch.setattr(RG, "_cancel", lambda wk, jid: (cancels.append(jid), real(wk, jid)))
    req = _req(w)
    got = _rr(w, req).run(req)
    job = RPS.job_id_for(CID, "001-implementer")
    assert got.worker is None and got.verdict == VERDICT        # the local runner's run
    rec = RJ.read(w.cfg, job)
    assert rec["state"] == "lost" and rec["lost_reason"].startswith("error")
    assert cancels == [job]
    losses = json.loads(WK.losses_path(w.cfg).read_text())["workers"]["box"]
    assert len(losses["losses"]) == 1 and losses["recent"][0]["job_id"] == job
    assert not (w.repo / "c.txt").exists()                      # nothing written back


def test_submit_failure_runs_locally_at_once_with_cooldown(tmp_path, monkeypatch):
    from nightwright.agentic import remote_gates as RG
    w = _world(tmp_path)
    _remote(w, _write_script(tmp_path / "s.json", WRITER))

    def boom(*a, **k):
        raise RG.RemoteFailure("upload", "link dropped")
    monkeypatch.setattr(RG, "_submit", boom)
    req = _req(w)
    got = _rr(w, req).run(req)
    assert got.worker is None and got.verdict == VERDICT
    assert RJ.read(w.cfg, RPS.job_id_for(CID, "001-implementer"))["state"] == "local"
    failed, = w.ledger(["worker_failed"])
    assert failed["worker"] == "box" and failed["stage"] == "upload"
    assert WK.in_cooldown(WK.load(w.cfg)[0])


def test_cut_failure_runs_locally_without_a_record(tmp_path, monkeypatch):
    from nightwright.agentic import remote_gates as RG
    w = _world(tmp_path)
    _remote(w, _write_script(tmp_path / "s.json", WRITER))

    def boom(*a, **k):
        raise RG.RemoteFailure("bundle", "git bundle failed", cooldown=False)
    monkeypatch.setattr(RG, "cut", boom)
    req = _req(w)
    got = _rr(w, req).run(req)
    assert got.worker is None and got.verdict == VERDICT
    assert RJ.read(w.cfg, RPS.job_id_for(CID, "001-implementer")) is None
    assert not WK.in_cooldown(WK.load(w.cfg)[0])                # not the worker's fault


def test_cancelled_job_ends_as_an_interrupted_run(tmp_path):
    from nightwright.agentic import remote_gates as RG
    w = _world(tmp_path)
    _remote(w, _write_script(tmp_path / "s.json", [dict(WRITER[0], sleep_s=5)]))
    req = _req(w)
    rr = _rr(w, req)
    out = {}
    t = threading.Thread(target=lambda: out.update(res=rr.run(req)))
    t.start()
    job = RPS.job_id_for(CID, "001-implementer")
    until(lambda: (RJ.read(w.cfg, job) or {}).get("accepted_at"))
    assert RG.cancel_campaign(w.cfg, CID) == [job]
    join(t)
    got = out["res"]
    assert got.interrupted and got.is_error and got.worker is None
    assert _git(w.repo, "status", "--porcelain") == "" and not (w.repo / "c.txt").exists()


# ---- the persona slot outlives a detach ------------------------------------------------------

def test_detached_and_adopted_jobs_keep_the_worker_slot(tmp_path):
    w, ctx, req, job = _detached_job(tmp_path, sleep_s=4)
    other = _req(w, "005-implementer")
    assert _dispatch(w, other) is None          # the detached job counts: persona_slots is 1
    out = {}
    t = threading.Thread(target=lambda: out.update(res=_adopt(w, ctx, req)))
    t.start()
    worker = WK.load(w.cfg)[0]
    until(lambda: RPS._held(RPS.job_lock_path(w.cfg, worker, job)))
    assert _dispatch(w, other) is None          # the adopted runner holds the slot again
    join(t)
    assert out["res"].run_result.worker == "box"
    assert isinstance(_dispatch(w, other), R.RemoteRunner)       # free once the job ended
    assert not RPS.job_lock_path(w.cfg, worker, job).exists()


def test_slot_count_includes_jobs_adopted_without_a_slot(ew):
    rec_id = RPS.job_id_for(CID, "007-implementer")
    RJ.create(ew.cfg, job_id=rec_id, campaign=CID, worker="box", attempt=1, seq=0,
              submitted_at=0.0, kind=RPS.KIND, commit="a" * 40, head="a" * 40,
              deadline_at=9e9)
    assert _dispatch(ew) is None                # a live record nobody holds a slot for
    assert RJ.campaigns(ew.cfg) == {CID}         # an ended campaign's persona jobs get settled
    RJ.update(ew.cfg, rec_id, state="done")
    assert isinstance(_dispatch(ew), R.RemoteRunner)


# ---- run_job's env, end to end ---------------------------------------------------------------

def test_worker_re_sets_the_stripped_env(tmp_path):
    dump = [{"pre_shell": ["env | grep -E '^(NW_CAMPAIGN|NW_SPEC_ID|NW_BASE|NW_PERSONA_RUN|"
                           "NW_WORKTREE|NW_DATA_DIR|TMPDIR)=' | sort > env.txt"],
             "verdict": VERDICT, "cost_usd": 0.0}]
    w = _world(tmp_path)
    r = _remote(w, _write_script(tmp_path / "s.json", dump))
    req = _req(w)
    got = _rr(w, req).run(req)
    assert got.worker == "box" and not got.writeback_failed
    env = dict(ln.split("=", 1) for ln in (w.repo / "env.txt").read_text().splitlines())
    assert env["NW_CAMPAIGN"] == CID and env["NW_SPEC_ID"] == "SPEC-0900"
    assert env["NW_BASE"] == "main" and env["NW_PERSONA_RUN"] == "1"
    tree = Path(env["NW_WORKTREE"])
    assert tree.name == "tree" and tree != w.repo                # the worker's checkout
    assert Path(env["NW_DATA_DIR"]) == r.wdata                   # the worker's data dir
    assert env["TMPDIR"] != req.env["TMPDIR"] and Path(env["TMPDIR"]).parent == tree.parent


# ---- saved refs are pruned when the campaign closes ------------------------------------------

def test_saved_refs_are_pruned_when_the_campaign_is_abandoned(tmp_path, monkeypatch):
    w, base, res, st, ctx = _failure_case(
        tmp_path, monkeypatch, lambda w, b: (w.repo / "a.txt").write_text("human edit\n"),
        clean_stub=True)
    job = RPS.job_id_for(CID, "001-implementer")
    assert RPS._refs_of(w.repo, job)
    _git(w.repo, "update-ref", "refs/nw/jobs/OTHER-persona-001-implementer-a1/head", base)
    cur = S.read_state(w.cfg, CID)
    cur["state"] = "PARKED"
    cur["repo"]["root"] = str(w.repo)
    S.write_state(w.cfg, CID, cur)
    Q.apply_abandon(w.cfg, Q.plan_abandon(w.cfg, CID, keep_worktree=True))
    assert not RPS._refs_of(w.repo, job)
    _git(w.repo, "rev-parse", "--verify", "refs/nw/jobs/OTHER-persona-001-implementer-a1/head")


def test_close_prunes_the_campaign_saved_refs(tmp_path):
    from nightwright.agentic import close as CL
    w = _world(tmp_path)
    head = _git(w.repo, "rev-parse", "HEAD")
    mine = f"refs/nw/jobs/{RPS.job_id_for(CID, '001-implementer')}/head"
    theirs = "refs/nw/jobs/OTHER-persona-001-implementer-a1/head"
    for ref in (mine, theirs):
        _git(w.repo, "update-ref", ref, head)
    CL._prune_job_refs(w.repo, {"id": CID})
    assert _git(w.repo, "for-each-ref", "--format=%(refname)", "refs/nw/jobs/") == theirs


# ---- a detachable local runner on the fallback paths -----------------------------------------

class _DetachableLocal:
    """A stand-in for `ClaudeRunner`: like it, it attaches (and spawns nothing) when the hooks
    carry an adoption mode; otherwise it spawns, records it, and either finishes or is
    detached by the pool."""
    detachable = True

    def __init__(self, detach=False):
        self.calls, self.detach = [], detach

    def run(self, req, *, hooks=None):
        self.calls.append({"phase": hooks.phase, "adopt": hooks.adopt,
                           "run_dir": hooks.run_dir, "req_run_dir": req.run_dir})
        if hooks.adopt:                                 # ClaudeRunner: wait for a wrapper
            return R._synthetic(req, "orphaned: no wrapper claimed the run")
        self.calls[-1]["spawned"] = True
        req.run_dir.mkdir(parents=True, exist_ok=True)
        hooks.spawned()
        if self.detach:
            hooks.detach.set()
            return R.DETACHED
        assert hooks.enter_collect()
        return R.write_run_record(req, R.RunResult(
            exit_code=0, timed_out=False, interrupted=False, duration_s=0.0, is_error=False,
            cost_usd=0.0, verdict=VERDICT, verdict_ok=True), S.now_iso())


def test_lost_adopted_job_spawns_locally_with_a_detachable_runner(tmp_path):
    w = _world(tmp_path)
    _remote(w, _write_script(tmp_path / "s.json", [dict(WRITER[0], persona="reviewer")]))
    req = _req(w)
    local = _DetachableLocal()
    rr = RPS.dispatch(w.cfg, S.read_state(w.cfg, CID), "implement", req, local=local)
    hooks = R.RunHooks(threading.Lock(), req.run_dir, phase="wait")
    hooks.adopt = ("claim", 0.0)                        # as `queue._adopt` sets it: no proc.json
    got = rr.run(req, hooks=hooks)
    assert RJ.read(w.cfg, RPS.job_id_for(CID, "001-implementer"))["state"] == "lost"
    call, = local.calls
    assert call["spawned"] and call["phase"] == "spawning" and call["adopt"] is None
    assert call["run_dir"] == req.run_dir
    assert not got.interrupted and got.verdict == VERDICT


def _rerun_case(tmp_path, monkeypatch, local):
    w = _world(tmp_path)
    _remote(w, _write_script(tmp_path / "writer.json", WRITER))
    monkeypatch.setattr(RPS, "_before_writeback",
                        lambda wt, b: (w.repo / "late.txt").write_text("late\n"))
    req = _req(w)
    _start(w, req)
    ctx = Q._Ctx(w.cfg, runner=local)
    ctx._projects[w.state["project"]] = w.project
    hooks = R.RunHooks(threading.Lock(), req.run_dir)
    res = Q._do_persona(ctx, S.read_state(w.cfg, CID), Q.Step("implement", persona="implementer"),
                        "001-implementer", req, 1.0, hooks)
    return w, res, hooks


def test_rerun_after_failed_write_back_gets_the_hooks(tmp_path, monkeypatch):
    local = _DetachableLocal()
    w, res, hooks = _rerun_case(tmp_path, monkeypatch, local)
    call, = local.calls
    new_dir = S.runs_dir(w.cfg, CID) / "002-implementer"
    assert call["spawned"] and call["phase"] == "spawning" and call["run_dir"] == new_dir
    assert call["req_run_dir"] == new_dir
    assert res.run_id == "002-implementer" and res.run_result.verdict == VERDICT
    assert not res.detached and not res.interrupted and hooks.phase == "done"


def test_rerun_after_failed_write_back_detaches_on_a_stop(tmp_path, monkeypatch):
    local = _DetachableLocal(detach=True)
    w, res, hooks = _rerun_case(tmp_path, monkeypatch, local)
    assert len(local.calls) == 1
    assert res.detached and res.run_id == "002-implementer"
    assert hooks.run_dir.name == "002-implementer"      # the pool sees the new run
    assert [e["run_id"] for e in w.ledger(["run.end"])] == ["001-implementer"]
    assert [e["run_id"] for e in w.ledger(["run.start"])] == ["001-implementer",
                                                              "002-implementer"]
    # the failed remote run's charge is in state.json, though the re-run is never applied
    failed, = w.ledger(["run.end"])
    cost = S.read_state(w.cfg, CID)["cost"]
    assert failed["charged_usd"] == 0.5
    assert cost["total_usd"] == 0.5 and cost["by_persona"]["implementer"] == 0.5


def test_applied_record_of_a_lost_job_is_collected_not_rerun(tmp_path, monkeypatch):
    w, ctx, req, job = _detached_job(tmp_path)
    base = _git(w.repo, "rev-parse", "HEAD")
    RJ.update(w.cfg, job, writeback="applied")       # written back, then the runner stopped
    (req.run_dir / "result.json").write_text(json.dumps(
        {"type": "result", "structured_output": VERDICT, "is_error": False,
         "total_cost_usd": 0.5}))
    monkeypatch.setattr(RPS, "_attach", lambda *a: ("lost", "unknown"))
    res = _adopt(w, ctx, req)                          # the local script is empty: never run
    assert res.run_result.worker == "box" and res.run_result.verdict == VERDICT
    assert not res.run_result.is_error and res.run_result.cost_usd == 0.5
    assert _git(w.repo, "rev-parse", "HEAD") == base
    assert RJ.read(w.cfg, job) is None and not WK.losses_path(w.cfg).exists()


def test_reattach_loop_honours_the_deadline(tmp_path, monkeypatch):
    w = _world(tmp_path)
    _remote(w, _write_script(tmp_path / "s.json", WRITER))
    req = _req(w)
    rr = _rr(w, req)
    monkeypatch.setattr(RPS, "_attach", lambda *a: ("dropped", None))
    polls = []
    from nightwright.agentic import remote_gates as RG
    monkeypatch.setattr(RG, "poll", lambda *a: polls.append(a) or "running")
    monkeypatch.setattr(RPS, "BACKOFF_START_S", 0.01)
    monkeypatch.setattr(RPS, "_fresh", lambda rr_, req_, hooks: RPS._follow(rr_, req_, hooks, {
        "job_id": "j1", "deadline_at": RJ.iso(1000.0), "cursor": 0}))
    seen = []
    monkeypatch.setattr(RPS, "_lost", lambda rr_, req_, hooks, rec, why: seen.append(why) or why)
    assert rr.run(req) == "deadline" and seen == ["deadline"] and not polls


def test_allowed_tools_sibling_of_the_worktree_is_not_remotable(tmp_path):
    w = _world(tmp_path)
    req = _req(w, allowed_tools=(f"Bash({w.repo}-other/run.sh*)",))
    with pytest.raises(R.NotRemotable, match="-other"):
        R.request_to_job(req, w.repo)
    for tool in (f"Bash({w.repo}/../x*)", f"Read({w.repo}/a/../../x)", f"Bash({w.repo}/..:*)"):
        with pytest.raises(R.NotRemotable, match=r"\.\."):
            R.request_to_job(_req(w, allowed_tools=(tool,)), w.repo)
    dots = _req(w, allowed_tools=(f"Read({w.repo}/a..b/c.txt)",))      # not a `..` component
    assert R.request_to_job(dots, w.repo)["allowed_tools"] == ["Read(${NW_TREE}/a..b/c.txt)"]
    ok = _req(w, allowed_tools=(f"Read({w.repo})", f"Bash({w.repo}:*)"))
    assert R.request_to_job(ok, w.repo)["allowed_tools"] == ["Read(${NW_TREE})",
                                                             "Bash(${NW_TREE}:*)"]


def test_decode_is_bounded(monkeypatch):
    import base64
    import gzip
    data = base64.b64encode(gzip.compress(b"x" * 100)).decode()
    assert RPS._decode(data) == b"x" * 100
    monkeypatch.setattr(RPS, "MAX_RUN_FILE_BYTES", 99)
    with pytest.raises(ValueError):
        RPS._decode(data)
