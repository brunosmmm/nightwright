"""SPEC-0260: the close train. Per repository root at most one campaign is in the close
worktree phase; the others wait in FIFO order of entering CLOSING, so the queue's own merges
never invalidate a running re-gate. Reuses SPEC-0220's held re-gate (`hold-<spec>`)."""
import datetime as dt
import time

from test_agentic_close_inflight import Env, _held_closing, mk  # noqa: F401  (fixture)
from test_agentic_e2e import _git, run_cli
from nightwright.agentic import events as E
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S
import pytest

pytestmark = pytest.mark.fast


def _c(cid, root, entered_at=None, state="CLOSING", **closing):
    if entered_at is not None:
        closing["entered_at"] = entered_at
    return {"id": cid, "state": state, "repo": {"root": root}, "closing": closing}


def _queued_notes(env, cid):
    return [e for e in S.read_ledger(env.cfg, cid, types=["note"])
            if e["text"].startswith("close queued behind")]


# ---- close_train (pure) ---------------------------------------------------------------------

def test_close_train_orders_by_entry_then_id():
    states = [_c("c3", "R", "2026-10-26T10:00:05+00:00"),
              _c("c2", "R", "2026-10-26T10:00:01+00:00"),
              _c("c1", "R", "2026-10-26T10:00:01+00:00"),     # tie: id breaks it
              _c("c0", "R", "2026-10-26T12:00:01+02:00"),     # 10:00:01 UTC, ties c1/c2 too
              _c("legacy", "R"),                               # no entered_at: earliest
              _c("bad", "R", "not-a-time"),                    # unparseable: earliest
              _c("s1", "S", "2026-10-26T09:00:00+00:00"),
              {"id": "noroot", "state": "CLOSING", "closing": {}},
              _c("busy", "R", "2026-10-26T08:00:00+00:00", state="REVIEWING")]
    assert Q.close_train(states, set()) == {"R": ["bad", "legacy", "c0", "c1", "c2", "c3"],
                                            "S": ["s1"], "noroot": ["noroot"]}


def test_close_train_skips_resolving_and_parked():
    states = [_c("a", "R", "2026-10-26T10:00:00+00:00", resolve={"target": "main"}),
              _c("b", "R", "2026-10-26T10:00:01+00:00", state="PARKED"),
              _c("c", "R", "2026-10-26T10:00:02+00:00")]
    assert Q.close_train(states, set()) == {"R": ["c"]}


def test_close_train_inflight_member_is_head():
    states = [_c("a", "R", "2026-10-26T10:00:00+00:00"),
              _c("b", "R", "2026-10-26T10:00:09+00:00"),
              _c("c", "R")]
    assert Q.close_train(states, {"b"}) == {"R": ["b", "c", "a"]}


# ---- the pooled runner ----------------------------------------------------------------------

@pytest.mark.integration
def test_second_close_waits_while_first_regates(mk):
    env = mk(slots=2, specs=2)
    a = _held_closing(env, "SPEC-0001")
    b = env.add("SPEC-0002")
    env.hold("SPEC-0002")
    env.spin(lambda: env.state(b) == "CLOSING")
    env.tick()
    assert env.running("SPEC-0001") and env.state(a) == "CLOSING"
    assert env.started(b, "close") == [] and b not in env.pool.flights
    assert env.st(b)["closing"]["queued_behind"] == a
    assert env.gate_runs("SPEC-0002") == 1                     # its GATING run only
    for _ in range(5):
        env.tick()
    assert b not in env.pool.flights and len(_queued_notes(env, b)) == 1
    assert _queued_notes(env, b)[0]["text"] == f"close queued behind {a} (position 1)"

    seen = {}

    def b_running():
        hs = (env.st(b).get("closing") or {}).get("head_since")
        if hs:
            seen["head_since"] = hs
        return env.state(a) == "MERGED" and env.running("SPEC-0002")

    env.release("SPEC-0001")
    env.spin(b_running)
    assert seen.get("head_since") and "queued_behind" not in env.st(b)["closing"]
    env.release("SPEC-0002")
    env.spin(lambda: env.state(a) == "MERGED" and env.state(b) == "MERGED")
    assert [r for c in (a, b) for r in env.rows(c) if r["reason"] == "base_moved"] == []
    assert len(env.started(a)) == 1 and len(env.started(b)) == 1
    assert len(_queued_notes(env, b)) == 1
    assert _git(env.repo, "rev-list", "--merges", "main") == ""
    assert (env.repo / "feature-a.txt").exists() and (env.repo / "feature-b.txt").exists()


