"""SPEC-0505: a gate run that ran on the coordinator records why (`GateRun.placement`, the
`gate` event's `placement`, `nw campaign show`). One test per reason code, on SPEC-0338's
loopback worker (`remote_support.Remote`) or scripted `gate-job` frames."""
import pytest

from nightwright.agentic import gates as G
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import store as S
from nightwright.agentic import timeline as TL

from remote_support import CID, Remote, World, _gate
from remote_support import _short_runtime_dir  # noqa: F401  (autouse fixture)
from test_remote_gates import _error_frame, _fake_gate_job

pytestmark = pytest.mark.remote

LEDGER_RUN = dict(by="test")


def _gate_lines(story):
    return [ln for lp in story["loops"] for g in lp["groups"] for ln in g["lines"]
            if ln["kind"] == "gate"]


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    w.project["gates"] = [_gate("ok", "echo ok")]
    return w


@pytest.fixture
def remote(world):
    r = Remote(world)
    r.register()
    return r


def _check(world, run, code, **expect):
    assert run.worker is None and run.ok
    pl = run.placement
    assert pl["where"] == "local" and pl["reason"] == code and code in G.LOCAL_REASONS, pl
    for k, v in expect.items():
        assert pl[k] == v, pl
    assert G.as_payload(run)["placement"] == pl
    return pl


def test_requested_local(world, remote):
    run = G.run_all(world.project, world.state, world.cfg, seq=1, local=True)
    _check(world, run, "requested_local")


def test_ineligible_untracked_file(world, remote):
    (world.repo / "untracked.txt").write_text("u\n")
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    pl = _check(world, run, "ineligible")
    assert "untracked" in pl["detail"]


def test_ineligible_project_opt_out(world, remote):
    world.project["remote_gates"] = False
    pl = _check(world, G.run_all(world.project, world.state, world.cfg, seq=1), "ineligible")
    assert "remote_gates: false" in pl["detail"]


@pytest.mark.parametrize("registered", [True, False])
def test_no_placeable_worker(world, remote, registered):
    if registered:
        remote.register(enabled=False)
    else:
        from nightwright.agentic import workers as WK
        WK.path(world.cfg).unlink()
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    _check(world, run, "no_placeable_worker")


def test_no_placeable_worker_overflow_with_remote_pools(world, remote, monkeypatch):
    """Remote pools were offered but the slot picker chose the local one (SPEC-0374)."""
    from nightwright.agentic import gate_slots as GS
    real = GS.acquire
    from nightwright.agentic import workers as WK

    def acquire(*a, **kw):
        kw["pools"] = [(WK.LOCAL, 2)]
        return real(*a, **kw)
    monkeypatch.setattr(GS, "acquire", acquire)
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    pl = _check(world, run, "no_placeable_worker")
    assert "cooled down" in pl["detail"]


def test_transport(world, remote):
    remote.register(ssh=remote.variants["unreachable"])
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    pl = _check(world, run, "transport", worker="box")
    assert pl["stage"] and pl["detail"]


def test_tree(world, remote):
    world.project["gates"] = [_gate("regen", "printf 'regen\\n' > a.txt")]
    pl = _check(world, G.run_all(world.project, world.state, world.cfg, seq=1), "tree",
                stage="tree", worker="box")
    assert "a.txt" in pl["detail"]


def test_rejected(world, remote):
    _fake_gate_job(remote, "rej", [_error_frame("validate")])
    pl = _check(world, G.run_all(world.project, world.state, world.cfg, seq=1), "rejected",
                worker="box")
    assert pl["stage"] == "validate"


def _lost(world):
    return {"job_id": f"{CID}-gate-001-a1", "attempt": 1, "worker": "box", "seq": 1,
            "excluded_workers": []}


def test_retries_exhausted(world, remote):
    world.cfg["agentic"]["workers"] = {"max_remote_attempts": 1}
    run = G.run_all(world.project, world.state, world.cfg, seq=1, lost=_lost(world))
    _check(world, run, "retries_exhausted", worker="box")


def test_no_retry_pool(world, remote, monkeypatch):
    monkeypatch.setattr(RG, "retry_pools", lambda *a, **k: None)
    run = G.run_all(world.project, world.state, world.cfg, seq=1, lost=_lost(world))
    _check(world, run, "no_retry_pool", worker="box")


def test_adopt_fell_back(world, remote, monkeypatch):
    monkeypatch.setattr(RG, "adopt", lambda *a, **k: (RG.Outcome(None), 0.0))
    run = G.run_all(world.project, world.state, world.cfg, seq=1,
                    adopt={"job_id": f"{CID}-gate-001-a1", "worker": "box"})
    _check(world, run, "adopt_fell_back", worker="box")


def test_a_remote_run_records_where_remote(world, remote):
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert run.worker == "box"
    assert G.as_payload(run)["placement"] == {"where": "remote", "worker": "box"}


def test_the_gate_event_and_the_story_carry_it(world, remote):
    remote.register(enabled=False)
    G.run_and_log(world.cfg, world.state, world.project, **LEDGER_RUN)
    (ev,) = world.ledger(["gate"])
    assert ev["placement"]["reason"] == "no_placeable_worker"
    story = TL.story(S.read_ledger(world.cfg, CID), campaign=CID,
                     state=S.read_state(world.cfg, CID), now=S.now_iso())
    lines = _gate_lines(story)
    assert lines and lines[0]["placement"]["reason"] == "no_placeable_worker"
    assert "· local: no_placeable_worker (no remote worker" in lines[0]["text"]


def test_no_fixture_event_is_unknown(world, remote):
    for local in (True, False):
        G.run_and_log(world.cfg, world.state, world.project, local=local, **LEDGER_RUN)
    for ev in world.ledger(["gate"]):
        assert ev["placement"].get("reason") != "unknown"


def test_a_legacy_event_renders_without_a_suffix():
    ev = {"seq": 1, "type": "gate", "gate_seq": 1, "ok": True, "duration_s": 1.0, "results": []}
    story = TL.story([ev], campaign=CID, state={"id": CID}, now="2026-11-09T10:00:00-04:00")
    lines = _gate_lines(story)
    assert lines and "local:" not in lines[0]["text"] and "placement" not in lines[0]
