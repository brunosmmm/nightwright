"""Shared helpers for the remote worker tests (SPEC-0338's loopback worker, SPEC-0344's persona
jobs): the fake campaign `World`, the loopback `Remote` reached through a fake `ssh`, and the
persona-job helpers. Not a test module: importing these helpers does not make a test module
inherit what `test_remote_gates` references (`tools/affected_tests.py` walks test-to-test
imports for free)."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from nightwright.agentic import gates as G
from nightwright.agentic import runner as R
from nightwright.agentic import store as S

CID = "SPEC-0900-20261101-abcd"


@pytest.fixture(autouse=True)
def _short_runtime_dir(monkeypatch):
    """A long pytest tmp path makes the ssh socket dir fall back to $XDG_RUNTIME_DIR: keep
    those dirs out of the real one."""
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="nwr")
    monkeypatch.setenv("XDG_RUNTIME_DIR", d)
    G.reset()
    yield
    G.reset()
    shutil.rmtree(d, ignore_errors=True)


# The identity is pinned too: conftest's autouse `_git_identity` exports its own, which would
# otherwise override the `t <t@example.com>` World configures and change every sha.
_DATES = {"GIT_AUTHOR_DATE": "2026-11-01T10:00:00+00:00",
          "GIT_COMMITTER_DATE": "2026-11-01T10:00:00+00:00",
          "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
          "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


_IDENTITY = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")


def _git(root, *args):
    """git with fixed dates, so the fake repo's shas (and the golden record) are stable. The
    conftest identity env is dropped: the repo's own `user.*` config names the author, as the
    golden record was made with."""
    env = {k: v for k, v in os.environ.items() if k not in _IDENTITY}
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, env={**env, **_DATES}).stdout.strip()


def _commit(root, name, text):
    (root / name).write_text(text)
    _git(root, "add", name)
    _git(root, "commit", "-q", "-m", f"add {name}")
    return _git(root, "rev-parse", "HEAD")


def _cfg(tmp_path, slots=2):
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "cfg"),
            "agentic": {"gates": {"slots": slots}}}


def _gate(name, cmd, **kw):
    return {"name": name, "cmd": cmd, "cwd": ".", "timeout_s": 60, "required": True,
            "retries": 0, **kw}


class World:
    def __init__(self, tmp_path, *, feature=True):
        self.tmp = tmp_path
        self.cfg = _cfg(tmp_path)
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (self.repo / ".git" / "info" / "exclude").write_text("ignored.txt\n")
        self.base = _commit(self.repo, "a.txt", "a\n")
        _git(self.repo, "checkout", "-q", "-b", f"nw/{CID}")
        self.head = _commit(self.repo, "b.txt", "b\n") if feature else self.base
        self.state = {"id": CID, "spec_id": "SPEC-0900", "project": "demo",
                      "profile": "balanced", "state": "GATING",
                      "created_at": "2026-11-01T10:00:00-04:00",
                      "repo": {"base": "main", "worktree": str(self.repo),
                               "branch": f"nw/{CID}"},
                      "counters": {"loops": 0, "runs": 0},
                      "cost": {"total_usd": 0.0, "cap_usd": 10.0}, "last_gate": None,
                      "park": None}
        S.create_campaign(self.cfg, self.state)
        self.counter = tmp_path / "counter"
        self.project = {"project": "demo", "base": "main", "gates": [
            _gate("ok", "echo hello; git rev-parse HEAD"),
            _gate("opt", "echo boom >&2; exit 3", required=False),
            _gate("flaky", f"n=$(cat {self.counter} 2>/dev/null || echo 0); "
                           f"echo $((n+1)) > {self.counter}; echo try $n; [ $n -ge 1 ]",
                  retries=1),
        ]}

    def split_counter(self):
        """The `flaky` gate with one counter per side (worker vs local). After a dropped
        connection the worker job keeps running, so with one shared counter it may spend the
        local re-run's first failure, and whether the local run writes `.r1` files depends on
        which side reached the counter first. Gate text differs from the golden's, so only the
        drop cases use it."""
        (g,) = [g for g in self.project["gates"] if g["name"] == "flaky"]
        g["cmd"] = (f'c={self.counter}.$(case "$NW_DATA_DIR" in *wdata*) echo w;; *) echo l;; esac); '
                    'n=$(cat $c 2>/dev/null || echo 0); echo $((n+1)) > $c; echo try $n; [ $n -ge 1 ]')

    @property
    def gates_dir(self):
        return S.gates_dir(self.cfg, CID)

    def ledger(self, types=None):
        return S.read_ledger(self.cfg, CID, types=types)


def _script(path, body):
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


_DROP_AFTER_GATE = """import subprocess, sys, os, time
p = subprocess.Popen(sys.argv[1:], stdout=subprocess.PIPE)
for line in p.stdout:
    sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()
    if b'"type": "gate"' in line:
        time.sleep(DROP_DELAY_S)
        os._exit(255)