@pytest.mark.integration
def test_different_roots_close_concurrently(mk, tmp_path, monkeypatch):
    env = mk(slots=2, specs=2)
    a = _held_closing(env, "SPEC-0001")
    b = env.add("SPEC-0002")
    env.spin(lambda: env.state(b) == "CLOSING"
             and (env.st(b).get("closing") or {}).get("queued_behind") == a)
    other = tmp_path / "other-root"
    other.mkdir()
    with S.item_lock(env.cfg, b):                 # B now belongs to another repository
        st = env.st(b)
        st["repo"] = {**st["repo"], "root": str(other)}
        S.write_state(env.cfg, b, st)
    calls = []
    real = Q._do_prepare_close

    def stub(ctx, state):
        if state["id"] != b:
            return real(ctx, state)
        calls.append(state["id"])
        return Q.StepResult(state["id"], Q.Step("close"), "CLOSING", error="stub")

    monkeypatch.setattr(Q, "_do_prepare_close", stub)
    env.tick()
    assert len(env.started(b, "close")) == 1 and env.running("SPEC-0001")
    assert a in env.pool.flights and env.state(a) == "CLOSING"
    env.release("SPEC-0001")
    env.spin(lambda: env.state(a) == "MERGED")
    assert calls == [b]


@pytest.mark.integration
def test_config_recovered_close_joins_train(mk):
    """A campaign step 0 brings back to CLOSING (it was PARKED `config_invalid` when the tick
    read `items`) is a train member too: it waits behind the running close, never beside it."""
    env = mk(slots=2, specs=2)
    a = _held_closing(env, "SPEC-0001")
    b = env.add("SPEC-0002")
    env.spin(lambda: env.state(b) == "CLOSING"
             and (env.st(b).get("closing") or {}).get("queued_behind") == a)
    with S.item_lock(env.cfg, b):                 # parked on a config that loads again now
        st = env.st(b)
        st.pop("closing", None)
        st["state"] = "PARKED"
        st["park"] = {"reason": "config_invalid", "resume_to": "CLOSING"}
        S.write_state(env.cfg, b, st)
    env.tick()
    assert env.state(b) == "CLOSING" and env.running("SPEC-0001")
    assert env.started(b, "close") == [] and b not in env.pool.flights
    assert env.st(b)["closing"]["queued_behind"] == a
    env.release("SPEC-0001")
    env.spin(lambda: env.state(a) == "MERGED" and env.state(b) == "MERGED")
    assert [r for c in (a, b) for r in env.rows(c) if r["reason"] == "base_moved"] == []
    assert len(env.started(b, "close")) == 1


@pytest.mark.integration
def test_entered_at_set_and_reset(mk):
    env = mk()
    a = _held_closing(env)
    entered = env.st(a)["closing"]["entered_at"]
    assert entered
    env.move_main("other.txt")
    env.release()
    env.settle(a)
    env.hold()                                     # the redo's re-gate holds too
    env.tick()                                     # harvest: base_moved, resubmitted
    st = env.st(a)
    assert env.rows(a)[-1]["reason"] == "base_moved"
    assert st["closing"]["attempts"] == 1 and st["closing"]["entered_at"] == entered
    r = run_cli(env.cfg, env.monkeypatch, ["queue", "park", a, "--reason", "look", "--yes"])
    assert r.exit_code == 0, r.output
    env.release()
    env.spin(lambda: env.state(a) == "PARKED")
    assert "closing" not in env.st(a)
    time.sleep(1.1)                                # timestamps have one-second resolution
    r = run_cli(env.cfg, env.monkeypatch, ["queue", "resume", a, "--yes"])
    assert r.exit_code == 0, r.output
    st = env.st(a)
    assert st["state"] == "CLOSING"
    assert dt.datetime.fromisoformat(st["closing"]["entered_at"]) \
        > dt.datetime.fromisoformat(entered)
    env.spin(lambda: env.state(a) == "MERGED")


