"""Persona backend preflight (SPEC-0484 / SPEC-0486): a missing credential or SDK is an
environment fault, not a verdict.

- `check(kind)` is the cheap, in-process probe of a backend (no billed request, no
  subprocess for pi): `cursor` uses `probe_cursor_sdk`; `pi` checks node, the script, the SDK
  entry, and named provider credentials.
- `env_fault(result)` recognises startup failure texts from Cursor and Pi children.
- `note_fault(cfg, kind, error)` journals `backend_preflight_failed` once per process and
  fault, and files one queue-wide escalation (`kind: backend_unavailable`) while none is open
  for that backend. Never raises; never writes a credential value."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from pathlib import Path

from . import runproc as RP

ESC_KIND = "backend_unavailable"
HOLD = "backend_unavailable"
_PI_PKG = "pi-" + "coding-agent"
_SDK_SCOPE = "@" + "earendil-works/" + _PI_PKG
_SDK_PKG = "node_modules/" + _SDK_SCOPE
_ENV_FAULT_RE = re.compile(
    r"CURSOR_API_KEY is not set|cursor-sdk is not installed|"
    + re.escape(_PI_PKG)
    + r" is not installed|no credential for provider \S+|"
    r"node not found on PATH|pi_exec\.mjs missing(?: at \S+)?")
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]*$")
_NOTED: set = set()
_NOTED_LOCK = threading.Lock()


def _environ(environ):
    return os.environ if environ is None else environ


def _home(environ) -> Path:
    env = _environ(environ)
    home = env.get("HOME")
    if home and str(home).strip():
        return Path(str(home).strip())
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):
        return Path(os.path.expanduser("~"))


def _expand(path: str, environ) -> Path:
    if path.startswith("~/"):
        remainder = path[2:]
        if os.path.isabs(remainder):
            return _home(environ) / ".nw-invalid-tilde" / remainder.lstrip(os.sep)
        return _home(environ) / remainder
    if path == "~":
        return _home(environ)
    return Path(os.path.expandvars(path) if environ is None else path)


def _nonempty(env, name: str) -> bool:
    return bool(str(env.get(name) or "").strip())


def _is_file(env, name: str) -> bool:
    raw = env.get(name)
    if not raw or not str(raw).strip():
        return False
    return _expand(str(raw).strip(), env).is_file()


def spawn_backend(cfg, state: dict, persona: str) -> tuple[str, list[str]]:
    """The backend and Pi providers `build_request` would spawn for `persona`. May raise."""
    from . import personas
    from . import project
    from . import profiles
    from . import model_sets
    from . import runner
    from .persona_defs import user_personas_dir

    proj = state.get("project")
    project_root = project.project_config(cfg, proj)["_root"] if proj else None
    profile = profiles.resolve_profile(
        cfg, state.get("profile"),
        project_root=(state.get("repo") or {}).get("root"))
    if persona in personas.PERSONAS:
        settings = profiles.persona_settings(profile, persona)
    else:
        p = personas.get_persona(
            persona, project_root, user_dir=user_personas_dir(cfg))
        settings = personas.declared_settings(profile, p)
    alias = str(settings.get("model") or "sonnet")
    effort = settings.get("effort")
    chosen = model_sets.resolve_model_set(cfg, project_root=project_root)
    if chosen is not None:
        bound = model_sets.bind(chosen, persona, alias, effort)
        kind = bound["runner"]
        providers = [bound["model"].split("/", 1)[0]] if kind == "pi" else []
    else:
        kind = runner.resolve_runner(cfg, campaign=state, project=state.get("project"))
        if kind == "pi":
            providers = [runner.map_pi_model(
                alias, effort, cfg, project_root=project_root)["provider"]]
        else:
            providers = []
    return kind, providers


def check(kind, environ=None, *, providers: list[str] | None = None) -> str | None:
    """The preflight error of backend `kind` in `environ` (default this process's), else
    None. `claude` and `fake` pass."""
    if kind == "cursor":
        try:
            rec = RP.probe_cursor_sdk(environ)
        except Exception:                    # noqa: BLE001 — a probe bug is not a fault
            return None
        return None if rec.get("ok") else (rec.get("error") or "cursor backend unavailable")
    if kind != "pi":
        return None
    return _check_pi(environ, providers)


def _pi_script() -> Path:
    return Path(__file__).resolve().parent / "pi_exec.mjs"


def _sdk_on_walk(script: Path, environ=None) -> bool:
    env = _environ(environ)
    starts: list[Path] = [script.parent]
    for entry in (env.get("NODE_PATH") or "").split(os.pathsep):
        if entry.strip():
            starts.append(Path(entry.strip()))
    for root in starts:
        here = root
        while True:
            if (here / _SDK_PKG / "package.json").is_file():
                return True
            parent = here.parent
            if parent == here:
                break
            here = parent
    return False


def _sdk_managed(environ) -> bool:
    env = _environ(environ)
    agent = env.get("PI_CODING_AGENT_DIR")
    if agent and str(agent).strip():
        agent_dir = Path(str(agent).strip())
    else:
        agent_dir = _home(environ) / ".pi" / "agent"
    version_file = agent_dir / "install" / "current-version"
    if not version_file.is_file():
        return False
    try:
        version = version_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    version = version.strip()
    if version.startswith("\ufeff"):
        version = version[1:].strip()
    if not _VERSION_RE.fullmatch(version):
        return False
    entry = (agent_dir / "install" / "releases" / version / _SDK_PKG / "dist" / "index.js")
    return entry.is_file()


def _auth_json(environ) -> dict | None:
    env = _environ(environ)
    agent = env.get("PI_CODING_AGENT_DIR")
    if agent and str(agent).strip():
        path = Path(str(agent).strip()) / "auth.json"
    else:
        path = _home(environ) / ".pi" / "agent" / "auth.json"
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _auth_provider(auth: dict | None, name: str) -> bool:
    if not auth:
        return False
    row = auth.get(name)
    return isinstance(row, dict) and bool(row)


def _has_credential(name: str, environ) -> bool:
    env = _environ(environ)
    auth = _auth_json(environ)
    n = str(name).strip().lower()
    if n == "anthropic":
        return (_nonempty(env, "ANTHROPIC_API_KEY") or _nonempty(env, "ANTHROPIC_AUTH_TOKEN")
                or _is_file(env, "ANTHROPIC_IDENTITY_TOKEN_FILE")
                or _auth_provider(auth, "anthropic"))
    if n == "openai":
        return _nonempty(env, "OPENAI_API_KEY") or _auth_provider(auth, "openai")
    if n == "google":
        adc = _home(environ) / ".config" / "gcloud" / "application_default_credentials.json"
        return (_nonempty(env, "GOOGLE_API_KEY") or _nonempty(env, "GEMINI_API_KEY")
                or _is_file(env, "GOOGLE_APPLICATION_CREDENTIALS") or adc.is_file()
                or _auth_provider(auth, "google"))
    if n == "google-vertex":
        adc = _home(environ) / ".config" / "gcloud" / "application_default_credentials.json"
        return (_nonempty(env, "GOOGLE_API_KEY")
                or _is_file(env, "GOOGLE_APPLICATION_CREDENTIALS") or adc.is_file()
                or _auth_provider(auth, "google-vertex"))
    if n in ("amazon-bedrock", "bedrock"):
        return (_nonempty(env, "AWS_ACCESS_KEY_ID")
                or _is_file(env, "AWS_WEB_IDENTITY_TOKEN_FILE")
                or _auth_provider(auth, n))
    return _auth_provider(auth, n)


def _check_pi(environ, providers: list[str] | None) -> str | None:
    if RP._resolve_binary("node", environ) is None:
        return "node not found on PATH"
    script = _pi_script()
    if not script.is_file():
        return f"pi_exec.mjs missing at {script}"
    if not (_sdk_on_walk(script, environ) or _sdk_managed(environ)):
        return f"{_PI_PKG} is not installed"
    names = [str(p).strip() for p in (providers or ()) if str(p).strip()]
    for name in names:
        if not _has_credential(name, environ):
            return f"no credential for provider {name}"
    return None


def env_fault(result) -> str | None:
    """The environment fault a finished, failed persona run reports, else None."""
    if result is None or not getattr(result, "is_error", False) \
            or getattr(result, "interrupted", False):
        return None
    m = _ENV_FAULT_RE.search(str(getattr(result, "result_text", "") or ""))
    return m.group(0) if m else None


def fault_kind(error: str) -> str:
    if "CURSOR_API_KEY is not set" in error or "cursor-sdk is not installed" in error:
        return "cursor"
    if any(s in error for s in (
            f"{_PI_PKG} is not installed",
            "no credential for provider ",
            "node not found on PATH",
            "pi_exec.mjs missing",
    )):
        return "pi"
    return "unknown"


def _question(kind: str, error: str, cfg) -> tuple[str, list, list]:
    q = (f"The {kind} persona backend cannot start on this host: {error}. Persona steps that "
         "need it are held (no triage or review retry is spent) until it is fixed.")
    if kind == "pi":
        opts = [
            "install Node and " + _SDK_SCOPE + " (managed install under ~/.pi/agent, or a "
            "node_modules walk that satisfies the check) and export the provider key or store "
            "it in Pi's auth.json",
            "switch agentic.runner (or the project's runner) to claude",
        ]
    else:
        opts = ["map the credential in agentic.credentials (e.g. CURSOR_API_KEY: "
                "pass:cursor/sdk), or install the missing SDK, then restart the runner: "
                "nw queue restart --yes",
                "switch agentic.runner (or the project's runner) to claude"]
    facts = [f"backend: {kind}", f"preflight: {error}"]
    return q, opts, facts


def note_fault(cfg, kind: str, error: str, *, where: str = "tick") -> dict | None:
    """Journal the fault once per process and file one escalation while none is open for
    `kind`. Returns the new escalation record, else None. Never raises."""
    key = (kind, error)
    with _NOTED_LOCK:
        first = key not in _NOTED
        _NOTED.add(key)
    try:
        from . import escalations as ESC
        from .queue import journal
        if first:
            journal(cfg, "backend_preflight_failed", backend=kind, error=error, where=where)
        from . import credentials as CR
        creds = CR.settings(cfg)
        if any(f"{var} is not set" in error for var in creds):
            return None             # SPEC-0483: the credential's own escalation names the fix
        if error.startswith("no credential for provider "):
            prov = error.split("no credential for provider ", 1)[1].strip().lower()
            env_keys = {
                "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE"),
                "openai": ("OPENAI_API_KEY",),
                "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"),
                "google-vertex": ("GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"),
                "amazon-bedrock": ("AWS_ACCESS_KEY_ID", "AWS_WEB_IDENTITY_TOKEN_FILE"),
                "bedrock": ("AWS_ACCESS_KEY_ID", "AWS_WEB_IDENTITY_TOKEN_FILE"),
            }.get(prov, ())
            if any(k in creds for k in env_keys):
                return None
        for rec in ESC.list_all(cfg, "open"):
            if rec.get("kind") == ESC_KIND and (rec.get("facts") or [""])[0] == f"backend: {kind}":
                return None
        q, opts, facts = _question(kind, error, cfg)
        plan = ESC.plan_escalate(cfg, None, question=q, options=opts, facts=facts,
                                 frm="conductor", kind=ESC_KIND, topic="human_settings")
        return ESC.apply_escalate(cfg, plan)
    except Exception:                        # noqa: BLE001 — a hold must never fail on this
        return None


def startup_check(cfg, *, err=None) -> str | None:
    """Runner start: warn loudly when a backend this host needs fails its preflight."""
    out = err or sys.stderr
    last = None
    try:
        from .runner import cursor_needed
        if cursor_needed(cfg):
            error = check("cursor")
            if error is not None:
                try:
                    out.write(f"WARNING: the cursor persona backend fails its preflight: "
                              f"{error}. Persona steps that need it are held until it is "
                              "fixed (see the escalation).\n")
                    out.flush()
                except (OSError, ValueError):
                    pass
                note_fault(cfg, "cursor", error, where="start")
                last = error
    except Exception:                        # noqa: BLE001
        pass
    try:
        from . import pi_map as PM
        from .runner import pi_needed
        if pi_needed(cfg):
            providers = PM.probe_providers(cfg)
            error = check("pi", providers=providers)
            if error is not None:
                try:
                    out.write(f"WARNING: the pi persona backend fails its preflight: {error}. "
                              "Persona steps that need it are held until it is fixed (see the "
                              "escalation).\n")
                    out.flush()
                except (OSError, ValueError):
                    pass
                note_fault(cfg, "pi", error, where="start")
                last = error
    except Exception:                        # noqa: BLE001
        pass
    return last


def reset_noted() -> None:
    """Tests: forget which faults this process journaled."""
    with _NOTED_LOCK:
        _NOTED.clear()
