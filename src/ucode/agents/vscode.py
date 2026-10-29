"""VS Code: install ug's model extension so Copilot Chat can use the workspace's AI Gateway models.

Configure-only, like Cursor (it stays out of `agents.__init__._MODULES`): `ug configure --agents
vscode` packs the extension that ships with ug into a vsix, installs it with VS Code's `code`
command, and writes the extension's settings into the user settings.json. `ug revert` undoes both.
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

from ucode import config_io
from ucode.agents.copilot import (
    copilot_exclusion_reason,
    copilot_model_limits,
    uses_responses_api,
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
WORKSPACE_SETTING = "unityGateway.workspace"
MODELS_SETTING = "unityGateway.models"
AUTH_COMMAND_SETTING = "unityGateway.authCommand"
SETTINGS = (WORKSPACE_SETTING, MODELS_SETTING, AUTH_COMMAND_SETTING)
# Each `code` command and the folder its user settings live in.
CODE_COMMANDS = (("code", "Code"), ("code-insiders", "Code - Insiders"))
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


def find_code() -> tuple[Path, str] | None:
    """VS Code's `code` command (or Insiders') and its settings folder name, or None."""
    for command, product in CODE_COMMANDS:
        if found := shutil.which(command):
            return Path(found), product
    for command, product in CODE_COMMANDS:
        for path in _install_locations(command):
            if path.is_file():
                return path, product
    return None


def settings_path(product: str) -> Path:
    system = current_os()
    if system is OS.MACOS:
        base = Path.home() / "Library" / "Application Support"
    elif system is OS.WINDOWS:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / product / "User" / "settings.json"


def _package() -> dict:
    return json.loads((EXTENSION_DIR / "package.json").read_text(encoding="utf-8"))


def pack_vsix(directory: Path) -> Path:
    """Zip the bundled extension into a vsix in ``directory``, with no Node or `vsce` needed."""
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


def install_extension(code: Path) -> str:
    """Install the bundled extension over whatever is installed; returns what happened."""
    # Always reinstall: a ug upgrade can change the bundle without changing its version.
    version = _package()["version"]
    with tempfile.TemporaryDirectory(prefix="ug-vscode-") as directory:
        vsix = pack_vsix(Path(directory))
        result = _run_code(code, "--install-extension", str(vsix), "--force")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise RuntimeError(f"`{code} --install-extension` failed: {detail}")
    return f"installed {EXTENSION_ID} {version}"


def _model_entry(model: str, name: str) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": model,
        "name": name,
        "api": "responses" if uses_responses_api(model) else "chat_completions",
    }
    # Claude's limits hold on Chat Completions too. Models without known limits get the
    # extension's 128K-in, 32K-out default.
    if limits := copilot_model_limits(model):
        entry["maxInputTokens"], entry["maxOutputTokens"] = limits
    return entry


def model_settings(workspace: str, token: str) -> tuple[list[dict[str, object]], str | None]:
    """The workspace's Claude and GPT models, each with the API the extension calls it on."""
    # The models Copilot CLI offers: Gemini and open models, gpt-oss among them, fail on Copilot's
    # requests or aren't served on every workspace.
    claude, gpt, _gemini, _oss, reason = discover_model_services(workspace, token)
    models = [
        model
        for model in dict.fromkeys([*claude.values(), *gpt])
        if copilot_exclusion_reason(model) is None
    ]
    labels = {model: model_picker_label(model.rsplit(".", 1)[-1]) for model in models}
    counts = Counter(labels.values())
    # As in Codex's picker: two models with the same label show their full ids instead.
    return [
        _model_entry(model, model if counts[label] > 1 else label)
        for model, label in labels.items()
    ], reason


def auth_command(state: dict) -> str:
    workspace = state["workspace"]
    custom_oauth = state.get("custom_oauth")
    if isinstance(custom_oauth, dict):
        return build_custom_auth_shell_command(workspace, cast(CustomOAuthConfig, custom_oauth))
    return build_auth_shell_command(
        workspace, state.get("profile"), use_pat=bool(state.get("use_pat"))
    )


def _edit_settings(path: Path, values: dict[str, object], remove: tuple[str, ...] = ()) -> bool:
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        updated = config_io.update_jsonc_keys(text, values, remove)
        if updated != text:
            config_io.write_text_file(path, updated)
    except (OSError, RuntimeError, ValueError) as exc:
        print_warning(f"Couldn't update {path}: {exc}. Fix the file, then rerun this command.")
        return False
    return True


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


def configure(state: dict) -> bool:
    """Write the extension's settings and install it. Returns False, after a warning, when VS
    Code isn't found or the settings can't be updated."""
    found = find_code()
    if found is None:
        print_warning(
            "VS Code's `code` command wasn't found. Install VS Code (on macOS, also run "
            "\"Shell Command: Install 'code' command in PATH\"), then rerun `ug configure --agents vscode`."
        )
        return False
    code, product = found
    workspace = state["workspace"]
    models, reason = model_settings(workspace, launch_token(state, workspace))
    if not models:
        # Keep what an earlier configure wrote: a failed listing shouldn't wipe a working setup.
        print_warning(
            f"No Claude or GPT models for VS Code ({reason or 'the workspace has none'}). "
            "VS Code was left unchanged."
        )
        return False
    path = settings_path(product)
    values = {
        WORKSPACE_SETTING: workspace,
        MODELS_SETTING: models,
        AUTH_COMMAND_SETTING: auth_command(state),
    }
    if not _edit_settings(path, values):
        return False
    print_kv("VS Code settings", f"{len(models)} models in {path}")
    try:
        print_kv("VS Code extension", install_extension(code))
    except RuntimeError as exc:
        print_warning(
            f"{exc} Install it from VS Code's Extensions view, or rerun `ug configure --agents vscode`."
        )
        return False
    print_heading("Next, in VS Code:")
    for line in next_steps():
        console.print(f"  {line}", highlight=False)
    return True


def revert() -> str:
    """Remove ug's settings and uninstall the extension: "removed" or "unchanged"."""
    changed = configured = False
    for _, product in CODE_COMMANDS:
        path = settings_path(product)
        if not path.exists():
            continue
        try:
            current = config_io.loads_jsonc(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print_warning(f"Couldn't read {path}; remove its unityGateway settings by hand.")
            continue
        if isinstance(current, dict) and any(key in current for key in SETTINGS):
            configured = True
            changed = _edit_settings(path, {}, SETTINGS) or changed
    # Configure writes the settings before it installs, so without them there's nothing to
    # uninstall, and revert doesn't run `code` for people who never set up VS Code.
    found = find_code() if configured else None
    if found is not None and installed_version(found[0]) is not None:
        result = _run_code(found[0], "--uninstall-extension", EXTENSION_ID)
        if result.returncode == 0:
            changed = True
        else:
            print_warning(
                f"Couldn't uninstall {EXTENSION_ID}: {(result.stderr or '').strip()[:300]}"
            )
    return "removed" if changed else "unchanged"
