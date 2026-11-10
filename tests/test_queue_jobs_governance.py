"""SPEC-0512: `nw queue jobs` governs nw's Slurm jobs by nw id. Worker side (`slurm_govern`,
against the fake Slurm CLI): the list, the scontrol argv per verb, the refusal of a job that is
not this user's nw job, a local executor's `executor is local`. Coordinator side
(`queue_jobs`, its SSH calls routed in-process through the forced-command grammar to the worker
functions): the id resolution table, the event per verb, cancel, and the CLI."""
import getpass
import io
import json
import shlex
import signal
import subprocess
import sys

import click
import pytest
from click.testing import CliRunner

from nightwright.agentic import cli_queue_jobs as CQJ
from nightwright.agentic import events as E
from nightwright.agentic import executor_slurm as ES
from nightwright.agentic import queue_jobs as QJ
from nightwright.agentic import slurm_govern as SG
from nightwright.agentic import worker as W
from nightwright.agentic import worker_job as WJ
from slurm_fake import SlurmFake
from test_worker import _gate, _header, _job_dir, _no_finish_gc, wenv  # noqa: F401
from waits import WAIT_S

pytestmark = pytest.mark.integration
ME = getpass.getuser()


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = SlurmFake(tmp_path / "slurm").install(monkeypatch)
    f.as_user(ME)                       # the worker's own user (on the box: `nw`)
    ES.forget()
    ES._LICENSES.clear()
    ES._LIMITS.clear()
    yield f
    f.close()
    ES.forget()
    ES._LICENSES.clear()
    ES._LIMITS.clear()


def _cfg(env, executor="slurm"):
    return {"data_dir": env["NW_DATA_DIR"], "config_dir": env["NW_CONFIG_DIR"],
            "agentic": {"workers": {"min_free_gb": 0, "abort_free_gb": 0, "executor": executor},
                        "slurm": {"cache_s": 0}}}


def _submit(cfg, job_id, job_class=None, campaign="C-1"):
    h = _header(job_id, "a" * 40, "b" * 40, [_gate("x", "true")], bundle_bytes=0,
                campaign=campaign)
    h["payload"]["resources"] = {"cpus": 2, "mem_mb": 1024, "licenses": {}, "time_s": 60,
                                 "gates": {}}
    if job_class:
        h["payload"]["job_class"] = job_class
    out = io.BytesIO()
    assert WJ.submit(cfg, io.BytesIO((json.dumps(h) + "\n").encode()), out) == 0, \
        out.getvalue()
    return json.loads((_job_dir_of(cfg, job_id) / "job.json").read_text())["slurm_job_id"]


def _job_dir_of(cfg, job_id):
    return W.jobs_dir(cfg) / job_id


def _pending(fake, jid, reason="Resources"):
    fake.pending(jid, reason)


# ---- worker side -------------------------------------------------------------------------------

def test_the_list_shows_this_worker_s_nw_jobs_with_reason_and_class(wenv, fake):
    cfg = _cfg(wenv)
    a = _submit(cfg, "C-1-gate-001-a1", "campaign")
    b = _submit(cfg, "C-2-persona-r1-a1", "persona", campaign="C-2")
    _pending(fake, a, "Resources")
    _pending(fake, b, "Priority")
    subprocess.run(["sbatch", "--parsable", "--job-name=not-ours", "--wrap", "true"],
                   check=True, capture_output=True, timeout=WAIT_S)       # no job dir here
    rc, rec = SG.listing(cfg)
    assert rc == 0 and rec["schema"] == SG.LIST_SCHEMA and rec["ok"] is True
    rows = {r["job_id"]: r for r in rec["jobs"]}
    assert set(rows) == {"C-1-gate-001-a1", "C-2-persona-r1-a1"}
    assert rows["C-1-gate-001-a1"]["class"] == "campaign"
    assert rows["C-1-gate-001-a1"]["partition"] == "nw-campaign"
    assert rows["C-1-gate-001-a1"]["reason"] == "Resources"
    assert rows["C-2-persona-r1-a1"]["reason"] == "Priority"
    assert rows["C-2-persona-r1-a1"]["slurm_job_id"] == b
    assert rows["C-1-gate-001-a1"]["cpus"] == 2 and rows["C-1-gate-001-a1"]["mem_mb"] == 1024
    assert ["-u", ME] == fake.calls("squeue")[-1][1:3]


