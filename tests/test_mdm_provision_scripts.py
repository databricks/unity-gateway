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
        line
        for line in _commands(path)
        if line.startswith(
            ("export UV_", "UG_REPO=", "UG_INSTALL_SPEC=", "uv tool install", "ug configure")
        )
        or "/oidc/v1/token" in line
    ]


def test_linux_script_matches_macos_install_token_and_configure_steps():
    macos = _shared(MACOS)
    assert any("scope=" in line for line in macos)
    assert any(line.startswith("ug configure") for line in macos)
    assert _shared(LINUX) == macos
