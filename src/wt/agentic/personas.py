"""Personas (SPEC-0174): prompt assembly and verdict schemas for the headless runs.

A persona is a markdown template shipped next to this module (`personas/<name>.md`) with
`{{placeholders}}` that `build_prompt` fills by regex substitution — never `str.format`, since
spec bodies contain braces. The prompt carries no timestamps, so identical inputs give a
byte-identical prompt (its sha is logged by the queue). Every prompt ends with the same
invariant block, which the runner also passes as `--append-system-prompt`."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from . import gitops as git
from .profiles import persona_settings
from .runner import RunRequest

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")
_TEMPLATES_DIR = Path(__file__).resolve().parent / "personas"
_GATE_TAIL_LINES = 60
_PREVIOUS_VERDICTS = 2
_DIFF_MAX_LINES = 1500

INVARIANT = """\
## wt queue invariants (read before anything else)

You are one run of wt's agentic queue for campaign `{{campaign}}` (spec `{{spec_id}}`).
- Work only inside the worktree `{{worktree}}`. Do not switch branches, do not touch the main
  checkout, do not create other worktrees.
- Do not commit. The runner commits after you finish.
- `wt campaign note "<text>"` records a note on this campaign (WT_CAMPAIGN is set).
- Finish by emitting the JSON verdict for your persona. A program parses it; nothing else
  you print is read."""

IMPLEMENTER_SCHEMA = {
    "type": "object",
    "required": ["status", "summary"],
    "properties": {
        "status": {"type": "string", "enum": ["done", "partial", "blocked"]},
        "summary": {"type": "string"},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "tests_run": {"type": "array", "items": {"type": "string"}},
        "ac_status": {"type": "array", "items": {
            "type": "object",
            "properties": {"ac": {"type": "string"}, "met": {"type": "boolean"}}}},
        "blockers": {"type": "array", "items": {"type": "string"}},
        "followup_ideas": {"type": "array", "items": {"type": "string"}},
    },
}

GATE_TRIAGER_SCHEMA = {
    "type": "object",
    "required": ["disposition", "fix_brief"],
    "properties": {
        "disposition": {"type": "string", "enum": ["fix", "retry_gate", "waive", "escalate"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "suspected_flaky": {"type": "boolean"},
        "must_fix": {"type": "array", "items": {
            "type": "object", "required": ["title"],
            "properties": {"gate": {"type": "string"}, "title": {"type": "string"},
                           "detail": {"type": "string"},
                           "files": {"type": "array", "items": {"type": "string"}}}}},
        "waived": {"type": "array", "items": {
            "type": "object",
            "properties": {"gate": {"type": "string"}, "reason": {"type": "string"}}}},
        "fix_brief": {"type": "string"},
        "followup_ideas": {"type": "array", "items": {"type": "string"}},
    },
}

CONFORMANCE_REVIEWER_SCHEMA = {
    "type": "object",
    "required": ["verdict", "score", "findings", "ac_assessment", "fix_brief"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fix"]},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "findings": {"type": "array", "items": {
            "type": "object", "required": ["severity", "title"],
            "properties": {"severity": {"type": "string",
                                        "enum": ["must_fix", "major", "minor", "nit"]},
                           "ac": {"type": "string"}, "title": {"type": "string"},
                           "detail": {"type": "string"},
                           "files": {"type": "array", "items": {"type": "string"}}}}},
        "ac_assessment": {"type": "array", "items": {
            "type": "object", "required": ["ac", "met"],
            "properties": {"ac": {"type": "string"}, "met": {"type": "boolean"},
                           "evidence": {"type": "string"}}}},
        "fix_brief": {"type": "string"},
    },
}

SPEC_REVIEW_INVARIANT = """\
## wt spec review invariants (read before anything else)

