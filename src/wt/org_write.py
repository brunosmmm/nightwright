"""Safe TODO-state write-back for org files (SPEC-0009). orgparse is read-only, so this is
custom, deliberately-minimal mutation: it rewrites exactly the keyword token of one headline
(line-anchored + old-state verified against drift), optionally manages a CLOSED: stamp, backs
the file up under cfg['data_dir']/org-backups, and writes atomically. It never edits tags,
priority, body, or any other headline."""
import datetime as dt
import fcntl
import functools
import os
import re
import shutil
import tempfile
import threading

import orgparse
from orgparse.node import OrgEnv

from .render import warn
from .dates import parse_date
from .org import _split_keywords, filter_tasks, load_tasks

_HEADLINE_RE = re.compile(r"^(\*+)(\s+)(.*)$")


# ---- concurrency (SPEC-0190) ----------------------------------------------------------------
# Several sessions write the same ideas file. Every read-modify-write of an org file holds one
# cross-process lock (flock on <data_dir>/org-write.lock), re-entrant within a thread so
# writers can call writers. Inside it, a writer re-reads the file and finds its headline by
# identity (`_locate_headline`), never trusting the line number it parsed earlier.

_ORG_LOCK_GUARD = threading.Lock()
_ORG_LOCK_STATE = threading.local()
_ORG_THREAD_LOCK = threading.RLock()


def _org_lock_path(cfg) -> str:
    return os.path.join(cfg["data_dir"], "org-write.lock")


class org_write_lock:
    """`with org_write_lock(cfg):` — exclusive across processes and threads, re-entrant."""

    def __init__(self, cfg):
        self.cfg = cfg

    def __enter__(self):
        _ORG_THREAD_LOCK.acquire()
        depth = getattr(_ORG_LOCK_STATE, "depth", 0)
        if depth == 0:
            path = _org_lock_path(self.cfg)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX)
            _ORG_LOCK_STATE.fd = fd
        _ORG_LOCK_STATE.depth = depth + 1
        return self

    def __exit__(self, *exc):
        _ORG_LOCK_STATE.depth -= 1
        if _ORG_LOCK_STATE.depth == 0:
            fd = _ORG_LOCK_STATE.fd
            _ORG_LOCK_STATE.fd = None
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        _ORG_THREAD_LOCK.release()
        return False


PERSONA_ENV = "WT_PERSONA_RUN"


class PersonaOrgWriteRefused(ValueError):
    """An org write was attempted from inside a campaign agent run (SPEC-0272)."""


def refuse_in_persona_run():
    """Raise when this process is a campaign persona run: agents report through
    `wt campaign note` / the verdict's followup_ideas; the runner files those itself."""
    if os.environ.get(PERSONA_ENV) == "1":
        raise PersonaOrgWriteRefused(
            "wt: refusing to write the org from a campaign agent (WT_PERSONA_RUN=1); "
            "report it with 'wt campaign note' or the verdict's followup_ideas instead")


def org_writer(fn):
    """Decorator for an org-file writer whose first argument is `cfg`: runs it under
    `org_write_lock`. `tests/test_org_write_race.py` checks every writer carries it."""
    @functools.wraps(fn)
    def wrapper(cfg, *args, **kwargs):
        refuse_in_persona_run()
        with org_write_lock(cfg):
            return fn(cfg, *args, **kwargs)
    wrapper.__org_writer__ = True
    return wrapper


def _drawer_id(lines, idx):
    """The `:ID:` in the property drawer of the headline at `lines[idx]`, or None. Lenient
    like orgparse: the drawer may follow body lines, as long as it is before the next
    headline."""
    j = idx + 1
    in_drawer = False
    while j < len(lines) and not _HEADLINE_RE.match(lines[j]):
        line = lines[j].strip()
        if line == ":PROPERTIES:":
            in_drawer = True
        elif line == ":END:":
            in_drawer = False
        elif in_drawer:
            m = re.match(r"^:ID:\s*(\S+)$", line)
            if m:
                return m.group(1)
        j += 1
    return None


def _find_drawer(lines, idx) -> tuple[int, int] | None:
    """(start, end) indexes of the first `:PROPERTIES:` ... `:END:` block between the headline
    at `lines[idx]` and the next headline, or None. Lenient like orgparse: the drawer may follow
    body text."""
    j = idx + 1
    while j < len(lines) and not _HEADLINE_RE.match(lines[j]):
        if lines[j].strip() == ":PROPERTIES:":
            end = j + 1
            while end < len(lines) and not _HEADLINE_RE.match(lines[end]):
                if lines[end].strip() == ":END:":
                    return j, end
                end += 1
            return None
        j += 1
    return None


def _is_same_headline(task, lines, idx) -> bool:
    m = _HEADLINE_RE.match(lines[idx].rstrip("\n"))
    if not m:
        return False
    want_id = (task.properties or {}).get("ID")
    if want_id:
        return _drawer_id(lines, idx) == want_id
    return len(m.group(1)) == task.level and bool(task.heading) and task.heading in m.group(3)


def _locate_headline(task, lines) -> int:
    """0-based index of `task`'s headline in the current `lines` (SPEC-0190).

    Trusts `task.line` only when the headline there is the same entry — same `:ID:` when the
    task has one, else same level and heading text. Otherwise (another session inserted,
    archived or rotated entries since `task` was parsed) it searches the file and returns the
    unique match, or raises a drift error. Never returns a different headline."""
    idx = task.line - 1
    if 0 <= idx < len(lines) and _is_same_headline(task, lines, idx):
        return idx
    hits = [i for i, line in enumerate(lines)
            if _HEADLINE_RE.match(line) and _is_same_headline(task, lines, i)]
    if len(hits) == 1:
        return hits[0]
    who = (task.properties or {}).get("ID") or repr(task.heading)
    why = "not found" if not hits else f"{len(hits)} matches"
    raise ValueError(f"drift: {who} is no longer at {task.file}:{task.line} ({why}) — the "
                     f"file changed since it was read; re-run")


def file_keywords(cfg, path):
    """(active, done) TODO keyword lists for a file: its own #+TODO if present, else the
    seeded org_todo_keywords fallback."""
    todos, dones = _split_keywords(cfg.get("org_todo_keywords") or ["TODO", "|", "DONE"])
    root = orgparse.load(path, env=OrgEnv(todos=todos, dones=dones, filename=path))
    return list(root.env.todo_keys), list(root.env.done_keys)


_IDEA_SELECTOR_RE = re.compile(r"^IDEA-(\d+)$", re.IGNORECASE)


