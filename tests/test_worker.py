"""SPEC-0336: `nw worker gate-job` and friends, driven as a subprocess with a header and bundle
the test builds itself (offline: the "coordinator" is a temp git repo)."""
import base64
import fcntl
import gzip
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

from nightwright.agentic import gate_slots as GS
from nightwright.agentic import worker as W

from waits import WAIT_S, until


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


class Src:
    """The coordinator's checkout: `main` at `base`, a feature commit on top."""

    def __init__(self, root):
        self.root = root
        root.mkdir()
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.email", "t@example.com")
        _git(root, "config", "user.name", "t")
        self.base = self.commit("a.txt", "a\n")
        _git(root, "checkout", "-q", "-b", "feature")
        self.head = self.commit("b.txt", "b\n")

    def commit(self, name, text):
        (self.root / name).write_text(text)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", name)
        return _git(self.root, "rev-parse", "HEAD")

    def bundle(self, job_id, commit, base, dest):
        refs = [f"refs/nw/gate-jobs/{job_id}", f"refs/nw/gate-jobs/{job_id}-base"]
        _git(self.root, "update-ref", refs[0], commit)
        _git(self.root, "update-ref", refs[1], base)
        try:
            _git(self.root, "bundle", "create", "-q", str(dest), *refs)
        finally:
            for r in refs:
                _git(self.root, "update-ref", "-d", r)
        return dest.read_bytes()


@pytest.fixture
def src(tmp_path):
    return Src(tmp_path / "src")


@pytest.fixture
def wenv(tmp_path):
    data, conf = tmp_path / "wdata", tmp_path / "wconf"
    data.mkdir()
    conf.mkdir()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("NW_") or k == "NW_TEST_WAIT_S"}
    env.update(NW_DATA_DIR=str(data), NW_CONFIG_DIR=str(conf))
    return env


def _cfg(env, slots=None):
    cfg = {"data_dir": env["NW_DATA_DIR"], "config_dir": env["NW_CONFIG_DIR"]}
    if slots:
        cfg["agentic"] = {"gates": {"slots": slots}}
    return cfg


def _header(job_id, commit, base_sha, gates, *, bundle_bytes, repo_key="proj", **over):
    h = {"schema": "nw.job.v1", "job_id": job_id, "kind": "gate", "campaign": "C-1",
         "spec_id": "SPEC-9999", "repo_key": repo_key, "commit": commit,
         "base": {"name": "main", "sha": base_sha}, "have": [], "bundle_bytes": bundle_bytes,
         "env": {"NW_CAMPAIGN": "C-1", "NW_SPEC_ID": "SPEC-9999", "NW_BASE": "main"},
         "timeout_s": 600, "attempt": 0,
         "payload": {"seq": 1, "tier": "loop", "gates": gates, "gate_budget_s": 600}}
    h.update(over)
    return h


def _gate(name, cmd, **kw):
    return {"name": name, "cmd": cmd, "timeout_s": 120, "required": True, **kw}