# ---- watchdog and single shot ---------------------------------------------------------------

def test_stuck_items_ignores_train_waiter_and_uses_head_since(mk):
    env = mk()
    sett = dict(E.settings(env.cfg), stuck_after_s=0)
    budget = sett["gate_budget_s"]
    now = dt.datetime.now().astimezone() + dt.timedelta(seconds=2 * budget)
    recent, old = S.now_iso(now - dt.timedelta(seconds=10)), S.now_iso(now - dt.timedelta(seconds=2 * budget))

    def closing_campaign(cid, closing):
        S.create_campaign(env.cfg, {"id": cid, "spec_id": "SPEC-0001", "project": "p",
                                    "state": "CLOSING", "closing": closing})
        S.append_event(env.cfg, cid, "transition", {"from": "GATING", "to": "CLOSING",
                                                    "reason": "gates_passed"})

    closing_campaign("SPEC-0001-20261026-0001", {"attempts": 0,
                                                 "queued_behind": "SPEC-0001-20261026-0002"})
    closing_campaign("SPEC-0001-20261026-0002", {"attempts": 0, "head_since": recent})
    closing_campaign("SPEC-0001-20261026-0003", {"attempts": 0, "head_since": old})
    closing_campaign("SPEC-0001-20261026-0004", {"attempts": 0})
    stuck = sorted(i["campaign"] for i in E.stuck_items(env.cfg, now, sett))
    assert stuck == ["SPEC-0001-20261026-0003", "SPEC-0001-20261026-0004"]


def _silent_env(mk):
    env = mk()
    sett = E.settings(env.cfg)
    now = dt.datetime.now().astimezone() + dt.timedelta(seconds=2 * sett["silent_after_s"])
    return env, now, S.now_iso(now - dt.timedelta(seconds=10)), \
        S.now_iso(now - dt.timedelta(seconds=2 * sett["silent_after_s"]))


def _closing(env, cid, state="CLOSING", closing=None):
    S.create_campaign(env.cfg, {"id": cid, "spec_id": "SPEC-0001", "project": "p",
                                "state": state, "closing": closing})
    S.append_event(env.cfg, cid, "transition", {"from": "GATING", "to": state,
                                                "reason": "gates_passed"})


def _silent(env, now):
    return sorted(c for _k, t, c, _d in E.watchdog_checks(env.cfg, now) if t == "campaign_silent")


def test_waits_in_close_train_predicate():
    live = {"h"}
    wait = lambda st: E.waits_in_close_train(st, live)  # noqa: E731
    assert wait({"id": "w", "state": "CLOSING", "closing": {"queued_behind": "h"}})
    assert not wait({"id": "h", "state": "CLOSING", "closing": {"attempts": 0}})
    assert not wait({"id": "w", "state": "GATING", "closing": {"queued_behind": "h"}})
    assert not wait({"id": "w", "state": "CLOSING", "closing": {"queued_behind": ""}})
    assert not wait({"id": "w", "state": "CLOSING", "closing": None})
    assert not wait({"id": "w", "state": "CLOSING", "closing": {"queued_behind": "w"}})
    assert not wait({"id": "w", "state": "CLOSING", "closing": {"queued_behind": "merged"}})


def test_watchdog_silent_skips_train_waiter(mk):
    env, now, _recent, _old = _silent_env(mk)
    head, waiter = "SPEC-0001-20261026-0001", "SPEC-0001-20261026-0002"
    _closing(env, head, closing={"attempts": 0})
    _closing(env, waiter, closing={"attempts": 0, "queued_behind": head})
    assert _silent(env, now) == [head]
    # a stale `queued_behind` (head merged, absent, or itself) leaves the normal clock
    merged, gone, selfref = (f"SPEC-0001-20261026-000{n}" for n in (3, 4, 5))
    _closing(env, merged, "MERGED")
    for cid, behind in (("SPEC-0001-20261026-0006", merged), ("SPEC-0001-20261026-0007", gone),
                        (selfref, selfref)):
        _closing(env, cid, closing={"attempts": 0, "queued_behind": behind})
    assert _silent(env, now) == sorted([head, selfref, "SPEC-0001-20261026-0006",
                                        "SPEC-0001-20261026-0007"])