def test_another_user_s_job_is_never_listed_nor_governed(wenv, fake):
    cfg = _cfg(wenv)
    fake.as_user("mallory")
    jid = _submit(cfg, "C-1-gate-001-a1")
    _pending(fake, jid)
    assert SG.listing(cfg)[1]["jobs"] == []
    for job in ("C-1-gate-001-a1", str(jid)):
        rc, rec = SG.control(cfg, "hold", job)
        assert rc == SG.EXIT_REFUSED and rec["ok"] is False and "not an nw job" in rec["error"]
    assert not [c for c in fake.calls("scontrol") if c[0] == "hold"]


def test_a_slurm_job_without_an_nw_job_dir_is_refused(wenv, fake):
    cfg = _cfg(wenv)
    p = subprocess.run(["sbatch", "--parsable", "--job-name=C-1-gate-001-a1", "--wrap", "true"],
                       check=True, capture_output=True, text=True, timeout=WAIT_S)
    rc, rec = SG.control(cfg, "hold", p.stdout.strip())
    assert rc == SG.EXIT_REFUSED and "not an nw job of this worker" in rec["error"]
    rc, rec = SG.control(cfg, "hold", "C-9-gate-001-a1")
    assert rc == SG.EXIT_REFUSED and "no Slurm job" in rec["error"]


@pytest.mark.parametrize("verb,arg,argv", [
    ("hold", None, ["hold", "{id}"]),
    ("release", None, ["release", "{id}"]),
    ("top", None, ["top", "{id}"]),
    ("nice", 500, ["update", "JobId={id}", "Nice=500"]),
    ("class", "bulk", ["update", "JobId={id}", "Partition=nw-bulk"]),
])
def test_each_verb_runs_its_scontrol_argv(wenv, fake, verb, arg, argv):
    cfg = _cfg(wenv)
    jid = _submit(cfg, "C-1-gate-001-a1", "campaign")
    _pending(fake, jid)
    rc, rec = SG.control(cfg, verb, "C-1-gate-001-a1", arg)
    assert rc == 0 and rec["ok"] is True, rec
    want = [a.format(id=jid) for a in argv]
    assert rec["argv"] == ["scontrol", *want] and fake.calls("scontrol")[-1] == want
    assert rec["slurm_job_id"] == jid and rec["job_id"] == "C-1-gate-001-a1"


def test_hold_release_class_change_slurm_s_view(wenv, fake):
    cfg = _cfg(wenv)
    jid = _submit(cfg, "C-1-gate-001-a1", "gatejob")
    _pending(fake, jid)
    assert SG.control(cfg, "hold", str(jid))[0] == 0              # by Slurm id
    assert fake.job(jid)["reason"] == "JobHeldUser"
    assert SG.control(cfg, "release", "C-1-gate-001-a1")[0] == 0
    assert fake.job(jid)["held"] is False
    assert SG.control(cfg, "class", "C-1-gate-001-a1", "bulk")[0] == 0
    assert fake.job(jid)["partition"] == "nw-bulk"
    rec = json.loads((_job_dir_of(cfg, "C-1-gate-001-a1") / "job.json").read_text())
    assert rec["job_class"] == "bulk" and rec["slurm_partition"] == "nw-bulk"
    row = SG.listing(cfg)[1]["jobs"][0]
    assert row["class"] == "bulk" and row["partition"] == "nw-bulk"


def test_requeue_a_running_job_and_class_only_while_pending(wenv, fake):
    cfg = _cfg(wenv)
    jid = _submit(cfg, "C-1-gate-001-a1", "bulk")
    fake._set(jid, state="RUNNING", reason="None")
    rc, rec = SG.control(cfg, "class", "C-1-gate-001-a1", "campaign")
    assert rc == SG.EXIT_REFUSED and "only a pending job" in rec["error"]
    assert SG.control(cfg, "requeue", "C-1-gate-001-a1")[0] == 0
    assert fake.job(jid)["state"] == "PENDING" and fake.job(jid)["restart_cnt"] == 1


