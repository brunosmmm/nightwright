"""Role tiers and the lean-loop flags (SPEC-0553, phase 1 of SPEC-0552).

**Flags.** `agentic.lean` (`{tiers, packet, collator, sessions}`, all off by default) with a
per-project overlay `agentic.project_lean.<project>`. `effective_lean` resolves them for one
project; `store.create_campaign` pins the result into `state.lean` (only when a flag is on,
so a flag-off campaign's state is unchanged) and every later step reads `state.lean`, never the
live config. Unmet dependencies (`collator` without `packet`; `sessions` without `packet` and
`collator`) are switched off with a reason.

**Tiers.** Every persona declares a tier (`marshal | judge | deep`, `persona_defs/*.yaml`).
`tier_map.yaml` maps each tier to a concrete model per backend, with the same overlay chain as
`cursor_map.yaml`: builtin, `<config_dir>/tier_map.yaml`, `agentic.tiers` in config.yaml,
`<project>/.nw/tier_map.yaml`. With `state.lean.tiers` on, `personas.build_request` takes a
persona's model from its tier unless the profile (above the builtin layer) or the model set
names an explicit model for it (`explicit_model`)."""
from __future__ import annotations

from pathlib import Path

import yaml

from .persona_registry import TIERS
from .profiles import EFFORTS, MODEL_ALIASES, MODEL_RE, deep_merge

LEAN_FLAGS = ("tiers", "packet", "collator", "sessions")
LEAN_REQUIRES = {"collator": ("packet",), "sessions": ("packet", "collator")}

SCHEMA_ID = "nw.tier_map.v1"
MAP_FILENAME = "tier_map.yaml"
BACKENDS = ("claude", "cursor", "pi")
_BUILTIN = Path(__file__).resolve().parent / MAP_FILENAME
_ROW_KEYS = frozenset({"model", "effort"})


# ---- flags ----------------------------------------------------------------------------------

def _agentic(cfg) -> dict:
    a = (cfg or {}).get("agentic") if isinstance(cfg, dict) else None
    return a if isinstance(a, dict) else {}


def effective_lean(cfg, project=None) -> tuple[dict, list]:
    """`({flag: bool}, [reason, …])` for a campaign of `project`: `agentic.lean` with
    `agentic.project_lean[project]` over it. Only a literal `true` turns a flag on; a flag
    whose dependencies are off is switched off and the reason says why."""
    a = _agentic(cfg)
    base = a.get("lean") if isinstance(a.get("lean"), dict) else {}
    over_all = a.get("project_lean") if isinstance(a.get("project_lean"), dict) else {}
    over = over_all.get(project) if project and isinstance(over_all.get(project), dict) else {}
    flags = {f: (over[f] if f in over else base.get(f)) is True for f in LEAN_FLAGS}
    reasons = []
    for flag, needs in LEAN_REQUIRES.items():
        missing = [n for n in needs if not flags[n]]
        if flags[flag] and missing:
            flags[flag] = False
            reasons.append(f"{flag} off: needs {' and '.join(missing)}")
    return flags, reasons


def pinned(state) -> dict:
    """The flags a campaign pinned at open (absent = every flag off)."""
    lean = (state or {}).get("lean") if isinstance(state, dict) else None
    return lean if isinstance(lean, dict) else {}


def tiers_on(state) -> bool:
    return pinned(state).get("tiers") is True


# ---- the tier map ---------------------------------------------------------------------------

def _sdk_models() -> dict:
    from . import cursor_map as CM
    return CM.load_sdk_models()


def validate_tier_map_doc(doc, *, origin, in_file) -> list:
    """`"<origin>: <path>: <problem>"` for a tier-map document (a file or a config delta)."""
    from .model_sets import _pi_model_ok
    out = []

    def bad(path, problem):
        out.append(f"{origin}: {path}: {problem}")

    if not isinstance(doc, dict):
        return [f"{origin}: <root>: must be a mapping"]
    if in_file and doc.get("schema") != SCHEMA_ID:
        bad("schema", f"must be {SCHEMA_ID!r}")
    elif "schema" in doc and doc["schema"] != SCHEMA_ID:
        bad("schema", f"must be {SCHEMA_ID!r}")
    catalog = None
    for backend, rows in doc.items():
        if backend == "schema":
            continue
        if backend not in BACKENDS:
            bad(backend, f"unknown backend; known: {', '.join(BACKENDS)}")
            continue
        if not isinstance(rows, dict):
            bad(backend, "must be a mapping of tier to {model, effort}")
            continue
        for tier, row in rows.items():
            path = f"{backend}.{tier}"
            if tier not in TIERS:
                bad(path, f"unknown tier; known: {', '.join(TIERS)}")
                continue
            if not isinstance(row, dict):
                bad(path, "must be a mapping {model, effort}")
                continue
            for key in row:
                if key not in _ROW_KEYS:
                    bad(f"{path}.{key}", "unknown key")
            model, effort = row.get("model"), row.get("effort")
            if "effort" in row and effort not in EFFORTS:
                bad(f"{path}.effort", f"must be one of {', '.join(EFFORTS)}, got {effort!r}")
                effort = None
            if "model" not in row:
                continue                    # a delta may change only the effort
            if not (isinstance(model, str) and model.strip()):
                bad(f"{path}.model", f"must be a model id, got {model!r}")
                continue
            if backend == "claude":
                if not (model in MODEL_ALIASES or MODEL_RE.match(model)):
                    bad(f"{path}.model", f"must be a claude-* id or {'|'.join(MODEL_ALIASES)}, "
                                         f"got {model!r}")
            elif backend == "cursor":
                if model.startswith("claude-") or model in MODEL_ALIASES:
                    bad(f"{path}.model", f"must be a Cursor id, not a Claude one: {model!r}")
                    continue
                catalog = _sdk_models() if catalog is None else catalog
                spec = catalog.get(model)
                allowed = (spec or {}).get("efforts") if isinstance(spec, dict) else None
                if effort and allowed and effort not in allowed:
                    bad(f"{path}.effort", f"{model} takes {', '.join(allowed)} "
                                          f"(sdk_models.yaml), got {effort!r}")
            elif not _pi_model_ok(model):
                bad(f"{path}.model", f"pi needs provider/id, got {model!r}")
    return out