"""

# The slow-drop variant holds the connection open this long after relaying the `gate` frame
# before dropping it: the stall a loaded box puts between the worker's job and the drop. The
# plain variant drops at once.
SLOW_DROP_S = 3.0


DISK_GUARD_OFF = {"workers": {"min_free_gb": 0}}


def no_disk_guard(cfg_dir) -> None:
    """A loopback worker is a real `nw worker` process: SPEC-0387 refuses its jobs while the
    host's free disk is under `min_free_gb` (default 5), which a busy gate box can be, so the
    placement falls back local. Keep the host's disk out of the test."""
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(json.dumps({"agentic": DISK_GUARD_OFF}))


class Remote:
    """A worker reached through a fake `ssh`: the destination is dropped and the command runs
    as the SSH forced command would, against its own data dir."""

    def __init__(self, w: World):
        self.w = w
        tmp = w.tmp
        self.wdata = tmp / "wdata"
        self.wcfg = {"data_dir": str(self.wdata)}
        no_disk_guard(tmp / "wcfgdir")
        self.ssh = _script(tmp / "fake-ssh", (
            f'shift\nexport SSH_ORIGINAL_COMMAND="$*" NW_DATA_DIR={self.wdata} '
            f'NW_CONFIG_DIR={tmp / "wcfgdir"} NW_WORKER_FINISH_GC=0\n'
            f'exec {sys.executable} -m nightwright worker ssh-command'))
        (tmp / "drop.py").write_text(_DROP_AFTER_GATE.replace("DROP_DELAY_S", "0"))
        (tmp / "drop-slow.py").write_text(_DROP_AFTER_GATE.replace("DROP_DELAY_S", str(SLOW_DROP_S)))
        self.variants = {
            "unreachable": _script(tmp / "ssh-255", "echo 'ssh: connect: refused' >&2\nexit 255"),
            "job255": _script(tmp / "ssh-job255", f'case "$*" in *gate-job*) exit 255;; esac\n'
                                                  f'exec {self.ssh} "$@"'),
            "have": _script(tmp / "ssh-have", f'case "$*" in *" have "*) exit 255;; esac\n'
                                             f'exec {self.ssh} "$@"'),
            "garbage": _script(tmp / "ssh-garbage", f'case "$*" in *gate-job*) echo garbage; '
                                                    f'exec cat >/dev/null;; esac\n'
                                                    f'exec {self.ssh} "$@"'),
            "drop": _script(tmp / "ssh-drop", f'case "$*" in *gate-job*) exec {sys.executable} '
                                              f'{tmp / "drop.py"} {self.ssh} "$@";; esac\n'
                                              f'exec {self.ssh} "$@"'),
            "slowdrop": _script(tmp / "ssh-slowdrop", f'case "$*" in *gate-job*) exec {sys.executable} '
                                                      f'{tmp / "drop-slow.py"} {self.ssh} "$@";; '
                                                      f'esac\nexec {self.ssh} "$@"'),
        }

    def register(self, ssh=None, **kw):
        from nightwright.agentic import workers as WK
        p = WK.path(self.w.cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"name": "box", "ssh": "me@box", "slots": 1, "nw": "nw",
               "ssh_argv": [ssh or self.ssh], **kw}
        p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": [rec]}))

    def record(self):
        from nightwright.agentic import workers as WK
        return WK.load(self.w.cfg)[0]

    def frames(self, seq=1):
        job = self.wdata / "agentic" / "worker" / "jobs" / f"{CID}-gate-{seq:03d}-a0"
        return [json.loads(ln) for ln in (job / "frames.jsonl").read_text().splitlines()]


# ---- SPEC-0344 persona jobs ------------------------------------------------------------------

INFO = {"protocol": "nw.job.v1", "features": ["async", "persona"], "git": "git version 2"}
VERDICT = {"summary": "did it"}

# commits c.txt; leaves a modified tracked file (a.txt) and a new untracked one (new.txt)
WRITER = [{"edits": [{"path": "c.txt", "write": "c\n"}, {"path": "a.txt", "append": "more\n"},
                     {"path": "new.txt", "write": "new\n"}],
           "shell": ["git add c.txt && git commit -q -m 'add c'"],
           "verdict": VERDICT, "cost_usd": 0.5}]

LOCAL_OK = [{"verdict": VERDICT, "cost_usd": 0.0}]


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
