"""SPEC-0185: `wt queue watch` dashboard — `queue.dashboard_payload` sections, activity and
totals, and the rendered frame (`--once`, `--json`, `--project`, widths)."""
import datetime as dt
import json

from rich.console import Console

from test_agentic_e2e import (SPEC_ACCEPTED, _git, _write, impl_done, make_cfg, make_repo, write_review,
                              run_cli, script)
from wt.agentic import cli_queue as CQ
from wt.agentic import queue as Q
from wt.agentic import store as S
from wt.agentic import schemas as SC

def _ev(cfg, cid, etype, payload=None, **kw):
    """append_event with the schema's required keys defaulted (SPEC-0192 fixtures)."""
    return S.append_event(cfg, cid, etype, SC.with_defaults(etype, payload), **kw)

NOW = dt.datetime.now().astimezone()


def ev_at(cfg, cid, etype, payload=None, *, ago_s=0, **kw):
    """`_ev`, then rewrite the ledger line's `ts` to `ago_s` seconds before now (SPEC-0222:
    park ownership and age come from ledger timestamps)."""
    rec = _ev(cfg, cid, etype, payload, **kw)
    path = S.campaign_dir(cfg, cid) / "ledger.jsonl"
    lines = path.read_text().splitlines()
    last = json.loads(lines[-1])
    last["ts"] = (dt.datetime.now().astimezone() - dt.timedelta(seconds=ago_s)).isoformat(
        timespec="seconds")
    lines[-1] = json.dumps(last)
    path.write_text("\n".join(lines) + "\n")
    return last


