"""Personas (SPEC-0174): prompt assembly and verdict schemas for the headless runs.

A persona is a markdown template shipped next to this module (`personas/<name>.md`) with
`{{placeholders}}` that `build_prompt` fills by regex substitution — never `str.format`, since
spec bodies contain braces. The prompt carries no timestamps, so identical inputs give a
byte-identical prompt (its sha is logged by the queue). Every prompt ends with the same
invariant block, which the runner also passes as `--append-system-prompt`."""
from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import flaky
from . import gitops as git
from . import persona_registry as PR
from . import store
from .gates import TARGETED_UNRESOLVED_EXITS, tmp_env
from .profiles import persona_settings
from .runner import RunRequest

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")
_TEMPLATES_DIR = Path(__file__).resolve().parent / "personas"
_GATE_TAIL_LINES = 60
_PREVIOUS_VERDICTS = 2
_DIFF_MAX_LINES = 1500
_TESTS_LIST_MAX = 200
_TEST_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)", re.MULTILINE)
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
_CHANGED_FILES_MAX = 300

INVARIANT = """\
## nw queue invariants (read before anything else)

You are one run of nightwright's agentic queue for campaign `{{campaign}}` (spec `{{spec_id}}`).
- Work only inside the worktree `{{worktree}}`. Do not switch branches, do not touch the main
  checkout, do not create other worktrees.
- Do not commit. The runner commits after you finish.
- `nw campaign note "<text>"` records a note on this campaign (NW_CAMPAIGN is set).
- Finish by emitting the JSON verdict for your persona. A program parses it; nothing else
  you print is read."""

RESOLVER_INVARIANT = """\
## nw queue invariants (read before anything else)

You are one run of nightwright's agentic queue for campaign `{{campaign}}` (spec `{{spec_id}}`),
resolving a rebase conflict on the campaign branch.
- Work only in `{{worktree}}`, on its current branch. Do not touch the main checkout.
- Allowed git: `rebase {{rebase_target}}`, `rebase --continue`, `rebase --abort`,
  `rebase --skip` (only for a commit you then list in `dropped_commits`), `add`,
  `checkout --ours/--theirs -- <path>`, `commit` (never `--amend`), `status`, `diff`, `log`,
  `show`.
- Every campaign commit must survive. The one exception: a commit whose intent was already
  delivered a different way upstream, so that keeping it would be wrong. Do not port it; list
  it in `dropped_commits` (full or abbreviated sha, from `git log <fork>..<pre-head>`) with a
  one-sentence reason. Never use this to skip a conflict you could resolve.
- Forbidden git: `push`, `merge`, `reset` (any form), `commit --amend`, `switch`,
  `checkout <branch>`, `branch -f`, `worktree`, `stash`, and anything in the main checkout.
- Finish with the rebase complete (`git status` shows no rebase in progress) or aborted.
- After a `resolved` verdict the runner commits any tracked or staged changes you leave in the
  tree. You may add commits of your own when the target needs them (a port commit), but an
  extra commit must not revert a campaign commit.
- `nw campaign note "<text>"` records a note on this campaign (NW_CAMPAIGN is set).
- Finish by emitting the JSON verdict for your persona. A program parses it; nothing else
  you print is read."""

RESOLUTION_STRATEGIES = ["both", "ours", "theirs", "regenerated", "rewritten"]

CONFLICT_RESOLVER_SCHEMA = {
    "type": "object",
    "required": ["status", "summary", "resolutions"],
    "properties": {
        "status": {"type": "string", "enum": ["resolved", "gave_up"]},
        "summary": {"type": "string"},
        "resolutions": {"type": "array", "items": {
            "type": "object", "required": ["path", "strategy", "how"],
            "properties": {"path": {"type": "string"},
                           "strategy": {"type": "string", "enum": RESOLUTION_STRATEGIES},
                           "how": {"type": "string"}}}},
        "questions": {"type": "array", "items": {"type": "string"}},
        "dropped_commits": {"type": "array", "items": {         # SPEC-0259
            "type": "object", "required": ["sha", "reason"],
            "properties": {"sha": {"type": "string", "minLength": 1},
                           "reason": {"type": "string", "minLength": 1}}}},
    },
}

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
            "type": "object", "required": ["severity", "title", "suggested_fix"],
            "properties": {"severity": {"type": "string",
                                        "enum": ["must_fix", "major", "minor", "nit"]},
                           "ac": {"type": "string"}, "title": {"type": "string"},
                           "detail": {"type": "string"},
                           "suggested_fix": {"type": "string"},
                           "acceptance": {"type": "string"},
                           "files": {"type": "array", "items": {"type": "string"}}}}},
        "ac_assessment": {"type": "array", "items": {
            "type": "object", "required": ["ac", "met"],
            "properties": {"ac": {"type": "string"}, "met": {"type": "boolean"},
                           "evidence": {"type": "string"}}}},
        "fix_brief": {"type": "string"},
    },
}

TEST_REVIEWER_SCHEMA = {
    "type": "object",
    "required": ["verdict", "score", "findings", "coverage_assessment", "fix_brief"],
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
        "coverage_assessment": {"type": "array", "items": {
            "type": "object", "required": ["ac", "covered"],
            "properties": {"ac": {"type": "string"}, "covered": {"type": "boolean"},
                           "tests": {"type": "array", "items": {"type": "string"}}}}},
        "fix_brief": {"type": "string"},
    },
}

