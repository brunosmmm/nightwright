"""SPEC-0558: the per-campaign lean opt-in. `nw queue add --lean` / `--lean-flags a,b` /
`--cohort NAME`: parsing and refusals, the pin into `state.lean` at create (overriding the
config for that campaign only), the cohort tag on state, the `open` event and the export, a
campaign queued without the options left exactly as before, and the persona models of a lean
Claude campaign under the user's `cursor` profile shape."""
import json
import os
import subprocess

import pytest
import yaml

from test_agentic_e2e import (adv_review, added, impl_done, make_cfg, make_repo, review,
                              run_cli, script, _PerPersona)
from test_agentic_effort_ceiling import USER_CONFIG_PROFILES, USER_CURSOR_PROFILE
from nightwright.agentic import campaign_export as CE
from nightwright.agentic import personas as PS
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S
from nightwright.agentic import tiers as T
from nightwright.agentic.profiles import resolve_profile

pytestmark = pytest.mark.integration

CONF, ADV = "conformance_reviewer", "adversarial_reviewer"
ALL_ON = {"tiers": True, "packet": True, "collator": True, "sessions": True}
ALL_OFF = {f: False for f in T.LEAN_FLAGS}
NEW_KEYS = {"lean", "lean_request", "cohort"}


# ---- parsing ---------------------------------------------------------------------------------

def test_lean_request_parsing():
    assert T.lean_request() is None and T.lean_request(False, None) is None
    assert T.lean_request(True) == list(T.LEAN_FLAGS)
    assert T.lean_request(lean_flags="packet,tiers") == ["tiers", "packet"]    # flag order
    assert T.lean_request(lean_flags=" collator , packet,packet") == ["packet", "collator"]
    for bad, msg in ((dict(lean=True, lean_flags="tiers"), "--lean-flags alone"),
                     (dict(lean_flags=""), "at least one"), (dict(lean_flags=" , "), "at least one"),
                     (dict(lean_flags="tiers,turbo"), "unknown lean flag turbo")):
        with pytest.raises(ValueError, match=msg):
            T.lean_request(**bad)


def test_cohort_default_and_validation():
    assert T.campaign_cohort(None, None) is None
    assert T.campaign_cohort(None, ["tiers"]) == "lean"
    assert T.campaign_cohort("control", None) == "control"
    assert T.campaign_cohort(" ab-1.x_y ", ["tiers"]) == "ab-1.x_y"
    for bad in ("", "-x", "a b", "x" * 41, "a/b"):
        with pytest.raises(ValueError, match="bad cohort"):
            T.campaign_cohort(bad, None)


def test_effective_lean_request_replaces_config_and_keeps_dependencies():
    cfg = {"agentic": {"lean": {"tiers": True, "packet": True},
                       "project_lean": {"P": {"collator": True}}}}
    assert T.effective_lean(cfg, "P") == (
        {"tiers": True, "packet": True, "collator": True, "sessions": False}, [])
    assert T.effective_lean(cfg, "P", request=["packet"]) == (dict(ALL_OFF, packet=True), [])
    assert T.effective_lean({}, "P", request=list(T.LEAN_FLAGS)) == (ALL_ON, [])
    flags, why = T.effective_lean(cfg, "P", request=["tiers", "collator", "sessions"])
    assert flags == dict(ALL_OFF, tiers=True)
    assert why == ["collator off: needs packet", "sessions off: needs packet and collator"]
    assert T.effective_lean(cfg, "P", request=None) == T.effective_lean(cfg, "P")
    with pytest.raises(ValueError, match="unknown lean flag"):
        T.effective_lean(cfg, "P", request=["nope"])


# ---- through `nw queue add` --------------------------------------------------------------------

def _setup(tmp_path, *, config_lean=None):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []), profile={"reviewers": [CONF, ADV]})
    cfg["agentic"]["profiles"]["thorough"] = {"reviewers": [CONF, ADV]}
    if config_lean is not None:
        cfg["agentic"]["lean"] = config_lean
    return repo, cfg


