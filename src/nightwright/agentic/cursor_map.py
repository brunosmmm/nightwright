"""Cursor model map (SPEC-0436): YAML that translates profile aliases to Cursor ids.

Same overlay idea as workflow profiles (SPEC-0237), lowest first:

1. builtin `cursor_map.yaml` (this package)
2. `<config_dir>/cursor_map.yaml`
3. `agentic.cursor` in config.yaml (`defaults`, `aliases`, optional flat `alias_map`)
4. `<project>/.nw/cursor_map.yaml`

A profile persona setting `cursor_model` (when set) wins over the map for that spawn.
Builtin profiles keep `model: opus|sonnet|haiku`; they do not fork per backend."""
from __future__ import annotations

from pathlib import Path

import yaml

from .profiles import EFFORTS, MODEL_ALIASES, MODEL_RE, deep_merge

SCHEMA_ID = "nw.cursor_map.v1"
MAP_FILENAME = "cursor_map.yaml"
_BUILTIN = Path(__file__).resolve().parent / MAP_FILENAME
SDK_MODELS_FILENAME = "sdk_models.yaml"
_SDK_MODELS = Path(__file__).resolve().parent / SDK_MODELS_FILENAME
# Rank used when a requested effort is not in that model's allowed values.
# `xhigh` and `extra-high` are the same step; some models spell it one way.
_EFFORT_RANK = {
    "none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4,
    "xhigh": 5, "extra-high": 5, "max": 6,
}
_EFFORT_PREFER = {
    "none": ("none", "minimal", "low"),
    "minimal": ("minimal", "none", "low"),
    "low": ("low", "minimal", "medium", "none"),
    "medium": ("medium", "high", "low"),
    "high": ("high", "medium", "max", "xhigh", "extra-high"),
    "xhigh": ("xhigh", "extra-high", "max", "high"),
    "extra-high": ("extra-high", "xhigh", "max", "high"),
    "max": ("max", "xhigh", "extra-high", "high"),
}
_sdk_models_cache = None
_FILE_KEYS = frozenset({"schema", "defaults", "aliases", "alias_map"})
_CLAUDE_MODEL_RE = MODEL_RE


