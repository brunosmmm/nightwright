"""The marshal review packet and delta reviews (SPEC-0554, phase 2 of SPEC-0552).

With `state.lean.packet` pinned on (SPEC-0553 pins the flags at open), every review loop starts
with a pre-step inside REVIEWING (not a queue state): `queue._prepare_reviews` builds
`<campaign_dir>/packet-loop-N.json` (`nw.review_packet.v1`) and runs the `review_marshal`
persona, whose only output is the AC -> hunks/tests map. Everything else in the packet is
computed here by code: the spec's ACs (verbatim), the diff against base and the delta since the
previous loop's reviewed head (full text in side files, the JSON holds the stat), the last gate
results, every prior finding with its status (open / fixed / waived) and evidence, and pointers.

From loop 2 the reviewers run in `delta` mode: their diff is the delta, they see only their own
open prior findings and return a `prior_status` for each, and a new finding needs a `basis`.
`enforce` (called when the verdicts are stored) demotes a new finding without a valid basis to
advisory and turns the reviewer's open prior findings back into findings, so a prior finding
marked `fixed` stops counting and one it omits stays open.

A packet that cannot be built leaves the reviewers on today's prompt (`state.packet.ok` false);
a failed marshal leaves `ac_map: null` and `ac_map_error`. Neither ever parks."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from . import gitops as git
from . import store

SCHEMA_ID = "nw.review_packet.v1"
MARSHAL = "review_marshal"
# The marshal's run settings when a profile names none (builtin profiles do not: adding the
# persona to them would change every profile's sha for flag-off campaigns). With `lean.tiers`
# the model comes from the marshal tier instead.
MARSHAL_SETTINGS = {"model": "haiku", "effort": "medium", "max_budget_usd": 0.25,
                    "permission_mode": "dontAsk", "read_only": True}
BASES = ("initial", "delta_regression", "missed_ac")
STATUSES = ("open", "fixed", "waived")
COVERAGE = ("covered", "partial", "none")
_DIFF_PROMPT_MAX_LINES = 1500
_HUNKS_MAX = 400
_TESTS_MAX = 200
_EVIDENCE_MAX = 400
_AC_HEAD_RE = re.compile(r"^##\s+Acceptance criteria\s*$", re.IGNORECASE)
_BOX_RE = re.compile(r"^\s*[-*]\s+\[[ xX]\]\s+(.*)$")


# ---- state -----------------------------------------------------------------------------------

def on(state) -> bool:
    from . import tiers as T
    return T.pinned(state).get("packet") is True


def loop_of(state) -> int:
    """The review loop a packet is for: 1 for the first review, `counters.loops + 1` after."""
    return int(((state or {}).get("counters") or {}).get("loops") or 0) + 1


def name(loop: int) -> str:
    return f"packet-loop-{int(loop)}.json"


def _worktree_head(state) -> str | None:
    wt = ((state or {}).get("repo") or {}).get("worktree")
    if not wt or not Path(wt).is_dir():
        return None
    try:
        return git.git(wt, "rev-parse", "HEAD").stdout.strip() or None
    except Exception:                       # noqa: BLE001 — unverifiable: not stale
        return None


def current(state) -> dict | None:
    """`state.packet` when it is this loop's and was built at the worktree's current HEAD,
    else None (not attempted yet, or stale: REVIEWING re-entered at the same loop after a new
    commit). A HEAD that cannot be read does not make it stale."""
    p = (state or {}).get("packet")
    if not (isinstance(p, dict) and p.get("loop") == loop_of(state)):
        return None
    if p.get("head"):
        now = _worktree_head(state)
        if now is not None and now != p["head"]:
            return None
    return p


def forget(state) -> None:
    """Called as REVIEWING is entered: drop the packet meta so this review builds its own
    (`review_heads` of earlier loops and `packet_prev` stay). Flag off: nothing happens."""
    if on(state):
        state.pop("packet", None)


def current_ok(state) -> bool:
    p = current(state)
    return bool(p and p.get("ok"))


def marshal_due(state) -> bool:
    """The REVIEWING pre-step is due: the flag is pinned and this loop's packet was not
    attempted, or its marshal run is still pending (an interrupted run)."""
    if not on(state):
        return False
    p = current(state)
    return p is None or p.get("marshal") == "pending"


def remember_verdicts(state) -> None:
    """Called as REVIEWING is entered (before the previous verdicts are cleared): keep the
    outgoing loop's verdicts and waivers in `state.packet_prev`, which the next packet's prior
    findings are built from. Flag off: nothing happens."""
    if not on(state):
        return
    lr = state.get("last_review") if isinstance(state.get("last_review"), dict) else {}
    verdicts = lr.get("verdicts") or {}
    if not verdicts:
        return
    loops = {v.get("loop") for v in verdicts.values() if isinstance(v, dict)}
    loop = max((x for x in loops if isinstance(x, int)), default=None)
    if loop is None or loop >= loop_of(state) - 1:
        # verdicts of the loop being re-entered (a gate retry, a resume, a re-review): they
        # are not the previous loop's, so the previous loop's snapshot stays
        return
    state["packet_prev"] = {"loop": loop,
                            "verdicts": copy.deepcopy(verdicts),
                            "waived": copy.deepcopy(lr.get("waived") or [])}
    from . import collator as CO
    out = CO.out_findings(state)                    # SPEC-0555: empty unless it scoped any out
    if out:
        state["packet_prev"]["out_of_scope"] = out


def path_of(cfg, state, loop=None) -> Path:
    return store.campaign_dir(cfg, state["id"]) / name(loop or loop_of(state))


def load(cfg, state, loop=None) -> dict | None:
    try:
        doc = json.loads(path_of(cfg, state, loop).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == SCHEMA_ID else None


def _write(path: Path, doc) -> int:
    data = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)
    return len(data.encode("utf-8"))


# ---- deterministic parts ---------------------------------------------------------------------

def parse_acs(spec_text) -> list:
    """`[{id: AC1.., text}]` from the checkboxes of the spec's `## Acceptance criteria` section,
    in order. A checkbox's continuation lines are joined with single spaces; the text is
    otherwise verbatim (backticks, punctuation)."""
    out, inside, cur = [], False, None
    for line in (spec_text or "").splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = bool(_AC_HEAD_RE.match(line.strip()))
            continue
        if not inside:
            continue
        m = _BOX_RE.match(line)
        if m:
            cur = [m.group(1).strip()]
            out.append(cur)
        elif cur is not None and line.strip() and line[:1].isspace():
            cur.append(line.strip())
        elif not line.strip():
            cur = None
    return [{"id": f"AC{i}", "text": " ".join(parts)} for i, parts in enumerate(out, 1)]


def numstat(worktree, rng: str) -> list:
    """`[{path, added, deleted}]` of `git diff --numstat <rng>` (binary files: null counts)."""
    out = []
    raw = git.git(worktree, "-c", "core.quotepath=off", "diff", "--numstat", rng).stdout
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a, d = parts[0], parts[1]
        out.append({"path": "\t".join(parts[2:]), "added": int(a) if a.isdigit() else None,
                    "deleted": int(d) if d.isdigit() else None})
    return out


def _hunk_names(worktree, rng: str) -> list:
    from .personas import _split_diff
    full = git.git(worktree, "-c", "core.quotepath=off", "diff", "--unified=0", rng).stdout
    names = []
    for path, ranges in sorted(_split_diff(full).items()):
        for start, count in ranges:
            names.append(f"{path}:{start}" if count <= 1 else f"{path}:{start}-{start + count - 1}")
    return names


def candidate_tests(worktree, base) -> list:
    from .personas import tests_touched
    out = []
    for e in tests_touched(worktree, base):
        out += [f"{e['path']}::{n}" for n in e.get("changed") or [] if not n.startswith("…")]
    return out[:_TESTS_MAX]


def gates_of(cfg, state) -> dict:
    """The last gate payload, condensed: per gate `{name, ok, required, exit_code, timed_out,
    log, summary}`, the failing test ids and the ids known flaky (this run's retries or the
    cross-campaign record, `flaky.py`)."""
    from . import flaky
    results, failing, flaky_ids = [], [], []
    for r in ((state.get("last_gate") or {}).get("results") or []):
        if not isinstance(r, dict):
            continue
        s = r.get("summary") if isinstance(r.get("summary"), dict) else {}
        results.append({"name": r.get("name"), "ok": bool(r.get("ok")),
                        "required": r.get("required", True), "exit_code": r.get("exit_code"),
                        "timed_out": bool(r.get("timed_out")), "log": r.get("log"),
                        "summary": {k: s[k] for k in ("passed", "failed", "errors", "skipped")
                                    if k in s}})
        failing += [i for i in s.get("failing") or [] if not r.get("ok")]
        flaky_ids += list(r.get("flaky") or [])
    try:
        known = flaky.lookup(cfg, state.get("project") or "", failing) if failing else {}
    except (OSError, ValueError, TypeError, AttributeError):
        known = {}
    flaky_ids += [i for i in failing if i in known]
    return {"results": results, "failing": list(dict.fromkeys(failing)),
            "flaky": list(dict.fromkeys(flaky_ids))}


def _key(spec_id, f) -> str:
    from .followups import finding_key
    return finding_key(spec_id, f.get("title"), f.get("files"))


def _waiver_hit(spec_id, item, waivers) -> dict | None:
    from .queue import norm_finding_title
    for w in waivers:
        if not isinstance(w, dict):
            continue
        if _key(spec_id, w) == item["id"] or (
                norm_finding_title(w.get("title")) == norm_finding_title(item.get("title"))
                and norm_finding_title(w.get("title"))):
            return w
    return None


def prior_findings(spec_id, prev: list, verdicts: dict, prev_loop: int, human_waived=(),
                   triager_waived=(), out_of_scope=()) -> list:
    """Pure: every finding of the earlier loops, keyed by the stable follow-up key
    (`followups.finding_key`). `prev` = the previous packet's list; `verdicts` = the previous
    loop's stored verdicts (`state.packet_prev`), whose `prior_status` entries move a reviewer's
    own earlier findings to `fixed` / `open` and whose new findings are added as `open` (a
    demoted one is `advisory`). Human waivers and the triager's waivers make a finding
    `waived`; so does a finding the collator scoped out (SPEC-0555, `out_of_scope`: it is a
    follow-up idea now, so the reviewer does not carry it into the next loop)."""
    out = [dict(f) for f in prev or [] if isinstance(f, dict) and f.get("id")]
    by_id = {f["id"]: f for f in out}
    for persona, v in (verdicts or {}).items():
        if not isinstance(v, dict):
            continue
        for ps in v.get("prior_status") or []:
            f = by_id.get(ps.get("id")) if isinstance(ps, dict) else None
            if f is None or f.get("persona") != persona or ps.get("status") not in ("fixed", "open"):
                continue
            if f.get("status") == "waived":
                continue
            f["status"] = ps["status"]
            f["evidence"] = str(ps.get("evidence") or "")[:_EVIDENCE_MAX]
            f["status_loop"] = prev_loop
        for raw in v.get("findings") or []:
            if not isinstance(raw, dict) or raw.get("prior"):
                continue
            fid = _key(spec_id, raw)
            if fid in by_id:
                if by_id[fid].get("status") == "fixed":          # raised again: open again
                    by_id[fid]["status"] = "open"
                    by_id[fid]["evidence"] = f"raised again in loop {prev_loop}"
                continue
            item = {"id": fid, "persona": persona, "loop": prev_loop,
                    "severity": raw.get("severity") or "minor", "ac": raw.get("ac"),
                    "title": raw.get("title") or "", "files": list(raw.get("files") or []),
                    "category": raw.get("category"), "status": "open",
                    "evidence": str(raw.get("detail") or "")[:_EVIDENCE_MAX],
                    "basis": raw.get("basis") or "initial", "advisory": bool(raw.get("demoted"))}
            out.append(item)
            by_id[fid] = item
    for f in out:
        for origin, waivers in (("human", human_waived), ("triager", triager_waived)):
            w = _waiver_hit(spec_id, f, waivers or ())
            if w is not None and f.get("status") != "waived":
                f["status"] = "waived"
                f["evidence"] = f"{origin} waiver: {w.get('reason') or ''}".strip()[:_EVIDENCE_MAX]
        if out_of_scope and f.get("status") != "waived" and any(
                _key(spec_id, o) == f["id"] for o in out_of_scope if isinstance(o, dict)):
            f["status"] = "waived"
            f["evidence"] = "out of scope: filed as a follow-up idea"
    return out


def build(cfg, state) -> dict:
    """Build this loop's packet (marshal map pending) and its diff side files in the campaign
    dir; record the reviewed head in `state.review_heads`. Returns the `state.packet` meta.
    Raises on any failure (the caller records `ok: false`)."""
    from .queue import human_overrides_of
    loop = loop_of(state)
    repo = state.get("repo") or {}
    worktree, base = repo.get("worktree"), repo.get("base")
    if not worktree or not base or not Path(worktree).is_dir():
        raise ValueError("no worktree or base to build the packet from")
    cdir = store.campaign_dir(cfg, state["id"])
    head = git.git(worktree, "rev-parse", "HEAD").stdout.strip()
    base_sha = git.git(worktree, "merge-base", base, "HEAD").stdout.strip()
    heads = state.get("review_heads") if isinstance(state.get("review_heads"), dict) else {}
    prev_head = heads.get(str(loop - 1)) if loop > 1 else None
    stem = name(loop)[:-len(".json")]
    full_rng = f"{base_sha}..{head}"
    (cdir / f"{stem}.diff").write_text(git.git(worktree, "diff", full_rng).stdout,
                                       encoding="utf-8")
    delta = None
    if prev_head:
        rng = f"{prev_head}..{head}"
        (cdir / f"{stem}.delta.diff").write_text(git.git(worktree, "diff", rng).stdout,
                                                 encoding="utf-8")
        delta = {"stat": numstat(worktree, rng), "path": f"{stem}.delta.diff"}
    rel = (state.get("spec") or {}).get("rel_path")
    spec_text = (Path(worktree) / rel).read_text(encoding="utf-8") \
        if rel and (Path(worktree) / rel).is_file() else ""
    prev_doc = load(cfg, state, loop - 1) if loop > 1 else None
    pp = state.get("packet_prev") if isinstance(state.get("packet_prev"), dict) else {}
    hw, _at = human_overrides_of(state)
    findings = prior_findings(
        state.get("spec_id"), (prev_doc or {}).get("findings") or [],
        pp.get("verdicts") if pp.get("loop") == loop - 2 else {}, loop - 1, hw,
        pp.get("waived") if pp.get("loop") == loop - 2 else (),
        pp.get("out_of_scope") if pp.get("loop") == loop - 2 else ()) if loop > 1 else []
    gates = gates_of(cfg, state)
    doc = {
        "schema": SCHEMA_ID, "campaign": state["id"], "spec_id": state.get("spec_id"),
        "loop": loop, "mode": "delta" if delta is not None else "full",
        "head": head, "base": base_sha, "prev_head": prev_head,
        "acs": parse_acs(spec_text),
        "diff": {"stat": numstat(worktree, full_rng), "path": f"{stem}.diff"},
        "delta": delta,
        "ac_map": None, "ac_map_error": "marshal pending",
        "candidates": {"hunks": _hunk_names(worktree, full_rng)[:_HUNKS_MAX],
                       "tests": candidate_tests(worktree, base_sha)},
        "gates": gates,
        "findings": findings,
        "pointers": {"spec": rel, "diff": f"{stem}.diff",
                     "delta": delta["path"] if delta else None, "runs": "runs/",
                     "gate_logs": [r["log"] for r in gates["results"] if r.get("log")]},
    }
    problems = validate(doc)
    if problems:
        raise ValueError("packet invalid: " + "; ".join(problems[:5]))
    size = _write(cdir / name(loop), doc)
    heads = dict(heads)
    heads[str(loop)] = head
    state["review_heads"] = heads
    return {"loop": loop, "ok": True, "mode": doc["mode"], "path": name(loop),
            "marshal": "pending", "bytes": size, "head": head}


def failed(state, error) -> dict:
    out = {"loop": loop_of(state), "ok": False, "marshal": "skipped",
           "error": str(error)[:400]}
    head = _worktree_head(state)
    if head:
        out["head"] = head
    return out


# ---- schema ----------------------------------------------------------------------------------

_STR = {"type": "string"}
_OPT_STR = {"type": ["string", "null"]}
_STAT = {"type": "array", "items": {"type": "object", "required": ["path"],
                                    "properties": {"path": _STR}}}
PACKET_SCHEMA = {
    "type": "object",
    "required": ["schema", "campaign", "spec_id", "loop", "head", "base", "prev_head", "acs",
                 "diff", "delta", "ac_map", "gates", "findings", "pointers"],
    "properties": {
        "schema": {"type": "string", "enum": [SCHEMA_ID]},
        "loop": {"type": "integer"},
        "mode": {"type": "string", "enum": ["full", "delta"]},
        "acs": {"type": "array", "items": {"type": "object", "required": ["id", "text"],
                                           "properties": {"id": _STR, "text": _STR}}},
        "diff": {"type": "object", "required": ["stat", "path"],
                 "properties": {"stat": _STAT, "path": _STR}},
        "ac_map": {"type": ["array", "null"], "items": {
            "type": "object", "required": ["ac", "hunks", "tests", "coverage"],
            "properties": {"ac": _STR, "hunks": {"type": "array", "items": _STR},
                           "tests": {"type": "array", "items": _STR},
                           "coverage": {"type": "string", "enum": list(COVERAGE)}}}},
        "gates": {"type": "object", "required": ["results", "failing", "flaky"],
                  "properties": {"results": {"type": "array"}, "failing": {"type": "array"},
                                 "flaky": {"type": "array"}}},
        "findings": {"type": "array", "items": {
            "type": "object", "required": ["id", "persona", "loop", "severity", "title",
                                           "status", "evidence"],
            "properties": {"id": _STR, "persona": _STR, "loop": {"type": "integer"},
                           "title": _STR, "status": {"type": "string", "enum": list(STATUSES)},
                           "evidence": _STR}}},
        "pointers": {"type": "object", "required": ["spec", "diff", "runs", "gate_logs"],
                     "properties": {"diff": _STR, "runs": _STR,
                                    "gate_logs": {"type": "array", "items": _STR}}},
    },
}

_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "null": type(None)}


def _check(value, schema, path, out) -> None:
    types = schema.get("type")
    if types is not None:
        types = types if isinstance(types, list) else [types]
        if not any(isinstance(value, _TYPES[t]) and not (t == "integer" and isinstance(value, bool))
                   for t in types):
            out.append(f"{path}: expected {'|'.join(types)}")
            return
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, dict):
        for k in schema.get("required") or ():
            if k not in value:
                out.append(f"{path}.{k}: missing")
        for k, sub in (schema.get("properties") or {}).items():
            if k in value:
                _check(value[k], sub, f"{path}.{k}", out)
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            _check(item, schema["items"], f"{path}[{i}]", out)


def validate(doc) -> list:
    """Every problem of a packet against `nw.review_packet.v1` (empty when valid). Hand-rolled:
    jsonschema is dev-only."""
    out: list = []
    _check(doc, PACKET_SCHEMA, "packet", out)
    if isinstance(doc, dict) and doc.get("delta") is not None:
        _check(doc["delta"], PACKET_SCHEMA["properties"]["diff"], "packet.delta", out)
    return out


# ---- the marshal -----------------------------------------------------------------------------

MARSHAL_SCHEMA = {
    "type": "object",
    "required": ["ac_map"],
    "properties": {
        "ac_map": {"type": "array", "items": {
            "type": "object", "required": ["ac", "hunks", "tests", "coverage"],
            "properties": {"ac": {"type": "string"},
                           "hunks": {"type": "array", "items": {"type": "string"}},
                           "tests": {"type": "array", "items": {"type": "string"}},
                           "coverage": {"type": "string", "enum": list(COVERAGE)}}}},
    },
}


def marshal_values(cfg, state) -> dict:
    """The marshal's prompt values: `marshal_input` (ACs + candidate hunks and tests, JSON) and
    `packet_path`."""
    doc = load(cfg, state) or {}
    payload = {"acs": doc.get("acs") or [], "hunks": (doc.get("candidates") or {}).get("hunks")
               or [], "tests": (doc.get("candidates") or {}).get("tests") or []}
    return {"marshal_input": json.dumps(payload, indent=2, ensure_ascii=False),
            "packet_path": str(path_of(cfg, state))}


def clean_map(verdict, acs, candidates) -> list:
    """The marshal's `ac_map`, kept to the packet's ACs and candidates (a hunk or test the code
    did not list is dropped), one entry per AC in order; an AC the marshal left out is listed
    as `coverage: none`."""
    hunks = set((candidates or {}).get("hunks") or [])
    tests = set((candidates or {}).get("tests") or [])
    got = {}
    for e in (verdict or {}).get("ac_map") or []:
        if not isinstance(e, dict) or e.get("ac") in got:
            continue
        got[e.get("ac")] = e
    out = []
    for ac in acs or []:
        e = got.get(ac["id"]) or {}
        h = [x for x in e.get("hunks") or [] if x in hunks]
        t = [x for x in e.get("tests") or [] if x in tests]
        cov = e.get("coverage") if e.get("coverage") in COVERAGE else "none"
        out.append({"ac": ac["id"], "hunks": h, "tests": t,
                    "coverage": "none" if not (h or t) else cov})
    return out


def apply_marshal(cfg, state, verdict, error=None) -> dict:
    """Write the marshal's map (or `ac_map: null` + `ac_map_error`) into this loop's packet and
    mark `state.packet.marshal` done / failed. Returns the event payload."""
    meta = current(state) or {}
    doc = load(cfg, state)
    if doc is None:
        meta.update(marshal="failed", ok=False, error="packet file missing")
        state["packet"] = meta
        return {"loop": loop_of(state), "ok": False, "error": meta["error"]}
    if error is None and isinstance(verdict, dict) and isinstance(verdict.get("ac_map"), list):
        doc["ac_map"] = clean_map(verdict, doc.get("acs"), doc.get("candidates"))
        doc.pop("ac_map_error", None)
        meta["marshal"] = "done"
    else:
        doc["ac_map"] = None
        doc["ac_map_error"] = str(error or "marshal returned no ac_map")[:400]
        meta["marshal"] = "failed"
    meta["bytes"] = _write(path_of(cfg, state), doc)
    state["packet"] = meta
    out = {"loop": doc["loop"], "ok": True, "mode": doc.get("mode"), "bytes": meta["bytes"],
           "ac_map": doc["ac_map"] is not None,
           "none": sum(1 for e in doc["ac_map"] or [] if e["coverage"] == "none")}
    if doc["ac_map"] is None:
        out["ac_map_error"] = doc["ac_map_error"]
    return out


# ---- reviewers -------------------------------------------------------------------------------

FULL_NOTE = """\
## Review packet (read it first)