def _read_file(path: Path, *, origin, required: bool) -> dict:
    if not path.is_file():
        if required:
            raise ValueError(f"{origin}: missing {path}")
        return {}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ValueError(f"{origin}: not valid YAML ({' '.join(str(e).split())})") from e
    problems = validate_tier_map_doc(doc, origin=origin, in_file=True)
    if problems:
        raise ValueError("invalid tier map:\n  - " + "\n  - ".join(problems))
    return {k: v for k, v in doc.items() if k != "schema"}


def _config_delta(cfg) -> dict:
    raw = _agentic(cfg).get("tiers")
    if not raw:
        return {}
    problems = validate_tier_map_doc(raw, origin="config.yaml:agentic.tiers", in_file=False)
    if problems:
        raise ValueError("invalid tier map:\n  - " + "\n  - ".join(problems))
    return {k: v for k, v in raw.items() if k != "schema"}


def load_tier_map(cfg=None, *, project_root=None) -> dict:
    """`{backend: {tier: {model, effort?}}}`, the layers merged lowest first."""
    layers = [_read_file(_BUILTIN, origin=f"builtin:{MAP_FILENAME}", required=True)]
    cfg_dir = (cfg or {}).get("config_dir") if isinstance(cfg, dict) else None
    if cfg_dir:
        layers.append(_read_file(Path(cfg_dir) / MAP_FILENAME,
                                 origin=f"{cfg_dir}/{MAP_FILENAME}", required=False))
    layers.append(_config_delta(cfg))
    if project_root:
        layers.append(_read_file(Path(project_root) / ".nw" / MAP_FILENAME,
                                 origin=f"{project_root}/.nw/{MAP_FILENAME}", required=False))
    out: dict = {}
    for delta in layers:
        if delta:
            out = deep_merge(out, delta)
    return out


def tier_model(cfg, backend, tier, project_root=None) -> dict:
    """`{model, effort}` of `tier` on `backend` (`effort` may be None). A backend other than
    cursor / pi (claude, the test runner) reads the claude row. ValueError when the merged map
    has no model for it."""
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}; known: {', '.join(TIERS)}")
    which = backend if backend in ("cursor", "pi") else "claude"
    row = (load_tier_map(cfg, project_root=project_root).get(which) or {}).get(tier) or {}
    model = row.get("model")
    if not model:
        raise ValueError(f"tier map has no {which}.{tier}.model")
    return {"model": str(model).strip(), "effort": row.get("effort")}


# ---- which tier, and is the model pinned by hand --------------------------------------------

DEEP_PERSONA = "adversarial_reviewer"


def deep_pass_pending(state) -> bool:
    """A hand-raised campaign's one deep adversarial pass at close is due (SPEC-0553)."""
    dc = (state or {}).get("deep_close") if isinstance(state, dict) else None
    return isinstance(dc, dict) and dc.get("status") == "pending"


def persona_tier(persona, state) -> str:
    """The tier of this run: the persona's declared tier, or `deep` for the adversarial
    reviewer's close pass of a hand-raised campaign. `persona` is a `personas.Persona`."""
    if getattr(persona, "name", None) == DEEP_PERSONA and deep_pass_pending(state):
        return "deep"
    tier = getattr(persona, "tier", None)
    return tier if tier in TIERS else "judge"


def _model_origin(cfg, profile, name, project_root) -> str | None:
    """The layer origin of `personas.<name>.model` in `profile`, or None (unknown profile,
    hand-built profile dict, or no such key)."""
    from .profiles import profile_provenance
    pname = (profile or {}).get("name")
    if not pname:
        return None
    try:
        prov = profile_provenance(cfg, pname, project_root=project_root)
    except (ValueError, OSError):
        return None
    return prov.get(f"personas.{name}.model")


def explicit_model(cfg, profile, persona, *, model_set=None, backend="claude",
                   project_root=None) -> bool:
    """True when something above the builtin layer names this persona's model, which then
    overrides its tier: a model set's `personas.<name>.model`, a `cursor_model` on a cursor
    spawn, a user/config/project profile layer's `personas.<name>.model`, or a declared
    persona's own `model:`. The builtin profiles' aliases are what tiers replace."""
    name = persona.name
    over = ((model_set or {}).get("personas") or {}).get(name)
    if isinstance(over, dict) and over.get("model"):
        return True
    settings = ((profile or {}).get("personas") or {}).get(name) or {}
    if backend == "cursor" and isinstance(settings, dict) and settings.get("cursor_model"):
        return True
    declared = getattr(persona, "declared", None)
    if declared is not None and "model" in ((declared or {}).get("settings") or {}):
        return True
    origin = _model_origin(cfg, profile, name, project_root)
    return origin is not None and not origin.startswith("builtin:")
