"""Read-only org-mode task model (SPEC-0005). Parses the files/dirs/globs in
`cfg["org_files"]` into a flat list of normalized `Task`s via `orgparse`, honoring per-file
`#+TODO` keyword sets (with `org_todo_keywords` as the fallback for files that declare none),
grouped tags, priorities, `:PROPERTIES:`, and SCHEDULED/DEADLINE/CLOSED. The JIRA `topic_key`
links a task to `nw`'s tracked time; it comes from the headline (via topics.KEY_RE) unless a
`:TOPIC:`/`:JIRA:`/`:REPO:` property overrides it. Nothing here writes org files."""
import datetime as dt
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from glob import glob
from pathlib import Path

import orgparse
from orgparse.node import OrgEnv

from .topics import KEY_RE

# Property names that override the headline-derived topic key, in precedence order.
_TOPIC_PROPS = ("TOPIC", "JIRA", "REPO")
_EMACSD = os.path.expanduser("~/.emacs.d")

# Org tags allow letters, numbers, `_`, `@`, and `-` (Emacs). orgparse's RE_HEADING_TAGS is
# historically `[\w@:]+` only — hyphenated tags like :session-triage: stay stuck in the
# heading (SPEC-0038). Re-parse with a hyphen-aware cookie at the end of the headline.
_RE_HEADING_TAGS = re.compile(r"(.*?)\s*:([\w@:-]+):\s*$")


@dataclass(frozen=True)
class Task:
    id: str
    heading: str
    state: str | None
    is_done: bool
    tags: frozenset
    priority: str | None
    properties: dict = field(default_factory=dict)
    scheduled: dt.date | None = None
    deadline: dt.date | None = None
    closed: dt.date | None = None
    level: int = 1
    project: str = ""
    file: str = ""
    line: int = 0
    topic_key: str | None = None
    is_idea: bool = False
    epic: str | None = None
    # (start, end) pairs from the headline's :LOGBOOK: CLOCK: entries (SPEC-0061); end is None
    # for a still-open clock. Never touch orgparse's own `.duration` on an open entry — it
    # raises (None - datetime). Compute end - start yourself when end is not None.
    clock: tuple = ()


def _split_keywords(keywords):
    """Split an org-style keyword list (`["TODO", "|", "DONE"]`) into (todos, dones).
    Everything before a `"|"` is active; everything after is done. No `"|"` ⇒ all active."""
    if "|" in keywords:
        i = keywords.index("|")
        return list(keywords[:i]), list(keywords[i + 1:])
    return list(keywords), []


def _own_org_files(cfg):
    """The files nightwright itself rewrites (by atomic rename): the ideas, capture and ideas-archive
    files, those that exist."""
    paths = [cfg.get("org_ideas_file"), cfg.get("org_capture_file"),
             cfg.get("org_ideas_archive_file")]
    if cfg.get("org_ideas_file") and not cfg.get("org_ideas_archive_file"):
        paths.append(os.path.join(os.path.dirname(os.path.expanduser(cfg["org_ideas_file"])),
                                  "ideas-archive.org"))
    return [q for q in (os.path.expanduser(str(x)) for x in paths if x) if os.path.isfile(q)]


def _iter_org_paths(cfg):
    """Expand cfg['org_files'] (files / dirs / globs) into a de-duped, stable list of .org
    paths. Dirs expand to **/*.org; anything under ~/.emacs.d is skipped.

    A directory scan racing an atomic rename in that directory can miss the renamed file (seen
    on btrfs: ~2% of scans while another process rewrites `ideas.org`), so nightwright's own files under
    a configured directory are added by name, never left to the scan."""
    seen = {}
    own = _own_org_files(cfg)
    for entry in cfg.get("org_files", []) or []:
        p = os.path.expanduser(str(entry))
        candidates = []
        if os.path.isdir(p):
            found = [str(x) for x in Path(p).rglob("*.org")]
            real = {os.path.realpath(x) for x in found}
            root = os.path.realpath(p) + os.sep
            found += [q for q in own if q.endswith(".org") and os.path.realpath(q) not in real
                      and os.path.realpath(q).startswith(root)]
            candidates = sorted(found, key=Path)
        elif os.path.isfile(p):
            candidates = [p]
        else:                                       # treat as a glob pattern
            candidates = sorted(glob(p, recursive=True))
        for c in candidates:
            rc = os.path.realpath(c)
            if rc.startswith(_EMACSD + os.sep) or rc == _EMACSD:
                continue
            seen.setdefault(rc, c)
    return list(seen.values())