def _normalize_selector(selector):
    """Normalize an IDEA-<n> selector (any case, unpadded) to the canonical IDEA-NNN form
    (zero-padded to 3) so `idea-7`/`IDEA-7` match a stored `IDEA-007`. Other selectors pass
    through unchanged."""
    m = _IDEA_SELECTOR_RE.match(selector)
    if not m:
        return selector
    return f"IDEA-{int(m.group(1)):03d}"


def resolve_selector(cfg, selector):
    """Resolve a selector to exactly one Task: an :ID:/file:line id, else an exact topic_key,
    else a case-insensitive heading substring. Raises on no/ambiguous match."""
    selector = _normalize_selector(selector)
    tasks = load_tasks(cfg)
    for pred in (lambda t: t.id == selector,
                 lambda t: t.topic_key == selector,
                 lambda t: selector.lower() in t.heading.lower()):
        matches = [t for t in tasks if pred(t)]
        if matches:
            break
    if not matches:
        raise ValueError(f"no task matched {selector!r}")
    if len(matches) > 1:
        cand = ", ".join(f"{t.id} ({t.state} {t.heading})" for t in matches[:6])
        raise ValueError(f"ambiguous selector {selector!r} → {len(matches)} tasks: {cand}")
    return matches[0]


def _inactive_stamp(cfg, *, when=None, with_time=False):
    """Org inactive timestamp `[YYYY-MM-DD Day]` or with time `[YYYY-MM-DD Day HH:MM]`.

    `when` may be a datetime (uses that instant) or None (now in cfg timezone).
    """
    now = when if isinstance(when, dt.datetime) else dt.datetime.now(cfg.get("_tz"))
    if with_time:
        return now.strftime("[%Y-%m-%d %a %H:%M]")
    return now.strftime("[%Y-%m-%d %a]")


def _closed_stamp(cfg):
    return _inactive_stamp(cfg, with_time=False)


# The inverse of `_inactive_stamp`: `[2026-07-27 Mon 14:03]`, `[2026-07-27 Mon]`, `[2026-07-27]`.
_INACTIVE_STAMP_RE = re.compile(
    r"\[(\d{4}-\d{2}-\d{2})(?:\s+[A-Za-z]{2,})?(?:\s+(\d{1,2}):(\d{2}))?\]")


def parse_inactive_stamp(text):
    """Read an inactive org stamp back into a naive datetime, or None (SPEC-0077).

    Deliberately tolerant: a missing, malformed or hand-mangled value reads as None so a listing
    command renders the idea as unstamped instead of raising."""
    m = _INACTIVE_STAMP_RE.search(str(text or ""))
    if not m:
        return None
    day, hh, mm = m.group(1), m.group(2), m.group(3)
    try:
        when = dt.datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return None
    if hh is not None:
        hour, minute = int(hh), int(mm)
        if hour > 23 or minute > 59:
            return None
        when = when.replace(hour=hour, minute=minute)
    return when


# Properties that record *when* an idea was captured / last deliberately changed (SPEC-0076).
# `:CREATED:` is written once at capture and never touched again; `:UPDATED:` is bumped by the
# content + lifecycle mutators. Neither is ever bumped by clock activity, archival, rotation,
# or the formatting-only normalizers — see the spec for why.
_STAMP_PROPS = ("CREATED", "UPDATED")


def _inject_property(lines, idx, stars, name, value):
    """Set `:NAME: value` in the `:PROPERTIES:` drawer of the headline at `lines[idx]`, mutating
    `lines` in place (creating the drawer after any PLANNING/CLOSED lines if absent).

    A pure list transform, so a caller that already holds the file's lines can add a property
    inside the write it is about to make instead of paying a second read-modify-write.
    Replacing an existing property does not shift any offsets; only creating one does, which is
    why stamping callers do it last (SPEC-0076)."""
    indent = " " * (len(stars) + 1)
    drawer = _find_drawer(lines, idx)
    if drawer:
        prop_re = re.compile(rf"^\s*:{re.escape(name)}:\s*.*$")
        start, end = drawer
        for k in range(start + 1, end):
            if prop_re.match(lines[k]):
                lines[k] = f"{indent}:{name}: {value}\n"
                return lines
        lines.insert(end, f"{indent}:{name}: {value}\n")
    else:
        nxt = idx + 1
        while nxt < len(lines) and _PLANNING_RE.match(lines[nxt]):
            nxt += 1
        lines[nxt:nxt] = [f"{indent}:PROPERTIES:\n", f"{indent}:{name}: {value}\n",
                          f"{indent}:END:\n"]
    return lines


def _remove_property(lines, idx, name) -> bool:
    """Delete a `:NAME:` line from the drawer of the headline at `lines[idx]`, in place.

    Clearing writes *no* property rather than an empty one (SPEC-0109): `ideas.org` is
    hand-edited, and a dangling `:PROJECT:` with no value is a wart a human has to wonder about.
    Returns True if a line was removed. Leaves an emptied drawer in place — dropping
    `:PROPERTIES:`/`:END:` would shift offsets for a caller mid-write.
    """
    drawer = _find_drawer(lines, idx)
    if not drawer:
        return False
    prop_re = re.compile(rf"^\s*:{re.escape(name)}:\s*.*$")
    start, end = drawer
    for k in range(start + 1, end):
        if prop_re.match(lines[k]):
            del lines[k]
            return True
    return False


@org_writer
def clear_property(cfg, task, name):
    """Remove `:NAME:` from `task`'s drawer, bumping `:UPDATED:` like `set_property` does."""
    lines = open(task.file).read().splitlines(keepends=True)
    idx = _locate_headline(task, lines)
    m = _HEADLINE_RE.match(lines[idx].rstrip("\n"))
    if not m:
        raise ValueError(f"line {task.line} in {task.file} is not a headline: {lines[idx]!r}")
    stars = m.group(1)
    _remove_property(lines, idx, name)
    if task.is_idea and name not in _STAMP_PROPS:
        stamp_updated(cfg, lines, idx, stars)
    _atomic_backup_write(cfg, task.file, "".join(lines))
    return task.file


def stamp_updated(cfg, lines, idx, stars, *, when=None):
    """Bump `:UPDATED:` on the headline at `lines[idx]` (SPEC-0076). Call immediately before
    writing, after every body edit — creating the property shifts the offsets below it."""
    return _inject_property(lines, idx, stars, "UPDATED",
                            _inactive_stamp(cfg, when=when, with_time=True))


# SPEC-0135: SHIPPED is the post-export (and outbound-done) terminal; archive like PROMOTED.
_ARCHIVE_ON_STATES = frozenset({"DROPPED", "RESEARCHED", "PROMOTED", "SHIPPED"})


