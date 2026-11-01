"""`nw workers bootstrap` (SPEC-0354): install nightwright on a worker, install the
forced-command `authorized_keys` line for a dedicated worker key, and register the worker so
the coordinator connects with that key. Every step first checks whether it is needed.

Setup runs over the human's own ssh session (`-o ControlPath=none`, so these unrestricted
sessions never land in the coordinator's ControlMaster directory). With `--admin`, every
worker-side command runs through the admin account as `sudo -n -i -u DESTUSER`. The
coordinator later reaches the worker only with the dedicated key (the record's `identity`),
which the forced command binds to `nw worker ssh-command`."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import workers as WK
from .store import now_iso

STEPS = ("admin", "install", "verify", "key", "register")
DEFAULT_KEY = "~/.ssh/nw_worker_ed25519.pub"
BIN_RE = re.compile(r"/[A-Za-z0-9._/-]+")
USER_RE = re.compile(r"[a-z_][a-z0-9_.-]{0,31}")
PUBKEY_RE = re.compile(r"([A-Za-z0-9@.-]+) ([A-Za-z0-9+/]+={0,3})( [^\r\n]*)?")
DRY_BIN = "<uv tool bin dir>"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ControlPath=none"]
SCP = ["scp", "-q", "-o", "BatchMode=yes", "-o", "ControlPath=none"]
TIMEOUT_S = 120.0
INSTALL_TIMEOUT_S = 900.0
CONFLICT = "worker {name} is registered with different settings; nw workers rm {name} first"
NO_UV = 42                                      # `UV_PROBE`'s exit when uv is missing
UV_PROBE = f"command -v uv >/dev/null || exit {NO_UV}"
# with --admin the wheel dir is made by ADMIN; /tmp (not ADMIN's TMPDIR, which pam_tmpdir
# makes 0700) so DESTUSER can reach it
ADMIN_MKTEMP = "mktemp -d /tmp/nw-wheel.XXXXXXXX"


class StepFailed(Exception):
    pass


@dataclass
class Plan:
    name: str
    dest: str
    user: str
    slots: int
    install_key: bool
    admin: str | None
    pub_path: Path
    identity: str
    pubkey: str
    blob: str
    version: str
    bin: str | None = None
    status: dict = field(default_factory=dict)       # step -> (status, detail)
    notes: list = field(default_factory=list)        # lines printed after the table


def key_path(cfg) -> Path:
    """`agentic.workers.key`: the worker's public key (default ~/.ssh/nw_worker_ed25519.pub)."""
    raw = ((cfg.get("agentic") or {}).get("workers") or {})
    raw = raw if isinstance(raw, dict) else {}
    v = raw.get("key")
    return Path(os.path.abspath(os.path.expanduser(v if isinstance(v, str) and v
                                                   else DEFAULT_KEY)))


def forced_line(bin_dir: str, pubkey: str) -> str:
    """The SPEC-0336 forced-command line for `pubkey`."""
    return f'command="{bin_dir}/nw worker ssh-command",restrict {pubkey}'


def line_ok(line: str, pubkey: str) -> bool:
    """True when `line` is exactly the SPEC-0336 form: one line, `command="<bin>/nw worker
    ssh-command",restrict` and nothing else before the key, `<bin>` a safe absolute path."""
    m = re.fullmatch(r'command="(/[A-Za-z0-9._/-]+)/nw worker ssh-command",restrict (.+)', line)
    return bool(m) and "\n" not in line and "\r" not in line and m.group(2) == pubkey \
        and BIN_RE.fullmatch(m.group(1)) is not None


# ---- local pre-checks -----------------------------------------------------------------------

def _read_key(pub: Path) -> tuple[str, str]:
    """`(pubkey line, base64 blob)` of the worker public key; `StepFailed` when it or its
    private key is missing/unreadable, or it is not a single `<type> <blob> [comment]` line."""
    if pub.suffix != ".pub":
        raise StepFailed(f"agentic.workers.key must name a .pub file (got {pub})")
    priv = pub.with_suffix("")
    for p in (pub, priv):
        if not (p.is_file() and os.access(p, os.R_OK)):
            raise StepFailed(f"worker key {p} not found; create it with "
                             f"ssh-keygen -t ed25519 -N '' -f {priv}")
    lines = [ln for ln in pub.read_text(encoding="utf-8", errors="replace").splitlines()
             if ln.strip()]
    if len(lines) != 1:
        raise StepFailed(f"worker key {pub} must hold exactly one line (has {len(lines)})")
    m = PUBKEY_RE.fullmatch(lines[0].strip())
    if not m:
        raise StepFailed(f"worker key {pub} is not a `<type> <base64> [comment]` public key")
    return lines[0].strip(), m.group(2)


