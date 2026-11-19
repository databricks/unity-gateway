"""Run the Jamf script with redirected paths and stubbed installers, never as root."""

import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts/mdm/macos-provision.sh"
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell component tests")


def _executable(path, content):
    path.write_text("#!/bin/bash\nset -eu\n" + content)
    path.chmod(0o755)


@pytest.fixture
def provision(tmp_path):
    shared = tmp_path / "usr/local"
    bin_dir = shared / "bin"
    bin_dir.mkdir(parents=True)
    mocks = tmp_path / "mocks"
    mocks.mkdir()
    etc = tmp_path / "etc"
    etc.mkdir()
    for name in ("zshrc", "bashrc"):
        (etc / name).write_text("# existing settings\n")
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    for name in ("mkdir", "mktemp", "rm", "cat"):
        executable = shutil.which(name)
        assert executable is not None
        (mocks / name).symlink_to(executable)
    _executable(mocks / "xcode-select", "exit 0\n")
    _executable(bin_dir / "uv", "exit 0\n")
    _executable(bin_dir / "databricks", "exit 0\n")
    _executable(
        mocks / "sed",
        '[ -z "${DATABRICKS_BEARER+x}" ] || exit 99\n'
        + (
            'if [ "$1" = -i ]; then shift 2; exec /usr/bin/sed -i "$@"; fi\n'
            if sys.platform != "darwin"
            else ""
        )
        + 'exec /usr/bin/sed "$@"\n',
    )
    _executable(
        mocks / "curl",
        'if [ "$1" = -fsSL ]; then\n'
        '  printf "node installer fixture" > "$3"\n'
        "else\n"
        "  cat >/dev/null\n"
        '  printf \'{"access_token":"test-token"}\'\n'
        "fi\n",
    )
    _executable(mocks / "plutil", 'cat >/dev/null\nprintf "test-token"\n')
    _executable(
        mocks / "installer",
        'printf "installed\\n" >> "$TEST_ROOT/node-install"\n'
        "for tool in node npm; do\n"
        '  /bin/ln -s /usr/bin/true "$npm_config_prefix/bin/$tool"\n'
        "done\n",
    )
    _executable(
        bin_dir / "ug",
        'printf "%s\\n" "$*" > "$TEST_ROOT/configure-args"\n'
        'printf "%s\\n" "$npm_config_prefix" "$HOME" "$DATABRICKS_BEARER" '
        '> "$TEST_ROOT/configure-env"\n'
        'if [ "${UG_EXIT:-0}" != 0 ]; then exit "$UG_EXIT"; fi\n'
        "for agent in claude codex; do\n"
        '  [ "$agent" != "${MISSING_AGENT:-}" ] || continue\n'
        '  /bin/ln -sf /usr/bin/true "$npm_config_prefix/bin/$agent"\n'
        "done\n",
    )
    text = SCRIPT.read_text()
    text = text.replace("/usr/local", str(shared)).replace("/var/root", str(tmp_path / "root"))
    text = text.replace(f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin", f"{bin_dir}:{mocks}")
    text = text.replace("/etc/zshrc", str(etc / "zshrc"))
    text = text.replace("/etc/bashrc", str(etc / "bashrc"))
    text = text.replace("/usr/bin/curl", str(mocks / "curl"))
    text = text.replace("/usr/sbin/installer", str(mocks / "installer"))
    text = re.sub(
        r"NODE_SHA256=[0-9a-f]+",
        "NODE_SHA256=" + hashlib.sha256(b"node installer fixture").hexdigest(),
        text,
    )
    script = tmp_path / "provision.sh"
    script.write_text(text)
    env = {"PATH": str(mocks), "TMPDIR": str(downloads), "TEST_ROOT": str(tmp_path)}

    def run(*, node_present=False, **overrides):
        if node_present:
            for name in ("node", "npm"):
                (bin_dir / name).symlink_to("/usr/bin/true")
        return subprocess.run(
            [
                "/bin/bash",
                str(script),
                "/",
                "computer",
                "user",
                "https://workspace.invalid/",
                "id",
                "secret",
            ],
            env={**env, **overrides},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )

    return run, tmp_path


def test_provision_delegates_agents_and_marks_mdm(provision):
    run, root = provision
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "configure-args").read_text().split() == [
        "configure",
        "--global",
        "--mdm",
        "--workspace",
        "https://workspace.invalid",
        "--agents",
        "claude,codex",
    ]
    assert (root / "configure-env").read_text().splitlines() == [
        str(root / "usr/local"),
        str(root / "root"),
        "test-token",
    ]
    assert (root / "node-install").read_text() == "installed\n"
    assert list((root / "downloads").iterdir()) == []
    for name in ("zshrc", "bashrc"):
        content = (root / "etc" / name).read_text()
        assert content.startswith("# existing settings\n")
        assert "alias claude='UG_SKIP_OS_SETTINGS=1 ug claude'" in content
        assert "alias codex='UG_SKIP_OS_SETTINGS=1 ug codex'" in content


def test_provision_reuses_shared_node_and_rewrites_aliases(provision):
    run, root = provision
    for result in (run(node_present=True), run()):
        assert result.returncode == 0, result.stdout + result.stderr
    assert not (root / "node-install").exists()
    for name in ("zshrc", "bashrc"):
        assert (root / "etc" / name).read_text().count("# BEGIN unity-gateway") == 1


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_provision_accepts_a_single_agent_workspace(provision, agent):
    run, root = provision
    result = run(MISSING_AGENT=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (root / "usr/local/bin" / agent).exists()
    assert "# BEGIN unity-gateway" in (root / "etc/zshrc").read_text()


def test_provision_propagates_configure_failure(provision):
    run, root = provision
    assert run(UG_EXIT="42").returncode == 42
    assert "# BEGIN unity-gateway" not in (root / "etc/zshrc").read_text()


def test_provision_rejects_bad_node_checksum(provision):
    run, root = provision
    _executable(root / "mocks/curl", 'printf "corrupt installer" > "$3"\n')
    result = run()
    assert result.returncode != 0
    assert not (root / "node-install").exists()
    assert not (root / "configure-args").exists()
    assert list((root / "downloads").iterdir()) == []