@org_writer
def set_state(cfg, task, new_state, *, stamp_closed=True):
    """Set `task`'s TODO keyword to `new_state` in its file. Validates the state against the
    file's keyword set, verifies the on-disk headline still matches (drift guard), manages the
    CLOSED: stamp, backs up the original, and writes atomically. Returns the file path."""
    active, done = file_keywords(cfg, task.file)
    valid = set(active) | set(done)
    if new_state not in valid:
        raise ValueError(f"invalid state {new_state!r} for {task.file}; "
                         f"valid: {' '.join(active)} | {' '.join(done)}")

    lines = open(task.file).read().splitlines(keepends=True)
    idx = _locate_headline(task, lines)
    m = _HEADLINE_RE.match(lines[idx].rstrip("\n"))
    if not m:
        raise ValueError(f"line {task.line} in {task.file} is not a headline: {lines[idx]!r}")
    stars, gap, rest = m.group(1), m.group(2), m.group(3)

    # drift guard: the first token must be the state we parsed (or none, for a plain heading)
    first = rest.split(None, 1)[0] if rest.split() else ""
    if task.state and first != task.state:
        raise ValueError(f"drift: {task.file}:{task.line} expected state {task.state!r}, "
                         f"found {first!r} — re-run after a fresh parse")
    if task.state:                                   # replace the leading keyword
        remainder = rest[len(task.state):].lstrip()
    else:                                            # plain heading → insert a keyword
        remainder = rest
    newline = "\n" if lines[idx].endswith("\n") else ""
    lines[idx] = f"{stars}{gap}{new_state} {remainder}".rstrip() + newline

    # CLOSED stamp management (only the line immediately under the headline)
    into_done = new_state in done
    nxt = idx + 1
    has_closed = nxt < len(lines) and lines[nxt].lstrip().startswith("CLOSED:")
    if stamp_closed:
        closed_line = " " * (len(stars) + 1) + f"CLOSED: {_closed_stamp(cfg)}\n"
        if into_done and has_closed:
            lines[nxt] = closed_line
        elif into_done:
            lines.insert(nxt, closed_line)
        elif has_closed:                             # leaving done → drop the stamp
            del lines[nxt]

    if task.is_idea:                                 # SPEC-0076: a lifecycle move is an update
        stamp_updated(cfg, lines, idx, stars)

    _atomic_backup_write(cfg, task.file, "".join(lines))

    # SPEC-0073: auto-archive an idea the moment it becomes terminal via this path
    # (DROPPED/RESEARCHED via wt idea close, PROMOTED via generate, SHIPPED via pull_status /
    # reconcile / wt idea state — SPEC-0135). EXPORTED does not archive; becoming SHIPPED does.
    if task.is_idea and new_state in _ARCHIVE_ON_STATES:
        archive_idea(cfg, _fresh_idea(cfg, task))

    return task.file


def _fresh_idea(cfg, task):
    """`task` re-read after its own write. An `:ID:` several ideas carry (reused across rotated
    org files, SPEC-0267) is not a unique selector, so fall back to the idea's file + heading."""
    selector = task.properties.get("ID") or task.id
    try:
        return resolve_selector(cfg, selector)
    except ValueError:
        hits = [t for t in filter_tasks(load_tasks(cfg), is_idea=True)
                if t.id == selector and (t.file, t.heading) == (task.file, task.heading)]
        if len(hits) != 1:
            raise
        return hits[0]


_PLANNING_RE = re.compile(r"^\s*(SCHEDULED|DEADLINE|CLOSED):")


@org_writer
def set_property(cfg, task, name, value, *, touch=True):
    """Add/update a `:NAME: value` property in `task`'s `:PROPERTIES:` drawer (SPEC-0013),
    sibling to `set_state`: line-anchored + drift-guarded, backs up, writes atomically. Creates
    the drawer immediately after the headline (and any PLANNING/CLOSED lines already there) if
    it doesn't exist yet.

    SPEC-0076: on an idea this also bumps `:UPDATED:` in the same write. `touch=False` opts out
    for mechanical backfills (`reindex_ideas`), and the stamp properties never bump themselves."""
    lines = open(task.file).read().splitlines(keepends=True)
    idx = _locate_headline(task, lines)
    m = _HEADLINE_RE.match(lines[idx].rstrip("\n"))
    if not m:
        raise ValueError(f"line {task.line} in {task.file} is not a headline: {lines[idx]!r}")
    stars, gap, rest = m.group(1), m.group(2), m.group(3)

    # drift guard: same check as set_state's
    first = rest.split(None, 1)[0] if rest.split() else ""
    if task.state and first != task.state:
        raise ValueError(f"drift: {task.file}:{task.line} expected state {task.state!r}, "
                         f"found {first!r} — re-run after a fresh parse")

    _inject_property(lines, idx, stars, name, value)
    if touch and task.is_idea and name not in _STAMP_PROPS:
        stamp_updated(cfg, lines, idx, stars)

    _atomic_backup_write(cfg, task.file, "".join(lines))
    return task.file


# A CLOCK line: `CLOCK: [start]` (open/active) or `CLOCK: [start]--[end] => H:MM` (closed).
_CLOCK_LINE_RE = re.compile(
    r"^(\s*)CLOCK:\s*(\[[^\]]+\])(?:--(\[[^\]]+\])\s*=>\s*(\S+))?\s*$")
_CLOCK_STAMP_FMT = "[%Y-%m-%d %a %H:%M]"


def _format_duration(delta):
    """`datetime.timedelta` -> org's `H:MM` (unpadded hours), e.g. `1:30`, `12:05`."""
    total_min = round(delta.total_seconds() / 60)
    h, m = divmod(max(total_min, 0), 60)
    return f"{h}:{m:02d}"


def _find_logbook(lines, idx, stars):
    """Return (drawer_start, drawer_end, indent) for the headline at `idx`'s `:LOGBOOK:`
    drawer — `drawer_start`/`drawer_end` bracket the lines between `:LOGBOOK:` and `:END:`
    (exclusive of both marker lines); `None` if no drawer exists. Locates it after any
    PLANNING line and `:PROPERTIES:` drawer (org's conventional order)."""
    indent = " " * (len(stars) + 1)
    nxt = idx + 1
    while nxt < len(lines) and _PLANNING_RE.match(lines[nxt]):
        nxt += 1
    if nxt < len(lines) and lines[nxt].strip() == ":PROPERTIES:":
        nxt += 1
        while nxt < len(lines) and lines[nxt].strip() != ":END:":
            nxt += 1
        if nxt < len(lines):
            nxt += 1                                     # past :END:
    if nxt < len(lines) and lines[nxt].strip() == ":LOGBOOK:":
        end = nxt + 1
        while end < len(lines) and lines[end].strip() != ":END:":
            end += 1
        return nxt + 1, end, indent
    return None, nxt, indent