def _drive(cfg, cid, runner, ticks=20):
    for _ in range(ticks):
        Q.tick(cfg, runner=runner)
        if S.read_state(cfg, cid)["state"] in ("MERGED", "PARKED"):
            break
    return S.read_state(cfg, cid)


def _starts(cfg, cid):
    return sorted((e["persona"], e["model"], e.get("tier"))
                  for e in S.read_ledger(cfg, cid, types=["run.start"]))


def _open(cfg, cid):
    return S.read_ledger(cfg, cid, types=["open"])[0]


def test_lean_pins_all_four_flags_and_the_cohort(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path)                                   # config: every flag off
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--lean", "--json"])
    assert r.exit_code == 0, r.output
    plan = json.loads(r.output)
    assert plan["written"] is False and plan["lean"] == ALL_ON
    assert plan["lean_request"] == list(T.LEAN_FLAGS) and plan["cohort"] == "lean"
    cid = added(cfg, monkeypatch, "SPEC-0001", "--lean", "--runner", "claude")
    st = S.read_state(cfg, cid)
    assert st["lean"] == ALL_ON and st["lean_request"] == list(T.LEAN_FLAGS)
    assert st["cohort"] == "lean" and st["runner"] == "claude"
    ev = _open(cfg, cid)
    assert ev["lean"] == ALL_ON and ev["lean_request"] == list(T.LEAN_FLAGS)
    assert ev["cohort"] == "lean" and "lean_off" not in ev
    camp = CE.export_tables(cfg)["campaigns"][0]
    assert (camp["cohort"], camp["lean_request"], camp["lean"]) == (
        "lean", list(T.LEAN_FLAGS), ALL_ON)


def test_lean_flags_subset_runs_tier_models_while_config_is_off(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch, "SPEC-0001", "--lean-flags", "tiers", "--cohort", "tiers-only")
    st = S.read_state(cfg, cid)
    assert st["lean"] == dict(ALL_OFF, tiers=True) and st["cohort"] == "tiers-only"
    runner = _PerPersona({"implementer": [impl_done()], CONF: [review()], ADV: [adv_review()]})
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    assert _starts(cfg, cid) == [(ADV, "claude-sonnet-5-5", "judge"),
                                 (CONF, "claude-sonnet-5-5", "judge"),
                                 ("implementer", "claude-sonnet-5-5", "judge")]


def test_lean_flags_dependency_switched_off_with_a_reason(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch, "SPEC-0001", "--lean-flags", "tiers,sessions")
    st = S.read_state(cfg, cid)
    assert st["lean"] == dict(ALL_OFF, tiers=True) and st["lean_request"] == ["tiers", "sessions"]
    assert _open(cfg, cid)["lean_off"] == ["sessions off: needs packet and collator"]


def test_opt_in_overrides_the_config_for_that_campaign_only(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, config_lean={"tiers": True})
    cid = added(cfg, monkeypatch, "SPEC-0001", "--lean-flags", "packet")
    assert S.read_state(cfg, cid)["lean"] == dict(ALL_OFF, packet=True)   # tiers not inherited


def test_lean_caps_the_profile_flag(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch, "SPEC-0001", "--lean", "--profile", "thorough")
    st = S.read_state(cfg, cid)
    assert st["profile"] == "balanced" and st["sizing"]["flag"] == "thorough"


def test_cohort_without_lean_only_tags(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch, "SPEC-0001", "--cohort", "control")
    st = S.read_state(cfg, cid)
    assert st["cohort"] == "control" and "lean" not in st and "lean_request" not in st
    assert _open(cfg, cid)["cohort"] == "control"
    assert CE.export_tables(cfg)["campaigns"][0]["cohort"] == "control"


