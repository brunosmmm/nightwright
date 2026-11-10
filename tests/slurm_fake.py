"""A fake Slurm CLI for tests (SPEC-0510; used by SPEC-0508 and SPEC-0511..0515 too).

`SlurmFake(tmp_path)` writes executable fakes `sbatch`, `squeue`, `scontrol`, `scancel`, `sacct`,
`sinfo` and `sstat` into a dir the test puts first on `PATH` (`fake.install(monkeypatch)` or
`fake.env(base)`), backed by one JSON state file (flocked). Every call's argv is recorded
(`fake.calls("sbatch")`). The test drives job transitions:

    fake.pending(jid, "Resources")      # PENDING with a reason
    fake.run(jid)                       # RUNNING, execute the `--wrap` command, then
                                        # COMPLETED / FAILED by its exit code (bounded, reaped)
    p = fake.start(jid)                 # the same in the background (fake.finish(jid) waits)
    fake.end(jid, "OUT_OF_MEMORY")      # any end state, no process run
    fake.forget(jid)                    # past MinJobAge: squeue and scontrol no longer know it
    fake.fail("sbatch", "error: ...")   # the next calls of a command fail

The JSON records have the shape of real Slurm 26.05 output (`tests/fixtures/slurm/`): `job_state`
a list, numbers as `{set, infinite, number}`. `squeue` lists only jobs not yet ended (an unknown
id exits 1 "Invalid job id specified"); `scontrol show job` knows ended jobs until forgotten
(then an empty list, exit 0); `sacct` says accounting is disabled (no slurmdbd), as on the box,
until `fake.accounting()` (SPEC-0514: `show config` reports slurmdbd, `fake.acct(jid, rows)`).

`scancel N` cancels (CANCELLED; a running job's process group gets SIGTERM); `scancel --batch
--signal=TERM N` sends TERM to the job's batch process only and leaves the state to the process.
Background processes are always reaped: `fake.close()` (the fixture's teardown) kills what is
left."""
from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path

from waits import WAIT_S

COMMANDS = ("sbatch", "squeue", "scontrol", "scancel", "sacct", "sinfo", "sstat")
FIXTURES = Path(__file__).parent / "fixtures" / "slurm"
ENDED = ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL",
         "PREEMPTED", "BOOT_FAIL", "DEADLINE")