class Job:
    """One `nw worker gate-job` subprocess; stdin stays open until `close()`."""

    def __init__(self, env, header, bundle=b""):
        self.proc = subprocess.Popen([sys.executable, "-m", "nightwright", "worker", "gate-job"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env)
        self.frames = []
        self._q = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.proc.stdin.write((json.dumps(header) + "\n").encode() + bundle)
        self.proc.stdin.flush()

    def _read(self):
        for line in self.proc.stdout:
            self.frames.append(json.loads(line))

    def types(self):
        return [f["type"] for f in self.frames]

    def wait_for(self, type_, timeout=None):
        assert until(lambda: type_ in self.types(), timeout=timeout), \
            (self.types(), self.proc.poll(), self.proc.stderr.read() if self.proc.poll()
             is not None else "")
        return next(f for f in self.frames if f["type"] == type_)

    def final(self):
        assert until(lambda: {"done", "error"} & set(self.types())), self.types()
        return next(f for f in self.frames if f["type"] in ("done", "error"))

    def close(self):
        try:
            self.proc.stdin.close()
        except BrokenPipeError:
            pass

    def finish(self):
        """Wait for the final frame, then close stdin (a coordinator's normal ending)."""
        fin = self.final()
        self.close()
        rc = self.proc.wait(timeout=WAIT_S)
        return fin, rc


def _run(env, header, bundle=b""):
    job = Job(env, header, bundle)
    fin, rc = job.finish()
    return job, fin, rc


def _bundle_job(src, tmp_path, gates, job_id=None, **over):
    job_id = job_id or f"j-{uuid.uuid4().hex[:8]}"
    data = src.bundle(job_id, src.head, src.base, tmp_path / f"{job_id}.bundle")
    return _header(job_id, src.head, src.base, gates, bundle_bytes=len(data), **over), data


def _file(frame, name):
    return gzip.decompress(base64.b64decode(frame["files"][name]))


def _job_dir(env, job_id):
    from pathlib import Path
    return Path(env["NW_DATA_DIR"]) / "agentic" / "worker" / "jobs" / job_id


def _cache(env, key="proj"):
    from pathlib import Path
    return Path(env["NW_DATA_DIR"]) / "agentic" / "worker" / "repos" / f"{key}.git"


def _nw(env, *args):
    return subprocess.run([sys.executable, "-m", "nightwright", "worker", *args], env=env,
                          capture_output=True, text=True)


# ---- the job ---------------------------------------------------------------------------------

def test_pass_emits_frames_and_files(src, tmp_path, wenv):
    h, data = _bundle_job(src, tmp_path, [_gate("hello", "echo hi; echo wt=$NW_WORKTREE; "
                                                         "cat b.txt")])
    job, fin, rc = _run(wenv, h, data)
    assert job.types() == ["accepted", "started", "gate", "done"]
    assert rc == 0 and fin["ok"] is True and fin["interrupted"] is False
    assert all(f["schema"] == "nw.job.frame.v1" and f["job_id"] == h["job_id"]
               for f in job.frames)
    g = job.frames[2]
    assert g["result"]["ok"] is True and g["result"]["log"] == "gates/001-hello.log"
    assert set(g["files"]) == {"001-hello.log", "001-hello.json"}
    log = _file(g, "001-hello.log").decode()
    tree = _job_dir(wenv, h["job_id"]) / "tree"
    assert "hi" in log and f"wt={tree}" in log and "b" in log
    assert json.loads(_file(g, "001-hello.json"))["schema"] == "nw.gate.v1"
    jd = _job_dir(wenv, h["job_id"])
    assert (jd / "job.json").is_file() and (jd / "frames.jsonl").is_file()
    assert not (jd / "tree").exists()
    copied = [json.loads(ln)["type"] for ln in (jd / "frames.jsonl").read_text().splitlines()]
    assert copied == job.types()
    rec = json.loads((jd / "job.json").read_text())
    assert rec["attempt"] == 0 and rec["state"] == "done"
    # the job refs are gone from the cache; the have refs stay
    refs = _git(_cache(wenv), "for-each-ref", "--format=%(refname)")
    assert "gate-jobs" not in refs and f"refs/nw/have/{src.head}" in refs


def test_fail_is_done_with_ok_false_and_exit_0(src, tmp_path, wenv):
    h, data = _bundle_job(src, tmp_path, [_gate("bad", "echo nope; exit 3"),
                                          _gate("opt", "true", required=False)])
    job, fin, rc = _run(wenv, h, data)
    assert rc == 0 and fin["type"] == "done" and fin["ok"] is False
    gates = [f["result"] for f in job.frames if f["type"] == "gate"]
    assert [(g["name"], g["ok"], g["exit_code"]) for g in gates] == [("bad", False, 3),
                                                                     ("opt", True, 0)]


def test_retry_ladder_sends_r1_files(src, tmp_path, wenv):
    h, data = _bundle_job(src, tmp_path, [_gate(
        "flaky", 'if [ -f "$TMPDIR/once" ]; then echo second; else touch "$TMPDIR/once"; '
                 'echo first; exit 1; fi', retries=1)])
    job, fin, rc = _run(wenv, h, data)
    assert rc == 0 and fin["ok"] is True
    g = next(f for f in job.frames if f["type"] == "gate")
    assert set(g["files"]) == {"001-flaky.log", "001-flaky.json", "001-flaky.r1.log",
                               "001-flaky.r1.json"}
    assert len(g["result"]["attempts"]) == 2
    assert b"second" in _file(g, "001-flaky.r1.log")


def test_merge_base_and_base_placeholder_match_the_coordinator(src, tmp_path, wenv):
    local = _git(src.root, "merge-base", "main", "HEAD")
    h, data = _bundle_job(src, tmp_path, [_gate("mb", 'git merge-base "$NW_BASE" HEAD; '
                                                      'git rev-parse {base}; echo s={spec_id}')])
    job, fin, rc = _run(wenv, h, data)
    assert rc == 0 and fin["ok"] is True, job.frames
    log = _file(job.frames[2], "001-mb.log").decode().split()
    assert log == [local, src.base, "s=SPEC-9999"]


def test_concurrent_jobs_see_their_own_base(src, tmp_path, wenv):
    base1 = src.base
    _git(src.root, "checkout", "-q", "main")
    base2 = src.commit("c.txt", "c\n")
    _git(src.root, "checkout", "-q", "-b", "feature2")
    head2 = src.commit("d.txt", "d\n")
    sync = tmp_path / "sync"
    sync.mkdir()
    # both jobs must hold a slot at once (each gate waits for the other): pin two slots
    (Path(wenv["NW_CONFIG_DIR"]) / "config.yaml").write_text("agentic:\n  gates:\n    slots: 2\n")
    cmd = (f'git rev-parse "$NW_BASE"; touch "{sync}/$NW_CAMPAIGN"; '
           f'while [ "$(ls "{sync}" | wc -l)" -lt 2 ]; do sleep 0.1; done; '
           'git rev-parse "$NW_BASE"')
    jobs = []
    for i, (head, base) in enumerate(((src.head, base1), (head2, base2))):
        jid = f"cc-{i}"
        data = src.bundle(jid, head, base, tmp_path / f"{jid}.bundle")
        h = _header(jid, head, base, [_gate("rp", cmd, timeout_s=60)], bundle_bytes=len(data),
                    campaign=f"C-{i}", env={"NW_CAMPAIGN": f"C-{i}"})
        jobs.append((Job(wenv, h, data), base))
    for job, base in jobs:
        fin, rc = job.finish()
        assert rc == 0 and fin["ok"] is True, (job.frames, job.proc.stderr.read())
        log = _file(job.frames[2], "001-rp.log").decode().split()
        assert log == [base, base]


def test_env_outside_allowlist_is_refused(src, tmp_path, wenv):
    marker = tmp_path / "ran"
    h, data = _bundle_job(src, tmp_path, [_gate("x", f"touch {marker}")],
                          env={"NW_CAMPAIGN": "C-1", "LD_PRELOAD": "/evil.so"})
    job, fin, rc = _run(wenv, h, data)
    assert rc == 3 and fin["type"] == "error" and fin["stage"] == "validate"
    assert "LD_PRELOAD" in fin["error"] and job.types() == ["error"]
    assert not marker.exists() and not _job_dir(wenv, h["job_id"]).exists()


def test_bad_schema_and_nw_base_mismatch_are_refused(src, tmp_path, wenv):
    h, data = _bundle_job(src, tmp_path, [_gate("x", "true")], schema="nw.job.v2")
    _, fin, rc = _run(wenv, h, data)
    assert rc == 3 and fin["stage"] == "validate" and "nw.job.v2" in fin["error"]
    h, data = _bundle_job(src, tmp_path, [_gate("x", "true")], env={"NW_BASE": "develop"})
    _, fin, rc = _run(wenv, h, data)
    assert rc == 3 and fin["stage"] == "validate" and "NW_BASE" in fin["error"]


def _set(path, value):
    """A header mutator: set the dotted `path` (list indexes as ints) to `value`."""
    def mutate(h):
        keys = path.split(".")
        node = h
        for k in keys[:-1]:
            node = node[int(k)] if isinstance(node, list) else node[k]
        last = keys[-1]
        node[int(last) if isinstance(node, list) else last] = value
    return mutate


def _dup_gates(h):
    h["payload"]["gates"].append(dict(h["payload"]["gates"][0]))


@pytest.mark.parametrize("mutate, why", [
    (_set("job_id", "../x"), "job_id"),
    (_set("job_id", "a/b"), "job_id"),
    (_set("job_id", "x\n"), "job_id"),
    (_set("job_id", ""), "job_id"),
    (_set("repo_key", "../x"), "repo_key"),
    (_set("repo_key", "a/b"), "repo_key"),
    (_set("repo_key", "proj\n"), "repo_key"),
    (_set("campaign", "a/b"), "campaign"),
    (_set("spec_id", "../s"), "spec_id"),
    (_set("kind", "persona"), "kind"),
    (_set("commit", "zz"), "commit"),
    (_set("commit", "a" * 40 + "\n"), "commit"),
    (_set("base.sha", "not-a-sha"), "base"),
    (_set("base.name", "../main"), "base"),
    (_set("base.name", "-main"), "base"),
    (_set("payload.seq", 1000), "payload.seq"),
    (_set("payload.seq", "1"), "payload.seq"),
    (_set("payload.gates.0.name", "../../evil"), "name matching"),
    (_set("payload.gates.0.name", "a/b"), "name matching"),
    (_set("payload.gates.0.timeout_s", "abc"), "timeout_s"),
    (_set("payload.gates.0.cwd", 5), "cwd"),
    (_set("payload.gates.0.retries", 9), "retries"),
    (_set("payload.gates.0.results", {"format": "tap"}), "results"),
    (_set("payload.gates.0.surprise", 1), "unknown key"),
    (_dup_gates, "duplicate gate name"),
])
def test_bad_header_fields_are_refused_before_anything_runs(src, tmp_path, wenv, mutate, why):
    marker = tmp_path / "ran"
    h, data = _bundle_job(src, tmp_path, [_gate("x", f"touch {marker}"), _gate("y", "true")])
    mutate(h)
    job, fin, rc = _run(wenv, h, data)
    assert rc == 3 and fin["type"] == "error" and fin["stage"] == "validate", job.frames
    assert why in fin["error"]
    assert job.types() == ["error"] and not marker.exists()
    wd = Path(wenv["NW_DATA_DIR"]) / "agentic" / "worker"
    assert not (wd / "jobs").exists() or not any((wd / "jobs").iterdir())
    assert not (wd / "x").exists() and not (wd / "repos").exists()


def test_stdin_short_of_bundle_bytes_is_a_read_error(src, tmp_path, wenv):
    marker = tmp_path / "ran"
    h, data = _bundle_job(src, tmp_path, [_gate("x", f"touch {marker}")])
    h["bundle_bytes"] = len(data) + 100
    job = Job(wenv, h, data)
    job.close()
    fin = job.final()
    assert job.proc.wait(timeout=WAIT_S) == 3
    assert fin["type"] == "error" and fin["stage"] == "read" and "short" in fin["error"]
    assert job.types() == ["error"] and not marker.exists()
    jd = _job_dir(wenv, h["job_id"])
    assert sorted(p.name for p in jd.iterdir()) == ["frames.jsonl", "job.json", "job.lock"]
    rec = json.loads((jd / "job.json").read_text())
    assert rec["state"] == "error" and rec["stage"] == "read"
    # the job let go of its dir: gc removes it
    r = _nw(wenv, "gc", "--keep-days", "0")
    assert r.returncode == 0 and h["job_id"] in json.loads(r.stdout)["jobs_removed"]
    assert not jd.exists()


def test_the_workers_own_nw_env_never_reaches_a_gate(src, tmp_path, wenv):
    env = {**wenv, "NW_CAMPAIGN": "LEAK-C", "NW_SPEC_ID": "LEAK-S", "NW_BASE": "leak"}
    h, data = _bundle_job(src, tmp_path, [_gate("e", 'echo "$NW_CAMPAIGN $NW_SPEC_ID $NW_BASE"; '
                                                     'git rev-parse "$NW_BASE"')], env={})
    job, fin, rc = _run(env, h, data)
    assert rc == 0 and fin["ok"] is True, job.frames
    assert _file(job.frames[2], "001-e.log").decode().split() == ["C-1", "SPEC-9999", "main",
                                                                  src.base]
    # the header's env wins over its own defaults, still never the worker's
    h, data = _bundle_job(src, tmp_path, [_gate("e", 'echo "$NW_CAMPAIGN $NW_SPEC_ID"')],
                          env={"NW_CAMPAIGN": "HDR-C"})
    job, fin, rc = _run(env, h, data)
    assert rc == 0 and _file(job.frames[2], "001-e.log").decode().split() == ["HDR-C",
                                                                              "SPEC-9999"]


def test_duplicate_job_id_is_a_claim_error(src, tmp_path, wenv):
    h, data = _bundle_job(src, tmp_path, [_gate("x", "true")], job_id="dup")
    _, fin, rc = _run(wenv, h, data)
    assert rc == 0 and fin["ok"] is True
    before = (_job_dir(wenv, "dup") / "frames.jsonl").read_text()
    job, fin, rc = _run(wenv, h, data)
    assert rc == 3 and fin["type"] == "error" and fin["stage"] == "claim"
    assert job.types() == ["error"]
    assert (_job_dir(wenv, "dup") / "frames.jsonl").read_text() == before


def test_empty_bundle_against_a_warm_cache(src, tmp_path, wenv):
    h, data = _bundle_job(src, tmp_path, [_gate("x", "cat b.txt")])
    assert _run(wenv, h, data)[2] == 0
    h2 = _header("warm-2", src.head, src.base, [_gate("x", "cat b.txt")], bundle_bytes=0)
    job, fin, rc = _run(wenv, h2)
    assert rc == 0 and fin["ok"] is True
    # an empty bundle for a commit the cache lacks is a checkout error
    h3 = _header("cold-3", "f" * 40, src.base, [_gate("x", "true")], bundle_bytes=0)
    _, fin, rc = _run(wenv, h3)
    assert rc == 3 and fin["stage"] == "checkout"


def _wait_log(env, job_id, name, text):
    p = _job_dir(env, job_id) / "out" / f"001-{name}.log"
    assert until(lambda: p.is_file() and text in p.read_text(errors="replace"))


def _alive(pid):
    """True while `pid` runs (a zombie counts as gone)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def _sleeper(pidfile):
    """A gate whose `sleep 600` child records its pid, so a test can check it was killed."""
    return _gate("slow", f'sleep 600 & echo $! > "{pidfile}"; echo go; wait', timeout_s=900)


def _sleep_pid(pidfile):
    assert until(lambda: pidfile.is_file() and pidfile.read_text().strip())
    pid = int(pidfile.read_text())
    assert _alive(pid)
    return pid


def test_eof_mid_gate_interrupts(src, tmp_path, wenv):
    pidfile = tmp_path / "sleep.pid"
    h, data = _bundle_job(src, tmp_path, [_sleeper(pidfile), _gate("never", "true")],
                          job_id="eof-gate")
    job = Job(wenv, h, data)
    job.wait_for("started")
    _wait_log(wenv, "eof-gate", "slow", "go")
    pid = _sleep_pid(pidfile)
    job.close()
    fin = job.final()
    assert job.proc.wait(timeout=WAIT_S) == 0
    assert fin["type"] == "done" and fin["interrupted"] is True and fin["ok"] is False
    gates = [f["result"] for f in job.frames if f["type"] == "gate"]
    assert [g["name"] for g in gates] == ["slow"] and gates[0]["interrupted"] is True
    assert until(lambda: not _alive(pid)), f"the gate's sleep {pid} outlived the job"


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_mid_gate_kills_the_gate_and_sends_done(src, tmp_path, wenv, sig):
    pidfile = tmp_path / "sleep.pid"
    h, data = _bundle_job(src, tmp_path, [_sleeper(pidfile), _gate("never", "true")])
    job = Job(wenv, h, data)
    job.wait_for("started")
    pid = _sleep_pid(pidfile)
    job.proc.send_signal(sig)
    fin = job.final()
    assert job.proc.wait(timeout=WAIT_S) == 0
    assert fin["type"] == "done" and fin["interrupted"] is True and fin["ok"] is False
    assert [f["result"]["name"] for f in job.frames if f["type"] == "gate"] == ["slow"]
    assert until(lambda: not _alive(pid)), f"the gate's sleep {pid} outlived the job"
    assert not (_job_dir(wenv, h["job_id"]) / "tree").exists()
    job.close()


def test_signal_before_the_slot_is_an_error_frame(src, tmp_path, wenv):
    marker = tmp_path / "ran"
    h, data = _bundle_job(src, tmp_path, [_gate("x", f"touch {marker}")])
    h["bundle_bytes"] = len(data) + 10          # the job waits for bytes that never come
    job = Job(wenv, h, data)
    assert until(lambda: (_job_dir(wenv, h["job_id"]) / "job.json").is_file())
    job.proc.send_signal(signal.SIGTERM)
    fin = job.final()
    assert job.proc.wait(timeout=WAIT_S) == 3
    assert fin["type"] == "error" and fin["stage"] == "signal" and "SIGTERM" in fin["error"]
    assert job.types() == ["error"] and not marker.exists()
    job.close()


def test_signal_under_the_repo_lock_waits_for_the_git_and_the_lock(src, tmp_path, wenv,
                                                                  monkeypatch):
    """A pre-slot signal never raises inside the repo lock or a running git: the have-ref
    update it lands in completes (no stale `.lock`), the lock is released, then the job ends
    with stage `signal` and its cleanup (which takes the repo lock again) does not hang."""
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal handlers need the main thread")
    import io
    h, data = _bundle_job(src, tmp_path, [_gate("x", f"touch {tmp_path / 'ran'}")])
    real, sent = W._git, []

    def git(*args, **kw):
        if "update-ref" in args and not sent and str(args[-2]).startswith(W.HAVE_REFS):
            sent.append(1)
            os.kill(os.getpid(), signal.SIGTERM)    # handled while the repo lock is held
        return real(*args, **kw)

    monkeypatch.setattr(W, "_git", git)
    old = signal.getsignal(signal.SIGTERM)
    out = io.BytesIO()
    rc = W.gate_job(_cfg(wenv), io.BytesIO((json.dumps(h) + "\n").encode() + data), out)
    assert signal.getsignal(signal.SIGTERM) is old
    frames = [json.loads(ln) for ln in out.getvalue().splitlines()]
    assert sent and rc == 3 and [f["type"] for f in frames] == ["error"]
    assert frames[0]["stage"] == "signal" and "SIGTERM" in frames[0]["error"]
    cache = _cache(wenv)
    assert _git(cache, "rev-parse", f"refs/nw/have/{src.head}") == src.head
    assert not list((cache / "refs").rglob("*.lock"))
    jd = _job_dir(wenv, h["job_id"])
    assert not (jd / "tree").exists() and not (tmp_path / "ran").exists()
    assert _git(cache, "for-each-ref", "--format=%(refname)", "refs/nw/gate-jobs/") == ""
    fd = os.open(cache.parent / "proj.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # nobody kept the repo lock
    finally:
        os.close(fd)


def test_a_stale_git_ref_lock_in_the_cache_is_cleared(src, tmp_path, wenv):
    """A git killed hard leaves `<ref>.lock`; every cache writer holds the repo lock, so the
    next job clears it instead of failing its have-ref update."""
    h, data = _bundle_job(src, tmp_path, [_gate("x", "true")])
    assert _run(wenv, h, data)[2] == 0
    cache = _cache(wenv)
    stale = [cache / "refs" / "nw" / "have" / f"{src.base}.lock", cache / "packed-refs.lock"]
    for path in stale:
        path.write_text("")
    h2, data2 = _bundle_job(src, tmp_path, [_gate("x", "true")])
    job, fin, rc = _run(wenv, h2, data2)
    assert rc == 0 and fin["ok"] is True, job.frames
    assert not any(path.exists() for path in stale)


def _slot_conf(env):
    from pathlib import Path
    (Path(env["NW_CONFIG_DIR"]) / "config.yaml").write_text("agentic:\n  gates:\n    slots: 1\n")
    return {**env, W.SLOT_WAIT_ENV: "0.5"}


def test_held_slot_gives_slot_wait_then_started(src, tmp_path, wenv):
    env = _slot_conf(wenv)
    h, data = _bundle_job(src, tmp_path, [_gate("x", "true")])
    with GS.acquire(_cfg(env, slots=1), "other") as hold:
        assert hold.slot == 0
        job = Job(env, h, data)
        job.wait_for("slot_wait")
        assert "started" not in job.types()
        # a remote job waits as `remote:<campaign>`, never as a local campaign of the worker
        assert [w["campaign"] for w in GS.occupancy(_cfg(env, slots=1))["waiting"]] \
            == ["remote:C-1"]
        assert GS.waiting(_cfg(env, slots=1), "C-1") is None
    fin, rc = job.finish()
    assert rc == 0 and fin["ok"] is True
    assert job.types() == ["accepted", "slot_wait", "started", "gate", "done"]
    assert job.frames[1]["waited_s"] >= 0.5 and fin["slot_wait_s"] > 0


def test_eof_while_waiting_for_the_slot(src, tmp_path, wenv):
    env = _slot_conf(wenv)
    marker = tmp_path / "ran"
    h, data = _bundle_job(src, tmp_path, [_gate("x", f"touch {marker}")])
    with GS.acquire(_cfg(env, slots=1), "other"):
        job = Job(env, h, data)
        job.wait_for("slot_wait")
        job.close()
        fin = job.final()
        assert job.proc.wait(timeout=WAIT_S) == 0
    assert fin["type"] == "done" and fin["interrupted"] is True and fin["ok"] is False
    assert "started" not in job.types() and "gate" not in job.types()
    assert not marker.exists()


def test_log_over_16_mib_is_truncated(src, tmp_path, wenv):
    n = 17 * 1024 * 1024
    h, data = _bundle_job(src, tmp_path, [_gate("big", f"echo HEAD-MARKER; yes 0123456789abcdef"
                                                       f" | head -c {n}; echo; echo TAIL-MARKER")])
    job, fin, rc = _run(wenv, h, data)
    assert rc == 0 and fin["ok"] is True
    g = next(f for f in job.frames if f["type"] == "gate")
    log = _file(g, "001-big.log")
    assert len(log) == W.MAX_LOG_BYTES
    # the last 16 MiB are kept: the tail survives, the head is cut
    assert log.endswith(b"TAIL-MARKER\n") and b"HEAD-MARKER" not in log
    full = len("HEAD-MARKER\n") + n + len("\nTAIL-MARKER\n")
    assert json.loads(_file(g, "001-big.json"))["truncated_bytes"] == full - W.MAX_LOG_BYTES


# ---- have / gc / info ------------------------------------------------------------------------

def test_have_then_gc_removes_refs_objects_and_job_dirs(src, tmp_path, wenv):
    h, data = _bundle_job(src, tmp_path, [_gate("x", "true")])
    assert _run(wenv, h, data)[2] == 0
    other = "e" * 40
    r = _nw(wenv, "have", "--repo", "proj", src.head, src.base, other)
    assert r.returncode == 0 and r.stdout.split() == [src.head, src.base]
    assert _nw(wenv, "have", "--repo", "../x", src.head).returncode == 2
    r = _nw(wenv, "gc", "--keep-days", "0")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["pruned"] == ["proj"]
    assert _nw(wenv, "have", "--repo", "proj", src.head, src.base).stdout.split() == []
    p = subprocess.run(["git", "-C", str(_cache(wenv)), "cat-file", "-e", src.head],
                       capture_output=True)
    assert p.returncode != 0
    assert not _job_dir(wenv, h["job_id"]).exists()


def test_gc_during_a_live_job_keeps_its_dir(src, tmp_path, wenv):
    h, data = _bundle_job(src, tmp_path, [_gate("slow", "echo go; sleep 600", timeout_s=900)],
                          job_id="live")
    job = Job(wenv, h, data)
    job.wait_for("started")
    _wait_log(wenv, "live", "slow", "go")
    r = _nw(wenv, "gc", "--keep-days", "0")
    assert r.returncode == 0, r.stderr
    assert "live" not in json.loads(r.stdout)["jobs_removed"]
    jd = _job_dir(wenv, "live")
    assert (jd / "tree" / "b.txt").is_file() and (jd / "job.lock").exists()
    assert _git(_cache(wenv), "cat-file", "-t", src.head) == "commit"
    job.close()
    fin = job.final()
    assert job.proc.wait(timeout=WAIT_S) == 0 and fin["interrupted"] is True


def test_gc_drops_orphan_job_refs_and_keeps_fresh_have_refs(src, tmp_path, wenv):
    h, data = _bundle_job(src, tmp_path, [_gate("x", "true")])
    assert _run(wenv, h, data)[2] == 0
    cache = _cache(wenv)
    # a crashed job left its refs behind; no live job owns them
    _git(cache, "update-ref", "refs/nw/gate-jobs/crashed", src.head)
    _git(cache, "update-ref", "refs/nw/gate-jobs/crashed-base", src.base)
    r = _nw(wenv, "gc")                                  # the default --keep-days 2
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)
    assert rec["refs_removed"] == 2 and rec["pruned"] == ["proj"]
    assert h["job_id"] not in rec["jobs_removed"] and _job_dir(wenv, h["job_id"]).is_dir()
    refs = _git(cache, "for-each-ref", "--format=%(refname)").split()
    assert sorted(refs) == sorted([f"refs/nw/have/{src.head}", f"refs/nw/have/{src.base}"])
    assert _nw(wenv, "have", "--repo", "proj", src.head).stdout.split() == [src.head]
    # three days later the have refs (now stale) and the job dir go
    hj = cache.parent / "proj.have.json"
    stale = {k: v - 3 * 86400 for k, v in json.loads(hj.read_text()).items()}
    hj.write_text(json.dumps(stale))
    old = _job_dir(wenv, h["job_id"]).stat().st_mtime - 3 * 86400
    os.utime(_job_dir(wenv, h["job_id"]), (old, old))
    rec = json.loads(_nw(wenv, "gc").stdout)
    assert rec["refs_removed"] == 2 and rec["jobs_removed"] == [h["job_id"]]
    assert _git(cache, "for-each-ref", "--format=%(refname)") == ""


def test_gc_reads_the_live_jobs_again_under_the_repo_lock(src, tmp_path, wenv, monkeypatch):
    """A job claimed after gc's first scan of jobs/ (and before its repo phase) keeps its have
    refs, its job refs and so its objects, even with --keep-days 0."""
    h, data = _bundle_job(src, tmp_path, [_gate("x", "true")])
    assert _run(wenv, h, data)[2] == 0
    cache, cfg = _cache(wenv), _cfg(wenv)
    real, fds = W._live_jobs, []

    def late_claim(cfg_, *a, **kw):
        res = real(cfg_, *a, **kw)
        if not fds:                                     # right after the first scan
            jd = _job_dir(wenv, "late")
            jd.mkdir()
            fds.append(os.open(jd / "job.lock", os.O_RDWR | os.O_CREAT))
            fcntl.flock(fds[0], fcntl.LOCK_EX)
            (jd / "job.json").write_text(json.dumps({"commit": src.head,
                                                     "base": {"sha": src.base}}))
            _git(cache, "update-ref", "refs/nw/gate-jobs/late", src.head)
            _git(cache, "update-ref", "refs/nw/gate-jobs/late-base", src.base)
        return res

    monkeypatch.setattr(W, "_live_jobs", late_claim)
    try:
        W.gc(cfg, keep_days=0)
        refs = set(_git(cache, "for-each-ref", "--format=%(refname)").split())
        assert {f"refs/nw/have/{src.head}", f"refs/nw/have/{src.base}",
                "refs/nw/gate-jobs/late", "refs/nw/gate-jobs/late-base"} <= refs
        assert _git(cache, "cat-file", "-t", src.head) == "commit"
        assert _job_dir(wenv, "late").is_dir()
    finally:
        for fd in fds:
            os.close(fd)


def test_info_json_keys(wenv):
    r = _nw(wenv, "info", "--json")
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)
    assert set(rec) == {"schema", "protocol", "version", "git_sha", "data_dir", "git", "uv",
                        "free_bytes"}
    assert rec["schema"] == "nw.worker.info.v1" and rec["protocol"] == "nw.job.v1"
    assert rec["data_dir"] == wenv["NW_DATA_DIR"] and isinstance(rec["free_bytes"], int)