@pytest.mark.parametrize("args,msg", [
    (["--lean", "--lean-flags", "tiers"], "--lean-flags alone"),
    (["--lean-flags", "tiers,warp"], "unknown lean flag warp"),
    (["--lean-flags", ","], "at least one"),
    (["--lean", "--cohort", "a b"], "bad cohort"),
])
def test_cli_refusals_write_nothing(tmp_path, monkeypatch, args, msg):
    repo, cfg = _setup(tmp_path)
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--yes", *args])
    assert r.exit_code != 0 and msg in r.output
    assert S.list_campaigns(cfg) == []


@pytest.mark.parametrize("config_lean", [None, {"tiers": True}])
def test_without_options_the_campaign_is_unchanged(tmp_path, monkeypatch, config_lean):
    """No --lean / --lean-flags / --cohort: state, open event and runs exactly as before
    SPEC-0558 (the config decides; flag-off writes no lean keys at all)."""
    repo, cfg = _setup(tmp_path, config_lean=config_lean)
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--json"])
    assert not {"lean_request", "cohort"} & set(json.loads(r.output))
    cid = added(cfg, monkeypatch)
    st, ev = S.read_state(cfg, cid), _open(cfg, cid)
    assert not {"lean_request", "cohort"} & set(st) and not {"lean_request", "cohort"} & set(ev)
    runner = _PerPersona({"implementer": [impl_done()], CONF: [review()], ADV: [adv_review()]})
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    camp = CE.export_tables(cfg)["campaigns"][0]
    assert (camp["cohort"], camp["lean_request"]) == (None, None)
    if config_lean is None:
        assert not NEW_KEYS & set(st) and camp["lean"] is None
        assert _starts(cfg, cid) == [(ADV, "opus", None), (CONF, "opus", None),
                                     ("implementer", "sonnet", None)]
    else:
        assert st["lean"] == dict(ALL_OFF, tiers=True)               # the config pin, as before
        assert {m for _, m, _ in _starts(cfg, cid)} == {"claude-sonnet-5-5"}


def test_lean_and_normal_campaigns_side_by_side(tmp_path, monkeypatch):
    from nightwright import org_write as OW
    repo, cfg = _setup(tmp_path)
    _, _, iid = OW.add_idea(cfg, "an idea to build")
    lean_cid = added(cfg, monkeypatch, "SPEC-0001", "--lean")
    plain_cid = added(cfg, monkeypatch, iid)
    assert S.read_state(cfg, lean_cid)["lean"] == ALL_ON
    assert not NEW_KEYS & set(S.read_state(cfg, plain_cid))
    rows = {c["campaign"]: c for c in CE.export_tables(cfg)["campaigns"]}
    assert (rows[lean_cid]["cohort"], rows[plain_cid]["cohort"]) == ("lean", None)


def test_idea_campaign_opt_in(tmp_path, monkeypatch):
    from nightwright import org_write as OW
    repo, cfg = _setup(tmp_path)
    _, _, iid = OW.add_idea(cfg, "an idea to build")
    cid = added(cfg, monkeypatch, iid, "--lean-flags", "tiers,packet", "--cohort", "ab")
    st = S.read_state(cfg, cid)
    assert st["lean"] == dict(ALL_OFF, tiers=True, packet=True) and st["cohort"] == "ab"


def test_batch_carries_the_opt_in_to_every_item(tmp_path, monkeypatch):
    from nightwright import org_write as OW
    repo, cfg = _setup(tmp_path)
    _, _, iid = OW.add_idea(cfg, "an idea to build")
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", iid, "--lean", "--yes", "--json"])
    assert r.exit_code == 0, r.output
    batch = json.loads(r.output)
    assert batch["params"]["lean_request"] == list(T.LEAN_FLAGS)
    assert batch["params"]["cohort"] == "lean"
    for st in S.list_campaigns(cfg):
        assert st["lean"] == ALL_ON and st["cohort"] == "lean"
    assert len(S.list_campaigns(cfg)) == 2


# ---- persona models of a lean Claude campaign (the user's profile shape) -----------------------

