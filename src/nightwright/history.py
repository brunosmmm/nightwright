"""Durable per-day archive (history.jsonl): snapshot closed days as BASE topics so reports
survive Claude transcript cleanup. read_history is consumed by aggregate.assemble."""
import datetime as dt
import json
import os

from .console import console


def history_path(cfg):
    p = cfg.get("history_file", "history.jsonl")
    return p if os.path.isabs(p) else os.path.join(cfg["data_dir"], p)


def read_history(cfg):
    """List of per-day records {day, topics{base->secs}, actor{claude,you}, wall}."""
    p = history_path(cfg); by_day = {}
    if os.path.exists(p):
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        r = json.loads(line); by_day[r["day"]] = r
                    except Exception:
                        pass
    return list(by_day.values())


def snapshot(cfg, through=None):
    """Archive *closed* (past) days to history.jsonl as BASE topics (overrides applied,
    mappings NOT) so current mappings still reapply retroactively. Idempotent."""
    from .aggregate import build, union_secs
    from .rules import load_overrides
    from .topics import RepoResolver
    tz = cfg["_tz"]; overrides = load_overrides(cfg); rr = RepoResolver(cfg)
    day_topic, day_intervals, _, day_actor = build(cfg, overrides, rr)
    today = dt.datetime.now(tz).date().isoformat()
    cutoff = through or (dt.datetime.now(tz).date() - dt.timedelta(days=1)).isoformat()
    existing = {r["day"]: r for r in read_history(cfg)}
    n = 0
    for d in sorted(day_topic):
        if d >= today or d > cutoff:      # only closed days, up to cutoff
            continue
        existing[d] = {"day": d,
                       "topics": {tp: round(s, 1) for tp, s in day_topic[d].items()},
                       "actor": {k: round(v, 1) for k, v in day_actor[d].items()},
                       "wall": round(union_secs(day_intervals[d]), 1)}
        n += 1
    os.makedirs(os.path.dirname(history_path(cfg)) or ".", exist_ok=True)
    with open(history_path(cfg), "w") as f:
        for d in sorted(existing):
            f.write(json.dumps(existing[d]) + "\n")
    console.print(f"[green]✓[/] snapshotted {n} closed day(s) → {history_path(cfg)} "
                  f"([bold]{len(existing)}[/] archived total)")