# the program every fake command runs (argv[1] is the command's name)
_PROGRAM = r'''
import fcntl, json, os, signal, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state.json"
ENDED = %(ended)r


def num(n, set_=True):
    return {"set": set_, "infinite": False, "number": n}


def record(jid, j):
    rc = j.get("exit_code") or 0
    return {"job_id": int(jid), "name": j["name"], "job_state": [j["state"]],
            "state_reason": j.get("reason") or "None", "state_description": "",
            "cpus_per_task": num(j["cpus"]), "cpus": num(j["cpus"]),
            "memory_per_node": num(j["mem_mb"]), "comment": j.get("comment", ""),
            "partition": j.get("partition") or "nw-gatejob", "user_name": j.get("user") or "nw",
            "qos": j.get("qos") or "", "nice": j.get("nice", 0), "hold": bool(j.get("held")),
            "nodes": "fake" if j["state"] not in ("PENDING",) else "",
            "submit_time": num(j.get("submit_ts") or 1700000000),
            "start_time": num(j.get("start_ts") or 0),
            "time_limit": num(j.get("time_min") or 0), "restart_cnt": j.get("restart_cnt", 0),
            "licenses": j.get("licenses", ""),
            "exit_code": {"status": ["SUCCESS" if rc == 0 else "FAILED"],
                          "return_code": num(rc), "signal": {"id": num(0, False), "name": ""}}}


def doc(jobs, cmd):
    return json.dumps({"jobs": jobs, "meta": {"command": cmd}, "errors": [], "warnings": []})


def opts(args):
    """`--k=v`, `--k v`, `-k v` -> {k: v}; flags -> True; the rest positional."""
    out, pos, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            out[k] = v
        elif a in ("-p", "-c", "-L", "-J", "-t", "-o", "-D", "-j", "-u", "-N", "-h"):
            if a in ("-N", "-h"):
                out[a[1:]] = True
            else:
                out[a[1:]] = args[i + 1] if i + 1 < len(args) else ""
                i += 1
        elif a.startswith("--"):
            out[a[2:]] = True
        else:
            pos.append(a)
        i += 1
    return out, pos


def control(st, pos):
    """`scontrol hold|release|top|requeue N` and `scontrol update JobId=N Key=V...`."""
    verb = pos[0]
    kv = dict(x.split("=", 1) for x in pos[1:] if "=" in x)
    jid = kv.get("JobId") if verb == "update" else (pos[1] if len(pos) > 1 else None)
    j = st["jobs"].get(jid)
    if j is None or jid in st["forgotten"]:
        return 1, "", "Invalid job id specified"
    deny = st.get("deny", {}).get(verb)
    if deny and deny["left"] != 0:
        if deny["left"] is not None:
            deny["left"] -= 1
        return 1, "", deny["err"] + f" for job {jid}"
    if verb in ("hold", "release", "top") and j["state"] != "PENDING":
        return 1, "", f"Job is no longer pending execution for job {jid}"
    if verb == "hold":
        j["held"], j["reason"] = True, "JobHeldUser"
    elif verb == "release":
        j["held"], j["reason"] = False, "None"
    elif verb == "top":
        st.setdefault("topped", []).append(jid)
    elif verb == "requeue":
        if j["state"] == "PENDING":
            return 1, "", f"Job is pending execution for job {jid}"
        j.update(state="PENDING", reason="BeginTime",
                 restart_cnt=int(j.get("restart_cnt") or 0) + 1, pid=None)
    else:
        if "Partition" in kv:
            if j["state"] != "PENDING":
                return 1, "", f"Job is no longer pending execution for job {jid}"
            if kv["Partition"] in st.get("reject", {}).get("partition", []):
                return 1, "", f"Invalid partition name specified for job {jid}"
            j["partition"] = kv["Partition"]
        if "QOS" in kv:
            j["qos"] = kv["QOS"]
        if "Nice" in kv:
            if int(kv["Nice"]) < 0:
                return 1, "", f"Access/permission denied for job {jid}"
            j["nice"] = int(kv["Nice"])
    return 0, "", ""


def main():
    name, args = sys.argv[1], sys.argv[2:]
    fd = os.open(STATE, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        st = json.loads(Path(STATE).read_text())
        st["calls"].append([name, *args])
        rc, out, err = run(st, name, args)
        Path(STATE).write_text(json.dumps(st))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    if out:
        sys.stdout.write(out if out.endswith("\n") else out + "\n")
    if err:
        sys.stderr.write(err if err.endswith("\n") else err + "\n")
    sys.exit(rc)


def run(st, name, args):
    fail = st["fail"].get(name)
    if fail:
        return fail["rc"], "", fail["err"]
    o, pos = opts(args)
    jobs = st["jobs"]
    if name == "sbatch":
        part = o.get("partition") or o.get("p")
        if part and part in st.get("reject", {}).get("partition", []):
            return 1, "", ("sbatch: error: invalid partition specified: %%s\nsbatch: error: Batch "
                           "job submission failed: Invalid partition name specified" %% part)
        if o.get("qos") and o["qos"] in st.get("reject", {}).get("qos", []):
            return 1, "", ("sbatch: error: Batch job submission failed: Invalid qos "
                           "specification")
        jid = str(st["next_id"])
        st["next_id"] += 1
        mem = str(o.get("mem", "1024M"))
        mem_mb = int(float(mem[:-1]) * (1024 if mem[-1] == "G" else 1)) if mem[-1] in "MG" \
            else int(mem)
        j = {"name": o.get("job-name", "wrap"), "comment": o.get("comment", ""),
             "partition": o.get("partition") or o.get("p"), "qos": o.get("qos"),
             "user": st.get("user") or "nw",
             "cpus": int(o.get("cpus-per-task") or o.get("c") or 1), "mem_mb": mem_mb,
             "time_min": int(o.get("time") or 0), "licenses": o.get("licenses") or o.get("L") or "",
             "wrap": o.get("wrap"), "chdir": o.get("chdir"), "output": o.get("output"),
             "state": "PENDING", "reason": "None", "restart_cnt": 0, "argv": args,
             "env": dict(os.environ)}
        auto = st["auto"].get(j["name"])
        if auto:
            j["state"] = auto["state"]
            if j["output"] and auto.get("output") is not None:
                Path(j["output"]).write_text(auto["output"])
        jobs[jid] = j
        return 0, jid if "parsable" in o else f"Submitted batch job {jid}", ""
    if name == "squeue":
        if "j" in o or "jobs" in o:
            ids = str(o.get("j") or o.get("jobs")).split(",")
            known = [i for i in ids if i in jobs and i not in st["forgotten"]]
            if not known:
                return 1, "", "slurm_load_jobs error: Invalid job id specified"
        else:
            known = [i for i in jobs if i not in st["forgotten"]
                     and ("u" not in o or (jobs[i].get("user") or "nw") == o["u"])]
        live = [record(i, jobs[i]) for i in known if jobs[i]["state"] not in ENDED]
        return 0, doc(live, ["squeue", *args]), ""
    if name == "scontrol":
        if pos[:2] == ["show", "job"]:
            jid = pos[2] if len(pos) > 2 else None
            j = jobs.get(jid) if jid not in st["forgotten"] else None
            return 0, doc([record(jid, j)] if j else [], ["scontrol", *args]), ""
        if pos[:2] == ["show", "licenses"]:
            return 0, "".join(f"LicenseName={k}\n    Total={v} Used=0 Free={v} Reserved=0 "
                              f"Remote=no\n" for k, v in st["licenses"].items()), ""
        if pos[:2] == ["show", "config"]:
            return 0, ("SelectType              = select/cons_tres\n"
                       "ProctrackType           = proctrack/cgroup\n"
                       "TaskPlugin              = task/cgroup,task/affinity\n"
                       "AccountingStorageType   = " + ("accounting_storage/slurmdbd"
                                                      if st.get("accounting") else "(null)")
                       + "\n"
                       "MinJobAge               = 300 sec\n"), ""
        if pos[:2] in (["show", "partition"], ["show", "node"]) and "json" in o:
            if st["fixtures"].get(pos[1]) is None:
                return 1, "", "slurm_load_partitions error: Unable to contact slurm controller"
            return 0, Path(st["fixtures"][pos[1]]).read_text(), ""
        if pos[:2] == ["show", "node"]:
            return 0, ("NodeName=fake CoresPerSocket=6\n   CoreSpecCount=1 CPUSpecList=10-11 "
                       "MemSpecLimit=6000\n   CfgTRES=cpu=10,mem=31955M,billing=10\n"), ""
        if pos[:1] in (["hold"], ["release"], ["top"], ["requeue"], ["update"]):
            return control(st, pos)
        return 1, "", f"scontrol: unsupported {args}"
    if name == "scancel":
        jid = pos[-1] if pos else None
        j = jobs.get(jid)
        if j is None:
            return 1, "", f"scancel: error: Invalid job id {jid}"
        if j["state"] in ENDED:
            return 1, "", f"scancel: error: Job/step already completing or completed"
        pid = j.get("pid")
        if "signal" in o:
            if pid:
                try:
                    os.kill(pid, getattr(signal, "SIG" + o["signal"]))
                except OSError:
                    pass
            return 0, "", ""
        j["state"], j["reason"] = "CANCELLED", "None"
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                pass
        return 0, "", ""
    if name == "sacct":
        if not st.get("accounting"):
            return 1, "", "Slurm accounting storage is disabled"
        jid = str(o.get("j") or o.get("jobs") or "")
        return 0, "\n".join((jobs.get(jid) or {}).get("acct") or []), ""
    if name == "sinfo":
        if "version" in o:
            return 0, "slurm 26.05.5", ""
        return 0, "fake 12 31955 idle", ""
    if name == "sstat":
        return 0, "", ""
    return 1, "", f"unknown fake command {name}"


main()
'''


