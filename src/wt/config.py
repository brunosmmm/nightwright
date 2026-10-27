"""Config loading: merge user config.yaml (from the XDG config dir) over defaults and
attach resolved runtime paths (config/data/cache dirs, timezone, roots)."""
import os
import sys
from zoneinfo import ZoneInfo

try:
    import yaml
except ImportError:
    print("pyyaml required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from . import paths

DEFAULT_CONFIG = {
    "transcripts_root": "~/.claude/projects",
    "cursor_transcripts_root": "~/.cursor/projects",  # set to null to disable
    "work_root": "~/work",
    # SPEC-0166: path to this wt checkout (skills/, docs/specs, Meta-Tools research root).
    # Only needed for a non-editable install; `uv tool install --editable .` resolves it.
    "wt_checkout": None,
    "timezone": "America/New_York",
    "default_axis": "topic",   # report grouping when --by omitted ("topic" = base topics)
    "gap_minutes": 15,
    "min_block_seconds": 60,
    "meetings_file": "meetings.jsonl",
    "history_file": "history.jsonl",
    "meeting_filter": {"status": ["busy"], "response": ["accepted"], "allowlist": []},
    # org-mode task management (SPEC-0004/0005). Sources: files/dirs/globs; dirs -> **/*.org
    # (~/.emacs.d excluded). A user-supplied list REPLACES this default (lists don't merge).
    "org_files": ["~/work/org"],
    # Fallback TODO keyword set for org files that declare no `#+TODO`. `|` splits
    # active from done keywords (org syntax). A file's own `#+TODO` always wins.
    "org_todo_keywords": ["TODO", "|", "DONE"],
    # Default append target for `wt add` (SPEC-0010). Lives under ~/work/org so captured
    # tasks are immediately visible to `wt tasks`/`wt digest`.
    "org_capture_file": "~/work/org/inbox.org",
    # Idea capture (SPEC-0012): a dedicated file + its own idea vocabulary (`|` splits
    # active from done, same convention as org_todo_keywords). A task's state being in
    # this set marks it Task.is_idea, keeping ideas out of `wt tasks` by default.
    "org_ideas_file": "~/work/org/ai/ideas.org",
    "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "SHIPPED",
                          "DROPPED", "RESEARCHED"],
    # Automatic rotation (SPEC-0071): once org_ideas_file crosses this many lines, the next
    # capture renames it to a dated historical file and starts a fresh one at the same path.
    "idea_rotation_max_lines": 1200,
    # Terminal-idea archival (SPEC-0072/0073): where archive_idea() moves a settled idea's
    # whole subtree once it's DROPPED/RESEARCHED/PROMOTED/EXPORTED-and-done. None (SPEC-0168) =
    # `ideas-archive.org` next to org_ideas_file, so relocating the ideas file moves the archive
    # with it instead of writing into ~/work/org/ai (see org_write.ideas_archive_path).
    "org_ideas_archive_file": None,
    # Curated workstream lanes (SPEC-0090): closed list; empty = feature inert until configured.
    # A user-supplied list REPLACES this default (lists don't merge).
    "workstreams": [],
    # Hub tasks slice (SPEC-0092): drop these TODO states from hub task lists; cap open slice.
    "hub_noise_states": ["CATEGORIZE"],
    "hub_tasks_cap": 40,
    # Open-questions state vocabulary (SPEC-0054): questions under an idea's `** Open
    # questions` are `*** OPEN|RESOLVED` headlines, backed by a `#+TODO: OPEN | RESOLVED`
    # line in the ideas file. `|` splits active from done, same convention as above.
    "org_question_keywords": ["OPEN", "|", "RESOLVED"],
    # Outbound spec export targets (SPEC-0015): per-project destination repo for
    # `wt spec export`. { "<project>": { "repo_path": "~/work/<repo>", "spec_dir": "docs/specs" } }
    "outbox_targets": {},
    # Per-project idea extension schema (SPEC-0120): `{ "<project>": { required: [], properties: {} } }`.
    # No entry ⇒ freeform Ext under `** Ext <Project>` (SPEC-0119).
    "idea_extensions": {},
    # Entity association on capture (SPEC-0018): which mappings.yaml facet axis is treated
    # as "project". Known projects = distinct values of this axis across load_mappings(cfg).
    "project_axis": "bucket",
    # Pretty wt idea show (SPEC-0035/0036): Pygments style name for Rich Syntax.
    # Default `nord` (dark-terminal friendly); light themes like `manni` wash out.
    "idea_show_theme": "nord",
    # Org backup retention (SPEC-0271): per backed-up file, keep the newest `keep_last`
    # backups plus the newest of each day within `keep_days`; the rest is pruned on every
    # write and by `wt backups prune`. Invalid (non-integer/negative) values use these.
    "org_backups": {"keep_last": 50, "keep_days": 14},
    # Agentic workflow (SPEC-0169/0171): the loop's runner, service and profiles. `profiles`
    # holds deltas over the profiles in agentic/profile_defs/*.yaml (and over
    # <config_dir>/profiles/*.yaml); a project's .wt/profiles/*.yaml sits above them all
    # (SPEC-0237). load_config merges one level deep, so resolve_profile deep-merges the
    # layers in code. Scalar keys replace outright.
    "agentic": {
        "runner": "claude",            # claude | fake (tests; SPEC-0174)
        "fake_script": None,           # FakeRunner script path (SPEC-0174)
        "default_profile": "balanced",
        "spec_review_profile": None,   # `wt spec review` default profile; None = default_profile (SPEC-0278)
        "poll_interval_s": 20,
        "lease_ttl_s": 120,
        "run_launcher": "nohup",       # nohup | tmux | foreground (used by /wt-conduct)
        "pr_sweep": {"interval_s": 900},   # follower's HANDED_OFF PR sweep; 0 disables (SPEC-0283)
        "profiles": {},
    },
}


def _load_yaml(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return yaml.safe_load(f) or default


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    config_dir = paths.config_dir()
    user = _load_yaml(os.path.join(config_dir, "config.yaml"), {})
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k] = {**cfg[k], **v}
        else:
            cfg[k] = v
    cfg["_tz"] = ZoneInfo(cfg["timezone"])
    cfg["_work"] = os.path.expanduser(cfg["work_root"]).rstrip("/")
    cfg["_home"] = os.path.expanduser("~").rstrip("/")
    cfg["_root"] = os.path.expanduser(cfg["transcripts_root"])
    cur = cfg.get("cursor_transcripts_root")
    cfg["_cursor_root"] = os.path.expanduser(cur) if cur else None
    cfg["config_dir"] = config_dir
    cfg["data_dir"] = paths.data_dir()
    cfg["cache_dir"] = paths.cache_dir()
    return cfg
