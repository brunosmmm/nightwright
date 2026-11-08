"""Spec review before admission (SPEC-0188): a read-only `spec_reviewer` persona reads a spec,
its source idea and the spec schema. Its verdict is stored in the data dir
(`<data>/agentic/reviews/<SPEC-ID>/<sha256>.json`, SPEC-0210), keyed by the spec file's sha256 —
so any later edit to the spec invalidates an `approve` by itself. `nw queue add` admits a spec
on a matching `approve`, or on a `revise` whose findings were applied by an edit (SPEC-0230:
one review round, no second review). Every standalone run is journaled to `spec-reviews.jsonl`."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

from .. import specs as SP
from ..specmeta import split_frontmatter
from . import personas as PS
from . import project as P
from . import store
from .gates import tmp_env
from .profiles import SIZING_VERDICTS, resolve_profile, select_profile
from .runner import select_runner

SCHEMA = "nw.spec.review.v1"
PERSONA = "spec_reviewer"
REVIEWS_DIRNAME = "reviews"
RUN_SCHEMA = "nw.spec.review.run.v1"
_RUN_ID_RE = re.compile(r"[^A-Za-z0-9_.-]")


def spec_sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sidecar_path(spec_path, spec_id) -> Path:
    """Legacy `<dir of the spec>/reviews/<SPEC-ID>.json` (pre-SPEC-0210)."""
    return Path(spec_path).parent / REVIEWS_DIRNAME / f"{spec_id}.json"


def _read_json_dict(p):
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_legacy_sidecar(spec_path, spec_id):
    """The legacy sidecar payload, or None when absent or unreadable."""
    return _read_json_dict(sidecar_path(spec_path, spec_id))


# ---- the store ---------------------------------------------------------------------------------

def store_path(cfg, spec_id, sha) -> Path:
    return Path(cfg["data_dir"]) / "agentic" / REVIEWS_DIRNAME / spec_id / f"{sha}.json"


def save_review(cfg, payload) -> Path:
    """Write the review atomically (temp file + `os.replace`); the last write for a given
    spec id and sha wins."""
    out = store_path(cfg, payload["spec_id"], payload["spec_sha256"])
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=out.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return out


def load_review(cfg, spec_id, sha):
    """The stored review payload, or None when missing, unreadable or not a JSON object."""
    return _read_json_dict(store_path(cfg, spec_id, sha))


def _repo_identity(root):
    """What makes two checkouts the same repository: the resolved git common dir (main
    checkout and linked worktrees share it), else the resolved path. None for an empty root."""
    if not root:
        return None
    root = Path(root)
    try:
        checkout = SP._git_checkout(root) if root.is_dir() else None
        return str(checkout[1] if checkout else root.resolve())
    except OSError:
        return str(root)


def _reviewed_at_key(review):
    """Sort key: the parsed aware `reviewed_at` (naive ⇒ UTC); unparsable or missing sorts first."""
    try:
        at = dt.datetime.fromisoformat(str(review.get("reviewed_at") or ""))
    except ValueError:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    return at if at.tzinfo else at.replace(tzinfo=dt.timezone.utc)


def list_reviews(cfg, spec_id, spec_path=None) -> list:
    """Every readable stored review of `spec_id` (all shas), oldest `reviewed_at` first.
    Corrupt files and temp files are skipped. With `spec_path`, reviews recorded for another
    repository's spec of the same id (`--path` reviews, a `repo_root` that exists and has a
    different git common dir) are ignored; an empty `repo_root` (legacy reviews) or one that no
    longer exists (removed linked worktree, moved checkout) matches."""
    d = store_path(cfg, spec_id, "x").parent
    if not d.is_dir():
        return []
    out = [r for r in (_read_json_dict(p) for p in sorted(d.glob("*.json"))) if r]
    if spec_path is not None and _in_outbox(cfg, spec_path):
        # SPEC-0243: any repo_root counts (legacy Meta-Tools roots), but not a review of another
        # file of the same id (the exported portable copy in the target repo).
        out = [r for r in out if not r.get("spec_path") or _in_outbox(cfg, r["spec_path"])]
    elif spec_path is not None:
        mine, seen = _repo_identity(spec_root(cfg, spec_path)), {}
        def same_repo(r):
            root = str(r.get("repo_root") or "")
            if not root or not Path(root).is_dir():
                return True
            if root not in seen:
                seen[root] = _repo_identity(root)
            return seen[root] == mine
        out = [r for r in out if same_repo(r)]
    return sorted(out, key=_reviewed_at_key)


SPLIT_PARTS_MIN, SPLIT_PARTS_MAX = 2, 5          # SPEC-0290: parts a split campaign writes


def latest_review(cfg, spec_id, spec_path=None):
    """The most recent stored review of `spec_id` (of this spec's repository when `spec_path`
    is given), or None."""
    reviews = list_reviews(cfg, spec_id, spec_path)
    return reviews[-1] if reviews else None


def check_admissible(cfg, spec_path, spec_id) -> dict:
    """The review that admits the spec, or ValueError with the fix (SPEC-0230). An `approve`
    for the current text admits it (`admission: "approved"`; a stored review wins, a legacy
    sidecar with a matching sha still counts). Otherwise the latest stored review governs: a
    `revise` for an earlier sha admits it (`admission: "after_revision"`, the revision
    happened); an unapplied `revise` of an internal spec admits it as `"revise_pending"`
    (SPEC-0279: the spec writer amends it first); a `split`, an unapplied `revise` of an
    outbound spec or an edit after an `approve` is refused. SPEC-0290: a `split` of an internal
    spec for its current text with 2-5 suggested splits admits it as `"split_pending"` (a split
    campaign writes the parts)."""
    sha = spec_sha256(spec_path)
    review = load_review(cfg, spec_id, sha)
    if not (review and review.get("verdict") == "approve"):
        legacy = load_legacy_sidecar(spec_path, spec_id)
        review = legacy if (legacy and legacy.get("verdict") == "approve"
                            and legacy.get("spec_sha256") == sha) else None
    if review:
        return {**review, "admission": "approved", "current_sha": sha}
    latest = latest_review(cfg, spec_id, spec_path)
    if latest is None:
        raise ValueError(f"{spec_id} has no spec review; run nw spec review {spec_id}")
    verdict, rsha = latest.get("verdict"), str(latest.get("spec_sha256") or "")
    if verdict == "split":
        parts = latest.get("suggested_splits") or []
        why = None
        if SP._is_outbound_spec_id(spec_id):
            why = "an outbound spec cannot be split automatically"
        elif rsha != sha:
            why = f"the review is stale (the text changed since {rsha[:12]})"
        elif not SPLIT_PARTS_MIN <= len(parts) <= SPLIT_PARTS_MAX:
            why = (f"it suggests {len(parts)} parts; a split campaign needs "
                   f"{SPLIT_PARTS_MIN}-{SPLIT_PARTS_MAX}")
        if why is None:                                                # SPEC-0290
            return {**latest, "admission": "split_pending", "current_sha": sha}
        raise ValueError(f"{spec_id}: the spec review recommends a split ({rsha[:12]}); "
                         f"split the spec and queue the parts (not automatic: {why})")
    if verdict == "revise":
        if rsha == sha:
            if not SP._is_outbound_spec_id(spec_id):                   # SPEC-0279
                return {**latest, "admission": "revise_pending", "current_sha": sha}
            raise ValueError(f"{spec_id}: the review asks for revisions that are not applied "
                             f"yet (the text is unchanged since {rsha[:12]}); edit the spec, "
                             f"then queue it (no second review needed)")
        return {**latest, "admission": "after_revision", "current_sha": sha}
    raise ValueError(f"{spec_id} was edited after its approving review ({rsha[:12]}); "
                     f"run nw spec review {spec_id} --force")


def revision_brief(review) -> dict:
    """The implementer's `pending_brief` for a spec admitted after one revision: every finding
    of the `revise` review as a MUST FIX to check (nits included)."""
    must_fix = []
    for f in review.get("findings") or []:
        title = f"[{f.get('severity')}] {f.get('title')}"
        if f.get("section"):
            title += f" ({f['section']})"
        must_fix.append({"title": title, "detail": PS.finding_text(f)})
    sha12 = str(review.get("spec_sha256") or "")[:12]
    return {
        "source": "spec_review",
        "fix_brief": (f"This spec was admitted after one revision of review {sha12}, with no "
                      "second review. Before implementing, check that the spec text addresses "
                      "each finding below. Implement the spec as written; where a finding is "
                      "not addressed, apply its suggestion in a way consistent with the spec."),
        "must_fix": must_fix, "human_notes": [],
    }


def would_pick(cfg, review, *, project_root=None) -> dict | None:
    """What `nw queue add` would pick for `review` under the current config (SPEC-0219), or
    None unless the review approves or revises. Not stored: the matrix is config and may change."""
    if not review or review.get("verdict") not in SIZING_VERDICTS:
        return None
    return select_profile(cfg, review=review, project_root=project_root)


def show_payload(cfg, spec_path, spec_id) -> dict | None:
    """`nw spec review --show` (SPEC-0242): the stored review of the spec, with whether it still
    matches the file, whether `nw queue add` admits the spec now and the profile it would pick.
    None when the spec has no stored review. Runs nothing, writes nothing."""
    stored = list_reviews(cfg, spec_id, spec_path)
    if not stored:
        return None
    current = spec_sha256(spec_path)
    review = load_review(cfg, spec_id, current) or stored[-1]
    try:
        admitted = check_admissible(cfg, spec_path, spec_id)
        admission = {"ok": True, "kind": admitted["admission"], "message": None}
    except ValueError as e:
        admission = {"ok": False, "kind": None, "message": str(e)}
    return {"schema": "nw.spec.review.show.v1", "spec_id": spec_id, "spec_path": str(spec_path),
            "current_sha": current, "matches_current": review.get("spec_sha256") == current,
            "review": review, "admission": admission, "profile": would_pick(cfg, review),
            "stored": len(stored)}


def all_latest_reviews(cfg) -> list:
    """`nw spec reviews` (SPEC-0242): one row per spec id in the review store, its latest review.
    `matches_current` is None when the spec id no longer resolves (another repository's `--path`
    review, a deleted spec). The spec resolves as `nw spec review --show` does, from the checkout
    of the current directory. Ids without a readable review are skipped."""
    root = Path(cfg["data_dir"]) / "agentic" / REVIEWS_DIRNAME
    rows = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        spec_id = d.name
        try:
            path = SP.resolve_spec_path_for_cwd(cfg, spec_id)[0]
        except ValueError:
            path = None
        review = latest_review(cfg, spec_id, path)
        if review is None and path is not None:      # only another repository's `--path` reviews
            review, path = latest_review(cfg, spec_id), None
        if review is None:
            continue
        rows.append({
            "spec_id": spec_id, "verdict": review.get("verdict"), "size": review.get("size"),
            "tone": review.get("tone"), "counts": _counts(review.get("findings")),
            "reviewed_at": review.get("reviewed_at"), "spec_sha256": review.get("spec_sha256"),
            "matches_current": (None if path is None
                                else review.get("spec_sha256") == spec_sha256(path)),
            "model": review.get("model"), "reviewer_profile": review.get("reviewer_profile"),
            "cost_usd": review.get("cost_usd")})
    return rows


# ---- root, context, payload ----------------------------------------------------------------------

def _fallback_root(spec_path) -> Path:
    return Path(spec_path).resolve().parent.parent.parent


def _in_outbox(cfg, spec_path) -> bool:
    try:
        Path(spec_path).resolve().relative_to(Path(SP.outbox_dir(cfg)).resolve())
    except ValueError:
        return False
    return True


def _meta_tools_root(cfg, path) -> Path:
    try:
        return Path(P.project_config(cfg, "Meta-Tools")["_root"])
    except (ValueError, KeyError):
        return _fallback_root(path)


def spec_root(cfg, spec_path) -> Path:
    """The repository whose checkout the reviewer sees. Outbound specs (in the outbox) use their
    target project's checkout (SPEC-0243; the Meta-Tools checkout when that project cannot be
    loaded); any other spec its own git toplevel, or outside git `<root>/docs/specs/<file>`."""
    path = Path(spec_path).resolve()
    if _in_outbox(cfg, path):
        try:
            fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
            return Path(P.project_config(cfg, SP.outbound_project(path, fm))["_root"])
        except (ValueError, KeyError, OSError, yaml.YAMLError):   # unreadable/odd front matter too
            return _meta_tools_root(cfg, path)
    try:
        r = subprocess.run(["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return _fallback_root(path)
    top = r.stdout.strip()
    return Path(top) if top else _fallback_root(path)


def _idea_text(cfg, idea_id) -> str:
    if not idea_id:
        return "(none: the spec names no source idea)"
    from ..explore import idea_show_payload
    from ..report import _idea_by_id
    task = _idea_by_id(cfg, idea_id)
    if task is None:
        return f"(source idea {idea_id} not found)"
    return json.dumps(idea_show_payload(cfg, task), indent=2, ensure_ascii=False)


def review_context(spec_path, spec_id, *, idea_text, root=None, schema_root=None) -> dict:
    """The persona context: spec id, path relative to the root, spec text, SCHEMA.md text (read
    from `schema_root`, default the root; None when absent) and the source-idea text."""
    path = Path(spec_path)
    root = Path(root) if root is not None else _fallback_root(path)
    schema_file = Path(schema_root or root) / "docs" / "specs" / "SCHEMA.md"
    schema_text = schema_file.read_text(encoding="utf-8") if schema_file.is_file() else None
    try:
        rel = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        rel = str(path)
    return {"spec_id": spec_id, "spec_rel_path": rel, "spec_text": path.read_text(encoding="utf-8"),
            "schema_text": schema_text, "idea_text": idea_text}


def _check_verdict(spec_id, verdict) -> None:
    findings = verdict.get("findings") or []
    if verdict["verdict"] == "approve" and any(f.get("severity") == "must_fix" for f in findings):
        raise ValueError(f"spec review of {spec_id} is `approve` but lists a must_fix finding")
    for f in findings:                                                  # SPEC-0295
        fix = f.get("suggested_fix") if isinstance(f, dict) else None
        if not (isinstance(fix, str) and fix.strip()):
            title = f.get("title") if isinstance(f, dict) else f
            raise ValueError(f"spec review of {spec_id} has a finding without a "
                             f"`suggested_fix`: {title!r}")
    if verdict.get("size") == "xl" and verdict["verdict"] != "split":      # SPEC-0219
        raise ValueError(f"spec review of {spec_id} sizes the spec `xl` (too big for one "
                         f"campaign) but its verdict is `{verdict['verdict']}`, not `split`")


def review_payload(spec_id, sha, *, model, cost_usd, verdict, root, spec_path=None,
                   origin=None, reviewer_profile=None) -> dict:
    """The `nw.spec.review.v1` dict; raises ValueError on an approve carrying a must_fix.
    `spec_path` (absolute) and `origin` (SPEC-0227) are recorded when given. `reviewer_profile`
    (SPEC-0278) names the profile whose `spec_reviewer` settings ran; not `profile`, which
    `nw spec review --json` uses for the campaign profile `would_pick` returns."""
    _check_verdict(spec_id, verdict)
    extra = {}
    if spec_path is not None:
        extra["spec_path"] = str(Path(spec_path).resolve())
    if origin is not None:
        extra["origin"] = origin
    return {
        "schema": SCHEMA, "spec_id": spec_id, "verdict": verdict["verdict"],
        "spec_sha256": sha, "reviewed_at": store.now_iso(), "model": model,
        "reviewer_profile": reviewer_profile, "cost_usd": cost_usd, "size": verdict.get("size"), "tone": verdict.get("tone"),
        "rationale": verdict.get("rationale"), "findings": verdict.get("findings") or [],
        "suggested_splits": verdict.get("suggested_splits") or [],
        "repo_root": str(root), **extra,
    }


# ---- the run journal ---------------------------------------------------------------------------------

def review_profile_name(cfg, flag=None) -> str:
    """The spec reviewer's profile name (SPEC-0278): `flag` > `agentic.spec_review_profile` >
    `agentic.default_profile` > `balanced`."""
    ag = (cfg or {}).get("agentic") or {}
    return flag or ag.get("spec_review_profile") or ag.get("default_profile") or "balanced"


def reviewer_run_settings(cfg, spec_path, name=None):
    """`(profile, spec_reviewer settings)` for a review of `spec_path`, from `name` or the
    spec-review default. The one resolution the CLI's reviewing line and the run share; an
    unknown profile raises ValueError listing the known ones."""
    prof = resolve_profile(cfg, review_profile_name(cfg, name),
                           project_root=spec_root(cfg, spec_path))
    return prof, PS.persona_settings(prof, PERSONA)


def reviewing_line(cfg, spec_path, origin) -> str:
    """`reviewing <rel path> @ <sha12> (<origin>)  <toplevel>`: which file a review reads
    (SPEC-0227)."""
    path = Path(spec_path).resolve()
    root = spec_root(cfg, path).resolve()
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = str(path)
    return f"reviewing {rel} @ {spec_sha256(path)[:12]} ({origin})  {root}"


def journal_path(cfg) -> Path:
    return store.agentic_dir(cfg) / "spec-reviews.jsonl"


def _journal_append(cfg, rec) -> None:
    p = journal_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def journal_start(cfg, run_id, spec_id, timeout_s, run_dir, *, reviewer_profile=None,
                  model=None) -> None:
    _journal_append(cfg, {"schema": RUN_SCHEMA, "kind": "start", "run_id": run_id,
                          "spec_id": spec_id, "ts": store.now_iso(), "timeout_s": timeout_s,
                          "run_dir": str(run_dir), "reviewer_profile": reviewer_profile,
                          "model": model})


def journal_end(cfg, run_id, spec_id, *, verdict=None, counts=None, cost_usd=None,
                duration_s=None, error=None, spec_sha256=None) -> None:
    rec = {"schema": RUN_SCHEMA, "kind": "end", "run_id": run_id, "spec_id": spec_id,
           "ts": store.now_iso(), "verdict": verdict, "counts": counts, "cost_usd": cost_usd,
           "duration_s": duration_s, "error": error}
    if spec_sha256 is not None:
        rec["spec_sha256"] = spec_sha256
    _journal_append(cfg, rec)


def _tail_lines(path, n) -> list:
    """The last `n` lines of a file, read backwards in blocks."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        buf = b""
        while pos > 0 and buf.count(b"\n") <= n:
            step = min(65536, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
    return buf.splitlines()[-n:] if n > 0 else []


def _ts(value):
    if isinstance(value, dt.datetime):
        d = value
    else:
        try:
            d = dt.datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return d if d.tzinfo else d.astimezone()


def read_journal(cfg, *, since_ts=None, tail=2000) -> list:
    """Journal records from the last `tail` lines, optionally only those at or after `since_ts`
    (ISO string or datetime). Torn or foreign lines are skipped."""
    p = journal_path(cfg)
    if not p.is_file():
        return []
    since = _ts(since_ts) if since_ts is not None else None
    out = []
    try:
        lines = _tail_lines(p, tail)
    except OSError:                                        # unreadable: an optional side journal
        return []
    for raw in lines:
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if since is not None:
            t = _ts(rec.get("ts"))
            if t is None or t < since:
                continue
        out.append(rec)
    return out


# ---- the run ---------------------------------------------------------------------------------------------

def _counts(findings) -> dict:
    c: dict = {}
    for f in findings if isinstance(findings, list) else []:
        sev = f.get("severity") if isinstance(f, dict) else None
        sev = sev if sev and isinstance(sev, str) else "unknown"
        c[sev] = c.get(sev, 0) + 1
    return c


def run_spec_review(cfg, spec_path, spec_id, *, profile=None, idea_text=None,
                    origin=None, resolved=None) -> dict:
    """Run the spec reviewer on the spec file, store the review and journal the run. A run
    error or a verdict that does not conform appends an `end` with `error`, raises ValueError
    and stores nothing. `resolved` is a `reviewer_run_settings` pair, else `profile` (default
    `review_profile_name(cfg)`) is resolved here."""
    path = Path(spec_path)
    sha = spec_sha256(path)
    root = spec_root(cfg, path)
    if idea_text is None:
        fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        idea_text = _idea_text(cfg, (fm or {}).get("source_idea"))
    schema_root = None
    if _in_outbox(cfg, path):          # SPEC-0243: the target's schema, else nightwright's (the portable format)
        schema_root = root if (root / "docs" / "specs" / "SCHEMA.md").is_file() \
            else _meta_tools_root(cfg, path)
    ctx = review_context(path, spec_id, idea_text=idea_text, root=root, schema_root=schema_root)
    persona = PS.get_persona(PERSONA)
    prof, settings = resolved or reviewer_run_settings(cfg, path, profile)
    run_id = _RUN_ID_RE.sub("-", f"{spec_id}-{store.now_iso()}-{os.getpid()}-{secrets.token_hex(2)}")
    run_dir = store.agentic_dir(cfg) / "spec-reviews" / run_id
    run_tmp = run_dir / "tmp"
    run_tmp.mkdir(parents=True, exist_ok=True)
    from .runner import mapped_model
    alias = str(settings.get("model") or "sonnet")
    effort = settings.get("effort")
    model, model_alias = mapped_model(
        cfg, alias, effort, project_root=str(root) if root else None,
        cursor_model=settings.get("cursor_model"))
    from .runner import map_pi_model, resolve_runner
    pi_provider = pi_id = pi_thinking = None
    if resolve_runner(cfg) == "pi":
        row = map_pi_model(alias, effort, cfg, project_root=str(root) if root else None)
        pi_provider, pi_id, pi_thinking = row["provider"], row["id"], row["thinking"]
    req = PS.RunRequest(
        persona=PERSONA,
        prompt=PS.build_prompt(persona, ctx),
        system_append=PS.render(persona.invariant, ctx),
        cwd=root,
        model=model,
        effort=effort,
        max_budget_usd=float(settings.get("max_budget_usd") or 0.8),
        timeout_s=int(prof.get("run_timeout_s") or 1800),
        permission_mode=settings.get("permission_mode") or "dontAsk",
        read_only=True,
        allowed_tools=(),
        add_dirs=(root,),
        json_schema=persona.schema,
        env={"NW_DATA_DIR": str(cfg.get("data_dir") or ""),
             "NW_CONFIG_DIR": str(cfg.get("config_dir") or ""), "NW_SPEC_ID": spec_id,
             "NW_PERSONA_RUN": "1", **tmp_env(run_tmp)},
        run_dir=run_dir,
        profile=prof.get("name"),
        profile_sources=tuple(prof.get("sources") or ()),
        model_alias=model_alias,
        pi_provider=pi_provider,
        pi_id=pi_id,
        pi_thinking=pi_thinking,
    )
    journal_start(cfg, run_id, spec_id, req.timeout_s, req.run_dir,
                  reviewer_profile=prof.get("name"), model=req.model)
    t0 = time.monotonic()
    cost = None
    try:
        result = select_runner(cfg).run(req)
        cost = result.cost_usd
        if result.timed_out or result.is_error or not result.verdict_ok:
            why = ("timed out" if result.timed_out else "failed" if result.is_error
                   else "returned a verdict that does not match the schema")
            raise ValueError(f"spec review of {spec_id} {why} (see {req.run_dir})")
        payload = review_payload(spec_id, sha, model=req.model, cost_usd=result.cost_usd,
                                 verdict=result.verdict, root=root, spec_path=path,
                                 origin=origin, reviewer_profile=prof.get("name"))
        save_review(cfg, payload)
    except Exception as e:
        journal_end(cfg, run_id, spec_id, cost_usd=cost, error=str(e),
                    duration_s=round(time.monotonic() - t0, 1))
        raise
    journal_end(cfg, run_id, spec_id, verdict=payload["verdict"],
                counts=_counts(payload["findings"]), cost_usd=payload["cost_usd"],
                duration_s=round(time.monotonic() - t0, 1), spec_sha256=sha)
    return payload


def review_spec(cfg, spec_id, *, profile=None) -> dict:
    """Review `spec_id` (internal or outbound) by id; see `run_spec_review`."""
    return run_spec_review(cfg, SP.resolve_spec_path(cfg, spec_id), spec_id, profile=profile)