def org_files_mtime_signature(cfg) -> frozenset:
    """A cheap, comparable snapshot of the org corpus's on-disk state: {(path, mtime_ns), ...}
    for every file `_iter_org_paths` resolves. Two calls compare equal iff nothing changed (an
    edit bumps mtime; an added/removed file changes the set) — used by the TUI to detect
    external edits without re-parsing (SPEC-0128)."""
    out = set()
    for p in _iter_org_paths(cfg):
        try:
            out.add((p, os.stat(p).st_mtime_ns))
        except OSError:
            continue
    return frozenset(out)


def _coerce_date(orgdate):
    """orgparse OrgDate -> datetime.date (or None). .start is a date or datetime."""
    if not orgdate:
        return None
    start = orgdate.start
    if isinstance(start, dt.datetime):
        return start.date()
    return start


def _project(node, props=None):
    """The task's project: a `:PROJECT:` property if present (SPEC-0018), else the nearest
    ancestor level-1 (category) heading text, else the file stem."""
    if props and props.get("PROJECT"):
        return props["PROJECT"]
    p = node.parent
    while p is not None and getattr(p, "level", 0) > 0:
        if p.level == 1:
            return p.heading
        p = p.parent
    return Path(node.env.filename).stem


def _topic_key(heading, properties):
    for name in _TOPIC_PROPS:
        if properties.get(name):
            return properties[name]
    m = KEY_RE.search(heading or "")
    return m.group(1) if m else None


def _heading_and_tags(heading, parsed_tags=()) -> tuple[str, frozenset]:
    """Strip a trailing org tag cookie from `heading` and union with orgparse tags.

    Hyphenated tags (e.g. :session-triage:) are included (SPEC-0038); orgparse alone leaves
    them stuck in the heading text.
    """
    heading = heading or ""
    tags = set(parsed_tags or ())
    m = _RE_HEADING_TAGS.search(heading)
    if m:
        heading = m.group(1)
        tags.update(t for t in m.group(2).split(":") if t)
    return heading, frozenset(tags)


def _is_enrichment(node, idea_keywords):
    """True if `node` is nested under an idea-state headline — i.e. it lives inside an idea's
    Summary / Open questions / Log body (SPEC-0054). Such nodes (stateful question headlines
    like `*** OPEN …`, Log timestamp stamps) are enrichment, never tasks, so they must not
    leak into `nw tasks`/`nw agenda`. The idea headline itself has no idea ancestor and is
    therefore not enrichment."""
    if not idea_keywords:
        return False
    p = node.parent
    while p is not None and getattr(p, "level", 0) > 0:
        if getattr(p, "todo", None) in idea_keywords:
            return True
        p = p.parent
    return False


def _node_clock(node):
    """(start, end) pairs from `node.clock` (SPEC-0061); end is None for an open clock. Never
    reads orgparse's `.duration` — it raises on an open (end=None) entry."""
    out = []
    for c in getattr(node, "clock", None) or ():
        start = c.start.replace(tzinfo=None) if isinstance(c.start, dt.datetime) else c.start
        end = c.end
        if end is not None:
            end = end.replace(tzinfo=None) if isinstance(end, dt.datetime) else end
        out.append((start, end))
    return tuple(out)


def _node_to_task(node, file, idea_keywords=frozenset()):
    props = dict(node.properties)
    heading, tags = _heading_and_tags(node.heading, node.tags)
    state = node.todo
    is_done = bool(state) and state in node.env.done_keys
    task_id = props.get("ID") or f"{file}:{node.linenumber}"
    return Task(
        id=task_id,
        heading=heading,
        state=state,
        is_done=is_done,
        tags=tags,
        priority=node.priority,
        properties=props,
        scheduled=_coerce_date(node.scheduled),
        deadline=_coerce_date(node.deadline),
        closed=_coerce_date(node.closed),
        level=node.level,
        project=_project(node, props),
        file=file,
        line=node.linenumber,
        topic_key=_topic_key(heading, props),
        is_idea=bool(state) and state in idea_keywords,
        epic=props.get("EPIC"),
        clock=_node_clock(node),
    )


# Process-local cache keyed by (mtime signature, keyword config) (SPEC-0134). A hit means
# the corpus bytes/paths *and* the TODO/idea vocabularies that shape Task flags are unchanged
# — safe to reuse Task objects. Callers may mutate the returned *list*; we always hand back a
# shallow copy.
_tasks_cache_key = None
_tasks_cache: list | None = None


def invalidate_tasks_cache() -> None:
    """Drop the load_tasks cache (tests / explicit bust). Mtime changes also miss naturally."""
    global _tasks_cache_key, _tasks_cache
    _tasks_cache_key = None
    _tasks_cache = None


