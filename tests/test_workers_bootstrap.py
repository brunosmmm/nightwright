"""SPEC-0354: `nw workers bootstrap`. Offline: loopback stand-ins for `ssh`/`scp` (they drop
the destination and run the command on this host against a temp HOME), a fake `uv` (`build`,
`tool dir --bin`, `tool install` writing a fake `nw` that reports a set version), and fake
`sudo`/`id`. With a dedicated key (`-i`, `IdentitiesOnly=yes`) the fake `ssh` acts as sshd:
the first `authorized_keys` line holding the key's blob decides, and a `command="..."` line
runs that forced command with `SSH_ORIGINAL_COMMAND` (the fake `nw worker ssh-command` allows
only `nw worker info --json`). Every stand-in records its argv (and `ssh` its stdin, the worker script) in
`<state>/log.jsonl`. The fake `sudo -i` re-joins its command the way real sudo does
(backslash before every char but alnum, `_`, `-`, `$`) and runs it with `sh -c`."""
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright import __version__
from nightwright.agentic import workers as WK
from nightwright.agentic import workers_bootstrap as WB
from nightwright.cli import cli

PY = sys.executable

# Each fake reads its state dir from $FAKE_STATE and appends {"prog", "argv"} to log.jsonl.
COMMON = f"""#!{PY}
import json, os, subprocess, sys
S = os.environ["FAKE_STATE"]
PROG = os.path.basename(sys.argv[0])
STDIN = sys.stdin.read() if PROG == "ssh" else None
def log():
    with open(os.path.join(S, "log.jsonl"), "a") as f:
        f.write(json.dumps({{"prog": PROG, "argv": sys.argv[1:], "stdin": STDIN}}) + "\\n")
def read(name, default=None):
    p = os.path.join(S, name)
    return open(p).read().strip() if os.path.exists(p) else default
log()
"""

FAKE_SSH = COMMON + """
args = sys.argv[1:]
while args and args[0].startswith("-"):
    a = args.pop(0)
    if a in ("-o", "-i", "-p", "-l"):
        args.pop(0)
if "-O" in sys.argv:                          # a ControlMaster command: no master here
    sys.exit(0)
dest, cmd = args[0], " ".join(args[1:])
if read("unreachable"):
    print("Host key verification failed.", file=sys.stderr)
    sys.exit(255)
env = {**os.environ, "HOME": os.path.join(S, "home"),
       "PATH": (os.path.join(S, "nouvbin") if read("no_uv")
                else os.path.join(S, "fakebin") + ":" + os.environ["PATH"])}
if "IdentitiesOnly=yes" in sys.argv:          # sshd deciding on the dedicated key
    if read("check_fail"):
        sys.exit(255)
    blob = open(sys.argv[sys.argv.index("-i") + 1] + ".pub").read().split()[1]
    ak = os.path.join(S, "home", ".ssh", "authorized_keys")
    lines = open(ak).read().splitlines() if os.path.exists(ak) else []
    hit = next((ln for ln in lines if blob in ln.split()), None)
    if hit is None:
        print("Permission denied (publickey).", file=sys.stderr)
        sys.exit(255)
    if hit.startswith('command="'):
        env["SSH_ORIGINAL_COMMAND"] = cmd
        cmd = hit[len('command="'):].split('"', 1)[0]
sys.exit(subprocess.run(["sh", "-c", cmd], env=env, input=STDIN, text=True).returncode)
"""

FAKE_SCP = COMMON + """
import shutil
if read("scp_fail"):
    print("scp: lost connection", file=sys.stderr)
    sys.exit(1)
args = [a for a in sys.argv[1:]]
out = []
while args:
    a = args.pop(0)
    if a == "-o":
        args.pop(0)
    elif not a.startswith("-"):
        out.append(a)
*srcs, target = out
path = target.split(":", 1)[1]
for s in srcs:
    shutil.copy(s, path)
"""

