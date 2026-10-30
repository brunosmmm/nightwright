"""SPEC-0226: the queue runner reloads the user config (and journals project config changes)
between ticks. The loader is the real `load_config` over a tmp config dir; the loop is driven
either by `Q.run` or, where a step must stay in flight, tick by tick the way `run()` does."""
import json
import threading
import time

import pytest
import yaml

from test_agentic_e2e import (_git, _write, added, impl_done, make_cfg, make_repo, review,
                              run_cli, script, write_review)
from wt.agentic import events as E
from wt.agentic import lease as L
from wt.agentic import queue as Q
from wt.agentic import store as S
from wt.agentic.runner import select_runner
from wt.config import load_config

RUNTIME = ("_tz", "data_dir", "config_dir")


class Env:
    """A repo, a user config file under a tmp config dir, the real loader, a lease and a
    `ConfigWatch`. `tick()` is one iteration of `run()`'s loop."""

    def __init__(self, tmp_path, monkeypatch, *, profile=None, runner=None, entries=None):
        self.tmp = tmp_path
        monkeypatch.setenv("WT_CONFIG_DIR", str(tmp_path / "cfg"))
        monkeypatch.setenv("WT_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("WT_CACHE_DIR", str(tmp_path / "cache"))
        self.repo = make_repo(tmp_path)
        base = make_cfg(tmp_path, self.repo,
                        script(tmp_path, entries if entries is not None else [impl_done()]),
                        profile=profile)
        self.path = tmp_path / "cfg" / "config.yaml"
        self.doc = {k: v for k, v in base.items() if k not in RUNTIME}
        self._t = time.time_ns()
        self.write(self.doc)
        self.cfg = load_config()
        self.monkeypatch = monkeypatch
        self.lease = L.acquire(self.cfg, ["wt", "queue", "run"])
        self.watch = Q.ConfigWatch(self.cfg, load_config)
        self.pool = Q.StepPool()
        self.runner = runner(self.cfg) if runner else None

    def close(self):
        self.pool.shutdown()
        self.lease.release()

    # -- the user file --
    def _bump(self):
        import os
        self._t += 10**9
        os.utime(self.path, ns=(self._t, self._t))

    def write(self, doc):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(doc))
        self._bump()

    def write_raw(self, text):
        self.path.write_text(text)
        self._bump()

    def edit(self, mutate):
        mutate(self.doc)
        self.write(self.doc)

    def profile(self):
        return self.doc["agentic"]["profiles"]["balanced"]

    # -- the loop --
    def live(self):
        return [c for c in S.list_campaigns(self.cfg) if c.get("state") not in Q.TERMINAL_STATES]

    def check(self):
        self.cfg = self.watch.check(self.cfg, self.lease, self.live())
        return self.cfg

    def tick(self):
        self.check()
        return Q.tick(self.cfg, runner=self.runner, pool=self.pool)

    def drive(self, cond, n=60):
        for _ in range(n):
            if cond():
                return
            self.tick()
            time.sleep(0.05)
        assert cond()

    # -- observations --
    def records(self, *kinds):
        return [r for r in Q.read_journal(self.cfg) if r["kind"] in kinds]

    def errors(self):
        return (L.read_lease(self.cfg) or {}).get("config_errors") or {}

    def conditions(self):
        return [c for c in E.watchdog_checks(self.cfg) if c[1] == "runner_config_invalid"]


@pytest.fixture
def make_env(tmp_path, monkeypatch):
    made = []

    def make(**kw):
        env = Env(tmp_path, monkeypatch, **kw)
        made.append(env)
        return env

    yield make
    for env in made:
        if getattr(env.runner, "release", None):
            env.runner.release.set()
        env.close()


class HeldRunner:
    """Runs of the held specs (by worktree path) or personas block until `release` is set."""

    def __init__(self, cfg, *held, personas=()):
        self.inner = select_runner(cfg)
        self.held, self.personas = set(held), set(personas)
        self.release = threading.Event()

    def run(self, req):
        if req.persona in self.personas or any(h in str(req.cwd) for h in self.held):
            assert self.release.wait(30), "test never released the held run"
        return self.inner.run(req)


