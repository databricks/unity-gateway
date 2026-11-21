"""The Linux and macOS MDM provisioning scripts must install, authenticate and configure alike."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MACOS = ROOT / "scripts" / "mdm" / "macos-provision.sh"
LINUX = ROOT / "scripts" / "mdm" / "linux-provision.sh"


def _commands(path: Path) -> list[str]:
    joined = re.sub(r"\\\n\s*", " ", path.read_text(encoding="utf-8"))
    lines = [line.strip() for line in joined.splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def _shared(path: Path) -> list[str]:
    return [
        # The token request's scope and endpoint, not curl's transport flags.
        " ".join(re.findall(r"scope=[^']*|/oidc/v1/token", line))
        if "/oidc/v1/token" in line
        else line
        for line in _commands(path)
        if line.startswith(
            (
                "export UV_",
                "export npm_config_prefix=",
                "NODE_VERSION=",
                "UG_REPO=",
                "UG_INSTALL_SPEC=",
                "uv tool install",
                "/usr/local/bin/ug configure",
                "alias ",
            )
        )
        or "/oidc/v1/token" in line
    ]


def test_linux_script_matches_macos_install_token_configure_and_alias_steps():
    macos = _shared(MACOS)
    assert any("scope=" in line for line in macos)
    assert any(line.startswith("/usr/local/bin/ug configure --global --mdm") for line in macos)
    assert any(line.startswith("alias ") for line in macos)
    assert _shared(LINUX) == macos