def _record_diff(rec: dict, want: dict, with_nw: bool) -> bool:
    """True when the registered `rec` differs from `want` in ssh, slots, identity (and nw)."""
    slots = rec.get("slots")
    slots = slots if isinstance(slots, int) and not isinstance(slots, bool) else WK.MIN_SLOTS
    if rec.get("ssh") != want["ssh"] or slots != want["slots"] \
            or rec.get("identity") != want["identity"]:
        return True
    return with_nw and (rec.get("nw") or "nw") != want["nw"]


def _existing(cfg, name):
    for r in WK.read_strict(cfg):
        if r.get("name") == name:
            return r
    return None


def prepare(cfg, name, dest, slots, install_key, admin) -> Plan:
    """The plan after every local pre-check; `StepFailed` (with `.step`) when one fails."""
    from .. import __version__

    def fail(step, msg):
        e = StepFailed(msg)
        e.step = step
        return e

    if not WK.NAME_RE.fullmatch(name) or name == "local":
        raise fail("register", f"NAME must match {WK.NAME_RE.pattern} and not be 'local'")
    if not WK.DEST_RE.fullmatch(dest) or dest.startswith("-") or dest.count("@") != 1:
        raise fail("install", "--ssh must be USER@HOST matching "
                              f"{WK.DEST_RE.pattern} and not start with '-'")
    user = dest.split("@", 1)[0]
    if not USER_RE.fullmatch(user):
        raise fail("install", f"worker user {user!r} must match {USER_RE.pattern}")
    if admin is not None and (not WK.DEST_RE.fullmatch(admin) or admin.startswith("-")):
        raise fail("admin", f"--admin must match {WK.DEST_RE.pattern} and not start with '-'")
    if user == "root":
        raise fail("key" if install_key else "install",
                   "refused: the worker account must not be root")
    pub = key_path(cfg)
    try:
        pubkey, blob = _read_key(pub)
    except StepFailed as e:
        raise fail("key", str(e))
    identity = str(pub.with_suffix(""))
    if not WK.identity_ok(identity):
        raise fail("register", f"worker private key path {identity} must match "
                               f"{WK.IDENTITY_RE.pattern}")
    try:
        rec = _existing(cfg, name)
    except (OSError, ValueError) as e:
        raise fail("register", f"{WK.path(cfg)} is unreadable: {e}")
    plan = Plan(name=name, dest=dest, user=user, slots=slots, install_key=install_key,
                admin=admin, pub_path=pub, identity=identity, pubkey=pubkey, blob=blob,
                version=__version__)
    if rec is not None and _record_diff(rec, _want(plan), with_nw=False):
        raise fail("register", CONFLICT.format(name=name))
    return plan


def _want(plan: Plan) -> dict:
    return {"ssh": plan.dest, "slots": plan.slots, "identity": plan.identity,
            "nw": f"{plan.bin}/nw" if plan.bin else None}


# ---- remote commands ------------------------------------------------------------------------

def worker_argv(plan: Plan) -> list[str]:
    """ssh argv starting `sh -s` on the worker as DESTUSER; the script goes on its stdin
    (`on_worker`). Never on the command line: `sudo -i` re-joins its command for the target's
    login shell and leaves `$` unescaped, so that shell would expand the script's `$...`."""
    if plan.admin:
        return SSH + [plan.admin, f"sudo -n -i -u {plan.user} sh -s"]
    return SSH + [plan.dest, "sh -s"]


def on_worker(plan: Plan, script: str, timeout=TIMEOUT_S) -> subprocess.CompletedProcess:
    """Run the one-line POSIX `script` on the worker as DESTUSER. One line, so `sh -s` has
    read all of it before any command runs, and nothing it starts can read the script."""
    if "\n" in script or "\r" in script:
        raise StepFailed("refused: a worker script must be a single line")
    return _run(worker_argv(plan), timeout=timeout, input=script + "\n")


def worker_shown(plan: Plan, script: str) -> str:
    """`on_worker(plan, script)` as an equivalent shell command line (for --dry-run)."""
    return f"printf '%s\\n' {shlex.quote(script)} | {shlex.join(worker_argv(plan))}"


def admin_argv(plan: Plan, cmd: str) -> list[str]:
    return SSH + [plan.admin, cmd]