def _headline_and_drift(task, lines):
    """Shared drift-guard + headline parse for the clock primitives."""
    idx = _locate_headline(task, lines)
    m = _HEADLINE_RE.match(lines[idx].rstrip("\n"))
    if not m:
        raise ValueError(f"line {task.line} in {task.file} is not a headline: {lines[idx]!r}")
    stars, _gap, rest = m.group(1), m.group(2), m.group(3)
    first = rest.split(None, 1)[0] if rest.split() else ""
    if task.state and first != task.state:
        raise ValueError(f"drift: {task.file}:{task.line} expected state {task.state!r}, "
                         f"found {first!r} — re-run after a fresh parse")
    return idx, stars


@org_writer
def clock_in(cfg, selector, *, when=None):
    """Open a clock on `selector`'s idea: insert `CLOCK: [stamp]` into its `:LOGBOOK:` drawer
    (creating the drawer if absent), org-native so orgparse/Emacs read it back. Raises (no
    write) if a clock is already open."""
    task = resolve_selector(cfg, selector)
    lines = open(task.file).read().splitlines(keepends=True)
    idx, stars = _headline_and_drift(task, lines)
    start, end, indent = _find_logbook(lines, idx, stars)
    stamp = _inactive_stamp(cfg, when=when, with_time=True)

    if start is not None:                                # drawer exists — check for an open clock
        for i in range(start, end):
            m = _CLOCK_LINE_RE.match(lines[i])
            if m and m.group(3) is None:
                raise ValueError(f"{selector!r} is already clocked in (since {m.group(2)})")
        lines.insert(end, f"{indent}CLOCK: {stamp}\n")
    else:
        lines[end:end] = [f"{indent}:LOGBOOK:\n", f"{indent}CLOCK: {stamp}\n", f"{indent}:END:\n"]

    _atomic_backup_write(cfg, task.file, "".join(lines))
    return task.file


@org_writer
def clock_out(cfg, selector, *, when=None):
    """Close the open clock on `selector`'s idea: rewrite its open `CLOCK: [start]` line to
    `CLOCK: [start]--[end] => H:MM`. Raises (no write) if nothing is clocked in."""
    task = resolve_selector(cfg, selector)
    lines = open(task.file).read().splitlines(keepends=True)
    idx, stars = _headline_and_drift(task, lines)
    start, end, _indent = _find_logbook(lines, idx, stars)

    open_i = None
    open_m = None
    if start is not None:
        for i in range(start, end):
            m = _CLOCK_LINE_RE.match(lines[i])
            if m and m.group(3) is None:
                open_i, open_m = i, m
                break
    if open_i is None:
        raise ValueError(f"{selector!r} is not clocked in")

    now = when if isinstance(when, dt.datetime) else dt.datetime.now(cfg.get("_tz"))
    started = dt.datetime.strptime(open_m.group(2), _CLOCK_STAMP_FMT)
    end_stamp = _inactive_stamp(cfg, when=now, with_time=True)
    duration = _format_duration(now.replace(tzinfo=None) - started)
    nl = "\n" if lines[open_i].endswith("\n") else ""
    lines[open_i] = f"{open_m.group(1)}CLOCK: {open_m.group(2)}--{end_stamp} => {duration}{nl}"

    _atomic_backup_write(cfg, task.file, "".join(lines))
    return task.file


@org_writer
def add_clock_entry(cfg, selector, start, end):
    """Append a **closed** `CLOCK: [start]--[end] => H:MM` line to `selector`'s idea (SPEC-0063
    reconciliation — a historical, already-complete pair, not a live clock_in/clock_out
    session). Locates/creates `:LOGBOOK:` the same way `clock_in` does. Idempotent: a no-op if
    an identical formatted line already exists in the drawer. Returns (path, added: bool)."""
    task = resolve_selector(cfg, selector)
    lines = open(task.file).read().splitlines(keepends=True)
    idx, stars = _headline_and_drift(task, lines)
    drawer_start, end_idx, indent = _find_logbook(lines, idx, stars)

    start_stamp = _inactive_stamp(cfg, when=start, with_time=True)
    end_stamp = _inactive_stamp(cfg, when=end, with_time=True)
    duration = _format_duration(end.replace(tzinfo=None) - start.replace(tzinfo=None))
    new_line = f"{indent}CLOCK: {start_stamp}--{end_stamp} => {duration}\n"

    if drawer_start is not None:
        for i in range(drawer_start, end_idx):
            if lines[i] == new_line:
                return task.file, False                 # already reconciled
        lines.insert(end_idx, new_line)
    else:
        lines[end_idx:end_idx] = [f"{indent}:LOGBOOK:\n", new_line, f"{indent}:END:\n"]

    _atomic_backup_write(cfg, task.file, "".join(lines))
    return task.file, True


@org_writer
def ensure_idea_keyword(cfg, path, keyword, *, marker="INCUBATE"):
    """Ensure the ideas file's idea-state `#+TODO` line (identified by containing `marker`,
    e.g. INCUBATE) includes `keyword` in its done section (SPEC-0056). A file's own `#+TODO`
    wins over the env fallback, so a new terminal keyword must be present here for `set_state`
    / orgparse to accept it. Idempotent; only appends a token. Returns True if it wrote."""
    lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
    for i, ln in enumerate(lines):
        parts = ln.strip().split()
        if parts and parts[0] == "#+TODO:" and marker in parts:
            if keyword in parts:
                return False
            nl = "\n" if ln.endswith("\n") else ""
            lines[i] = ln.rstrip("\n").rstrip() + f" {keyword}" + nl
            _atomic_backup_write(cfg, path, "".join(lines))
            return True
    raise ValueError(f"no idea #+TODO line (containing {marker!r}) in {path}")


# ---- backup retention (SPEC-0271) -----------------------------------------------------------
# Every write copies the whole original to <data_dir>/org-backups/<basename>.<ts>.bak. Per
# basename we keep the newest `keep_last` plus the newest of each day in the last `keep_days`;
# the rest is pruned after each backup (that file only) or by `wt backups prune` (all files).

BACKUP_RE = re.compile(r"^(?P<base>.+)\.(?P<ts>\d{8}T\d{6})\.bak$")
_BACKUP_TS_FMT = "%Y%m%dT%H%M%S"
_RETENTION_DEFAULTS = {"keep_last": 50, "keep_days": 14}


def _backup_dir(cfg):
    return os.path.join(cfg["data_dir"], "org-backups")


def _retention(cfg):
    """(keep_last, keep_days) from cfg['org_backups']; a non-integer or negative value falls
    back to its default."""
    raw = cfg.get("org_backups")
    raw = raw if isinstance(raw, dict) else {}
    out = []
    for key, default in _RETENTION_DEFAULTS.items():
        v = raw.get(key, default)
        ok = isinstance(v, int) and not isinstance(v, bool) and v >= 0
        out.append(v if ok else default)
    return tuple(out)