FAKE_NW = """#!{py}
import json, os, shlex, sys
def info():
    if {version!r} == "exit":
        print("nw: broken install", file=sys.stderr)
        sys.exit(3)
    if {version!r} == "garbage":
        print("not json")
        sys.exit(0)
    nogit = os.path.exists(os.path.join(os.environ.get("FAKE_STATE", "/nonexistent"), "nogit"))
    print(json.dumps({{"schema": "nw.worker.info.v1", "protocol": "nw.job.v1",
                      "version": {version!r}, "git": None if nogit else "git version 2.0",
                      "git_sha": None}}))
    sys.exit(0)
if sys.argv[1:3] == ["worker", "info"]:
    info()
if sys.argv[1:3] == ["worker", "ssh-command"]:
    w = shlex.split(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    if len(w) == 4 and w[0].endswith("nw") and w[1:] == ["worker", "info", "--json"]:
        info()
    print("refused: only `nw worker <...>` is allowed", file=sys.stderr)
    sys.exit(2)
sys.exit(2)
"""

FAKE_UV = COMMON + f"""
a = sys.argv[1:]
if a[:2] == ["build", "--wheel"]:
    out = a[a.index("--out-dir") + 1]
    open(os.path.join(out, "nightwright-{__version__}-py3-none-any.whl"), "w").write("whl")
elif a[:3] == ["tool", "dir", "--bin"]:
    print(read("bin"))
elif a[:3] == ["tool", "install", "--force"]:
    assert os.path.isfile(a[3]), a[3]
    b = read("bin")
    os.makedirs(b, exist_ok=True)
    p = os.path.join(b, "nw")
    open(p, "w").write({FAKE_NW!r}.format(py={PY!r}, version=read("install_version", {__version__!r})))
    os.chmod(p, 0o755)
else:
    sys.exit(2)
"""

FAKE_SUDO = COMMON + """
a = sys.argv[1:]
user, login = None, False
while a and a[0].startswith("-"):
    o = a.pop(0)
    if o == "-u":
        user = a.pop(0)
    elif o == "-i":
        login = True
if user and read("runas_fail"):
    print(f"Sorry, user ops is not allowed to execute 'sh -s' as {user} on box.", file=sys.stderr)
    sys.exit(1)
if login:            # sudo -i: one string for the login shell's -c, as sudo's parse_args.c
    cmd = " ".join("".join(c if (c.isascii() and c.isalnum()) or c in "_-$" else "\\\\" + c
                           for c in arg) for arg in a)
    sys.exit(subprocess.run(["sh", "-c", cmd]).returncode)
if a == ["true"]:
    sys.exit(1 if read("sudo_fail") else 0)
if a[:1] == ["useradd"] and read("useradd_fail"):
    print("useradd: cannot lock /etc/passwd; try again later.", file=sys.stderr)
    sys.exit(1)
if a[:1] == ["useradd"]:
    with open(os.path.join(S, "users"), "a") as f:
        f.write(a[-1] + "\\n")
    sys.exit(0)
sys.exit(subprocess.run(a).returncode)
"""

FAKE_ID = COMMON + """
a = sys.argv[1:]
if a == ["-u"]:
    print(read("uid", str(os.getuid())))
    sys.exit(0)
users = (read("users", "") or "").split()
sys.exit(0 if a[:1] == ["-u"] and a[1] in users else 1)
"""


@pytest.fixture(autouse=True)
def _short_runtime_dir(monkeypatch):
    import tempfile
    d = tempfile.mkdtemp(prefix="nwr")
    monkeypatch.setenv("XDG_RUNTIME_DIR", d)
    yield
    shutil.rmtree(d, ignore_errors=True)


