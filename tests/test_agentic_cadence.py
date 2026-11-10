"""SPEC-0491: cadence config validation, state, merge counter, `due`, consumption, status."""
import datetime as dt
import json

import pytest

from nightwright.agentic import cadence as CAD
from nightwright.agentic import project as P
from nightwright.agentic import queue as Q
from nightwright.agentic import sentinel as SN

pytestmark = pytest.mark.fast

T0 = dt.datetime(2026, 11, 9, 12, 0, 0, tzinfo=dt.timezone.utc).timestamp()
H = 3600


def _doc(*jobs, gates=()):
    return {"schema": P.SCHEMA, "project": "demo", "gates": list(gates), "cadence": list(jobs)}


def _problems(*jobs, gates=()):
    with pytest.raises(ValueError) as e:
        P.validate(_doc(*jobs, gates=gates))
    return str(e.value)


def _job(**kw):
    return {"name": "full", "cmd": "true", **({"every_n_merges": 2} | kw)}


@pytest.fixture
def cfg(tmp_path):
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "cfg")}


def _pcfg(*jobs):
    return {"cadence": list(jobs)}


# ---- schema ------------------------------------------------------------------------------

def test_valid_job_and_cadence_gate_defaults():
    P.validate(_doc(_job()))
    g = P.cadence_gate(_job())
    assert g == {"name": "full", "cmd": "true", "timeout_s": 1200, "retries": 0,
                 "required": True, "when": "close"}
    g = P.cadence_gate(_job(timeout_s=9, retries=1, retry_cmd="pytest --lf {failing}",
                            results={"format": "pytest"}))
    assert g["timeout_s"] == 9 and g["retries"] == 1 and g["results"] == {"format": "pytest"}
    assert "paths" not in g and "cwd" not in g


@pytest.mark.parametrize("job,frag", [
    ({"name": "Bad_Name", "cmd": "x", "always": True}, "cadence[0].name"),
    ({"cmd": "x", "always": True}, "cadence[0].name"),
    ({"name": "a", "always": True}, "cadence[0].cmd"),
    ({"name": "a", "cmd": "x", "bogus": 1, "always": True}, "cadence[0].bogus: unknown key"),
    ({"name": "a", "cmd": "x", "always": False, "on_paths": []}, "needs a trigger"),
    ({"name": "a", "cmd": "x"}, "needs a trigger"),
    ({"name": "a", "cmd": "x", "every_n_merges": True}, "every_n_merges"),
    ({"name": "a", "cmd": "x", "every_n_merges": 0}, "every_n_merges"),
    ({"name": "a", "cmd": "x", "every_hours": True}, "every_hours"),
    ({"name": "a", "cmd": "x", "every_hours": 0}, "every_hours"),
    ({"name": "a", "cmd": "x", "on_paths": "a"}, "on_paths"),
    ({"name": "a", "cmd": "x", "always": True, "retries": 4}, "retries"),
    ({"name": "a", "cmd": "x", "always": True, "retries": True}, "retries"),
    ({"name": "a", "cmd": "x", "always": True, "retry_cmd": "pytest"}, "retry_cmd"),
    ({"name": "a", "cmd": "x", "always": True, "retry_cmd": "p {failing}"}, "retry_cmd"),
    ({"name": "a", "cmd": "x", "always": True, "timeout_s": 0}, "timeout_s"),
])
def test_invalid_jobs_name_the_key_path(job, frag):
    assert frag in _problems(job)


def test_duplicate_and_gate_names_rejected():
    assert "duplicate" in _problems(_job(), _job())
    assert "gate name" in _problems(_job(name="unit"),
                                    gates=[{"name": "unit", "cmd": "true"}])
    with pytest.raises(ValueError, match="cadence: must be a list"):
        P.validate({"schema": P.SCHEMA, "cadence": {}})


def test_opt_in_predicate_is_the_raw_nonempty_list(tmp_path):
    for body, want in (("", False), ("cadence: []\n", False),
                       ("cadence:\n  - {name: a, cmd: x, always: true}\n", True)):
        (tmp_path / ".nw").mkdir(exist_ok=True)
        (tmp_path / ".nw" / "project.yaml").write_text("schema: nw.project.v1\n" + body)
        assert P.project_has_cadence(tmp_path) is want
        assert P.load_project_config(tmp_path / ".nw" / "project.yaml")["cadence"] == (
            [{"name": "a", "cmd": "x", "always": True}] if want else [])


# ---- state, seeding, merges --------------------------------------------------------------

def _state(cfg):
    return json.loads(CAD.state_path(cfg, "demo").read_text())