def _second_spec(repo, spec_id="SPEC-0002", rel="docs/specs/0002-second.md"):
    p = _write(repo, rel, (repo / "docs/specs/0001-trivial.md").read_text()
               .replace("SPEC-0001", spec_id))
    write_review({"data_dir": str(repo.parent / "data")}, p, spec_id)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", spec_id)


def _starts(cfg, cid):
    return S.read_ledger(cfg, cid, types=["run.start"])


def _state(cfg, cid):
    return S.read_state(cfg, cid)["state"]


# ---- apply semantics ---------------------------------------------------------------------------

def test_profile_model_change_applies_to_next_run(make_env, monkeypatch):
    env = make_env(profile={"slots": 2},
                   runner=lambda cfg: HeldRunner(cfg, "SPEC-0001", "SPEC-0002"))
    _second_spec(env.repo)
    a = added(env.cfg, monkeypatch, "SPEC-0001")
    env.tick()                                              # admit + open A
    env.tick()                                              # A's implementer: in flight
    assert a in env.pool.flights
    assert _starts(env.cfg, a)[0]["model"] == "sonnet"

    b = added(env.cfg, monkeypatch, "SPEC-0002")
    env.edit(lambda d: d["agentic"]["profiles"]["balanced"].setdefault("personas", {})
             .setdefault("implementer", {}).update(model="haiku"))
    env.tick()                                              # reload; B admitted + opened
    env.tick()                                              # B's implementer starts
    assert env.cfg["agentic"]["profiles"]["balanced"]["personas"]["implementer"]["model"] == "haiku"
    assert _starts(env.cfg, b)[0]["model"] == "haiku"
    assert a in env.pool.flights and len(_starts(env.cfg, a)) == 1
    assert _starts(env.cfg, a)[0]["model"] == "sonnet"      # the in-flight run keeps its model
    (rec,) = env.records("config_reloaded")
    assert rec["scope"] == "user" and "agentic.profiles.balanced.personas.implementer.model" \
        in rec["changed"]


def test_inflight_harvest_uses_planning_profile(make_env, monkeypatch):
    weak = [{"severity": "minor", "ac": "feature.txt exists", "title": f"nit {i}",
             "detail": "d", "suggested_fix": "f", "files": ["feature.txt"]} for i in range(3)]       # score 88
    env = make_env(profile={"review": {"pass_score": 80, "triager": False, "followups": False}},
                   entries=[impl_done(), review("pass", findings=weak)],
                   runner=lambda cfg: HeldRunner(cfg, personas=("conformance_reviewer",)))
    cid = added(env.cfg, monkeypatch)
    env.drive(lambda: _state(env.cfg, cid) == "REVIEWING" and cid in env.pool.flights)
    time.sleep(0.2)                                         # the reviewer is blocked in the runner
    env.edit(lambda d: d["agentic"]["profiles"]["balanced"]["review"].update(pass_score=95))
    env.tick()                                              # reload; the review is still in flight
    assert env.cfg["agentic"]["profiles"]["balanced"]["review"]["pass_score"] == 95
    assert _state(env.cfg, cid) == "REVIEWING"
    env.runner.release.set()
    env.drive(lambda: _state(env.cfg, cid) in ("CLOSING", "MERGED"))
    # judged by the pass score it was planned with (80), not the new 95
    assert Q.resolve_profile(env.cfg, "balanced")["review"]["pass_score"] == 95
    assert S.read_state(env.cfg, cid)["counters"].get("loops", 0) == 0


def test_slots_change_applies_to_admission(make_env, monkeypatch):
    env = make_env(profile={"slots": 1}, runner=lambda cfg: HeldRunner(cfg, "SPEC-0001"))
    _second_spec(env.repo)
    a = added(env.cfg, monkeypatch, "SPEC-0001")
    b = added(env.cfg, monkeypatch, "SPEC-0002")
    env.tick()
    env.tick()
    assert _state(env.cfg, a) == "IMPLEMENTING" and _state(env.cfg, b) == "QUEUED"
    env.tick()
    assert _state(env.cfg, b) == "QUEUED"                   # one slot, held by A
    env.edit(lambda d: d["agentic"]["profiles"]["balanced"].update(slots=2))
    env.tick()
    assert _state(env.cfg, b) != "QUEUED"                   # admitted at the next tick


