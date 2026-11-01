"""SPEC-0356: queue visibility in the CLI — the shared row model, the column fitter, width
snapshots of the three list commands, JSON parity, `nw queue graph`, `nw queue unqueued` and the
remote gate line of `nw campaign show`."""
import datetime as dt
import json
import os
from pathlib import Path

import pytest
from rich.console import Console

from test_agentic_dashboard import _campaign
from test_agentic_e2e import SPEC_ACCEPTED, make_cfg, make_repo, run_cli, script
from nightwright import org as ORG
from nightwright import org_write as OW
from nightwright.agentic import cli_campaign as CC
from nightwright.agentic import cli_queue as CQ
from nightwright.agentic import listview as LV
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S

DATA = Path(__file__).parent / "data" / "queue_visibility"
WIDTHS = (60, 100, 160)
IDS = {"impl": "SPEC-0338-20261031-23a7", "gate": "SPEC-0340-20261031-9b1c",
       "park": "SPEC-0341-20261031-c0de", "wait": "SPEC-0342-20261031-d00d",
       "queued": "SPEC-0343-20261031-e1e1"}
STATES = {"impl": "IMPLEMENTING", "gate": "GATING", "park": "PARKED", "wait": "WAITING",
          "queued": "QUEUED"}


def _spec(repo, n, *, title="Trivial feature", status="accepted", deps=(), kind=None,
          source_idea=None):
    extra = f"depends_on: [{', '.join(deps)}]\n" if deps else ""
    extra += f"kind: {kind}\n" if kind else ""
    extra += f"source_idea: {source_idea}\n" if source_idea else ""
    text = SPEC_ACCEPTED.format(id=f"SPEC-{n:04d}", extra=extra).replace(
        "Trivial feature", title).replace("status: accepted", f"status: {status}")
    path = repo / "docs" / "specs" / f"{n:04d}-s{n}.md"
    path.write_text(text)
    return f"SPEC-{n:04d}", f"docs/specs/{n:04d}-s{n}.md"


def _cols(monkeypatch, width):
    """The terminal width the CLI sees: COLUMNS, and the shared console's width (rich reads
    COLUMNS once at construction when it is set then, as under xdist)."""
    from nightwright.console import console
    monkeypatch.setenv("COLUMNS", str(width))
    monkeypatch.setattr(console, "_width", width)


def _render(renderable, width):
    c = Console(width=width, record=True, force_terminal=False)
    c.print(renderable)
    return c.export_text()


@pytest.fixture
def snap(tmp_path, monkeypatch):
    """Five campaigns with long ids, one per state: PARKED (budget_cap), WAITING and QUEUED
    blocked on two dependencies (SPEC-0337 accepted, SPEC-0399 missing)."""
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    _spec(repo, 337, title="Worker registry")
    titles = {"impl": "Remote gate dispatch over ssh workers",
              "gate": "Failed-first loop gate with a fast prelude",
              "park": "Coordinator re-attach and adoption after a crash",
              "wait": "nw workers bootstrap",
              "queued": "Lost-job retry across workers and escalation"}
    for i, key in enumerate(IDS):
        sid, rel = _spec(repo, 338 + i if key != "impl" else 338, title=titles[key],
                         deps=("SPEC-0337", "SPEC-0399") if key == "queued" else ())
        extra = {}
        if key == "park":
            extra["park"] = {"reason": "budget_cap", "detail": "cost 15.20 > cap 15",
                             "resume_to": "IMPLEMENTING", "since": "2026-10-31T10:00:00-04:00"}
        if key == "wait":
            extra["waiting"] = {"resume_to": "IMPLEMENTING"}
        _campaign(cfg, repo, IDS[key], STATES[key], spec_id=sid, rel=rel,
                  created_min=60 - i, priority=1 + i % 3, **extra)
        p = S.campaign_dir(cfg, IDS[key]) / "state.json"
        st = json.loads(p.read_text())
        st["updated_at"] = "2026-10-31T12:00:00-04:00"
        p.write_text(json.dumps(st))
    return cfg, repo


def _frozen(payload):
    """Ages and the lease line depend on the clock; pin them for the golden files."""
    payload = json.loads(json.dumps(payload))
    payload["lease"] = {"held": False}
    for it in payload["items"]:
        it["updated_age_s"] = it["age_s"] = 240
        it["run"] = None
        if it.get("park"):
            it["park"]["overdue"] = False
    return payload