def test_no_cadence_writes_no_file(cfg):
    for pc in ({}, _pcfg()):
        CAD.ensure(cfg, "demo", project_cfg=pc)
        CAD.record_merge(cfg, "demo", "s1", ["a"], project_cfg=pc)
        assert not CAD.state_path(cfg, "demo").exists()


def test_merge_before_any_tick_seeds_and_counts(cfg):
    pc = _pcfg(_job(on_paths=["src/**"]))
    CAD.record_merge(cfg, "demo", "s1", ["src/a.py", "docs/x.md"], project_cfg=pc, now=T0)
    st = _state(cfg)
    row = st["jobs"]["full"]
    assert st["schema"] == "nw.cadence.v1" and st["last_merge_sha"] == "s1"
    assert row["merges_since"] == 1 and row["paths_hit"] == ["src/a.py"]
    assert row["seeded_at"].startswith("2026-11-09") and "inflight" not in row
    assert set(row) == {"merges_since", "paths_hit", "seeded_at", "last_run_at", "last_run_sha",
                        "last_ok", "last_green_at", "last_green_sha", "last_error", "forced"}


def test_ensure_never_resets_and_record_merge_is_idempotent(cfg):
    pc = _pcfg(_job())
    CAD.record_merge(cfg, "demo", "s1", [], project_cfg=pc, now=T0)
    seeded = _state(cfg)["jobs"]["full"]["seeded_at"]
    CAD.ensure(cfg, "demo", project_cfg=_pcfg(_job(), _job(name="b")), now=T0 + 500)
    CAD.record_merge(cfg, "demo", "s1", [], project_cfg=pc, now=T0 + 600)
    jobs = _state(cfg)["jobs"]
    assert jobs["full"]["merges_since"] == 1 and jobs["full"]["seeded_at"] == seeded
    assert jobs["b"]["merges_since"] == 0
    CAD.record_merge(cfg, "demo", "s2", [], project_cfg=pc)
    assert _state(cfg)["jobs"]["full"]["merges_since"] == 2


def test_unknown_paths_marker_and_cap(cfg):
    pc = _pcfg(_job(on_paths=["a.py"]), _job(name="nopaths"))
    CAD.record_merge(cfg, "demo", "s1", None, project_cfg=pc, now=T0)
    jobs = _state(cfg)["jobs"]
    assert jobs["full"]["paths_hit"] == ["*"] and jobs["nopaths"]["paths_hit"] == []
    assert jobs["full"]["merges_since"] == 1
    for i in range(25):
        CAD.record_merge(cfg, "demo", f"m{i}", ["a.py"], project_cfg=pc)
    assert len(_state(cfg)["jobs"]["full"]["paths_hit"]) == 20


def test_unreadable_state_is_renamed_and_journalled(cfg):
    path = CAD.state_path(cfg, "demo")
    assert CAD._read_doc(path) is None
    pc = _pcfg(_job())
    CAD.ensure(cfg, "demo", project_cfg=pc)
    assert not Q.runner_journal_path(cfg).exists()          # missing file: no journal entry
    for bad in ("{nope", "[]", json.dumps({"schema": "x", "jobs": {}}),
                json.dumps({"schema": CAD.SCHEMA, "jobs": []})):
        path.write_text(bad)
        CAD.record_merge(cfg, "demo", f"s-{len(bad)}", [], project_cfg=pc)
        assert _state(cfg)["jobs"]["full"]["merges_since"] == 1
    assert len(list(path.parent.glob("demo.json.bad-*"))) == 4
    kinds = [json.loads(l)["kind"] for l in Q.runner_journal_path(cfg).read_text().splitlines()]
    assert kinds == ["cadence_unreadable"] * 4


def test_oserror_leaves_the_file_in_place(cfg, monkeypatch):
    pc = _pcfg(_job())
    CAD.ensure(cfg, "demo", project_cfg=pc)
    path = CAD.state_path(cfg, "demo")
    real = type(path).read_text

    def boom(self, *a, **k):
        if self == path:
            raise PermissionError("denied")
        return real(self, *a, **k)

    monkeypatch.setattr(type(path), "read_text", boom)
    with pytest.raises(OSError):
        CAD.record_merge(cfg, "demo", "s1", [], project_cfg=pc)
    CAD.ensure(cfg, "demo", project_cfg=pc)                  # journalled, not raised
    monkeypatch.undo()
    assert path.exists() and not list(path.parent.glob("*.bad-*"))
    assert "cadence_unreadable" in Q.runner_journal_path(cfg).read_text()


# ---- due ---------------------------------------------------------------------------------

def _row(**kw):
    return {**CAD.initial_row(T0), **kw}


def _iso(t):
    return CAD._iso(t)


