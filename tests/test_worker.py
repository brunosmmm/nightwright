"""SPEC-0336: `nw worker gate-job` and friends, driven as a subprocess with a header and bundle
the test builds itself (offline: the "coordinator" is a temp git repo)."""
import base64
import gzip
import json
import os
import queue
import subprocess
import sys
import threading
import uuid

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


def test_eof_mid_gate_interrupts(src, tmp_path, wenv):
    h, data = _bundle_job(src, tmp_path, [_gate("slow", "echo go; sleep 600", timeout_s=900),
                                          _gate("never", "true")], job_id="eof-gate")
    job = Job(wenv, h, data)
    job.wait_for("started")
    _wait_log(wenv, "eof-gate", "slow", "go")
    job.close()
    fin = job.final()
    assert job.proc.wait(timeout=WAIT_S) == 0
    assert fin["type"] == "done" and fin["interrupted"] is True and fin["ok"] is False
    gates = [f["result"] for f in job.frames if f["type"] == "gate"]
    assert [g["name"] for g in gates] == ["slow"] and gates[0]["interrupted"] is True


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
    h, data = _bundle_job(src, tmp_path, [_gate("big", f"yes 0123456789abcdef | head -c {n}")])
    job, fin, rc = _run(wenv, h, data)
    assert rc == 0 and fin["ok"] is True
    g = next(f for f in job.frames if f["type"] == "gate")
    assert len(_file(g, "001-big.log")) == W.MAX_LOG_BYTES
    assert json.loads(_file(g, "001-big.json"))["truncated_bytes"] == n - W.MAX_LOG_BYTES


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


def test_info_json_keys(wenv):
    r = _nw(wenv, "info", "--json")
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)
    assert set(rec) == {"schema", "protocol", "version", "git_sha", "data_dir", "git", "uv",
                        "free_bytes"}
    assert rec["schema"] == "nw.worker.info.v1" and rec["protocol"] == "nw.job.v1"
    assert rec["data_dir"] == wenv["NW_DATA_DIR"] and isinstance(rec["free_bytes"], int)