def test_slurm_s_refusal_is_reported(wenv, fake):
    cfg = _cfg(wenv)
    jid = _submit(cfg, "C-1-gate-001-a1")
    _pending(fake, jid)
    fake.deny("top", "Access/permission denied")
    rc, rec = SG.control(cfg, "top", "C-1-gate-001-a1")
    assert rc == SG.EXIT_SLURM and "permission denied" in rec["error"]


def test_a_transient_eagain_is_retried(wenv, fake, monkeypatch):
    """The box's Slurm refuses a user's partition change with "Resource temporarily
    unavailable" for ~20 s after a submit or an update, then accepts it."""
    monkeypatch.setattr(SG, "EAGAIN_SLEEP_S", 0.01)
    cfg = _cfg(wenv)
    jid = _submit(cfg, "C-1-gate-001-a1")
    _pending(fake, jid)
    fake.deny("update", "Resource temporarily unavailable", times=2)
    rc, rec = SG.control(cfg, "class", "C-1-gate-001-a1", "bulk")
    assert rc == 0 and fake.job(jid)["partition"] == "nw-bulk"
    assert len([c for c in fake.calls("scontrol") if c[0] == "update"]) == 3
    monkeypatch.setattr(SG, "EAGAIN_RETRY_S", 0.0)
    fake.deny("update", "Resource temporarily unavailable")
    rc, rec = SG.control(cfg, "class", "C-1-gate-001-a1", "campaign")
    assert rc == SG.EXIT_SLURM and "temporarily unavailable" in rec["error"]


def test_bad_arguments_are_refused_before_slurm(wenv, fake):
    cfg = _cfg(wenv)
    for verb, arg in (("nice", -1), ("nice", 10001), ("nice", None), ("class", "vip"),
                      ("scancel", None)):
        rc, rec = SG.control(cfg, verb, "C-1-gate-001-a1", arg)
        assert rc == SG.EXIT_REFUSED and rec["ok"] is False
    assert fake.calls("scontrol") == [] and fake.calls("squeue") == []


def test_a_local_executor_answers_executor_is_local(wenv, fake):
    cfg = _cfg(wenv, executor="local")
    rc, rec = SG.listing(cfg)
    assert rc == SG.EXIT_REFUSED and rec["error"] == "executor is local"
    rc, rec = SG.control(cfg, "hold", "C-1-gate-001-a1")
    assert rc == SG.EXIT_REFUSED and rec["error"] == "executor is local"
    assert fake.calls() == []


def test_the_worker_cli_prints_one_json_line(wenv, fake):
    env = {**fake.env(wenv), "NW_CONFIG_DIR": wenv["NW_CONFIG_DIR"]}
    (__import__("pathlib").Path(wenv["NW_CONFIG_DIR"]) / "config.yaml").write_text(
        "agentic:\n  workers:\n    executor: slurm\n    min_free_gb: 0\n    abort_free_gb: 0\n")
    p = subprocess.run([sys.executable, "-m", "nightwright", "worker", "slurm-jobs", "--json"],
                       env=env, capture_output=True, text=True, timeout=WAIT_S * 4)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["schema"] == SG.LIST_SCHEMA
    p = subprocess.run([sys.executable, "-m", "nightwright", "worker", "slurm-ctl", "hold",
                        "C-9-gate-001-a1"], env=env, capture_output=True, text=True,
                       timeout=WAIT_S * 4)
    assert p.returncode == SG.EXIT_REFUSED and json.loads(p.stdout)["ok"] is False


# ---- the forced-command grammar --------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "nw worker slurm-jobs", "nw worker slurm-jobs --json",
    "nw worker slurm-ctl hold C-1-gate-001-a1", "nw worker slurm-ctl release 123",
    "nw worker slurm-ctl top GATEJOB-0001-gate-000-t1", "nw worker slurm-ctl requeue 9",
    "nw worker slurm-ctl cancel C-1-gate-001-a1",
    "nw worker slurm-ctl nice C-1-gate-001-a1 0", "nw worker slurm-ctl nice 7 10000",
    "nw worker slurm-ctl class C-1-gate-001-a1 bulk",
    "/home/nw/.local/bin/nw worker slurm-ctl class 5 campaign"])
