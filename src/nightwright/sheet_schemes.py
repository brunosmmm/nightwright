"""Sheet-sync manifest format + config (SPEC-0124): a `SheetManifest` describing one Google
Sheet's column mapping (which columns nightwright owns/pushes vs. which the sheet/human owns), loaded
from `<config_dir>/schemes/*.toml` (the same directory `export_schemes/template_scheme.py`
loads user export manifests from, per SPEC-0058) disambiguated by `kind = "sheet"`, plus
`outbox_targets[project].sheet` config resolution. Pure data model — no Sheets API call, no
new runtime dependency (SPEC-0123 Non-goal); see SPEC-0125 for the plan/record CLI that
actually consumes this."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

_VALID_OWNERS = ("nw", "sheet")
# SPEC-0330: manifests written before the rename say `owner = "wt"`; read it as "nw".
_LEGACY_OWNERS = {"wt": "nw"}


def _user_schemes_dir() -> Path:
    """Computed per-call (not a module-level constant) so tests can point NW_CONFIG_DIR at a
    tmp dir without needing importlib.reload."""
    return Path(paths.config_dir()) / "schemes"


@dataclass(frozen=True)
class SheetColumn:
    header: str
    source: str
    owner: str
    value_map: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SheetManifest:
    name: str
    description: str
    id_column: str
    columns: tuple[SheetColumn, ...] = field(default_factory=tuple)
    origin: str = "user-config"

    def column(self, header: str) -> SheetColumn | None:
        for col in self.columns:
            if col.header == header:
                return col
        return None

    def nw_owned(self) -> tuple[SheetColumn, ...]:
        return tuple(c for c in self.columns if c.owner == "nw")

    def sheet_owned(self) -> tuple[SheetColumn, ...]:
        return tuple(c for c in self.columns if c.owner == "sheet")


def _parse_manifest(raw: dict, *, path: str = "<manifest>") -> SheetManifest:
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise ValueError(f"{path}: manifest requires a non-empty 'name'")
    id_column = raw.get("id_column")
    if not id_column or not isinstance(id_column, str):
        raise ValueError(f"{path}: manifest requires a non-empty 'id_column'")

    raw_columns = raw.get("columns") or []
    if not isinstance(raw_columns, list) or not raw_columns:
        raise ValueError(f"{path}: manifest requires a non-empty 'columns' list")

    columns: list[SheetColumn] = []
    seen_headers: set[str] = set()
    for entry in raw_columns:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: each columns[] entry must be a table")
        header = entry.get("header")
        source = entry.get("source")
        owner = entry.get("owner")
        owner = _LEGACY_OWNERS.get(owner, owner)
        if not header or not isinstance(header, str):
            raise ValueError(f"{path}: column entry missing non-empty 'header'")
        if header in seen_headers:
            raise ValueError(f"{path}: duplicate column header {header!r}")
        seen_headers.add(header)
        if not source or not isinstance(source, str):
            raise ValueError(f"{path}: column {header!r} missing non-empty 'source'")
        if owner not in _VALID_OWNERS:
            raise ValueError(
                f"{path}: column {header!r} has owner {owner!r}; must be one of {_VALID_OWNERS}")
        value_map = entry.get("value_map") or {}
        if not isinstance(value_map, dict):
            raise ValueError(f"{path}: column {header!r} value_map must be a table")
        for k, v in value_map.items():
            if not isinstance(k, str) or not k or not isinstance(v, str) or not v:
                raise ValueError(
                    f"{path}: column {header!r} value_map keys/values must be non-empty strings")
        columns.append(SheetColumn(header=header, source=source, owner=owner,
                                   value_map=dict(value_map)))

    id_col = next((c for c in columns if c.header == id_column), None)
    if id_col is None:
        raise ValueError(f"{path}: id_column {id_column!r} is not one of the declared columns")
    if id_col.owner != "nw":
        raise ValueError(f"{path}: id_column {id_column!r} must have owner 'nw' (got {id_col.owner!r})")

    return SheetManifest(
        name=name,
        description=raw.get("description", ""),
        id_column=id_column,
        columns=tuple(columns),
    )


def load_manifest(path) -> SheetManifest:
    """Load and validate one `kind = "sheet"` TOML manifest file."""
    path = Path(path)
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return _parse_manifest(raw, path=str(path))


def available_sheet_manifests(cfg=None) -> dict[str, SheetManifest]:
    """All `kind = "sheet"` manifests under `<config_dir>/schemes/`, keyed by name. Non-sheet
    manifests (no `kind`, or a different `kind`) are silently skipped — they belong to
    `export_schemes.template_scheme`'s loader instead."""
    del cfg  # reserved for a future override of the schemes dir; unused today
    out: dict[str, SheetManifest] = {}
    schemes_dir = _user_schemes_dir()
    if not schemes_dir.exists():
        return out
    for p in sorted(schemes_dir.glob("*.toml")):
        with open(p, "rb") as f:
            raw = tomllib.load(f)
        if raw.get("kind") != "sheet":
            continue
        manifest = _parse_manifest(raw, path=str(p))
        out[manifest.name] = manifest
    return out


def get_sheet_manifest(cfg, name: str) -> SheetManifest:
    """Look up a sheet manifest by name; raises ValueError (with available names) if unknown."""
    manifests = available_sheet_manifests(cfg)
    if name not in manifests:
        raise ValueError(
            f"unknown sheet manifest {name!r}; available: {', '.join(sorted(manifests)) or '(none)'}")
    return manifests[name]


def sheet_target_for(cfg, project: str) -> dict | None:
    """The raw `outbox_targets[project].sheet` dict, or None when unconfigured."""
    target = (cfg.get("outbox_targets") or {}).get(project) or {}
    return target.get("sheet")


def resolve_sheet_manifest(cfg, project: str) -> tuple[dict, SheetManifest]:
    """Resolve `project` to its (sheet target dict, SheetManifest). Raises ValueError with a
    single clear message when the project has no sheet target, the target is missing
    required keys, or the named manifest can't be loaded."""
    target = sheet_target_for(cfg, project)
    if not target:
        raise ValueError(
            f"no sheet target for project {project!r}: configure "
            f"outbox_targets[{project!r}].sheet = {{spreadsheet_id, tab, manifest}}")
    missing = [k for k in ("spreadsheet_id", "tab", "manifest") if not target.get(k)]
    if missing:
        raise ValueError(
            f"outbox_targets[{project!r}].sheet missing required key(s): {', '.join(missing)}")
    manifest = get_sheet_manifest(cfg, target["manifest"])
    return target, manifest