class World:
    def __init__(self, root: Path):
        self.state = root / "state"
        self.fakebin = self.state / "fakebin"
        self.home = self.state / "home"
        self.bin = root / "wbin"
        self.fakebin.mkdir(parents=True)
        self.home.mkdir()
        for name, body in (("ssh", FAKE_SSH), ("scp", FAKE_SCP), ("uv", FAKE_UV),
                           ("sudo", FAKE_SUDO), ("id", FAKE_ID)):
            p = self.fakebin / name
            p.write_text(body)
            p.chmod(0o755)
        nouv = self.state / "nouvbin"                # the worker PATH when uv is missing
        nouv.mkdir()
        for tool in ("sh", "cat", "id", "sudo"):
            target = self.fakebin / tool if tool in ("id", "sudo") else shutil.which(tool)
            (nouv / tool).symlink_to(target)
        self.set("bin", str(self.bin))
        keys = root / "keys"
        keys.mkdir()
        self.priv = keys / "nw_worker_ed25519"
        self.pub = keys / "nw_worker_ed25519.pub"
        if shutil.which("ssh-keygen"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test@coord",
                            "-f", str(self.priv)], check=True, stdin=subprocess.DEVNULL)
        else:
            self.priv.write_text("private\n")
            self.pub.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeBlobForTests0 test@coord\n")
        self.pubkey = self.pub.read_text().strip()
        self.blob = self.pubkey.split()[1]
        self.cfg = {"data_dir": str(root / "data"), "agentic": {"workers": {"key": str(self.pub)}}}

    def set(self, name, value):
        (self.state / name).write_text(value)

    def calls(self, prog=None):
        p = self.state / "log.jsonl"
        out = [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []
        return [c for c in out if prog is None or c["prog"] == prog]

    def clear(self):
        (self.state / "log.jsonl").unlink(missing_ok=True)

    @property
    def ak(self) -> Path:
        return self.home / ".ssh" / "authorized_keys"

    def seed_nw(self, version):
        self.bin.mkdir(exist_ok=True)
        p = self.bin / "nw"
        p.write_text(FAKE_NW.format(py=PY, version=version))
        p.chmod(0o755)

    def set_comment(self, comment):
        self.pubkey = f"{self.pubkey.split()[0]} {self.blob} {comment}"
        self.pub.write_text(self.pubkey + "\n")

    def line(self):
        return f'command="{self.bin}/nw worker ssh-command",restrict {self.pubkey}'


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path)
    monkeypatch.setenv("FAKE_STATE", str(w.state))
    monkeypatch.setenv("PATH", f"{w.fakebin}:{os.environ['PATH']}")
    monkeypatch.setenv("COLUMNS", "300")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: w.cfg)
    return w


def boot(world, *args):
    return CliRunner().invoke(cli, ["workers", "bootstrap", *args])


def plan_run(world, name="box", dest="nw@box", slots=1, install_key=True, admin=None):
    plan = WB.prepare(world.cfg, name, dest, slots, install_key, admin)
    ok = WB.run(world.cfg, plan)
    return ok, plan


def st(plan):
    return {s: v[0] for s, v in plan.status.items()}


def ssh_cmds(world):
    """Each ssh call's remote command, followed by the script it got on stdin (if any)."""
    return [(c["argv"][-1] + " " + (c["stdin"] or "")).strip() for c in world.calls("ssh")]


# ---- tests ----------------------------------------------------------------------------------

def test_fresh_run(world):
    res = boot(world, "box", "--ssh", "nw@box", "--install-key", "--slots", "2")
    assert res.exit_code == 0, res.output
    for step in WB.STEPS:
        assert step in res.output
    cmds = ssh_cmds(world)
    assert any("command -v uv" in c for c in cmds)
    assert any("uv tool dir --bin" in c for c in cmds)
    assert [c["argv"][:2] for c in world.calls("uv") if c["argv"][0] == "build"] \
        == [["build", "--wheel"]]
    assert any(c["argv"][:3] == ["tool", "install", "--force"] for c in world.calls("uv"))
    assert len(world.calls("scp")) == 1
    (rec,) = WK.read_strict(world.cfg)
    assert rec["nw"] == f"{world.bin}/nw" and rec["identity"] == str(world.priv)
    assert rec["slots"] == 2 and rec["ssh"] == "nw@box"
    check = [c for c in world.calls("ssh") if "IdentitiesOnly=yes" in c["argv"]]
    assert check and "-i" in check[0]["argv"] and str(world.priv) in check[0]["argv"]
    ok, plan = plan_run(world, slots=2)                   # second run: everything skipped
    assert ok and st(plan) == {"admin": "not requested",
                               "install": f"skipped (already {__version__})", "verify": "done",
                               "key": "skipped (already present)",
                               "register": "skipped (already registered)"}