# ---- journal -----------------------------------------------------------------------------------

def test_reload_journal_changed_keys_and_restart_required(make_env):
    env = make_env()

    def both(d):
        d["agentic"]["lease_ttl_s"] = 240
        d["agentic"]["profiles"]["balanced"].setdefault("personas", {}) \
            .setdefault("gate_triager", {})["max_budget_usd"] = 0.9
    env.edit(both)
    env.check()
    (rec,) = env.records("config_reloaded")
    assert rec["scope"] == "user" and rec["path"] == str(env.path)
    assert rec["changed"] == ["agentic.lease_ttl_s",
                              "agentic.profiles.balanced.personas.gate_triager.max_budget_usd"]
    assert rec["restart_required"] == ["agentic.lease_ttl_s"]
    assert env.cfg["agentic"]["lease_ttl_s"] == 240

    env._bump()                                             # a touch: new mtime, same content
    env.check()
    env.write_raw(env.path.read_text() + "\n# a comment\n")  # a whitespace/comment edit
    env.check()
    assert len(env.records("config_reloaded")) == 1
    assert env.records("config_load_failed") == []


def _good(d):
    return d


def _set(path, value):
    def mutate(d):
        node = d
        for k in path[:-1]:
            node = node.setdefault(k, {})
        node[path[-1]] = value
    return mutate


BAD = [
    ("broken_yaml", lambda e: e.write_raw("agentic: [unclosed\n")),
    ("list_doc", lambda e: e.write_raw("- a\n- b\n")),
    ("scalar_doc", lambda e: e.write_raw("just a string\n")),
    ("empty_file", lambda e: e.write_raw("  \n\n")),
    ("truncated", lambda e: e.write_raw("")),
    ("deleted", lambda e: e.path.unlink()),
    ("bad_timezone", lambda e: e.write({**e.doc, "timezone": "Bogus/Zone"})),
    ("work_root_int", lambda e: e.write({**e.doc, "work_root": 5})),
    ("lease_ttl_str", lambda e: e.write(_apply(e.doc, _set(["agentic", "lease_ttl_s"], "x")))),
    ("poll_zero", lambda e: e.write(_apply(e.doc, _set(["agentic", "poll_interval_s"], 0)))),
    ("slots_zero", lambda e: e.write(_apply(
        e.doc, _set(["agentic", "profiles", "balanced", "slots"], 0)))),
    ("unknown_runner", lambda e: e.write(_apply(e.doc, _set(["agentic", "runner"], "gpt")))),
    ("unknown_default_profile", lambda e: e.write(_apply(
        e.doc, _set(["agentic", "default_profile"], "nope")))),
]


def _apply(doc, mutate):
    import copy
    out = copy.deepcopy(doc)
    mutate(out)
    return out


@pytest.mark.parametrize("name,break_it", BAD, ids=[b[0] for b in BAD])
def test_invalid_user_config_keeps_old(make_env, monkeypatch, name, break_it):
    env = make_env(profile={"slots": 1})
    cid = added(env.cfg, monkeypatch)
    old = env.cfg
    break_it(env)
    env.tick()                                              # must not crash
    env.tick()
    assert env.cfg is old
    assert _state(env.cfg, cid) != "QUEUED"                 # the old cfg still admits
    (rec,) = env.records("config_load_failed")              # one per file version
    assert rec["scope"] == "user" and rec["path"] == str(env.path) and rec["error"]
    assert env.records("config_reloaded") == []
    assert list(env.errors()) == [str(env.path)]
    (cond,) = env.conditions()
    assert cond[0].startswith(f"runner_config_invalid:{env.path}:")
    assert cond[3]["scope"] == "user" and cond[3]["path"] == str(env.path)
    E.validate("runner_config_invalid", cond[3])

    env.write(_apply(env.doc, _set(["agentic", "profiles", "balanced", "loop_max"], 5)))
    env.tick()                                              # fixed (and one key changed)
    (rec,) = env.records("config_reloaded")
    assert rec["changed"] == ["agentic.profiles.balanced.loop_max"]
    assert env.cfg is not old and env.errors() == {} and env.conditions() == []


