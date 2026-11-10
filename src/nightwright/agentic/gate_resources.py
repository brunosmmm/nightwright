"""Per-gate resource requests and the job request derived from them (SPEC-0504).

A gate may declare `resources: {cpus, mem, licenses}` in `.nw/project.yaml`; what it leaves out
comes from the per-kind defaults below (overridable per host by
`agentic.gates.resource_defaults`). A worker job runs its gates one after another, so its
request is the max of their cpus and memory, the union of their licenses and the job's existing
timeout. Requests are recorded in every gate record next to what the gate used (`gates.py`),
and are what a Slurm executor asks for (SPEC-0510)."""
from __future__ import annotations

import re

MEM_RE = re.compile(r"^\d+(\.\d+)?[MG]\Z")
LICENSE_RE = re.compile(r"^[a-z][a-z0-9_]*(:\d+)?\Z")
RESOURCE_KEYS = ("cpus", "mem", "licenses")
# the gate key a resolved request travels under inside the gate runner (never in config)
REQUEST_KEY = "_request"

# SPEC-0501's low-load estimates; re-derived from measured usage later (SPEC-0514)
DEFAULTS = {
    "pytest-full": {"cpus": 5, "mem": "5G"},    # `results.format: pytest`, gate named `pytest`
    "pytest": {"cpus": 3, "mem": "3G"},         # any other `results.format: pytest` gate
    "default": {"cpus": 1, "mem": "1G"},        # anything else
    "persona": {"cpus": 1, "mem": "2G"},        # a persona job (`kind: persona`)
}