This loop's review packet is `{{packet_path}}` (review mode: `{{review_mode}}`). Read it before
anything else: it holds the spec's acceptance criteria verbatim (`acs`, ids `AC1..`), the diff
stat (full text in `diff.path`, next to the packet), the gate results, an AC -> hunks/tests map
(`ac_map`; `coverage: none` means no code or test was found for that AC; the map is a hint, so
check it) and pointers to the spec, the runs and the gate logs. You may open any raw file it
points to, and any file in the worktree.

Every finding also has a `basis`: `initial` in this first loop. Return `prior_status: []`."""

DELTA_NOTE = """\
## Review packet (read it first) — delta review

This loop's review packet is `{{packet_path}}` (review mode: `{{review_mode}}`). Read it before
anything else: the spec's acceptance criteria (`acs`), the gate results, the AC -> hunks/tests
map (`ac_map`, a hint), every earlier finding with its status, and pointers. The diff shown
above is only the **delta** since the head reviewed in the previous loop (`{{base}}`); the full
diff against the base is in the packet's `diff.path`. You may open any raw file.

This is loop {{packet_loop}}. Do not re-review the whole change:

1. Verify each of **your own** open findings from earlier loops (below). Return one
   `prior_status` entry per finding: `{"id": "<id>", "status": "fixed" | "open", "evidence":
   "<file/test that shows it>"}`. A finding you leave out stays open.