def test_rerun_skips(world):
    ok, plan = plan_run(world)
    assert ok and st(plan)["install"] == "done" and st(plan)["key"] == "done"
    world.clear()
    ok, plan = plan_run(world)
    assert ok
    assert st(plan)["install"].startswith("skipped") and st(plan)["key"].startswith("skipped")
    assert st(plan)["register"] == "skipped (already registered)"
    assert not world.calls("scp") and not [c for c in world.calls("uv")
                                           if c["argv"][:2] == ["tool", "install"]]
    assert world.ak.read_text().splitlines() == [world.line()]
    assert stat.S_IMODE(world.ak.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(world.ak.stat().st_mode) == 0o600
    assert len(WK.read_strict(world.cfg)) == 1


def test_version_mismatch_reinstalls(world):
    world.seed_nw("0.0.1")
    ok, plan = plan_run(world)
    assert ok and st(plan)["install"] == "done"
    assert any(c["argv"][:3] == ["tool", "install", "--force"] for c in world.calls("uv"))
    out = subprocess.run([str(world.bin / "nw"), "worker", "info", "--json"],
                         capture_output=True, text=True).stdout
    assert json.loads(out)["version"] == __version__


def test_verify_fails_after_install(world):
    world.set("install_version", "9.9.9")
    res = boot(world, "box", "--ssh", "nw@box", "--install-key")
    assert res.exit_code == 1
    assert f"worker reports 9.9.9, expected {__version__}" in res.output
    ok, plan = plan_run(world)
    assert not ok and st(plan) == {"admin": "not requested", "install": "done",
                                   "verify": "failed", "key": "not run", "register": "not run"}
    assert not world.ak.exists() and WK.read_strict(world.cfg) == []


def test_key_line_form(world):
    world.ak.parent.mkdir()
    world.ak.write_text("ssh-ed25519 AAAAOTHERKEY human@laptop")      # no trailing newline
    ok, plan = plan_run(world)
    assert ok and st(plan)["key"] == "done"
    assert world.ak.read_text().splitlines() == ["ssh-ed25519 AAAAOTHERKEY human@laptop",
                                                 world.line()]
    assert world.line() == WB.forced_line(str(world.bin), world.pubkey)
    assert world.line().startswith(f'command="{world.bin}/nw worker ssh-command",restrict ')
    before = world.ak.read_bytes()
    WK.update(world.cfg, lambda recs: recs.clear())
    ok, plan = plan_run(world)
    assert ok and st(plan)["key"] == "skipped (already present)"
    assert world.ak.read_bytes() == before
    pk = world.pubkey
    assert WB.line_ok(WB.forced_line("/a/b", pk), pk)
    for bad in (f'restrict,command="/a/nw worker ssh-command" {pk}',
                f'command="/a/nw worker ssh-command" {pk}',
                f'command="/a/nw worker ssh-command",restrict,pty {pk}',
                f'command="a/nw worker ssh-command",restrict {pk}',
                f'command="/a b/nw worker ssh-command",restrict {pk}',
                f'command="/a/nw worker ssh-command",restrict {pk}\nssh-ed25519 X'):
        assert not WB.line_ok(bad, pk), bad


@pytest.mark.parametrize("admin", [None, "ops@box"])
@pytest.mark.parametrize("existing", ["plain", "other-path"])
def test_key_already_authorized_unrestricted(world, existing, admin):
    world.ak.parent.mkdir()
    line = (world.pubkey if existing == "plain"
            else f'command="/old/bin/nw worker ssh-command",restrict {world.pubkey}')
    world.ak.write_text("ssh-ed25519 AAAAOTHER x\n" + line + "\n")
    before = world.ak.read_bytes()
    if admin:
        world.set("users", "nw\n")                  # the existing user already holds the blob
    res = boot(world, "box", "--ssh", "nw@box", "--install-key",
               *(["--admin", admin] if admin else []))
    assert res.exit_code == 1
    assert "key already authorized without the forced command; use a dedicated worker key" \
        in res.output
    assert world.ak.read_bytes() == before
    assert WK.read_strict(world.cfg) == []
    if admin:
        reads = [c for c in world.calls("ssh") if c["stdin"] == WB.READ_KEYS + "\n"]
        assert reads and all(c["argv"][-2:] == ["ops@box", "sudo -n -i -u nw sh -s"]
                             for c in reads)


@pytest.mark.parametrize("bad", ["relative/bin", "/tmp/with space"])
def test_bad_bin_dir(world, bad):
    world.set("bin", bad)
    ok, plan = plan_run(world)
    assert not ok and st(plan)["install"] == "failed"
    assert "unexpected uv tool bin dir" in plan.status["install"][1]
    assert st(plan)["key"] == "not run" and not world.ak.exists()
    assert not [c for c in world.calls("uv") if c["argv"][:1] in (["build"], ["tool"])
                and c["argv"][:2] != ["tool", "dir"]]
    assert not world.calls("scp")


@pytest.mark.parametrize("missing", ["pub", "priv"])
def test_missing_worker_key(world, missing):
    (world.pub if missing == "pub" else world.priv).unlink()
    res = boot(world, "box", "--ssh", "nw@box", "--install-key")
    assert res.exit_code == 1
    assert f"ssh-keygen -t ed25519 -N '' -f {world.priv}" in res.output
    assert world.calls() == []


def test_no_install_key(world):
    res = boot(world, "box", "--ssh", "nw@box")
    assert res.exit_code == 0, res.output
    assert world.line() in res.output
    assert "run nw workers check box after installing the key line" in res.output
    assert "not requested" in res.output
    assert not world.ak.exists()
    assert not [c for c in world.calls("ssh") if "IdentitiesOnly=yes" in c["argv"]]  # --no-check
    (rec,) = WK.read_strict(world.cfg)
    assert rec["nw"] == f"{world.bin}/nw" and rec["identity"] == str(world.priv)


def test_refuse_root(world):
    for extra in (["--install-key"], [], ["--dry-run"]):   # root is never a worker account
        res = boot(world, "box", "--ssh", "root@x", *extra)
        assert res.exit_code == 1 and "must not be root" in res.output, (extra, res.output)
        assert world.calls() == [] and WK.read_strict(world.cfg) == []
    world.set("uid", "0")
    ok, plan = plan_run(world)
    assert not ok and st(plan)["key"] == "failed" and "uid 0" in plan.status["key"][1]
    assert st(plan)["install"] == "not run" and st(plan)["register"] == "not run"
    assert not world.ak.exists() and not world.calls("scp") and not world.calls("uv")
    assert WK.read_strict(world.cfg) == []


@pytest.mark.parametrize("content,msg", [
    ("{k}\nssh-ed25519 AAAASECOND x\n", "must hold exactly one line (has 2)"),
    ("ssh-ed25519 not!base64 x\n", "is not a `<type> <base64> [comment]` public key"),
    (None, "must name a .pub file"),
])
def test_bad_worker_key(world, content, msg):
    if content is None:
        world.cfg["agentic"]["workers"]["key"] = str(world.priv)    # the private path
    else:
        world.pub.write_text(content.format(k=world.pubkey))
    res = boot(world, "box", "--ssh", "nw@box", "--install-key")
    assert res.exit_code == 1 and msg in res.output, res.output
    assert world.calls() == [] and WK.read_strict(world.cfg) == []


def test_default_key_path():
    want = Path(os.path.expanduser("~/.ssh/nw_worker_ed25519.pub"))
    assert WB.key_path({}) == want
    assert WB.key_path({"agentic": {"workers": {"key": ""}}}) == want
    assert WB.key_path({"agentic": {"workers": {"slots": 1}}}) == want
    assert WB.key_path({"agentic": {"workers": {"key": "~/k.pub"}}}) \
        == Path(os.path.expanduser("~/k.pub"))


def test_uv_missing(world):
    world.set("no_uv", "1")
    ok, plan = plan_run(world, install_key=False)
    assert not ok
    assert plan.status["install"] == ("failed", "uv not found for nw on the worker; "
                                                "install uv system-wide")
    assert st(plan) == {"admin": "not requested", "install": "failed", "verify": "not run",
                        "key": "not run", "register": "not run"}
    assert not world.calls("scp") and not world.calls("uv")
    assert WK.read_strict(world.cfg) == []


def test_uid_probe_unreachable_is_install(world):
    """With --install-key the uid probe runs first; a host it cannot reach fails `install`
    (the step that needs the worker), not `key`."""
    world.set("unreachable", "1")
    ok, plan = plan_run(world)
    assert not ok and st(plan)["install"] == "failed" and st(plan)["key"] == "not run"
    assert "could not run a command as nw on the worker (exit 255)" in plan.status["install"][1]
    assert "Host key verification failed" in plan.status["install"][1]


def test_install_key_after_no_check_runs_the_check(world):
    """Registered earlier with --no-check, a later --install-key run whose record matches
    still runs the dedicated-key check (the forced-command proof), and fails when it fails."""
    ok, plan = plan_run(world, install_key=False)
    assert ok and st(plan)["register"] == "done"
    world.clear()
    ok, plan = plan_run(world)
    assert ok and st(plan)["key"] == "done"
    assert plan.status["register"] == ("skipped (already registered)", "box, checked")
    assert [c for c in world.calls("ssh") if c["argv"][-1] == WK.FORCED_PROBE]
    world.set("check_fail", "1")                  # the dedicated-key ssh now fails
    world.clear()
    ok, plan = plan_run(world)
    assert not ok and st(plan)["key"].startswith("skipped") and st(plan)["register"] == "failed"
    assert "box failed its check" in plan.status["register"][1]
    (world.state / "check_fail").unlink()
    world.clear()
    ok, plan = plan_run(world, install_key=False)       # the hint is printed on a rerun too
    assert ok and any("run nw workers check box" in n for n in plan.notes)


def test_unreachable_is_not_uv_missing(world):
    world.set("unreachable", "1")
    ok, plan = plan_run(world, install_key=False)
    assert not ok and st(plan)["install"] == "failed"
    msg = plan.status["install"][1]
    assert "uv not found" not in msg
    assert "exit 255" in msg and "Host key verification failed" in msg


def test_admin_runas_refused_is_not_uv_missing(world):
    world.set("users", "nw\n")
    world.set("runas_fail", "1")                  # sudo -n true passes, sudo -u nw does not
    ok, plan = plan_run(world, admin="ops@box", install_key=False)
    assert not ok and st(plan)["admin"].startswith("skipped") and st(plan)["install"] == "failed"
    msg = plan.status["install"][1]
    assert "uv not found" not in msg and "through ops@box" in msg and "not allowed" in msg


def test_admin_useradd_fails(world):
    world.set("useradd_fail", "1")
    ok, plan = plan_run(world, admin="ops@box")
    assert not ok and st(plan) == {"admin": "failed", "install": "not run", "verify": "not run",
                                   "key": "not run", "register": "not run"}
    assert "useradd failed (exit 1)" in plan.status["admin"][1]
    assert "cannot lock /etc/passwd" in plan.status["admin"][1]
    assert not world.calls("uv") and not world.calls("scp") and not world.ak.exists()


@pytest.mark.parametrize("mode,msg", [("exit", "worker info --json failed (exit 3)"),
                                      ("garbage", "gave unreadable output")])
def test_verify_info_fails_after_install(world, mode, msg):
    world.set("install_version", mode)
    res = boot(world, "box", "--ssh", "nw@box", "--install-key")
    assert res.exit_code == 1 and msg in res.output, res.output
    ok, plan = plan_run(world)
    assert not ok and st(plan) == {"admin": "not requested", "install": "done",
                                   "verify": "failed", "key": "not run", "register": "not run"}
    assert not world.ak.exists() and WK.read_strict(world.cfg) == []


def test_key_line_form_refused_in_step(world, monkeypatch):
    monkeypatch.setattr(WB, "forced_line", lambda b, pk:
                        f'command="{b}/nw worker ssh-command",restrict,pty {pk}')
    ok, plan = plan_run(world)
    assert not ok and st(plan)["key"] == "failed" and st(plan)["register"] == "not run"
    assert "not exactly the" in plan.status["key"][1]
    assert not [c for c in world.calls("ssh") if "authorized_keys" in (c["stdin"] or "")]
    assert not world.ak.exists() and WK.read_strict(world.cfg) == []


@pytest.mark.parametrize("scp_fails", [False, True])
def test_cleanup_failure_is_a_warning(world, monkeypatch, scp_fails):
    real = WB._run

    def run(argv, timeout=WB.TIMEOUT_S, cwd=None, input=None):
        if "rm -rf" in (input or ""):
            raise WB.StepFailed(f"`ssh` timed out after {timeout:g}s")
        return real(argv, timeout=timeout, cwd=cwd, input=input)
    monkeypatch.setattr(WB, "_run", run)
    if scp_fails:
        world.set("scp_fail", "1")
    ok, plan = plan_run(world)
    assert any(n.startswith("warning: could not remove nw@box:") and "timed out" in n
               for n in plan.notes), plan.notes
    if scp_fails:                               # the real cause is kept
        assert not ok and st(plan)["install"] == "failed"
        assert plan.status["install"][1].startswith("scp failed (exit 1)")
    else:                                       # a finished install stays done
        assert ok and st(plan)["install"] == "done"


def test_check_needs_the_forced_command(world):
    """A dedicated key that reaches an unrestricted shell (a plain authorized_keys line, or
    an ssh_config IdentityFile fallback) fails the check even though `info` answers."""
    ok, plan = plan_run(world, install_key=False)
    assert ok
    (rec,) = WK.read_strict(world.cfg)
    world.ak.parent.mkdir()
    world.ak.write_text(world.pubkey + "\n")
    res = WK.check(world.cfg, rec)
    assert not res["ok"] and res["info"] is not None
    assert any("not bound to the forced command" in e for e in res["errors"]), res["errors"]
    assert CliRunner().invoke(cli, ["workers", "check", "box"]).exit_code != 0
    closes = [c["argv"] for c in world.calls("ssh") if "-O" in c["argv"]]
    assert closes and all(a[-2:] == ["exit", "nw@box"] and "IdentitiesOnly=yes" in a
                          for a in closes)            # the unrestricted master is closed
    world.ak.write_text(world.line() + "\n")
    res = WK.check(world.cfg, rec)
    assert res["ok"], res["errors"]
    probes = [c for c in world.calls("ssh") if c["argv"][-1] == WK.FORCED_PROBE]
    assert probes and all("IdentitiesOnly=yes" in c["argv"] for c in probes)


def test_failed_check_closes_the_master(world):
    """An identity record whose check fails after `info` answered (no git here) through an
    unrestricted line still gets the forced-command verdict, and its master is closed."""
    ok, plan = plan_run(world, install_key=False)
    assert ok
    (rec,) = WK.read_strict(world.cfg)
    world.ak.parent.mkdir()
    world.ak.write_text(world.pubkey + "\n")
    world.set("nogit", "1")
    world.clear()
    res = WK.check(world.cfg, rec)
    assert not res["ok"] and "git is missing on the worker" in res["errors"]
    assert any("not bound to the forced command" in e for e in res["errors"]), res["errors"]
    closes = [c["argv"] for c in world.calls("ssh") if "-O" in c["argv"]]
    assert len(closes) == 1 and closes[0][-2:] == ["exit", "nw@box"]
    world.ak.write_text(world.line() + "\n")            # forced line, but still no git
    world.clear()
    res = WK.check(world.cfg, rec)
    assert res["errors"] == ["git is missing on the worker"]
    assert [c for c in world.calls("ssh") if "-O" in c["argv"]]   # any failed check closes it
    (world.state / "nogit").unlink()
    world.clear()
    assert WK.check(world.cfg, rec)["ok"]
    assert not [c for c in world.calls("ssh") if "-O" in c["argv"]]


def test_registry_conflict(world):
    WK.update(world.cfg, lambda recs: recs.append(
        {"name": "box", "ssh": "nw@box", "slots": 3, "identity": str(world.priv)}))
    res = boot(world, "box", "--ssh", "nw@box", "--install-key")
    assert res.exit_code == 1
    assert "worker box is registered with different settings; nw workers rm box first" \
        in res.output
    assert world.calls() == []
    # a different `nw` is only visible after step 1: register fails, nothing is re-registered
    WK.update(world.cfg, lambda recs: recs.__setitem__(0, {**recs[0], "slots": 1,
                                                           "nw": "/elsewhere/nw"}))
    ok, plan = plan_run(world, install_key=False)
    assert not ok and st(plan)["register"] == "failed"
    assert WK.read_strict(world.cfg)[0]["nw"] == "/elsewhere/nw"


@pytest.mark.parametrize("field", ["ssh", "identity", "no-identity"])
def test_registry_conflict_ssh_identity(world, field):
    rec = {"name": "box", "ssh": "nw@box", "slots": 1, "identity": str(world.priv)}
    if field == "ssh":
        rec["ssh"] = "nw@other"
    elif field == "identity":
        rec["identity"] = "/elsewhere/old_key"
    else:
        del rec["identity"]
    WK.update(world.cfg, lambda recs: recs.append(rec))
    res = boot(world, "box", "--ssh", "nw@box", "--install-key")
    assert res.exit_code == 1
    assert "worker box is registered with different settings; nw workers rm box first" \
        in res.output
    assert world.calls() == []
    assert WK.read_strict(world.cfg) == [rec]


def test_check_fails_after_install(world):
    world.set("check_fail", "1")                  # the coordinator's dedicated-key ssh fails
    res = boot(world, "box", "--ssh", "nw@box", "--install-key")
    assert res.exit_code == 1
    assert "box failed its check" in res.output
    assert [c for c in world.calls("ssh") if "IdentitiesOnly=yes" in c["argv"]]
    assert WK.read_strict(world.cfg) == []
    world.clear()
    ok, plan = plan_run(world)
    assert not ok and st(plan)["register"] == "failed" and st(plan)["key"].startswith("skipped")
    assert WK.read_strict(world.cfg) == []


def test_admin_password_needed(world):
    world.set("sudo_fail", "1")
    res = boot(world, "box", "--ssh", "nw@box", "--admin", "ops@box", "--install-key")
    assert res.exit_code == 1
    assert "sudo on ADMIN needs a password" in res.output
    assert [c["argv"][-1] for c in world.calls("ssh")] == ["sudo -n true"]
    assert not world.calls("uv") and not world.calls("scp")


def test_admin_existing_user(world):
    world.set("users", "nw\n")
    ok, plan = plan_run(world, admin="ops@box")
    assert ok and st(plan)["admin"] == "skipped (already exists)"
    assert not [c for c in world.calls("sudo") if "useradd" in c["argv"]]


def test_admin_fresh_user_end_to_end(world):
    world.set_comment("o'neil $HOME `id` \\x@coord")   # survives sudo -i and both shells
    ok, plan = plan_run(world, admin="ops@box")
    assert ok, plan.status
    assert set(st(plan).values()) == {"done"}
    (ua,) = [c["argv"] for c in world.calls("sudo") if "useradd" in c["argv"]]
    assert ua == ["-n", "useradd", "-m", "nw"]
    admin_cmds = {"sudo -n true", "id -u nw", "sudo -n useradd -m nw", WB.ADMIN_MKTEMP}
    bootstrap_ssh = [c for c in world.calls("ssh") if "IdentitiesOnly=yes" not in c["argv"]]
    worker_side = []
    for call in bootstrap_ssh:
        argv = call["argv"]
        assert argv[-2] == "ops@box"
        cmd = argv[-1]
        if cmd in admin_cmds or cmd.split()[0] in ("chmod", "rm"):
            continue
        assert cmd == "sudo -n -i -u nw sh -s", cmd
        worker_side.append(call["stdin"])
    for what in ("command -v uv", "uv tool dir --bin", "worker info --json",
                 "uv tool install --force", "authorized_keys", "id -u"):
        assert any(what in c for c in worker_side), what
    (scp,) = world.calls("scp")
    assert scp["argv"][-1].startswith("ops@box:/tmp/nw-wheel.")    # not ADMIN's own TMPDIR
    assert world.ak.read_text().splitlines() == [world.line()]
    assert stat.S_IMODE(world.ak.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(world.ak.stat().st_mode) == 0o600
    (rec,) = WK.read_strict(world.cfg)
    assert rec["ssh"] == "nw@box" and rec["identity"] == str(world.priv)


def test_dry_run(world):
    res = boot(world, "box", "--ssh", "nw@box", "--install-key", "--admin", "ops@box",
               "--dry-run")
    assert res.exit_code == 0, res.output
    assert world.calls() == []
    assert not WK.path(world.cfg).exists()
    out = res.output
    assert "<uv tool bin dir>/nw worker ssh-command" in out
    assert "--nw '<uv tool bin dir>/nw'" in out and f"--identity {world.priv}" in out
    for what in ("sudo -n true", "useradd -m nw", "command -v uv", "uv tool dir --bin",
                 "uv build --wheel", "mktemp -d", "scp", "uv tool install --force",
                 "authorized_keys", "nw workers add box"):
        assert what in out, what
    assert out.count("would run") == 5
    res = boot(world, "box", "--ssh", "nw@box", "--dry-run")
    assert res.exit_code == 0 and "--no-check" in res.output and world.calls() == []


def test_controlpath_none(world):
    ok, plan = plan_run(world, admin="ops@box")
    assert ok
    own = [c for c in world.calls() if c["prog"] in ("ssh", "scp")
           and "IdentitiesOnly=yes" not in c["argv"]]     # the coordinator's own check excepted
    assert own and all("ControlPath=none" in c["argv"] for c in own)