def test_watchdog_silent_uses_head_since(mk):
    env, now, recent, old = _silent_env(mk)
    fresh, stale = "SPEC-0001-20261026-0001", "SPEC-0001-20261026-0002"
    _closing(env, fresh, closing={"attempts": 0, "head_since": recent})
    _closing(env, stale, closing={"attempts": 0, "head_since": old})
    assert _silent(env, now) == [stale]


def test_stale_queued_behind_is_reported_stuck(mk):
    """SPEC-0287: `queued_behind` hides a waiter only while its head is CLOSING. A head that
    is MERGED, missing or the waiter itself leaves the normal CLOSING clock in charge."""
    env = mk()
    sett = dict(E.settings(env.cfg), stuck_after_s=0)
    budget = sett["gate_budget_s"]
    now = dt.datetime.now().astimezone() + dt.timedelta(seconds=2 * budget)
    old = S.now_iso(now - dt.timedelta(seconds=2 * budget))

    def campaign(cid, state, closing=None):
        S.create_campaign(env.cfg, {"id": cid, "spec_id": "SPEC-0001", "project": "p",
                                    "state": state, "closing": closing})
        S.append_event(env.cfg, cid, "transition", {"from": "GATING", "to": state,
                                                    "reason": "gates_passed"})

    head, merged = "SPEC-0001-20261026-0001", "SPEC-0001-20261026-0002"
    campaign(head, "CLOSING", {"attempts": 0, "entered_at": old, "head_since": S.now_iso(now)})
    campaign(merged, "MERGED")
    stale = {"SPEC-0001-20261026-0003": merged, "SPEC-0001-20261026-0004": "SPEC-0001-gone",
             "SPEC-0001-20261026-0005": "SPEC-0001-20261026-0005"}
    for cid, behind in stale.items():
        campaign(cid, "CLOSING", {"attempts": 0, "entered_at": old, "queued_behind": behind})
    campaign("SPEC-0001-20261026-0006", "CLOSING",
             {"attempts": 0, "entered_at": old, "queued_behind": head})
    stuck = sorted(i["campaign"] for i in E.stuck_items(env.cfg, now, sett))
    assert stuck == sorted(stale)


def test_single_shot_closes_in_train_order(mk):
    env = mk(slots=2, specs=2)
    a = env.add("SPEC-0001")
    env.to_closing(a)
    r = run_cli(env.cfg, env.monkeypatch, ["queue", "park", a, "--reason", "hold", "--yes"])
    assert r.exit_code == 0, r.output
    b = env.add("SPEC-0002")
    env.to_closing(b)
    assert env.state(a) == "PARKED" and env.state(b) == "CLOSING"
    r = run_cli(env.cfg, env.monkeypatch, ["queue", "resume", a, "--yes"])
    assert r.exit_code == 0, r.output
    # the campaign `busy` lists second (`list_campaigns` order) entered CLOSING first
    first, second = [s["id"] for s in S.list_campaigns(env.cfg)
                     if s.get("state") in Q.ACTIVE_STATES]
    for cid, ts in ((first, "2026-10-26T10:00:09+00:00"), (second, "2026-10-26T10:00:00+00:00")):
        with S.item_lock(env.cfg, cid):
            st = env.st(cid)
            st["closing"]["entered_at"] = ts
            S.write_state(env.cfg, cid, st)
    order = []
    for _ in range(4):
        rep = Q.tick(env.cfg, runner=env.runner)
        order += [s["campaign"] for s in rep.steps if s["action"] == "close"]
        if env.state(a) == env.state(b) == "MERGED":
            break
    assert order == [second, first]
    assert env.st(first)["close"]["merged_sha"] == env.main_sha()
    assert _git(env.repo, "rev-list", "--merges", "main") == ""