def _iso(minutes_ago):
    return (NOW - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def _campaign(cfg, repo, cid, state, *, spec_id="SPEC-0001", rel="docs/specs/0001-trivial.md",
              created_min=30, updated_min=1, project="Meta-Tools", **extra):
    S.create_campaign(cfg, {
        "id": cid, "kind": "implement", "spec_id": spec_id, "project": project,
        "profile": "balanced", "state": state, "created_at": _iso(created_min),
        "repo": {"root": str(repo), "base": "main", "branch": f"wt/{cid}",
                 "worktree": f"/tmp/wt/{cid}"},
        "spec": {"rel_path": rel, "internal": True},
        "counters": {"loops": 1, "runs": 1}, "cost": {"total_usd": 1.25, "cap_usd": 10.0},
        **extra})
    # write_state stamps updated_at = now; rewrite it raw to control the age
    p = S.campaign_dir(cfg, cid) / "state.json"
    st = json.loads(p.read_text())
    st["updated_at"] = _iso(updated_min)
    p.write_text(json.dumps(st))
    return cid


def _fixture(tmp_path):
    repo = make_repo(tmp_path)
    write_review(make_cfg(tmp_path, repo, None), _write(repo, "docs/specs/0002-second.md",
                        SPEC_ACCEPTED.format(id="SPEC-0002", extra="depends_on: [SPEC-0009]\n")
                        .replace("Trivial feature", "Blocked second")), "SPEC-0002")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    impl = _campaign(cfg, repo, "SPEC-0001-impl", "IMPLEMENTING",
                     last_gate={"gate_seq": 1, "ok": False, "results": [
                         {"name": "test", "ok": False, "required": True, "duration_s": 3.2}]},
                     last_verdict={"persona": "implementer", "run": "001-implementer",
                                   "verdict": {"status": "done", "summary": "first line\nmore"}})
    _ev(cfg, impl, "run.start", {"persona": "implementer", "run_id": "002-implementer",
                                            "model": "sonnet", "effort": "high",
                                            "timeout_s": 1800, "max_budget_usd": 4.0})
    gating = _campaign(cfg, repo, "SPEC-0001-gate", "GATING")
    parked = _campaign(cfg, repo, "SPEC-0001-park", "PARKED",
                       park={"reason": "rate_limited", "detail": "429 from the API",
                             "resume_to": "IMPLEMENTING"})
    _ev(cfg, parked, "park", {"reason": "rate_limited"})
    ready = _campaign(cfg, repo, "SPEC-0001-ready", "QUEUED")
    blocked = _campaign(cfg, repo, "SPEC-0002-blk", "QUEUED", spec_id="SPEC-0002",
                        rel="docs/specs/0002-second.md")
    merged = _campaign(cfg, repo, "SPEC-0001-merged", "MERGED", created_min=50, updated_min=5,
                       close={"mode": "merge", "merged_sha": "abcdef1234567", "pr_url": None})
    _campaign(cfg, repo, "SPEC-0001-old", "MERGED", created_min=3000, updated_min=2900)
    abandoned = _campaign(cfg, repo, "SPEC-0001-gone", "ABANDONED", updated_min=10)
    return repo, cfg, dict(impl=impl, gating=gating, parked=parked, ready=ready,
                           blocked=blocked, merged=merged, abandoned=abandoned)


def test_payload_sections_activity_and_totals(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    p = Q.dashboard_payload(cfg)
    assert p["schema"] == "wt.queue.dashboard.v1"
    active = {it["campaign"]: it for it in p["active"]}
    assert set(active) == {ids["impl"], ids["gating"]}
    a = active[ids["impl"]]["activity"]
    assert a["kind"] == "run" and a["persona"] == "implementer" and a["model"] == "sonnet"
    assert a["run_id"] == "002-implementer" and a["elapsed_s"] is not None and a["timeout_s"] == 1800
    assert active[ids["gating"]]["activity"] == {"kind": "gates"}
    assert active[ids["impl"]]["phase_index"] == Q.phases_for({"kind": "implement"}).index("IMPLEMENTING")
    assert active[ids["impl"]]["spec_title"] == "Trivial feature"
    assert active[ids["impl"]]["last_gate"]["results"][0] == {
        "name": "test", "ok": False, "required": True, "duration_s": 3.2}
    assert active[ids["impl"]]["last_verdict"]["summary"] == "first line"
    assert [e["type"] for e in active[ids["impl"]]["recent_events"]][-1] == "run.start"

    assert [it["campaign"] for it in p["attention"]] == [ids["parked"]]
    assert p["attention"][0]["park"]["reason"] == "rate_limited"
    assert [it["campaign"] for it in p["conductor_handling"]] == [ids["parked"]]
    assert p["needs_you"] == []
    assert p["attention"][0]["park_age_s"] is not None

    back = {b["campaign"]: b for b in p["backlog"]}
    assert back[ids["ready"]]["ready"] is True
    assert back[ids["blocked"]]["ready"] is False and back[ids["blocked"]]["blocked_by"]
    assert back[ids["blocked"]]["spec_title"] == "Blocked second"

    # newest first, the 2-day-old merge is outside the 24h window
    assert [d["campaign"] for d in p["delivered"]] == [ids["merged"], ids["abandoned"]]
    m = p["delivered"][0]
    assert m["merged_sha"] == "abcdef1234567" and m["state"] == "MERGED"
    assert 40 * 60 <= m["duration_s"] <= 50 * 60

    t = p["totals"]
    assert (t["active"], t["attention"], t["backlog"], t["ready"], t["blocked"],
            t["delivered"]) == (2, 1, 2, 1, 1, 2)
    assert Q.dashboard_payload(cfg, recent=1)["delivered"][0]["campaign"] == ids["merged"]
    assert Q.dashboard_payload(cfg, project="other")["active"] == []


def test_run_end_closes_activity(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    _ev(cfg, ids["impl"], "run.end", {"persona": "implementer",
                                                 "run_id": "002-implementer"})
    act = {it["campaign"]: it for it in Q.dashboard_payload(cfg)["active"]}
    assert act[ids["impl"]]["activity"] == {"kind": "idle"}


def _render(payload, width):
    c = Console(width=width, record=True, force_terminal=False)
    c.print(CQ._dashboard(payload, width))
    return c.export_text()


def test_frame_renders_every_section_at_both_widths(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    p = Q.dashboard_payload(cfg)
    for width in (80, 160):
        out = _render(p, width)
        for header in ("wt queue", "Active (2)", "Conductor handling (1)", "Backlog (2)",
                       "Recently delivered (2"):
            assert header in out, (width, header, out)
        for cid in (ids["impl"], ids["gating"], ids["parked"], ids["merged"]):
            assert cid in out, (width, cid)
        assert "implementer" in out and "rate_limited" in out and "merged" in out
        assert "waiting on" in out
        assert max(len(line) for line in out.splitlines()) <= width


def test_empty_frame(tmp_path):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    out = _render(Q.dashboard_payload(cfg), 80)
    assert "No active campaigns" in out and "empty" in out and "nothing in this window" in out
    assert "Needs you" not in out and "Conductor handling" not in out and "no loop running" in out


def test_cli_once_json_and_project(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    r = run_cli(cfg, monkeypatch, ["queue", "watch", "--once"])
    assert r.exit_code == 0, r.output
    assert "Active (2)" in r.output and ids["merged"] in r.output
    r = run_cli(cfg, monkeypatch, ["queue", "watch", "--json", "--recent", "1"])
    d = json.loads(r.output)
    assert d["schema"] == "wt.queue.dashboard.v1" and len(d["delivered"]) == 1
    r = run_cli(cfg, monkeypatch, ["queue", "watch", "--json", "--project", "nobody"])
    assert json.loads(r.output)["active"] == []


def test_multiple_projects_in_flight_are_labelled(tmp_path, monkeypatch):
    """Several projects in flight at once: every row and panel names its project, the header
    breaks counts down per project, and --project narrows the view."""
    repo, cfg, ids = _fixture(tmp_path)
    other = _campaign(cfg, repo, "COMF-0003-impl", "IMPLEMENTING", spec_id="COMF-0003",
                      project="comfort-assistant")
    p = Q.dashboard_payload(cfg)
    by = p["totals"]["by_project"]
    assert by["comfort-assistant"] == {"active": 1, "needs_you": 0, "conductor": 0,
                                       "waiting": 0, "backlog": 0, "delivered": 0}
    assert by["Meta-Tools"]["active"] == 2 and by["Meta-Tools"]["backlog"] == 2
    assert {it["campaign"]: it["project"] for it in p["active"]}[other] == "comfort-assistant"
    assert all(b["project"] == "Meta-Tools" for b in p["backlog"])
    out = _render(p, 120)
    assert "comfort-assistant ▸ 1 active" in out and "Meta-Tools ▸ 2 active" in out
    panel_title = next(line for line in out.splitlines() if other in line)
    assert "comfort-assistant" in panel_title
    only = Q.dashboard_payload(cfg, project="comfort-assistant")
    assert [it["campaign"] for it in only["active"]] == [other] and only["backlog"] == []
    for cmd in (["queue", "list"], ["queue", "backlog"]):
        r = run_cli(cfg, monkeypatch, cmd)
        assert r.exit_code == 0 and "project" in r.output and "Meta-Tools" in r.output, r.output
    r = run_cli(cfg, monkeypatch, ["queue", "list"])
    assert "comfort-assistant" in r.output
    r = run_cli(cfg, monkeypatch, ["queue", "backlog", "--json"])
    assert {b["project"] for b in json.loads(r.output)["items"]} == {"Meta-Tools"}


def test_dashboard_shows_size_tone_and_profile(tmp_path):                  # SPEC-0219
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    sizing = {"size": "m", "tone": "high-end", "source": "flag", "reason": "--profile balanced"}
    _campaign(cfg, repo, "SPEC-0001-act", "IMPLEMENTING", sizing=sizing)
    _campaign(cfg, repo, "SPEC-0001-old", "IMPLEMENTING")
    _campaign(cfg, repo, "SPEC-0001-q", "QUEUED", sizing={**sizing, "source": "review"})
    _campaign(cfg, repo, "SPEC-0001-w", "WAITING", sizing=sizing,
              waiting={"resume_to": "GATING", "since": _iso(5)})
    p = Q.dashboard_payload(cfg)
    act = {it["campaign"]: it for it in p["active"]}
    assert (act["SPEC-0001-act"]["size"], act["SPEC-0001-act"]["tone"],
            act["SPEC-0001-act"]["profile"], act["SPEC-0001-act"]["profile_source"]) == (
        "m", "high-end", "balanced", "flag")
    assert act["SPEC-0001-old"]["size"] is None
    assert p["backlog"][0]["size"] == "m" and p["waiting"][0]["tone"] == "high-end"
    out = _render(p, 140)
    assert "m/high-end → balanced (flag)" in out
    old_panel = out[out.index("SPEC-0001-old"):]
    assert "- → balanced" in old_panel
    backlog = out[out.index("Backlog ("):]
    assert "size" in backlog and "m/high-end" in backlog
    assert "m/high-end" in out[out.index("Waiting for a slot"):out.index("Backlog (")]


def _park_fixture(tmp_path):
    """A main_dirty park, a decided rebase_conflict, an overdue base_moving and a pending park
    request (SPEC-0222)."""
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    ids = {}
    for key, reason, mins in (("dirty", "main_dirty", 20), ("rebase", "rebase_conflict", 15),
                              ("moving", "base_moving", 7)):
        cid = _campaign(cfg, repo, f"SPEC-0001-{key}", "PARKED",
                        park={"reason": reason, "detail": f"{reason} detail",
                              "resume_to": "GATING", "since": _iso(mins)})
        ev_at(cfg, cid, "park", {"reason": reason}, ago_s=mins * 60)
        ids[key] = cid
    ev_at(cfg, ids["rebase"], "note", {"text": "rebased onto main\nand re-gating",
                                       "author": "conductor"}, by="agent:conductor", ago_s=180)
    ids["req"] = _campaign(cfg, repo, "SPEC-0001-req", "IMPLEMENTING",
                           park_requested={"reason": "human", "detail": "hold this", "at": _iso(2)})
    return repo, cfg, ids


def test_park_split_lists_and_totals(tmp_path):
    repo, cfg, ids = _park_fixture(tmp_path)
    p = Q.dashboard_payload(cfg)
    assert [it["campaign"] for it in p["needs_you"]] == [ids["dirty"]]
    handling = [it["campaign"] for it in p["conductor_handling"]]
    assert handling == [ids["moving"], ids["rebase"], ids["req"]]      # overdue first, then oldest
    assert [it["campaign"] for it in p["attention"]] == [ids["dirty"]] + handling
    t = p["totals"]
    assert (t["needs_you"], t["conductor_handling"], t["conductor_overdue"],
            t["attention"]) == (1, 3, 1, 4)
    moving = p["conductor_handling"][0]["park"]
    assert moving["overdue"] is True and moving["decision"] is None
    req = p["conductor_handling"][2]["park"]
    assert req["decision"]["text"] == "park requested: hold this" and req["owner"] == "conductor"
    assert {c["campaign"] for c in p["active"]} == {ids["req"]}
    assert t["by_project"]["Meta-Tools"]["needs_you"] == 1
    assert t["by_project"]["Meta-Tools"]["conductor"] == 3


def test_park_split_rendering(tmp_path):
    repo, cfg, ids = _park_fixture(tmp_path)
    out = _render(Q.dashboard_payload(cfg), 140)
    assert "Needs you (1)" in out and "Conductor handling (3)" in out
    assert "Needs attention" not in out and "attention" not in out
    assert "needs you 1" in out and "conductor 3 (1 overdue)" in out
    assert "rebased onto main and re-gating" in out and "3m ago" in out
    assert "overdue 7m" in out
    assert "park requested: hold this" in out
    assert out.index("Needs you (1)") < out.index("Conductor handling (3)")


def test_young_undecided_conductor_park_renders_pending(tmp_path):
    repo, cfg, ids = _park_fixture(tmp_path)
    young = _campaign(cfg, repo, "SPEC-0001-young", "PARKED",
                      park={"reason": "transient", "detail": "retrying", "resume_to": "GATING",
                            "since": _iso(2)})
    ev_at(cfg, young, "park", {"reason": "transient"}, ago_s=120)
    p = Q.dashboard_payload(cfg)
    park = next(it for it in p["conductor_handling"] if it["campaign"] == young)["park"]
    assert park["overdue"] is False and park["decision"] is None
    assert p["totals"]["conductor_overdue"] == 1
    cells = {it["campaign"]: CQ._decision_cell(it).plain for it in p["conductor_handling"]}
    assert cells[young].startswith("pending 2m"), cells
    assert "overdue" not in cells[young]
    assert cells[ids["moving"]].startswith("overdue 7m")
    out = _render(p, 140)
    assert "pending 2m" in out and "conductor 4 (1 overdue)" in out


# ---- SPEC-0277: per-reviewer review chips ---------------------------------------------------

import pytest

_REVIEWERS = ("conformance_reviewer", "test_reviewer", "adversarial_reviewer")


def _verdict(verdict, score, *severities, loop=1):
    return {"verdict": verdict, "score": score, "run": f"r-{verdict}", "loop": loop,
            "findings": [{"severity": s, "title": s} for s in severities]}


def _review_campaign(tmp_path, state="REVIEWING", *, verdicts=None, loops=1):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]),
                   profile={"reviewers": list(_REVIEWERS)})
    cid = _campaign(cfg, repo, "SPEC-0001-rev", state,
                    last_review={"verdicts": verdicts or {}})
    p = S.campaign_dir(cfg, cid) / "state.json"
    st = json.loads(p.read_text())
    st["counters"]["loops"] = loops
    p.write_text(json.dumps(st))
    _ev(cfg, cid, "transition", {"from": "GATING", "to": "REVIEWING"})
    return cfg, cid


def _start(cfg, cid, persona, run_id=None):
    _ev(cfg, cid, "run.start", {"persona": persona, "run_id": run_id or f"{persona}-1",
                                "model": "sonnet", "effort": "high"})


def _end(cfg, cid, persona, run_id=None):
    _ev(cfg, cid, "run.end", {"persona": persona, "run_id": run_id or f"{persona}-1"})


def _row(cfg, cid):
    return next(it for it in Q.dashboard_payload(cfg)["active"] if it["campaign"] == cid)


def test_review_chips_running_and_not_run(tmp_path):
    cfg, cid = _review_campaign(tmp_path)
    _start(cfg, cid, "conformance_reviewer")
    _start(cfg, cid, "test_reviewer")
    chips = {c["persona"]: c for c in _row(cfg, cid)["reviews"]}
    assert list(chips) == list(_REVIEWERS)
    assert [chips[p]["status"] for p in _REVIEWERS] == ["running", "running", "not_run"]
    assert chips["conformance_reviewer"]["elapsed_s"] is not None
    assert chips["adversarial_reviewer"]["elapsed_s"] is None


def test_review_chips_verdicts_and_counts(tmp_path):
    cfg, cid = _review_campaign(tmp_path, verdicts={
        "conformance_reviewer": _verdict("pass", 92),
        "test_reviewer": _verdict("fix", 40, "must_fix", "major", "major")})
    for p in _REVIEWERS[:2]:
        _start(cfg, cid, p)
        _end(cfg, cid, p)
    chips = {c["persona"]: c for c in _row(cfg, cid)["reviews"]}
    assert chips["conformance_reviewer"]["status"] == "pass"
    assert chips["conformance_reviewer"]["score"] == 92
    assert chips["test_reviewer"]["status"] == "fix"
    assert chips["test_reviewer"]["counts"] == {"must_fix": 1, "major": 2, "minor": 0, "nit": 0}
    assert chips["adversarial_reviewer"]["status"] == "not_run"


def test_review_chips_ignore_previous_loop(tmp_path):
    cfg, cid = _review_campaign(tmp_path, loops=2, verdicts={
        "conformance_reviewer": _verdict("pass", 92, loop=1)})
    chips = _row(cfg, cid)["reviews"]
    assert [c["status"] for c in chips] == ["not_run"] * 3


def test_review_chips_failed_run(tmp_path):
    cfg, cid = _review_campaign(tmp_path)
    _start(cfg, cid, "test_reviewer")
    _end(cfg, cid, "test_reviewer")
    chips = {c["persona"]: c["status"] for c in _row(cfg, cid)["reviews"]}
    assert chips == {"conformance_reviewer": "not_run", "test_reviewer": "failed",
                     "adversarial_reviewer": "not_run"}


def test_review_chips_after_fix_route_empty(tmp_path):
    cfg, cid = _review_campaign(tmp_path, state="IMPLEMENTING", loops=2, verdicts={
        p: _verdict("fix", 30, "major", loop=1) for p in _REVIEWERS})
    for p in _REVIEWERS:
        _start(cfg, cid, p)
        _end(cfg, cid, p)
    assert _row(cfg, cid)["reviews"] == []


@pytest.mark.parametrize("exc", [ValueError("bad profile"), OSError("unreadable")])
def test_review_chips_broken_profile_fallback(tmp_path, monkeypatch, exc):
    cfg, cid = _review_campaign(tmp_path)
    _start(cfg, cid, "adversarial_reviewer")
    _start(cfg, cid, "review_triager")

    real = Q.resolve_profile

    def boom(cfg, name, **k):
        if k.get("project_root"):          # the per-campaign resolution, not the slot count
            raise exc
        return real(cfg, name, **k)
    monkeypatch.setattr(Q, "resolve_profile", boom)
    chips = _row(cfg, cid)["reviews"]
    assert [(c["persona"], c["status"]) for c in chips] == [("adversarial_reviewer", "running")]


def test_reviews_running_total_and_header(tmp_path):
    cfg, cid = _review_campaign(tmp_path, verdicts={
        "conformance_reviewer": _verdict("fix", 40, "must_fix", "major", "major")})
    _start(cfg, cid, "test_reviewer")
    p = Q.dashboard_payload(cfg)
    assert p["totals"]["reviews_running"] == 1
    out = _render(p, 120)
    assert "reviews 1" in out
    assert "✗ 1mf/2mj" in out and "▶" in out and "reviews " in out
    panel = Console(width=120, record=True, force_terminal=False)
    panel.print(CQ._active_panel(_row(cfg, cid)))
    text = panel.export_text()
    assert "✗ 1mf/2mj" in text and "▶" in text and "adversarial ·" in text
    # nothing running: no header counter
    _end(cfg, cid, "test_reviewer")
    p = Q.dashboard_payload(cfg)
    assert p["totals"]["reviews_running"] == 0 and "reviews 0" not in _render(p, 120)


def test_review_chips_window_starts_at_last_reviewing_transition(tmp_path):
    cfg, cid = _review_campaign(tmp_path, loops=2)
    # loop 1's review: two ended runs and one left open
    for p in _REVIEWERS[:2]:
        _start(cfg, cid, p)
        _end(cfg, cid, p)
    _start(cfg, cid, "adversarial_reviewer")
    # loop 2 enters REVIEWING again, no new runs yet
    _ev(cfg, cid, "transition", {"from": "IMPLEMENTING", "to": "REVIEWING"})
    chips = _row(cfg, cid)["reviews"]
    assert [c["status"] for c in chips] == ["not_run"] * 3


def test_review_chips_current_verdicts_show_outside_reviewing(tmp_path):
    cfg, cid = _review_campaign(tmp_path, state="TRIAGING", verdicts={
        "test_reviewer": _verdict("pass", 88)})
    # run events of the finished review must not make chips failed/running outside REVIEWING
    _start(cfg, cid, "adversarial_reviewer")
    chips = {c["persona"]: c for c in _row(cfg, cid)["reviews"]}
    assert list(chips) == list(_REVIEWERS)
    assert chips["test_reviewer"]["status"] == "pass" and chips["test_reviewer"]["score"] == 88
    assert chips["conformance_reviewer"]["status"] == "not_run"
    assert chips["adversarial_reviewer"]["status"] == "not_run"


def test_review_chips_non_conforming_verdict_is_failed(tmp_path):
    cfg, cid = _review_campaign(tmp_path, verdicts={
        "conformance_reviewer": {**_verdict("pass", 90), "verdict": "maybe"}})
    _start(cfg, cid, "conformance_reviewer")
    _end(cfg, cid, "conformance_reviewer")
    chips = {c["persona"]: c["status"] for c in _row(cfg, cid)["reviews"]}
    assert chips["conformance_reviewer"] == "failed"


def test_review_chips_elapsed_and_render(tmp_path):
    cfg, cid = _review_campaign(tmp_path)
    ev_at(cfg, cid, "run.start", {"persona": "test_reviewer", "run_id": "t-1",
                                  "model": "sonnet", "effort": "high"}, ago_s=180)
    chip = next(c for c in _row(cfg, cid)["reviews"] if c["persona"] == "test_reviewer")
    assert 175 <= chip["elapsed_s"] <= 240
    panel = Console(width=120, record=True, force_terminal=False)
    panel.print(CQ._reviews_text([chip]))
    assert "test ▶ 3m" in panel.export_text()


def test_review_chips_fallback_ordering_and_loop_filter(tmp_path, monkeypatch):
    cfg, cid = _review_campaign(tmp_path, loops=2, verdicts={
        "test_reviewer": _verdict("pass", 80, loop=2),
        "conformance_reviewer": _verdict("fix", 30, "major", loop=1)})
    _start(cfg, cid, "adversarial_reviewer")
    real = Q.resolve_profile

    def boom(cfg, name, **k):
        if k.get("project_root"):
            raise ValueError("bad profile")
        return real(cfg, name, **k)
    monkeypatch.setattr(Q, "resolve_profile", boom)
    chips = _row(cfg, cid)["reviews"]
    expected = [p for p in Q.PS.REVIEWER_PERSONAS if p in ("test_reviewer", "adversarial_reviewer")]
    assert [c["persona"] for c in chips] == expected
    assert {c["persona"]: c["status"] for c in chips} == {
        "test_reviewer": "pass", "adversarial_reviewer": "running"}


def test_reviews_running_total_sums_across_campaigns(tmp_path):
    cfg, cid = _review_campaign(tmp_path)
    _start(cfg, cid, "test_reviewer")
    repo = tmp_path / "repo"
    cid2 = _campaign(cfg, repo, "SPEC-0002-rev", "REVIEWING", spec_id="SPEC-0002")
    _ev(cfg, cid2, "transition", {"from": "GATING", "to": "REVIEWING"})
    _start(cfg, cid2, "conformance_reviewer")
    _start(cfg, cid2, "adversarial_reviewer")
    assert Q.dashboard_payload(cfg)["totals"]["reviews_running"] == 3


def test_gate_text_shows_failed_count():
    g = {"gate_seq": 1, "results": [
        {"name": "pytest", "ok": False, "duration_s": 3, "summary": {"failed": 2}},
        {"name": "lint", "ok": True, "duration_s": 1, "summary": {"failed": 0}},
        {"name": "other", "ok": False, "duration_s": 1}]}
    assert CQ._gate_text(g).plain.count("failed)") == 1
    assert "pytest" in CQ._gate_text(g).plain and "(2 failed)" in CQ._gate_text(g).plain


def test_dashboard_payload_carries_gate_summary_to_the_gate_cell(tmp_path):
    """SPEC-0284: the real path (state -> dashboard_payload -> _gate_text) keeps `summary`."""
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    summary = {"format": "pytest", "passed": 5, "failed": 2, "errors": 0, "skipped": 0,
               "failing": ["t.py::a", "t.py::b"]}
    cid = _campaign(cfg, repo, "SPEC-0001-gs", "REVIEWING", last_gate={
        "gate_seq": 1, "ok": False, "results": [
            {"name": "pytest", "ok": False, "required": True, "duration_s": 3, "summary": summary},
            {"name": "lint", "ok": True, "required": True, "duration_s": 1}]})
    item = _row(cfg, cid)
    assert item["last_gate"]["results"][0]["summary"] == summary
    assert "summary" not in item["last_gate"]["results"][1]
    assert "(2 failed)" in CQ._gate_text(item["last_gate"]).plain


def test_gate_text_retry_marker():
    """SPEC-0289: `↻N` for a gate with retries; `(flaky)` only when the gate ended ok."""
    att = [{"attempt": 0, "kind": "full"}, {"attempt": 1, "kind": "targeted"},
           {"attempt": 2, "kind": "full"}]
    ok = {"gate_seq": 1, "results": [{"name": "pytest", "ok": True, "duration_s": 3,
                                      "attempts": att, "flaky": ["a::b"]}]}
    bad = {"gate_seq": 1, "results": [{"name": "pytest", "ok": False, "duration_s": 3,
                                       "attempts": att[:2], "flaky": ["a::b"]}]}
    plain = {"gate_seq": 1, "results": [{"name": "pytest", "ok": True, "duration_s": 3}]}
    assert "↻2 (flaky)" in CQ._gate_text(ok).plain
    assert "↻1" in CQ._gate_text(bad).plain and "flaky" not in CQ._gate_text(bad).plain
    assert "↻" not in CQ._gate_text(plain).plain


def test_watch_shows_split_campaign_kind(tmp_path, monkeypatch):      # SPEC-0290
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    cid = _campaign(cfg, repo, "SPEC-0001-20261030-aaaa", "SPEC_WRITING", kind="split",
                    split={"original": "SPEC-0001", "original_rel": "docs/specs/0001-trivial.md",
                           "review_sha": "x", "parts": []})
    r = run_cli(cfg, monkeypatch, ["queue", "watch", "--once"])
    assert r.exit_code == 0, r.output
    assert cid in r.output and "kind split" in r.output
    (item,) = Q.queue_payload(cfg)["items"]
    assert item["kind"] == "split"
