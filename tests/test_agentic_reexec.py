"""SPEC-0194: the runner follows merged code (re-exec between ticks, lease handed over) and a
bad project config parks as `config_invalid`, then recovers by itself."""
import json
import os
import subprocess
import sys
import textwrap

from test_agentic_e2e import (_git, _write, added, impl_done, make_cfg, make_repo, review,
                              run_cli, script)
from wt.agentic import lease as L
from wt.agentic import queue as Q
from wt.agentic import store as S


def _code_repo(tmp_path):
    root = tmp_path / "wtcode"
    (root / "src" / "wt").mkdir(parents=True)
    _git(root.parent, "init", "-q", "-b", "main", str(root))
    _write(root, "src/wt/x.py", "A = 1\n")
    _write(root, "docs/notes.md", "n\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _commit(root, rel, text, msg):
    _write(root, rel, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


def test_code_changes_ignores_docs_and_reports_code(tmp_path):
    root = _code_repo(tmp_path)
    start = _git(root, "rev-parse", "HEAD")
    assert Q.code_changes(root, start) == (start, [])
    docs = _commit(root, "docs/notes.md", "m\n", "docs")
    assert Q.code_changes(root, start) == (docs, [])
    code = _commit(root, "src/wt/x.py", "A = 2\n", "code")
    assert Q.code_changes(root, start) == (code, ["src/wt/x.py"])


def _loop_with_commits(tmp_path, monkeypatch, commits):
    """Run the loop with re-exec on a scratch code repo; `commits[i]` (rel, text) lands right
    after tick i+1. Returns (cfg, root, exec_calls)."""
    root = _code_repo(tmp_path)
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    real_tick = Q.tick
    n = {"ticks": 0}

    def tick(*a, **k):
        report = real_tick(*a, **k)
        if n["ticks"] < len(commits):
            rel, text = commits[n["ticks"]]
            _commit(root, rel, text, f"c{n['ticks']}")
        n["ticks"] += 1
        return report

    calls = []
    monkeypatch.setattr(Q, "tick", tick)
    monkeypatch.setattr(Q, "_EXEC", lambda exe, argv, env: calls.append((exe, argv, env)))
    Q.run(cfg, max_ticks=len(commits) + 2, poll_interval_s=0, reexec=True, code_root=root)
    return cfg, root, calls


def test_loop_reexecs_on_code_change_and_keeps_the_lease(tmp_path, monkeypatch):
    cfg, root, calls = _loop_with_commits(tmp_path, monkeypatch,
                                          [("docs/notes.md", "x\n"), ("src/wt/x.py", "A = 3\n")])
    assert len(calls) == 1
    exe, argv, env = calls[0]
    assert exe == sys.executable and argv[0] == sys.executable
    assert L.HANDOVER_ENV in env
    stop = Q.read_journal(cfg)[-1]
    assert stop["kind"] == "stop" and stop["reason"] == "code_changed" and stop["ticks"] == 2
    assert stop["paths"] == ["src/wt/x.py"] and stop["to_sha"] == _git(root, "rev-parse", "HEAD")
    assert L.is_held(cfg)                       # handed over, not released
    os.environ[L.HANDOVER_ENV] = env[L.HANDOVER_ENV]
    lease = L.acquire(cfg, ["wt", "queue", "run"])   # the re-executed image adopts it
    try:
        assert L.read_lease(cfg)["pid"] == os.getpid() and "restarted_at" in L.read_lease(cfg)
        assert L.HANDOVER_ENV not in os.environ
    finally:
        lease.release()
    assert not L.is_held(cfg)


def test_docs_only_commits_do_not_restart(tmp_path, monkeypatch):
    cfg, root, calls = _loop_with_commits(tmp_path, monkeypatch, [("docs/notes.md", "y\n")])
    assert calls == []
    assert Q.read_journal(cfg)[-1]["reason"] == "max_ticks"
    assert not L.is_held(cfg)



def test_diff_failure_counts_as_code_change(tmp_path, monkeypatch):
    """SPEC-0262: HEAD moved and `git diff` failed: the runner drains and re-execs (paths
    unknown) instead of moving `code_sha` past the merge and running stale code."""
    from wt.agentic import codewatch as CW
    monkeypatch.setattr(CW, "_changed_paths", lambda *a: None)
    cfg, root, calls = _loop_with_commits(tmp_path, monkeypatch, [("src/wt/x.py", "A = 4\n")])
    head = _git(root, "rev-parse", "HEAD")
    draining, = [r for r in Q.read_journal(cfg) if r["kind"] == "draining"]
    assert draining["diff_failed"] is True and draining["to_sha"] == head
    assert draining["paths"] == []
    assert len(calls) == 1
    stop = Q.read_journal(cfg)[-1]
    assert stop["reason"] == "code_changed" and stop["to_sha"] == head
    assert stop["diff_failed"] is True

def test_reexec_off_by_default(tmp_path, monkeypatch):
    root = _code_repo(tmp_path)
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    calls = []
    monkeypatch.setattr(Q, "_EXEC", lambda *a: calls.append(a))
    real_tick = Q.tick
    n = iter(range(100))
    monkeypatch.setattr(Q, "tick", lambda *a, **k: (
        _commit(root, "src/wt/x.py", f"B = {next(n)}\n", "c"), real_tick(*a, **k))[1])
    Q.run(cfg, max_ticks=2, poll_interval_s=0, code_root=root)
    assert calls == [] and Q.read_journal(cfg)[0].get("code_sha") is None


def test_real_execve_adopts_the_lease(tmp_path):
    """A real exec: the first image takes the lease and hands it over; the second adopts it
    while a third process sees the lease held throughout and cannot take it."""
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({k: v for k, v in cfg.items() if not k.startswith("_")}))
    prog = tmp_path / "prog.py"
    prog.write_text(textwrap.dedent(f"""
        import json, os, sys
        from wt.agentic import lease as L
        cfg = json.load(open({str(cfg_path)!r}))
        if sys.argv[1] == "first":
            lease = L.acquire(cfg, ["first"])
            env = {{**os.environ, **lease.handover()}}
            os.execve(sys.executable, [sys.executable, sys.argv[0], "second"], env)
        lease = L.acquire(cfg, ["second"])
        try:
            L.acquire(cfg, ["intruder"])
            print("INTRUDER GOT IT")
        except ValueError:
            pass
        print(json.dumps({{"pid": os.getpid(), "lease_pid": L.read_lease(cfg)["pid"],
                          "argv": L.read_lease(cfg)["argv"]}}))
        lease.release()
    """))
    r = subprocess.run([sys.executable, str(prog), "first"], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert "INTRUDER" not in r.stdout
    assert out["pid"] == out["lease_pid"] and out["argv"] == ["second"]
    assert not L.is_held(cfg)


# ---- config_invalid -----------------------------------------------------------------------------

def _break_config(repo):
    p = repo / ".wt" / "project.yaml"
    good = p.read_text()
    p.write_text(good + "bogus_key: 1\n")
    return lambda: p.write_text(good)


def test_bad_config_at_open_parks_config_invalid_then_recovers(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), review()]))
    cid = added(cfg, monkeypatch)
    fix = _break_config(repo)
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "config_invalid"
    assert st["park"]["resume_to"] == "QUEUED" and "bogus_key" in st["park"]["detail"]
    Q.tick(cfg)                                          # still broken: stays parked
    assert S.read_state(cfg, cid)["state"] == "PARKED"
    fix()
    report = Q.tick(cfg)
    assert any(r["reason"] == "config_recovered" for r in report.steps)
    st = S.read_state(cfg, cid)
    assert st["park"] is None and st["state"] in ("QUEUED", "OPENING", "IMPLEMENTING")
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", (st.get("park"), r.output)


def test_bad_config_mid_campaign_resumes_where_it_was(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    cid = added(cfg, monkeypatch)
    Q.tick(cfg)                                          # admit + open → IMPLEMENTING
    assert S.read_state(cfg, cid)["state"] == "IMPLEMENTING"
    fix = _break_config(repo)
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    assert st["park"]["reason"] == "config_invalid" and st["park"]["resume_to"] == "IMPLEMENTING"
    fix()
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    assert st["park"] is None and st["state"] != "PARKED"
    reasons = [e["reason"] for e in S.read_ledger(cfg, cid, types=["transition"])]
    assert "config_recovered" in reasons


def test_recovery_waits_for_a_free_slot(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    cid = added(cfg, monkeypatch)
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="PARKED", park={
        "reason": "config_invalid", "detail": "x", "resume_to": "IMPLEMENTING"}))
    monkeypatch.setattr(Q, "slots_for", lambda cfg, profile_name=None: 0)
    Q.tick(cfg)
    st = S.read_state(cfg, cid)                          # no slot: started work WAITs (SPEC-0200)
    assert st["state"] == "WAITING" and st["park"] is None
    assert st["waiting"]["resume_to"] == "IMPLEMENTING"
