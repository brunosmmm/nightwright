"""Lint outbound specs (`<data_dir>/outbox/<project>/<PROJ>-NNNN-slug.md`) before export
(SPEC-0561).

The one validator behind `nw spec lint` and `python3 tools/spec_lint.py --outbox PATH`. Stdlib +
PyYAML only (no `jsonschema`, a dev-only dependency) so the installed `nw` can import it. Read-only.

`specs.validate_outbound` stays the light scaffold-time presence check; this is the full
validator: frontmatter/ids/statuses, required sections, a real Test plan, placeholders, duplicate
ids, and the resolved export scheme's own `validate` (so a spec that passes here is never one a
non-forced `nw spec export` rejects). Rule numbers in the comments below are SPEC-0561's.
"""
import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import yaml

from . import export_schemes as ES
from . import specmeta
from .export_schemes.canonical import (has_outcome_signal, missing_outcome_problem,
                                       parse_canonical)
from .specs import _proj_code

SPEC_FILE_RE = re.compile(r"^[A-Za-z0-9]+-\d{4}-.+\.md$")
_PREFIX_RE = re.compile(r"^([A-Za-z0-9]+-\d{4})-")
_ID_RE = re.compile(r"^[A-Z0-9]+-\d{4}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NA_RE = re.compile(r"(?i)^n/?a\b")  # same check as tools/spec_lint.py (NA_RE)

STATUSES = ("draft", "proposed", "accepted", "in-progress", "done", "superseded",
            "rejected", "shipped")
KINDS = ("feature", "epic", "policy", "spike")
PLACEHOLDER_STATUSES = ("accepted", "in-progress", "done", "shipped")
REQUIRED_KEYS = ("id", "title", "status", "owner", "created", "target_project")
REQUIRED_SECTIONS = ("Context", "Decision", "Acceptance criteria", "Test plan",
                     "Definition of done")
_REF_KEYS = ("depends_on", "supersedes", "superseded_by")
_PRODUCT_FIT = "product-fit outcome"


class Problem(NamedTuple):
    file: str
    severity: str  # "error" | "warning"
    code: str
    message: str


@dataclass
class FileReport:
    id: str
    path: str
    errors: list = field(default_factory=list)  # [Problem]
    warnings: list = field(default_factory=list)

    def to_dict(self):
        def one(p):
            return {"code": p.code, "message": p.message}
        return {"id": self.id, "path": self.path,
                "errors": [one(p) for p in self.errors],
                "warnings": [one(p) for p in self.warnings]}


@dataclass
class LintReport:
    files: list = field(default_factory=list)  # [FileReport]

    @property
    def ok(self):
        return not any(f.errors for f in self.files)

    @property
    def n_errors(self):
        return sum(len(f.errors) for f in self.files)

    @property
    def n_warnings(self):
        return sum(len(f.warnings) for f in self.files)

    def exit_code(self, strict=False):
        if not self.ok or (strict and self.n_warnings):
            return 1
        return 0

    def to_dict(self):
        return {"ok": self.ok, "files": [f.to_dict() for f in self.files]}

    def format_text(self):
        lines = []
        for f in self.files:
            for p in f.errors + f.warnings:
                lines.append(f"{p.file}: {p.severity} [{p.code}] {p.message}")
        lines.append(f"{len(self.files)} spec(s) linted: {self.n_errors} error(s), "
                     f"{self.n_warnings} warning(s)")
        return "\n".join(lines)


def spec_files(directory):
    """Lintable spec files directly in `directory` (INDEX.md, PROVENANCE.md and
    `*.contract.md` are not specs)."""
    return sorted(p for p in Path(directory).glob("*.md")
                  if p.is_file() and SPEC_FILE_RE.match(p.name)
                  and not p.name.endswith(".contract.md"))


def _prefix(name):
    m = _PREFIX_RE.match(name)
    return m.group(1) if m else None


def _frontmatter(text):
    """(fm, body, error). Malformed YAML or a non-mapping block is an error, never a raise."""
    try:
        fm, body = specmeta.split_frontmatter(text)
    except yaml.YAMLError as e:
        first = str(e).strip().splitlines()[0] if str(e).strip() else "parse error"
        return None, "", f"frontmatter is not valid YAML: {first}"
    if fm is None:
        return None, body, "no YAML frontmatter block"
    if not isinstance(fm, dict):
        return None, body, "frontmatter is not a mapping"
    return fm, body, None


def _sibling_ids(siblings):
    out = {}
    for s in siblings:
        try:
            fm, _, err = _frontmatter(Path(s).read_text())
        except (OSError, UnicodeDecodeError):
            continue
        out[Path(s)] = fm.get("id") if not err and isinstance(fm.get("id"), str) else None
    return out


def _resolve_scheme(cfg, fm):
    """The scheme export would pick for this spec (no explicit --scheme): keyed by the spec's
    `target_project` in `cfg['outbox_targets']`. Without a cfg only nw-native is known."""
    if cfg is None:
        return ES.get("nw-native")
    from .export import resolve_scheme
    return resolve_scheme((cfg.get("outbox_targets") or {}).get(fm.get("target_project"), {}),
                          None)


def lint_file(path, *, cfg=None, siblings=None):
    """Lint one outbound spec file -> list[Problem]. `siblings` (paths) are the project's other
    spec files for the duplicate check (rule 9); default: the other spec files in this file's
    directory, so linting one file still finds duplicates."""
    path = Path(path)
    name = path.name
    problems = []

    def add(sev, code, msg):
        problems.append(Problem(name, sev, code, msg))

    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        add("error", "frontmatter", f"cannot read file: {e}")
        return problems
    fm, body, err = _frontmatter(text)
    if err:  # 1
        add("error", "frontmatter", err)
        return problems

    # 2 required keys
    for key in REQUIRED_KEYS:
        v = fm.get(key)
        if v is None or (isinstance(v, str) and not v.strip()):
            add("error", "required-keys", f"missing required frontmatter '{key}'")
    created = fm.get("created")
    if created is not None and not (
            isinstance(created, dt.date)
            or (isinstance(created, str) and _DATE_RE.match(created.strip()))):
        add("error", "required-keys", f"'created' is not an ISO date: {created!r}")

    # 3 id format / filename / project
    sid = fm.get("id")
    project_dir = path.parent.name
    if sid is not None:
        if not isinstance(sid, str) or not _ID_RE.match(sid):
            add("error", "id-format", f"id {sid!r} does not match PROJ-NNNN")
        else:
            file_prefix = _prefix(name)
            if file_prefix and file_prefix.split("-")[1] != sid.split("-")[1]:
                add("error", "id-format",
                    f"id {sid!r} does not match filename number {file_prefix.split('-')[1]}")
            if sid.split("-")[0] != _proj_code(project_dir):
                add("error", "id-format",
                    f"id prefix {sid.split('-')[0]!r} does not match project directory "
                    f"{project_dir!r} (expected {_proj_code(project_dir)})")
    tp = fm.get("target_project")
    if isinstance(tp, str) and tp.strip() and tp != project_dir:
        add("error", "id-format",
            f"target_project {tp!r} does not match project directory {project_dir!r}")

    # 4 status / kind
    status = fm.get("status")
    if status is not None and status not in STATUSES:
        add("error", "status", f"status {status!r} is not one of {', '.join(STATUSES)}")
    if status == "superseded" and not fm.get("superseded_by"):
        add("error", "status", "status 'superseded' requires superseded_by")
    kind = fm.get("kind")
    if kind is not None and kind not in KINDS:
        add("error", "status", f"kind {kind!r} is not one of {', '.join(KINDS)}")
    kind = kind if kind in KINDS else "feature"

    # 5 sections
    secs = specmeta.sections(body)
    for sec in REQUIRED_SECTIONS:
        if sec not in secs:
            add("error", "sections", f"missing required section '## {sec}'")
        elif not secs[sec].strip():
            add("error", "sections", f"section '## {sec}' is empty")

    # 6 test plan
    tplan = secs.get("Test plan", "").strip()
    if tplan and kind in ("feature", "epic") and _NA_RE.match(tplan):
        add("error", "test-plan",
            f"kind '{kind}' needs a real Test plan (N/A only for policy/spike)")

    # 7 placeholders
    if status in PLACEHOLDER_STATUSES:
        hit = specmeta.first_placeholder(text)
        if hit:
            add("error", "placeholder",
                f"status '{status}' but line {hit[0]} is a template placeholder: {hit[1]}")

    # 8 unchecked boxes. A warning, not an error: `nw spec pull-status` mirrors only the target
    # repo's frontmatter status into the outbox copy, never its ticked boxes, so every real
    # pulled-back done/shipped spec would otherwise fail (found in the real-outbox pass).
    if status in ("done", "shipped"):
        for sec in ("Acceptance criteria", "Definition of done"):
            if re.search(r"^\s*-\s*\[ \]", secs.get(sec, ""), re.M):
                add("warning", "unchecked",
                    f"status '{status}' but '## {sec}' has unchecked boxes")

    # 9 duplicates within the project directory
    if siblings is None:
        siblings = [p for p in spec_files(path.parent)]
    others = {Path(s): i for s, i in _sibling_ids(
        [s for s in siblings if Path(s).resolve() != path.resolve()]).items()}
    my_prefix = _prefix(name)
    for other, oid in sorted(others.items()):
        if isinstance(sid, str) and oid == sid:
            add("error", "duplicate", f"duplicate id {sid} (also in {other.name})")
        elif my_prefix and _prefix(other.name) == my_prefix:
            add("error", "duplicate", f"duplicate number prefix {my_prefix} (also in {other.name})")

    # 10 frozen target_spec_path
    tsp = fm.get("target_spec_path")
    if tsp and Path(str(tsp)).name != name:
        add("error", "target-spec-path",
            f"target_spec_path basename {Path(str(tsp)).name!r} does not match {name!r}")

    # 11 resolved scheme validate (+ 12 outcome)
    outcome_warn = None
    try:
        scheme = _resolve_scheme(cfg, fm)
        canonical = parse_canonical(fm, body, specmeta)
        scheme_problems = list(scheme.validate(canonical) or [])
    except Exception as e:  # unknown scheme, or a spec too malformed to canonicalize
        scheme, canonical, scheme_problems = None, None, []
        add("error", "scheme", f"project {fm.get('target_project')!r}: {e}")
    for p in scheme_problems:
        if _PRODUCT_FIT in p:
            outcome_warn = p
        else:
            add("error", "scheme", p)
    if canonical is not None and outcome_warn is None and not has_outcome_signal(canonical):
        outcome_warn = missing_outcome_problem(canonical)
    if outcome_warn:
        add("warning", "outcome", outcome_warn)

    # 13 dangling outbound references
    known = {_prefix(s.name) for s in spec_files(path.parent)} | {my_prefix}
    for key in _REF_KEYS:
        v = fm.get(key)
        for ref in (v if isinstance(v, list) else [v]):
            if (isinstance(ref, str) and _ID_RE.match(ref) and not ref.startswith("SPEC-")
                    and ref not in known):
                add("warning", "reference", f"{key} names {ref}, which is not in {project_dir}/")

    # 14 design section
    want = "Architecture / cross-cutting design" if kind == "epic" else "Design"
    if want not in secs:
        add("warning", "design", f"no '## {want}' section")
    return problems


def _collect(root, project=None):
    root = Path(root)
    if root.is_file():
        return [root]
    files = list(spec_files(root))
    if root.is_dir():
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            files.extend(spec_files(d))
    if project:
        files = [f for f in files if f.parent.name == project]
    return files


def lint_files(paths, *, cfg=None):
    report = LintReport()
    for path in paths:
        probs = lint_file(path, cfg=cfg)
        fm = None
        try:
            fm, _, _ = _frontmatter(Path(path).read_text())
        except (OSError, UnicodeDecodeError):
            pass
        sid = (fm or {}).get("id") if isinstance((fm or {}).get("id"), str) else None
        report.files.append(FileReport(
            id=sid or _prefix(Path(path).name) or Path(path).stem, path=str(path),
            errors=[p for p in probs if p.severity == "error"],
            warnings=[p for p in probs if p.severity == "warning"]))
    return report


def lint_outbox(root, *, cfg=None, project=None, ids=None):
    """Lint the outbox root (`<root>/<project>/<PROJ>-NNNN-*.md`), one project directory, or one
    spec file. `project` narrows to a project directory name; `ids` to those `PROJ-NNNN` ids."""
    files = _collect(root, project)
    if ids:
        want = set(ids)
        files = [f for f in files if _prefix(f.name) in want]
    return lint_files(files, cfg=cfg)
