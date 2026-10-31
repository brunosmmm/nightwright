#!/usr/bin/env python3
"""SPEC-0149/0152/0154: regenerate product-docs SVG captures from a deterministic demo fixture.

Usage (repo root):

    uv run python scripts/docs-capture.py

Does not read the developer's live XDG org data or transcripts.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "docs" / "product" / "assets" / "captures"
DEFAULT_EXAMPLES = ROOT / "docs" / "product" / "assets" / "examples"


def _enable_capture_color() -> None:
    """Docs SVGs must keep hue. Agent/CI shells often export NO_COLOR=1, which makes Textual
    install a Monochrome line filter — every desk screenshot collapses to greys. Rich CLI
    captures are less affected (force_terminal + truecolor), but clear the flag for both."""
    import os

    os.environ.pop("NO_COLOR", None)
    # FORCE_COLOR=0 is also common in piped/agent environments; prefer colorful captures.
    if os.environ.get("FORCE_COLOR") == "0":
        os.environ.pop("FORCE_COLOR", None)

# (title, state, project, priority, kind, epic) — dense enough that the desk capture reads "busy".
DEMO_IDEAS = [
    ("Ship SVG captures for product docs", "INCUBATE", "Acme", "A", "improvement", "Docs"),
    ("Passive hours from transcripts", "IDEA", "Partner", "B", "idea", "Time"),
    ("Outbound export to partner repo", "SPECCED", "Acme", "B", "idea", "Docs"),
    ("Desk filter presets for triage", "IDEA", "Acme", "C", "improvement", "Docs"),
    ("Weekly review digest mailer", "INCUBATE", "Partner", "B", "idea", "Time"),
    ("Clock glyph on CLI idea tables", "SPECCED", "Acme", "A", "improvement", "Time"),
    ("Hub JSON schema for agents", "IDEA", "Acme", "B", "idea", "Docs"),
    ("Archive rotation dry-run flag", "IDEA", "Partner", "C", "chore", ""),
]

TZ = ZoneInfo("America/New_York")

# Wave A/B captures (SPEC-0154) in addition to the ideas-suite set from SPEC-0149/0152.
WAVE_AB = (
    "cli-nw-dashboard.svg",
    "cli-report-week.svg",
    "cli-topics-unmapped.svg",
    "cli-map.svg",
    "cli-review-week.svg",
    "cli-agenda.svg",
    "cli-tasks.svg",
)

# Wave C (SPEC-0158): projects + export schemes.
WAVE_C = (
    "cli-projects.svg",
    "cli-spec-schemes.svg",
)


def _demo_cfg(tmp: Path) -> dict:
    from nightwright.config import DEFAULT_CONFIG

    org = tmp / "org"
    org.mkdir()
    data = tmp / "data"
    data.mkdir()
    specs = tmp / "specs"
    specs.mkdir()
    config = tmp / "config"
    config.mkdir()
    work = tmp / "work"
    (work / "acme").mkdir(parents=True)
    (work / "partner-notes").mkdir(parents=True)
    home = tmp / "home"
    home.mkdir()
    claude = tmp / "claude-projects"
    claude.mkdir()

    cfg = dict(DEFAULT_CONFIG)
    cfg.update({
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "INPROGRESS", "|", "DONE"],
        "org_idea_keywords": [
            "IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "SHIPPED", "DROPPED",
        ],
        "org_question_keywords": ["OPEN", "|", "RESOLVED"],
        "timezone": "America/New_York",
        "_tz": TZ,
        "data_dir": str(data),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "org_ideas_archive_file": str(org / "ideas-archive.org"),
        "idea_rotation_max_lines": 1200,
        "idea_show_theme": "nord",
        "specs_dir": str(specs),
        "config_dir": str(config),
        "project_axis": "bucket",
        "work_root": str(work),
        "transcripts_root": str(claude),
        "cursor_transcripts_root": None,
        "_work": str(work),
        "_home": str(home),
        "_root": str(claude),
        "_cursor_root": None,
        "outbox_targets": {
            # Stable display paths for SVG captures (no /tmp noise in the desk detail pane).
            "Acme": {
                "repo_path": "/home/demo/work/acme",
                "scheme": "nw-native",
            },
            "Partner": {
                "repo_path": "/home/demo/work/partner",
                "scheme": "plain-md",
            },
        },
    })
    (tmp / "partner-repo").mkdir(parents=True, exist_ok=True)
    (work / "acme").mkdir(parents=True, exist_ok=True)
    # Seed facet registry so idea :PROJECT: Acme/Partner don't warn as unknown (SPEC-0018).
    (config / "mappings.yaml").write_text(
        "acme:\n  bucket: Acme\npartner-notes:\n  bucket: Partner\n",
        encoding="utf-8",
    )
    return cfg


def _day(offset: int) -> str:
    return (dt.datetime.now(TZ).date() - dt.timedelta(days=offset)).isoformat()


def _write_history(cfg: dict) -> None:
    from nightwright.history import history_path

    rows = [
        {"day": _day(1), "topics": {"acme": 12600.0, "partner-notes": 4320.0},
         "actor": {"claude": 7000.0, "you": 5500.0}, "wall": 14400.0},
        {"day": _day(2), "topics": {"acme": 7200.0, "docs-capture": 5400.0},
         "actor": {"claude": 6000.0, "you": 4000.0}, "wall": 11520.0},
        {"day": _day(3), "topics": {"partner-notes": 14400.0},
         "actor": {"claude": 8000.0, "you": 6400.0}, "wall": 14400.0},
        {"day": _day(4), "topics": {"acme": 18000.0},
         "actor": {"claude": 10000.0, "you": 8000.0}, "wall": 18000.0},
        # Today via history so bare dashboard is non-empty without relying on live streams.
        {"day": _day(0), "topics": {"acme": 6300.0, "partner-notes": 1800.0},
         "actor": {"claude": 4000.0, "you": 3000.0}, "wall": 7200.0},
    ]
    path = history_path(cfg)
    Path(path).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _write_transcripts(cfg: dict) -> None:
    """Minimal Claude jsonl so `review --week` has live segments."""
    root = Path(cfg["_root"]) / "home-demo-work-acme"
    root.mkdir(parents=True)
    work = Path(cfg["_work"])
    session = "demo-sess-001"
    lines = []
    stamps = [
        (0, 10, 0, str(work / "acme")),
        (0, 10, 25, str(work / "acme")),
        (0, 14, 0, str(work / "partner-notes")),
        (1, 11, 0, str(work / "acme")),
        (1, 11, 30, str(work / "acme")),
        (2, 15, 0, str(work / "partner-notes")),
        (2, 15, 35, str(work / "partner-notes")),
    ]
    for i, (day_off, hour, minute, cwd) in enumerate(stamps):
        local = dt.datetime.combine(
            dt.datetime.now(TZ).date() - dt.timedelta(days=day_off),
            dt.time(hour, minute),
            tzinfo=TZ,
        )
        lines.append(json.dumps({
            "timestamp": local.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "cwd": cwd,
            "gitBranch": "main",
            "sessionId": session,
            "uuid": f"u{i}",
            "type": "user",
            "message": {"role": "user", "content": f"demo prompt {i}"},
        }))
    (root / "sess.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_tasks(cfg: dict) -> None:
    from nightwright import org_write as W

    Path(cfg["org_capture_file"]).write_text(
        "#+TODO: TODO INPROGRESS | DONE\n\n", encoding="utf-8",
    )
    today = dt.datetime.now(TZ).date().isoformat()
    tomorrow = (dt.datetime.now(TZ).date() + dt.timedelta(days=1)).isoformat()
    W.add_task(cfg, "Review partner export checklist", state="TODO",
               scheduled=today, priority="A")
    W.add_task(cfg, "Draft weekly hours note", state="INPROGRESS",
               scheduled=today)
    W.add_task(cfg, "Triage open questions on docs epic", state="TODO",
               scheduled=tomorrow)


def _seed_ideas(cfg: dict) -> str:
    from nightwright import explore as EX
    from nightwright import org_write as W
    from nightwright.org import load_tasks

    for title, _state, project, priority, kind, epic in DEMO_IDEAS:
        W.add_idea(cfg, title, project=project, priority=priority, kind=kind,
                   epic=epic or None)
    tasks = [t for t in load_tasks(cfg) if t.is_idea]
    by_title = {t.heading: t for t in tasks}
    for title, state, *_rest in DEMO_IDEAS:
        task = by_title.get(title) or next(t for t in tasks if title in (t.heading or ""))
        if (task.state or "IDEA") != state:
            W.set_state(cfg, task, state)

    by_id = {}
    show_id = None
    for t in load_tasks(cfg):
        if not t.is_idea:
            continue
        iid = t.properties.get("ID") or t.id
        by_id[iid] = t
        if "Ship SVG captures" in (t.heading or ""):
            show_id = iid
    if not show_id:
        raise RuntimeError("demo seed missing show target")

    # Sibling summaries first, then backdate UPDATED with a fresh resolve (set_summary
    # invalidates the cached Task line offsets).
    sibling_ids = [iid for iid in by_id if iid != show_id]
    for iid in sibling_ids:
        heading = by_id[iid].heading or ""
        EX.set_summary(cfg, iid, f"Demo fixture note for {heading[:48]}.")

    age_stamps = [
        "[2026-09-09 Wed 09:10]",
        "[2026-09-08 Tue 16:40]",
        "[2026-09-08 Tue 11:05]",
        "[2026-09-07 Mon 14:22]",
        "[2026-09-06 Sun 19:50]",
        "[2026-09-05 Sat 10:15]",
        "[2026-09-04 Fri 13:30]",
    ]
    for age_i, iid in enumerate(sibling_ids):
        task = next(
            t for t in load_tasks(cfg)
            if (t.properties.get("ID") or t.id) == iid
        )
        W.set_property(cfg, task, "UPDATED", age_stamps[age_i % len(age_stamps)], touch=False)

    EX.set_summary(
        cfg, show_id,
        "Docs should show real CLI/TUI output and explain how org files hold the "
        "durable idea record — Summary, Questions, and Log included. The desk is the "
        "human triage surface; agents stay on CLI + --json.",
    )
    EX.add_question(cfg, show_id, "Do we embed org excerpts as fenced code or SVG?")
    EX.add_question(cfg, show_id, "Should rotation knobs appear in the guide?")
    EX.add_question(cfg, show_id, "Wide capture (140 cols) enough for project+epic?")
    EX.add_question(cfg, show_id, "Cache-bust the SVG filename on content changes?")
    EX.resolve_question(cfg, show_id, 2)
    EX.resolve_question(cfg, show_id, 4)
    for note in (
        "Sketched capture pipeline: fixture → Rich/Textual SVG → docs assets.",
        "Summary stays the promote gate; Log is the explore trail.",
        "Desk is the human triage surface; agents stay on CLI + --json.",
        "Verified detail pane shows Summary, Questions, and Log together.",
        "Pointed research root at /home/demo/work/acme for outbound routing.",
        "Queued follow-up: cache-bust SVG URLs when captures change.",
    ):
        EX.append_log(cfg, show_id, note)
    # Open clock last so ◕ shows on the enriched row (and UPDATED stays freshest).
    W.clock_in(cfg, show_id)
    return show_id


def _seed(cfg: dict) -> str:
    _write_history(cfg)
    _write_transcripts(cfg)
    _seed_tasks(cfg)
    return _seed_ideas(cfg)


def _make_console(width: int = 100, height: int = 40):
    from rich.console import Console
    from rich.terminal_theme import MONOKAI

    return Console(
        record=True,
        width=width,
        height=height,
        force_terminal=True,
        color_system="truecolor",
    ), MONOKAI


def _patch_consoles(rec) -> None:
    import nightwright.console as C
    import nightwright.report as R

    C.console = rec
    R.console = rec


def _save(rec, path: Path, title: str, theme) -> Path:
    rec.save_svg(str(path), title=title, theme=theme)
    return path


def capture_time_ops(cfg: dict, out: Path) -> list[Path]:
    """Wave A/B: dashboard, report, topics, map, review, agenda, tasks."""
    import nightwright.console as C
    import nightwright.report as R
    from nightwright.rules import set_facets

    written: list[Path] = []

    rec, theme = _make_console(width=100, height=50)
    _patch_consoles(rec)
    rec.rule("[bold cyan]today[/]")
    R.report(cfg)
    rec.rule("[bold cyan]this week[/]")
    R.report(cfg, week="now")
    rec.print("[dim]› nw report --week · nw review --day --fix · nw refresh-meetings[/]")
    written.append(_save(rec, out / "cli-nw-dashboard.svg", "nw", theme))

    rec, theme = _make_console(width=100, height=45)
    _patch_consoles(rec)
    R.report(cfg, week="now")
    written.append(_save(rec, out / "cli-report-week.svg", "nw report --week", theme))

    rec, theme = _make_console(width=100, height=35)
    _patch_consoles(rec)
    R.topics(cfg, unmapped=True)
    written.append(_save(rec, out / "cli-topics-unmapped.svg", "nw topics --unmapped", theme))

    rec, theme = _make_console(width=80, height=12)
    _patch_consoles(rec)
    set_facets(cfg, "acme", {"bucket": "Acme", "area": "docs"})
    shown = "  ".join(
        f"[cyan]{k}[/]=[green]{v}[/]" for k, v in (("bucket", "Acme"), ("area", "docs"))
    )
    C.console.print(f"[green]✓[/] [white]acme[/] → {shown}")
    written.append(_save(rec, out / "cli-map.svg", "nw map acme", theme))

    rec, theme = _make_console(width=100, height=40)
    _patch_consoles(rec)
    R.review(cfg, week="now")
    written.append(_save(rec, out / "cli-review-week.svg", "nw review --week", theme))

    rec, theme = _make_console(width=100, height=35)
    _patch_consoles(rec)
    R.agenda(cfg, day="now")
    written.append(_save(rec, out / "cli-agenda.svg", "nw agenda", theme))

    rec, theme = _make_console(width=100, height=30)
    _patch_consoles(rec)
    R.tasks(cfg)
    written.append(_save(rec, out / "cli-tasks.svg", "nw tasks", theme))

    return written


def capture_wave_c(cfg: dict, out: Path) -> list[Path]:
    """Wave C: projects list + export schemes (SPEC-0158)."""
    import nightwright.console as C
    from nightwright.export_schemes import available as available_schemes
    from nightwright.rules import known_projects, load_mappings

    written: list[Path] = []

    rec, theme = _make_console(width=90, height=20)
    _patch_consoles(rec)
    axis = cfg.get("project_axis", "bucket")
    mappings = load_mappings(cfg)
    counts: dict[str, int] = {}
    for facets in mappings.values():
        val = facets.get(axis)
        if val:
            counts[val] = counts.get(val, 0) + 1
    projects = known_projects(cfg)
    outbox = cfg.get("outbox_targets") or {}
    C.console.print("[bold]projects[/]  [dim](demo fixture)[/]")
    for p in projects:
        flag = "outbound" if p in outbox else "facet"
        C.console.print(
            f"[white]{p}[/]  [dim]({counts.get(p, 0)} topic(s), {flag})[/]"
        )
    written.append(_save(rec, out / "cli-projects.svg", "nw projects", theme))

    rec, theme = _make_console(width=100, height=28)
    _patch_consoles(rec)
    for s in available_schemes():
        origin = getattr(s, "origin", "built-in")
        tag = "  [yellow]\\[user-config][/]" if origin == "user-config" else ""
        C.console.print(f"[bold]{s.name}[/]{tag}  [dim]{s.description}[/]")
        C.console.print(f"  required: {', '.join(s.required_fields) or '(none)'}")
        C.console.print(f"  emit: {', '.join(s.artifacts)}")
    written.append(_save(rec, out / "cli-spec-schemes.svg", "nw spec schemes", theme))

    return written


def capture_cli(cfg: dict, out: Path, show_id: str) -> list[Path]:
    import nightwright.explore as EX
    import nightwright.report as R
    from nightwright.org_write import resolve_selector

    written: list[Path] = []

    rec, theme = _make_console()
    _patch_consoles(rec)
    R.ideas(cfg)
    written.append(_save(rec, out / "cli-ideas.svg", "nw ideas", theme))

    rec, theme = _make_console()
    _patch_consoles(rec)
    R.next_ideas(cfg)
    written.append(_save(rec, out / "cli-next.svg", "nw next", theme))

    rec, theme = _make_console(width=100, height=55)
    _patch_consoles(rec)
    task = resolve_selector(cfg, show_id)
    EX.print_idea_show(cfg, task, plain=False, console=rec)
    written.append(_save(rec, out / "cli-idea-show.svg", "nw idea show", theme))

    rec, theme = _make_console(width=100, height=45)
    _patch_consoles(rec)
    payload = R.hub_payload(cfg)
    rec.print_json(json.dumps(payload))
    written.append(_save(rec, out / "cli-hub.svg", "nw hub --json", theme))

    rec, theme = _make_console()
    _patch_consoles(rec)
    try:
        R.ideas(cfg, tree=True)
        written.append(_save(rec, out / "cli-ideas-tree.svg", "nw ideas --tree", theme))
    except TypeError:
        pass

    return written


def capture_tui(cfg: dict, out: Path, show_id: str) -> list[Path]:
    from nightwright.tui import textual_available

    if not textual_available():
        print("skip tui-desk.svg (textual extra not installed)", file=sys.stderr)
        return []

    from textual.widgets import DataTable

    from nightwright.tui.app import IdeaDesk

    async def _run() -> str:
        app = IdeaDesk(cfg)
        # Wide enough that state/project columns survive DROP_ORDER clipping.
        async with app.run_test(size=(140, 36)) as pilot:
            for _ in range(80):
                if app._pairs and not app._reload_in_flight:
                    break
                await pilot.pause()
            else:
                raise RuntimeError("desk never loaded idea rows for docs capture")
            # Select the enriched idea (freshness sort alone is not enough once siblings update).
            app.paint(keep_id=show_id)
            index = next(i for i, (r, _) in enumerate(app._pairs) if r.id == show_id)
            app.query_one("#list", DataTable).move_cursor(row=index)
            app.show_detail(index)
            await pilot.pause()
            return app.export_screenshot(title="nw tui")

    import asyncio

    path = out / "tui-desk.svg"
    path.write_text(asyncio.run(_run()), encoding="utf-8")
    return [path]



def capture_agentic(out: Path) -> list[Path]:
    """The agentic loop's `nw queue watch --once` frame from a deterministic demo queue:
    two active campaigns, one waiting, one parked, a backlog and recent deliveries."""
    import datetime as dt
    import json as _json

    from nightwright.agentic import cli_queue as CQ
    from nightwright.agentic import queue as Q
    from nightwright.agentic import store as S

    with tempfile.TemporaryDirectory(prefix="nw-docs-agentic-") as tmp:
        tmp = Path(tmp)
        repo = tmp / "acme"
        specs = repo / "docs" / "specs"
        specs.mkdir(parents=True)
        titles = {
            "SPEC-0041": "CSV export for the weekly report",
            "SPEC-0042": "Group the weekly digest by project",
            "SPEC-0043": "Retry flaky uploads with backoff",
            "SPEC-0044": "Per-project cost budget alerts",
            "SPEC-0045": "Dark mode for the desk",
            "SPEC-0046": "Faster idea search index",
            "SPEC-0039": "Clock-in reminders",
            "SPEC-0038": "Archive old campaigns",
        }
        for sid, title in titles.items():
            (specs / f"{sid[5:]}-x.md").write_text(
                f"---\nid: {sid}\ntitle: \"{title}\"\nstatus: accepted\nowner: user\n"
                f"created: 2026-10-25\n---\n")
        cfg = {"data_dir": str(tmp / "data")}
        now = dt.datetime.now().astimezone()

        def iso(minutes):
            return (now - dt.timedelta(minutes=minutes)).isoformat(timespec="seconds")

        def camp(cid, sid, state, *, created, updated, cost=0.0, **extra):
            S.create_campaign(cfg, {
                "id": cid, "kind": "implement", "spec_id": sid, "project": "Acme",
                "profile": "balanced", "state": state, "created_at": iso(created),
                "repo": {"root": str(repo), "base": "main", "branch": f"nw/{cid}",
                         "worktree": str(tmp / "nw" / cid)},
                "spec": {"rel_path": f"docs/specs/{sid[5:]}-x.md", "internal": True},
                "counters": {"loops": extra.pop("loops", 0), "runs": extra.pop("runs", 1)},
                "cost": {"total_usd": cost, "cap_usd": 10.0}, **extra})
            path = S.campaign_dir(cfg, cid) / "state.json"
            st = _json.loads(path.read_text())
            st["updated_at"] = iso(updated)
            path.write_text(_json.dumps(st))
            return cid

        a = camp("SPEC-0042-20261025-a1f3", "SPEC-0042", "IMPLEMENTING", created=38, updated=4,
                 cost=1.84, loops=1, runs=3,
                 last_gate={"gate_seq": 2, "ok": False, "results": [
                     {"name": "tests", "ok": False, "required": True, "duration_s": 96.0},
                     {"name": "lint", "ok": True, "required": False, "duration_s": 2.1}]},
                 last_verdict={"persona": "gate_triager", "run": "002-gate_triager",
                               "verdict": {"disposition": "fix",
                                           "summary": "test_digest_groups fails: totals not summed per project"}})
        from nightwright.agentic import schemas as SC

        def ev(cid, etype, minutes_ago, **payload):
            S.append_event(cfg, cid, etype, SC.with_defaults(etype, payload))
            lp = S.campaign_dir(cfg, cid) / "ledger.jsonl"
            lines = lp.read_text().splitlines()
            last = _json.loads(lines[-1])
            last["ts"] = iso(minutes_ago)
            lines[-1] = _json.dumps(last)
            lp.write_text("\n".join(lines) + "\n")

        ev(a, "gate", 9, gate_seq=2, ok=False, started_at=iso(10), duration_s=98.1, results=[])
        ev(a, "transition", 8, **{"from": "TRIAGING", "to": "IMPLEMENTING", "reason": "triage_fix"})
        ev(a, "run.start", 4, persona="implementer", run_id="003-implementer", model="sonnet",
           effort="high", max_budget_usd=4.0, timeout_s=1800,
           permission_mode="bypassPermissions", prompt_sha256="0" * 64)
        b = camp("SPEC-0043-20261025-7c2e", "SPEC-0043", "REVIEWING", created=55, updated=2,
             cost=2.47, runs=2,
             last_gate={"gate_seq": 1, "ok": True, "results": [
                 {"name": "tests", "ok": True, "required": True, "duration_s": 112.4},
                 {"name": "lint", "ok": True, "required": False, "duration_s": 1.9}]},
             last_verdict={"persona": "implementer", "run": "001-implementer",
                           "verdict": {"status": "done",
                                       "summary": "Upload retries with exponential backoff and jitter"}})
        ev(b, "gate", 6, gate_seq=1, ok=True, started_at=iso(8), duration_s=114.3, results=[])
        ev(b, "transition", 5, **{"from": "GATING", "to": "REVIEWING", "reason": "gates_green"})
        ev(b, "run.start", 2, persona="conformance_reviewer", run_id="002-conformance_reviewer",
           model="opus", effort="high", max_budget_usd=1.5, timeout_s=1800,
           permission_mode="dontAsk", prompt_sha256="0" * 64)
        camp("SPEC-0044-20261025-55b0", "SPEC-0044", "WAITING", created=70, updated=12,
             cost=0.92, waiting={"resume_to": "GATING", "since": iso(12), "reason": "resumed"})
        p = camp("SPEC-0045-20261025-9e14", "SPEC-0045", "PARKED", created=90, updated=20,
                 cost=10.0, park={"reason": "cost_cap",
                                  "detail": "spent $10.00; an implementer run may cost $4.00",
                                  "resume_to": "IMPLEMENTING"})
        S.append_event(cfg, p, "park", {"reason": "cost_cap", "detail": "cap", "resume_to": "IMPLEMENTING"})
        camp("SPEC-0046-20261025-0d77", "SPEC-0046", "QUEUED", created=15, updated=15, runs=0)
        camp("SPEC-0041-20261025-c3a9", "SPEC-0041", "MERGED", created=140, updated=25, cost=1.31,
             close={"mode": "merge", "merged_sha": "4f2a9c1e0b", "pr_url": None})
        camp("SPEC-0039-20261025-e81b", "SPEC-0039", "MERGED", created=300, updated=180, cost=0.74,
             close={"mode": "merge", "merged_sha": "9b1d77a3c2", "pr_url": None})

        payload = Q.dashboard_payload(cfg)
        payload["lease"] = {"held": True, "pid": 48213, "heartbeat_age_s": 3.2, "stale": False}
        rec, theme = _make_console(width=132, height=60)
        rec.print(CQ._dashboard(payload, 132))
        return [_save(rec, out / "cli-queue-watch.svg", "nw queue watch", theme)]

def export_org_examples(cfg: dict, examples: Path) -> list[Path]:
    examples.mkdir(parents=True, exist_ok=True)
    src = Path(cfg["org_ideas_file"])
    dest = examples / "ideas-demo.org"
    shutil.copyfile(src, dest)

    archive = Path(cfg["org_ideas_archive_file"])
    archive.write_text(
        "#+TODO: IDEA INCUBATE SPECCED | PROMOTED EXPORTED SHIPPED DROPPED\n\n"
        "* SHIPPED Example archived idea\n"
        "  :PROPERTIES:\n"
        "  :ID: IDEA-900\n"
        "  :END:\n"
        "** Summary\n"
        "Terminal states land in the archive file so the active ideas.org stays triage-sized.\n",
        encoding="utf-8",
    )
    arch_dest = examples / "ideas-archive-demo.org"
    shutil.copyfile(archive, arch_dest)

    rotated = examples / "ideas-20260901.org"
    rotated.write_text(
        "#+TODO: IDEA INCUBATE SPECCED | PROMOTED EXPORTED SHIPPED DROPPED\n\n"
        "* INCUBATE Idea from a previous rotation window\n"
        "  :PROPERTIES:\n"
        "  :ID: IDEA-050\n"
        "  :END:\n"
        "** Summary\n"
        "Still readable: rotated files stay under org_files and keep their IDs.\n",
        encoding="utf-8",
    )
    return [dest, arch_dest, rotated]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--examples", type=Path, default=DEFAULT_EXAMPLES)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    _enable_capture_color()

    with tempfile.TemporaryDirectory(prefix="nw-docs-capture-") as tmp:
        cfg = _demo_cfg(Path(tmp))
        show_id = _seed(cfg)
        written = []
        written.extend(capture_time_ops(cfg, args.out))
        written.extend(capture_wave_c(cfg, args.out))
        written.extend(capture_cli(cfg, args.out, show_id))
        written.extend(capture_tui(cfg, args.out, show_id))
        written.extend(capture_agentic(args.out))
        written.extend(export_org_examples(cfg, args.examples))

    for path in written:
        size = path.stat().st_size
        try:
            rel = path.relative_to(ROOT)
        except ValueError:
            rel = path
        print(f"wrote {rel} ({size} bytes)")
        if path.suffix == ".svg":
            text = path.read_text(encoding="utf-8")
            if size < 200 or "<svg" not in text[:500].lower():
                print(f"ERROR: {path} looks empty/invalid", file=sys.stderr)
                return 1
    missing = [n for n in (*WAVE_AB, *WAVE_C) if not (args.out / n).is_file()]
    if missing:
        print(f"ERROR: missing Wave A/B/C captures: {missing}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
