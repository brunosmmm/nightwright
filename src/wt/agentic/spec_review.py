"""Spec review before admission (SPEC-0188): a read-only `spec_reviewer` persona reads a spec,
its source idea and the spec schema, and its verdict is stored as a sidecar
`docs/specs/reviews/<SPEC-ID>.json` keyed by the spec file's sha256 — so any later edit to the
spec invalidates the review by itself. `wt queue add` admits a spec only on a matching
`approve`."""
from __future__ import annotations

import hashlib
import json
import os
import re
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
_RUN_ID_RE = re.compile(r"[^A-Za-z0-9_.-]")


def spec_sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sidecar_path(spec_path, spec_id) -> Path:
    """`<dir of the spec>/reviews/<SPEC-ID>.json`."""
    return Path(spec_path).parent / REVIEWS_DIRNAME / f"{spec_id}.json"


def load_review(spec_path, spec_id):
    """The stored review payload, or None when absent or unreadable."""
    p = sidecar_path(spec_path, spec_id)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def check_admissible(spec_path, spec_id) -> dict:
    """The approving review for the spec's current text, or ValueError with the fix."""
    review = load_review(spec_path, spec_id)
    if (not review or review.get("verdict") != "approve"
            or review.get("spec_sha256") != spec_sha256(spec_path)):
        raise ValueError(f"{spec_id} has no approving review for its current text; "
                         f"run wt spec review {spec_id} (or --force)")
    return review


def _repo_root(cfg, spec_path) -> Path:
    try:
        return Path(P.project_config(cfg, "Meta-Tools")["_root"])
    except (ValueError, KeyError):
        return Path(spec_path).resolve().parent.parent.parent


def _idea_text(cfg, idea_id) -> str:
    if not idea_id:
        return "(none: the spec names no source idea)"
    from ..explore import idea_show_payload
    from ..report import _idea_by_id
    task = _idea_by_id(cfg, idea_id)
    if task is None:
        return f"(source idea {idea_id} not found)"
    return json.dumps(idea_show_payload(cfg, task), indent=2, ensure_ascii=False)


def _check_verdict(spec_id, verdict) -> None:
    findings = verdict.get("findings") or []
    if verdict["verdict"] == "approve" and any(f.get("severity") == "must_fix" for f in findings):
        raise ValueError(f"spec review of {spec_id} is `approve` but lists a must_fix finding")


def review_spec(cfg, spec_id, *, profile=None) -> dict:
    """Run the spec reviewer on `spec_id`, write the sidecar atomically and return it. A run
    error or a verdict that does not conform raises ValueError and writes nothing."""
    path = SP.resolve_spec_path(cfg, spec_id)
    text = path.read_text(encoding="utf-8")
    sha = spec_sha256(path)
    fm, _ = split_frontmatter(text)
    root = _repo_root(cfg, path)
    schema_file = root / "docs" / "specs" / "SCHEMA.md"
    schema_text = schema_file.read_text(encoding="utf-8") if schema_file.is_file() else None
    try:
        rel = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        rel = str(path)
    ctx = {"spec_id": spec_id, "spec_rel_path": rel, "spec_text": text,
           "schema_text": schema_text, "idea_text": _idea_text(cfg, (fm or {}).get("source_idea"))}
    persona = PS.get_persona(PERSONA)
    prof = resolve_profile(cfg, profile)
    settings = PS.persona_settings(prof, PERSONA)
    run_id = _RUN_ID_RE.sub("-", f"{spec_id}-{store.now_iso()}")
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
    result = select_runner(cfg).run(req)
    if result.timed_out or result.is_error or not result.verdict_ok:
        why = ("timed out" if result.timed_out else "failed" if result.is_error
               else "returned a verdict that does not match the schema")
        raise ValueError(f"spec review of {spec_id} {why} (see {req.run_dir})")
    verdict = result.verdict
    _check_verdict(spec_id, verdict)
    payload = {
        "schema": SCHEMA, "spec_id": spec_id, "verdict": verdict["verdict"],
        "spec_sha256": sha, "reviewed_at": store.now_iso(), "model": req.model,
        "cost_usd": result.cost_usd, "findings": verdict.get("findings") or [],
        "suggested_splits": verdict.get("suggested_splits") or [],
    }
    out = sidecar_path(path, spec_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, out)
    return payload