def test_due_reasons_each():
    job = {"name": "j", "cmd": "x", "always": True, "every_n_merges": 3, "on_paths": ["a"],
           "every_hours": 2}
    assert CAD.due(job, _row(forced=True), "B", T0) == "forced"
    assert CAD.due(job, _row(merges_since=1), "B", T0) == "always"
    j2 = {**job, "always": False}
    assert CAD.due(j2, _row(merges_since=3), "B", T0) == "merges"
    assert CAD.due(j2, _row(paths_hit=["*"]), "B", T0) == "paths"
    assert CAD.due(j2, _row(), "B", T0 + 2 * H - 1) is None
    assert CAD.due(j2, _row(), "B", T0 + 2 * H) == "hours"
    red = _row(last_run_sha="A", last_ok=False, last_run_at=_iso(T0))
    assert CAD.due(j2, red, "A", T0 + SN.RETRY_S) == "retry"


def test_green_same_sha_idle_unless_forced():
    job = {"name": "j", "cmd": "x", "every_hours": 1}
    green = _row(last_run_sha="A", last_ok=True, last_run_at=_iso(T0), merges_since=9,
                 paths_hit=["a"])
    assert CAD.due(job, green, "A", T0 + 100 * H) is None
    assert CAD.due(job, {**green, "forced": True}, "A", T0) == "forced"
    assert CAD.due({**job, "always": True}, green, "B", T0) == "always"


@pytest.mark.parametrize("ok", [False, None])
def test_red_or_failed_same_sha_backoff(ok):
    job = {"name": "j", "cmd": "x", "every_n_merges": 1, "every_hours": 1}
    row = _row(last_run_sha="A", last_ok=ok, last_run_at=_iso(T0), merges_since=5)
    assert CAD.due(job, row, "A", T0 + SN.RETRY_S - 1) is None      # trigger matches, still None
    assert CAD.due(job, row, "A", T0 + SN.RETRY_S) == "retry"
    assert CAD.due({**job, "every_n_merges": 99}, _row(last_run_sha="A", last_ok=ok,
                                                       last_run_at=_iso(T0)), "A",
                   T0 + SN.RETRY_S) == "retry"
    assert CAD.due(job, {**row, "forced": True}, "A", T0 + 1) == "forced"
    # a moved base leaves the backoff
    assert CAD.due(job, row, "B", T0 + 1) == "merges"


def test_never_run_job_rules():
    job = {"name": "j", "cmd": "x", "every_n_merges": 2, "every_hours": 12}
    assert CAD.due(job, _row(merges_since=2), None, T0) == "merges"
    only_h = {"name": "j", "cmd": "x", "every_hours": 12}
    assert CAD.due(only_h, _row(), None, T0 + 12 * H - 1) is None
    assert CAD.due(only_h, _row(), None, T0 + 12 * H) == "hours"
    assert CAD.due({"name": "j", "cmd": "x", "every_n_merges": 2}, _row(), None, T0 + 99 * H) is None


# ---- consumption -------------------------------------------------------------------------

def _prime(cfg, **job):
    pc = _pcfg(_job(on_paths=["*.py"], **job))
    CAD.record_merge(cfg, "demo", "m0", ["a.py"], project_cfg=pc, now=T0)
    return pc


def test_begin_snapshots_and_finish_keeps_later_hits(cfg):
    pc = _prime(cfg)
    CAD.begin(cfg, "demo", "full", "A", now=T0 + 1)
    row = _state(cfg)["jobs"]["full"]
    assert row["inflight"] == {"merges": 1, "paths": ["a.py"], "sha": "A", "forced": False}
    assert row["merges_since"] == 1 and row["forced"] is False
    CAD.record_merge(cfg, "demo", "m1", ["a.py"], project_cfg=pc)
    CAD.finish(cfg, "demo", "full", True, "A", None, now=T0 + 5)
    row = _state(cfg)["jobs"]["full"]
    assert row["merges_since"] == 1 and row["paths_hit"] == ["a.py"] and "inflight" not in row
    assert row["last_ok"] is True and row["last_run_sha"] == "A"
    assert row["last_green_sha"] == "A" and row["last_green_at"] == row["last_run_at"]


def test_abort_restores_forced_without_touching_counters(cfg):
    _prime(cfg)
    st = _state(cfg)
    st["jobs"]["full"]["forced"] = True
    CAD.state_path(cfg, "demo").write_text(json.dumps(st))
    CAD.begin(cfg, "demo", "full", "A")
    assert _state(cfg)["jobs"]["full"]["forced"] is False
    CAD.abort(cfg, "demo", "full")
    row = _state(cfg)["jobs"]["full"]
    assert row["forced"] is True and row["merges_since"] == 1 and "inflight" not in row
    assert row["last_run_at"] is None