def test_the_grammar_allows_the_table(cmd):
    assert W.check_ssh_command(cmd) == shlex.split(cmd)[2:]


@pytest.mark.parametrize("cmd", [
    "nw worker slurm-jobs --all", "nw worker slurm-jobs -u root",
    "nw worker slurm-ctl", "nw worker slurm-ctl hold", "nw worker slurm-ctl cancel 5 6",
    "nw worker slurm-ctl scancel 5", "nw worker slurm-ctl update 5",
    "nw worker slurm-ctl hold 5 6", "nw worker slurm-ctl hold --help",
    "nw worker slurm-ctl hold 'a;b'", "nw worker slurm-ctl nice 5", "nw worker slurm-ctl nice 5 -1",
    "nw worker slurm-ctl nice 5 10001", "nw worker slurm-ctl nice 5 1e3",
    "nw worker slurm-ctl nice 5 000000001", "nw worker slurm-ctl class 5 vip",
    "nw worker slurm-ctl class 5", "nw worker slurm-ctl class 5 bulk Partition=x",
    "nw worker slurm-ctl top 5 Priority=1"])
def test_the_grammar_refuses_anything_else(cmd):
    with pytest.raises(ValueError):
        W.check_ssh_command(cmd)


# ---- coordinator side ----------------------------------------------------------------------------

@pytest.fixture
def coord(wenv, fake, monkeypatch, tmp_path):
    """The coordinator's cfg; its SSH calls run the worker's commands in-process, each argv
    checked by the forced-command grammar first."""
    wcfg = _cfg(wenv)
    ccfg = {"data_dir": str(tmp_path / "coord"), "config_dir": str(tmp_path / "coordc")}
    calls = []

    def call(w, *args, timeout=None, input=None):
        argv = W.check_ssh_command("nw worker " + shlex.join(str(a) for a in args))
        calls.append(argv)
        if argv[0] == "slurm-jobs":
            rc, rec = SG.listing(wcfg)
            return rc, [rec], b""
        if argv[0] == "slurm-ctl":
            arg = argv[3] if len(argv) > 3 else None
            rc, rec = SG.control(wcfg, argv[1], argv[2],
                                 int(arg) if argv[1] == "nice" else arg)
            return rc, [rec], b""
        if argv[0] == "cancel":
            out = io.BytesIO()
            rc = WJ.cancel(wcfg, argv[1], out)
            return rc, [json.loads(ln) for ln in out.getvalue().splitlines()], b""
        raise AssertionError(argv)

    monkeypatch.setattr(QJ.RG, "_call", call)
    monkeypatch.setattr(QJ.WK, "load", lambda cfg: [{"name": "box", "mode": "ssh"},
                                                    {"name": "old", "mode": "ssh"}])
    monkeypatch.setattr(QJ.WK, "runs_slurm", lambda cfg, name: name == "box")
    return types_ns(wcfg=wcfg, cfg=ccfg, calls=calls, fake=fake)


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


def _events(cfg):
    return E.read_since(cfg, 0, types=["queue_job_governed"])


def test_resolution_by_nw_id_campaign_gatejob_and_slurm_id(coord):
    a = _submit(coord.wcfg, "C-1-gate-001-a1", "campaign")
    b = _submit(coord.wcfg, "GATEJOB-0007-gate-000-t1700000000", "gatejob",
                campaign="GATEJOB-0007")
    c = _submit(coord.wcfg, "C-2-persona-r9-a1", "persona", campaign="C-2")
    for jid in (a, b, c):
        coord.fake.pending(jid, "Resources")
    for job, want in (("C-1-gate-001-a1", a), ("C-1", a), ("GATEJOB-0007", b), (str(c), c),
                      ("C-2", c)):
        rec = QJ.govern(coord.cfg, "hold", job)
        assert rec["slurm_id"] == want and rec["worker"] == "box", job
        assert coord.fake.calls("scontrol")[-1] == ["hold", str(want)]
    # only the Slurm worker was asked (`old` reports no executor:slurm)
    assert all(argv[0] in ("slurm-jobs", "slurm-ctl") for argv in coord.calls)


