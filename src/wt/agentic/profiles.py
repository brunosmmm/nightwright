"""Workflow profiles (SPEC-0171, SPEC-0237): which model / effort / budget each persona runs
with, how many loops and slots a campaign gets, and the campaign dollar cap.

A profile is a YAML document (`wt.profile.v1`). Four layers define them, lowest first: the
builtin files in `profile_defs/`, `<config_dir>/profiles/*.yaml`, the `agentic.profiles` deltas
in `config.yaml`, and `<project main checkout>/.wt/profiles/*.yaml`. A definition is complete,
or a delta over the same name in a lower layer, or over `extends:`. `load_config` merges user
config one level deep (config.py), so a user `agentic:` block would replace `profiles`
wholesale — `resolve_profile` therefore deep-merges the layers here, and users write only what
differs."""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Canonical reviewer order (SPEC-0205, SPEC-0202): a profile's default `reviewers` list follows it.
REVIEWER_ORDER = ("conformance_reviewer", "test_reviewer", "adversarial_reviewer")

# SPEC-0203: severity weights and the pass threshold of the review triage score.
SEVERITIES = ("must_fix", "major", "minor", "nit")
DEFAULT_WEIGHTS = {"must_fix": 100, "major": 15, "minor": 4, "nit": 1}
DEFAULT_PASS_SCORE = 80
REVIEW_KEYS = frozenset({"pass_score", "weights", "triager", "followups", "followups_nits",
                           "followups_max"})


def deep_merge(base, delta):
    """Recursive dict merge: mappings merge key by key, anything else from `delta` wins."""
    if not isinstance(base, dict) or not isinstance(delta, dict):
        return copy.deepcopy(delta)
    out = {k: copy.deepcopy(v) for k, v in base.items()}
    for k, v in delta.items():
        out[k] = deep_merge(out[k], v) if k in out else copy.deepcopy(v)
    return out


def _agentic(cfg):
    return (cfg or {}).get("agentic") or {}


SCHEMA_ID = "wt.profile.v1"
PROFILE_KEYS = frozenset({"schema", "extends", "description", "slots", "loop_max",
                          "campaign_cap_usd", "run_timeout_s", "reviewers", "review", "personas"})
REQUIRED_KEYS = ("slots", "loop_max", "campaign_cap_usd", "run_timeout_s", "reviewers", "review",
                 "personas")
META_KEYS = ("schema", "extends", "description")      # in a file, not part of the resolved profile
PERSONA_SETTING_KEYS = frozenset({"model", "effort", "max_budget_usd", "permission_mode",
                                  "read_only"})
MODEL_ALIASES = ("opus", "sonnet", "haiku")
MODEL_RE = re.compile(r"^claude-[a-z0-9][a-z0-9.-]*(\[1m\])?$")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PERMISSION_MODES = ("default", "acceptEdits", "bypassPermissions", "dontAsk", "plan")
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
SCOPES = ("builtin", "user", "config", "project")     # layers, lowest first
_DEFS_DIR = Path(__file__).resolve().parent / "profile_defs"


@dataclass
class Def:
    """One profile definition in one layer: where it came from, its document, and the
    validation errors (a non-empty `errors` makes resolving this profile raise)."""
    origin: str
    doc: dict
    errors: list = field(default_factory=list)


