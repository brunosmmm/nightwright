"""Declared personas (SPEC-0371): `nw.persona.v1` YAML files in the user config dir
(`<config dir>/personas/`) and in a project's main checkout (`.nw/personas/`).

The role (implementer | reviewer | triager), not the file, fixes the output schema and
`read_only`; a reviewer's only schema freedom is a finding `category` vocabulary. A bad
definition is a validation message, never an exception: `load_registry` leaves it out and
`validate_persona_defs` reports it, so one broken file hides neither its siblings nor other
projects. Nothing here is reachable from profiles or the queue (the module-level `PERSONAS`,
`REVIEWER_PERSONAS` and `ADOPT_REGISTRY` stay builtin-only)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

import yaml

from .. import paths
from . import persona_registry as PR
from . import personas as PS

SCHEMA_ID = "nw.persona.v1"
KEYS = ("schema", "name", "role", "template", "description", "category") + PR.DECLARED_KEYS
ROLES = ("implementer", "reviewer", "triager")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_CATEGORIES = 24
_ROLE_SCHEMA = {"implementer": (PS.IMPLEMENTER_SCHEMA, False),
                "triager": (PS.GATE_TRIAGER_SCHEMA, True)}


@dataclass
class Declared:
    """One definition file, valid or not."""
    name: str
    layer: str                  # user | project
    origin: str                 # `user:<file>` or `<project>:.nw/personas/<file>`
    path: Path
    role: str | None = None
    categories: tuple = ()
    description: str = ""
    template_path: Path | None = None
    template_sha256: str | None = None      # SPEC-0394: of the bytes read during validation
    doc: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)
    persona: PS.Persona | None = None

    @property
    def ok(self) -> bool:
        return not self.problems


def user_personas_dir(cfg=None) -> Path:
    cfg_dir = (cfg or {}).get("config_dir") or paths.config_dir()
    return Path(cfg_dir) / "personas"


def _inside(path: Path, directory: Path) -> bool:
    try:
        return path.resolve().parent == directory.resolve()
    except OSError:
        return False


def _check_template(doc, role, directory: Path, put):
    """`(template path, sha256 of its bytes)` of `doc`, or `(None, None)`; problems go to
    `put(key, msg)`."""
    tmpl = doc.get("template")
    if not isinstance(tmpl, str) or not tmpl:
        put("template", "required: the template file name, next to the definition")
        return None, None
    if "/" in tmpl or "\\" in tmpl or ".." in tmpl or Path(tmpl).is_absolute():
        put("template", f"{tmpl!r} must be a file name inside the definition's directory")
        return None, None
    path = directory / tmpl
    if not path.is_file():
        put("template", f"{tmpl!r} is not a file next to the definition")
        return None, None
    if not _inside(path, directory):
        put("template", f"{tmpl!r} resolves outside {directory.name}/")
        return None, None
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        put("template", f"{tmpl!r} is not valid UTF-8")
        return None, None
    except OSError as e:
        put("template", f"{tmpl!r}: {e.strerror or e}")
        return None, None
    if role in PS.ROLE_PLACEHOLDERS:
        allowed = PS.ROLE_PLACEHOLDERS[role]
        for n, line in enumerate(text.splitlines(), 1):
            for m in PS._PLACEHOLDER_RE.finditer(line):
                if m.group(1) not in allowed:
                    kind = "out of role" if m.group(1) in PS.PLACEHOLDERS else "unknown"
                    put("template", f"line {n}: placeholder {{{{{m.group(1)}}}}} is {kind} "
                                    f"for role {role!r}")
    return path, hashlib.sha256(data).hexdigest()


def _categories(doc, role, put) -> tuple:
    if "category" not in doc:
        return ()
    cats = doc["category"]
    if role != "reviewer":
        put("category", "only a reviewer-role persona has a category vocabulary")
        return ()
    if not isinstance(cats, list) or not all(isinstance(c, str) for c in cats):
        put("category", "must be a list of lowercase slugs")
        return ()
    if len(cats) > MAX_CATEGORIES:
        put("category", f"at most {MAX_CATEGORIES} entries, got {len(cats)}")
    for c in cats:
        if not CATEGORY_RE.fullmatch(c):
            put("category", f"{c!r} must match {CATEGORY_RE.pattern}")
    dup = sorted({c for c in cats if cats.count(c) > 1})
    if dup:
        put("category", f"duplicate entries: {', '.join(dup)}")
    return tuple(cats)


def _read_file(path: Path, layer: str, origin: str, directory: Path) -> Declared:
    d = Declared(path.stem, layer, origin, path)
    put = lambda key, msg: d.problems.append(f"{origin}: {key}: {msg}")      # noqa: E731
    if not _inside(path, directory):
        put("<file>", "resolves outside its directory")
        return d
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
        put("<file>", " ".join(str(e).split()))
        return d
    if not isinstance(doc, dict):
        put("<file>", "must be a mapping")
        return d
    if doc.get("schema") != SCHEMA_ID:
        put("schema", f"must be {SCHEMA_ID!r}")
    for key in sorted(map(str, doc)):
        if key not in KEYS:
            put(key, "unknown key")
    name = doc.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        put("name", f"must match {NAME_RE.pattern}")
    elif name != path.stem:
        put("name", f"{name!r} must equal the file stem {path.stem!r}")
    role = doc.get("role")
    if role not in ROLES:
        put("role", f"must be one of {', '.join(ROLES)}")
        role = None
    d.role = role
    desc = doc.get("description", "")
    if not isinstance(desc, str):
        put("description", "must be a string")
    else:
        d.description = desc
    d.categories = _categories(doc, role, put)
    PR.check_declared_keys(doc, role, put)
    d.template_path, d.template_sha256 = _check_template(doc, role, directory, put)
    d.doc = doc
    return d


def _read_layer(directory: Path, layer: str, prefix: str) -> list:
    """`prefix` + file name is a definition's origin (`user:`, `<project>:.nw/personas/`)."""
    try:
        if not directory.is_dir():
            return []
        files = sorted(p for p in directory.iterdir() if p.suffix == ".yaml" and p.is_file())
    except OSError as e:
        d = Declared(directory.name, layer, prefix.rstrip("/"), directory)
        d.problems.append(f"{d.origin}: <dir>: {e.strerror or e}")
        return [d]
    return [_read_file(p, layer, prefix + p.name, directory) for p in files]