def test_unknown_and_ambiguous_ids_are_refused(coord):
    for jid in (_submit(coord.wcfg, "C-1-gate-001-a1"), _submit(coord.wcfg, "C-1-gate-002-a1")):
        coord.fake.pending(jid)
    with pytest.raises(QJ.GovernError, match="ambiguous"):
        QJ.govern(coord.cfg, "hold", "C-1")
    with pytest.raises(QJ.GovernError, match="no live nw Slurm job"):
        QJ.govern(coord.cfg, "hold", "C-9")
    with pytest.raises(QJ.GovernError, match="no live nw Slurm job"):
        QJ.govern(coord.cfg, "hold", "C-1-gate-00")    # a prefix must end at a `-`
    assert _events(coord.cfg) == []


@pytest.mark.parametrize("verb,kw,scontrol", [
    ("hold", {}, ["hold", "{id}"]), ("release", {}, ["release", "{id}"]),
    ("top", {}, ["top", "{id}"]), ("bump", {"down": 250}, ["update", "JobId={id}", "Nice=250"]),
    ("bump", {}, ["update", "JobId={id}", "Nice=1000"]),
    ("class", {"cls": "bulk"}, ["update", "JobId={id}", "Partition=nw-bulk"])])
def test_each_verb_appends_queue_job_governed(coord, verb, kw, scontrol):
    jid = _submit(coord.wcfg, "C-1-gate-001-a1", "campaign")
    coord.fake.pending(jid)
    rec = QJ.govern(coord.cfg, verb, "C-1", by="alice", **kw)
    assert coord.fake.calls("scontrol")[-1] == [a.format(id=jid) for a in scontrol]
    (ev,) = _events(coord.cfg)
    assert ev["data"]["job"] == "C-1-gate-001-a1" and ev["data"]["verb"] == verb
    assert ev["data"]["by"] == "alice" and ev["data"]["slurm_id"] == jid
    assert ev["campaign"] == "C-1" and rec["event_id"] == ev["id"]


def test_requeue_and_cancel_append_events_too(coord):
    jid = _submit(coord.wcfg, "C-1-gate-001-a1", "bulk")
    coord.fake._set(jid, state="RUNNING", reason="None")
    QJ.govern(coord.cfg, "requeue", "C-1-gate-001-a1")
    assert coord.fake.job(jid)["restart_cnt"] == 1
    rec = QJ.govern(coord.cfg, "cancel", str(jid))
    assert rec["via"] == "worker cancel"
    assert (_job_dir_of(coord.wcfg, "C-1-gate-001-a1") / "cancel").exists()
    assert coord.fake.calls("scancel")[-1] == [str(jid)]            # pending again: scancel
    assert [e["data"]["verb"] for e in _events(coord.cfg)] == ["requeue", "cancel"]


def test_cancel_of_a_job_nw_finished_but_slurm_requeued_scancels_it(coord):
    """Seen on the box: a requeue TERMs the run, which ends `done` interrupted (result.json),
    and Slurm queues the job again; `nw worker cancel` is then a no-op, so the coordinator
    follows with `slurm-ctl cancel` (scancel)."""
    jid = _submit(coord.wcfg, "C-1-gate-001-a1", "bulk")
    job = _job_dir_of(coord.wcfg, "C-1-gate-001-a1")
    WJ._write_atomic(job / "result.json", {"state": "done", "frame": None, "attempt": 1})
    coord.fake.pending(jid, "BeginTime")
    rec = QJ.govern(coord.cfg, "cancel", "C-1")
    assert rec["via"] == "slurm-ctl cancel" and rec["nw_state"] == "done"
    assert coord.fake.calls("scancel")[-1] == [str(jid)]
    assert coord.fake.job(jid)["state"] == "CANCELLED" and (job / "cancel").exists()
    assert [a[:2] for a in coord.calls if a[0] != "slurm-jobs"] == [
        ["cancel", "C-1-gate-001-a1"], ["slurm-ctl", "cancel"]]