def _load_tasks_cache_key(cfg) -> tuple:
    """Cache identity: on-disk corpus signature + keyword lists that affect Task construction."""
    todos = tuple(cfg.get("org_todo_keywords") or ["TODO", "|", "DONE"])
    ideas = tuple(cfg.get("org_idea_keywords") or [])
    return (org_files_mtime_signature(cfg), todos, ideas)


def load_tasks(cfg):
    """Parse every configured org source into a flat list[Task] (read-only).

    SPEC-0134: results are cached against `_load_tasks_cache_key(cfg)` (mtime signature +
    TODO/idea keywords). Unchanged corpus and vocabulary → no reparse (TUI j/k +
    resolve_selector stay cheap). Any path add/remove, mtime bump, or keyword change → miss
    and reparse, so SPEC-0128 freshness still holds.
    """
    global _tasks_cache_key, _tasks_cache
    key = _load_tasks_cache_key(cfg)
    if _tasks_cache is not None and key == _tasks_cache_key:
        return list(_tasks_cache)

    todos, dones = _split_keywords(cfg.get("org_todo_keywords") or ["TODO", "|", "DONE"])
    # Idea vocabulary (SPEC-0012) is global (not per-file, unlike is_done): a task's state
    # being in this set (either side of `|`) marks it as an idea regardless of source file.
    idea_todos, idea_dones = _split_keywords(cfg.get("org_idea_keywords") or [])
    idea_keywords = frozenset(idea_todos) | frozenset(idea_dones)
    tasks = []
    for path in _iter_org_paths(cfg):
        # A file's own #+TODO wins; the seeded env is the fallback for files without one.
        env = OrgEnv(todos=todos, dones=dones, filename=path)
        root = orgparse.load(path, env=env)
        for node in root[1:]:                       # root[0] is the file-level node
            if _is_enrichment(node, idea_keywords):  # idea Summary/questions/Log body
                continue
            tasks.append(_node_to_task(node, path, idea_keywords))
    _tasks_cache_key = _load_tasks_cache_key(cfg)
    _tasks_cache = tasks
    return list(tasks)


def filter_tasks(tasks, *, state=None, tag=None, project=None, priority=None, epic=None,
                 done=None, has_key=None, is_idea=None, workstream=None):
    """Narrow a task list (all criteria AND-compose). `done` filters is_done when not None;
    `has_key` filters on topic_key presence when not None; `is_idea` selects/excludes ideas.
    SPEC-0090: `workstream` matches `:WORKSTREAM:` property exactly."""
    out = tasks
    if state is not None:
        out = [t for t in out if t.state == state]
    if tag is not None:
        out = [t for t in out if tag in t.tags]
    if project is not None:
        out = [t for t in out if t.project == project]
    if epic is not None:                                     # SPEC-0116
        out = [t for t in out if (t.epic or "") == epic]
    if priority is not None:
        out = [t for t in out if t.priority == priority]
    if done is not None:
        out = [t for t in out if t.is_done == done]
    if has_key is not None:
        out = [t for t in out if (t.topic_key is not None) == has_key]
    if is_idea is not None:
        out = [t for t in out if t.is_idea == is_idea]
    if workstream is not None:
        out = [t for t in out if (t.properties.get("WORKSTREAM") or "") == workstream]
    return list(out)


def join_time(tasks, topic_secs):
    """Join tracked time to tasks by topic_key (SPEC-0008). `topic_secs` is
    {base_topic: seconds} (as produced from aggregate.assemble over a scope). A base topic
    matches a task when it equals a task's topic_key, or when its embedded JIRA key does.

    Returns (hours_by_key, key_to_tasks, untracked):
      hours_by_key: {topic_key: seconds} summed over matching base topics;
      key_to_tasks: {topic_key: [Task, ...]} (a key may map to several tasks);
      untracked:    {base_topic: seconds} for tracked topics matching no task.

    Invariant: for a base topic that *is* a task key, its seconds land verbatim in
    hours_by_key[key] — so a key's joined hours equal that key's tracked hours in the scope."""
    key_to_tasks = {}
    for t in tasks:
        if t.topic_key:
            key_to_tasks.setdefault(t.topic_key, []).append(t)
    hours_by_key = defaultdict(float)
    untracked = defaultdict(float)
    for topic, secs in topic_secs.items():
        key = topic if topic in key_to_tasks else None
        if key is None:
            m = KEY_RE.search(topic)
            if m and m.group(1) in key_to_tasks:
                key = m.group(1)
        if key:
            hours_by_key[key] += secs
        else:
            untracked[topic] += secs
    return dict(hours_by_key), key_to_tasks, dict(untracked)