def select_backup_prunes(names, *, keep_last, keep_days, today):
    """Pure: the backup names to delete. Grouped by basename; within a group keep the newest
    `keep_last` plus the newest of each date in [today - keep_days + 1, today]. A name that
    doesn't match BACKUP_RE (or has an unparseable timestamp) is never selected."""
    groups = {}
    for name in names:
        m = BACKUP_RE.match(name)
        if not m:
            continue
        try:
            ts = dt.datetime.strptime(m.group("ts"), _BACKUP_TS_FMT)
        except ValueError:
            continue
        groups.setdefault(m.group("base"), []).append((ts, name))
    # clamp so a huge keep_days ("keep dailies forever") means "since date.min", not OverflowError
    first_day = today - dt.timedelta(days=max(0, min(keep_days - 1, (today - dt.date.min).days)))
    prunes = []
    for entries in groups.values():
        entries.sort(reverse=True)                   # newest first
        keep = {name for _, name in entries[:keep_last]}
        seen_days = set()
        for ts, name in entries:
            day = ts.date()
            if keep_days > 0 and first_day <= day <= today and day not in seen_days:
                seen_days.add(day)
                keep.add(name)
        prunes.extend(name for _, name in entries if name not in keep)
    return sorted(prunes)


def prune_backups(cfg, *, basename=None, keep_last=None, keep_days=None, dry_run=False):
    """Apply retention to the backup dir (only `basename`'s backups when given). Explicit
    keep_last/keep_days override the config. Deletes are best-effort: a delete that raises
    OSError is skipped and listed under `failed`; it still counts as kept, and its bytes don't
    count (`bytes` is what was, or in a dry run would be, freed)."""
    d = _backup_dir(cfg)
    cfg_last, cfg_days = _retention(cfg)
    keep_last = cfg_last if keep_last is None else keep_last
    keep_days = cfg_days if keep_days is None else keep_days
    names = [n for n in (sorted(os.listdir(d)) if os.path.isdir(d) else []) if BACKUP_RE.match(n)]
    delete = select_backup_prunes(names, keep_last=keep_last, keep_days=keep_days,
                                  today=dt.date.today())
    if basename is not None:                         # selection is per basename; keep only ours
        mine = lambda n: BACKUP_RE.match(n).group("base") == basename  # noqa: E731
        names, delete = [n for n in names if mine(n)], [n for n in delete if mine(n)]
    size, failed = 0, []
    for name in delete:
        try:
            st_size = os.stat(os.path.join(d, name)).st_size
        except OSError:
            st_size = 0
        if not dry_run:
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                failed.append(name)
                continue
        size += st_size
    return {"dir": d, "scanned": len(names), "delete": delete,
            "kept": len(names) - len(delete) + len(failed), "bytes": size, "failed": failed}


def _atomic_backup_write(cfg, path, content):
    refuse_in_persona_run()
    if os.path.exists(path):                         # back up the original (new files: nothing to)
        ts = dt.datetime.now().strftime(_BACKUP_TS_FMT)
        backups = _backup_dir(cfg)
        os.makedirs(backups, exist_ok=True)
        shutil.copy2(path, os.path.join(backups, f"{os.path.basename(path)}.{ts}.bak"))
        try:                                         # best-effort: never fails the write
            prune_backups(cfg, basename=os.path.basename(path))
        except Exception:                            # any error, not just OSError (SPEC-0271)
            pass
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".wt-org-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _org_date(cfg, s):
    """'YYYY-MM-DD' (or 'now') -> org active-timestamp text '<YYYY-MM-DD Ddd>'."""
    return parse_date(s, cfg.get("_tz")).strftime("<%Y-%m-%d %a>")


def _warn_unknown_associations(cfg, *, project=None, epic=None):
    """Permissive validation (SPEC-0018): an unknown --project/--epic prints a warning +
    the known set to stderr, but never blocks capture. Lazy imports avoid a circular import
    (specs.py imports this module)."""
    if project:
        from .rules import registered_projects
        known = registered_projects(cfg)
        if project not in known:
            warn(f"  ! unknown project {project!r}; known projects: "
                 f"{', '.join(known) or '(none mapped yet)'}")
    if epic:
        from .specs import known_epics
        known = known_epics(cfg)
        if epic not in known:
            warn(f"  ! unknown epic {epic!r}; known epics: "
                 f"{', '.join(known) or '(none)'}")


# Capture kinds on ideas (SPEC-0044) — not org TODO keywords; not spec frontmatter `kind`.
IDEA_KINDS = ("idea", "bug", "improvement", "chore")
IDEA_KINDS_SET = frozenset(IDEA_KINDS)
DEFAULT_IDEA_KIND = "idea"


def normalize_idea_kind(value):
    """Validate/normalize a capture kind. Raises ValueError if invalid.

    Returns lowercase kind. Callers that omit kind at capture pass None and skip writing
    `:KIND:` — use `idea_kind(task)` on read for the resolved default.
    """
    if value is None:
        raise ValueError("kind is required")
    kind = str(value).strip().lower()
    if kind not in IDEA_KINDS_SET:
        raise ValueError(f"invalid kind {value!r}; expected one of: {', '.join(IDEA_KINDS)}")
    return kind


def configured_workstreams(cfg) -> list[str]:
    """Curated workstream lane names from config (SPEC-0090)."""
    raw = cfg.get("workstreams") or []
    out = []
    for item in raw:
        s = str(item).strip()
        if s and s not in out:
            out.append(s)
    return out


def normalize_workstream(cfg, value):
    """Validate a workstream write against cfg['workstreams']. Raises ValueError if invalid.

    Empty config list → every value fails (feature inert until configured).
    """
    if value is None:
        raise ValueError("workstream is required")
    ws = str(value).strip()
    if not ws:
        raise ValueError("workstream is empty")
    known = configured_workstreams(cfg)
    if ws not in known:
        listing = ", ".join(known) if known else "(none configured — set workstreams: in config)"
        raise ValueError(f"invalid workstream {value!r}; expected one of: {listing}")
    return ws


def idea_kind(task) -> str:
    """Resolved capture kind for an idea Task (missing/unknown → `idea`)."""
    raw = (task.properties.get("KIND") or "").strip().lower()
    return raw if raw in IDEA_KINDS_SET else DEFAULT_IDEA_KIND


