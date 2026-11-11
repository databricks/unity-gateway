"""Fixtures for the MDM provisioning E2E test.

This mutates the machine: it creates a user, writes /etc and /usr/local, and sudoers entries. It
only runs on a disposable host with UG_MDM_E2E=1, and imports nothing from ucode.
"""

import json
import os
import pwd
import secrets
import shutil
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


# The client secret and every minted bearer, masked in any output a failure prints.
SECRETS = [os.environ.get("UG_MDM_E2E_CLIENT_SECRET", "")]


def scrub(text: str) -> str:
    for value in filter(None, SECRETS):
        text = text.replace(value, "***")
    return text


def run(argv, *, timeout, extra_env=None, check=False, merge_stderr=True) -> Run:
    """Run a command with no stdin and no controlling terminal, so a password prompt cannot wait."""
    argv = [str(a) for a in argv]
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            text=True,
            timeout=timeout,
            start_new_session=True,
            env={**os.environ, **(extra_env or {})},
        )
    except subprocess.TimeoutExpired as exc:
        partial = (exc.output or b"").decode(errors="replace")
    else:
        partial = None
    # Outside the except block, so the failure doesn't chain TimeoutExpired, whose message holds
    # argv and with it any minted bearer, which GitHub doesn't mask.
    if partial is not None:
        pytest.fail(
            f"{scrub(str(argv[:4]))} timed out after {timeout}s:\n{scrub(partial)[-4000:]}",
            pytrace=False,
        )
    result = Run(proc.returncode, scrub(proc.stdout))
    if check and result.returncode:
        detail = result.output + scrub(proc.stderr or "")
        pytest.fail(f"{argv[:4]} exited {result.returncode}:\n{detail[-4000:]}", pytrace=False)
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

    def as_user(self, argv, *, home=str(HOME), timeout=60, **extra) -> Run:
        """Run as the test user from a clean environment, as a login from MDM-managed Macs would."""
        in_home = ["sh", "-c", 'cd "$HOME" && exec "$@"', "sh", *argv]
        return run(
            ["sudo", "-n", "-u", USER, "-H", "env", "-i", *self.env(home, **extra), *in_home],
            timeout=timeout,
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
    result = machine.as_user(
        [
            "sudo",
            "-n",
            "env",
            *machine.env("/var/root" if DARWIN else "/root"),
            "bash",
            str(PROVISION_SCRIPT),
            "/",
            "mdm-e2e",
            USER,
            machine.workspace,
            machine.client_id,
            machine.client_secret,
        ],
        timeout=1500,
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
