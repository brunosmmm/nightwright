"""Per-project idea extension schema (SPEC-0120) and validation (SPEC-0121)."""
from __future__ import annotations


from .render import warn
from .rules import registered_projects

_WARNED_UNKNOWN: set[str] = set()


def _warn_unknown_extension_projects(cfg) -> None:
    """Warn once per unknown project name in idea_extensions (permissive, like associations)."""
    known = set(registered_projects(cfg))
    for proj in (cfg.get("idea_extensions") or {}):
        if proj in known or proj in _WARNED_UNKNOWN:
            continue
        _WARNED_UNKNOWN.add(proj)
        warn(f"! unknown project in idea_extensions: {proj}")


def extension_schema_for(cfg, project: str) -> dict | None:
    """Return normalized schema for `project`, or None when freeform Ext is allowed."""
    raw_map = cfg.get("idea_extensions") or {}
    if not raw_map:
        return None
    _warn_unknown_extension_projects(cfg)
    raw = raw_map.get(project)
    if not raw:
        return None
    return normalize_extension_schema(raw, project=project)


def normalize_extension_schema(raw, *, project: str = "") -> dict:
    """Validate and normalize one project's idea_extensions entry. Raises ValueError."""
    label = f"idea_extensions[{project!r}]" if project else "idea_extensions entry"
    if not isinstance(raw, dict):
        raise ValueError(f"{label}: expected mapping")
    required = raw.get("required") or []
    properties = raw.get("properties") or {}
    if not isinstance(required, list):
        raise ValueError(f"{label}: required must be a list")
    if not isinstance(properties, dict):
        raise ValueError(f"{label}: properties must be a mapping")
    for key in required:
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{label}: required entries must be non-empty strings")
        if key not in properties:
            raise ValueError(f"{label}: required key {key!r} missing from properties")
    norm_props: dict[str, dict] = {}
    for key, spec in properties.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{label}: property names must be non-empty strings")
        if not isinstance(spec, dict):
            raise ValueError(f"{label}.properties[{key!r}]: expected mapping")
        typ = (spec.get("type") or "string").strip().lower()
        if typ not in ("string", "enum"):
            raise ValueError(f"{label}.properties[{key!r}]: type must be string or enum")
        entry: dict = {"type": typ}
        if typ == "enum":
            values = spec.get("values")
            if not isinstance(values, list) or not values:
                raise ValueError(f"{label}.properties[{key!r}]: enum requires non-empty values")
            if not all(isinstance(v, str) and v for v in values):
                raise ValueError(f"{label}.properties[{key!r}]: enum values must be strings")
            entry["values"] = list(values)
        norm_props[key] = entry
    return {"required": list(required), "properties": norm_props}


def validate_extension_map(cfg, project: str, mapping: dict[str, str], *,
                           updating: bool = False) -> list[str]:
    """Return human-readable problems for `mapping` under `project`. Empty when OK.

    `updating=True` is used before writes; behavior is the same today but reserved for
    callers that want to distinguish lint vs write paths."""
    del updating
    schema = extension_schema_for(cfg, project)
    if not schema:
        return []
    problems: list[str] = []
    props = schema["properties"]
    required = set(schema["required"])
    for key, val in mapping.items():
        if key not in props:
            problems.append(f"unknown key {key!r}")
            continue
        spec = props[key]
        if spec["type"] == "enum" and val and val not in spec["values"]:
            problems.append(f"{key}: value {val!r} not in {spec['values']}")
    for key in sorted(required):
        if key not in mapping or not str(mapping.get(key, "")).strip():
            problems.append(f"missing required key {key!r}")
    return problems


def extension_keys_for(cfg, project: str, existing: dict[str, str] | None = None) -> list[str]:
    """Schema property keys ∪ keys already in the drawer (SPEC-0122 completers / desk)."""
    keys: set[str] = set(existing or {})
    schema = extension_schema_for(cfg, project)
    if schema:
        keys.update(schema["properties"])
    return sorted(keys)


def enum_values_for(cfg, project: str, key: str) -> list[str] | None:
    """Return enum choices for `key`, or None when not an enum field."""
    schema = extension_schema_for(cfg, project)
    if not schema:
        return None
    spec = schema["properties"].get(key)
    if not spec or spec["type"] != "enum":
        return None
    return list(spec["values"])