@pytest.fixture
def user_env(tmp_path, monkeypatch):
    """The user's config shape (2026-11-12): default profile `cursor` (the user file),
    `balanced` / `thorough` overrides, `model_set: claude`, `runner: claude`."""
    monkeypatch.delenv("NW_AGENTIC_RUNNER", raising=False)
    cfg_dir = tmp_path / "cfg"
    (cfg_dir / "personas").mkdir(parents=True)
    (cfg_dir / "profiles").mkdir()
    (cfg_dir / "profiles" / "cursor.yaml").write_text(yaml.safe_dump(USER_CURSOR_PROFILE))
    cfg = {"config_dir": str(cfg_dir), "data_dir": str(tmp_path / "data"),
           "agentic": {"runner": "claude", "profiles": USER_CONFIG_PROFILES,
                       "default_profile": "cursor", "model_set": "claude"}}
    root = tmp_path / "main"
    (root / ".nw").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    genv = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1", "PATH": os.environ["PATH"]}
    for args in (("init", "-q", "-b", "main"), ("commit", "-q", "--allow-empty", "-m", "b")):
        subprocess.run(["git", "-C", str(wt), *args], check=True, capture_output=True, env=genv)
    flags, _ = T.effective_lean(cfg, "NoSuchProject", request=T.lean_request(True))
    state = {"id": "SPEC-0001-20261112-ab12", "kind": "implement", "spec_id": "SPEC-0001",
             "project": "NoSuchProject", "state": "REVIEWING", "runner": "claude",
             "lean": flags, "lean_request": list(T.LEAN_FLAGS), "cohort": "lean",
             "repo": {"root": str(root), "base": "main", "branch": "main", "worktree": str(wt)},
             "spec": {"rel_path": "docs/specs/0001-x.md"}, "counters": {"loops": 0}}
    return cfg, root, state, tmp_path


def _table(env, profile):
    cfg, root, state, tmp = env
    prof = resolve_profile(cfg, profile, project_root=root)
    out = {}
    for name in sorted(PS.PERSONAS):
        req = PS.build_request(cfg, prof, {"_root": str(root)}, dict(state), name,
                               run_dir=tmp / "runs" / f"001-{name}")
        out[name] = (req.model, req.effort, req.tier)
    return out


# What SPEC-0558's As shipped table records (the real config gave the same rows).
USER_LEAN_CLAUDE = {
    "adversarial_reviewer": ("sonnet", "high", "judge"),
    "conflict_resolver": ("sonnet", "high", "judge"),
    "conformance_reviewer": ("sonnet", "high", "judge"),
    "divergence_triager": ("claude-sonnet-5-5", "high", "judge"),
    "findings_collator": ("claude-haiku-5-5", "medium", "marshal"),
    "gate_triager": ("sonnet", "medium", "marshal"),
    "implementer": ("sonnet", "high", "judge"),
    "review_marshal": ("claude-haiku-5-5", "medium", "marshal"),
    "review_triager": ("sonnet", "medium", "marshal"),
    "spec_reviewer": ("sonnet", "high", "judge"),
    "spec_writer": ("sonnet", "high", "judge"),
    "test_reviewer": ("sonnet", "high", "judge"),
}


def test_user_cursor_profile_lean_claude_table(user_env):
    """The `cursor` profile file names `model: sonnet` for nine personas, which `explicit_model`
    treats as an override: those keep `sonnet` (effort clamped to high); only the personas
    it does not name take their tier. No Opus, no effort above high."""
    table = _table(user_env, "cursor")
    assert table == USER_LEAN_CLAUDE
    assert not any("opus" in m for m, _, _ in table.values())
    assert {e for _, e, _ in table.values()} <= {"low", "medium", "high"}


def test_builtin_profile_lean_claude_takes_every_tier(user_env):
    """Contrast: a builtin profile names no model above the builtin layer, so every persona
    runs its tier's model."""
    table = _table(user_env, "frugal")
    want = {"marshal": ("claude-haiku-5-5", "medium"), "judge": ("claude-sonnet-5-5", "high")}
    assert {n: (m, e) for n, (m, e, _) in table.items()} == {
        n: want[t] for n, (_, _, t) in table.items()}
