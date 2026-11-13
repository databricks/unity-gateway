"""Fixtures for the MDM provisioning E2E test.

This mutates the machine: it creates a user, writes /etc and /usr/local, and sudoers entries. It
only runs on a disposable host with UG_MDM_E2E=1, and imports nothing from ucode.
"""

import json
import os
import pwd
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from base64 import b64encode
from dataclasses import dataclass, field
from pathlib import Path

import pytest

DARWIN = sys.platform == "darwin"
HERE = Path(__file__).parent
ROOT = HERE.parent.parent
USER = "ugmdm"
HOME = Path("/Users" if DARWIN else "/home") / USER
SUDOERS = Path("/etc/sudoers.d") / f"ug-mdm-e2e-{USER}"
PROVISION_SCRIPT = (
    ROOT / "scripts/mdm/macos-provision.sh" if DARWIN else HERE / "linux-provision.sh"
)
MACHINE_WIDE_ROOTS = (
    Path("/etc/unitygateway"),
    Path("/etc/claude-code"),
    Path("/etc/codex"),
    Path("/Library/Application Support/ClaudeCode"),
)
POLICY = Path("/etc/unitygateway/managed-settings.json")
CLAUDE_SETTINGS = (
    Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    if DARWIN
    else Path("/etc/claude-code/managed-settings.json")
)
CLAUDE_PLUGINS = CLAUDE_SETTINGS.parent / "ucode-plugins"
CODEX_SETTINGS = Path("/etc/codex/managed_config.toml")
# Machine-wide binaries ug, Claude, Codex and the Databricks CLI install (stat only).
INSTALLED = (
    Path("/usr/local/bin/ug"),
    Path("/usr/local/bin/claude"),
    Path("/usr/local/bin/codex"),
    Path("/usr/local/bin/codex-code-mode-host"),
    Path("/usr/local/bin/databricks"),
    Path("/usr/local/bin/uv"),
    Path("/usr/local/unity-gateway"),
)
# Network and certificate settings forwarded into the privileged and user processes.
PASSTHROUGH = (
    "UG_INSTALL_SPEC",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "UV_SYSTEM_CERTS",
    "UV_DEFAULT_INDEX",
    "UV_PYTHON",
    "NPM_CONFIG_REGISTRY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
)
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

WALKER = r"""
import hashlib, json, os, stat, sys

def entry(path, digest):
    st = os.lstat(path)
    row = [st.st_mode, st.st_uid, st.st_gid, st.st_size, st.st_ino, st.st_mtime_ns, st.st_ctime_ns]
    if stat.S_ISLNK(st.st_mode):
        row.append(os.readlink(path))
    elif digest and stat.S_ISREG(st.st_mode):
        with open(path, "rb") as f:
            row.append(hashlib.sha256(f.read()).hexdigest())
    return row

out = {}
for root, digest in json.loads(sys.argv[1]):
    if not os.path.lexists(root):
        continue
    out[root] = entry(root, digest)
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(base, name)
            out[path] = entry(path, digest)
json.dump(out, sys.stdout)
"""


@dataclass
class Run:
    returncode: int
    output: str
    stderr: str = ""


# The client secret and every minted bearer, masked in any output a failure prints.
SECRETS = [os.environ.get("UG_MDM_E2E_CLIENT_SECRET", "")]
# A Databricks OAuth token, such as the one the provisioning script mints and exports.
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")


def scrub(text: str) -> str:
    for value in filter(None, SECRETS):
        text = text.replace(value, "***")
    return JWT.sub("***", text)