def _tables(cfg):
    payload = _frozen(Q.queue_payload(cfg))
    backlog = [{**b, "age_s": 240} for b in Q.backlog_view(cfg)]
    items = [CC._summary(s, cfg, {b["campaign"]: b for b in backlog})
             for s in S.list_campaigns(cfg)]
    return {
        "queue_list": lambda w: CQ._queue_table(payload, w),
        "queue_backlog": lambda w: CQ._backlog_table(
            [{"campaign": b["campaign"], "kind": b.get("kind"), "label": b.get("label"),
              "project": b.get("project"), "title": b.get("title"), "age_s": b["age_s"],
              "backlog": b} for b in backlog], w),
        "campaign_list": lambda w: CC._list_table("Campaigns", items, w),
    }


def _headers(text):
    """Header cells of every table in a render (the line above each `───` rule)."""
    lines = text.splitlines()
    return [{h.strip() for h in lines[i - 1].split("  ")} - {""} for i, line in enumerate(lines)
            if line.startswith("─") and i]


@pytest.mark.parametrize("width", WIDTHS)
def test_width_snapshots(snap, width):
    cfg, _ = snap
    for name, table in _tables(cfg).items():
        out = _render(table(width), width)
        path = DATA / f"{name}_{width}.txt"
        if os.environ.get("NW_UPDATE_SNAPSHOTS"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(out)
        assert out == path.read_text(), f"{name} at {width}: regenerate with NW_UPDATE_SNAPSHOTS=1"
        assert max(map(len, out.splitlines())) <= width, (name, width)
        for key, cid in IDS.items():
            if name == "queue_backlog" and key != "queued":
                continue
            row = next(line for line in out.splitlines() if cid in line)    # the id is whole
            state = "QUEUED" if name == "queue_backlog" else STATES[key]
            if name == "queue_list" and key == "wait":
                state = "IMPLEMENTING"                          # `continues at`
            assert state in row and f"{state[:-1]}…" not in row, (name, width, row)


@pytest.mark.parametrize("width", WIDTHS)
def test_cli_lists_never_cut_ids_or_states(snap, monkeypatch, width):
    cfg, _ = snap
    _cols(monkeypatch, width)
    for args in (["campaign", "list"], ["queue", "list"], ["queue", "backlog"]):
        r = run_cli(cfg, monkeypatch, args)
        assert r.exit_code == 0, r.output
        lines = r.output.splitlines()
        assert max(map(len, lines)) <= width, (args, width, r.output)
        for key, cid in IDS.items():
            if args[1] == "backlog" and key != "queued":
                continue
            row = next((line for line in lines if cid in line), None)
            assert row is not None, (args, cid, r.output)       # the id is whole
            state = "QUEUED" if args[1] == "backlog" else STATES[key]
            if args == ["queue", "list"] and key == "wait":
                state = "IMPLEMENTING"                          # `continues at`
            assert state in row, (args, row)
            assert f"{state[:-1]}…" not in row
        for line in (l for l in lines if any(cid in l for cid in IDS.values())):
            cells = [c for c in line.split("  ") if c.strip()]
            assert len(cells) >= 3, (args, line)                # id, state, a title cell


def test_drop_order_follows_ranks(snap):
    """At every width the dropped columns all outrank the kept ones; at 160 everything shows;
    blocked-by and park reason fold into the title when dropped."""
    cfg, _ = snap
    tables = _tables(cfg)
    rank = {"blocked by": 1, "waiting on": 1, "park": 2, "pri": 3, "cost": 4, "project": 5,
            "loops": 6, "size": 7, "tone": 7, "profile": 7, "age": 8, "updated": 8}
    full = {name: set().union(*_headers(_render(t(160), 160))) for name, t in tables.items()}
    assert {"park", "pri", "cost", "project", "loops", "size", "tone", "profile",
            "age"} <= full["queue_list"]
    assert {"blocked by", "park", "pri", "cost", "project", "loops", "updated"} <= \
        full["campaign_list"]
    assert {"waiting on", "pri", "project", "size", "age"} <= full["queue_backlog"]
    for name, table in tables.items():
        wide = _headers(_render(table(160), 160))
        for width in range(60, 161, 5):
            for full_t, kept in zip(wide, _headers(_render(table(width), width))):   # per table
                for d in {h for h in full_t - kept if h in rank}:
                    assert all(rank[k] <= rank[d] for k in kept if k in rank), \
                        (name, width, d, kept)
    narrow = _render(tables["campaign_list"](60), 60)
    assert "[budget_cap]" in narrow and "⧗ SPEC-0337" in narrow
    assert "[budget_cap]" in _render(tables["queue_list"](60), 60)


def test_park_reason_is_its_own_column(snap, monkeypatch):
    cfg, _ = snap
    _cols(monkeypatch, 160)
    r = run_cli(cfg, monkeypatch, ["queue", "list"])
    row = next(line for line in r.output.splitlines() if IDS["park"] in line)
    assert "PARKED:" not in r.output and "budget_cap" in row and "park" in r.output


# ---- fit_columns --------------------------------------------------------------------------------

COLS = [LV.Column("id", "id", fixed=True), LV.Column("state", "state", fixed=True),
        LV.Column("title", "title", flex=True),
        LV.Column("blocked_by", "blocked", rank=1, fold="⧗ {} "),
        LV.Column("park", "park", rank=2, fold="[{}] "), LV.Column("pri", "pri", rank=3),
        LV.Column("age", "age", rank=8)]
ROWS = [{"id": "SPEC-0338-20261031-23a7", "state": "PARKED", "title": "A rather long title",
         "blocked_by": "SPEC-0337", "park": "budget_cap", "pri": "1", "age": "4m"}]


def test_fit_columns_drops_highest_rank_first():
    keys = lambda w: [c.key for c in LV.fit_columns(COLS, ROWS, w)]      # noqa: E731
    assert keys(200) == ["id", "state", "title", "blocked_by", "park", "pri", "age"]
    widths = {c.key: LV.natural_width(c, ROWS) for c in COLS}
    full = sum(widths.values()) - widths["title"] + 12 + 3 * 6
    assert keys(full) == [c.key for c in COLS]
    assert keys(full - 1) == ["id", "state", "title", "blocked_by", "park", "pri"]   # age (8)
    assert "pri" not in keys(full - 1 - widths["age"] - 3)
    assert keys(50) == ["id", "state", "title"]


def test_fit_columns_fold_fixed_and_narrow():
    out = _render(LV.render(None, COLS, ROWS, 60), 60)            # park (2) folds, blocked stays
    assert "[budget_cap]" in out and "blocked" in out and "SPEC-0338-20261031-23a7" in out
    out = _render(LV.render(None, COLS, ROWS, 48), 48)            # both fold into the title
    assert "⧗ SPEC-0337" in out and "blocked" not in out          # declared order: blocked first
    assert max(map(len, out.splitlines())) <= 48
    tiny = _render(LV.render(None, COLS, ROWS, 36), 36)           # id + state fit, title does not
    first = tiny.splitlines()[2]
    assert "SPEC-0338-20261031-23a7" in first and "PARKED" in first     # id/state whole
    assert "title" not in tiny.splitlines()[0]                         # title under the id
    assert "A rather" in tiny.replace("\n", "")


def test_waiting_on_ids():
    cases = [(["waiting on SPEC-0337 (accepted)"], ["SPEC-0337"]),
             (["PROJ-0012 not found"], ["PROJ-0012"]),
             (["waiting on SPEC-0001 (superseded; waiting on SPEC-0002)", "SPEC-0099 not found",
               "waiting on SPEC-0001 (accepted)"], ["SPEC-0001", "SPEC-0099"]),
             (["spec status is draft"], []), (["branch fix/x missing"], []),
             (["idea IDEA-1 not found"], []), (["spec file missing"], []), ([], [])]
    for blocked, want in cases:
        assert Q.waiting_on_ids(blocked) == want, blocked


# ---- JSON parity --------------------------------------------------------------------------------

PRE_QUEUE_V1_KEYS = {"campaign", "spec_id", "project", "kind", "idea_id", "label", "merge_branch",
                     "profile", "state", "size", "tone", "profile_source", "loops", "runs",
                     "cost_usd", "cap_usd", "cost", "age_s", "updated_age_s", "park",
                     "park_requested", "worktree", "branch", "priority", "waiting",
                     "review_score", "run", "spec_round"}
FIELDS = ("title", "priority", "depends_on", "blocked_by", "waiting_on", "park_reason",
          "cost_usd", "cap_usd")


def test_json_parity(snap, monkeypatch):
    cfg, _ = snap
    camp = {c["id"]: c for c in json.loads(run_cli(cfg, monkeypatch, ["campaign", "list",
                                                                       "--json"]).output)["campaigns"]}
    queue = {i["campaign"]: i for i in json.loads(run_cli(cfg, monkeypatch, ["queue", "list",
                                                                              "--json"]).output)["items"]}
    back = {b["campaign"]: b for b in json.loads(run_cli(cfg, monkeypatch, ["queue", "backlog",
                                                                             "--json"]).output)["items"]}
    q = IDS["queued"]
    assert {k: camp[q][k] for k in FIELDS} == {k: queue[q][k] for k in FIELDS} \
        == {k: back[q][k] for k in FIELDS}
    assert camp[q]["waiting_on"] == ["SPEC-0337", "SPEC-0399"]
    assert camp[q]["depends_on"] == ["SPEC-0337", "SPEC-0399"]
    assert camp[q]["title"] == "Lost-job retry across workers and escalation"
    for cid in IDS.values():
        assert {k: camp[cid][k] for k in FIELDS} == {k: queue[cid][k] for k in FIELDS}
        assert PRE_QUEUE_V1_KEYS <= set(queue[cid])
    assert queue[IDS["park"]]["park_reason"] == "budget_cap"
    assert queue[IDS["park"]]["park"]["reason"] == "budget_cap"          # pre-existing kept
    assert queue[IDS["wait"]]["blocked_by"] == ["waiting for a slot"]
    assert queue[IDS["wait"]]["waiting_on"] == []
    assert queue[IDS["gate"]]["remote_gate"] is None and "remote_gate" not in queue[IDS["impl"]]
    assert queue[q]["backlog"]["blocked_by"] == queue[q]["blocked_by"]


# ---- nw queue graph ----------------------------------------------------------------------------

@pytest.fixture
def dag(tmp_path):
    """A ← B ← C, A ← D, C → D (shared), E ⇄ F (cycle), B → X (accepted, no campaign),
    C → G (a campaign in another project)."""
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    deps = {20: (), 21: ("SPEC-0020", "SPEC-0026"), 22: ("SPEC-0021", "SPEC-0023", "SPEC-0027"),
            23: ("SPEC-0020",), 24: ("SPEC-0025",), 25: ("SPEC-0024",)}
    names = dict(zip(range(20, 28), "ABCDEFXG"))
    for n, d in deps.items():
        sid, rel = _spec(repo, n, title=f"node {names[n]}", deps=d)
        _campaign(cfg, repo, f"{sid}-20261031-0000", "QUEUED" if n != 20 else "IMPLEMENTING",
                  spec_id=sid, rel=rel, created_min=100 - n)
    _spec(repo, 26, title="node X")
    sid, rel = _spec(repo, 27, title="node G")
    _campaign(cfg, repo, f"{sid}-20261031-0000", "IMPLEMENTING", spec_id=sid, rel=rel,
              project="other", created_min=10)
    return cfg, repo


def test_graph_tree_reference_cycles_and_stubs(dag, monkeypatch):
    cfg, _ = dag
    _cols(monkeypatch, 120)
    r = run_cli(cfg, monkeypatch, ["queue", "graph"])
    assert r.exit_code == 0, r.output
    out = r.output
    lines = out.splitlines()
    pos = lambda s: next(i for i, l in enumerate(lines) if s in l)        # noqa: E731
    # dependents under their dependencies
    assert pos("SPEC-0020  IMPLEMENTING") < pos("SPEC-0021  QUEUED") < pos("SPEC-0022  QUEUED")
    assert "↑ SPEC-0022 (above)" in out                     # shared C: once in full, once as ref
    assert sum(1 for l in lines if "SPEC-0022  QUEUED" in l) == 1
    assert "SPEC-0026  accepted, no campaign" in out        # stub for the unqueued dependency
    assert "↑ SPEC-0021 (above)" in out                     # B under its second root X
    cyc = pos("Cycles")
    assert pos("SPEC-0024  QUEUED") > cyc and pos("SPEC-0025  QUEUED") > cyc
    assert "SPEC-0024 ⇄ SPEC-0025" in out
    assert "⧗ waiting on SPEC-0020, SPEC-0026" in out      # B waits on A and the stub X


def test_graph_json_and_project_external(dag, monkeypatch):
    cfg, _ = dag
    g = json.loads(run_cli(cfg, monkeypatch, ["queue", "graph", "--json"]).output)
    assert g["schema"] == "nw.queue.graph.v1"
    nodes = {n["id"]: n for n in g["nodes"]}
    assert nodes["SPEC-0026"]["stub"] is True and nodes["SPEC-0026"]["campaign"] is None
    assert nodes["SPEC-0026"]["spec_status"] == "accepted, no campaign"
    assert nodes["SPEC-0021"]["depends_on"] == ["SPEC-0020", "SPEC-0026"]
    edges = {(e["from"], e["to"]) for e in g["edges"]}
    assert {("SPEC-0020", "SPEC-0021"), ("SPEC-0021", "SPEC-0022"), ("SPEC-0023", "SPEC-0022"),
            ("SPEC-0020", "SPEC-0023"), ("SPEC-0026", "SPEC-0021"),
            ("SPEC-0027", "SPEC-0022")} <= edges
    assert g["cycles"] == [["SPEC-0024", "SPEC-0025"]]
    assert set(g["roots"]) == {"SPEC-0020", "SPEC-0026", "SPEC-0027"}
    for key in ("id", "campaign", "kind", "state", "spec_status", "title", "project", "priority",
                "cost_usd", "cap_usd", "park_reason", "blocked_by", "waiting_on", "depends_on",
                "stub", "external"):
        assert key in nodes["SPEC-0021"]
    g = json.loads(run_cli(cfg, monkeypatch, ["queue", "graph", "--json", "--project",
                                              "Meta-Tools"]).output)
    nodes = {n["id"]: n for n in g["nodes"]}
    assert nodes["SPEC-0027"]["external"] is True and nodes["SPEC-0027"]["stub"] is True
    assert nodes["SPEC-0027"]["project"] == "other"
    assert nodes["SPEC-0026"]["external"] is False
    assert ("SPEC-0027", "SPEC-0022") in {(e["from"], e["to"]) for e in g["edges"]}


def test_graph_terminates_on_self_loop_and_all(dag, monkeypatch):
    cfg, repo = dag
    _spec(repo, 28, title="self", deps=("SPEC-0028",))
    _campaign(cfg, repo, "SPEC-0028-20261031-0000", "QUEUED", spec_id="SPEC-0028",
              rel="docs/specs/0028-s28.md")
    _campaign(cfg, repo, "SPEC-0026-20261030-0000", "MERGED", spec_id="SPEC-0026",
              rel="docs/specs/0026-s26.md", created_min=500)
    _campaign(cfg, repo, "SPEC-0026-20261030-0001", "ABANDONED", spec_id="SPEC-0026",
              rel="docs/specs/0026-s26.md", created_min=400)
    g = Q.graph_view(cfg, include_terminal=True)
    assert ["SPEC-0028"] in g["cycles"]
    node = next(n for n in g["nodes"] if n["id"] == "SPEC-0026")
    assert node["campaign"] == "SPEC-0026-20261030-0000" and node["state"] == "MERGED"
    assert run_cli(cfg, monkeypatch, ["queue", "graph", "--all"]).exit_code == 0


# ---- nw queue unqueued -------------------------------------------------------------------------

def _idea(cfg, text, state, spec=None):
    _, _, iid = OW.add_idea(cfg, text, state=state)
    if spec:
        ORG.invalidate_tasks_cache()
        OW.set_property(cfg, Q._idea_task(cfg, iid), "SPEC", spec)
    ORG.invalidate_tasks_cache()
    return iid


@pytest.fixture
def unq(tmp_path):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    _campaign(cfg, repo, "SPEC-0001-20261031-0000", "QUEUED")            # queued: omitted
    _spec(repo, 10, title="forgotten")                                    # ready
    _spec(repo, 11, title="a draft", status="draft")                      # not-accepted
    _spec(repo, 12, title="waits", deps=("SPEC-0011",))                   # deps-unmet
    sid, rel = _spec(repo, 13, title="abandoned once")                    # previous-campaign
    _campaign(cfg, repo, "SPEC-0013-20261030-ab12", "ABANDONED", spec_id=sid, rel=rel)
    sid, rel = _spec(repo, 14, title="delivered")                         # MERGED: omitted
    _campaign(cfg, repo, "SPEC-0014-20261030-0000", "MERGED", spec_id=sid, rel=rel)
    _spec(repo, 15, title="an epic", kind="epic")                         # omitted
    _spec(repo, 16, title="a policy", kind="policy")                      # omitted
    _spec(repo, 17, title="already done", status="done")
    out = Path(cfg["outbox_dir"]) / "jerome"
    out.mkdir(parents=True)
    (out / "PROJ-0003-thing.md").write_text(
        "---\nid: PROJ-0003\ntitle: outbound thing\nstatus: accepted\nkind: feature\n---\n")
    ideas = {"incubate": _idea(cfg, "Incubating thought", "INCUBATE"),
             "collapsed": _idea(cfg, "Forgotten idea", "SPECCED", "SPEC-0010"),
             "done": _idea(cfg, "Shipped idea", "SPECCED", "SPEC-0017"),
             "missing": _idea(cfg, "Lost spec idea", "SPECCED", "SPEC-0099")}
    return cfg, ideas


def test_unqueued_rows_reasons_and_order(unq, monkeypatch):
    cfg, ideas = unq
    r = run_cli(cfg, monkeypatch, ["queue", "unqueued", "--json"])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["schema"] == "nw.queue.unqueued.v1"
    rows = {i["id"]: i for i in d["items"]}
    codes = {k: [x["code"] for x in v["reasons"]] for k, v in rows.items()}
    order = [i["id"] for i in d["items"]]
    assert order[:2] == ["PROJ-0003", "SPEC-0010"]                     # ready first, by id
    assert codes["SPEC-0010"] == ["ready"] and rows["SPEC-0010"]["idea_id"] == ideas["collapsed"]
    assert rows["SPEC-0010"]["next"] == "nw queue add SPEC-0010"
    assert rows["PROJ-0003"]["kind"] == "outbound" and rows["PROJ-0003"]["project"] == "jerome"
    assert codes["SPEC-0012"] == ["deps-unmet"]
    assert rows["SPEC-0012"]["reasons"][0]["text"] == "waiting on SPEC-0011 (draft)"
    assert codes["SPEC-0013"] == ["previous-campaign"]
    assert rows["SPEC-0013"]["previous_campaign"] == "SPEC-0013-20261030-ab12"
    assert "ABANDONED" in rows["SPEC-0013"]["reasons"][0]["text"]
    assert codes["SPEC-0011"] == ["not-accepted"]
    assert codes[ideas["missing"]] == ["spec-missing"]
    assert codes[ideas["incubate"]] == ["no-spec"]
    assert rows[ideas["incubate"]]["next"] == f"nw queue add {ideas['incubate']}"
    assert order.index("SPEC-0012") < order.index("SPEC-0013") < order.index("SPEC-0011") \
        < order.index(ideas["missing"]) < order.index(ideas["incubate"])
    for gone in ("SPEC-0001", "SPEC-0014", "SPEC-0015", "SPEC-0016", "SPEC-0017",
                 ideas["collapsed"], ideas["done"]):
        assert gone not in rows, gone
    assert all("ready" not in codes[i["id"]] for i in d["items"] if i["kind"] == "idea")
    only = json.loads(run_cli(cfg, monkeypatch, ["queue", "unqueued", "--json", "--status",
                                                 "draft"]).output)["items"]
    assert [i["id"] for i in only if i["kind"] != "idea"] == ["SPEC-0011"]
    _cols(monkeypatch, 100)
    text = run_cli(cfg, monkeypatch, ["queue", "unqueued"]).output
    assert text.index("SPEC-0010") < text.index("SPEC-0012")
    assert max(map(len, text.splitlines())) <= 100


# ---- remote gate line -------------------------------------------------------------------------

T0 = dt.datetime(2026, 11, 1, 10, 0, 0, tzinfo=dt.timezone.utc)


def _raw(cfg, cid, events):
    """Ledger lines written raw: `gate_dispatched` is not in EVENT_TYPES (SPEC-0338 emits it)."""
    path = S.campaign_dir(cfg, cid) / "ledger.jsonl"
    with path.open("a") as f:
        for seq, (etype, payload) in enumerate(events, 100):
            f.write(json.dumps({"schema": "nw.ledger.event.v1", "ts": T0.isoformat(), "seq": seq,
                                "campaign": cid, "type": etype, "by": "gates", **payload}) + "\n")


def _gate(seq, *, worker=None, ok=True, started=T0, dur=242.0):
    p = {"gate_seq": seq, "ok": ok, "started_at": started.isoformat(), "duration_s": dur,
         "results": []}
    if worker:
        p["worker"] = worker
    return "gate", p


@pytest.fixture
def gated(tmp_path):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    ids = {}
    for key in ("running", "finished", "fell", "local"):
        ids[key] = _campaign(cfg, repo, f"SPEC-0001-20261101-{key[:4]}", "GATING")
    c = ids["running"]
    _raw(cfg, c, [_gate(6, worker="build-01"),
                  ("gate_dispatched", {"gate_seq": 7, "worker": "build-01",
                                       "job_id": f"{c}-gate-007-a0",
                                       "started_at": T0.isoformat()})])
    _raw(cfg, ids["finished"], [_gate(6, worker="build-01")])
    _raw(cfg, ids["fell"], [("worker_failed", {"worker": "build-01", "stage": "bundle",
                                               "error": "boom", "job_id": "j-a0"}),
                            _gate(5)])
    _raw(cfg, ids["local"], [_gate(3)])
    return cfg, ids


def test_remote_gate_line_and_json(gated, monkeypatch):
    cfg, ids = gated
    st = lambda k: S.read_state(cfg, ids[k])                              # noqa: E731
    now = T0 + dt.timedelta(seconds=192)
    rg = Q.remote_gate_view(cfg, st("running"), now=now)
    assert rg["status"] == "running" and rg["elapsed_s"] == 192 and rg["job_id_derived"] is False
    assert Q.remote_gate_line(rg, now=now) == (
        f"remote gate #7 on worker build-01  job {ids['running']}-gate-007-a0  running 3m12s")
    now = T0 + dt.timedelta(seconds=242 + 18 * 60)
    rg = Q.remote_gate_view(cfg, st("finished"), now=now)
    assert rg["status"] == "ok" and rg["job_id"] == f"{ids['finished']}-gate-006-a0"
    assert rg["job_id_derived"] is True and rg["duration_s"] == 242.0
    assert Q.remote_gate_line(rg, now=now) == (
        f"remote gate #6 on worker build-01  job {ids['finished']}-gate-006-a0  ok in 4m02s, "
        f"18m ago")
    rg = Q.remote_gate_view(cfg, st("fell"), now=now)
    assert rg["status"] == "fell_back" and rg["job_id"] == "j-a0" and rg["gate_seq"] == 5
    assert Q.remote_gate_line(rg) == "remote gate #5 worker build-01 failed (stage bundle) → ran locally"
    assert Q.remote_gate_view(cfg, st("local"), now=now) is None
    for key in ("running", "finished", "fell"):
        out = run_cli(cfg, monkeypatch, ["campaign", "show", ids[key]]).output
        assert "remote gate #" in out, out
        j = json.loads(run_cli(cfg, monkeypatch, ["campaign", "show", ids[key], "--json"]).output)
        assert set(j["remote_gate"]) >= {"gate_seq", "worker", "job_id", "job_id_derived",
                                         "status", "started_at", "elapsed_s", "duration_s"}
    out = run_cli(cfg, monkeypatch, ["campaign", "show", ids["local"]]).output
    assert "remote gate" not in out
    assert json.loads(run_cli(cfg, monkeypatch, ["campaign", "show", ids["local"],
                                                 "--json"]).output)["remote_gate"] is None
    items = {i["campaign"]: i for i in Q.queue_payload(cfg)["items"]}
    assert items[ids["running"]]["remote_gate"]["status"] == "running"
    assert items[ids["local"]]["remote_gate"] is None