def test_a_gatejob_whose_worker_refuses_cancel_is_stopped_through_its_process(coord, monkeypatch):
    jid = _submit(coord.wcfg, "GATEJOB-0003-gate-000-t1", campaign="GATEJOB-0003")
    coord.fake.pending(jid)
    real = QJ.RG._call

    def refuse_cancel(w, *args, **kw):
        if args[0] == "cancel":
            return 1, [{"type": "status", "state": "running"}], b"a gate-job is cancelled by closing its stream"
        return real(w, *args, **kw)

    monkeypatch.setattr(QJ.RG, "_call", refuse_cancel)
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        from nightwright.agentic import gate_jobs as GJ
        monkeypatch.setattr(GJ, "read", lambda cfg, job_id: {"id": job_id, "status": "running",
                                                             "pid": p.pid})
        rec = QJ.govern(coord.cfg, "cancel", "GATEJOB-0003")
        assert rec["via"] == "gate job process" and rec["pid"] == p.pid
        assert p.wait(timeout=WAIT_S) == -signal.SIGTERM
    finally:
        if p.poll() is None:
            p.kill()
        p.wait(timeout=WAIT_S)
    (ev,) = _events(coord.cfg)
    assert ev["campaign"] is None and ev["data"]["verb"] == "cancel"


def test_bump_up_is_refused(coord):
    with pytest.raises(QJ.GovernError, match="operator"):
        QJ.govern(coord.cfg, "bump", "C-1", down=-5)


def test_no_slurm_worker_says_so(coord, monkeypatch):
    monkeypatch.setattr(QJ.WK, "runs_slurm", lambda cfg, name: False)
    assert QJ.listing(coord.cfg) == {"schema": QJ.SCHEMA, "workers": [], "jobs": []}
    with pytest.raises(QJ.GovernError, match="no Slurm worker"):
        QJ.govern(coord.cfg, "hold", "C-1")


def test_a_named_local_worker_reports_executor_is_local(coord, wenv):
    coord.wcfg["agentic"]["workers"]["executor"] = "local"
    rec = QJ.listing(coord.cfg, "old")
    assert rec["workers"] == [{"name": "old", "ok": False, "error": "executor is local"}]
    with pytest.raises(QJ.GovernError, match="executor is local"):
        QJ.govern(coord.cfg, "hold", "C-1", worker="old")


def _invoke(cfg, args):
    root = click.Group("root")
    grp = click.Group("queue")
    CQJ.attach(grp)
    root.add_command(grp)
    return CliRunner().invoke(root, ["queue", *args], obj=cfg)


def test_the_cli_lists_as_json_and_text_and_acts(coord):
    jid = _submit(coord.wcfg, "C-1-gate-001-a1", "campaign")
    coord.fake.pending(jid, "Priority")
    r = _invoke(coord.cfg, ["jobs", "--json"])
    assert r.exit_code == 0, r.output
    rec = json.loads(r.output)
    assert rec["schema"] == "nw.queue.jobs.v1" and rec["jobs"][0]["reason"] == "Priority"
    assert rec["jobs"][0]["worker"] == "box"
    r = _invoke(coord.cfg, ["jobs"])
    # the test console is narrow: the nw id folds onto a second line, never cut
    assert r.exit_code == 0 and "C-1-gate-0" in r.output and "01-a1" in r.output, r.output
    assert "Priority" in r.output and "…" not in r.output.split("\n")[5].split("│")[1]
    r = _invoke(coord.cfg, ["jobs", "class", "C-1", "bulk"])
    assert r.exit_code == 0 and "class bulk" in r.output, r.output
    r = _invoke(coord.cfg, ["jobs", "bump", "C-1", "--down", "10", "--json"])
    assert r.exit_code == 0 and json.loads(r.output)["nice"] == 10
    r = _invoke(coord.cfg, ["jobs", "hold", "C-9"])
    assert r.exit_code == 1 and "no live nw Slurm job" in r.output
