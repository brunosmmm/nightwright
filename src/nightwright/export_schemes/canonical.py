"""CanonicalSpec (SPEC-0016): a scheme-agnostic parse of an outbound spec's frontmatter +
body sections — the model every `ExportScheme.render` works from, so schemes don't each
re-implement frontmatter/section parsing."""
import re
from dataclasses import dataclass, field

_BULLET_RE = re.compile(r"^\s*-\s*(?:\[[ xX]\]\s*)?(.+?)\s*$")
_BULLET_START_RE = re.compile(r"^\s*-\s")
_OUTCOME_AC_RE = re.compile(
    r"^\s*(?:I|User|Operator|Stakeholder|We)\s+can\b", re.I)


def has_outcome_signal(spec) -> bool:
    """True if CanonicalSpec has product-fit signal (SPEC-0048): non-empty `outcome:`
    frontmatter or an acceptance bullet that looks outcome-shaped (\"I can …\" / \"User can …\").
    """
    if (getattr(spec, "outcome", None) or "").strip():
        return True
    for item in getattr(spec, "acceptance", None) or []:
        text = re.sub(r"^\[.\]\s*", "", str(item)).strip()
        text = re.sub(r"^\*\*?|\*\*?$", "", text).strip()
        if _OUTCOME_AC_RE.match(text):
            return True
    return False


def missing_outcome_problem(spec) -> str | None:
    """Human-readable problem if outbound should carry outcome signal, else None."""
    if has_outcome_signal(spec):
        return None
    return ("missing product-fit outcome signal: set frontmatter outcome: or add an "
            "Acceptance criterion starting with 'I can' / 'User can' / 'Operator can' "
            "(SPEC-0048); pass --force on export to bypass")



def _merge_wrapped_lines(text):
    """Collapse a hand-wrapped bullet list into one logical line per bullet: a non-blank line
    that doesn't itself start a new bullet (`- …`) is a wrapped continuation of the previous
    line, joined with a space rather than silently dropped (bug found in real specs — e.g.
    EXAMPLE-0030's Goals section wraps `- Every batch … and the\n  consumer PR fan-in row …`
    across two physical lines). Blank lines reset continuation (they mark a paragraph break,
    e.g. between `**Goals**` and `**Non-goals**`)."""
    out = []
    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            out.append("")
            continue
        if _BULLET_START_RE.match(raw) or not out or not out[-1]:
            out.append(stripped)
        else:
            out[-1] = f"{out[-1]} {stripped}"
    return out


def _bullets(text):
    """Bullet lines of a section's body, checkbox/markup stripped, in order (same shape as
    nightwright.specs._bullet_items, duplicated here to keep export_schemes decoupled from the
    internal spec-generation module)."""
    out = []
    for line in _merge_wrapped_lines(text):
        m = _BULLET_RE.match(line)
        if m:
            out.append(m.group(1))
    return out


def _split_goals(text):
    """Split a '## Goals / Non-goals' section body (rendered as '**Goals**\\n- …\\n\\n
    **Non-goals**\\n- …') into (goals, non_goals) bullet lists."""
    parts = re.split(r"\*\*Non-goals\*\*", text or "", maxsplit=1)
    goals = _bullets(re.sub(r"\*\*Goals\*\*", "", parts[0]))
    non_goals = _bullets(parts[1]) if len(parts) > 1 else []
    return goals, non_goals


@dataclass
class CanonicalSpec:
    id: str
    title: str
    kind: str
    context: str
    goals: list
    non_goals: list
    decision: str
    design: str
    acceptance: list
    test_plan: str
    depends_on: list
    # Epic-only: child task titles from '## Breakdown / sub-specs'.
    breakdown: list = field(default_factory=list)
    # Target-oriented extras a scheme like export-scheme wants but nightwright usually can't know;
    # surfaced from optional frontmatter when present, else left blank (the scheme marks
    # them <!-- fill --> rather than guessing).
    scope_paths: str = ""
    verification_commands: str = ""
    outcome: str = ""
    do: str = ""
    dont: str = ""
    test_bar: str = ""


def parse_canonical(fm, body, sections_mod):
    """Parse an outbound spec's frontmatter (`fm`, already split) + `body` into a
    `CanonicalSpec`. `sections_mod` is a module exposing `.sections()` — pass `nightwright.specmeta`
    (the runtime-safe parser SPEC-0001 factored out of `tools/spec_lint.py`, which requires
    `jsonschema`, a dev-only dependency, and must never be imported at runtime)."""
    secs = sections_mod.sections(body)
    goals, non_goals = _split_goals(secs.get("Goals / Non-goals", ""))
    design = secs.get("Design") or secs.get("Architecture / cross-cutting design", "")
    return CanonicalSpec(
        id=fm.get("id", ""),
        title=fm.get("title", ""),
        kind=fm.get("kind", "feature"),
        context=(secs.get("Context") or "").strip(),
        goals=goals,
        non_goals=non_goals,
        decision=(secs.get("Decision") or "").strip(),
        design=design.strip(),
        acceptance=_bullets(secs.get("Acceptance criteria", "")),
        test_plan=(secs.get("Test plan") or "").strip(),
        depends_on=list(fm.get("depends_on") or []),
        breakdown=_bullets(secs.get("Breakdown / sub-specs", "")),
        scope_paths=fm.get("scope_paths", ""),
        verification_commands=fm.get("verification_commands", ""),
        outcome=fm.get("outcome", ""),
        do=fm.get("do", ""),
        dont=fm.get("dont", ""),
        test_bar=fm.get("test_bar", ""),
    )
