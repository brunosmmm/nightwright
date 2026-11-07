"""Pi model map (SPEC-0451): YAML that translates profile aliases to provider/id/thinking.

Same overlay order as cursor_map, lowest first:

1. builtin `pi_map.yaml` (this package)
2. `<config_dir>/pi_map.yaml`
3. `agentic.pi` in config.yaml
4. `<project>/.nw/pi_map.yaml`
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .profiles import EFFORTS, MODEL_ALIASES, deep_merge

SCHEMA_ID = "nw.pi_map.v1"
MAP_FILENAME = "pi_map.yaml"
_BUILTIN = Path(__file__).resolve().parent / MAP_FILENAME
_THINKING = frozenset({"off", "minimal", "low", "medium", "high", "xhigh", "max"})
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_FILE_KEYS = frozenset({"schema", "defaults", "aliases"})


def _is_str(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def validate_pi_map_doc(doc, *, origin, in_file) -> list:
    """Messages `"<origin>: <key.path>: <problem>"`; empty when valid."""
    out = []

    def bad(path, problem):
        out.append(f"{origin}: {path}: {problem}")

    if not isinstance(doc, dict):
        bad("<root>", f"must be a mapping, got {type(doc).__name__}")
        return out
    for key in doc:
        if key not in _FILE_KEYS:
            bad(key, "unknown key")
    if in_file:
        if doc.get("schema") != SCHEMA_ID:
            bad("schema", f"must be {SCHEMA_ID!r}, got {doc.get('schema')!r}" if "schema" in doc
                else f"is required (schema: {SCHEMA_ID})")
    elif "schema" in doc and doc["schema"] != SCHEMA_ID:
        bad("schema", f"must be {SCHEMA_ID!r}, got {doc['schema']!r}")
    defaults = doc.get("defaults")
    if defaults is not None:
        if not isinstance(defaults, dict):
            bad("defaults", f"must be a mapping, got {type(defaults).__name__}")
        else:
            for alias, effort in defaults.items():
                if alias not in MODEL_ALIASES:
                    bad(f"defaults.{alias}", f"unknown alias; known: {', '.join(MODEL_ALIASES)}")
                elif effort not in EFFORTS:
                    bad(f"defaults.{alias}", f"must be one of {', '.join(EFFORTS)}, got {effort!r}")
    aliases = doc.get("aliases")
    if aliases is not None:
        if not isinstance(aliases, dict):
            bad("aliases", f"must be a mapping, got {type(aliases).__name__}")
        else:
            for alias, efforts in aliases.items():
                path = f"aliases.{alias}"
                if alias not in MODEL_ALIASES:
                    bad(path, f"unknown alias; known: {', '.join(MODEL_ALIASES)}")
                    continue
                if _is_str(efforts):
                    bad(path, "must be a mapping of effort to provider/id/thinking object")
                    continue
                if not isinstance(efforts, dict):
                    bad(path, "must be a mapping of effort to provider/id/thinking object")
                    continue
                for effort, row in efforts.items():
                    p = f"{path}.{effort}"
                    if effort not in EFFORTS:
                        bad(p, f"unknown effort; known: {', '.join(EFFORTS)}")
                    if not isinstance(row, dict):
                        bad(p, f"must be an object with provider, id, and thinking, got {row!r}")
                        continue
                    for key in ("provider", "id", "thinking"):
                        if not _is_str(row.get(key)):
                            bad(p, f"missing or empty {key}")
                    prov = str(row.get("provider") or "")
                    if prov and not _PROVIDER_RE.match(prov):
                        bad(p, f"provider {prov!r} must match {_PROVIDER_RE.pattern}")
                    if " " in str(row.get("id") or ""):
                        bad(p, "id must not contain whitespace")
                    if row.get("thinking") not in _THINKING:
                        bad(p, f"thinking must be one of {', '.join(sorted(_THINKING))}")
    return out


def _read_file(path: Path, *, origin, required: bool) -> dict:
    if not path.is_file():
        if required:
            raise ValueError(f"{origin}: missing {path}")
        return {}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ValueError(f"{origin}: not valid YAML ({e})") from e
    problems = validate_pi_map_doc(doc, origin=origin, in_file=True)
    if problems:
        raise ValueError("invalid pi map:\n  - " + "\n  - ".join(problems))
    return {k: v for k, v in doc.items() if k != "schema"}


def _config_delta(cfg) -> dict:
    raw = ((cfg or {}).get("agentic") or {}).get("pi")
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("agentic.pi must be a mapping")
    problems = validate_pi_map_doc(raw, origin="config.yaml:agentic.pi", in_file=False)
    if problems:
        raise ValueError("invalid pi map:\n  - " + "\n  - ".join(problems))
    return {k: v for k, v in raw.items() if k != "schema"}


def load_pi_map(cfg=None, *, project_root=None) -> dict:
    """Merged `{defaults, aliases}` for this cfg / project, lowest layer first."""
    layers = [_read_file(_BUILTIN, origin=f"builtin:{MAP_FILENAME}", required=True)]
    cfg_dir = (cfg or {}).get("config_dir")
    if cfg_dir:
        layers.append(_read_file(Path(cfg_dir) / MAP_FILENAME,
                                 origin=f"{cfg_dir}/{MAP_FILENAME}", required=False))
    layers.append(_config_delta(cfg))
    if project_root:
        layers.append(_read_file(Path(project_root) / ".nw" / MAP_FILENAME,
                                 origin=f"{project_root}/.nw/{MAP_FILENAME}", required=False))
    out = {}
    for delta in layers:
        if delta:
            out = deep_merge(out, delta)
    return out


def distinct_providers(doc: dict | None = None, *, cfg=None, project_root=None) -> list[str]:
    """Sorted provider names appearing in the merged map."""
    doc = doc if doc is not None else load_pi_map(cfg, project_root=project_root)
    providers: set[str] = set()
    for efforts in (doc.get("aliases") or {}).values():
        if not isinstance(efforts, dict):
            continue
        for row in efforts.values():
            if isinstance(row, dict) and _is_str(row.get("provider")):
                providers.add(str(row["provider"]).strip())
    return sorted(providers)


def _flatten(doc: dict) -> tuple[dict, dict]:
    defaults = dict(doc.get("defaults") or {})
    table: dict[str, dict] = {}
    for alias, efforts in (doc.get("aliases") or {}).items():
        if isinstance(efforts, dict):
            for effort, row in efforts.items():
                if isinstance(row, dict):
                    table[f"{alias}/{effort}"] = row
    return defaults, table


def map_pi_model(model, effort=None, cfg=None, *, project_root=None) -> dict:
    """`{provider, id, thinking, model}` where model is `provider/id`."""
    raw = str(model or "").strip()
    if not raw:
        raise ValueError("pi runner needs a model")
    if raw not in MODEL_ALIASES:
        raise ValueError(f"no Pi mapping for {raw!r}")
    doc = load_pi_map(cfg, project_root=project_root)
    defaults, table = _flatten(doc)
    eff = effort if effort in EFFORTS else defaults.get(raw) or "high"
    row = table.get(f"{raw}/{eff}")
    if not row:
        raise ValueError(f"no Pi mapping for {raw}/{eff}")
    provider = str(row["provider"]).strip()
    mid = str(row["id"]).strip()
    thinking = str(row["thinking"]).strip()
    return {"provider": provider, "id": mid, "thinking": thinking,
            "model": f"{provider}/{mid}"}