REVIEW_TRIAGER_SCHEMA = {
    "type": "object",
    "required": ["waive", "note"],
    "properties": {
        "waive": {"type": "array", "items": {
            "type": "object", "required": ["id", "reason"],
            "properties": {"id": {"type": "string"}, "reason": {"type": "string"}}}},
        "note": {"type": "string"},
    },
}

ADVERSARIAL_CATEGORIES = ["bug", "logic", "security", "injection", "path_traversal",
                          "subprocess", "secrets", "concurrency", "error_handling",
                          "data_loss", "resource_leak", "other"]

ADVERSARIAL_REVIEWER_SCHEMA = {
    "type": "object",
    "required": ["verdict", "score", "findings", "fix_brief"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fix"]},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "findings": {"type": "array", "items": {
            "type": "object", "required": ["severity", "category", "title"],
            "properties": {"severity": {"type": "string",
                                        "enum": ["must_fix", "major", "minor", "nit"]},
                           "category": {"type": "string", "enum": ADVERSARIAL_CATEGORIES},
                           "title": {"type": "string"}, "detail": {"type": "string"},
                           "trigger": {"type": "string"},
                           "files": {"type": "array", "items": {"type": "string"}},
                           "lines": {"type": "array", "items": {"type": "string"}}}}},
        "fix_brief": {"type": "string"},
    },
}

SPEC_REVIEW_INVARIANT = """\
## nw spec review invariants (read before anything else)

You are one run of `nw spec review` on spec `{{spec_id}}`. There is no campaign and no worktree.
- Read only: do not edit, create or delete anything.
- Finish by emitting the JSON verdict for your persona. A program parses it; nothing else
  you print is read."""

SPEC_REVIEW_CAMPAIGN_INVARIANT = """\
## nw spec review invariants (read before anything else)

You are the spec-review step of campaign {{campaign}} on {{spec_id}}, in its worktree. Read only.
Finish with the JSON verdict."""

SPEC_WRITER_INVARIANT = """\
## nw spec writer invariants (read before anything else)

You are the spec writer of campaign {{campaign}} in its worktree. Edit only the spec file you are
told to. Finish with the JSON verdict."""

_SEVERITIES = ["must_fix", "major", "minor", "nit"]