@dataclass
class Layer:
    scope: str
    defs: dict


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_profile_doc(doc, *, origin, in_file) -> list:
    """Messages `"<origin>: <key.path>: <problem>"` for a profile document (a file or a
    `config.yaml` delta); empty when it is valid. `schema` is required in files only."""
    from . import personas as PS      # personas imports this module
    out = []

    def bad(path, problem):
        out.append(f"{origin}: {path}: {problem}")

    if not isinstance(doc, dict):
        bad("<root>", f"must be a mapping, got {type(doc).__name__}")
        return out
    for key in doc:
        if key not in PROFILE_KEYS:
            bad(key, "unknown key")
    if in_file and doc.get("schema") != SCHEMA_ID:
        bad("schema", f"must be {SCHEMA_ID!r}, got {doc.get('schema')!r}" if "schema" in doc
            else f"is required (schema: {SCHEMA_ID})")
    elif not in_file and "schema" in doc and doc["schema"] != SCHEMA_ID:
        bad("schema", f"must be {SCHEMA_ID!r}, got {doc['schema']!r}")
    for key in ("extends", "description"):
        if key in doc and not isinstance(doc[key], str):
            bad(key, f"must be a string, got {doc[key]!r}")
    for key in ("slots", "loop_max", "run_timeout_s"):
        if key in doc and (not _is_int(doc[key]) or doc[key] < 1):
            bad(key, f"must be an integer >= 1, got {doc[key]!r}")
    if "campaign_cap_usd" in doc:
        cap = doc["campaign_cap_usd"]
        if cap is not None and (not _is_num(cap) or cap <= 0):
            bad("campaign_cap_usd", f"must be a number > 0 or null, got {cap!r}")
    if "reviewers" in doc:
        rv = doc["reviewers"]
        if not isinstance(rv, list) or not all(isinstance(r, str) for r in rv):
            bad("reviewers", f"must be a list of strings, got {rv!r}")
    if "review" in doc:
        review = doc["review"]
        if review is None:
            bad("review", "must be a mapping, got None")
        else:
            for path, problem in _review_problems(review, partial=True):
                bad(path, problem)
    if "personas" in doc:
        personas = doc["personas"]
        if not isinstance(personas, dict):
            bad("personas", f"must be a mapping, got {type(personas).__name__}")
            personas = {}
        for pname, settings in personas.items():
            path = f"personas.{pname}"
            if pname not in PS.PERSONAS:
                bad(path, f"unknown persona; known: {', '.join(sorted(PS.PERSONAS))}")
            if not isinstance(settings, dict):
                bad(path, f"must be a mapping, got {type(settings).__name__}")
                continue
            for key in settings:
                if key not in PERSONA_SETTING_KEYS:
                    bad(f"{path}.{key}", "unknown key")
            model = settings.get("model")
            if "model" in settings and not (isinstance(model, str) and (
                    model in MODEL_ALIASES or MODEL_RE.match(model))):
                bad(f"{path}.model", f"must be one of {', '.join(MODEL_ALIASES)} or a "
                    f"claude-* model id, got {model!r}")
            if "effort" in settings and settings["effort"] not in EFFORTS:
                bad(f"{path}.effort", f"must be one of {', '.join(EFFORTS)}, "
                    f"got {settings['effort']!r}")
            if "max_budget_usd" in settings and (not _is_num(settings["max_budget_usd"])
                                                 or settings["max_budget_usd"] <= 0):
                bad(f"{path}.max_budget_usd", f"must be a number > 0, "
                    f"got {settings['max_budget_usd']!r}")
            if "permission_mode" in settings and settings["permission_mode"] not in PERMISSION_MODES:
                bad(f"{path}.permission_mode", f"must be one of {', '.join(PERMISSION_MODES)}, "
                    f"got {settings['permission_mode']!r}")
            if "read_only" in settings and not isinstance(settings["read_only"], bool):
                bad(f"{path}.read_only", f"must be true or false, got {settings['read_only']!r}")
    return out


# ---- layers ----------------------------------------------------------------------------------

def _read_dir(directory, origin_of) -> dict:
    """`{name: Def}` for the `*.yaml` files of `directory` (missing directory: none). A `*.yml`
    file or a file whose stem is not a valid name defines a broken profile; other files are
    ignored."""
    defs = {}
    if not directory.is_dir():
        return defs
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix not in (".yaml", ".yml"):
            continue
        name, origin = path.stem, origin_of(path)
        if path.suffix == ".yml":
            defs[name] = Def(origin, {}, [f"{origin}: <file>: rename to .yaml"])
            continue
        if not NAME_RE.match(name):
            defs[name] = Def(origin, {}, [f"{origin}: <file>: profile name {name!r} must match "
                                         f"{NAME_RE.pattern}"])
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
            defs[name] = Def(origin, {}, [f"{origin}: {' '.join(str(e).split())}"])
            continue
        errors = validate_profile_doc(doc, origin=origin, in_file=True)
        defs[name] = Def(origin, doc if isinstance(doc, dict) else {}, errors)
    return defs


def _builtin_layer() -> Layer:
    return Layer("builtin", _read_dir(_DEFS_DIR, lambda p: f"builtin:{p.name}"))