def test_red_then_green_last_green_fields(cfg):
    _prime(cfg)
    CAD.begin(cfg, "demo", "full", "A", now=T0)
    CAD.finish(cfg, "demo", "full", False, "A", "red", now=T0)
    row = _state(cfg)["jobs"]["full"]
    assert row["last_ok"] is False and row["last_green_sha"] is None and row["last_error"] == "red"
    CAD.begin(cfg, "demo", "full", "B", now=T0 + 10)
    CAD.finish(cfg, "demo", "full", None, "B", "boom", now=T0 + 10)
    assert _state(cfg)["jobs"]["full"]["last_ok"] is None
    CAD.begin(cfg, "demo", "full", "C", now=T0 + 20)
    CAD.finish(cfg, "demo", "full", True, "C", None, now=T0 + 20)
    row = _state(cfg)["jobs"]["full"]
    assert row["last_green_sha"] == "C" and row["last_error"] is None


def test_crash_reconciles_to_a_failed_run_at_the_new_sha(cfg):
    pc = _prime(cfg)
    CAD.begin(cfg, "demo", "full", "A", now=T0)
    CAD.finish(cfg, "demo", "full", True, "A", None, now=T0)
    CAD.record_merge(cfg, "demo", "m1", ["a.py"], project_cfg=pc)
    CAD.begin(cfg, "demo", "full", "B", now=T0 + 100)          # ...then the runner dies
    assert "inflight" in _state(cfg)["jobs"]["full"]
    CAD.reconcile(cfg, "demo", now=T0 + 200)
    row = _state(cfg)["jobs"]["full"]
    assert row["last_ok"] is None and row["last_run_sha"] == "B" and row["last_error"] == "crashed"
    assert row["merges_since"] == 0 and "inflight" not in row and row["last_green_sha"] == "A"
    job = pc["cadence"][0]
    assert CAD.due(job, row, "B", T0 + 200 + SN.RETRY_S - 1) is None
    assert CAD.due(job, row, "B", T0 + 200 + SN.RETRY_S) == "retry"


def test_begin_reconciles_a_leftover_inflight(cfg):
    _prime(cfg)
    CAD.begin(cfg, "demo", "full", "A", now=T0)
    CAD.begin(cfg, "demo", "full", "B", now=T0 + 5)
    row = _state(cfg)["jobs"]["full"]
    assert row["last_error"] == "crashed" and row["last_run_sha"] == "A"
    assert row["inflight"]["sha"] == "B"


# ---- status rows -------------------------------------------------------------------------

def test_status_unseeded_creates_nothing(cfg):
    rows = CAD.status_rows(cfg, "demo", project_cfg=_pcfg(_job()), base_sha="A", now=T0)
    assert rows == [{"name": "full", "triggers": ["every_n_merges"], "merges_since": 0,
                     "every_n_merges": 2, "paths_hit": [], "last_run_at": None,
                     "last_run_sha": None, "last_ok": None, "due": None,
                     "next": "not seeded", "running": "no"}]
    assert list(rows[0]) == list(CAD.STATUS_KEYS)
    assert not CAD.state_path(cfg, "demo").exists()


def test_status_next_strings(cfg):
    job = {"name": "full", "cmd": "x", "always": True, "every_n_merges": 3, "on_paths": ["a"],
           "every_hours": 5}
    pc = _pcfg(job)
    CAD.ensure(cfg, "demo", project_cfg=pc, now=T0)

    def row(now=T0):
        return CAD.status_rows(cfg, "demo", project_cfg=pc, base_sha="A", now=now)[0]

    r = row()
    assert r["triggers"] == ["always", "every_n_merges", "on_paths", "every_hours"]
    assert r["due"] is None and r["next"] == "on the next merge"
    CAD.record_merge(cfg, "demo", "m1", ["zzz"], project_cfg=pc)
    r = row()
    assert r["due"] == "always" and r["next"] is None and r["merges_since"] == 1
    j2 = {**job, "always": False, "on_paths": []}
    pc2 = _pcfg(j2)
    r = CAD.status_rows(cfg, "demo", project_cfg=pc2, base_sha="A", now=T0)[0]
    assert r["next"] == "in 2 merges"
    j3 = {"name": "full", "cmd": "x", "every_hours": 5}
    r = CAD.status_rows(cfg, "demo", project_cfg=_pcfg(j3), base_sha="A", now=T0)[0]
    assert r["next"].startswith("at 2026-11-09T")
    CAD.begin(cfg, "demo", "full", "A", now=T0)
    CAD.finish(cfg, "demo", "full", True, "A", None, now=T0)
    r = CAD.status_rows(cfg, "demo", project_cfg=pc2, base_sha="A", now=T0 + 1)[0]
    assert r["due"] is None and r["next"] == "when the base moves" and r["last_ok"] is True