SPEC_WRITER_SCHEMA = {
    "type": "object",
    "required": ["status", "summary"],
    "properties": {
        "status": {"type": "string", "enum": ["written", "blocked"]},
        "spec_path": {"type": "string"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
}

SPEC_REVIEWER_SCHEMA = {
    "type": "object",
    "required": ["verdict", "findings", "size", "tone", "rationale"],
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise", "split"]},
        "size": {"type": "string", "enum": ["xs", "s", "m", "l", "xl"]},
        "tone": {"type": "string", "enum": ["cost-effective", "high-end", "critical"]},
        "rationale": {"type": "string"},
        "findings": {"type": "array", "items": {
            "type": "object", "required": ["severity", "title", "suggested_fix"],
            "properties": {"severity": {"type": "string", "enum": _SEVERITIES},
                           "section": {"type": "string"}, "title": {"type": "string"},
                           "detail": {"type": "string"},
                           "suggested_fix": {"type": "string"},
                           "acceptance": {"type": "string"}}}},
        "suggested_splits": {"type": "array", "items": {
            "type": "object", "required": ["title"],
            "properties": {"title": {"type": "string"}, "scope": {"type": "string"}}}},
    },
}


def reviewer_schema(categories=None) -> dict:
    """SPEC-0371: `ADVERSARIAL_REVIEWER_SCHEMA` with the finding `category` enum replaced by
    `categories` plus `other` (appended only if absent). No `categories`: the default enum, which
    already ends with `other`."""
    schema = json.loads(json.dumps(ADVERSARIAL_REVIEWER_SCHEMA))
    if categories:
        enum = list(categories)
        if "other" not in enum:
            enum.append("other")
        schema["properties"]["findings"]["items"]["properties"]["category"]["enum"] = enum
    return schema


@dataclass(frozen=True)
class Persona:
    name: str
    template_file: str
    schema: dict
    read_only: bool
    invariant: str = INVARIANT
    role: str | None = None             # SPEC-0371: set for declared (non-builtin) personas
    # SPEC-0394: a declared persona's {settings, required, when, template_sha256}; builtins None
    declared: Mapping | None = None

    @property
    def template(self) -> str:
        # a declared persona carries the absolute path of its template (SPEC-0371): `dir / abs` is `abs`
        return (_TEMPLATES_DIR / self.template_file).read_text(encoding="utf-8")


# SPEC-0370: the persona registry is data (`persona_defs/*.yaml`). The YAML names code objects
# by key; these maps resolve the keys to the schemas and invariants defined in this module.
SCHEMAS = {
    "implementer": IMPLEMENTER_SCHEMA,
    "gate_triager": GATE_TRIAGER_SCHEMA,
    "conformance_reviewer": CONFORMANCE_REVIEWER_SCHEMA,
    "test_reviewer": TEST_REVIEWER_SCHEMA,
    "adversarial_reviewer": ADVERSARIAL_REVIEWER_SCHEMA,
    "spec_reviewer": SPEC_REVIEWER_SCHEMA,
    "spec_writer": SPEC_WRITER_SCHEMA,
    "review_triager": REVIEW_TRIAGER_SCHEMA,
    "conflict_resolver": CONFLICT_RESOLVER_SCHEMA,
}
INVARIANTS = {
    "default": INVARIANT,
    "spec_review": SPEC_REVIEW_INVARIANT,
    "spec_writer": SPEC_WRITER_INVARIANT,
    "resolver": RESOLVER_INVARIANT,
}

PLACEHOLDERS = ("campaign", "spec_id", "spec_rel_path", "spec_text", "worktree", "base",
                "loop", "loop_max", "context_files", "gate_results", "pending_brief",
                "previous_verdicts", "diff", "test_files", "tests_touched", "changed_files",
                "schema_text", "idea_text", "triage_input", "rebase_target", "conflicts",
                "campaign_diff", "base_diff", "landed", "regenerate", "template_text",
                "write_problems", "write_rules", "epic_text", "human_overrides", "flaky_history")

# What the registry validator needs from this module (it cannot import it).
KNOWN = {"output_schemas": frozenset(SCHEMAS), "invariants": frozenset(INVARIANTS),
         "placeholders": PLACEHOLDERS}


def _build_persona(defn) -> Persona:
    return Persona(defn.name, defn.template, SCHEMAS[defn.output_schema], defn.read_only,
                   INVARIANTS[defn.invariant])


_BUILTIN_DEFS = PR.load_builtin(KNOWN)

# Builtin layer only: user/project definitions are listed by `nw personas`, never run here.
PERSONAS = {d.name: _build_persona(d) for d in _BUILTIN_DEFS}

# Personas that get the review context (diff, gate summary, previous verdicts) and may be listed
# in a profile's `reviewers` (SPEC-0201).
REVIEWER_PERSONAS = PR.reviewer_personas(_BUILTIN_DEFS)

# SPEC-0225: how adoption classifies an open run that has no `step` in its `run.start` (written
# by an earlier runner): persona -> (step kind, review personas or None = the reviewer group).
# Every campaign persona must be here; `spec_reviewer` has no campaign ledger.
ADOPT_REGISTRY = PR.adopt_registry(_BUILTIN_DEFS)


# SPEC-0371: the `{{placeholders}}` a declared persona's template may use, per role. Derived from
# the builtin templates of the role (implementer.md; conformance/test/adversarial_reviewer.md;
# gate_triager.md) plus the always-available campaign context. `spec_reviewer`, `spec_writer`,
# `review_triager` and `conflict_resolver` are in no role's subset.
_COMMON_PLACEHOLDERS = frozenset((
    "campaign", "spec_id", "spec_rel_path", "spec_text", "worktree", "base", "loop", "loop_max",
    "context_files", "schema_text", "human_overrides"))
ROLE_PLACEHOLDERS = {
    "implementer": _COMMON_PLACEHOLDERS | {"gate_results", "pending_brief", "previous_verdicts"},
    "reviewer": _COMMON_PLACEHOLDERS | {
        "diff", "gate_results", "pending_brief", "previous_verdicts", "test_files",
        "tests_touched", "changed_files", "campaign_diff"},
    "triager": _COMMON_PLACEHOLDERS | {
        "gate_results", "pending_brief", "previous_verdicts", "triage_input", "flaky_history"},
}


def get_persona(name, project_root=None, *, user_dir=None) -> Persona:
    """A builtin persona; with `project_root` also one declared by the user or that project
    (SPEC-0371). Without it only builtins are known. `user_dir` is the user personas dir
    (default `<config dir>/personas`, as the CLI)."""
    if name in PERSONAS:
        return PERSONAS[name]
    if project_root is not None:
        from .persona_defs import load_registry
        registry = load_registry(project_root, user_dir=user_dir)
        if name in registry:
            return registry[name]
    raise ValueError(f"unknown persona {name!r}; known: {', '.join(sorted(PERSONAS))}")


def is_reviewer(name, project_root=None, *, user_dir=None) -> bool:
    """SPEC-0394: a builtin reviewer, or (with `project_root`) a persona the user or that project
    declares with `role: reviewer`. Without a root only builtins count."""
    if name in REVIEWER_PERSONAS:
        return True
    if name in PERSONAS or project_root is None:
        return False
    from .persona_defs import load_registry
    p = load_registry(project_root, user_dir=user_dir).get(name)
    return p is not None and p.role == "reviewer"


# SPEC-0394: run settings of a declared persona, lowest layer first: these nightwright defaults,
# the profile's entry for the role's builtin, the definition's own keys, the profile's explicit
# `personas.<name>` entry.
DECLARED_DEFAULTS = {"model": "sonnet", "effort": "medium", "max_budget_usd": 1.0}
ROLE_DEFAULT_PERSONA = {"reviewer": "conformance_reviewer", "implementer": "implementer",
                        "triager": "gate_triager"}


def declared_settings(profile, p: Persona) -> dict:
    """The layered settings of declared persona `p` (later layers win key by key). A read-only
    role is forced to `read_only: True`, `permission_mode: dontAsk` whatever the profile says."""
    entries = (profile or {}).get("personas") or {}
    out = dict(DECLARED_DEFAULTS)
    out.update(entries.get(ROLE_DEFAULT_PERSONA.get(p.role)) or {})
    out.update(((p.declared or {}).get("settings")) or {})
    out.update(entries.get(p.name) or {})
    if p.read_only:
        out.update(read_only=True, permission_mode="dontAsk")
    return out


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


def _summary_line(s) -> str:
    """SPEC-0284: `results: 3 failed, 120 passed; failing: a::b, c::d`, or the parse error."""
    if not isinstance(s, dict):
        return ""
    if s.get("error"):
        return f"results: could not parse {s.get('format')} results ({s['error']})"
    parts = [f"{s[k]} {k}" for k in
             ("failed", "errors", "passed", "skipped") if s.get(k)]
    line = "results: " + (", ".join(parts) or "no tests")
    if s.get("failing"):
        line += "; failing: " + ", ".join(s["failing"])
    return line


def _retries_line(r) -> str:
    """SPEC-0289: how the gate's retry ladder went, from its `attempts`; "" without retries."""
    attempts = r.get("attempts") or []
    ff = next((a for a in attempts if a.get("kind") == "failed-first"), None)
    attempts = [a for a in attempts if a.get("kind") != "failed-first"]
    retried = [a for a in attempts if a.get("attempt")]
    if ff is not None and not attempts:     # SPEC-0358: the prelude reproduced the failure
        return f"failed-first: the previous round's {len(ff.get('failing') or [])} failing ids " \
               "still fail (the rest of the gate was not run)"
    prefix = ""
    if ff is not None:                      # SPEC-0358: the prelude ran, then the normal run
        prefix = "failed-first: previous failing ids " + (
            "timed out" if ff.get("timed_out")
            else f"could not run (pytest exit {ff['exit_code']})"
            if ff.get("exit_code") in TARGETED_UNRESOLVED_EXITS else "passed")
        if not retried:
            return prefix
        prefix += "; "
    if not retried:
        return ""
    targeted = next((a for a in retried if a.get("kind") == "targeted"), None)
    fulls = [a for a in retried if a.get("kind") == "full"]
    ids = len(attempts[0].get("failing") or [])
    full = ""
    if fulls:
        n_ok = sum(1 for a in fulls if a.get("ok"))
        full = (f"full rerun {n_ok}/{len(fulls)} passed" if n_ok
                else f"full rerun {len(fulls)}/{len(fulls)} failed")
    tdesc = f"targeted rerun of {ids} ids"
    if targeted is None:
        line = full
    elif targeted.get("timed_out") or targeted.get("interrupted"):
        line = f"{tdesc} {'timed out' if targeted.get('timed_out') else 'was interrupted'}"
    elif targeted.get("exit_code") in TARGETED_UNRESOLVED_EXITS:
        line = f"{tdesc} could not run (pytest exit {targeted['exit_code']}), " \
               + (full or "no full rerun left")
    elif not targeted.get("ok"):
        line = f"{tdesc} reproduced the failure"
    elif not fulls:
        line = f"{tdesc} passed, no full rerun left (the gate stays failed)"
    elif r.get("ok"):
        line = f"{tdesc} passed, {full}"
    else:
        line = f"targeted rerun passed but {full} (order-dependent or other failure)"
    if r.get("flaky"):
        line += f" (flaky: {', '.join(r['flaky'])})"
    return prefix + "retries: " + line


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
        res = _summary_line(r.get("summary"))
        if res:
            head += f"\n{res}"
        retries = _retries_line(r)
        if retries:
            head += f"\n{retries}"
        tail_lines = (r.get("tail") or "").rstrip("\n").splitlines()[-_GATE_TAIL_LINES:]
        tail = "\n".join(tail_lines) if tail_lines else "(no output)"
        blocks.append(f"{head}\n```\n{tail}\n```")
    return "\n\n".join(blocks)


def render_flaky_history(cfg, state) -> str:
    """SPEC-0293: one line per currently failing test id that is on the cross-campaign flaky
    record, or `(none known)`. Only the ids failing in `last_gate` are looked up."""
    failing = list(dict.fromkeys(
        i for r in ((state.get("last_gate") or {}).get("results") or []) if not r.get("ok")
        for i in (r.get("summary") or {}).get("failing") or []))
    try:
        known = flaky.lookup(cfg, state.get("project") or "", failing) if failing else {}
    except (OSError, ValueError, TypeError, AttributeError):
        known = {}
    lines = []
    for tid in failing:
        e = known.get(tid)
        if not e:
            continue
        via = ", ".join(f"{label} {e['sources'][key]}" for key, label in
                        (("rerun", "via rerun"), ("triage_retry", "triage retry"))
                        if (e.get("sources") or {}).get(key))
        lines.append(f"- {tid} — flaked {e.get('count')}× in {len(e.get('campaigns') or [])} "
                     f"campaigns (last {str(e.get('last_seen') or '')[:10]}"
                     + (f"; {via}" if via else "") + ")")
    return "\n".join(lines) or "(none known)"


def finding_text(f: dict, detail=None) -> str | None:
    """`detail` (default `f['detail']`), then `Fix: …` and `Done when: …` lines for a non-blank
    `suggested_fix` / `acceptance` (SPEC-0295); None when all three are empty. Pure: a stored
    finding is never trusted to be well-typed, so non-string values are ignored."""
    def clean(value):
        return value.strip() if isinstance(value, str) and value.strip() else None
    lines = [clean(f.get("detail") if detail is None else detail)]
    lines += [f"{label}: {text}" for label, key in (("Fix", "suggested_fix"),
                                                     ("Done when", "acceptance"))
              if (text := clean(f.get(key)))]
    lines = [line for line in lines if line]
    return "\n".join(lines) or None


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
            tag = item.get("gate") or item.get("ac") or item.get("category") or item.get("section")
            where = f" [{tag}]" if tag else ""
            files = f" ({', '.join(item.get('files') or [])})" if item.get("files") else ""
            parts.append(f"- MUST FIX{where}: {item.get('title')}{files}"
                         + (f" — {item['detail']}" if item.get("detail") else ""))
        else:
            parts.append(f"- MUST FIX: {item}")
    for split in brief.get("suggested_splits") or []:     # SPEC-0215: a spec review's splits
        if isinstance(split, dict):
            scope = f" — {split['scope']}" if split.get("scope") else ""
            parts.append(f"- Suggested split: {split.get('title')}{scope}")
        else:
            parts.append(f"- Suggested split: {split}")
    for note in brief.get("human_notes") or []:
        parts.append(f"- Human note: {note}")
    return "\n".join(parts) if parts else "(none)"


def render_human_overrides(state) -> str:
    """SPEC-0235: the human's waivers and attestations of this campaign, as "do not re-raise"
    lists for the reviewers; `""` when there are none."""
    ov = (state or {}).get("human_overrides") or {}
    waive = [w for w in ov.get("waive") or [] if isinstance(w, dict)]
    attest = [a for a in ov.get("attest") or [] if isinstance(a, dict)]
    parts = []
    if waive:
        lines = ["The human waived these findings; do not raise them again, even reworded:"]
        for w in waive:
            files = f" ({', '.join(w.get('files') or [])})" if w.get("files") else ""
            lines.append(f"- {w.get('severity')} {w.get('title')}{files} — {w.get('reason')}")
        parts.append("\n".join(lines))
    if attest:
        lines = ["The human attested these ACs as met; do not assess them as unmet:"]
        lines += [f"- {a.get('ac')} — {a.get('reason')}" for a in attest]
        parts.append("\n".join(lines))
    if not parts:
        return ""
    return "## Human overrides\n\n" + "\n\n".join(parts)


def render_previous_verdicts(events) -> str:
    """Last two `run.end` events' verdict summaries, newest last."""
    lines = []
    for ev in (events or [])[-_PREVIOUS_VERDICTS:]:
        v = ev.get("verdict") or {}
        summary = v.get("summary") or v.get("fix_brief") or "(no summary)"
        head = v.get("status") or v.get("disposition") or v.get("verdict") or "?"
        lines.append(f"- {ev.get('persona', '?')} run {ev.get('run_id', '?')}: {head} — {summary}")
    return "\n".join(lines) if lines else "(none)"


# ---- tests_touched (SPEC-0205) -----------------------------------------------------------

def _is_test_file(path: str) -> bool:
    """A `.py` file under a `tests/` directory or named `test_*.py` / `*_test.py`; `conftest.py`
    holds helpers, not tests."""
    parts = path.split("/")
    name = parts[-1]
    if not name.endswith(".py") or name == "conftest.py":
        return False
    return "tests" in parts[:-1] or name.startswith("test_") or name.endswith("_test.py")


def _test_defs(source: str):
    """`({name: (start, end)}, parsed)` — the test functions of `source` with their line spans
    (decorators included). `parsed` is False when `ast.parse` failed and the regex fallback was
    used (bare names, `start == end` = the `def` line)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        lines = {}
        for m in _TEST_DEF_RE.finditer(source):
            n = source.count("\n", 0, m.start(1)) + 1
            lines[m.group(1)] = (n, n)
        return lines, False
    out = {}

    def walk(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_"):
                    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    out[f"{prefix}::{node.name}" if prefix else node.name] = \
                        (start, node.end_lineno or node.lineno)
            elif isinstance(node, ast.ClassDef):
                walk(node.body, f"{prefix}.{node.name}" if prefix else node.name)

    walk(tree.body, "")
    return out, True


def _split_diff(text: str) -> dict:
    """`{new_path: [(start, count), …]}` new-side hunk ranges from a `--unified=0` diff,
    split per file on the `diff --git` headers. A quoted or `/dev/null` path gets no entry."""
    hunks = {}
    for section in re.split(r"^diff --git ", text, flags=re.MULTILINE)[1:]:
        m = re.search(r"^\+\+\+ b/(.+)$", section, flags=re.MULTILINE)
        if not m:
            continue
        hunks[m.group(1)] = [(int(a), 1 if b is None or b == "" else int(b))
                             for a, b in _HUNK_RE.findall(section)]
    return hunks


def _hits(span, ranges) -> bool:
    start, end = span
    for s, n in ranges:
        if n == 0:
            if start - 1 <= s <= end:
                return True
        elif s <= end and s + n - 1 >= start:
            return True
    return False


def _capped(names) -> list:
    names = sorted(set(names))
    if len(names) > _TESTS_LIST_MAX:
        extra = len(names) - _TESTS_LIST_MAX
        return names[:_TESTS_LIST_MAX] + [f"\u2026 {extra} more"]
    return names


def _show(worktree, rev, path):
    try:
        return git.git(worktree, "show", f"{rev}:{path}").stdout
    except git.GitError:
        return None


def tests_touched(worktree, base) -> list[dict]:
    """Deterministic, no LLM (SPEC-0205): per test file touched between `base` and HEAD,
    `{path, status: A|M|R|D, changed: [names], removed: [names]}` sorted by path. Names are
    `test_x` or `Class::test_x` (parametrization is not expanded). `changed` = tests whose span
    (decorators included) meets a new-side hunk; `removed` = tests of the base version absent from
    the new one. Lists are capped at 200 plus a trailing `"… N more"`. A `GitError` gives `[]`."""
    try:
        raw = git.git(worktree, "-c", "core.quotepath=off", "diff", "-M", "--name-status", "-z",
                      f"{base}...HEAD").stdout
        full = git.git(worktree, "-c", "core.quotepath=off", "diff", "-M", "--unified=0",
                       f"{base}...HEAD").stdout
        merge_base = git.git(worktree, "merge-base", base, "HEAD").stdout.strip()
    except git.GitError:
        return []
    hunks = _split_diff(full)
    toks = raw.split("\0")
    entries, i = [], 0
    while i < len(toks) and toks[i]:
        code = toks[i][0]
        if code in "RC":
            old, new, i = toks[i + 1], toks[i + 2], i + 3
            similarity = toks[i - 3][1:]
        else:
            old = new = toks[i + 1]
            i += 2
            similarity = ""
        status = {"T": "M", "C": "A"}.get(code, code)
        if status == "A":
            old = None
        if status not in ("A", "M", "R", "D"):
            continue
        old_test = old is not None and _is_test_file(old)
        new_test = status != "D" and _is_test_file(new)
        if not (old_test or new_test):
            continue
        changed, removed = [], []
        old_defs, old_parsed = {}, True
        if old_test:
            src = _show(worktree, merge_base, old)
            if src is not None:
                old_defs, old_parsed = _test_defs(src)
        if new_test:
            src = _show(worktree, "HEAD", new)
            new_defs, new_parsed = _test_defs(src) if src is not None else ({}, True)
            ranges = hunks.get(new)
            if status == "A" or (status == "R" and not old_test):
                changed = list(new_defs)
            elif status == "R" and similarity == "100":
                changed = []
            elif ranges is None:                  # quoted path: no hunk info
                changed = list(new_defs)
            else:
                changed = [n for n, span in new_defs.items() if _hits(span, ranges)]
            if old_parsed and new_parsed:
                removed = [n for n in old_defs if n not in new_defs]
            else:
                keep = {n.rsplit("::", 1)[-1] for n in new_defs}
                removed = [n for n in old_defs if n.rsplit("::", 1)[-1] not in keep]
        else:
            removed = list(old_defs)
        entries.append({"path": new if status != "D" else old, "status": status,
                        "changed": _capped(changed), "removed": _capped(removed)})
    return sorted(entries, key=lambda e: e["path"])


def _changed_files_text(paths) -> str:
    """Sorted paths, one per line: the first 300, then `… N more` (SPEC-0202); `(none)` if empty."""
    paths = sorted(paths)
    if not paths:
        return "(none)"
    shown = paths[:_CHANGED_FILES_MAX]
    if len(paths) > _CHANGED_FILES_MAX:
        shown.append(f"… {len(paths) - _CHANGED_FILES_MAX} more")
    return "\n".join(shown)


# ---- resolver context (SPEC-0221) -----------------------------------------------------------

_RESOLVE_DIFF_MAX_LINES = 600
_RESOLVE_SPECS_MAX = 3
_RESOLVE_SPEC_MAX_LINES = 400
_RESOLVE_LANDED_MAX = 50
_SPEC_ID_RE = re.compile(r"SPEC-(\d{4,})")
_SPEC_FILE_RE = re.compile(r"^docs/specs/(\d{4,})-[^/]*\.md$")


def _cut(text: str, max_lines: int, hint: str) -> str:
    lines = (text or "").splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[:max_lines] + [f"\u2026 truncated: {len(lines) - max_lines} more "
                                          f"line(s); run `{hint}` for the rest"])


def _git_text(worktree, *args) -> str:
    try:
        return git.git(worktree, *args).stdout
    except (git.GitError, OSError):
        return ""


def resolve_context(state, resolve, project_cfg) -> dict:
    """Prompt values of the conflict resolver (SPEC-0221) from git reads in the worktree,
    which sits at `resolve["pre_head"]` (the close rebase was aborted). Every range starts at
    `resolve["fork"]`, the merge-base of the campaign branch and the target, never at the
    sha the campaign opened on."""
    worktree = (state.get("repo") or {}).get("worktree")
    spec_rel = (state.get("spec") or {}).get("rel_path")
    resolve = resolve or {}
    fork, pre_head = resolve.get("fork"), resolve.get("pre_head")
    target_sha = resolve.get("target_sha")
    conflicts = list(resolve.get("conflicts") or [])
    regen = (project_cfg or {}).get("merge", {}).get("regenerate") or []
    out = {"rebase_target": target_sha, "conflicts": conflicts,
           "regenerate": [f"{r['path']}: {r['cmd']}" for r in regen] or None}
    if not (worktree and fork and pre_head and target_sha and conflicts):
        return out
    out["campaign_diff"] = _cut(
        _git_text(worktree, "diff", f"{fork}..{pre_head}", "--", *conflicts),
        _RESOLVE_DIFF_MAX_LINES, f"git diff {fork}..{pre_head} -- <conflicts>")
    out["base_diff"] = _cut(
        _git_text(worktree, "diff", f"{fork}..{target_sha}", "--", *conflicts),
        _RESOLVE_DIFF_MAX_LINES, f"git diff {fork}..{target_sha} -- <conflicts>")
    log = [ln.split("\t", 1) for ln in _git_text(
        worktree, "log", "--format=%H%x09%s", f"{fork}..{target_sha}", "--", *conflicts
    ).splitlines() if "\t" in ln]
    lines = [f"- {sha[:10]} {subject}" for sha, subject in log[:_RESOLVE_LANDED_MAX]]
    if len(log) > _RESOLVE_LANDED_MAX:
        lines.append(f"\u2026 {len(log) - _RESOLVE_LANDED_MAX} more commit(s)")
    # the governing specs of the base side: ids named in subjects, spec files the range touched
    changed = [p for p in _git_text(worktree, "diff", "--name-only", "--diff-filter=AM",
                                    f"{fork}..{target_sha}", "--", "docs/specs").splitlines()
               if _SPEC_FILE_RE.match(p) and p != spec_rel]
    by_num = {_SPEC_FILE_RE.match(p).group(1): p for p in changed}
    files, tree = [], None
    for _sha, subject in log:
        if len(files) >= _RESOLVE_SPECS_MAX:
            break
        for num in _SPEC_ID_RE.findall(subject):
            hit = by_num.get(num)
            if hit is None:
                if tree is None:        # one listing of the target's specs, only when needed
                    tree = {}
                    for p in _git_text(worktree, "ls-tree", "-r", "--name-only", target_sha,
                                       "--", "docs/specs").splitlines():
                        m = _SPEC_FILE_RE.match(p)
                        if m and p != spec_rel:
                            tree.setdefault(m.group(1), p)
                hit = tree.get(num)
            if hit and hit not in files:
                files.append(hit)
    files += [p for p in sorted(changed) if p not in files]
    for path in files[:_RESOLVE_SPECS_MAX]:
        text = _show(worktree, target_sha, path)
        if text is not None:
            lines += ["", f"### Governing spec `{path}` (as on the target)", "```markdown",
                      _cut(text, _RESOLVE_SPEC_MAX_LINES, f"git show {target_sha}:{path}"),
                      "```"]
    out["landed"] = "\n".join(lines) if lines else None
    return out


def persona_extra(state, persona, project_cfg) -> dict | None:
    """Persona-specific prompt values built from campaign state (resolver only)."""
    if persona != "conflict_resolver":
        return None
    return resolve_context(state, (state.get("closing") or {}).get("resolve"), project_cfg)


RED_FIX_MARK = "RED-BASE FIX (no spec)"           # SPEC-0420: opens a fix-forward's "spec"


def build_context(cfg, state, project_cfg=None, *, loop_max=None, previous=None,
                  review=False, with_tests=False, extra=None) -> dict:
    """Prompt values from campaign state (+ project config). `spec_text` is read from the
    worktree so the persona sees the spec as it is on the branch. `previous` = `run.end`
    ledger events (the caller reads them; this stays free of store I/O). `review` adds the
    branch `diff` (≤ 1500 lines) and the `test_files` it touches, read from the worktree;
    `with_tests` adds `tests_touched` (SPEC-0205), pre-rendered as sorted JSON. `extra` = more
    prompt values (SPEC-0206 `triage_input`), merged last."""
    repo = state.get("repo") or {}
    spec = state.get("spec") or {}
    worktree = repo.get("worktree")
    rel = spec.get("rel_path")
    spec_text = None
    if worktree and rel:
        p = Path(worktree) / rel
        if p.is_file():
            spec_text = p.read_text(encoding="utf-8")
    spec_id = state.get("spec_id")
    if state.get("kind") == "fixforward":
        # SPEC-0420: a red-base fix has no spec; the brief takes the spec's place
        spec_id = f"(none: red-base fix, episode {state.get('red_episode')})"
        spec_text = RED_FIX_MARK + "\n\n" + str(state.get("red_brief") or "(no brief)")
    counters = state.get("counters") or {}
    agents = (project_cfg or {}).get("agents") or {}
    diff = test_files = changed_files = None
    if review and worktree and repo.get("base"):
        try:
            d = git.diff_text(worktree, repo["base"], _DIFF_MAX_LINES)
            changed = sorted(git.changed_files(worktree, repo["base"]))
            diff, test_files = d, [f for f in changed if f.startswith("tests/")]
            changed_files = _changed_files_text(changed)
        except (git.GitError, OSError):
            pass
    touched = None
    if with_tests and worktree and repo.get("base"):
        entries = tests_touched(worktree, repo["base"])
        touched = json.dumps(entries, indent=2, sort_keys=True) if entries else None
    ctx = {
        "campaign": state.get("id"),
        "spec_id": spec_id,
        "spec_rel_path": rel,
        "spec_text": spec_text,
        "worktree": worktree,
        "base": repo.get("base"),
        "loop": counters.get("loops", 0),
        "loop_max": loop_max,
        "context_files": list(agents.get("context_files") or []),
        "gate_results": render_gate_results(state.get("last_gate")),
        "flaky_history": render_flaky_history(cfg, state),
        "pending_brief": render_pending_brief(state.get("pending_brief")),
        "previous_verdicts": render_previous_verdicts(previous),
        "human_overrides": render_human_overrides(state),
        "diff": diff,
        "test_files": test_files,
        "tests_touched": touched,
        "changed_files": changed_files,
    }
    ctx.update(extra or {})
    return ctx


def output_section(p: Persona) -> str:
    """SPEC-0394: the generated `## Output` block of a declared reviewer, read from its schema
    (so the category list is exactly what `--json-schema` enforces)."""
    props = p.schema["properties"]
    item = props["findings"]["items"]
    fprops = item["properties"]
    optional = [k for k in fprops if k not in item["required"]]
    code = lambda xs, sep: sep.join(f"`{x}`" for x in xs)                      # noqa: E731
    return "\n".join([
        "## Output",
        "",
        "Finish with ONE JSON object and nothing after it:",
        "",
        f"- `verdict`: {code(props['verdict']['enum'], ' | ')} (`fix` when any finding must "
        "be addressed)",
        f"- `score`: integer {props['score']['minimum']}..{props['score']['maximum']}",
        "- `findings`: a list; each finding has",
        f"  - `severity`: {code(fprops['severity']['enum'], ' | ')}",
        f"  - `category`: one of {code(fprops['category']['enum'], ', ')}",
        "  - `title`: one line",
        f"  - optionally {code(optional, ', ')}",
        "- `fix_brief`: what the implementer should change (empty when `verdict` is `pass`)",
    ])


def build_prompt(persona, ctx: dict, invariant: str | None = None) -> str:
    """Persona template + invariant block, both rendered from `ctx`. Deterministic.
    `invariant` replaces the persona's own block (SPEC-0214). A declared reviewer gets the
    generated Output section between the two (SPEC-0394)."""
    p = get_persona(persona) if isinstance(persona, str) else persona
    inv = p.invariant if invariant is None else invariant
    body = render(p.template, ctx).rstrip("\n")
    if p.declared is not None and p.role == "reviewer":
        body += "\n\n" + output_section(p)
    return body + "\n\n" + render(inv, ctx) + "\n"


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
                  previous=None, extra=None, invariant=None) -> RunRequest:
    """Turn profile + project config + campaign state into a `RunRequest` for `persona`.

    Model / effort / budget come from the profile's persona settings. The implementer's
    permission mode follows the project (`agents.permission_mode`, default bypassPermissions,
    so a project can opt into an allowlist); read-only personas keep the profile's mode and run
    `--restricted`. The implementer's allowlist is the project's `agents.allowed_tools` plus a
    `Bash(<gate cmd>*)` pattern per gate. `extra` = additional prompt values (`triage_input`).
    `invariant` overrides the persona's invariant block (the in-campaign spec reviewer).

    SPEC-0394: the persona is resolved against the project's main checkout
    (`project_cfg["_root"]`), so a declared persona is found and a campaign worktree's
    `.nw/personas/` is never read; its settings are layered by `declared_settings`."""
    from .persona_defs import user_personas_dir
    project_root = (project_cfg or {}).get("_root")
    user_dir = user_personas_dir(cfg)
    p = get_persona(persona, project_root, user_dir=user_dir)
    settings = persona_settings(profile, persona) if persona in PERSONAS \
        else declared_settings(profile, p)
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
    extra = {**(persona_extra(state, persona, project_cfg) or {}), **(extra or {})} or None
    ctx = build_context(cfg, state, project_cfg, loop_max=profile.get("loop_max"),
                        previous=previous,
                        review=persona in REVIEWER_PERSONAS
                        or (p.declared is not None and p.role == "reviewer"),
                        with_tests=persona == "test_reviewer", extra=extra)
    env = {
        "NW_CAMPAIGN": str(state.get("id") or ""),
        "NW_DATA_DIR": str(cfg.get("data_dir") or ""),
        "NW_CONFIG_DIR": str(cfg.get("config_dir") or ""),
        "NW_SPEC_ID": str(state.get("spec_id") or ""),
        "NW_WORKTREE": str(worktree),
        "NW_BASE": str(repo.get("base") or ""),
        "NW_PERSONA_RUN": "1",     # SPEC-0272: org writes refuse inside an agent run
        "NW_PERSONA": str(persona),   # SPEC-0425: `nw campaign note` attributes to it
    }
    cid = str(state.get("id") or "")
    if cid:           # SPEC-0253: a private TMPDIR per run, removed when its result is recorded
        env.update(tmp_env(store.child_tmp(cfg, cid, f"run-{Path(run_dir).name}", create=True)))
    if persona == "conflict_resolver":
        env["NW_REBASE_TARGET"] = str(ctx.get("rebase_target") or "")
    return RunRequest(
        persona=persona,
        prompt=build_prompt(p, ctx) if invariant is None else build_prompt(p, ctx, invariant),
        system_append=render(p.invariant if invariant is None else invariant, ctx),
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
        profile=profile.get("name"),
        profile_sources=tuple(profile.get("sources") or ()),
    )