def test_each_broken_version_gets_its_own_record_and_condition(make_env, monkeypatch):
    env = make_env()
    old = env.cfg
    env.write_raw("agentic: [unclosed\n")
    env.check()
    env.check()                                             # same version: nothing new
    (first,) = env.conditions()
    env.write_raw("- a\n- b\n")                             # broken a second, different way
    env.check()
    assert env.cfg is old
    assert len(env.records("config_load_failed")) == 2
    (second,) = env.conditions()
    assert second[0] != first[0] and list(env.errors()) == [str(env.path)]


def test_removing_a_live_campaigns_profile_is_invalid(make_env, monkeypatch):
    env = make_env()
    env.edit(_set(["agentic", "profiles", "custom"], {"slots": 1}))
    env.check()
    assert env.records("config_reloaded")                   # the profile arrived
    added(env.cfg, monkeypatch, "SPEC-0001", "--profile", "custom")
    old = env.cfg
    env.edit(lambda d: d["agentic"]["profiles"].pop("custom"))
    env.check()
    assert env.cfg is old
    (rec,) = env.records("config_load_failed")
    assert "unknown profile 'custom'" in rec["error"]
    assert len(env.conditions()) == 1


# ---- project configs ---------------------------------------------------------------------------

def _project_yaml(env):
    return env.repo / ".wt" / "project.yaml"


