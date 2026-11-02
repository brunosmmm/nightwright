"""Persona registry as data (SPEC-0370): `nw.persona.v1` definitions from three layers (builtin
`persona_defs/*.yaml`, user `<config_dir>/personas/`, project `<main checkout>/.nw/personas/`),
a strict validator that reports every problem of a file at once, and the derivations that
`personas.py` uses to build `PERSONAS`, `REVIEWER_PERSONAS` and `ADOPT_REGISTRY`.

This module must not import `personas.py` (that module imports it): the caller passes what it
owns as a `known` mapping, `{"output_schemas": keys, "invariants": keys, "placeholders": names}`.
Builtin templates live in `agentic/personas/`; a user/project template sits beside its YAML."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SCHEMA = "nw.persona.v1"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
ROLES = ("implementer", "reviewer", "triager", "system")
CUSTOM_ROLES = ("implementer", "reviewer", "triager")
ADOPT_STEPS = ("implement", "triage", "review")

_DEFS_DIR = Path(__file__).resolve().parent / "persona_defs"
_TEMPLATES_DIR = Path(__file__).resolve().parent / "personas"
_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")

_COMMON = ("schema", "name", "role", "template", "description", "category")
_BUILTIN_ONLY = ("order", "output_schema", "invariant", "read_only", "adopt")


@dataclass(frozen=True)
class PersonaDef:
    """A valid definition. `read_only` is derived from the role (explicit for `system`); the
    builtin-only fields are None/default for a user/project definition."""
    name: str
    role: str
    template: str
    description: str
    category: tuple
    order: object
    output_schema: object
    invariant: object
    read_only: bool
    adopt: object          # None, or (step, tuple(personas))
    scope: str
    origin: str
    path: object           # the YAML file's Path

    @property
    def template_path(self) -> Path:
        return (_TEMPLATES_DIR if self.scope == "builtin" else Path(self.path).parent) \
            / self.template


@dataclass
class Entry:
    """One file in one layer: its name (the stem), where it came from, the validated
    definition (None when broken) and every problem found."""
    name: str
    scope: str
    origin: str
    path: object
    defn: object = None
    doc: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    refused: bool = False   # shadows a builtin / conflicts with a sibling: never wins resolution


@dataclass
class Registry:
    entries: list          # builtin first, then user, then project; each sorted by name

    def resolved(self) -> dict:
        """`{name: Entry}`: the highest layer wins (no merge). A refused entry (one that shadows
        a builtin, or a `.yml` conflicting with its `.yaml` sibling) never displaces another."""
        out = {}
        for e in self.entries:
            if e.refused and e.name in out:
                continue
            out[e.name] = e
        return out

    def builtin_defs(self) -> list:
        return builtin_defs(self.entries)


def builtin_defs(entries) -> list:
    return sorted((e.defn for e in entries if e.scope == "builtin" and e.defn is not None),
                  key=lambda d: d.order)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate_persona_doc(doc, *, origin, layer, stem, known, directory=None) -> list:
    """Every problem of one definition document as `origin: key: problem` strings. `layer` is
    `builtin|user|project`; `directory` is where a user/project template is looked up."""
    errs = []

    def bad(key, msg):
        errs.append(f"{origin}: {key}: {msg}")

    if not isinstance(doc, dict):
        return [f"{origin}: <file>: must be a YAML mapping"]
    builtin = layer == "builtin"
    allowed = set(_COMMON) | (set(_BUILTIN_ONLY) if builtin else set())
    for key in doc:
        if key in allowed:
            continue
        if key in _BUILTIN_ONLY:
            bad(key, "allowed only in a builtin definition")
        else:
            bad(key, f"unknown key; allowed: {', '.join(sorted(allowed))}")
    if doc.get("schema") != SCHEMA:
        bad("schema", f"must be {SCHEMA}")
    name = doc.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        bad("name", f"must be a string matching {NAME_RE.pattern}")
    elif name != stem:
        bad("name", f"{name!r} must equal the file stem {stem!r}")
    role = doc.get("role")
    roles = ROLES if builtin else CUSTOM_ROLES
    if role not in roles:
        if role == "system":
            bad("role", "'system' is allowed only in a builtin definition")
        else:
            bad("role", f"must be one of {', '.join(roles)}")
    desc = doc.get("description")
    # SPEC-0371: a declared (user/project) persona may omit its description
    if (builtin or "description" in doc) and (not isinstance(desc, str) or not desc.strip()):
        bad("description", "must be a non-empty string")
    cat = doc.get("category")
    if "category" in doc and (not isinstance(cat, list) or not all(
            isinstance(c, str) and c.strip() for c in cat)):
        bad("category", "must be a list of non-empty strings")
    _check_template(doc.get("template"), builtin, directory, known, bad)
    if builtin:
        _check_builtin_keys(doc, role, known, bad)
    return errs


def _check_template(tpl, builtin, directory, known, bad):
    if not isinstance(tpl, str) or not tpl or "/" in tpl or "\\" in tpl or tpl in (".", ".."):
        bad("template", "must be a bare file name (no path separators)")
        return
    base = _TEMPLATES_DIR if builtin else (Path(directory) if directory else None)
    if base is None:
        return
    path = base / tpl
    if not path.is_file():
        bad("template", f"{tpl} not found in {base}")
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        bad("template", f"{tpl} is not readable UTF-8 text: {' '.join(str(e).split())}")
        return
    placeholders = set(known.get("placeholders") or ())
    unknown = sorted({m for m in _PLACEHOLDER_RE.findall(text)} - placeholders)
    if unknown:
        bad("template", f"{tpl} uses unknown placeholders: "
                        f"{', '.join('{{' + u + '}}' for u in unknown)}")


def _check_builtin_keys(doc, role, known, bad):
    if not _is_int(doc.get("order")):
        bad("order", "required, an integer")
    out = doc.get("output_schema")
    if not isinstance(out, str) or out not in (known.get("output_schemas") or ()):
        bad("output_schema", f"required, one of: "
                             f"{', '.join(sorted(known.get('output_schemas') or ()))}")
    if "invariant" in doc and (not isinstance(doc["invariant"], str)
                               or doc["invariant"] not in (known.get("invariants") or ())):
        bad("invariant", f"unknown invariant; known: "
                         f"{', '.join(sorted(known.get('invariants') or ()))}")
    system = role == "system"
    if system:
        if not isinstance(doc.get("read_only"), bool):
            bad("read_only", "required (true|false) for role system")
    elif "read_only" in doc:
        bad("read_only", "allowed only for role system (otherwise derived from the role)")
    if "adopt" in doc:
        adopt = doc["adopt"]
        if not system:
            bad("adopt", "allowed only for role system")
        elif not (isinstance(adopt, dict) and set(adopt) == {"step", "personas"}
                  and isinstance(adopt["step"], str) and adopt["step"] in ADOPT_STEPS
                  and isinstance(adopt["personas"], list)
                  and all(isinstance(p, str) for p in adopt["personas"])):
            bad("adopt", f"must be {{step: {'|'.join(ADOPT_STEPS)}, personas: [names]}}")


def _derive_read_only(doc) -> bool:
    return doc["read_only"] if doc["role"] == "system" else doc["role"] != "implementer"


def _make_def(doc, scope, origin, path) -> PersonaDef:
    adopt = doc.get("adopt")
    return PersonaDef(
        name=doc["name"], role=doc["role"], template=doc["template"],
        description=(doc.get("description") or "").strip(), category=tuple(doc.get("category") or ()),
        order=doc.get("order"), output_schema=doc.get("output_schema"),
        invariant=doc.get("invariant", "default") if scope == "builtin" else None,
        read_only=_derive_read_only(doc),
        adopt=(adopt["step"], tuple(adopt["personas"])) if adopt else None,
        scope=scope, origin=origin, path=path)


# ---- layers ----------------------------------------------------------------------------------

def _read_dir(directory, layer, origin_of, known) -> list:
    """`[Entry]` for the `*.yaml` files of `directory` (missing directory: none). A `*.yml` file
    or a file whose stem is not a valid name is a broken definition; other files are ignored.
    An unreadable directory raises ValueError."""
    try:
        if not directory.is_dir():
            return []
        paths = sorted(directory.iterdir())
    except OSError as e:
        raise ValueError(f"{origin_of(directory)}: <dir>: {e.strerror or e}") from e
    entries, seen = [], set()
    for path in paths:
        if not path.is_file() or path.suffix not in (".yaml", ".yml"):
            continue
        stem, origin = path.stem, origin_of(path)
        entry = Entry(stem, layer, origin, path)
        if path.suffix == ".yml":
            msg = "rename to .yaml" if stem not in seen else \
                f"conflicts with {stem}.yaml; remove or rename one"
            entry.errors = [f"{origin}: <file>: {msg}"]
            entry.refused = stem in seen
            entries.append(entry)
            continue
        seen.add(stem)
        if not NAME_RE.match(stem):
            entry.errors = [f"{origin}: <file>: persona name {stem!r} must match "
                            f"{NAME_RE.pattern}"]
        else:
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
                entry.errors = [f"{origin}: {' '.join(str(e).split())}"]
                entries.append(entry)
                continue
            entry.doc = doc if isinstance(doc, dict) else {}
            entry.errors = validate_persona_doc(doc, origin=origin, layer=layer, stem=stem,
                                                known=known, directory=path.parent)
            if not entry.errors:
                entry.defn = _make_def(doc, layer, origin, path)
        entries.append(entry)
    # a `.yml` listed before its `.yaml` sibling is still reported as a conflict
    return sorted(entries, key=lambda e: (e.name, e.path.suffix == ".yml"))


def _builtin_entries(known) -> list:
    entries = _read_dir(_DEFS_DIR, "builtin", lambda p: f"builtin:persona_defs/{p.name}", known)
    firsts = {}
    for e in sorted((e for e in entries if e.defn is not None), key=lambda e: e.name):
        other = firsts.setdefault(e.defn.order, e)
        if other is not e:
            e.errors.append(f"{e.origin}: order: {e.defn.order} duplicates {other.name}")
            e.defn = None
    return entries


def load_builtin(known) -> list:
    """The builtin definitions in `order`; a broken builtin file raises ValueError (a packaging
    bug)."""
    entries = _builtin_entries(known)
    problems = [p for e in entries for p in e.errors]
    if problems:
        raise ValueError("; ".join(problems))
    return builtin_defs(entries)


def load_registry(cfg, *, project_root=None, known) -> Registry:
    """Builtin, user and project entries, lowest layer first. Every file is read and validated on
    each call. The project layer is read only from `project_root`."""
    builtin = sorted(_builtin_entries(known), key=lambda e: (
        e.defn.order if e.defn is not None else 10 ** 9, e.name))
    names = {e.name for e in builtin}
    cfg_dir = (cfg or {}).get("config_dir")
    layers = []
    if cfg_dir:
        layers.append(("user", Path(cfg_dir) / "personas"))
    if project_root:
        layers.append(("project", Path(project_root) / ".nw" / "personas"))
    entries = list(builtin)
    for scope, directory in layers:
        for e in _read_dir(directory, scope, str, known):
            if e.name in names:
                e.errors.append(f"{e.origin}: name: shadows builtin persona {e.name!r}")
                e.defn = None
                e.refused = True
            entries.append(e)
    return Registry(entries)


# ---- derivations (used by personas.py) -------------------------------------------------------

def reviewer_personas(defs) -> tuple:
    return tuple(d.name for d in sorted(defs, key=lambda d: d.order) if d.role == "reviewer")


def adopt_registry(defs) -> dict:
    """Non-reviewers with an adoption entry first, then the reviewers, each by `order`."""
    out = {}
    ordered = sorted(defs, key=lambda d: d.order)
    for d in ordered:
        if d.role == "reviewer":
            continue
        if d.role == "implementer":
            out[d.name] = ("implement", ())
        elif d.role == "triager":
            out[d.name] = ("triage", ())
        elif d.adopt is not None:
            out[d.name] = (d.adopt[0], tuple(d.adopt[1]))
    for d in ordered:
        if d.role == "reviewer":
            out[d.name] = ("review", None)
    return out
