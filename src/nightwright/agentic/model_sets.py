"""Model sets (SPEC-0455): a selectable binding of runner and concrete model.

`balanced`, `frugal` and `thorough` stay workflow shapes (budgets, slots, reviewers,
and which persona is the strong alias). A model set says which backend speaks for
each persona and which concrete model that backend gets. One set is Claude Code.
One is Composer plus Grok. One sends writers to Claude and reviewers to Grok.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from . import cursor_map as CM
from .personas import PERSONAS

SCHEMA = "nw.model_set.v1"
RUNNERS = ("claude", "cursor", "pi")
_DEFS = Path(__file__).resolve().parent / "model_set_defs"
_KEYS = frozenset({"schema", "description", "runner", "model", "personas"})
_PERSONA_KEYS = frozenset({"runner", "model"})


def _read_file(path: Path) -> tuple[dict, list[str]]:
    origin = str(path)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as e:
        return {}, [f"{origin}: {' '.join(str(e).split())}"]
    return (doc if isinstance(doc, dict) else {}), _problems(doc, origin)


def _problems(doc, origin) -> list[str]:
    if not isinstance(doc, dict):
        return [f"{origin}: must be a mapping"]
    out = []
    if doc.get("schema") != SCHEMA:
        out.append(f"{origin}: schema must be {SCHEMA}")
    for key in doc:
        if key not in _KEYS:
            out.append(f"{origin}: unknown key {key}")
    runner = doc.get("runner")
    if runner not in RUNNERS:
        out.append(f"{origin}: runner must be {' or '.join(RUNNERS)}, got {runner!r}")
    model = doc.get("model")
    if model is not None and not _model_ok(model):
        out.append(f"{origin}: model must be a non-Claude id, got {model!r}")
    personas = doc.get("personas") or {}
    if not isinstance(personas, dict):
        out.append(f"{origin}: personas must be a mapping")
        return out
    for name, settings in personas.items():
        if name not in PERSONAS:
            out.append(f"{origin}: unknown persona {name}")
            continue
        if not isinstance(settings, dict):
            out.append(f"{origin}: personas.{name} must be a mapping")
            continue
        for key in settings:
            if key not in _PERSONA_KEYS:
                out.append(f"{origin}: personas.{name}.{key} is unknown")
        if "runner" in settings and settings["runner"] not in RUNNERS:
            out.append(f"{origin}: personas.{name}.runner must be {' or '.join(RUNNERS)}")
        if "model" in settings and not _model_ok(settings["model"]):
            out.append(f"{origin}: personas.{name}.model must be a non-Claude id")
        which = settings.get("runner", runner)
        selected = settings.get("model") or model
        if which in ("cursor", "pi") and not selected:
            out.append(f"{origin}: personas.{name} on {which} needs a model")
        if which == "pi" and selected and not _pi_model_ok(selected):
            out.append(f"{origin}: personas.{name} on pi needs provider/id")
    if runner == "pi" and not _pi_model_ok(model):
        out.append(f"{origin}: pi needs a provider/id model")
    return out


def _pi_model_ok(model) -> bool:
    if not isinstance(model, str) or model.count("/") != 1 or any(c.isspace() for c in model):
        return False
    provider, model_id = model.split("/", 1)
    import re
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", provider) and model_id)


def _model_ok(model) -> bool:
    if not isinstance(model, str) or not model.strip():
        return False
    raw = model.strip()
    return not (raw.startswith("claude-") or raw in ("opus", "sonnet", "haiku"))


def _load(cfg, *, project_root=None) -> dict[str, tuple[dict, list[str]]]:
    """`{name: (doc, problems)}`, later layers replacing earlier ones."""
    found: dict[str, tuple[dict, list[str]]] = {}
    for path in sorted(_DEFS.glob("*.yaml")):
        found[path.stem] = _read_file(path)
    cfg_dir = (cfg or {}).get("config_dir")
    if cfg_dir:
        user = Path(cfg_dir) / "model_sets"
        if user.is_dir():
            for path in sorted(user.glob("*.yaml")):
                found[path.stem] = _read_file(path)
    if project_root:
        proj = Path(project_root) / ".nw" / "model_sets"
        if proj.is_dir():
            for path in sorted(proj.glob("*.yaml")):
                found[path.stem] = _read_file(path)
    return found


def known_model_sets(cfg=None, *, project_root=None) -> list[str]:
    return sorted(_load(cfg, project_root=project_root))


def resolve_model_set(cfg, *, project_root=None) -> dict | None:
    """The selected set, or None when `agentic.model_set` is unset (today's runner)."""
    name = ((cfg or {}).get("agentic") or {}).get("model_set")
    if not name:
        return None
    found = _load(cfg, project_root=project_root)
    if name not in found:
        raise ValueError(f"unknown model set {name!r}; known: {', '.join(sorted(found))}")
    doc, problems = found[name]
    if problems:
        raise ValueError("invalid model set:\n  - " + "\n  - ".join(problems))
    return {**doc, "name": name}


def uses_cursor(model_set: dict | None) -> bool:
    if not model_set:
        return False
    if model_set.get("runner") == "cursor":
        return True
    return any((s or {}).get("runner") == "cursor" for s in (model_set.get("personas") or {}).values()
               if isinstance(s, dict))


def uses_pi(model_set: dict | None) -> bool:
    if not model_set:
        return False
    return model_set.get("runner") == "pi" or any(
        isinstance(s, dict) and s.get("runner") == "pi"
        for s in (model_set.get("personas") or {}).values())


def cursor_id(base: str, effort: str | None) -> str:
    """`grok-4.7` plus a profile effort becomes `grok-4.7-high`. A model with no
    effort parameter stays the bare id, so Composer ignores the profile effort."""
    catalog = CM.load_sdk_models()
    spec = catalog.get(base)
    if not isinstance(spec, dict) or not spec.get("effort_param") or not effort:
        return base
    value = CM._legal_effort(effort, spec.get("efforts"))
    return f"{base}-{value}" if value else base


def bind(model_set: dict, persona: str, alias: str, effort: str | None) -> dict:
    """`{runner, model, model_alias}` for one persona under this set.

    Claude keeps the profile alias (`opus` / `sonnet` / `haiku`). Cursor gets the
    set's model id, with the profile's effort applied when that model has one.
    """
    over = (model_set.get("personas") or {}).get(persona) or {}
    if not isinstance(over, dict):
        over = {}
    runner = over.get("runner") or model_set.get("runner") or "claude"
    if runner == "claude":
        return {"runner": "claude", "model": str(alias or "sonnet"), "model_alias": None}
    base = str(over.get("model") or model_set.get("model") or "").strip()
    if runner == "pi":
        if not _pi_model_ok(base):
            raise ValueError(f"pi model must be provider/id, got {base!r}")
        return {"runner": "pi", "model": base, "model_alias": str(alias or "sonnet")}
    return {"runner": "cursor", "model": cursor_id(base, effort), "model_alias": str(alias or "sonnet")}
