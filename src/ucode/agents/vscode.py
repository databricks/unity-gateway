"""VS Code: install ug's model extension so Copilot Chat can use the workspace's AI Gateway models.

Configure-only, like Cursor (it stays out of `agents.__init__._MODULES`): `ug configure --agents
vscode` packs the extension that ships with ug into a vsix, together with a `ug-config.json` that
holds the workspace, its models and the auth command, and installs it with VS Code's `code` command.
The extension reads that file from its own folder, so ug never edits the user's settings.json.
`ug revert` uninstalls the extension.

The models come from one source: the workspace's own (`system.ai`), a Model Provider Service
(`--provider`), or the model services of a Unity Catalog schema (`--model-location`).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from collections import Counter
from pathlib import Path
from typing import cast
from xml.sax.saxutils import escape

from ucode.agents.copilot import (
    CopilotModel,
    build_copilot_catalog,
    catalog_model_limits,
    claude_model_name,
    location_catalog,
    provider_service_catalog,
)
from ucode.custom_oauth import CustomOAuthConfig, build_custom_auth_shell_command, launch_token
from ucode.databricks import build_auth_shell_command, discover_model_services
from ucode.managed_files import OS, current_os
from ucode.os_compatibility import subprocess_cross_os
from ucode.os_compatibility.system_cross_os import windows_program_files_dir
from ucode.string_utils import model_picker_label
from ucode.ui import console, print_heading, print_kv, print_warning

EXTENSION_DIR = Path(__file__).resolve().parent.parent / "vscode" / "extension"
EXTENSION_ID = "databricks.unity-gateway"
# Read by the extension from its install folder; see CONFIG_FILE in vscode-extension/src/extension.ts.
CONFIG_FILE = "ug-config.json"
# Each `code` command and the folder in the home directory it installs extensions under.
CODE_COMMANDS = (("code", ".vscode"), ("code-insiders", ".vscode-insiders"))
_CODE_TIMEOUT_S = 120


def _install_locations(command: str) -> list[Path]:
    """Where VS Code's installer puts `command` when it isn't on PATH (by default on macOS)."""
    insiders = command == "code-insiders"
    system = current_os()
    if system is OS.MACOS:
        app = "Visual Studio Code - Insiders.app" if insiders else "Visual Studio Code.app"
        return [
            root / app / "Contents/Resources/app/bin" / command
            for root in (Path("/Applications"), Path.home() / "Applications")
        ]
    if system is OS.WINDOWS:
        folder = "Microsoft VS Code Insiders" if insiders else "Microsoft VS Code"
        roots = (
            [Path(os.environ["LOCALAPPDATA"]) / "Programs"]
            if os.environ.get("LOCALAPPDATA")
            else []
        )
        return [
            root / folder / "bin" / f"{command}.cmd"
            for root in [*roots, windows_program_files_dir()]
        ]
    return [Path("/usr/bin") / command, Path("/snap/bin") / command]


def _find_command(command: str) -> Path | None:
    if found := shutil.which(command):
        return Path(found)
    return next((path for path in _install_locations(command) if path.is_file()), None)


def find_code() -> Path | None:
    """VS Code's `code` command, or Insiders' when only that is installed."""
    for command, _ in CODE_COMMANDS:
        if found := _find_command(command):
            return found
    return None


def extensions_dir(folder: str) -> Path:
    return Path.home() / folder / "extensions"


def _package() -> dict:
    return json.loads((EXTENSION_DIR / "package.json").read_text(encoding="utf-8"))


def pack_vsix(directory: Path, config: dict[str, object]) -> Path:
    """Zip the bundled extension and its ``config`` into a vsix in ``directory``, with no Node or
    `vsce` needed."""
    package = _package()
    manifest = f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="{escape(package["name"])}" Version="{escape(package["version"])}" Publisher="{escape(package["publisher"])}"/>
    <DisplayName>{escape(package["displayName"])}</DisplayName>
    <Description xml:space="preserve">{escape(package["description"])}</Description>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{escape(package["engines"]["vscode"])}"/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="{escape(",".join(package["extensionKind"]))}"/>
    </Properties>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>
  <Dependencies/>
  <Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/></Assets>
</PackageManifest>
"""
    content_types = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension=".json" ContentType="application/json"/>'
        '<Default Extension=".js" ContentType="application/javascript"/>'
        '<Default Extension=".svg" ContentType="image/svg+xml"/>'
        '<Default Extension=".vsixmanifest" ContentType="text/xml"/>'
        "</Types>"
    )
    vsix = directory / f"{package['name']}-{package['version']}.vsix"
    with zipfile.ZipFile(vsix, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("extension.vsixmanifest", manifest)
        for path in sorted(EXTENSION_DIR.rglob("*")):
            if path.is_file():
                archive.write(path, f"extension/{path.relative_to(EXTENSION_DIR).as_posix()}")
        archive.writestr(f"extension/{CONFIG_FILE}", json.dumps(config, indent=2) + "\n")
    return vsix


def _run_code(code: Path, *args: str):
    try:
        return subprocess_cross_os.run(
            [str(code), *args], capture_output=True, text=True, timeout=_CODE_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"`{code} {args[0]}` failed: {exc}") from exc


def installed_version(code: Path) -> str | None:
    result = _run_code(code, "--list-extensions", "--show-versions")
    for line in (result.stdout or "").splitlines():
        name, _, version = line.strip().partition("@")
        if name.lower() == EXTENSION_ID:
            return version
    return None


def install_extension(code: Path, config: dict[str, object]) -> str:
    """Install the bundled extension with ``config`` over whatever is installed; returns what
    happened."""
    # Always reinstall: the config changes with each configure, and a ug upgrade can change the
    # bundle without changing its version.
    version = _package()["version"]
    with tempfile.TemporaryDirectory(prefix="ug-vscode-") as directory:
        vsix = pack_vsix(Path(directory), config)
        result = _run_code(code, "--install-extension", str(vsix), "--force")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise RuntimeError(f"`{code} --install-extension` failed: {detail}")
    return f"installed {EXTENSION_ID} {version}"


def _model_entry(model: CopilotModel, name: str, *, external: bool) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": model.id,
        "name": name,
        "family": claude_model_name(model.id),
        # The wire names of Copilot's catalog; the extension dispatches on them (see APIS in
        # vscode-extension/src/extension.ts).
        "api": model.wire,
    }
    # Models without known limits get the extension's 128K-in, 32K-out default.
    if limits := catalog_model_limits(model, external=external):
        entry["maxInputTokens"], entry["maxOutputTokens"] = limits
    return entry


