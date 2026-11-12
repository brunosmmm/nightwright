#!/usr/bin/env python3
"""SPEC-0555 regression set: replay the findings collator on one review loop of a past campaign.

Read-only on live data: the campaign's `state.json` / `spec_at_open.md` and the listed reviewer
run records are *copied* into `--scratch`, and the campaign head is checked out in a scratch clone
(`git clone --shared --no-checkout` of the project root, detached at `--head`, a local `main` at
`--base`). Then it does what the queue does in REVIEWING with `lean.{tiers,packet,collator}`
pinned: store the reviewers' verdicts as `_store_verdicts` would (loop 1: no delta enforcement),
build the loop's packet (code only; the marshal is not run), build the collator request with
`personas.build_request` (same call as `queue._prepare_run`), run it for real with
`ClaudeRunner`, and apply the code guards (`collator.collate`).

Prints one JSON summary: model, tier, cost, tokens, the collator's raw scope for each finding,
the scope after the guards, and the `--match` hits (a regex over title / detail): each hit must
come out `in`.

    uv run python tools/replay_collator.py --campaign SPEC-0529-20261110-f7b4 \\
        --runs 072-conformance_reviewer,073-test_reviewer,074-adversarial_reviewer \\
        --head ee44a190 --base 4bd6f0eb --scratch /tmp/x --match 'MAC oracle'
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from nightwright.config import load_config
from nightwright.agentic import collator as CO
from nightwright.agentic import packet as PK
from nightwright.agentic import personas as PS
from nightwright.agentic import project as P
from nightwright.agentic import store as S
from nightwright.agentic.profiles import resolve_profile
from nightwright.agentic.runner import ClaudeRunner

LIVE = Path.home() / ".local/share/nightwright/agentic/campaigns"


def git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def stored_verdicts(src: Path, run_ids, loop0: int) -> dict:
    out = {}
    for rid in run_ids:
        rec = json.loads((src / "runs" / rid / "run.json").read_text())
        v = rec.get("verdict") or {}
        entry = {"verdict": v.get("verdict"), "score": v.get("score"),
                 "fix_brief": v.get("fix_brief"),
                 "findings": [f for f in v.get("findings") or [] if isinstance(f, dict)]}
        for k in ("ac_assessment", "coverage_assessment"):
            if k in v:
                entry[k] = v[k]
        entry.update(run=rid, loop=loop0)
        out[rec["persona"]] = entry
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--scratch", required=True, type=Path)
    ap.add_argument("--runs", required=True, help="the loop's reviewer run ids, comma-separated")
    ap.add_argument("--head", required=True)
    ap.add_argument("--base", required=True, help="base sha the campaign diffed against")
    ap.add_argument("--match", default="", help="regex a must-stay-in finding matches")
    ap.add_argument("--dry-run", action="store_true", help="no model call")
    a = ap.parse_args(argv)
    src = LIVE / a.campaign
    sp = a.scratch.resolve()
    if sp.exists():
        ap.error(f"{sp} exists; give a fresh scratch dir")
    cfg = load_config()
    cfg["data_dir"] = str(sp / "data")
    cdir = S.campaign_dir(cfg, a.campaign)
    cdir.mkdir(parents=True)
    for name in ("state.json", "spec_at_open.md"):
        if (src / name).exists():
            shutil.copy2(src / name, cdir / name)
    (cdir / "runs").mkdir()
    for rid in a.runs.split(","):
        shutil.copytree(src / "runs" / rid, cdir / "runs" / rid)
    (cdir / "ledger.jsonl").write_text("")
    st = json.loads((src / "state.json").read_text())
    wt = sp / "wt"
    git(sp, "clone", "--quiet", "--shared", "--no-checkout", st["repo"]["root"], str(wt))
    git(wt, "checkout", "--quiet", "--detach", a.head)
    git(wt, "branch", "--quiet", "-f", "main", a.base)
    st.update(state="REVIEWING", runner="claude", pending_brief=None,
              lean={"tiers": True, "packet": True, "collator": True, "sessions": False})
    for k in ("packet", "packet_prev", "review_heads", "human_overrides", "review_carry",
              "close", "park", "last_gate"):
        st.pop(k, None)
    st["repo"] = {**st["repo"], "worktree": str(wt), "base": "main", "head": a.head}
    entry = max(int(r.split("-")[0]) for r in a.runs.split(","))
    st["counters"] = {**(st.get("counters") or {}), "loops": 0, "runs": entry}
    verdicts = stored_verdicts(src, a.runs.split(","), 0)
    st["last_review"] = {"entry_run": entry, "verdicts": verdicts}
    st["packet"] = PK.build(cfg, st)
    PK.apply_marshal(cfg, st, None, error="replay: marshal not run")
    pcfg = P.project_config(cfg, st["project"])
    prof = resolve_profile(cfg, st.get("profile"))
    rd = S.runs_dir(cfg, a.campaign) / f"{entry + 1:03d}-{CO.NAME}"
    rd.mkdir(parents=True)
    req = PS.build_request(cfg, prof, pcfg, st, CO.NAME, run_dir=rd, previous=[],
                           extra=CO.collator_values(cfg, st, verdicts))
    items = CO.inputs(verdicts)
    out = {"campaign": a.campaign, "head": a.head, "runs": a.runs, "model": req.model,
           "effort": req.effort, "tier": req.tier, "read_only": req.read_only,
           "prompt_chars": len(req.prompt), "findings": len(items)}
    if a.dry_run:
        (rd / "prompt.replay.md").write_text(req.prompt)
        print(json.dumps(out, indent=2))
        return 0
    rr = ClaudeRunner().run(req)
    u = rr.usage or {}
    out.update(exit_code=rr.exit_code, is_error=rr.is_error, verdict_ok=rr.verdict_ok,
               cost_usd=rr.cost_usd, duration_s=rr.duration_s,
               usage={k: u.get(k) for k in ("input_tokens", "cache_creation_input_tokens",
                                            "cache_read_input_tokens", "output_tokens")})
    if not rr.verdict_ok:
        out["result_tail"] = (rr.result_text or "")[-600:]
    else:
        raw = {}
        for e in rr.verdict.get("collated") or []:
            for i in [e.get("id"), *(e.get("merged") or [])]:
                raw[i] = {"scope": e.get("scope"), "tie": e.get("tie"), "reason": e.get("reason")}
        ac_ids = {x["id"] for x in CO.acs_of(cfg, st)}
        records, stats = CO.collate(rr.verdict, items, changed=CO.diff_files(cfg, st),
                                    unmet=CO.unmet_acs(verdicts), ac_ids=ac_ids,
                                    worktree=str(wt))
        sm = CO.scope_map(records)
        out["stats"] = stats
        out["collated"] = len(records)
        out["out"] = [r["title"] for r in records if r["scope"] == "out"]
        out["raw_out"] = sum(1 for v in raw.values() if v["scope"] == "out")
        if a.match:
            pat = re.compile(a.match, re.IGNORECASE | re.DOTALL)
            out["match"] = [{"id": f["id"], "severity": f["severity"], "title": f["title"],
                             "collator": raw.get(f["id"]), "final": sm.get(f["id"])}
                            for f in items if pat.search(f"{f['title']}\n{f.get('detail')}")]
    print(json.dumps(out, indent=2, default=str))
    (sp / "replay.json").write_text(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