def _is_str(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def validate_cursor_map_doc(doc, *, origin, in_file) -> list:
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
                    _reject_claude_id(bad, path, efforts)
                    continue
                if not isinstance(efforts, dict):
                    bad(path, "must be a mapping of effort to Cursor model id, or a model id")
                    continue
                for effort, mid in efforts.items():
                    p = f"{path}.{effort}"
                    if effort not in EFFORTS:
                        bad(p, f"unknown effort; known: {', '.join(EFFORTS)}")
                    if not _is_str(mid):
                        bad(p, f"must be a Cursor model id, got {mid!r}")
                    else:
                        _reject_claude_id(bad, p, mid)
    flat = doc.get("alias_map")
    if flat is not None:
        if not isinstance(flat, dict):
            bad("alias_map", f"must be a mapping, got {type(flat).__name__}")
        else:
            for k, v in flat.items():
                p = f"alias_map.{k}"
                if not _is_str(k) or not _is_str(v):
                    bad(p, "keys and values must be non-empty strings")
                else:
                    _reject_claude_id(bad, p, v)
    return out


def _reject_claude_id(bad, path, mid):
    if _CLAUDE_MODEL_RE.match(str(mid).strip()) or str(mid).startswith("claude-"):
        bad(path, f"cursor map rejects Claude model id {mid!r}")


def _read_file(path: Path, *, origin, required: bool) -> dict:
    if not path.is_file():
        if required:
            raise ValueError(f"{origin}: missing {path}")
        return {}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ValueError(f"{origin}: not valid YAML ({e})") from e
    problems = validate_cursor_map_doc(doc, origin=origin, in_file=True)
    if problems:
        raise ValueError("invalid cursor map:\n  - " + "\n  - ".join(problems))
    return {k: v for k, v in doc.items() if k != "schema"}


def _config_delta(cfg) -> dict:
    raw = ((cfg or {}).get("agentic") or {}).get("cursor")
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("agentic.cursor must be a mapping")
    problems = validate_cursor_map_doc(raw, origin="config.yaml:agentic.cursor", in_file=False)
    if problems:
        raise ValueError("invalid cursor map:\n  - " + "\n  - ".join(problems))
    return {k: v for k, v in raw.items() if k != "schema"}


def load_cursor_map(cfg=None, *, project_root=None) -> dict:
    """Merged `{defaults, aliases, alias_map}` for this cfg / project, lowest layer first."""
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


def _flatten(doc: dict) -> tuple[dict, dict]:
    """`(defaults, table)` where table keys are `alias` or `alias/effort`."""
    defaults = dict(doc.get("defaults") or {})
    table = {}
    for alias, efforts in (doc.get("aliases") or {}).items():
        if isinstance(efforts, str):
            table[alias] = efforts
            continue
        if isinstance(efforts, dict):
            for effort, mid in efforts.items():
                if _is_str(mid):
                    table[f"{alias}/{effort}"] = mid.strip()
    for k, v in (doc.get("alias_map") or {}).items():
        if _is_str(k) and _is_str(v):
            table[k.strip()] = v.strip()
    return defaults, table


def reject_claude_model(model: str) -> None:
    raw = str(model or "").strip()
    if _CLAUDE_MODEL_RE.match(raw) or raw.startswith("claude-"):
        raise ValueError(f"cursor runner rejects Claude model id {raw!r}")


def claude_family_alias(model) -> str | None:
    """`opus` / `sonnet` / `haiku` when `model` is a `claude-*` family id, else None."""
    raw = str(model or "").strip().lower()
    if not raw.startswith("claude"):
        return None
    if "opus" in raw:
        return "opus"
    if "haiku" in raw:
        return "haiku"
    if "sonnet" in raw:
        return "sonnet"
    return None


def map_launch_model(model, effort=None, cfg=None, *, project_root=None) -> str:
    """SPEC-0437: launch `model` through the YAML map. `claude-sonnet-5-5` is sonnet with
    the YAML default effort (builtin high → grok-4.7-high)."""
    alias = claude_family_alias(model)
    if alias:
        return map_model(alias, None, cfg, project_root=project_root)
    return map_model(model, effort, cfg, project_root=project_root)


def load_sdk_models():
    """Model id → ``{effort_param, efforts}`` from the checked-in account catalog.

    ``effort_param`` is absent when the model has no effort knob. ``efforts``
    are the values that parameter accepts.
    """
    global _sdk_models_cache
    if _sdk_models_cache is None:
        doc = yaml.safe_load(_SDK_MODELS.read_text(encoding="utf-8")) or {}
        models = doc.get("models") or {}
        if not isinstance(models, dict):
            raise ValueError(f"{SDK_MODELS_FILENAME}: models must be a mapping")
        _sdk_models_cache = models
    return _sdk_models_cache


def _legal_effort(requested, allowed):
    """An effort value this model accepts. Nearest rank when the word itself is not."""
    allowed = [v for v in (allowed or []) if v in _EFFORT_RANK]
    if not allowed:
        return None
    for candidate in _EFFORT_PREFER.get(requested, (requested,)):
        if candidate in allowed:
            return candidate
    rank = _EFFORT_RANK.get(requested, 4)
    return min(allowed, key=lambda v: (abs(_EFFORT_RANK[v] - rank), -_EFFORT_RANK[v]))


def _with_fast(spec, params):
    """SPEC-0460. A catalog ``fast`` key means the model has the knob. The value
    sent is always ``false``; the key is presence, not a switch."""
    if isinstance(spec, dict) and "fast" in spec:
        return [*params, {"id": "fast", "value": "false"}]
    return list(params)


def sdk_selection(model_id, catalog=None) -> dict:
    """The SDK model selection for a mapped Cursor id.

    The catalog is the account's ``Cursor.models.list()`` snapshot. A trailing
    effort word is that model's effort parameter, and the parameter name is
    whatever the model declares (``reasoning_effort``, ``reasoning``, or
    ``effort``). ``gpt-5.4-xhigh`` becomes ``reasoning=extra-high`` because
    that model spells the step ``extra-high``. A model with no effort parameter
    drops the suffix and is sent as the bare id. A model whose catalog entry
    has a ``fast`` key is sent ``fast=false``. Context and thinking stay on the
    model's default. An id that matches no catalog entry is passed through
    unchanged.
    """
    raw = str(model_id or "").strip()
    catalog = load_sdk_models() if catalog is None else catalog
    if raw in catalog:
        return {"id": raw, "params": _with_fast(catalog.get(raw) or {}, [])}
    for mid in sorted(catalog, key=len, reverse=True):
        prefix = f"{mid}-"
        if not raw.startswith(prefix):
            continue
        suffix = raw[len(prefix):]
        if suffix not in _EFFORT_RANK:
            return {"id": raw, "params": []}
        spec = catalog.get(mid) or {}
        param = spec.get("effort_param")
        if not param:
            return {"id": mid, "params": _with_fast(spec, [])}
        value = _legal_effort(suffix, spec.get("efforts"))
        if value is None:
            return {"id": mid, "params": _with_fast(spec, [])}
        return {"id": mid, "params": _with_fast(spec, [{"id": param, "value": value}])}
    return {"id": raw, "params": []}


_EFFORT_PARAM_IDS = frozenset({"reasoning_effort", "reasoning", "effort"})


def launch_model_arg(model_id, catalog=None) -> str:
    """The ``cursor-agent --model`` value for this id (SPEC-0488).

    Same selection as :func:`sdk_selection`, formatted as a flat id
    ``model[-effort][-fast]``. Effort comes from the effort parameter; ``-fast``
    is appended only when ``fast`` is ``true``. A selection with no parameters
    stays the bare id (or the original string when the id is not in the catalog).
    """
    sel = sdk_selection(model_id, catalog)
    params = sel["params"]
    if not params:
        return sel["id"]
    base = sel["id"]
    effort_value = None
    fast = False
    for p in params:
        pid = p["id"]
        if pid in _EFFORT_PARAM_IDS:
            effort_value = p["value"]
        elif pid == "fast" and p["value"] == "true":
            fast = True
    if effort_value is not None:
        base = f"{base}-{effort_value}"
    if fast:
        base = f"{base}-fast"
    return base


def map_model(model, effort=None, cfg=None, *, project_root=None, cursor_model=None) -> str:
    """The Cursor id for this spawn. `cursor_model` (profile persona setting) wins."""
    if cursor_model is not None and str(cursor_model).strip():
        raw = str(cursor_model).strip()
        reject_claude_model(raw)
        return raw
    raw = str(model or "").strip()
    if not raw:
        raise ValueError("cursor runner needs a model")
    reject_claude_model(raw)
    if raw not in MODEL_ALIASES:
        return raw
    doc = load_cursor_map(cfg, project_root=project_root)
    defaults, table = _flatten(doc)
    eff = effort if effort in EFFORTS else defaults.get(raw) or "high"
    mapped = table.get(f"{raw}/{eff}") or table.get(raw)
    if not mapped:
        raise ValueError(f"no Cursor mapping for {raw}/{eff}")
    return mapped