You are one run of `wt spec review` on spec `{{spec_id}}`. There is no campaign and no worktree.
- Read only: do not edit, create or delete anything.
- Finish by emitting the JSON verdict for your persona. A program parses it; nothing else
  you print is read."""

_SEVERITIES = ["must_fix", "major", "minor", "nit"]

SPEC_REVIEWER_SCHEMA = {
    "type": "object",
    "required": ["verdict", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise", "split"]},
        "findings": {"type": "array", "items": {
            "type": "object", "required": ["severity", "title"],
            "properties": {"severity": {"type": "string", "enum": _SEVERITIES},
                           "section": {"type": "string"}, "title": {"type": "string"},
                           "detail": {"type": "string"}}}},
        "suggested_splits": {"type": "array", "items": {
            "type": "object", "required": ["title"],
            "properties": {"title": {"type": "string"}, "scope": {"type": "string"}}}},
    },
}


@dataclass(frozen=True)
class Persona:
    name: str
    template_file: str
    schema: dict
    read_only: bool
    invariant: str = INVARIANT

    @property
    def template(self) -> str:
        return (_TEMPLATES_DIR / self.template_file).read_text(encoding="utf-8")


PERSONAS = {
    "implementer": Persona("implementer", "implementer.md", IMPLEMENTER_SCHEMA, read_only=False),
    "gate_triager": Persona("gate_triager", "gate_triager.md", GATE_TRIAGER_SCHEMA,
                            read_only=True),
    "conformance_reviewer": Persona("conformance_reviewer", "conformance_reviewer.md",
                                    CONFORMANCE_REVIEWER_SCHEMA, read_only=True),
    "spec_reviewer": Persona("spec_reviewer", "spec_reviewer.md", SPEC_REVIEWER_SCHEMA,
                             read_only=True, invariant=SPEC_REVIEW_INVARIANT),
}

# Personas that get the review context (diff, gate summary, previous verdicts) and may be listed
# in a profile's `reviewers` (SPEC-0201).
REVIEWER_PERSONAS = ("conformance_reviewer",)

PLACEHOLDERS = ("campaign", "spec_id", "spec_rel_path", "spec_text", "worktree", "base",
                "loop", "loop_max", "context_files", "gate_results", "pending_brief",
                "previous_verdicts", "diff", "test_files", "schema_text", "idea_text")


def get_persona(name) -> Persona:
    if name not in PERSONAS:
        raise ValueError(f"unknown persona {name!r}; known: {', '.join(sorted(PERSONAS))}")
    return PERSONAS[name]


# ---- rendering ---------------------------------------------------------------------------

def _as_text(value) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return "\n".join(f"- {_as_text(v)}" for v in value) if value else "(none)"
    text = str(value)
    return text if text.strip() else "(none)"


def render(text: str, values: dict) -> str:
    """Replace `{{name}}` with `values[name]` (`(none)` when missing/empty). Regex, not
    `str.format`, so braces in spec bodies are left alone."""
    return _PLACEHOLDER_RE.sub(lambda m: _as_text(values.get(m.group(1))), text)


def render_gate_results(last_gate) -> str:
    """name / required / result / tail (≤ 60 lines each) per gate, or `(none)`."""
    results = (last_gate or {}).get("results") or []
    if not results:
        return "(none)"
    blocks = []
    for r in results:
        status = "ok" if r.get("ok") else ("timed out" if r.get("timed_out") else "FAILED")
        req = "required" if r.get("required", True) else "optional"
        head = f"### gate `{r.get('name')}` — {status} ({req}, exit {r.get('exit_code')})"
        if r.get("cmd"):
            head += f"\ncommand: `{r['cmd']}`"
        tail_lines = (r.get("tail") or "").rstrip("\n").splitlines()[-_GATE_TAIL_LINES:]
        tail = "\n".join(tail_lines) if tail_lines else "(no output)"
        blocks.append(f"{head}\n```\n{tail}\n```")
    return "\n\n".join(blocks)


def render_pending_brief(brief) -> str:
    if not brief:
        return "(none)"
    parts = []
    if brief.get("source"):
        parts.append(f"From: {brief['source']}")
    if brief.get("fix_brief"):
        parts.append(f"Brief: {brief['fix_brief']}")
    for item in brief.get("must_fix") or []:
        if isinstance(item, dict):
            where = f" [{item.get('gate') or item.get('ac')}]" \
                if item.get("gate") or item.get("ac") else ""
            files = f" ({', '.join(item.get('files') or [])})" if item.get("files") else ""
            parts.append(f"- MUST FIX{where}: {item.get('title')}{files}"
                         + (f" — {item['detail']}" if item.get("detail") else ""))
        else:
            parts.append(f"- MUST FIX: {item}")
    for note in brief.get("human_notes") or []:
        parts.append(f"- Human note: {note}")
    return "\n".join(parts) if parts else "(none)"


def render_previous_verdicts(events) -> str:
    """Last two `run.end` events' verdict summaries, newest last."""
    lines = []
    for ev in (events or [])[-_PREVIOUS_VERDICTS:]:
        v = ev.get("verdict") or {}
        summary = v.get("summary") or v.get("fix_brief") or "(no summary)"
        head = v.get("status") or v.get("disposition") or v.get("verdict") or "?"
        lines.append(f"- {ev.get('persona', '?')} run {ev.get('run_id', '?')}: {head} — {summary}")
    return "\n".join(lines) if lines else "(none)"