def _run(argv, timeout=TIMEOUT_S, cwd=None, input=None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, errors="replace",
                              timeout=timeout, cwd=cwd,
                              **({"stdin": subprocess.DEVNULL} if input is None
                                 else {"input": input}))
    except subprocess.TimeoutExpired:
        raise StepFailed(f"`{argv[0]}` timed out after {timeout:g}s")
    except OSError as e:
        raise StepFailed(f"`{argv[0]}` failed to start: {e}")


def _tail(p) -> str:
    return ((p.stderr or "").strip().splitlines()[-1:] or [""])[0]


def _must(p, what) -> str:
    if p.returncode != 0:
        raise StepFailed(f"{what} failed (exit {p.returncode}): {_tail(p)}".rstrip(": "))
    return p.stdout


def _remote_info(plan: Plan) -> dict | None:
    """`<bin>/nw worker info --json` on the worker; `None` when it fails or is unreadable."""
    p = on_worker(plan, f"{plan.bin}/nw worker info --json")
    if p.returncode != 0:
        return None
    try:
        info = json.loads(p.stdout)
    except ValueError:
        return None
    return info if isinstance(info, dict) else None


def _checkout_root() -> Path:
    root = Path(__file__).resolve().parents[3]
    if not (root / "pyproject.toml").is_file():
        raise StepFailed(f"no checkout to build a wheel from at {root}")
    return root


# ---- steps ----------------------------------------------------------------------------------

def step_admin(plan: Plan) -> tuple[str, str]:
    if _run(admin_argv(plan, "sudo -n true")).returncode != 0:
        raise StepFailed("sudo on ADMIN needs a password; configure NOPASSWD for useradd "
                         "or create the user yourself")
    if _run(admin_argv(plan, f"id -u {plan.user}")).returncode == 0:
        return "skipped (already exists)", plan.user
    _must(_run(admin_argv(plan, f"sudo -n useradd -m {plan.user}")), "useradd")
    return "done", f"created {plan.user}"


def _probe_bin(plan: Plan) -> None:
    p = on_worker(plan, UV_PROBE)
    if p.returncode == NO_UV:
        raise StepFailed(f"uv not found for {plan.user} on the worker; install uv system-wide")
    if p.returncode != 0:                       # ssh (255) or sudo -u refusing: not uv's fault
        raise _unreachable(plan, p)
    out = _must(on_worker(plan, "uv tool dir --bin"), "uv tool dir --bin").strip()
    if not BIN_RE.fullmatch(out):
        raise StepFailed(f"unexpected uv tool bin dir {out!r}")
    plan.bin = out


def _unreachable(plan: Plan, p) -> StepFailed:
    via = f" through {plan.admin}" if plan.admin else ""
    return StepFailed(f"could not run a command as {plan.user} on the worker{via} "
                      f"(exit {p.returncode}): {_tail(p)}".rstrip(": "))


def _probe_root(plan: Plan) -> None:
    """`StepFailed` with `.step`: `install` when no command ran as the worker user at all
    (ssh, sudo -u), `key` for the uid-0 refusal."""
    p = on_worker(plan, "id -u")
    if p.returncode != 0:
        e = _unreachable(plan, p)
        e.step = "install"
        raise e
    if p.stdout.strip() == "0":
        e = StepFailed("refused: the worker account has uid 0")
        e.step = "key"
        raise e


def step_install(plan: Plan) -> tuple[str, str]:
    _probe_bin(plan)
    info = _remote_info(plan)
    if info is not None and info.get("version") == plan.version:
        return f"skipped (already {plan.version})", f"{plan.bin}/nw"
    with tempfile.TemporaryDirectory(prefix="nw-wheel-") as out:
        _must(_run(["uv", "build", "--wheel", "--out-dir", out, str(_checkout_root())],
                   timeout=INSTALL_TIMEOUT_S), "uv build --wheel")
        wheels = sorted(Path(out).glob("*.whl"))
        if len(wheels) != 1:
            raise StepFailed(f"uv build made {len(wheels)} wheels, expected 1")
        wheel = wheels[0]
        # with --admin the wheel goes to a dir in ADMIN's /tmp that DESTUSER can read
        host = plan.admin or plan.dest
        on_host = (lambda c: _run(admin_argv(plan, c))) if plan.admin else \
            (lambda c: on_worker(plan, c))
        mktemp = ADMIN_MKTEMP if plan.admin else "mktemp -d"
        tmp = _must(on_host(mktemp), "mktemp -d").strip()
        if not BIN_RE.fullmatch(tmp):
            raise StepFailed(f"unexpected remote temp dir {tmp!r}")
        remote = shlex.quote(f"{tmp}/{wheel.name}")
        try:
            if plan.admin:
                _must(on_host(f"chmod 755 {tmp}"), "chmod")
            _must(_run(SCP + [str(wheel), f"{host}:{tmp}/"]), "scp")
            if plan.admin:
                _must(on_host(f"chmod 644 {remote}"), "chmod")
            _must(on_worker(plan, f"uv tool install --force {remote}",
                            timeout=INSTALL_TIMEOUT_S), "uv tool install")
        finally:
            _cleanup(plan, on_host, host, tmp)
    return "done", f"installed {wheel.name} -> {plan.bin}/nw"


