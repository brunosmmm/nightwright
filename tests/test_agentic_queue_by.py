"""SPEC-0273: `nw queue park|resume|abandon|prioritize` record the real actor (`--by`, else
$NW_MAILBOX_ADDR, else human) on every ledger event they write."""
import json

import pytest

from test_agentic_e2e import added, impl_done, make_cfg, make_repo, run_cli, script
from test_agentic_nonblocking import HeldRunner, _settle
from nightwright.agentic import events as E
from nightwright.agentic import lease as L
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S

pytestmark = pytest.mark.fast


@pytest.fixture(autouse=True)
def _no_addr(monkeypatch):
    monkeypatch.delenv("NW_MAILBOX_ADDR", raising=False)


def _world(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    return cfg, added(cfg, monkeypatch)


def _since(cfg, cid, n):
    return S.read_ledger(cfg, cid)[n:]


def test_park_resume_abandon_record_by(tmp_path, monkeypatch):
    cfg, cid = _world(tmp_path, monkeypatch)
    n = len(S.read_ledger(cfg, cid))
    r = run_cli(cfg, monkeypatch, ["queue", "park", cid, "--reason", "x", "--by", "conductor",
                                   "--decision", "test hold", "--rule", "message.request", "--yes", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["by"] == "conductor"
    new = _since(cfg, cid, n)
    # the decision record's ledger note (SPEC-0318) is authored `agent:conductor`
    acts = [e for e in new if "decision" not in e]
    assert acts and {e["by"] for e in acts} == {"conductor"}, new
    assert any("decision" in e and e["by"] == "agent:conductor" for e in new), new
    assert S.read_state(cfg, cid)["state"] == "PARKED"

    n = len(S.read_ledger(cfg, cid))
    r = run_cli(cfg, monkeypatch, ["queue", "resume", cid, "--by", "conductor", "--yes"])
    assert r.exit_code == 0, r.output
    new = _since(cfg, cid, n)
    assert {"resume", "transition"} <= {e["type"] for e in new}
    assert {e["by"] for e in new} == {"conductor"}, new

    run_cli(cfg, monkeypatch, ["queue", "park", cid, "--reason", "y", "--yes"])
    n = len(S.read_ledger(cfg, cid))
    r = run_cli(cfg, monkeypatch, ["queue", "abandon", cid, "--by", "steward:Meta-Tools",
                                   "--decision", "drop it", "--rule", "message.request",
                                   "--kind", "abandon", "--yes", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["by"] == "steward:Meta-Tools"
    new = _since(cfg, cid, n)
    acts = [e for e in new if "decision" not in e]
    assert acts and {e["by"] for e in acts} == {"steward:Meta-Tools"}, new
    assert any("decision" in e and e["by"] == "agent:steward:Meta-Tools" for e in new), new
    assert S.read_state(cfg, cid)["state"] == "ABANDONED"


def test_prioritize_records_by_from_env(tmp_path, monkeypatch):
    cfg, cid = _world(tmp_path, monkeypatch)
    monkeypatch.setenv("NW_MAILBOX_ADDR", "conductor")
    n = len(S.read_ledger(cfg, cid))
    r = run_cli(cfg, monkeypatch, ["queue", "prioritize", cid, "2", "--yes", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["by"] == "conductor"
    new = _since(cfg, cid, n)
    assert new and {e["by"] for e in new} == {"conductor"}, new
    monkeypatch.setenv("NW_MAILBOX_ADDR", "steward:Meta-Tools")
    n = len(S.read_ledger(cfg, cid))
    run_cli(cfg, monkeypatch, ["queue", "prioritize", cid, "3", "--yes"])
    assert {e["by"] for e in _since(cfg, cid, n)} == {"steward:Meta-Tools"}


def test_default_by_is_human(tmp_path, monkeypatch):
    cfg, cid = _world(tmp_path, monkeypatch)
    n = len(S.read_ledger(cfg, cid))
    r = run_cli(cfg, monkeypatch, ["queue", "prioritize", cid, "2", "--yes", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["by"] == "human"
    new = _since(cfg, cid, n)
    assert new and {e["by"] for e in new} == {"human"}, new


@pytest.mark.parametrize("cmd", [["park", "--reason", "x"], ["prioritize", "2"], ["resume"],
                                 ["abandon"]])
def test_invalid_by_refused(tmp_path, monkeypatch, cmd):
    cfg, cid = _world(tmp_path, monkeypatch)
    run_cli(cfg, monkeypatch, ["queue", "park", cid, "--reason", "setup", "--yes"])
    before = S.read_ledger(cfg, cid)
    argv = ["queue", cmd[0], cid, *cmd[1:], "--yes"]
    r = run_cli(cfg, monkeypatch, argv + ["--by", "nobody"])
    assert r.exit_code == 1 and "unknown address" in r.output, r.output
    assert S.read_ledger(cfg, cid) == before
    monkeypatch.setenv("NW_MAILBOX_ADDR", "steward:NoSuchProject")
    r = run_cli(cfg, monkeypatch, argv)
    assert r.exit_code == 1 and "unknown steward project" in r.output, r.output
    assert S.read_ledger(cfg, cid) == before
    assert S.read_state(cfg, cid)["state"] == "PARKED"


def test_resume_withdrawal_records_by(tmp_path, monkeypatch):
    cfg, cid = _world(tmp_path, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "escalate", cid, "--question", "ship?",
                                   "--option", "yes", "--from", "conductor", "--yes"])
    assert r.exit_code == 0, r.output
    assert S.read_state(cfg, cid)["park"]["reason"] == "human_decision"
    n = len(S.read_ledger(cfg, cid))
    r = run_cli(cfg, monkeypatch, ["queue", "resume", cid, "--by", "conductor",
                                   "--decision", "answered", "--rule", "message.request", "--yes"])
    assert r.exit_code == 0, r.output
    notes = [e for e in _since(cfg, cid, n) if e["type"] == "note"]
    [w] = [e for e in notes if "withdrawn" in e["text"]]
    assert w["by"] == "conductor" and w["author"] == "conductor"
    assert "campaign resumed by conductor" in w["text"]
    [ev] = E.read_since(cfg, 0, types=["escalation_withdrawn"])
    assert ev["by"] == "conductor"


def test_abandon_withdrawal_records_by(tmp_path, monkeypatch):
    cfg, cid = _world(tmp_path, monkeypatch)
    run_cli(cfg, monkeypatch, ["queue", "escalate", cid, "--question", "ship?",
                               "--from", "conductor", "--yes"])
    n = len(S.read_ledger(cfg, cid))
    r = run_cli(cfg, monkeypatch, ["queue", "abandon", cid, "--by", "conductor",
                                   "--decision", "drop it", "--rule", "message.request", "--yes"])
    assert r.exit_code == 0, r.output
    [w] = [e for e in _since(cfg, cid, n) if e["type"] == "note" and "withdrawn" in e["text"]]
    assert w["by"] == "conductor" and "campaign abandoned by conductor" in w["text"]


def test_deferred_park_keeps_requester(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    runner = HeldRunner(cfg, "SPEC-0001")
    pool = Q.StepPool()
    lease = L.acquire(cfg, ["nw", "queue", "run"])
    try:
        cid = added(cfg, monkeypatch)
        Q.tick(cfg, runner=runner, pool=pool)
        Q.tick(cfg, runner=runner, pool=pool)                 # implementer in flight
        r = run_cli(cfg, monkeypatch, ["queue", "park", cid, "--reason", "later", "--by",
                                       "conductor", "--decision", "test hold", "--rule", "message.request", "--yes", "--json"])
        assert r.exit_code == 0, r.output
        assert json.loads(r.output)["mode"] == "deferred"
        st = S.read_state(cfg, cid)
        assert st["park_requested"]["by"] == "conductor"
        [note] = [e for e in S.read_ledger(cfg, cid, types=["note"])
                  if "park requested" in e["text"]]
        assert note["author"] == "conductor" and note["by"] == "conductor"
        runner.release.set()
        _settle(pool, cid)
        Q.tick(cfg, runner=runner, pool=pool)
        assert S.read_state(cfg, cid)["state"] == "PARKED"
        [park] = S.read_ledger(cfg, cid, types=["park"])
        assert park["by"] == "conductor", park
    finally:
        runner.release.set()
        pool.shutdown()
        lease.release()


def test_queue_add_records_by(tmp_path, monkeypatch):
    """SPEC-0320: `nw queue add --by conductor` attributes the campaign's opening events to
    the conductor (a plain, unprefixed command that the allow rules match)."""
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    cid = added(cfg, monkeypatch, "SPEC-0001", "--by", "conductor")
    assert {e["by"] for e in S.read_ledger(cfg, cid)} == {"conductor"}


def test_queue_add_by_defaults_to_env_then_human(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    monkeypatch.setenv("NW_MAILBOX_ADDR", "steward:Meta-Tools")
    cid = added(cfg, monkeypatch)
    assert {e["by"] for e in S.read_ledger(cfg, cid)} == {"steward:Meta-Tools"}


def test_queue_add_rejects_bad_by(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--by", "no such", "--yes"])
    assert r.exit_code != 0