def build_context(cfg, state, project_cfg=None, *, loop_max=None, previous=None,
                  review=False) -> dict:
    """Prompt values from campaign state (+ project config). `spec_text` is read from the
    worktree so the persona sees the spec as it is on the branch. `previous` = `run.end`
    ledger events (the caller reads them; this stays free of store I/O). `review` adds the
    branch `diff` (≤ 1500 lines) and the `test_files` it touches, read from the worktree."""
    repo = state.get("repo") or {}
    spec = state.get("spec") or {}
    worktree = repo.get("worktree")
    rel = spec.get("rel_path")
    spec_text = None
    if worktree and rel:
        p = Path(worktree) / rel
        if p.is_file():
            spec_text = p.read_text(encoding="utf-8")
    counters = state.get("counters") or {}
    agents = (project_cfg or {}).get("agents") or {}
    diff = test_files = None
    if review and worktree and repo.get("base"):
        try:
            diff = git.diff_text(worktree, repo["base"], _DIFF_MAX_LINES)
            test_files = [f for f in git.changed_files(worktree, repo["base"])
                          if f.startswith("tests/")]
        except (git.GitError, OSError):
            pass
    return {
        "campaign": state.get("id"),
        "spec_id": state.get("spec_id"),
        "spec_rel_path": rel,
        "spec_text": spec_text,
        "worktree": worktree,
        "base": repo.get("base"),
        "loop": counters.get("loops", 0),
        "loop_max": loop_max,
        "context_files": list(agents.get("context_files") or []),
        "gate_results": render_gate_results(state.get("last_gate")),
        "pending_brief": render_pending_brief(state.get("pending_brief")),
        "previous_verdicts": render_previous_verdicts(previous),
        "diff": diff,
        "test_files": test_files,
    }


def build_prompt(persona, ctx: dict) -> str:
    """Persona template + invariant block, both rendered from `ctx`. Deterministic."""
    p = get_persona(persona) if isinstance(persona, str) else persona
    return render(p.template, ctx).rstrip("\n") + "\n\n" + render(p.invariant, ctx) + "\n"


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


# ---- request -----------------------------------------------------------------------------

def _gate_tool_patterns(project_cfg) -> list[str]:
    out = []
    for gate in (project_cfg or {}).get("gates") or []:
        cmd = (gate.get("cmd") or "").strip()
        if cmd:
            out.append(f"Bash({cmd}*)")
    return out


def build_request(cfg, profile, project_cfg, state, persona, *, run_dir,
                  previous=None) -> RunRequest:
    """Turn profile + project config + campaign state into a `RunRequest` for `persona`.

    Model / effort / budget come from the profile's persona settings. The implementer's
    permission mode follows the project (`agents.permission_mode`, default bypassPermissions,
    so a project can opt into an allowlist); read-only personas keep the profile's mode and run
    `--restricted`. The implementer's allowlist is the project's `agents.allowed_tools` plus a
    `Bash(<gate cmd>*)` pattern per gate."""
    p = get_persona(persona)
    settings = persona_settings(profile, persona)
    repo = state.get("repo") or {}
    worktree = Path(repo.get("worktree") or ".")
    agents = (project_cfg or {}).get("agents") or {}
    read_only = bool(settings.get("read_only", p.read_only))
    if read_only:
        permission_mode = settings.get("permission_mode") or "dontAsk"
        allowed = ()
    else:
        permission_mode = agents.get("permission_mode") or settings.get("permission_mode") \
            or "bypassPermissions"
        allowed = tuple(list(agents.get("allowed_tools") or []) + _gate_tool_patterns(project_cfg))
    ctx = build_context(cfg, state, project_cfg, loop_max=profile.get("loop_max"),
                        previous=previous, review=persona in REVIEWER_PERSONAS)
    env = {
        "WT_CAMPAIGN": str(state.get("id") or ""),
        "WT_DATA_DIR": str(cfg.get("data_dir") or ""),
        "WT_CONFIG_DIR": str(cfg.get("config_dir") or ""),
        "WT_SPEC_ID": str(state.get("spec_id") or ""),
        "WT_WORKTREE": str(worktree),
        "WT_BASE": str(repo.get("base") or ""),
    }
    return RunRequest(
        persona=persona,
        prompt=build_prompt(p, ctx),
        system_append=render(INVARIANT, ctx),
        cwd=worktree,
        model=str(settings.get("model") or "sonnet"),
        effort=settings.get("effort"),
        max_budget_usd=float(settings.get("max_budget_usd") or 1.0),
        timeout_s=int(profile.get("run_timeout_s") or 1800),
        permission_mode=permission_mode,
        read_only=read_only,
        allowed_tools=allowed,
        add_dirs=(worktree,),
        json_schema=p.schema,
        env=env,
        run_dir=Path(run_dir),
    )
