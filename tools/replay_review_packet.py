#!/usr/bin/env python3
"""SPEC-0554 AC7: replay one review loop of a past campaign with `lean.packet` on.

Read-only on live data: the campaign dir is *copied* into `--scratch` (ledger cut at
`--upto-seq`, runs up to the review's entry run) and the branch is checked out in a scratch
clone (`git clone --shared` of the project root, detached at `--head`, a local `main` at
`--base`). Then it does what the queue does in REVIEWING with the flag pinned:

- delta mode (`--loop 2`): build the loop-1 packet at `--prev-head`, store the previous loop's
  verdicts (`--prior-runs`, from the copied run records) the way `_store_verdicts` does, and
  call `packet.remember_verdicts` (what `_begin_review` does);
- build this loop's packet at `--head` (`packet.build`), run the real marshal
  (`review_marshal`, tier marshal) and write its map (`packet.apply_marshal`);
- for each `--personas` reviewer: `personas.build_request` (same call as
  `queue._prepare_run`, previous runs from the copied ledger) and a real `ClaudeRunner` run;
  in delta mode `packet.enforce` marks which new findings would be demoted to advisory.

`--dry-run` stops before any model call and prints the request models and prompt sizes.
Prints one JSON summary per run (model, cost, tokens, findings, demoted, `--match` hits).

    uv run python tools/replay_review_packet.py --campaign SPEC-0529-20261110-f7b4 ...
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from nightwright.config import load_config
from nightwright.agentic import packet as PK
from nightwright.agentic import personas as PS
from nightwright.agentic import project as P
from nightwright.agentic import store as S
from nightwright.agentic.profiles import resolve_profile
from nightwright.agentic.queue import _previous_runs
from nightwright.agentic.runner import ClaudeRunner

LIVE = Path.home() / ".local/share/nightwright/agentic/campaigns"
_EVENT_META = ("schema", "ts", "seq", "campaign", "type", "by")


def git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def run_no(run_id) -> int:
    return int(str(run_id).split("-")[0])


def copy_campaign(src: Path, dst: Path, upto_seq: int, entry_run: int) -> list:
    dst.mkdir(parents=True)
    events = [json.loads(line) for line in (src / "ledger.jsonl").read_text().splitlines()
              if line.strip()]
    events = [e for e in events if e["seq"] <= upto_seq]
    (dst / "ledger.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    for name in ("state.json", "spec_at_open.md"):
        if (src / name).exists():
            shutil.copy2(src / name, dst / name)
    if (src / "gates").is_dir():
        shutil.copytree(src / "gates", dst / "gates")
    (dst / "runs").mkdir()
    for rd in sorted((src / "runs").iterdir()):
        if rd.is_dir() and rd.name[:3].isdigit() and run_no(rd.name) <= entry_run:
            shutil.copytree(rd, dst / "runs" / rd.name)
    return events


def stored_verdicts(src: Path, run_ids, loop0: int) -> dict:
    """The previous loop's `last_review.verdicts`, rebuilt from run records like
    `queue._store_verdicts` (flag pinned, loop 1: no enforcement)."""
    out = {}
    for rid in run_ids:
        rec = json.loads((src / "runs" / rid / "run.json").read_text())
        v = rec.get("verdict") or {}
        persona = rec["persona"]
        entry = {"verdict": v.get("verdict"), "score": v.get("score"),
                 "fix_brief": v.get("fix_brief"),
                 "findings": [f for f in v.get("findings") or [] if isinstance(f, dict)]}
        for k in ("ac_assessment", "coverage_assessment"):
            if k in v:
                entry[k] = v[k]
        entry.update(run=rid, loop=loop0)
        out[persona] = entry
    return out


def usage_of(rr) -> dict:
    u = rr.usage or {}
    return {k: u.get(k) for k in ("input_tokens", "cache_creation_input_tokens",
                                  "cache_read_input_tokens", "output_tokens")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--scratch", required=True, type=Path)
    ap.add_argument("--head", required=True)
    ap.add_argument("--base", required=True, help="merge-base sha the reviewers diffed against")
    ap.add_argument("--loop", type=int, default=1, help="packet loop (1 = full, 2 = delta)")
    ap.add_argument("--prev-head", help="head reviewed in loop 1 (delta mode)")
    ap.add_argument("--prior-runs", default="", help="run ids of the previous loop's reviewers")
    ap.add_argument("--upto-seq", type=int, required=True, help="last ledger seq before review")
    ap.add_argument("--gate-seq", type=int, required=True, help="ledger seq of the last gate")
    ap.add_argument("--entry-run", type=int, required=True, help="last run before the review")
    ap.add_argument("--personas", default="test_reviewer,adversarial_reviewer")
    ap.add_argument("--match", default="", help="regex a pass-finding's title/detail matches")
    ap.add_argument("--reviewer-model", default="",
                    help="override the reviewers' model (e.g. claude-sonnet-5-5, the judge tier)")
    ap.add_argument("--reviewer-effort", default="",
                    help="override the reviewers' effort (e.g. medium)")
    ap.add_argument("--dry-run", action="store_true",
                    help="no model calls; also write each reviewer's flag-off prompt "
                         "(prompt.flagoff.md) and its packet prompt (prompt.packet.md) to compare with the original run's prompt.md")
    a = ap.parse_args(argv)
    if a.loop > 1 and not (a.prev_head and a.prior_runs):
        ap.error("delta mode needs --prev-head and --prior-runs")

    src = LIVE / a.campaign
    sp = a.scratch.resolve()
    if sp.exists():
        ap.error(f"{sp} exists; give a fresh scratch dir")
    cfg = load_config()
    cfg["data_dir"] = str(sp / "data")
    cdir = S.campaign_dir(cfg, a.campaign)
    events = copy_campaign(src, cdir, a.upto_seq, a.entry_run)
    st = json.loads((src / "state.json").read_text())
    root = st["repo"]["root"]
    wt = sp / "wt"
    git(sp, "clone", "--quiet", "--shared", "--no-checkout", root, str(wt))
    git(wt, "checkout", "--quiet", "--detach", a.prev_head or a.head)
    git(wt, "branch", "--quiet", "-f", "main", a.base)

    gate = next(e for e in events if e["seq"] == a.gate_seq and e["type"] == "gate")
    st.update(state="REVIEWING", runner="claude", pending_brief=None,
              lean={"tiers": True, "packet": True, "collator": False, "sessions": False},
              last_gate={k: v for k, v in gate.items() if k not in _EVENT_META})
    for k in ("packet", "packet_prev", "review_heads", "human_overrides", "review_carry",
              "close", "park"):
        st.pop(k, None)
    st["repo"] = {**st["repo"], "worktree": str(wt), "base": "main", "head": a.head}
    st["counters"] = {**(st.get("counters") or {}), "loops": 0, "runs": a.entry_run}
    st["last_review"] = {"entry_run": a.entry_run}
    pcfg = P.project_config(cfg, st["project"])
    prof = resolve_profile(cfg, st.get("profile"))
    out = {"campaign": a.campaign, "loop": a.loop, "head": a.head, "runs": []}

    if a.loop > 1:                                   # loop 1 at prev_head, then its verdicts
        st["counters"]["loops"] = a.loop - 2
        st["packet"] = PK.build(cfg, st)
        PK.apply_marshal(cfg, st, None, error="replay: loop-1 marshal not run")
        st["last_review"]["verdicts"] = stored_verdicts(src, a.prior_runs.split(","),
                                                        a.loop - 2)
        PK.remember_verdicts(st)                     # _begin_review of the next loop
        st["last_review"] = {"entry_run": a.entry_run}
        st["counters"]["loops"] = a.loop - 1
        git(wt, "checkout", "--quiet", "--detach", a.head)

    t0 = time.time()
    st["packet"] = PK.build(cfg, st)
    doc = PK.load(cfg, st)
    out["packet"] = {**st["packet"], "build_s": round(time.time() - t0, 2),
                     "prev_head": doc["prev_head"], "acs": len(doc["acs"]),
                     "prior_findings": len(doc["findings"]),
                     "delta_files": [s["path"] for s in (doc.get("delta") or {}).get("stat") or []]}
    n = a.entry_run

    def request(persona):
        nonlocal n
        n += 1
        rd = S.runs_dir(cfg, a.campaign) / f"{n:03d}-{persona}"
        rd.mkdir(parents=True)
        prev = [] if persona == PK.MARSHAL else _previous_runs(cfg, st, persona)
        return PS.build_request(cfg, prof, pcfg, st, persona, run_dir=rd, previous=prev)

    def record(persona, req, rr):
        row = {"persona": persona, "run_dir": str(req.run_dir), "model": req.model,
               "effort": req.effort, "tier": req.tier, "prompt_chars": len(req.prompt)}
        if rr is not None:
            row.update(exit_code=rr.exit_code, is_error=rr.is_error, verdict_ok=rr.verdict_ok,
                       cost_usd=rr.cost_usd, duration_s=rr.duration_s, usage=usage_of(rr))
        out["runs"].append(row)
        return row

    req = request(PK.MARSHAL)
    rr = None if a.dry_run else ClaudeRunner().run(req)
    record(PK.MARSHAL, req, rr)
    if not a.dry_run:
        ev = PK.apply_marshal(cfg, st, rr.verdict if rr.verdict_ok else None,
                              error=None if rr.verdict_ok else "marshal failed")
        out["packet"]["marshal_event"] = ev
        out["packet"]["valid"] = PK.validate(PK.load(cfg, st))
    enf = PK.enforcement(cfg, st)
    pat = re.compile(a.match, re.IGNORECASE | re.DOTALL) if a.match else None
    for persona in [p for p in a.personas.split(",") if p]:
        req = request(persona)
        if a.reviewer_model:
            req = dataclasses.replace(req, model=a.reviewer_model, model_alias=None)
        if a.reviewer_effort:
            req = dataclasses.replace(req, effort=a.reviewer_effort)
        if a.dry_run:
            (req.run_dir / "prompt.packet.md").write_text(req.prompt)
            off = {**st, "lean": None}
            (req.run_dir / "prompt.flagoff.md").write_text(PS.build_request(
                cfg, prof, pcfg, off, persona, run_dir=req.run_dir,
                previous=_previous_runs(cfg, st, persona)).prompt)
        rr = None if a.dry_run else ClaudeRunner().run(req)
        row = record(persona, req, rr)
        if rr is None or not rr.verdict_ok:
            continue
        v = rr.verdict
        findings = v.get("findings") or []
        if enf is not None:
            findings, demoted = PK.enforce(st.get("spec_id"), persona, v, enf)
            row["demoted"] = demoted
        row["verdict"] = v.get("verdict")
        row["score"] = v.get("score")
        row["prior_status"] = v.get("prior_status")
        row["findings"] = [{k: f.get(k) for k in ("severity", "basis", "ac", "title", "prior",
                                                  "demoted")} for f in findings]
        if pat is not None:
            row["match"] = [
                {"title": f.get("title"), "severity": f.get("severity"),
                 "basis": f.get("basis"), "prior": f.get("prior"),
                 "advisory": bool(f.get("demoted"))}
                for f in findings
                if pat.search(f"{f.get('title')}\n{f.get('detail')}")]
    print(json.dumps(out, indent=2, default=str))
    (sp / "replay.json").write_text(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