2. Review the delta.
3. Raise a new finding only if it is a regression introduced by the delta (`basis:
   "delta_regression"`, and its `files` must name a file the delta touches) or it is tied to
   an acceptance criterion you missed in loop 1 (`basis: "missed_ac"`, and its `ac` must be a
   packet AC id such as `AC3`). Any other new finding is recorded as advisory and never blocks.
4. An AC you assessed met in an earlier loop stays met unless the delta touches its mapped
   hunks.

### Your open findings from earlier loops

{{prior_findings}}"""


def _reviewer_findings_text(findings) -> str:
    if not findings:
        return "(none)"
    lines = []
    for f in findings:
        files = f" ({', '.join(f.get('files') or [])})" if f.get("files") else ""
        ac = f" [{f['ac']}]" if f.get("ac") else ""
        lines.append(f"- `{f['id']}` loop {f.get('loop')} {f.get('severity')}{ac}: "
                     f"{f.get('title')}{files}")
    return "\n".join(lines)


def own_open(doc, persona) -> list:
    """The reviewer's own prior findings that still count: open, not advisory."""
    return [f for f in (doc or {}).get("findings") or []
            if f.get("persona") == persona and f.get("status") == "open"
            and not f.get("advisory")]


def review_values(cfg, state, persona) -> dict | None:
    """The prompt values of a reviewer run with this loop's packet, or None (no packet: today's
    prompt). Delta mode replaces `diff` with the delta (≤ 1500 lines) and `base` with the
    previous loop's head."""
    if not current_ok(state):
        return None
    doc = load(cfg, state)
    if doc is None:
        return None
    vals = {"packet_path": str(path_of(cfg, state)), "review_mode": doc.get("mode") or "full",
            "packet_loop": doc.get("loop")}
    if doc.get("mode") == "delta" and doc.get("delta"):
        cdir = store.campaign_dir(cfg, state["id"])
        try:
            text = (cdir / doc["delta"]["path"]).read_text(encoding="utf-8")
        except OSError:
            return None
        lines = text.splitlines()
        if len(lines) > _DIFF_PROMPT_MAX_LINES:
            lines = lines[:_DIFF_PROMPT_MAX_LINES] + [
                f"… delta truncated: {len(lines) - _DIFF_PROMPT_MAX_LINES} more line(s); "
                f"read {cdir / doc['delta']['path']} for the rest"]
        vals.update(diff="\n".join(lines) or "(the delta is empty)",
                    base=str(doc.get("prev_head") or "")[:12],
                    prior_findings=_reviewer_findings_text(own_open(doc, persona)))
    return vals