def scan(project_root=None, *, user_dir=None) -> list:
    """Every definition visible from `project_root` (user layer first, then project), valid or
    not, with cross-file problems (builtin shadowing, duplicates) attached. `user_dir` defaults
    to `<config dir>/personas`. Valid ones carry a built `Persona`."""
    udir = Path(user_dir) if user_dir is not None else user_personas_dir()
    found = _read_layer(udir, "user", "user:")
    if project_root is not None:
        root = Path(project_root)
        found += _read_layer(root / ".nw" / "personas", "project",
                             f"{root.name}:.nw/personas/")
    seen: dict = {}
    for d in found:                 # keyed on the file stem: a wrong `name:` claims nothing
        if d.path.is_dir() or not NAME_RE.fullmatch(d.name):
            continue
        if d.name in PS.PERSONAS:
            d.problems.append(f"{d.origin}: name: name {d.name!r} is a builtin persona; pick "
                              "another name")
        elif d.name in seen:
            d.problems.append(f"{d.origin}: name: name {d.name!r} is already declared by "
                              f"{seen[d.name]}")
        else:
            seen[d.name] = d.origin
    for d in found:
        if d.ok:
            d.persona = _build(d)
    return found


def _build(d: Declared) -> PS.Persona:
    if d.role == "reviewer":
        schema, read_only = PS.reviewer_schema(d.categories), True
    else:
        schema, read_only = _ROLE_SCHEMA[d.role]
    return PS.Persona(d.name, str(d.template_path.resolve()), schema, read_only, role=d.role,
                      declared=_declared(d))


def _declared(d: Declared) -> MappingProxyType:
    """SPEC-0394: the read-only declaration carried on `Persona.declared`."""
    v = PR.declared_values(d.doc, d.role)
    return MappingProxyType({
        "settings": MappingProxyType(v["settings"]), "required": v["required"],
        "when": None if v["when"] is None else MappingProxyType(v["when"]),
        "template_sha256": d.template_sha256})


def validate_persona_defs(project_root=None, *, user_dir=None) -> list:
    """Every problem of every visible definition, `"<origin>: <key>: <problem>"`. Never raises
    for a bad definition."""
    return [m for d in scan(project_root, user_dir=user_dir) for m in d.problems]


def load_registry(project_root=None, *, user_dir=None) -> dict:
    """`{name: Persona}`: the builtins plus every valid declared persona. Invalid definitions
    are left out (see `validate_persona_defs`)."""
    reg = dict(PS.PERSONAS)
    for d in scan(project_root, user_dir=user_dir):
        if d.persona is not None:
            reg[d.name] = d.persona
    return reg


def personas_for(project_root=None, *, user_dir=None) -> list:
    """The registry as a list: builtins in table order, then declared ones sorted by name."""
    reg = load_registry(project_root, user_dir=user_dir)
    return [reg[n] for n in PS.PERSONAS] + [reg[n] for n in sorted(set(reg) - set(PS.PERSONAS))]


def reviewer_names(project_root=None, *, user_dir=None) -> list:
    """Reviewer-role names: the builtin reviewers in canonical order, then declared reviewers
    sorted by name."""
    reg = load_registry(project_root, user_dir=user_dir)
    return list(PS.REVIEWER_PERSONAS) + sorted(
        n for n, p in reg.items() if n not in PS.PERSONAS and p.role == "reviewer")