def load_layers(cfg, *, project_root=None) -> list:
    """The four layers, lowest first. Every file is read and validated on each call."""
    layers = [_builtin_layer()]
    cfg_dir = (cfg or {}).get("config_dir")
    user = _read_dir(Path(cfg_dir) / "profiles", str) if cfg_dir else {}
    layers.append(Layer("user", user))
    deltas = {}
    for name, doc in (_agentic(cfg).get("profiles") or {}).items():
        origin = f"config.yaml:agentic.profiles.{name}"
        doc = {} if doc is None else doc
        deltas[name] = Def(origin, doc if isinstance(doc, dict) else {},
                           validate_profile_doc(doc, origin=origin, in_file=False))
    layers.append(Layer("config", deltas))
    project = (_read_dir(Path(project_root) / ".wt" / "profiles", str) if project_root else {})
    layers.append(Layer("project", project))
    return layers


def _chain(layers, name, top, stack=()) -> list | None:
    """`[(origin, delta), …]` lowest first for `name` as seen from layer index `top`, or None
    when no layer up to `top` defines it."""
    j = next((i for i in range(top, -1, -1) if name in layers[i].defs), None)
    if j is None:
        return None
    if (name, j) in stack:
        names = [n for n, _ in stack]
        raise ValueError("profile extends cycle: "
                         f"{' → '.join(names[stack.index((name, j)):] + [name])}")
    d = layers[j].defs[name]
    if d.errors:
        raise ValueError("; ".join(d.errors))
    here = stack + ((name, j),)
    extends = d.doc.get("extends")
    if extends and extends != name:
        base = _chain(layers, extends, len(layers) - 1, here)
        if base is None:
            raise ValueError(f"unknown profile {extends!r} (extended by {d.origin})")
    elif extends == name or any(name in layers[i].defs for i in range(j)):
        base = _chain(layers, name, j - 1, here) if j else None
        if base is None:
            raise ValueError(f"unknown profile {name!r} (extended by {d.origin}): "
                             f"no lower layer defines it")
    elif layers[j].scope == "config":                 # a config-only new name sits over balanced
        base = _chain(layers, "balanced", len(layers) - 1, here)
    else:
        base = []
    return (base or []) + [(d.origin, d.doc)]


def _chain_of(layers, name) -> list:
    chain = _chain(layers, name, len(layers) - 1)
    if chain is None:
        raise ValueError(f"unknown profile {name!r}; known: {', '.join(_known(layers))}")
    return chain


def profile_chain(cfg, name, *, project_root=None) -> list:
    """The `(origin, delta)` pairs, lowest first, that make up profile `name`."""
    return _chain_of(load_layers(cfg, project_root=project_root), name)


def _known(layers) -> list:
    return sorted({n for layer in layers for n in layer.defs})


def known_profiles(cfg, *, project_root=None):
    """Names defined in any layer (broken ones included; resolving those raises), sorted."""
    return _known(load_layers(cfg, project_root=project_root))


def _persona_names() -> tuple:
    from . import personas as PS
    return tuple(PS.PERSONAS)


def _resolve(layers, name) -> dict:
    chain = _chain_of(layers, name)
    profile = {}
    for _, delta in chain:
        profile = deep_merge(profile, {k: v for k, v in delta.items() if k not in META_KEYS})
    profile["name"] = name
    profile["description"] = next((d["description"] for _, d in reversed(chain)
                                   if d.get("description") is not None), "")
    profile["sources"] = list(dict.fromkeys(origin for origin, _ in chain))
    top = chain[-1][0]

    def missing(path):
        raise ValueError(f"profile {name} ({top}): missing {path} "
                         f"(add it or `extends: <profile>`)")

    for key in REQUIRED_KEYS:
        if key not in profile:
            missing(key)
    _check_reviewers(profile)
    _check_review(profile)
    for persona in _persona_names():
        if persona not in profile["personas"]:
            missing(f"personas.{persona}")
    return profile


# Spec sizing and tone pick the profile (SPEC-0219): tone -> size -> profile. `xl` is "too big
# for one campaign" (the review verdict is `split`), so it has no cell.
SIZES = ("xs", "s", "m", "l", "xl")
TONES = ("cost-effective", "high-end", "critical")
PROFILE_MATRIX = {
    "cost-effective": {"xs": "frugal", "s": "frugal", "m": "balanced", "l": "balanced"},
    "high-end": {"xs": "balanced", "s": "balanced", "m": "balanced", "l": "thorough"},
    "critical": {"xs": "thorough", "s": "thorough", "m": "thorough", "l": "thorough"},
}


