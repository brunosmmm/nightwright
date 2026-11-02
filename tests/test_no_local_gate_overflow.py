"""SPEC-0374: a gate never overflows to the local pool while a worker is placeable; a waiter
re-evaluates placement; `agentic.gates.local_overflow` restores the old overflow."""
import json
import threading

import pytest

from nightwright.agentic import gate_slots as GS
from nightwright.agentic import workers as WK
from waits import join, until

FUTURE = "2999-01-01T00:00:00+00:00"


@pytest.fixture
def cfg(tmp_path):
    return {"data_dir": str(tmp_path / "data"), "agentic": {"gates": {"slots": 2}}}


def _seed(cfg, *records):
    p = WK.path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": list(records)}))


def _w(name, **kw):
    return {"name": name, "ssh": f"me@{name}", "slots": 1, "kinds": ["gate"], "nw": "nw",
            "enabled": True, **kw}


def _waiter(cfg, cid="C1"):
    """Acquire as `gates.run_all` does, in a thread; `out["pool"]` is where it landed."""
    out, stop, done = {}, threading.Event(), threading.Event()

    def body():
        with GS.acquire(cfg, cid, stop=stop, poll_s=0.02, pools=WK.gate_pools(cfg),
                        repool=lambda: WK.gate_pools(cfg)) as hold:
            out["pool"], out["interrupted"] = hold.pool, hold.interrupted
            done.wait()
    t = threading.Thread(target=body, daemon=True)
    t.start()
    return t, out, stop, done


def test_pools_are_worker_only_while_a_worker_is_placeable(cfg):
    _seed(cfg, _w("box", slots=2))
    assert WK.gate_pools(cfg) == [("box", 2)]
    _seed(cfg, _w("box", enabled=False))
    assert WK.gate_pools(cfg) == [(None, 2)]
    _seed(cfg, _w("box", cooldown_until=FUTURE))
    assert WK.gate_pools(cfg) == [(None, 2)]
    _seed(cfg, _w("box", draining=True))
    assert WK.gate_pools(cfg) == [(None, 2)]
    assert WK.gate_pools({"data_dir": cfg["data_dir"] + "x"}) == [(None, 2)]
    _seed(cfg, _w("a"), _w("b"))
    assert WK.gate_pools(cfg, exclude=["a"]) == [("b", 1)]
    assert WK.gate_pools(cfg, exclude=["a", "b"]) == [(None, 2)]


def test_box_free_takes_the_box(cfg):
    _seed(cfg, _w("box"))
    with GS.acquire(cfg, "C1", pools=WK.gate_pools(cfg)) as hold:
        assert hold.pool == "box" and not hold.interrupted


def test_box_busy_waits_and_never_takes_a_local_slot(cfg):
    _seed(cfg, _w("box"))
    with GS.acquire(cfg, "OTHER", pools=[("box", 1)]) as other:
        assert other.pool == "box"
        t, out, stop, done = _waiter(cfg)
        assert until(lambda: GS.waiting(cfg, "C1") is not None)
        # many polls pass with both local slots free: still waiting, nothing local held
        assert until(lambda: False, timeout=0.5) is False
        assert "pool" not in out and GS.held(cfg, None) == []
    assert until(lambda: "pool" in out)            # the box slot freed: the waiter gets it
    assert out["pool"] == "box"
    done.set()
    join(t)


@pytest.mark.parametrize("change", [{"cooldown_until": FUTURE}, {"enabled": False},
                                    {"draining": True}])
def test_box_turning_unplaceable_while_waiting_falls_to_local(cfg, change):
    _seed(cfg, _w("box"))
    with GS.acquire(cfg, "OTHER", pools=[("box", 1)]):
        t, out, stop, done = _waiter(cfg)
        assert until(lambda: GS.waiting(cfg, "C1") is not None)
        assert "pool" not in out
        _seed(cfg, _w("box", **change))
        assert until(lambda: "pool" in out)
        assert out["pool"] is None and not out["interrupted"]
        done.set()
        join(t)


def test_no_placeable_worker_is_local(cfg):
    with GS.acquire(cfg, "C1", pools=WK.gate_pools(cfg)) as hold:
        assert hold.pool is None


def test_local_overflow_opt_out_restores_overflow(cfg):
    cfg["agentic"]["gates"]["local_overflow"] = True
    _seed(cfg, _w("box"))
    assert WK.gate_pools(cfg) == [("box", 1), (None, 2)]
    with GS.acquire(cfg, "OTHER", pools=[("box", 1)]):
        with GS.acquire(cfg, "C1", pools=WK.gate_pools(cfg)) as hold:
            assert hold.pool is None
    for bad in ("true", 1, "yes", None):
        cfg["agentic"]["gates"]["local_overflow"] = bad
        assert not WK.local_overflow(cfg) and WK.gate_pools(cfg) == [("box", 1)]


def test_bad_repool_keeps_the_current_list(cfg):
    _seed(cfg, _w("box"))

    def boom():
        raise ValueError("registry unreadable")
    for repool in (boom, lambda: [], lambda: [("Bad Name", 1)]):
        assert GS._repooled(cfg, repool, [("box", 1)]) == [("box", 1)]