@org_writer
def add_task(cfg, text, *, state=None, tags=(), priority=None, scheduled=None,
             deadline=None, file=None, project=None, epic=None, key=None, idea_id=None,
             kind=None, workstream=None):
    """Append a new level-1 task headline to the capture file (SPEC-0010). Returns
    (path, headline). State defaults to the target file's first active keyword and, if given,
    is validated against that file's keyword set. A JIRA key in `text` becomes the task's
    topic_key on read (no action needed here).

    `project`/`epic`/`key` (SPEC-0018) are entity associations: when any is given, the appended
    headline gets a `:PROPERTIES:` drawer with `:PROJECT:`/`:EPIC:`/`:TOPIC:` (re-parsed as
    `Task.project`/`Task.epic`/`Task.topic_key`). Validation is permissive: an unknown project
    (not in `rules.known_projects`) or epic (not in `specs.known_epics`) prints a warning
    listing the known set, then proceeds — it never blocks capture.

    `idea_id` (SPEC-0019, used by `add_idea`) is written as `:ID:`, FIRST in the drawer, ahead
    of `:PROJECT:`/`:EPIC:`/`:TOPIC:`.

    `kind` (SPEC-0044, ideas): when set, writes `:KIND:` after validation. Omitted → no
    `:KIND:` property (reads as default `idea`).

    `workstream` (SPEC-0090): when set, validates against cfg['workstreams'] and writes
    `:WORKSTREAM:`. Omitted → no property. Tasks have no post-capture mutator in v1."""
    path = os.path.expanduser(file or cfg["org_capture_file"])
    if os.path.exists(path):
        active, done = file_keywords(cfg, path)
    else:
        active, done = _split_keywords(cfg.get("org_todo_keywords") or ["TODO", "|", "DONE"])
    if not active:
        raise ValueError(f"{path} declares no active TODO keywords")
    if state is None:
        state = active[0]
    elif state not in set(active) | set(done):
        raise ValueError(f"invalid state {state!r} for {path}; "
                         f"valid: {' '.join(active)} | {' '.join(done)}")

    kind_value = normalize_idea_kind(kind) if kind is not None else None
    ws_value = normalize_workstream(cfg, workstream) if workstream is not None else None

    text = " ".join(str(text).split())
    if not text:
        raise ValueError("task text is empty")
    parts = ["*", state]
    if priority:
        parts.append(f"[#{priority}]")
    parts.append(text)
    if tags:
        parts.append(":" + ":".join(tags) + ":")
    headline = " ".join(parts)

    _warn_unknown_associations(cfg, project=project, epic=epic)

    new_lines = [headline]
    plan = []
    if scheduled:
        plan.append(f"SCHEDULED: {_org_date(cfg, scheduled)}")
    if deadline:
        plan.append(f"DEADLINE: {_org_date(cfg, deadline)}")
    if plan:
        new_lines.append("  " + " ".join(plan))
    props = []
    if idea_id:
        props.append(f"  :ID: {idea_id}")
        stamp = _inactive_stamp(cfg, with_time=True)   # SPEC-0076
        props.append(f"  :CREATED: {stamp}")
        props.append(f"  :UPDATED: {stamp}")
    if project:
        props.append(f"  :PROJECT: {project}")
    if epic:
        props.append(f"  :EPIC: {epic}")
    if key:
        props.append(f"  :TOPIC: {key}")
    if kind_value is not None:
        props.append(f"  :KIND: {kind_value}")
    if ws_value is not None:
        props.append(f"  :WORKSTREAM: {ws_value}")
    if props:
        new_lines.append("  :PROPERTIES:")
        new_lines.extend(props)
        new_lines.append("  :END:")

    existing = open(path).read() if os.path.exists(path) else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    content = existing + "\n".join(new_lines) + "\n"
    _atomic_backup_write(cfg, path, content)
    return path, headline


@org_writer
def generate_tasks(cfg, spec, items, *, file=None, key=None):
    """Generate org tasks from a spec (SPEC-0014): spec is {"id": "SPEC-NNNN", "title": ...}.
    Ensures a parent heading `* <id> <title>  :spec:` exists (created once, idempotent by
    scanning for a level-1 heading carrying a `:SPEC:<id>:` property), then appends a `TODO`
    child per item not already present under that parent (matched on heading text), each
    carrying `:SPEC:<id>:` (and `:TOPIC:<key>:` when `key` is given, so `topic_key` drives the
    `wt digest` join). Reuses the backup + atomic write path; never touches unrelated headings.
    Returns (path, added_count)."""
    path = os.path.expanduser(file or cfg["org_capture_file"])
    spec_id, title = spec["id"], spec["title"]

    if os.path.exists(path):
        active, done = file_keywords(cfg, path)
    else:
        active, done = _split_keywords(cfg.get("org_todo_keywords") or ["TODO", "|", "DONE"])
    if not active:
        raise ValueError(f"{path} declares no active TODO keywords")
    child_state = active[0]

    def _child_lines(text):
        text = " ".join(str(text).split())
        props = f"   :SPEC: {spec_id}\n"
        if key:
            props += f"   :TOPIC: {key}\n"
        return [f"** {child_state} {text}\n", "   :PROPERTIES:\n", props, "   :END:\n"]

    real_path = os.path.realpath(path)
    file_tasks = [t for t in load_tasks(cfg) if os.path.realpath(t.file) == real_path] \
        if os.path.exists(path) else []
    parent = next((t for t in file_tasks
                   if t.level == 1 and t.properties.get("SPEC") == spec_id), None)

    lines = open(path).read().splitlines(keepends=True) if os.path.exists(path) else []

    if parent is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines:
            lines.append("\n")
        lines.append(f"* {spec_id} {title}  :spec:\n")
        lines.append("  :PROPERTIES:\n")
        lines.append(f"  :SPEC: {spec_id}\n")
        lines.append("  :END:\n")
        for item in items:
            lines.extend(_child_lines(item))
        added = len(items)
    else:
        existing = {t.heading.strip() for t in file_tasks
                    if t.level == 2 and t.properties.get("SPEC") == spec_id}
        new_items = [i for i in items if i.strip() not in existing]
        n = len(lines)
        i = parent.line                              # 0-based index right after the headline
        while i < n:
            m = _HEADLINE_RE.match(lines[i].rstrip("\n"))
            if m and len(m.group(1)) <= parent.level:
                break
            i += 1
        insert = []
        for item in new_items:
            insert.extend(_child_lines(item))
        lines[i:i] = insert
        added = len(new_items)

    _atomic_backup_write(cfg, path, "".join(lines))
    return path, added


_IDEA_ID_RE = re.compile(r"^IDEA-(\d+)$")


def next_idea_number(cfg):
    """The next free IDEA-NNN number (SPEC-0019): 1 + the max existing `:ID:` matching
    `IDEA-(\\d+)` across all ideas (or 1 if none have one yet). Never collides with an
    existing id, even across gaps."""
    n = 0
    for t in filter_tasks(load_tasks(cfg), is_idea=True):
        m = _IDEA_ID_RE.match(t.properties.get("ID") or "")
        if m:
            n = max(n, int(m.group(1)))
    return n + 1


