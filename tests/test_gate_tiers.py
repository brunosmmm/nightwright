"""SPEC-0328: gate tiers (`when: loop | close | always`).

Unit: `gates_for` filtering, the `always` default, validation. Integration: a campaign's GATING
state runs only loop + always gates and the close re-gate only close + always gates; a config
without `when` keys (comfort-assistant style) runs every gate in both, as before."""
import pytest

from test_agentic_e2e import _git, _write, added, impl_done, make_cfg, make_repo, review, run_cli, script
from nightwright.agentic import project as P
from nightwright.agentic import store as S

pytestmark = pytest.mark.fast

BASE = {"schema": "nw.project.v1"}


def _doc(*gates):
    return {**BASE, "gates": [{"name": n, "cmd": "true", **({"when": w} if w else {})}
                              for n, w in gates]}


def _names(gates):
    return [g["name"] for g in gates]


def test_when_defaults_to_always_and_is_not_filled_in():
    cfg = P._with_defaults(_doc(("a", None)))
    assert "when" not in cfg["gates"][0]          # the resolved gate dict is unchanged
    assert P.gate_when(cfg["gates"][0]) == "always"


def test_gates_for_filters_by_tier_keeping_order():
    doc = _doc(("lint", None), ("fast", "loop"), ("full", "close"), ("smoke", "always"))
    P.validate(doc)
    cfg = P._with_defaults(doc)
    assert _names(P.gates_for(cfg, "loop")) == ["lint", "fast", "smoke"]
    assert _names(P.gates_for(cfg, "close")) == ["lint", "full", "smoke"]
    assert _names(P.gates_for(cfg, None)) == ["lint", "fast", "full", "smoke"]


def test_gates_for_rejects_an_unknown_tier():
    with pytest.raises(ValueError, match="unknown gate tier"):
        P.gates_for(P._with_defaults(_doc(("a", None))), "always")


@pytest.mark.parametrize("bad", ["sometimes", "", 1, True, ["loop"]])
def test_validation_rejects_bad_when(bad):
    doc = {**BASE, "gates": [{"name": "a", "cmd": "true", "when": bad}]}
    with pytest.raises(ValueError, match=r"gates\[0\]\.when: must be one of loop, close, always"):
        P.validate(doc)


@pytest.mark.parametrize("ok", ["loop", "close", "always"])
def test_validation_accepts_each_tier(ok):
    P.validate({**BASE, "gates": [{"name": "a", "cmd": "true", "when": ok}]})


def test_starter_template_documents_when():
    assert "when: always" in P.STARTER
    P.validate(__import__("yaml").safe_load(P.STARTER.format(project="X")))


def test_comfort_assistant_style_config_is_unchanged():
    """No `when` anywhere: both tiers run every gate, exactly as before SPEC-0328."""
    doc = {**BASE, "project": "comfort-assistant", "gates": [
        {"name": "lint", "cmd": "ruff check ."},
        {"name": "tests", "cmd": "pytest -q", "timeout_s": 900, "results": {"format": "pytest"},
         "retries": 1}]}
    P.validate(doc)
    cfg = P._with_defaults(doc)
    assert P.gates_for(cfg, "loop") == P.gates_for(cfg, "close") == cfg["gates"]
    assert cfg["gates"][1] == {"cwd": ".", "timeout_s": 900, "required": True, "retries": 1,
                               "name": "tests", "cmd": "pytest -q",
                               "results": {"format": "pytest"}}


# ---- integration: GATING vs CLOSING -----------------------------------------------------------

TIERED_YAML = """schema: nw.project.v1
base: main
gates:
  - {name: always-g, cmd: 'true', timeout_s: 30}
  - {name: loop-g, cmd: 'true', timeout_s: 30, when: loop}
  - {name: close-g, cmd: 'true', timeout_s: 30, when: close}
merge:
  policy: merge
  base: main
  remote: origin
agents:
  commit_trailer: "Co-Authored-By: nightwright test <nightwright@test>"
"""


def _gate_events(cfg, cid):
    return [[r["name"] for r in e["results"]] for e in S.read_ledger(cfg, cid, types=["gate"])]


def test_gating_runs_loop_and_always_close_runs_close_and_always(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    _write(repo, ".nw/project.yaml", TIERED_YAML)
    _git(repo, "commit", "-qam", "tiered gates")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), review()]))
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle", "--json-lines"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert _gate_events(cfg, cid) == [["always-g", "loop-g"], ["always-g", "close-g"]]


def test_untiered_config_runs_every_gate_in_both_states(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)        # the e2e PROJECT_YAML: two gates, no `when`
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), review()]))
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle", "--json-lines"])
    assert r.exit_code == 0, r.output
    assert S.read_state(cfg, cid)["state"] == "MERGED"
    assert _gate_events(cfg, cid) == [["test", "lint"], ["test", "lint"]]