def test_project_config_change_journaled(make_env, monkeypatch):
    env = make_env()
    cid = added(env.cfg, monkeypatch)
    project = S.read_state(env.cfg, cid)["project"]
    env.check()                                             # seeds silently
    assert env.records("config_reloaded", "config_load_failed") == []
    p = _project_yaml(env)
    good = p.read_text()
    p.write_text(good.replace("timeout_s: 30, required: true", "timeout_s: 45, required: true"))
    import os
    os.utime(p, ns=(env._t + 5 * 10**9,) * 2)
    env.check()
    (rec,) = env.records("config_reloaded")
    assert rec["scope"] == f"project:{project}" and rec["path"] == str(p)
    assert rec["changed"] == ["gates"] and env.conditions() == []

    p.write_text(good + "bogus_key: 1\n")
    os.utime(p, ns=(env._t + 6 * 10**9,) * 2)
    env.tick()                                              # journals, and the tick parks it
    (fail,) = env.records("config_load_failed")
    assert fail["scope"] == f"project:{project}" and "bogus_key" in fail["error"]
    (cond,) = env.conditions()
    assert cond[3]["scope"] == f"project:{project}"
    st = S.read_state(env.cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "config_invalid"

    p.write_text(good)
    os.utime(p, ns=(env._t + 7 * 10**9,) * 2)
    env.tick()                                              # SPEC-0194 recovery as before
    assert env.conditions() == [] and S.read_state(env.cfg, cid)["park"] is None


def test_two_broken_files_reported_independently(make_env, monkeypatch):
    import os
    env = make_env()
    added(env.cfg, monkeypatch)
    env.check()
    p = _project_yaml(env)
    good = p.read_text()
    env.write_raw("agentic: [unclosed\n")
    p.write_text(good + "bogus_key: 1\n")
    os.utime(p, ns=(env._t + 5 * 10**9,) * 2)
    env.check()
    assert sorted(c[3]["path"] for c in env.conditions()) == sorted([str(env.path), str(p)])
    assert len(env.errors()) == 2

    p.write_text(good)                                      # fix the project file only
    os.utime(p, ns=(env._t + 6 * 10**9,) * 2)
    env.check()
    (cond,) = env.conditions()
    assert cond[3]["path"] == str(env.path) and list(env.errors()) == [str(env.path)]

    env.write(_apply(env.doc, _set(["agentic", "profiles", "balanced", "loop_max"], 5)))
    env.check()
    assert env.conditions() == [] and env.errors() == {}


def test_fixing_the_user_file_first_keeps_the_project_condition(make_env, monkeypatch):
    import os
    env = make_env()
    added(env.cfg, monkeypatch)
    env.check()
    p = _project_yaml(env)
    good = p.read_text()
    env.write_raw("agentic: [unclosed\n")
    p.write_text(good + "bogus_key: 1\n")
    os.utime(p, ns=(env._t + 5 * 10**9,) * 2)
    env.check()
    assert len(env.conditions()) == 2

    env.write(_apply(env.doc, _set(["agentic", "profiles", "balanced", "loop_max"], 5)))
    env.check()
    (cond,) = env.conditions()
    assert cond[3]["path"] == str(p) and list(env.errors()) == [str(p)]


def test_leftover_lease_errors_are_cleared_on_first_check(make_env):
    env = make_env()
    env.lease.update_info(config_errors={"/old.yaml": {"scope": "user", "error": "x",
                                                         "since": "t", "mtime_ns": 1}})
    assert len(env.conditions()) == 1
    env.check()
    assert env.errors() == {} and env.conditions() == []


def test_project_entry_dropped_when_its_campaign_is_gone(make_env, monkeypatch):
    import os
    env = make_env()
    added(env.cfg, monkeypatch)
    env.check()
    p = _project_yaml(env)
    p.write_text(p.read_text() + "bogus_key: 1\n")
    os.utime(p, ns=(env._t + 5 * 10**9,) * 2)
    env.check()
    assert list(env.errors()) == [str(p)]
    env.watch.check(env.cfg, env.lease, [])                 # no live campaign any more
    assert env.errors() == {} and env.conditions() == []


def test_project_yaml_broken_at_first_sight_is_reported_then_cleared(make_env, monkeypatch):
    import os
    env = make_env()
    added(env.cfg, monkeypatch)
    p = _project_yaml(env)
    good = p.read_text()
    p.write_text(good + "bogus_key: 1\n")
    os.utime(p, ns=(env._t + 5 * 10**9,) * 2)
    env.check()                                             # the first time the watch sees it
    (fail,) = env.records("config_load_failed")
    assert "bogus_key" in fail["error"] and len(env.conditions()) == 1
    p.write_text(good)
    os.utime(p, ns=(env._t + 6 * 10**9,) * 2)
    env.check()
    assert env.conditions() == [] and env.errors() == {}
    assert env.records("config_reloaded") == []             # no good document to diff against


def test_project_name_mismatch_is_invalid(make_env, monkeypatch):
    import os
    env = make_env()
    added(env.cfg, monkeypatch)
    env.check()
    p = _project_yaml(env)
    p.write_text(p.read_text() + "project: somebody-else\n")
    os.utime(p, ns=(env._t + 5 * 10**9,) * 2)
    env.check()
    (fail,) = env.records("config_load_failed")
    assert "does not match" in fail["error"] and len(env.conditions()) == 1


def test_user_file_rewritten_while_loading_is_retried_not_recorded(make_env, monkeypatch):
    env = make_env()
    old = env.cfg
    env.edit(_set(["agentic", "profiles", "balanced", "loop_max"], 5))
    real = env.watch.loader

    def racing_loader():
        out = real()
        env.write_raw("")                                   # truncated right after we read it
        return out
    env.watch.loader = racing_loader
    env.check()
    assert env.cfg is old and env.records("config_reloaded", "config_load_failed") == []
    env.watch.loader = real
    env.check()                                             # next tick sees the truncated file
    assert len(env.records("config_load_failed")) == 1 and env.cfg is old
    env.edit(_set(["agentic", "profiles", "balanced", "loop_max"], 6))
    env.check()
    assert env.records("config_reloaded") and env.errors() == {}


def test_config_errors_updates_race_heartbeat(make_env):
    env = make_env()
    with Q._heartbeat_while(env.lease, interval_s=0.0005):
        for i in range(150):
            path = f"/x/{i % 3}.yaml"
            env.watch._set_error(env.lease, path, "user", "boom", (i, 1))
            json.loads(L.lease_path(env.cfg).read_text())
            env.watch._clear_error(env.lease, path)
            json.loads(L.lease_path(env.cfg).read_text())
    assert env.records("heartbeat_error") == [] and env.errors() == {}


# ---- the loop ----------------------------------------------------------------------------------

class FakeTime:
    """Stands in for `queue.time` (SPEC-0248): `sleep(s)` advances `monotonic()` by `s` and
    returns at once, so the loop's waits are exact arithmetic, not wall time. Everything
    else (`time()`, …) is the real module."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += s

    def __getattr__(self, name):
        return getattr(time, name)


def _spy_ticks(monkeypatch):
    seen = []
    real = Q.tick

    def tick(cfg, **kw):
        seen.append((Q.time.monotonic(), cfg))           # the fake clock when one is installed
        return real(cfg, **kw)
    monkeypatch.setattr(Q, "tick", tick)
    return seen


def _run(env, monkeypatch, *, loader, edit_after=1, mutate=None, ticks=3, **kw):
    seen = _spy_ticks(monkeypatch)
    n = {"ticks": 0}

    def on_tick(_report):
        n["ticks"] += 1
        if n["ticks"] == edit_after and mutate:
            env.edit(mutate)

    Q.run(env.cfg, max_ticks=ticks, on_tick=on_tick, config_loader=loader, **kw)
    return seen


def test_run_applies_reload_between_ticks(make_env, monkeypatch):
    env = make_env()
    env.lease.release()                                     # run() takes the lease itself
    seen = _run(env, monkeypatch, loader=load_config,
                mutate=_set(["agentic", "profiles", "balanced", "slots"], 3))
    slots = [Q.slots_for(cfg) for _, cfg in seen]
    assert slots == [2, 3, 3]
    assert len([r for r in Q.read_journal(env.cfg) if r["kind"] == "config_reloaded"]) == 1
    env.lease = L.acquire(env.cfg)


def test_reexec_is_deferred_while_the_user_config_is_broken(make_env, monkeypatch):
    from test_agentic_reexec import _code_repo, _commit
    env = make_env()
    env.lease.release()
    root = _code_repo(env.tmp)
    calls = []
    monkeypatch.setattr(Q, "_EXEC", lambda exe, argv, e: calls.append(e))
    n = {"ticks": 0}

    def on_tick(_report):
        n["ticks"] += 1
        if n["ticks"] == 1:
            env.write_raw("")                               # seen by the check of tick 2
        elif n["ticks"] == 2:
            _commit(root, "src/wt/x.py", "A = 2\n", "code")
        elif n["ticks"] == 4:
            env.write(env.doc)                              # fixed

    Q.run(env.cfg, max_ticks=8, poll_interval_s=0, on_tick=on_tick, config_loader=load_config,
          reexec=True, code_root=root)
    journal = Q.read_journal(env.cfg)
    (deferred,) = [r for r in journal if r["kind"] == "reexec_deferred"]
    assert deferred["reason"] == "user_config_invalid"
    stop = journal[-1]
    assert stop["kind"] == "stop" and stop["reason"] == "code_changed"
    assert stop["ticks"] == 5 and len(calls) == 1           # re-exec only after the fix
    assert [r["kind"] for r in journal].index("config_load_failed") < \
        [r["kind"] for r in journal].index("reexec_deferred")
    monkeypatch.setenv(L.HANDOVER_ENV, calls[0][L.HANDOVER_ENV])
    env.lease = L.acquire(env.cfg, ["wt", "queue", "run"])  # adopt the handed-over lease


def test_no_loader_no_reload(make_env, monkeypatch):
    env = make_env()
    env.lease.release()
    seen = _run(env, monkeypatch, loader=None,
                mutate=_set(["agentic", "profiles", "balanced", "slots"], 3))
    assert [Q.slots_for(cfg) for _, cfg in seen] == [2, 2, 2]
    assert all(cfg is env.cfg for _, cfg in seen)
    assert [r for r in Q.read_journal(env.cfg) if r["kind"].startswith("config_")] == []
    env.lease = L.acquire(env.cfg)


def test_poll_interval_reload_respects_cli_override(make_env, monkeypatch):
    env = make_env()
    env.lease.release()
    monkeypatch.setattr(Q, "time", FakeTime())
    old = float(env.doc["agentic"]["poll_interval_s"])
    mutate = _set(["agentic", "poll_interval_s"], 0.6)
    seen = _run(env, monkeypatch, loader=load_config, mutate=mutate)
    assert seen[2][0] - seen[1][0] == pytest.approx(0.6)    # the new interval applies
    assert seen[1][0] - seen[0][0] == pytest.approx(old)    # the old one before the reload
    env.doc["agentic"]["poll_interval_s"] = 0.01
    env.write(env.doc)
    seen = _run(env, monkeypatch, loader=load_config, mutate=mutate, poll_interval_s=0.01)
    assert seen[2][0] - seen[1][0] == pytest.approx(0.01)   # --poll-interval wins
    assert seen[1][0] - seen[0][0] == pytest.approx(0.01)
    env.lease = L.acquire(env.cfg)


def test_cli_queue_run_passes_load_config(make_env, monkeypatch):
    env = make_env()
    env.lease.release()
    seen = {}

    def fake_run(cfg, **kw):
        seen.update(kw)
        return 0
    monkeypatch.setattr(Q, "run", fake_run)
    r = run_cli(env.cfg, monkeypatch, ["queue", "run", "--once"])
    assert r.exit_code == 0, r.output
    assert seen["config_loader"] is load_config
    env.lease = L.acquire(env.cfg)


# ---- helpers and display -----------------------------------------------------------------------

def test_config_diff():
    old = {"a": {"b": 1, "c": [1, 2], "d": {"e": 1}}, "_tz": 1, "data_dir": "x", "gone": 1,
           "_hidden": 1, "n": {"_x": 1}}
    new = {"a": {"b": 2, "c": [1, 2, 3], "d": {"e": 1}}, "_tz": 2, "data_dir": "y", "added": 1,
           "_hidden": 2, "n": {"_x": 2}}
    assert Q.config_diff(old, new) == ["a.b", "a.c", "added", "gone"]
    assert Q.config_diff(old, old) == []
    many = Q.config_diff({}, {f"k{i:03}": i for i in range(60)})
    assert len(many) == 51 and many[-1] == "…" and many[0] == "k000"


def test_validate_runner_cfg():
    ok = {"agentic": {"poll_interval_s": 1, "lease_ttl_s": 5, "runner": "fake"}}
    Q.validate_runner_cfg(ok, [{"profile": "thorough"}])
    for bad in ({"poll_interval_s": True}, {"lease_ttl_s": -1}, {"runner": "x"},
                {"profiles": {"balanced": {"slots": 1.5}}}):
        with pytest.raises(ValueError):
            Q.validate_runner_cfg({"agentic": bad})
    with pytest.raises(ValueError, match="unknown profile"):
        Q.validate_runner_cfg(ok, [{"profile": "ghost"}])


def test_runner_log_renders_config_records(make_env, monkeypatch):
    env = make_env()
    cfg = env.cfg
    Q.journal(cfg, "config_reloaded", scope="user", path="/p", changed=["a.b", "c"],
              restart_required=["agentic.lease_ttl_s"])
    Q.journal(cfg, "config_load_failed", scope="project:wt", path="/q", error="boom")
    Q.journal(cfg, "reexec_deferred", reason="user_config_invalid", to_sha="abcdef123456")
    r = run_cli(cfg, monkeypatch, ["queue", "runner-log", "--tail", "0"])
    assert r.exit_code == 0, r.output
    text = " ".join(r.output.split())
    assert "config reloaded (user): a.b, c" in text and "restart required" in text
    assert "config load failed (project:wt): boom" in text
    assert "re-exec deferred user_config_invalid (code at abcdef12)" in text