def ideas_archive_path(cfg):
    """Where `archive_idea` moves terminal ideas (SPEC-0073): `org_ideas_archive_file`, else
    `ideas-archive.org` next to `org_ideas_file` — the default since SPEC-0168."""
    configured = cfg.get("org_ideas_archive_file")
    if configured:
        return os.path.expanduser(configured)
    ideas_dir = os.path.dirname(os.path.expanduser(cfg["org_ideas_file"]))
    return os.path.join(ideas_dir, "ideas-archive.org")


@org_writer
def archive_idea(cfg, task):
    """Move `task`'s whole subtree — headline through the line before the next top-level
    headline (or EOF), i.e. properties, Summary, Open questions, Log, everything nested under
    it — out of its source file and append it verbatim to `cfg['org_ideas_archive_file']`
    (SPEC-0072). Line-anchored + drift-guarded + atomic on both ends, same rigor as
    `set_property`/`set_state`. Raises if `task` isn't an idea, isn't a top-level headline, or
    has drifted on disk since it was parsed. Returns the archive file path."""
    if not task.is_idea:
        raise ValueError(f"{task.id} is not an idea")

    lines = open(task.file).read().splitlines(keepends=True)
    idx = _locate_headline(task, lines)
    m = _HEADLINE_RE.match(lines[idx].rstrip("\n"))
    if not m:
        raise ValueError(f"line {task.line} in {task.file} is not a headline: {lines[idx]!r}")
    stars, _gap, rest = m.group(1), m.group(2), m.group(3)
    if len(stars) != 1:
        raise ValueError(f"{task.file}:{task.line}: expected a top-level idea headline")

    first = rest.split(None, 1)[0] if rest.split() else ""
    if task.state and first != task.state:
        raise ValueError(f"drift: {task.file}:{task.line} expected state {task.state!r}, "
                         f"found {first!r} — re-run after a fresh parse")

    end = idx + 1
    while end < len(lines) and not re.match(r"^\*\s", lines[end]):
        end += 1
    subtree = lines[idx:end]

    _atomic_backup_write(cfg, task.file, "".join(lines[:idx] + lines[end:]))

    archive_path = ideas_archive_path(cfg)
    if os.path.exists(archive_path):
        existing = open(archive_path).read()
        if existing and not existing.endswith("\n"):
            existing += "\n"
    else:
        keywords = cfg.get("org_idea_keywords") or [
            "IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "SHIPPED", "DROPPED"]
        existing = f"#+TODO: {' '.join(keywords)}\n\n"
    _atomic_backup_write(cfg, archive_path, existing + "".join(subtree))
    return archive_path


@org_writer
def maybe_rotate_ideas_file(cfg):
    """Roll `cfg['org_ideas_file']` over to a dated historical file once it crosses
    `cfg['idea_rotation_max_lines']` (SPEC-0071). The renamed file keeps living under the same
    `org_files` root, so `load_tasks`'s recursive glob picks it up automatically — the
    configured path itself never changes; a fresh file with just the `#+TODO` header is written
    back at it. No-op if the file doesn't exist yet or is under threshold. Returns the
    rotated-to path, or None if no rotation happened."""
    path = os.path.expanduser(cfg["org_ideas_file"])
    if not os.path.exists(path):
        return None
    max_lines = cfg.get("idea_rotation_max_lines", 1200)
    with open(path) as f:
        n_lines = sum(1 for _ in f)
    if n_lines <= max_lines:
        return None

    today = dt.datetime.now(cfg.get("_tz")).strftime("%Y%m%d")
    d = os.path.dirname(path)
    base = os.path.splitext(os.path.basename(path))[0]
    dest = os.path.join(d, f"{base}-{today}.org")
    suffix = 1
    while os.path.exists(dest):
        suffix += 1
        dest = os.path.join(d, f"{base}-{today}-{suffix}.org")
    os.replace(path, dest)

    keywords = cfg.get("org_idea_keywords") or [
        "IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "SHIPPED", "DROPPED"]
    with open(path, "w") as f:
        f.write(f"#+TODO: {' '.join(keywords)}\n\n")
    return dest


@org_writer
def add_idea(cfg, text, *, state=None, tags=(), priority=None, scheduled=None, deadline=None,
             project=None, epic=None, key=None, kind=None, workstream=None):
    """Capture an idea (SPEC-0012): thin wrapper over add_task targeting cfg['org_ideas_file'],
    defaulting to state IDEA. If the ideas file doesn't exist yet, it's created with the idea
    `#+TODO` header first (so `file_keywords`/`add_task` resolve IDEA as a valid state).
    Rotates the ideas file first if it's crossed the configured threshold (SPEC-0071) — a
    no-op in the common case.

    `project`/`epic`/`key` (SPEC-0018) pass straight through to `add_task`.
    `kind` (SPEC-0044) likewise — only written when explicitly passed.
    `workstream` (SPEC-0090) likewise — validated against cfg['workstreams'].

    SPEC-0019: assigns a stable `:ID: IDEA-NNN` (sequential, never colliding with an existing
    one) as the FIRST property in the drawer. Returns (path, headline, id)."""
    maybe_rotate_ideas_file(cfg)
    path = os.path.expanduser(cfg["org_ideas_file"])
    if not os.path.exists(path):
        keywords = cfg.get("org_idea_keywords") or [
            "IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "SHIPPED", "DROPPED"]

        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w") as f:
            f.write(f"#+TODO: {' '.join(keywords)}\n\n")
    idea_id = f"IDEA-{next_idea_number(cfg):03d}"
    path, headline = add_task(cfg, text, state=state or "IDEA", tags=tags, priority=priority,
                              scheduled=scheduled, deadline=deadline, file=path,
                              project=project, epic=epic, key=key, idea_id=idea_id,
                              kind=kind, workstream=workstream)
    return path, headline, idea_id


def set_idea_kind(cfg, selector, kind):
    """Set `:KIND:` on an idea (SPEC-0044). Always writes the property (including `idea`)."""
    task = resolve_selector(cfg, selector)
    if not task.is_idea:
        raise ValueError(f"{selector!r} is not an idea")
    return set_property(cfg, task, "KIND", normalize_idea_kind(kind))


def set_idea_project(cfg, selector, project):
    """Set/clear `:PROJECT:` on an existing idea (SPEC-0109).

    Fills a library gap, not just a TUI one: before this, `--project` existed **only** at
    capture, so an idea captured without a project could never be given one from anywhere.

    Validation stays **permissive** per SPEC-0018 — an unknown project warns on stderr and is
    still written. Adding a chooser must not quietly turn a deliberately permissive association
    into a gate. `project=None`/`""` clears the property.
    """
    task = resolve_selector(cfg, selector)
    if not task.is_idea:
        raise ValueError(f"{selector!r} is not an idea")
    value = (project or "").strip()
    if not value:
        return clear_property(cfg, task, "PROJECT")
    _warn_unknown_associations(cfg, project=value)
    return set_property(cfg, task, "PROJECT", value)


def set_idea_workstream(cfg, selector, workstream):
    """Set `:WORKSTREAM:` on an idea (SPEC-0090). Validates against cfg['workstreams']."""
    task = resolve_selector(cfg, selector)
    if not task.is_idea:
        raise ValueError(f"{selector!r} is not an idea")
    return set_property(cfg, task, "WORKSTREAM", normalize_workstream(cfg, workstream))


_PRIO_COOKIE_RE = re.compile(r"^\[#([ABC])\]\s+")


@org_writer
def set_idea_priority(cfg, selector, priority):
    """Set/replace the org `[#A|B|C]` cookie on an idea headline (SPEC-0093)."""
    pri = str(priority).strip().upper()
    if pri not in ("A", "B", "C"):
        raise ValueError(f"invalid priority {priority!r}; expected A, B, or C")
    task = resolve_selector(cfg, selector)
    if not task.is_idea:
        raise ValueError(f"{selector!r} is not an idea")
    lines = open(task.file).read().splitlines(keepends=True)
    idx = _locate_headline(task, lines)
    m = _HEADLINE_RE.match(lines[idx].rstrip("\n"))
    if not m:
        raise ValueError(f"line {task.line} in {task.file} is not a headline: {lines[idx]!r}")
    stars, gap, rest = m.group(1), m.group(2), m.group(3)
    first = rest.split(None, 1)[0] if rest.split() else ""
    if task.state and first != task.state:
        raise ValueError(f"drift: {task.file}:{task.line} expected state {task.state!r}, "
                         f"found {first!r} — re-run after a fresh parse")
    if task.state:
        remainder = rest[len(task.state):].lstrip()
        prefix = f"{task.state} "
    else:
        remainder = rest
        prefix = ""
    remainder = _PRIO_COOKIE_RE.sub("", remainder)
    newline = "\n" if lines[idx].endswith("\n") else ""
    lines[idx] = f"{stars}{gap}{prefix}[#{pri}] {remainder}".rstrip() + newline
    stamp_updated(cfg, lines, idx, stars)
    _atomic_backup_write(cfg, task.file, "".join(lines))
    return task.file


# Org tags allow letters, digits, `_`, `@` and `-` (same set `wt.org._RE_HEADING_TAGS` parses).
_TAG_RE = re.compile(r"^[\w@-]+$")
_TAG_BLOCK_RE = re.compile(r"\s*:([\w@:-]+):\s*$")


@org_writer
def set_idea_tags(cfg, selector, tags):
    """Replace the trailing `:tag:tag:` block on an idea headline (SPEC-0101).

    `tags` is an iterable of bare names (no colons) or None/empty to strip the block. Only the
    headline's tail is rewritten, so the state keyword and the `[#A]` cookie are preserved by
    construction rather than by re-parsing them. Invalid tag → ValueError with no write.
    """
    names = [str(t).strip().strip(":") for t in (tags or [])]
    names = [n for n in names if n]
    for n in names:
        if not _TAG_RE.match(n):
            raise ValueError(f"invalid tag {n!r}; use letters, digits, '_', '@' or '-'")
    seen, ordered = set(), []
    for n in names:                                  # de-dup, first occurrence wins
        if n not in seen:
            seen.add(n)
            ordered.append(n)

    task = resolve_selector(cfg, selector)
    if not task.is_idea:
        raise ValueError(f"{selector!r} is not an idea")
    lines = open(task.file).read().splitlines(keepends=True)
    idx = _locate_headline(task, lines)
    m = _HEADLINE_RE.match(lines[idx].rstrip("\n"))
    if not m:
        raise ValueError(f"line {task.line} in {task.file} is not a headline: {lines[idx]!r}")
    stars, gap, rest = m.group(1), m.group(2), m.group(3)
    first = rest.split(None, 1)[0] if rest.split() else ""
    if task.state and first != task.state:
        raise ValueError(f"drift: {task.file}:{task.line} expected state {task.state!r}, "
                         f"found {first!r} — re-run after a fresh parse")
    body = _TAG_BLOCK_RE.sub("", rest).rstrip()
    tail = f" :{':'.join(ordered)}:" if ordered else ""
    newline = "\n" if lines[idx].endswith("\n") else ""
    lines[idx] = f"{stars}{gap}{body}{tail}".rstrip() + newline
    stamp_updated(cfg, lines, idx, stars)                              # SPEC-0076
    _atomic_backup_write(cfg, task.file, "".join(lines))
    return task.file


def reindex_ideas(cfg):
    """Backfill `:ID: IDEA-NNN` onto id-less ideas (SPEC-0019), in file order, via
    `set_property` (reusing its backup + atomic write path). Ideas that already carry a valid
    `IDEA-NNN` id are left untouched, so this is idempotent — a second run assigns nothing.
    Returns the list of (id, heading) assigned.

    Re-parses after every assignment: `set_property` may insert a `:PROPERTIES:` drawer,
    shifting the line numbers of every task below it in the same file, so cached `Task.line`
    values from before an assignment can't be reused for the next one."""
    n = next_idea_number(cfg)
    assigned = []
    while True:
        ideas = sorted(filter_tasks(load_tasks(cfg), is_idea=True),
                       key=lambda t: (t.file, t.line))
        target = next((t for t in ideas
                       if not _IDEA_ID_RE.match(t.properties.get("ID") or "")), None)
        if target is None:
            break
        idea_id = f"IDEA-{n:03d}"
        # touch=False: assigning a missing id is bookkeeping, not an edit to the idea (SPEC-0076)
        set_property(cfg, target, "ID", idea_id, touch=False)
        assigned.append((idea_id, target.heading))
        n += 1
    return assigned


def set_state_by_selector(cfg, selector, new_state, *, stamp_closed=True):
    return set_state(cfg, resolve_selector(cfg, selector), new_state, stamp_closed=stamp_closed)


def mark_done(cfg, selector, *, stamp_closed=True):
    """Set the resolved task to its file's first done keyword."""
    task = resolve_selector(cfg, selector)
    _, done = file_keywords(cfg, task.file)
    if not done:
        raise ValueError(f"{task.file} declares no done keywords")
    return set_state(cfg, task, done[0], stamp_closed=stamp_closed)