def _catalog(state: dict, provider: str | None, location: str | None) -> list[CopilotModel]:
    """The models Copilot CLI would offer from the same source; raises when there are none."""
    if provider:
        return provider_service_catalog(state, provider, None, tool="vscode")
    if location:
        return location_catalog(state, location)
    workspace = state["workspace"]
    # Gemini and open models, gpt-oss among them, fail on Copilot's requests or aren't served on
    # every workspace.
    claude, gpt, _gemini, _oss, reason = discover_model_services(
        workspace, launch_token(state, workspace)
    )
    catalog = build_copilot_catalog([*claude.values(), *gpt])
    if not catalog:
        raise RuntimeError(
            f"No Claude or GPT models for VS Code ({reason or 'the workspace has none'})."
        )
    return catalog


def model_entries(
    state: dict, *, provider: str | None = None, location: str | None = None
) -> list[dict[str, object]]:
    """The models of the chosen source, each with the API the extension calls it on."""
    catalog = _catalog(state, provider, location)
    labels = {model: model_picker_label(claude_model_name(model.id)) for model in catalog}
    counts = Counter(labels.values())
    # As in Codex's picker: two models with the same label show their full ids instead.
    return [
        _model_entry(
            model, model.id if counts[label] > 1 else label, external=bool(provider or location)
        )
        for model, label in labels.items()
    ]


def auth_command(state: dict) -> str:
    workspace = state["workspace"]
    custom_oauth = state.get("custom_oauth")
    if isinstance(custom_oauth, dict):
        return build_custom_auth_shell_command(workspace, cast(CustomOAuthConfig, custom_oauth))
    return build_auth_shell_command(
        workspace, state.get("profile"), use_pat=bool(state.get("use_pat"))
    )


def next_steps() -> list[str]:
    """What to do in VS Code once the extension is installed, for this OS."""
    restart = (
        "quit it (Cmd+Q) and open it again"
        if current_os() is OS.MACOS
        else "close every VS Code window and open it again"
    )
    return [
        # An open window keeps running the extension build it loaded, and configure reinstalls
        # every time. A restart covers every open window at once.
        f"1. If VS Code is open, {restart}.",
        "2. Open Chat, click the model picker, and choose a Databricks model.",
    ]


def configure(state: dict, *, provider: str | None = None, location: str | None = None) -> bool:
    """Install the extension with this workspace's setup and the models of ``provider`` (a Model
    Provider Service), ``location`` (a ``<catalog>.<schema>``) or else ``system.ai``. Returns False,
    after a warning, if any step fails."""
    code = find_code()
    if code is None:
        print_warning(
            "VS Code's `code` command wasn't found. Install VS Code (on macOS, also run "
            "\"Shell Command: Install 'code' command in PATH\"), then rerun `ug configure --agents vscode`."
        )
        return False
    try:
        models = model_entries(state, provider=provider, location=location)
    except RuntimeError as exc:
        # Keep what an earlier configure installed: a failed listing shouldn't wipe a working setup.
        print_warning(f"{exc} VS Code was left unchanged.")
        return False
    config: dict[str, object] = {
        "workspace": state["workspace"],
        "authCommand": auth_command(state),
        "models": models,
    }
    if provider:
        config["provider"] = provider
    source = f" from {provider or location}" if provider or location else ""
    try:
        print_kv(
            "VS Code extension",
            f"{install_extension(code, config)} with {len(models)} models{source}",
        )
    except RuntimeError as exc:
        print_warning(f"{exc} Rerun `ug configure --agents vscode` to try again.")
        return False
    print_heading("Next, in VS Code:")
    for line in next_steps():
        console.print(f"  {line}", highlight=False)
    return True


def revert() -> str:
    """Uninstall the extension, and its config with it: "removed" or "unchanged"."""
    removed = False
    for command, folder in CODE_COMMANDS:
        # Look on disk first, so revert runs `code` only for people who set VS Code up.
        if not any(extensions_dir(folder).glob(f"{EXTENSION_ID}-*")):
            continue
        code = _find_command(command)
        # VS Code can leave an uninstalled extension's folder behind until it next starts.
        if code is None or installed_version(code) is None:
            continue
        result = _run_code(code, "--uninstall-extension", EXTENSION_ID)
        if result.returncode == 0:
            removed = True
        else:
            print_warning(
                f"Couldn't uninstall {EXTENSION_ID}: {(result.stderr or '').strip()[:300]}"
            )
    return "removed" if removed else "unchanged"