def profile_matrix(cfg, *, project_root=None):
    """The builtin matrix with `agentic.profile_matrix` deep-merged over it. ValueError naming
    the cell for an unknown tone, size or profile, or for an `xl` cell."""
    delta = _agentic(cfg).get("profile_matrix") or {}
    matrix = deep_merge(PROFILE_MATRIX, delta)
    known = known_profiles(cfg, project_root=project_root)
    for tone, row in delta.items():
        if tone not in TONES:
            raise ValueError(f"profile_matrix: unknown tone {tone!r}; known: {', '.join(TONES)}")
        if not isinstance(row, dict):
            raise ValueError(f"profile_matrix[{tone}]: expected a mapping of size to profile")
        for size, name in row.items():
            if size == "xl":
                raise ValueError(f"profile_matrix[{tone}][xl]: xl has no profile "
                                 f"(too big for one campaign: split it)")
            if size not in SIZES:
                raise ValueError(f"profile_matrix[{tone}][{size}]: unknown size {size!r}; "
                                 f"known: {', '.join(s for s in SIZES if s != 'xl')}")
            if name not in known:
                raise ValueError(f"profile_matrix[{tone}][{size}]: unknown profile {name!r}; "
                                 f"known: {', '.join(known)}")
    return matrix


def select_profile(cfg, *, flag=None, review=None, project_root=None) -> dict:
    """Pick the campaign profile: `--profile` flag > the approving review's size and tone via
    the matrix > `agentic.default_profile`. Pure (no I/O beyond `cfg`). Returns
    `{profile, source: flag|review|default, reason, size, tone}`; size and tone are recorded
    whenever the review has valid ones, even when the flag wins."""
    matrix = profile_matrix(cfg, project_root=project_root)
    review = review or {}
    size, tone = review.get("size"), review.get("tone")
    sized = (size in SIZES and tone in TONES) and review.get("verdict", "approve") == "approve"
    if not sized:
        size = tone = None
    if flag:
        return {"profile": flag, "source": "flag", "reason": f"--profile {flag}",
                "size": size, "tone": tone}
    if sized and size != "xl":
        name = matrix[tone][size]
        return {"profile": name, "source": "review", "size": size, "tone": tone,
                "reason": f"size {size}, tone {tone} → {name}"}
    default = _agentic(cfg).get("default_profile") or "balanced"
    if not review:
        why = "no approving review (--force)"
    elif sized:
        why = f"size {size} has no profile (split recommended)"
    else:
        why = "review predates sizing (no size/tone)"
    return {"profile": default, "source": "default", "reason": why, "size": size, "tone": tone}


def resolve_profile(cfg, name=None, *, project_root=None):
    """The effective profile dict, with `name`, `description` and `sources` (the origins that
    make it up, lowest layer first). `name` defaults to `agentic.default_profile`. A definition
    in a higher layer merges over the same name below, or over `extends:`; a `config.yaml`-only
    new name merges over `balanced`. Unknown names raise ValueError listing the known profiles.
    `project_root` is the project's main checkout, for its `.wt/profiles` layer."""
    name = name or _agentic(cfg).get("default_profile") or "balanced"
    return _resolve(load_layers(cfg, project_root=project_root), name)


def builtin_profiles() -> dict:
    """The resolved builtin layer, `{name: profile}` as deep copies, without the `name`,
    `description` and `sources` keys."""
    layers = [_builtin_layer()]
    return {n: {k: v for k, v in _resolve(layers, n).items()
                if k not in ("name", "description", "sources")} for n in sorted(layers[0].defs)}


def reviewers_of(profile):
    """The effective reviewer personas for REVIEWING (SPEC-0201): `profile["reviewers"]`, or
    `["conformance_reviewer"]` when the key is absent or empty (SPEC-0187 behavior)."""
    return list(profile.get("reviewers") or []) or ["conformance_reviewer"]


