"""Run the Linux MDM script with redirected paths and stubbed installers, never as root."""

import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts/mdm/linux-provision.sh"
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="GNU tools")
RCS = ("bash.bashrc", "zsh/zshrc")


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
    (etc / "zsh").mkdir(parents=True)
    for name in RCS:
        (etc / name).write_text("# existing settings\n")
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    for name in ("mkdir", "mktemp", "rm", "cat", "grep", "sed", "ln", "sha256sum"):
        executable = shutil.which(name)
        assert executable is not None
        (mocks / name).symlink_to(executable)
    (mocks / "python3").symlink_to(sys.executable)
    for name in ("git", "unzip"):
        _executable(mocks / name, "exit 0\n")
    _executable(mocks / "id", 'echo "${TEST_UID:-0}"\n')
    _executable(mocks / "uname", 'echo "${TEST_ARCH:-x86_64}"\n')
    _executable(bin_dir / "uv", "exit 0\n")
    _executable(bin_dir / "databricks", "exit 0\n")
    _executable(
        mocks / "curl",
        'if [ "$1" = -fsSL ]; then\n'
        '  printf "node tarball fixture" > "$3"\n'
        "else\n"
        "  cat >/dev/null\n"
        '  printf \'{"access_token":"test-token"}\'\n'
        "fi\n",
    )
    _executable(
        mocks / "tar",
        'printf "installed\\n" >> "$TEST_ROOT/node-install"\n'
        '/bin/mkdir -p "$4/bin"\n'
        'for tool in node npm npx; do printf "#!/bin/sh\\necho 22\\n" > "$4/bin/$tool"; done\n'
        '/bin/chmod 755 "$4"/bin/*\n',
    )
    _executable(
        bin_dir / "ug",
        'printf "%s\\n" "$*" > "$TEST_ROOT/configure-args"\n'
        'printf "%s\\n" "$npm_config_prefix" "$HOME" "$DATABRICKS_BEARER" '
        '> "$TEST_ROOT/configure-env"\n',
    )
    text = SCRIPT.read_text()
    text = text.replace("/usr/local", str(shared)).replace("HOME=/root", f"HOME={tmp_path}/root")
    text = text.replace(f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin", f"{bin_dir}:{mocks}")
    text = text.replace("/etc/", f"{etc}/")
    digest = hashlib.sha256(b"node tarball fixture").hexdigest()
    text = re.sub(r"NODE_SHA256_(X64|ARM64)=[0-9a-f]+", rf"NODE_SHA256_\1={digest}", text)
    script = tmp_path / "provision.sh"
    script.write_text(text)
    env = {"PATH": str(mocks), "TMPDIR": str(downloads), "TEST_ROOT": str(tmp_path)}

    def run(*, workspace="https://workspace.invalid/", cwd=None, args=None, **overrides):
        return subprocess.run(
            ["/bin/bash", str(script), *(args or ["_", "_", "_", workspace, "id", "secret"])],
            env={**env, **overrides},
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )

    return run, tmp_path


def test_provision_installs_node_and_marks_mdm(provision):
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
    node_bin = root / "usr/local/unity-gateway/node/bin"
    assert (root / "usr/local/bin/node").resolve().parent == node_bin
    assert list((root / "downloads").iterdir()) == []
    for name in RCS:
        content = (root / "etc" / name).read_text()
        assert content.startswith("# existing settings\n")
        assert content.count("alias claude='UG_SKIP_OS_SETTINGS=1 ug claude'") == 1


def test_provision_reuses_node_and_rewrites_aliases(provision):
    run, root = provision
    for _ in range(2):
        result = run()
        assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "node-install").read_text() == "installed\n"
    for name in RCS:
        assert (root / "etc" / name).read_text().count("# BEGIN unity-gateway") == 1


def test_provision_reinstalls_old_node(provision):
    run, root = provision
    _executable(root / "mocks/node", "echo 18\n")
    _executable(root / "mocks/npm", "exit 0\n")
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "node-install").read_text() == "installed\n"


@pytest.mark.parametrize(
    ("workspace", "uid"), [("http://workspace.invalid", "0"), ("https://workspace.invalid", "1000")]
)
def test_provision_rejects_http_and_non_root_before_installing(provision, workspace, uid):
    run, root = provision
    result = run(workspace=workspace, TEST_UID=uid)
    assert result.returncode == 2
    assert not (root / "node-install").exists()
    assert not (root / "configure-args").exists()


def test_provision_prints_usage_without_the_jamf_style_arguments(provision):
    run, root = provision
    result = run(args=["https://workspace.invalid", "id", "secret"])
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert "unbound variable" not in result.stderr
    assert not (root / "configure-args").exists()


def test_provision_rejects_unsupported_architectures(provision):
    run, root = provision
    result = run(TEST_ARCH="ppc64le")
    assert result.returncode == 1
    assert "Unsupported CPU architecture: ppc64le" in result.stderr
    assert not (root / "node-install").exists()
    assert not (root / "configure-args").exists()


def test_provision_rejects_bad_node_checksum(provision):
    run, root = provision
    _executable(root / "mocks/curl", 'printf "corrupt tarball" > "$3"\n')
    result = run()
    assert result.returncode != 0
    assert not (root / "node-install").exists()
    assert not (root / "configure-args").exists()
    assert list((root / "downloads").iterdir()) == []


def test_provision_refuses_an_unterminated_alias_block(provision):
    run, root = provision
    rc = root / "etc/bash.bashrc"
    rc.write_text("# BEGIN unity-gateway (managed by MDM)\n# existing settings\n")
    result = run()
    assert result.returncode == 1
    assert "no END line" in result.stderr
    assert rc.read_text() == "# BEGIN unity-gateway (managed by MDM)\n# existing settings\n"


def test_token_parsing_ignores_modules_in_the_working_directory(provision, tmp_path):
    run, root = provision
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "json.py").write_text(f"open({str(root / 'hijacked')!r}, 'w').close()\n")
    result = run(cwd=planted)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (root / "hijacked").exists()