def note(values) -> str:
    from .personas import render
    tpl = DELTA_NOTE if values.get("review_mode") == "delta" else FULL_NOTE
    return render(tpl, values)


def extend_schema(schema) -> dict:
    """A reviewer's output schema plus `prior_status` and a per-finding `basis` (both optional,
    so a verdict without them still conforms)."""
    out = json.loads(json.dumps(schema))
    props = out.setdefault("properties", {})
    props["prior_status"] = {"type": "array", "items": {
        "type": "object", "required": ["id", "status"],
        "properties": {"id": {"type": "string"},
                       "status": {"type": "string", "enum": ["fixed", "open"]},
                       "evidence": {"type": "string"}}}}
    items = ((props.get("findings") or {}).get("items") or {})
    if isinstance(items.get("properties"), dict):
        items["properties"]["basis"] = {"type": "string", "enum": list(BASES)}
    return out


def run_marks(state, persona, step_kind) -> dict:
    """`run.start` keys with the flag pinned: `loop` and `packet` (the run used a packet).
    Empty when off, so a flag-off `run.start` is unchanged."""
    if not on(state):
        return {}
    used = step_kind == "review" and persona != "review_triager" and current_ok(state)
    return {"loop": loop_of(state), "packet": bool(used)}


# ---- enforcement (loop 2+) -------------------------------------------------------------------

