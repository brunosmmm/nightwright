"""SPEC-0410: the grouped queue rows agree with `nw queue backlog` and `dashboard_payload`."""
import json


from test_agentic_dashboard import _campaign, _fixture, _iso, _park_fixture
from test_agentic_e2e import run_cli
from nightwright.agentic import queue as Q
from nightwright.tui import agentic_model as AM


def _seed(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    for i, state in enumerate(("CLOSING", "REVIEWING", "TRIAGING", "OPENING", "SPEC_REVIEW")):
        _campaign(cfg, repo, f"SPEC-0001-s{i}", state, created_min=20 + i)
    _campaign(cfg, repo, "SPEC-0001-wait", "WAITING",
              waiting={"resume_to": "GATING", "since": _iso(5)})
    _campaign(cfg, repo, "SPEC-0001-q2", "QUEUED", created_min=10)
    _campaign(cfg, repo, "SPEC-0001-dirty", "PARKED",
              park={"reason": "main_dirty", "detail": "d", "resume_to": "GATING"})
    return repo, cfg, ids


def test_queued_order_equals_nw_queue_backlog_json(tmp_path, monkeypatch):
    repo, cfg, ids = _seed(tmp_path)
    r = run_cli(cfg, monkeypatch, ["queue", "backlog", "--json"])
    assert r.exit_code == 0, r.output
    want = [i["campaign"] for i in json.loads(r.output)["items"]]
    rows, _ = AM.load_queue(cfg)
    got = [x.campaign for x in rows if x.kind == "campaign" and x.group == "queued"]
    assert want and got == want


def test_every_group_matches_its_dashboard_list(tmp_path):
    repo, cfg, ids = _seed(tmp_path)
    p = Q.dashboard_payload(cfg, recent=20)
    rows = AM.rows_from_payload(p)
    by_group: dict = {}
    for r in rows:
        if r.kind == "campaign":
            by_group.setdefault(r.group, set()).add(r.campaign)
    assert set().union(*(by_group.get(g, set()) for g in
                         ("closing", "reviewing", "gating", "building"))) == {
        a["campaign"] for a in p["active"]}
    for group, key in (("needs you", "needs_you"), ("conductor", "conductor_handling"),
                       ("waiting", "waiting"), ("queued", "backlog"),
                       ("delivered", "delivered")):
        assert by_group.get(group, set()) == {i["campaign"] for i in p[key]}, group
    for state, group in (("CLOSING", "closing"), ("REVIEWING", "reviewing"),
                         ("TRIAGING", "gating"), ("OPENING", "building"),
                         ("SPEC_REVIEW", "building")):
        cid = next(a["campaign"] for a in p["active"] if a["state"] == state)
        assert cid in by_group[group]


def test_a_deferred_park_is_in_both_groups(tmp_path):
    repo, cfg, ids = _park_fixture(tmp_path)
    p = Q.dashboard_payload(cfg, recent=20)
    rows = AM.rows_from_payload(p)
    keys = {(r.group, r.campaign) for r in rows if r.kind == "campaign"}
    assert ("conductor", ids["req"]) in keys and ("building", ids["req"]) in keys


def test_mounted_table_order_equals_rows_from_payload(tmp_path):
    from test_tui_queue_tab import SIZE, _desk, _run, settle, view
    repo, cfg, ids = _seed(tmp_path)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = view(app)
            want = [f"{r.group}|{r.campaign}" if r.kind == "campaign" else f"#{r.group}"
                    for r in AM.rows_from_payload(Q.dashboard_payload(cfg, recent=20))]
            assert [k.value for k in v.table.rows] == want

    _run(drive())
