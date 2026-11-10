"""SPEC-0514: the sacct half, against the fake Slurm CLI in both accounting modes. Accounting
off (the box today): `sacct` is never called, no `--wckey`. On: `--wckey=<project>` on submit, the
`slurm` block (State, Elapsed, TotalCPU, MaxRSS folded over steps, NodeList, ExitCode) in
`nw worker poll`, kept in the job dir, merged into the gate records and the job record once
(one retry while `sacct` has no row yet)."""
import io
import json

import pytest

from nightwright.agentic import executor_slurm as ES
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import remote_jobs as RJ
from nightwright.agentic import worker_job as WJ
from slurm_fake import SlurmFake

pytestmark = pytest.mark.fast

ROWS = ("100|COMPLETED|00:02:43|03:38.900|0|morais-Desktop|0:0",
        "100.batch|COMPLETED|00:02:43|03:38.900|143288K|morais-Desktop|0:0",
        "100.0|COMPLETED|00:02:40|03:30.000|1.5G|morais-Desktop|0:0")


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = SlurmFake(tmp_path / "slurm").install(monkeypatch)
    ES._ACCT_ON.clear()
    ES.forget()
    yield f
    f.close()
    ES._ACCT_ON.clear()
    ES.forget()


def _sett():
    return ES.settings({"agentic": {"slurm": {"cache_s": 0}}})


def _h(project="Meta-Tools"):
    payload = {"seq": 1, "gates": [], "resources": {"cpus": 1, "mem_mb": 1024, "licenses": {},
                                                    "time_s": 60, "gates": {}}}
    if project:
        payload["project"] = project
    return {"job_id": "J-1", "kind": "gate", "campaign": "C", "attempt": 0, "payload": payload}


def _argv(tmp_path):
    return ES.sbatch_argv({}, _sett(), tmp_path, _h(), ["nw"])[0]


def test_accounting_off_no_wckey_and_sacct_never_called(fake, tmp_path):
    assert not ES.accounting_on(_sett())
    assert not any(a.startswith("--wckey") for a in _argv(tmp_path))
    job = tmp_path / "job"
    job.mkdir()
    (job / "job.json").write_text(json.dumps({"slurm_job_id": 100}))
    assert WJ._acct_extra({}, job) == {}
    assert ES.accounting_for({}, job) is None
    assert fake.calls("sacct") == []


def test_accounting_on_submits_with_the_project_wckey(fake, tmp_path):
    fake.accounting()
    assert "--wckey=Meta-Tools" in _argv(tmp_path)
    argv = ES.sbatch_argv({}, _sett(), tmp_path, _h(None), ["nw"])[0]
    assert not any(a.startswith("--wckey") for a in argv)
    bad = ES.sbatch_argv({}, _sett(), tmp_path, _h("bad name;rm"), ["nw"])[0]
    assert not any(a.startswith("--wckey") for a in bad)


def test_acct_block_folds_steps_and_poll_reports_it(fake, tmp_path):
    fake.accounting()
    jid = 100
    fake._edit(lambda st: st["jobs"].__setitem__(str(jid), {"name": "J-1", "state": "COMPLETED",
                                                           "acct": list(ROWS)}))
    blk = ES.acct_block(_sett(), jid)
    assert blk == {"JobID": 100, "State": "COMPLETED", "Elapsed": "00:02:43",
                   "TotalCPU": "03:38.900", "MaxRSS": "1536M", "MaxRSS_mb": 1536.0,
                   "NodeList": "morais-Desktop", "ExitCode": "0:0"}
    job = tmp_path / "job"
    job.mkdir()
    (job / "job.json").write_text(json.dumps({"slurm_job_id": jid}))
    assert WJ._acct_extra({}, job) == {"slurm": blk}
    assert json.loads((job / ES.ACCT_FILE).read_text()) == blk
    n = len(fake.calls("sacct"))
    assert WJ._acct_extra({}, job) == {"slurm": blk} and len(fake.calls("sacct")) == n


def test_poll_says_pending_while_sacct_has_no_row(fake, tmp_path):
    fake.accounting()
    job = tmp_path / "job"
    job.mkdir()
    (job / "job.json").write_text(json.dumps({"slurm_job_id": 555}))
    assert WJ._acct_extra({}, job) == {"slurm_pending": True}


def test_a_local_job_asks_nothing(fake, tmp_path):
    fake.accounting()
    job = tmp_path / "job"
    job.mkdir()
    (job / "job.json").write_text("{}")
    assert WJ._acct_extra({}, job) == {} and fake.calls("sacct") == []


def _record(gates_dir, name, **extra):
    gates_dir.mkdir(parents=True, exist_ok=True)
    (gates_dir / f"001-{name}.json").write_text(json.dumps(
        {"name": name, "resources": {"request": {}, "used": {}}, **extra}))
    (gates_dir / f"001-{name}.report.json").write_text("{}")


def test_merge_slurm_stamps_every_gate_record(tmp_path):
    gd = tmp_path / "gates"
    _record(gd, "a")
    _record(gd, "b")
    (gd / "001-c.json").write_text(json.dumps({"name": "c"}))      # no resources: left alone
    RG.merge_slurm(gd, 1, (), {"State": "COMPLETED"})
    assert json.loads((gd / "001-a.json").read_text())["resources"]["slurm"] == \
        {"State": "COMPLETED"}
    assert "resources" not in json.loads((gd / "001-c.json").read_text())
    assert (gd / "001-a.report.json").read_text() == "{}"


def test_accounting_merges_after_done_and_retries_once(tmp_path, monkeypatch):
    gd = tmp_path / "gates"
    _record(gd, "a")
    seen = []
    answers = [{"slurm_pending": True}, {"slurm": {"State": "COMPLETED", "MaxRSS": "9M"}}]
    monkeypatch.setattr(RG, "_poll_status", lambda w, j: seen.append(j) or answers.pop(0))
    monkeypatch.setattr(RG.time, "sleep", lambda s: seen.append(("sleep", s)))
    upd = []
    monkeypatch.setattr(RJ, "update", lambda cfg, jid, **f: upd.append((jid, f)))
    blk = RG.accounting({}, {"name": "w"}, "J-1", {"slurm_job_id": 100}, gd, 1, ())
    assert blk["MaxRSS"] == "9M"
    assert seen == ["J-1", ("sleep", RG.SACCT_RETRY_S), "J-1"]
    assert upd == [("J-1", {"slurm": blk})]
    assert json.loads((gd / "001-a.json").read_text())["resources"]["slurm"] == blk


def test_accounting_gives_up_after_one_retry_and_skips_local_jobs(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(RG, "_poll_status",
                        lambda w, j: calls.append(j) or {"slurm_pending": True})
    monkeypatch.setattr(RG.time, "sleep", lambda s: None)
    assert RG.accounting({}, {}, "J", {"slurm_job_id": 1}, tmp_path, 1, ()) is None
    assert len(calls) == 2
    calls.clear()
    assert RG.accounting({}, {}, "J", {"ok": True}, tmp_path, 1, ()) is None and calls == []