def _check_reviewers(profile):
    """ValueError naming the persona and the profile for a `reviewers` entry that is unknown,
    duplicated, not a registered reviewer, not read-only, or has no settings in the profile."""
    from . import personas as PS      # personas imports this module
    name = profile.get("name")
    seen = set()
    for r in profile.get("reviewers") or []:
        if r in seen:
            raise ValueError(f"profile {name!r}: reviewer {r!r} is listed twice")
        seen.add(r)
        if r not in PS.PERSONAS:
            raise ValueError(f"profile {name!r}: unknown reviewer persona {r!r}")
        if r not in PS.REVIEWER_PERSONAS:
            raise ValueError(f"profile {name!r}: persona {r!r} is not a reviewer persona")
        settings = (profile.get("personas") or {}).get(r)
        if settings is None:
            raise ValueError(f"profile {name!r}: reviewer {r!r} has no persona settings")
        if not PS.PERSONAS[r].read_only or not settings.get("read_only", True):
            raise ValueError(f"profile {name!r}: reviewer {r!r} must be read_only")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _review_problems(review, *, partial=False) -> list:
    """`(key.path, problem)` pairs for a bad `review:` block (SPEC-0203): only `pass_score`
    (int 0..100), `weights` (exactly the four severities, ints >= 0; no bools), `triager`
    (bool, SPEC-0206) and the SPEC-0207 booleans `followups` / `followups_nits`, and the SPEC-0239 cap `followups_max` (int >= 0, 0 = no cap). A `partial`
    block (one layer's delta) may leave out weights; the merged profile may not."""
    if not isinstance(review, dict):
        return [("review", "must be a mapping")]
    out = [(f"review.{key}", "unknown key") for key in review if key not in REVIEW_KEYS]
    if "pass_score" in review:
        ps = review["pass_score"]
        if not _is_int(ps) or not 0 <= ps <= 100:
            out.append(("review.pass_score", f"must be an integer 0..100, got {ps!r}"))
    if "triager" in review and not isinstance(review["triager"], bool):
        out.append(("review.triager", f"must be true or false, got {review['triager']!r}"))
    for key in ("followups", "followups_nits"):
        if key in review and not isinstance(review[key], bool):
            out.append((f"review.{key}", f"must be a boolean, got {review[key]!r}"))
    if "followups_max" in review and (not _is_int(review["followups_max"])
                                      or review["followups_max"] < 0):
        out.append(("review.followups_max",
                    f"must be an integer >= 0, got {review['followups_max']!r}"))
    if "weights" in review:
        weights = review["weights"]
        if not isinstance(weights, dict):
            out.append(("review.weights", "must be a mapping"))
        else:
            out += [(f"review.weights.{key}", "unknown key") for key in weights
                    if key not in SEVERITIES]
            for key in SEVERITIES:
                if key not in weights:
                    if not partial:
                        out.append((f"review.weights.{key}", "is missing"))
                elif not _is_int(weights[key]) or weights[key] < 0:
                    out.append((f"review.weights.{key}",
                                f"must be an integer >= 0, got {weights[key]!r}"))
    return out


def _check_review(profile):
    """ValueError naming the profile and the key for a bad `review:` block (SPEC-0203)."""
    review = profile.get("review")
    if review is None:
        return
    for path, problem in _review_problems(review):
        raise ValueError(f"profile {profile.get('name')!r}: {path}: {problem}")


def review_params(profile) -> tuple:
    """`(pass_score, weights)` of `profile["review"]`; a hand-built profile without it (or
    with part of it) uses the `balanced` values."""
    review = (profile or {}).get("review") or {}
    ps = review.get("pass_score")
    weights = dict(DEFAULT_WEIGHTS)
    weights.update(review.get("weights") or {})
    return (DEFAULT_PASS_SCORE if ps is None else ps), weights


def triager_enabled(profile) -> bool:
    """Is the review triager (SPEC-0206) on? A hand-built profile without `review.triager`
    has it off."""
    review = (profile or {}).get("review")
    return isinstance(review, dict) and review.get("triager") is True


def followup_params(profile) -> tuple:
    """`(followups, followups_nits)` of `profile["review"]` (SPEC-0207); a hand-built profile
    without them behaves as the defaults (file follow-ups, no nits)."""
    review = (profile or {}).get("review") or {}
    return bool(review.get("followups", True)), bool(review.get("followups_nits", False))


def followup_cap(profile) -> int:
    """`review.followups_max` (SPEC-0239): findings listed per campaign, 0 = no cap; 10 when a
    hand-built profile lacks it."""
    review = (profile or {}).get("review") or {}
    cap = review.get("followups_max")
    return 10 if cap is None else cap


def persona_settings(profile, persona):
    """`profile["personas"][persona]` or ValueError naming the persona and the profile."""
    personas = profile.get("personas") or {}
    if persona not in personas:
        raise ValueError(f"profile {profile.get('name')!r} has no persona {persona!r}; "
                         f"known: {', '.join(sorted(personas))}")
    return personas[persona]