def _cleanup(plan: Plan, on_host, host: str, tmp: str) -> None:
    """Remove the remote wheel dir. A failure here is a warning: it must neither turn a
    finished install into a failure nor replace the real cause of an earlier one."""
    try:
        p = on_host(f"rm -rf {tmp}")
        err = None if p.returncode == 0 else f"exit {p.returncode}: {_tail(p)}".rstrip(": ")
    except StepFailed as e:
        err = str(e)
    if err:
        plan.notes.append(f"warning: could not remove {host}:{tmp} ({err})")


def step_verify(plan: Plan) -> tuple[str, str]:
    p = on_worker(plan, f"{plan.bin}/nw worker info --json")
    _must(p, f"{plan.bin}/nw worker info --json")
    try:
        info = json.loads(p.stdout)
    except ValueError as e:
        raise StepFailed(f"`nw worker info --json` gave unreadable output: {e}")
    got = info.get("version") if isinstance(info, dict) else None
    if got != plan.version:
        raise StepFailed(f"worker reports {got}, expected {plan.version}")
    return "done", f"{plan.bin}/nw reports {got}"


def _key_script(line: str) -> str:
    q = shlex.quote(line)
    return ("umask 077 && mkdir -p ~/.ssh && chmod 700 ~/.ssh && f=~/.ssh/authorized_keys && "
            'touch "$f" && if grep -qxF -- ' + q + ' "$f"; then echo present; else '
            'if [ -s "$f" ] && [ -n "$(tail -c1 "$f")" ]; then echo >> "$f"; fi && '
            "printf '%s\\n' " + q + ' >> "$f" && echo added; fi && chmod 600 "$f"')


READ_KEYS = 'f=~/.ssh/authorized_keys; if [ -e "$f" ]; then cat "$f"; fi'


def step_key(plan: Plan) -> tuple[str, str]:
    line = forced_line(plan.bin, plan.pubkey)
    if not plan.install_key:
        plan.notes.append(f"authorized_keys line for {plan.user} on the worker:")
        plan.notes.append(line)
        return "not requested", "line printed below"
    if not line_ok(line, plan.pubkey):
        raise StepFailed(f"refused: the key line is not exactly the "
                         f'command="...",restrict <pubkey> form')
    current = _must(on_worker(plan, READ_KEYS), "reading authorized_keys")
    present = False
    for ln in current.splitlines():
        if ln.strip() == line:
            present = True
        elif plan.blob in ln.split():
            raise StepFailed("key already authorized without the forced command; "
                             "use a dedicated worker key")
    if present:
        return "skipped (already present)", "~/.ssh/authorized_keys"
    out = _must(on_worker(plan, _key_script(line)), "installing the key line")
    if out.strip() == "present":
        return "skipped (already present)", "~/.ssh/authorized_keys"
    return "done", "appended the forced-command line"


def step_register(cfg, plan: Plan) -> tuple[str, str]:
    want = _want(plan)
    try:
        rec = _existing(cfg, plan.name)
    except (OSError, ValueError) as e:
        raise StepFailed(f"{WK.path(cfg)} is unreadable: {e}")
    if rec is not None:
        if _record_diff(rec, want, with_nw=True):
            raise StepFailed(CONFLICT.format(name=plan.name))
        if not plan.install_key:
            plan.notes.append(f"run nw workers check {plan.name} after installing the key line")
            return "skipped (already registered)", plan.name
        # it may have been registered with --no-check: the forced-command proof is due now
        _register_check(cfg, plan, rec)
        return "skipped (already registered)", f"{plan.name}, checked"
    new = {"name": plan.name, "ssh": plan.dest, "slots": plan.slots,
           "kinds": list(WK.DEFAULT_KINDS), "nw": want["nw"], "enabled": True,
           "cooldown_until": None, "added_at": now_iso(), "identity": plan.identity}
    if plan.install_key:
        _register_check(cfg, plan, new)
    else:
        plan.notes.append(f"run nw workers check {plan.name} after installing the key line")

    def add(records):
        if any(r.get("name") == plan.name for r in records):
            raise StepFailed(CONFLICT.format(name=plan.name))
        records.append(new)

    try:
        WK.update(cfg, add)
    except (OSError, ValueError) as e:
        raise StepFailed(str(e))
    return "done", ("checked and registered" if plan.install_key
                    else "registered with --no-check")