class SlurmFake:
    def __init__(self, root: Path, licenses: dict | None = None):
        self.root = Path(root)
        self.bin = self.root / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        self.state_path.write_text(json.dumps({
            "next_id": 100, "jobs": {}, "calls": [], "fail": {}, "auto": {}, "forgotten": [],
            # `scontrol show partition|node --json`: the box's recorded output
            "fixtures": {"partition": str(FIXTURES / "scontrol_show_partition.json"),
                         "node": str(FIXTURES / "scontrol_show_node.json")},
            "licenses": {"jerome_target": 3} if licenses is None else licenses}))
        prog = self.root / "fake.py"
        prog.write_text(_PROGRAM % {"ended": ENDED})
        for name in COMMANDS:
            p = self.bin / name
            p.write_text(f"#!/bin/sh\nexec {sys.executable} {prog} {name} \"$@\"\n")
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self._procs: dict[str, subprocess.Popen] = {}

    # ---- wiring --------------------------------------------------------------------------------

    def env(self, base: dict | None = None) -> dict:
        base = dict(os.environ if base is None else base)
        base["PATH"] = f"{self.bin}{os.pathsep}{base.get('PATH', '')}"
        return base

    def install(self, monkeypatch) -> "SlurmFake":
        monkeypatch.setenv("PATH", f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}")
        return self

    # ---- state ---------------------------------------------------------------------------------

    def _load(self) -> dict:
        return json.loads(self.state_path.read_text())

    def _edit(self, fn) -> None:
        import fcntl
        fd = os.open(self.state_path, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            st = self._load()
            fn(st)
            self.state_path.write_text(json.dumps(st))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def calls(self, name: str | None = None) -> list[list[str]]:
        return [c[1:] if name else c for c in self._load()["calls"] if name in (None, c[0])]

    def job(self, jid) -> dict:
        return self._load()["jobs"][str(jid)]

    def ids(self) -> list[int]:
        return [int(i) for i in self._load()["jobs"]]

    def _set(self, jid, **fields) -> None:
        self._edit(lambda st: st["jobs"][str(jid)].update(fields))

    def pending(self, jid, reason: str = "Resources") -> None:
        self._set(jid, state="PENDING", reason=reason)

    def end(self, jid, state: str, exit_code: int = 0) -> None:
        self._set(jid, state=state, reason="None", exit_code=exit_code, pid=None)

    def accounting(self, on: bool = True) -> None:
        """`AccountingStorageType` is slurmdbd (`sacct` answers from `acct()`), or none."""
        self._edit(lambda st: st.__setitem__("accounting", bool(on)))

    def acct(self, jid, *rows: str) -> None:
        """The `sacct -P -n -o JobID,State,Elapsed,TotalCPU,MaxRSS,NodeList,ExitCode` rows."""
        self._set(jid, acct=list(rows))

    def forget(self, jid) -> None:
        self._edit(lambda st: st["forgotten"].append(str(jid)))

    def fail(self, name: str, err: str, rc: int = 1) -> None:
        self._edit(lambda st: st["fail"].__setitem__(name, {"rc": rc, "err": err}))

    def fixture(self, what: str, path) -> None:
        """Serve `scontrol show <what> --json` from `path` (None: the call fails)."""
        self._edit(lambda st: st["fixtures"].__setitem__(what, None if path is None
                                                         else str(path)))

    def reject(self, what: str, name: str) -> None:
        """sbatch / `scontrol update` refuse partition (or qos) `name` as Slurm does."""
        self._edit(lambda st: st.setdefault("reject", {}).setdefault(what, []).append(name))

    def deny(self, verb: str, err: str, times: int | None = None) -> None:
        """`scontrol <verb>` fails with `err` (e.g. `top` without enable_user_top), the next
        `times` calls only when given."""
        self._edit(lambda st: st.setdefault("deny", {}).__setitem__(
            verb, {"err": err, "left": times}))

    def as_user(self, user: str) -> None:
        """Jobs submitted from now on belong to `user` (default `nw`)."""
        self._edit(lambda st: st.__setitem__("user", user))

    def heal(self, name: str) -> None:
        self._edit(lambda st: st["fail"].pop(name, None))

    def auto(self, job_name: str, state: str, output: str | None = None) -> None:
        """An `sbatch --job-name=<job_name>` ends at once in `state`, `output` in its file."""
        self._edit(lambda st: st["auto"].__setitem__(job_name,
                                                     {"state": state, "output": output}))

    # ---- running a job -------------------------------------------------------------------------

    def _popen(self, jid) -> subprocess.Popen:
        j = self.job(jid)
        env = dict(j["env"])
        env.update(SLURM_JOB_ID=str(jid), SLURM_CPUS_PER_TASK=str(j["cpus"]),
                   SLURM_MEM_PER_NODE=str(j["mem_mb"]), SLURM_JOB_NAME=j["name"])
        if j.get("restart_cnt"):
            env["SLURM_RESTART_COUNT"] = str(j["restart_cnt"])
        out = open(j["output"], "ab") if j.get("output") else subprocess.DEVNULL
        try:
            # its own session, as slurmstepd starts a batch script: `exec` keeps the pid
            p = subprocess.Popen(["/bin/sh", "-c", j["wrap"]], cwd=j.get("chdir") or None,
                                 env=env, stdin=subprocess.DEVNULL, stdout=out,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            if out is not subprocess.DEVNULL:
                out.close()
        self._set(jid, state="RUNNING", reason="None", pid=p.pid)
        self._procs[str(jid)] = p
        return p

    def start(self, jid) -> subprocess.Popen:
        """RUNNING, its `--wrap` started in the background (`finish` waits for it)."""
        return self._popen(jid)

    def finish(self, jid, timeout: float | None = None) -> int:
        """Wait for a started job's process (bounded; killed past the bound) and end the job
        COMPLETED (exit 0) or FAILED, unless something (scancel) ended it already."""
        p = self._procs.pop(str(jid))
        try:
            rc = p.wait(timeout=WAIT_S if timeout is None else timeout)
        except subprocess.TimeoutExpired:
            self._kill(p)
            raise
        if self.job(jid)["state"] not in ENDED:
            self.end(jid, "COMPLETED" if rc == 0 else "FAILED", rc)
        else:
            self._set(jid, pid=None)
        return rc

    def run(self, jid, timeout: float | None = None) -> int:
        self._popen(jid)
        return self.finish(jid, timeout)

    def requeue(self, jid) -> None:
        """A requeue (node failure, preemption): PENDING again, one more restart."""
        j = self.job(jid)
        self._set(jid, state="PENDING", reason="BeginTime",
                  restart_cnt=int(j.get("restart_cnt") or 0) + 1, pid=None)

    @staticmethod
    def _kill(p: subprocess.Popen) -> None:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        for p in self._procs.values():
            if p.poll() is None:
                self._kill(p)
        self._procs.clear()