def mem_mb(text: str) -> int:
    """`3G` -> 3072, `512M` -> 512, `1.5G` -> 1536 (rounded up to a whole MB)."""
    n, unit = float(text[:-1]), text[-1]
    mb = n * 1024 if unit == "G" else n
    return int(-(-mb // 1))


def parse_licenses(items) -> dict[str, int]:
    """`["jerome_target", "x:2"]` -> `{"jerome_target": 1, "x": 2}`."""
    out: dict[str, int] = {}
    for item in items or []:
        name, _, count = str(item).partition(":")
        out[name] = max(out.get(name, 0), int(count) if count else 1)
    return out


def check(problems: list, res, path: str) -> None:
    """Config validation of one gate's `resources` (appends to `problems`)."""
    if not isinstance(res, dict):
        problems.append(f"{path}: must be a mapping with cpus, mem, licenses")
        return
    for key in sorted(set(res) - set(RESOURCE_KEYS)):
        problems.append(f"{path}.{key}: unknown key (allowed: {', '.join(RESOURCE_KEYS)})")
    if "cpus" in res:
        c = res["cpus"]
        if isinstance(c, bool) or not isinstance(c, int) or c < 1:
            problems.append(f"{path}.cpus: must be an integer >= 1")
    if "mem" in res:
        m = res["mem"]
        if not isinstance(m, str) or not MEM_RE.match(m) or mem_mb(m) < 1:
            problems.append(f"{path}.mem: must be a size like 3G or 512M")
    if "licenses" in res:
        lic = res["licenses"]
        if not isinstance(lic, list) or not all(isinstance(x, str) and LICENSE_RE.match(x)
                                                for x in lic):
            problems.append(f"{path}.licenses: must be a list of names like jerome_target "
                            "or jerome_target:2")


def _defaults(cfg) -> dict:
    """`DEFAULTS` with this host's `agentic.gates.resource_defaults` over it (a malformed
    entry is ignored: the built-in one stands)."""
    out = {k: dict(v) for k, v in DEFAULTS.items()}
    raw = (((cfg or {}).get("agentic") or {}).get("gates") or {}).get("resource_defaults")
    if isinstance(raw, dict):
        for kind, over in raw.items():
            if kind in out and isinstance(over, dict):
                probs: list = []
                check(probs, {k: v for k, v in over.items() if k in ("cpus", "mem")}, kind)
                if not probs:
                    out[kind].update({k: v for k, v in over.items() if k in ("cpus", "mem")})
    return out


def kind_of(gate: dict) -> str:
    results = gate.get("results") if isinstance(gate.get("results"), dict) else {}
    if results.get("format") == "pytest":
        return "pytest-full" if gate.get("name") == "pytest" else "pytest"
    return "default"


def gate_request(gate: dict, cfg=None) -> dict:
    """One gate's request `{cpus, mem_mb, licenses}`: a resolved one it carries
    (`REQUEST_KEY`, set by the coordinator or the worker), else its `resources` over the
    defaults for its kind."""
    pre = gate.get(REQUEST_KEY)
    if isinstance(pre, dict):
        return {"cpus": pre.get("cpus"), "mem_mb": pre.get("mem_mb"),
                "licenses": dict(pre.get("licenses") or {})}
    base = _defaults(cfg)[kind_of(gate)]
    res = gate.get("resources") if isinstance(gate.get("resources"), dict) else {}
    return {"cpus": int(res.get("cpus") or base["cpus"]),
            "mem_mb": mem_mb(res.get("mem") or base["mem"]),
            "licenses": parse_licenses(res.get("licenses"))}


def persona_request(cfg=None) -> dict:
    d = _defaults(cfg)["persona"]
    return {"cpus": int(d["cpus"]), "mem_mb": mem_mb(d["mem"]), "licenses": {}}


def job_request(gates: list, cfg=None, time_s: int | None = None) -> dict:
    """The request of one worker job that runs `gates` in sequence: max cpus and memory, the
    union of licenses (max count per name), `time_s` (given, else the coordinator's own
    deadline for such a job: `remote_gates.job_timeout_s` + `RUN_TIMEOUT_MARGIN_S`), and each
    gate's own request under `gates`."""
    per = {g["name"]: gate_request(g, cfg) for g in gates if isinstance(g, dict) and g.get("name")}
    lic: dict[str, int] = {}
    for r in per.values():
        for name, n in r["licenses"].items():
            lic[name] = max(lic.get(name, 0), n)
    if time_s is None:
        from . import remote_gates as RG        # lazy: remote_gates imports gates
        time_s = int(RG.job_timeout_s(cfg or {}, gates) + RG.RUN_TIMEOUT_MARGIN_S)
    empty = _defaults(cfg)["default"]
    return {"cpus": max((r["cpus"] for r in per.values()), default=int(empty["cpus"])),
            "mem_mb": max((r["mem_mb"] for r in per.values()), default=mem_mb(empty["mem"])),
            "licenses": lic, "time_s": int(time_s), "gates": per}


def check_payload(res) -> str | None:
    """Why a header's `payload.resources` is unusable (the worker's bounds check), else None."""
    if not isinstance(res, dict):
        return "payload.resources must be an object"

    def bad_req(r) -> bool:
        if not isinstance(r, dict):
            return True
        c, m, lic = r.get("cpus"), r.get("mem_mb"), r.get("licenses", {})
        return (isinstance(c, bool) or not isinstance(c, int) or not 1 <= c <= 4096
                or isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= 16 * 2**20
                or not isinstance(lic, dict)
                or not all(isinstance(k, str) and LICENSE_RE.match(k) and isinstance(v, int)
                           and not isinstance(v, bool) and 1 <= v <= 1024
                           for k, v in lic.items()))
    if bad_req(res):
        return "payload.resources needs cpus (1..4096), mem_mb (1..16Ti) and licenses {name: n}"
    t = res.get("time_s")
    if t is not None and (isinstance(t, bool) or not isinstance(t, int) or t <= 0):
        return "payload.resources.time_s must be a positive integer"
    per = res.get("gates", {})
    if not isinstance(per, dict) or any(bad_req(r) for r in per.values()):
        return "payload.resources.gates must map gate names to requests"
    return None