def kill_tree(pid: int) -> None:
    """Kill a process, its descendants and its process group, including root-owned members.

    The group catches a child whose parent already exited, which the descendant walk can't find.
    The walk catches what sudo runs in a session of its own.
    """
    listing = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,pgid="], capture_output=True, text=True, check=False
    ).stdout
    children: dict[int, list[int]] = {}
    targets = set()
    for line in listing.splitlines():
        child, parent, group = map(int, line.split())
        children.setdefault(parent, []).append(child)
        if group == pid:
            targets.add(child)
    stack = [pid]
    while stack:
        current = stack.pop()
        targets.add(current)
        stack.extend(children.get(current, ()))
    denied = []
    for target in sorted(targets):
        try:
            os.kill(target, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            denied.append(str(target))
    if denied:
        subprocess.run(
            ["sudo", "-n", "kill", "-KILL", *denied],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
        )


def run(argv, *, timeout, check=False, merge_stderr=True, secret=None) -> Run:
    """Run a command with no controlling terminal and no stdin, so a password prompt cannot wait.

    A `secret` is the only stdin, for a command that reads it first and gives what it runs
    /dev/null: sudo logs arguments and preserved variables, but not stdin.
    """
    argv = [str(a) for a in argv]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL if secret is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(
            None if secret is None else f"{secret}\n", timeout=timeout
        )
        timed_out = False
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            for stream in (proc.stdout, proc.stderr):
                if stream:
                    stream.close()
            proc.kill()
            proc.wait()
            stdout, stderr = "(a process still held the output open after the kill)", ""
        timed_out = True
    # Outside the except block, so the failure doesn't chain TimeoutExpired, whose message holds
    # argv and with it any minted bearer, which GitHub doesn't mask.
    if timed_out:
        pytest.fail(
            f"{scrub(str(argv[:4]))} timed out after {timeout}s:\n{scrub(stdout or '')[-4000:]}",
            pytrace=False,
        )
    result = Run(proc.returncode, scrub(stdout), scrub(stderr or ""))
    if check and result.returncode:
        detail = result.output + result.stderr
        pytest.fail(
            f"{scrub(str(argv[:4]))} exited {result.returncode}:\n{detail[-4000:]}", pytrace=False
        )
    return result


def sudo(*argv, timeout=60, check=True, merge_stderr=True) -> Run:
    return run(["sudo", "-n", *argv], timeout=timeout, check=check, merge_stderr=merge_stderr)


@dataclass
class Machine:
    workspace: str
    client_id: str
    client_secret: str = field(repr=False)

    def env(self, home: str, **extra) -> list[str]:
        values = {
            "HOME": home,
            "PATH": SAFE_PATH,
            "TERM": "dumb",
            "LANG": "C.UTF-8",
            **{k: os.environ[k] for k in PASSTHROUGH if k in os.environ},
            **extra,
        }
        return [f"{k}={v}" for k, v in values.items()]

    def as_user(
        self, argv, *, home=str(HOME), timeout=60, secret=None, merge_stderr=True, **extra
    ) -> Run:
        """Run as the test user from a clean environment, as a login from MDM-managed Macs would.

        A `secret` reaches the command on stdin; see `run` and `with_secret`.
        """
        in_home = ["sh", "-c", 'cd "$HOME" && exec "$@"', "sh", *argv]
        return run(
            ["sudo", "-n", "-u", USER, "-H", "env", "-i", *self.env(home, **extra), *in_home],
            timeout=timeout,
            secret=secret,
            merge_stderr=merge_stderr,
        )

    def mint_bearer(self) -> str:
        # Stands in for the developer's own OAuth sign-in, limited to the two APIs a launch uses.
        # The :read scopes can't send inference requests. Only this token, never the client
        # secret, reaches the demoted user.
        basic = b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        body = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "scope": "ai-gateway unity-catalog"}
        )
        request = urllib.request.Request(
            f"{self.workspace}/oidc/v1/token",
            data=body.encode(),
            headers={"Authorization": f"Basic {basic}"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            token = json.load(response)["access_token"]
        SECRETS.append(token)
        return token


ROOT_HOME = Path("/var/root" if DARWIN else "/root")
# What root's configure, the Databricks CLI and the agents write in the home they run with.
ROOT_HOME_STATE = (
    ".ucode",
    ".claude",
    ".claude.json",
    ".codex",
    ".databricks",
    ".databrickscfg",
    ".config",
)
CREDENTIAL_SCAN = r"""
import os, re, sys

secret = sys.stdin.readline().rstrip("\n")
jwt = re.compile(sys.argv[1])
leaked = []
for root in sys.argv[2:]:
    paths = [root] if os.path.isfile(root) else [
        os.path.join(base, name) for base, _, names in os.walk(root) for name in names
    ]
    for path in paths:
        if os.path.islink(path):
            continue
        with open(path, errors="ignore") as f:
            text = f.read()
        if (secret and secret in text) or jwt.search(text):
            leaked.append(path)
print("\n".join(leaked))
"""


def with_secret(name: str, argv) -> list[str]:
    """Run argv with the secret on stdin exported as `name`, and /dev/null as its stdin."""
    return ["sh", "-c", f'IFS= read -r {name}; export {name}; exec "$@" </dev/null', "sh", *argv]


def credential_leaks(machine, roots) -> list[str]:
    """Files under roots that hold the client secret or a token, read as root."""
    result = run(
        [
            "sudo",
            "-n",
            sys.executable,
            "-I",
            "-c",
            CREDENTIAL_SCAN,
            JWT.pattern,
            *(str(root) for root in roots),
        ],
        timeout=120,
        check=True,
        merge_stderr=False,
        secret=machine.client_secret,
    )
    return [line for line in result.output.splitlines() if line]


def snapshot(roots) -> dict:
    """Mode, owner, size, inode, mtime, ctime and content hash of every file, read as root."""
    spec = json.dumps([[str(path), digest] for path, digest in roots])
    result = sudo(sys.executable, "-I", "-c", WALKER, spec, timeout=300, merge_stderr=False)
    return json.loads(result.output)


def machine_snapshot() -> dict:
    return snapshot([(p, True) for p in MACHINE_WIDE_ROOTS] + [(p, False) for p in INSTALLED])


@pytest.fixture(scope="session")
def machine():
    if os.environ.get("UG_MDM_E2E") != "1":
        pytest.skip(
            "Set UG_MDM_E2E=1 to run on a disposable host: it creates a user and writes /etc"
        )
    values = {
        name: os.environ.get(name, "")
        for name in (
            "UG_MDM_E2E_WORKSPACE",
            "UG_MDM_E2E_CLIENT_ID",
            "UG_MDM_E2E_CLIENT_SECRET",
        )
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        pytest.fail(f"Set {', '.join(missing)}.", pytrace=False)
    assert run(["sudo", "-n", "true"], timeout=30).returncode == 0, "Needs passwordless sudo"
    dirty = [p for p in (POLICY, CLAUDE_SETTINGS, CODEX_SETTINGS) if p.exists()]
    assert not dirty and not HOME.exists(), f"Not a clean host: {dirty or HOME}"

    yield Machine(
        values["UG_MDM_E2E_WORKSPACE"].rstrip("/"),
        values["UG_MDM_E2E_CLIENT_ID"],
        values["UG_MDM_E2E_CLIENT_SECRET"],
    )
    sudo("rm", "-f", str(SUDOERS), check=False)


def create_user() -> None:
    if DARWIN:
        sudo(
            "sysadminctl",
            "-addUser",
            USER,
            "-fullName",
            "UG MDM",
            "-home",
            str(HOME),
            "-password",
            secrets.token_urlsafe(18),
            "-admin",
            timeout=120,
        )
        if not HOME.exists():
            sudo("mkdir", "-p", str(HOME))
            sudo("chown", f"{USER}:staff", str(HOME))
    else:
        # GitHub's Ubuntu images keep large toolchains in /etc/skel, which `useradd -m` copies.
        with tempfile.TemporaryDirectory() as skel:
            sudo("useradd", "-m", "-k", skel, "-s", "/bin/bash", USER)


@pytest.fixture(scope="module")
def admin(machine):
    """A fresh user with passwordless sudo, the account the provisioning is run from."""
    create_user()
    staging = Path(tempfile.mkdtemp(prefix="sudoers-", dir="/tmp"))
    entry = staging / "entry"
    entry.write_text(f"{USER} ALL=(ALL) NOPASSWD: ALL\n")
    entry.chmod(0o440)
    sudo("install", "-m", "0440", "-o", "root", str(entry), str(SUDOERS))
    shutil.rmtree(staging, ignore_errors=True)
    assert machine.as_user(["sudo", "-n", "true"]).returncode == 0
    return USER


@pytest.fixture(scope="module")
def provisioned(machine, admin):
    """Run the provisioning script as the admin user, the way MDM runs it as root, without a password."""
    # Sourced with Jamf's arguments set by `set --`, so the secret is $6 as Jamf passes it, but
    # it reaches root on stdin and is never a process argument.
    source = 'IFS= read -r secret; set -- / mdm-e2e "$1" "$2" "$3" "$secret"; unset secret; . "$0" </dev/null'
    result = machine.as_user(
        [
            "sudo",
            "-n",
            "env",
            *machine.env("/var/root" if DARWIN else "/root"),
            "bash",
            "-c",
            source,
            str(PROVISION_SCRIPT),
            USER,
            machine.workspace,
            machine.client_id,
        ],
        timeout=1500,
        secret=machine.client_secret,
    )
    return result


@dataclass
class Demoted:
    machine_wide: dict
    home: dict
    bearer: str = field(repr=False)


@pytest.fixture(scope="module")
def demoted(machine, provisioned):
    """Remove the user's sudo and capture the machine-wide state the launches must not change."""
    assert provisioned.returncode == 0, provisioned.output[-4000:]
    sudo("rm", "-f", str(SUDOERS))
    if DARWIN:
        sudo("dseditgroup", "-o", "edit", "-d", USER, "-t", "user", "admin")
    assert machine.as_user(["sudo", "-n", "true"]).returncode != 0, "User still has sudo"
    return Demoted(machine_snapshot(), snapshot([(HOME, False)]), machine.mint_bearer())


def user_uid() -> int:
    return pwd.getpwnam(USER).pw_uid
