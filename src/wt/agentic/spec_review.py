"""Spec review before admission (SPEC-0188): a read-only `spec_reviewer` persona reads a spec,
its source idea and the spec schema. Its verdict is stored in the data dir
(`<data>/agentic/reviews/<SPEC-ID>/<sha256>.json`, SPEC-0210), keyed by the spec file's sha256 —
so any later edit to the spec invalidates the review by itself. `wt queue add` admits a spec
only on a matching `approve`. Every standalone run is journaled to `spec-reviews.jsonl`."""
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

from .. import specs as SP
from ..specmeta import split_frontmatter
from . import personas as PS
from . import project as P
from . import store
from .profiles import resolve_profile
from .runner import select_runner

SCHEMA = "wt.spec.review.v1"
PERSONA = "spec_reviewer"
REVIEWS_DIRNAME = "reviews"
RUN_SCHEMA = "wt.spec.review.run.v1"
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


def check_admissible(cfg, spec_path, spec_id) -> dict:
    """The approving review for the spec's current text, or ValueError with the fix. A stored
    review wins; a legacy sidecar with a matching sha still counts."""
    sha = spec_sha256(spec_path)
    review = load_review(cfg, spec_id, sha)
    if not (review and review.get("verdict") == "approve"):
        legacy = load_legacy_sidecar(spec_path, spec_id)
        review = legacy if (legacy and legacy.get("verdict") == "approve"
                            and legacy.get("spec_sha256") == sha) else None
    if not review:
        raise ValueError(f"{spec_id} has no approving review for its current text; "
                         f"run wt spec review {spec_id} (or --force)")
    return review


# ---- root, context, payload ----------------------------------------------------------------------

def _fallback_root(spec_path) -> Path:
    return Path(spec_path).resolve().parent.parent.parent


def spec_root(cfg, spec_path) -> Path:
    """The repository whose checkout and SCHEMA.md the reviewer sees. Outbound specs (in the
    outbox) use the Meta-Tools checkout; any other spec its own git toplevel, or outside git
    `<root>/docs/specs/<file>`."""
    path = Path(spec_path).resolve()
    try:
        path.relative_to(Path(SP.outbox_dir(cfg)).resolve())
    except ValueError:
        pass
    else:
        try:
            return Path(P.project_config(cfg, "Meta-Tools")["_root"])
        except (ValueError, KeyError):
            return _fallback_root(path)
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


def review_context(spec_path, spec_id, *, idea_text, root=None) -> dict:
    """The persona context: spec id, path relative to the root, spec text, SCHEMA.md text (or
    None) and the source-idea text."""
    path = Path(spec_path)
    root = Path(root) if root is not None else _fallback_root(path)
    schema_file = root / "docs" / "specs" / "SCHEMA.md"
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


def review_payload(spec_id, sha, *, model, cost_usd, verdict, root) -> dict:
    """The `wt.spec.review.v1` dict; raises ValueError on an approve carrying a must_fix."""
    _check_verdict(spec_id, verdict)
    return {
        "schema": SCHEMA, "spec_id": spec_id, "verdict": verdict["verdict"],
        "spec_sha256": sha, "reviewed_at": store.now_iso(), "model": model,
        "cost_usd": cost_usd, "findings": verdict.get("findings") or [],
        "suggested_splits": verdict.get("suggested_splits") or [],
        "repo_root": str(root),
    }


# ---- the run journal ---------------------------------------------------------------------------------

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


def journal_start(cfg, run_id, spec_id, timeout_s, run_dir) -> None:
    _journal_append(cfg, {"schema": RUN_SCHEMA, "kind": "start", "run_id": run_id,
                          "spec_id": spec_id, "ts": store.now_iso(), "timeout_s": timeout_s,
                          "run_dir": str(run_dir)})


def journal_end(cfg, run_id, spec_id, *, verdict=None, counts=None, cost_usd=None,
                duration_s=None, error=None) -> None:
    rec = {"schema": RUN_SCHEMA, "kind": "end", "run_id": run_id, "spec_id": spec_id,
           "ts": store.now_iso(), "verdict": verdict, "counts": counts, "cost_usd": cost_usd,
           "duration_s": duration_s, "error": error}
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
    for raw in _tail_lines(p, tail):
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
    for f in findings or []:
        sev = f.get("severity") or "unknown"
        c[sev] = c.get(sev, 0) + 1
    return c


def run_spec_review(cfg, spec_path, spec_id, *, profile=None, idea_text=None) -> dict:
    """Run the spec reviewer on the spec file, store the review and journal the run. A run
    error or a verdict that does not conform appends an `end` with `error`, raises ValueError
    and stores nothing."""
    path = Path(spec_path)
    sha = spec_sha256(path)
    root = spec_root(cfg, path)
    if idea_text is None:
        fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        idea_text = _idea_text(cfg, (fm or {}).get("source_idea"))
    ctx = review_context(path, spec_id, idea_text=idea_text, root=root)
    persona = PS.get_persona(PERSONA)
    prof = resolve_profile(cfg, profile)
    settings = PS.persona_settings(prof, PERSONA)
    run_id = _RUN_ID_RE.sub("-", f"{spec_id}-{store.now_iso()}-{os.getpid()}-{secrets.token_hex(2)}")
    req = PS.RunRequest(
        persona=PERSONA,
        prompt=PS.build_prompt(persona, ctx),
        system_append=PS.render(persona.invariant, ctx),
        cwd=root,
        model=str(settings.get("model") or "sonnet"),
        effort=settings.get("effort"),
        max_budget_usd=float(settings.get("max_budget_usd") or 0.8),
        timeout_s=int(prof.get("run_timeout_s") or 1800),
        permission_mode=settings.get("permission_mode") or "dontAsk",
        read_only=True,
        allowed_tools=(),
        add_dirs=(root,),
        json_schema=persona.schema,
        env={"WT_DATA_DIR": str(cfg.get("data_dir") or ""),
             "WT_CONFIG_DIR": str(cfg.get("config_dir") or ""), "WT_SPEC_ID": spec_id},
        run_dir=store.agentic_dir(cfg) / "spec-reviews" / run_id,
    )
    journal_start(cfg, run_id, spec_id, req.timeout_s, req.run_dir)
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
                                 verdict=result.verdict, root=root)
        save_review(cfg, payload)
    except Exception as e:
        journal_end(cfg, run_id, spec_id, cost_usd=cost, error=str(e),
                    duration_s=round(time.monotonic() - t0, 1))
        raise
    journal_end(cfg, run_id, spec_id, verdict=payload["verdict"],
                counts=_counts(payload["findings"]), cost_usd=payload["cost_usd"],
                duration_s=round(time.monotonic() - t0, 1))
    return payload


def review_spec(cfg, spec_id, *, profile=None) -> dict:
    """Review `spec_id` (internal or outbound) by id; see `run_spec_review`."""
    return run_spec_review(cfg, SP.resolve_spec_path(cfg, spec_id), spec_id, profile=profile)