def _register_check(cfg, plan: Plan, rec: dict) -> None:
    res = WK.check(cfg, rec)
    if not res["ok"]:
        raise StepFailed(f"{plan.name} failed its check: " + "; ".join(res["errors"]))
    plan.notes.extend(f"warning: {w}" for w in res["warnings"])


def run(cfg, plan: Plan) -> bool:
    """Run every step in order, filling `plan.status`; False when one failed (every later
    step is then `not run`). With `--install-key` the uid-0 refusal of the key step is probed
    before install, since it is a read and install already writes to the worker."""
    steps = {"admin": lambda: step_admin(plan), "install": lambda: step_install(plan),
             "verify": lambda: step_verify(plan), "key": lambda: step_key(plan),
             "register": lambda: step_register(cfg, plan)}

    def failed(step, msg):
        for s in STEPS:
            plan.status.setdefault(s, ("not run", ""))
        plan.status[step] = ("failed", msg)
        return False

    for step in STEPS:
        if step == "admin" and not plan.admin:
            plan.status[step] = ("not requested", "")
            continue
        if step == "install" and plan.install_key:
            try:
                _probe_root(plan)
            except StepFailed as e:
                return failed(e.step, str(e))
        try:
            plan.status[step] = steps[step]()
        except StepFailed as e:
            return failed(step, str(e))
    return True


def dry_plan(plan: Plan) -> dict[str, list[str]]:
    """step -> the exact commands it would run, with `<uv tool bin dir>` for `<bin>`."""
    plan.bin = DRY_BIN
    j = shlex.join
    tmp, whl = "<remote tmp dir>", "<wheel>"
    out = {s: [] for s in STEPS}
    if plan.admin:
        out["admin"] = [j(admin_argv(plan, c)) for c in
                        ("sudo -n true", f"id -u {plan.user}",
                         f"sudo -n useradd -m {plan.user}  # only when missing")]
    host = plan.admin or plan.dest
    on_host = (lambda c: j(admin_argv(plan, c))) if plan.admin else \
        (lambda c: worker_shown(plan, c))
    out["install"] = [worker_shown(plan, UV_PROBE),
                      worker_shown(plan, "uv tool dir --bin"),
                      worker_shown(plan, f"{DRY_BIN}/nw worker info --json"),
                      "# only when nw is missing or another version:",
                      j(["uv", "build", "--wheel", "--out-dir", "<local tmp dir>",
                         str(Path(__file__).resolve().parents[3])]),
                      on_host(ADMIN_MKTEMP if plan.admin else "mktemp -d")]
    if plan.admin:
        out["install"].append(on_host(f"chmod 755 {tmp}"))
    out["install"].append(j(SCP + [whl, f"{host}:{tmp}/"]))
    if plan.admin:
        out["install"].append(on_host(f"chmod 644 {tmp}/{whl}"))
    out["install"] += [worker_shown(plan, f"uv tool install --force {tmp}/{whl}"),
                       on_host(f"rm -rf {tmp}")]
    out["verify"] = [worker_shown(plan, f"{DRY_BIN}/nw worker info --json")]
    line = forced_line(DRY_BIN, plan.pubkey)
    if plan.install_key:
        out["key"] = [worker_shown(plan, "id -u"), worker_shown(plan, READ_KEYS),
                      worker_shown(plan, _key_script(line))]
    else:
        out["key"] = [f"# print: {line}"]
    add = ["nw", "workers", "add", plan.name, "--ssh", plan.dest, "--nw", f"{DRY_BIN}/nw",
           "--identity", plan.identity, "--slots", str(plan.slots)]
    out["register"] = [j(add + ([] if plan.install_key else ["--no-check"]))]
    for s in STEPS:
        plan.status[s] = ("would run", "") if out[s] else ("not requested", "")
    if not plan.install_key:
        plan.status["key"] = ("not requested", "line printed")
    return out