def enforcement(cfg, state) -> dict | None:
    """What `enforce` needs from this loop's packet in delta mode, else None."""
    if not current_ok(state):
        return None
    doc = load(cfg, state)
    if doc is None or doc.get("mode") != "delta":
        return None
    return {"loop": doc["loop"], "delta_files": {s["path"] for s in (doc.get("delta") or {})
                                                 .get("stat") or []},
            "ac_ids": {a["id"] for a in doc.get("acs") or []}, "doc": doc}


def _basis_ok(f, enf) -> bool:
    basis = f.get("basis")
    if basis == "delta_regression":
        return bool(set(f.get("files") or []) & enf["delta_files"])
    if basis == "missed_ac":
        return str(f.get("ac") or "").strip() in enf["ac_ids"]
    return False


def enforce(spec_id, persona, verdict, enf) -> tuple:
    """Pure: `(findings, demoted)` to store for `persona`'s delta-mode verdict. The reviewer's
    own open prior findings that it did not mark `fixed` come first (as findings carrying
    `prior: <id>`); then its new findings, each with a `demoted: True` mark when its basis is
    not a valid `delta_regression` (a file in the delta) or `missed_ac` (a packet AC id). A new
    finding that repeats a prior one (same key) is that prior finding, not a new one."""
    status = {ps.get("id"): ps.get("status") for ps in verdict.get("prior_status") or []
              if isinstance(ps, dict)}
    prior = own_open(enf["doc"], persona)
    prior_ids = {f["id"] for f in prior}
    out = []
    for f in prior:
        if status.get(f["id"]) == "fixed":
            continue
        out.append({"severity": f.get("severity") or "minor", "title": f.get("title") or "",
                    "detail": f.get("evidence") or None, "files": list(f.get("files") or []),
                    "ac": f.get("ac"), **({"category": f["category"]} if f.get("category")
                                          else {}), "prior": f["id"]})
    demoted = 0
    for f in verdict.get("findings") or []:
        if not isinstance(f, dict):
            continue
        if _key(spec_id, f) in prior_ids:
            continue
        g = dict(f)
        if not _basis_ok(g, enf):
            g["demoted"] = True
            demoted += 1
        out.append(g)
    return out, demoted
