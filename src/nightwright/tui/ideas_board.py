"""View model for the ideas board (SPEC-0490): project rail, Open / In flight / Built buckets.

No Textual import, same rule as `model.py`: every bucketing, join and count decision lives here
so it is testable on a base install, and `app.py` only renders it.

The board answers three questions the old flat table could not: *what is still open*, *what
became a spec and how far along that spec really is*, and *which project it belongs to*. Spec
status is read from the specs themselves (internal `docs/specs` frontmatter — the ledger is
generated from exactly that — and the outbox's portable frontmatter, which `nw spec pull-status`
keeps mirrored from the target repo), never guessed from the idea's own state.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

ALL = "\x00all"
NO_PROJECT = "\x00none"
NO_PROJECT_LABEL = "(no project)"

OPEN, IN_FLIGHT, BUILT, DROPPED = "open", "in-flight", "built", "dropped"
BUCKET_ORDER = (OPEN, IN_FLIGHT, BUILT, DROPPED)
BUCKET_LABELS = {OPEN: "OPEN", IN_FLIGHT: "IN FLIGHT", BUILT: "BUILT", DROPPED: "DROPPED"}

# Idea states that claim "a spec exists" even when no `:SPEC:` is linked (an anomaly the board
# surfaces as in flight with "no spec link" rather than hiding it under OPEN).
_SPEC_CLAIMING_STATES = frozenset({"SPECCED", "PROMOTED", "EXPORTED"})
# In-flight order: the spec furthest along first, so live work sits at the top of the bucket.
_STATUS_RANK = {"in-progress": 0, "accepted": 1, "proposed": 2, "draft": 3}
_PRIORITY_RANK = {"A": 0, "B": 1, "C": 2}
_MAX_HOPS = 8                     # supersede-chain bound; a cycle can never loop forever
ACTIVITY_LIMIT = 60
MOVEMENT_DAYS = 7


# --- specs --------------------------------------------------------------------------------

@dataclass(frozen=True)
class SpecInfo:
    """The frontmatter facts the board needs about one spec, internal or outbound."""
    id: str
    status: str
    kind: str = "feature"
    title: str = ""
    parent: str = ""
    created: dt.date | None = None
    updated: dt.date | None = None
    superseded_by: str = ""
    source_idea: str = ""
    outbound: bool = False
    exported: bool = False


def _as_date(value) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def _s(value) -> str:
    return str(value).strip() if value is not None else ""


def _info(fm: dict, *, outbound: bool, exported: bool = False) -> SpecInfo:
    return SpecInfo(
        id=_s(fm.get("id")), status=_s(fm.get("status")).lower(),
        kind=_s(fm.get("kind")) or "feature", title=_s(fm.get("title")),
        parent=_s(fm.get("parent")), created=_as_date(fm.get("created")),
        updated=_as_date(fm.get("updated") or fm.get("created")),
        superseded_by=_s(fm.get("superseded_by")), source_idea=_s(fm.get("source_idea")),
        outbound=outbound, exported=exported)


def load_spec_index(cfg) -> dict[str, SpecInfo]:
    """`{spec id: SpecInfo}` over internal specs and the outbox, each file read once.

    Same sources as `hub_payload` (`_specs_dir` glob + `_outbound_specs`), so the board and
    `nw hub` cannot disagree about a status. A missing specs dir or outbox reads as empty.
    """
    from ..specmeta import split_frontmatter
    from ..specs import _outbound_specs, _specs_dir, outbox_dir

    out: dict[str, SpecInfo] = {}
    try:
        specs_dir = _specs_dir(cfg)
    except ValueError:
        specs_dir = None
    if specs_dir is not None and specs_dir.exists():
        for path in sorted(specs_dir.glob("[0-9]*.md")):
            try:
                fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            info = _info(fm or {}, outbound=False)
            if info.id:
                out[info.id] = info
    try:
        outbound = _outbound_specs(cfg)
        root = outbox_dir(cfg)
        provenance = "\n".join(p.read_text(encoding="utf-8")
                               for p in root.glob("*/PROVENANCE.md")) if root.exists() else ""
    except (OSError, ValueError, KeyError):
        outbound, provenance = [], ""
    for s in outbound:
        fm = s.get("fm") or {}
        sid = _s(fm.get("id"))
        if sid:
            out[sid] = _info(fm, outbound=True, exported=sid in provenance)
    return out


@dataclass(frozen=True)
class SpecLink:
    """An idea's `:SPEC:` resolved to the status that actually governs it.

    `status` is the frontmatter status, after following a `superseded_by` chain to the live
    spec (`via` names it), or one of the synthetic `missing` (no such file) / `foreign` (a bare
    `SPEC-NNNN` on another project's idea that this repo's spec does not claim — SPEC-0327).
    """
    id: str
    status: str
    via: str = ""
    info: SpecInfo | None = None        # the spec whose status governs (successor if followed)
    origin: SpecInfo | None = None      # the spec the idea names

    @property
    def label(self) -> str:
        sid = f"{self.id}→{self.via}" if self.via else self.id
        status = self.status or "?"
        if self.info is not None and self.info.outbound and self.info.exported \
                and status not in ("done",):
            status += " · exported"
        return f"{sid} {status}"


def resolve_link(spec_id: str, specs: dict[str, SpecInfo], *, idea_id: str = "",
                 internal: bool = True) -> SpecLink | None:
    spec_id = (spec_id or "").strip()
    if not spec_id:
        return None
    origin = specs.get(spec_id)
    bare = spec_id.upper().startswith("SPEC-")
    if bare and not internal and (origin is None or origin.source_idea != idea_id):
        return SpecLink(spec_id, "foreign")
    if origin is None:
        return SpecLink(spec_id, "missing")
    cur, hops = origin, 0
    while cur.status == "superseded" and cur.superseded_by and hops < _MAX_HOPS:
        nxt = specs.get(cur.superseded_by)
        if nxt is None:
            break
        cur, hops = nxt, hops + 1
    return SpecLink(spec_id, cur.status or "?", via=cur.id if cur.id != spec_id else "",
                    info=cur, origin=origin)


def classify(state: str, link: SpecLink | None) -> str:
    """Which bucket an idea belongs in (SPEC-0490 bucketing rules, in order)."""
    state = (state or "").upper()
    if state == "DROPPED":
        return DROPPED
    if state == "SHIPPED":
        return BUILT
    if link is not None:
        if link.status == "done":
            return BUILT
        if link.status == "rejected":
            return DROPPED
        return IN_FLIGHT
    if state in _SPEC_CLAIMING_STATES:
        return IN_FLIGHT
    return OPEN


# --- board --------------------------------------------------------------------------------

@dataclass
class BoardItem:
    row: object                     # model.DeskRow
    task: object
    bucket: str
    project_key: str
    link: SpecLink | None = None
    created: dt.datetime | None = None
    updated: dt.datetime | None = None
    specced_at: dt.datetime | None = None
    built_at: dt.datetime | None = None
    rollup: tuple[int, int] | None = None     # (built, total) for an epic idea

    @property
    def id(self) -> str:
        return self.row.id

    @property
    def spec_label(self) -> str:
        if self.link is not None:
            return self.link.label
        if (self.row.state or "").upper() in _SPEC_CLAIMING_STATES:
            return "no spec link"
        return ""

    @property
    def rollup_label(self) -> str:
        return f"{self.rollup[0]}/{self.rollup[1]} built" if self.rollup else ""


@dataclass
class RailEntry:
    key: str
    label: str
    open: int = 0
    in_flight: int = 0
    built: int = 0
    dropped: int = 0

    @property
    def counts(self) -> str:
        return f"{self.open}·{self.in_flight}"


@dataclass
class ScoreRow:
    key: str
    label: str
    open: int = 0
    in_flight: int = 0
    built: int = 0
    new_recent: int = 0
    built_recent: int = 0

    @property
    def movement(self) -> str:
        parts = []
        if self.new_recent:
            parts.append(f"+{self.new_recent} idea{'' if self.new_recent == 1 else 's'}")
        if self.built_recent:
            parts.append(f"{self.built_recent} built")
        return " · ".join(parts) or "—"


@dataclass
class Activity:
    when: dt.datetime
    kind: str                       # captured | specced | built | updated
    item: BoardItem

    @property
    def text(self) -> str:
        if self.kind == "specced" and self.item.link is not None:
            return f"became {self.item.link.id}"
        if self.kind == "built":
            return "built" + (f" ({self.item.link.id})" if self.item.link else "")
        if self.kind == "updated":
            return "updated"
        return "captured"


@dataclass
class Board:
    items: list[BoardItem] = field(default_factory=list)
    rail: list[RailEntry] = field(default_factory=list)
    scoreboard: list[ScoreRow] = field(default_factory=list)
    activity: list[Activity] = field(default_factory=list)
    _buckets: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self.by_id = {i.id: i for i in self.items}

    def buckets(self, key: str, *, show_dropped: bool = False) -> list[tuple[str, list]]:
        """`[(bucket, items)]` for one rail entry, in display order; DROPPED only on demand."""
        per = self._buckets.get(key, {})
        names = BUCKET_ORDER if show_dropped else BUCKET_ORDER[:3]
        return [(b, per.get(b, [])) for b in names]

    def entry(self, key: str) -> RailEntry | None:
        return next((e for e in self.rail if e.key == key), None)


def project_key(project: str | None) -> str:
    return (project or "").strip() or NO_PROJECT


def project_label(key: str) -> str:
    if key == ALL:
        return "All"
    return NO_PROJECT_LABEL if key == NO_PROJECT else key


def _at(value) -> dt.datetime | None:
    """A date or naive datetime as a naive datetime (a date sits at its midnight)."""
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    return None


def _on_day(day, stamp: dt.datetime | None) -> dt.datetime | None:
    """A date-only spec/closed day as a datetime, borrowing the idea's own `:UPDATED:` clock
    time when it falls on that same day (the spec write usually *is* that update), so the
    activity feed orders same-day events by when they happened rather than all at midnight."""
    when = _at(day)
    if when is None:
        return None
    if stamp is not None and stamp.date() == when.date():
        return stamp
    return when


def _ts(when: dt.datetime | None) -> float:
    return when.timestamp() if when is not None else 0.0


def _sort_key(item: BoardItem):
    if item.bucket == OPEN:
        return (0 if item.row.open_questions else 1,
                _PRIORITY_RANK.get(item.row.priority or "", 3), -_ts(item.updated))
    if item.bucket == IN_FLIGHT:
        status = item.link.status if item.link else ""
        return (_STATUS_RANK.get(status, 4), -_ts(item.updated))
    if item.bucket == BUILT:
        return (-_ts(item.built_at or item.updated),)
    return (-_ts(item.updated),)


def build_board(pairs, specs: dict[str, SpecInfo], *, now: dt.datetime) -> Board:
    """Bucket every `(DeskRow, Task)` pair and derive rail, scoreboard and activity feed.

    Pure: no org or spec reads (those happen in `load_board`), so the rules are unit-testable.
    `task` only needs `.properties` (CREATED / UPDATED) and `.closed`.
    """
    from ..org_write import parse_inactive_stamp
    from ..rules import INTERNAL_PROJECT

    now = now.replace(tzinfo=None)
    items: list[BoardItem] = []
    for row, task in pairs:
        props = getattr(task, "properties", None) or {}
        internal = (row.project or INTERNAL_PROJECT) == INTERNAL_PROJECT
        link = resolve_link(props.get("SPEC") or "", specs, idea_id=row.id, internal=internal)
        bucket = classify(row.state, link)
        created = parse_inactive_stamp(props.get("CREATED"))
        updated = parse_inactive_stamp(props.get("UPDATED")) or created
        item = BoardItem(row=row, task=task, bucket=bucket, project_key=project_key(row.project),
                         link=link, created=created, updated=updated)
        if link is not None and link.origin is not None:
            item.specced_at = _on_day(link.origin.created, updated)
        if bucket == BUILT:
            done_spec = link.info if link is not None and link.status == "done" else None
            item.built_at = (_on_day(done_spec.updated or done_spec.created, updated)
                             if done_spec else None) \
                or _on_day(getattr(task, "closed", None), updated) or updated
        items.append(item)

    # Epic roll-up: child specs by `parent`, else child ideas by `:EPIC:`.
    children: dict[str, list[SpecInfo]] = {}
    for s in specs.values():
        if s.parent:
            children.setdefault(s.parent, []).append(s)
    child_ideas: dict[str, list[BoardItem]] = {}
    for item in items:
        if item.row.epic:
            child_ideas.setdefault(item.row.epic, []).append(item)
    for item in items:
        epic = item.link.info if item.link is not None else None
        if epic is None or epic.kind != "epic":
            continue
        kids = [c for c in children.get(epic.id, []) if c.status not in ("superseded", "rejected")]
        if kids:
            item.rollup = (sum(1 for c in kids if c.status == "done"), len(kids))
            continue
        ideas = [c for c in child_ideas.get(epic.id, []) if c is not item and c.bucket != DROPPED]
        if ideas:
            item.rollup = (sum(1 for c in ideas if c.bucket == BUILT), len(ideas))

    # Buckets per rail key (ALL included), each sorted once.
    per: dict[str, dict[str, list]] = {ALL: {}}
    for item in items:
        for key in (ALL, item.project_key):
            per.setdefault(key, {}).setdefault(item.bucket, []).append(item)
    for buckets in per.values():
        for lst in buckets.values():
            lst.sort(key=_sort_key)

    def counts(key):
        b = per.get(key, {})
        return {name: len(b.get(name, [])) for name in BUCKET_ORDER}

    projects = sorted((k for k in per if k not in (ALL, NO_PROJECT)), key=str.casefold)
    if NO_PROJECT in per:
        projects.append(NO_PROJECT)
    rail = []
    for key in [ALL] + projects:
        c = counts(key)
        rail.append(RailEntry(key, project_label(key), c[OPEN], c[IN_FLIGHT], c[BUILT],
                              c[DROPPED]))

    cutoff = (now - dt.timedelta(days=MOVEMENT_DAYS)).date()
    recent_new: dict[str, int] = {}
    recent_built: dict[str, int] = {}
    activity: list[Activity] = []
    for item in items:
        if item.created is not None:
            activity.append(Activity(item.created, "captured", item))
            if item.created.date() >= cutoff:
                recent_new[item.project_key] = recent_new.get(item.project_key, 0) + 1
        elif item.updated is not None:
            # No `:CREATED:` (never backfilled, SPEC-0076): show the last touch instead of
            # leaving the idea out of the feed — labelled as such, never as a capture.
            activity.append(Activity(item.updated, "updated", item))
        if item.specced_at is not None:
            activity.append(Activity(item.specced_at, "specced", item))
        if item.bucket == BUILT and item.built_at is not None:
            activity.append(Activity(item.built_at, "built", item))
            if item.built_at.date() >= cutoff:
                recent_built[item.project_key] = recent_built.get(item.project_key, 0) + 1
    # Newest first; on the same instant a later lifecycle step reads first.
    step = {"built": 0, "specced": 1, "captured": 2, "updated": 3}
    activity.sort(key=lambda a: (-_ts(a.when), step[a.kind]))

    scoreboard = []
    for entry in rail[1:]:
        scoreboard.append(ScoreRow(entry.key, entry.label, entry.open, entry.in_flight,
                                   entry.built, recent_new.get(entry.key, 0),
                                   recent_built.get(entry.key, 0)))
    total = rail[0]
    scoreboard.append(ScoreRow(ALL, "total", total.open, total.in_flight, total.built,
                               sum(recent_new.values()), sum(recent_built.values())))

    return Board(items=items, rail=rail, scoreboard=scoreboard,
                 activity=activity[:ACTIVITY_LIMIT], _buckets=per)


def load_board(cfg, *, filters: dict | None = None, now: dt.datetime | None = None) -> Board:
    """Read every idea (open and closed) plus the spec index once, then `build_board`.

    `filters` are `collect_idea_tasks`' own vocabulary (the `nw tui --state/--kind/...` flags)
    minus `project`, which selects a rail entry instead of narrowing the load. One open-question
    pass reads each org file once (`open_question_counts`), which is what keeps ~1100 ideas fast.
    """
    from .. import report as R
    from ..explore import open_question_counts
    from . import model as M

    now = now or dt.datetime.now(tz=cfg.get("_tz"))
    narrow = {k: v for k, v in (filters or {}).items() if v and k != "project"}
    tasks = M._dedupe_idea_tasks(cfg, R.collect_idea_tasks(cfg, all_done=True, **narrow))
    oq = open_question_counts(cfg, tasks)
    pairs = [(M._row_for(cfg, t, now=now, open_questions=oq.get(id(t), 0)), t) for t in tasks]
    return build_board(pairs, load_spec_index(cfg), now=now)
