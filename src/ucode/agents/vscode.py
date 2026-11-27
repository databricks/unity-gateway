"""VS Code: install ug's model extension so Copilot Chat can use the workspace's Unity Gateway models.

Configure-only, like Cursor (it stays out of `agents.__init__._MODULES`): `ug configure --agents
vscode` packs the extension that ships with ug into a vsix, together with a `ug-config.json` that
holds the workspace, its models and the auth command, and installs it with VS Code's `code` command.
The extension reads that file from its own folder and writes the user settings it needs itself.
`ug revert` removes those settings from the user's settings.json, which an uninstalled extension
can't do, then uninstalls the extension.

On a GitHub Copilot seat relay, ug itself points VS Code's Copilot runtime at a wrapper that runs it
through the relay: VS Code's settings API can't write that setting, and VS Code applies a change to
it without a restart.

The models are the Copilot CLI's, from one source: the catalog `ug configure` discovered for the
workspace, a Model Provider Service (`--provider`), or the model services of a Unity Catalog schema
(`--model-location`).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import tempfile
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from xml.sax.saxutils import escape

from ucode import config_io, jsonc
from ucode.agents.copilot import (
    RUNTIME_WRAPPER_PID_ENV,
    CopilotModel,
    catalog_model_limits,
    claude_model_name,
    copilot_catalog,
    location_catalog,
    managed_http_headers,
    model_labels,
    provider_service_catalog,
    resolve_copilot_model,
    unservable_static_models,
    uses_seat_relay,
)
from ucode.constants import LOOPBACK_HOST
from ucode.custom_oauth import CustomOAuthConfig, build_custom_auth_shell_command
from ucode.databricks import build_auth_shell_command, running_launcher, ug_binary, windows_ug_exe
from ucode.managed_files import OS, current_os
from ucode.os_compatibility import subprocess_cross_os
from ucode.os_compatibility.system_cross_os import windows_program_files_dir
from ucode.ui import print_kv, print_note, print_warning
from ucode.workspace import workspace_headers, workspace_id_args

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
    snap = "code-insiders" if insiders else "code"
    flatpak = "com.visualstudio.code-insiders" if insiders else "com.visualstudio.code"
    return [
        Path("/usr/bin") / command,
        Path("/usr/share") / snap / "bin" / command,
        Path("/snap") / snap / "current/usr/share" / snap / "bin" / command,
        *(
            root / "app" / flatpak / "current/active/files/extra/vscode/bin" / command
            for root in (Path("/var/lib/flatpak"), Path.home() / ".local/share/flatpak")
        ),
    ]


def _path_candidates(command: str) -> list[Path]:
    """Every `command` on PATH, in PATH order: the first one can be another product's."""
    names = [command]
    if current_os() is OS.WINDOWS:
        names = [f"{command}{ext}" for ext in (".cmd", ".exe", ".bat", "")]
    found: list[Path] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        for name in names:
            candidate = Path(entry) / name
            if candidate.is_file() and candidate not in found:
                found.append(candidate)
    return found


# The products whose `code` ug sets up; others answer to `code` too (VSCodium, Cursor, Code - OSS).
_DESKTOP_PRODUCTS = {"Code": False, "Code - Insiders": True}


@dataclass(frozen=True)
class VSCodeInstall:
    """A desktop VS Code install: its `code` command, app folder and parsed product.json."""

    code: Path
    app: Path
    product: dict

    @property
    def insiders(self) -> bool:
        return _DESKTOP_PRODUCTS.get(str(self.product.get("nameShort")), False)

    @property
    def version(self) -> str:
        return str(self.product.get("version") or "unknown version")

    def runtime(self) -> Path | None:
        return bundled_runtime(self.code)


def _app_dirs(code: Path) -> list[Path]:
    """The app folders `code` may belong to, most specific first."""
    bin_dir = code.resolve().parent
    # macOS keeps `code` in the app folder's bin; Linux packages keep the app under resources/app.
    apps = [bin_dir.parent, bin_dir.parent / "resources" / "app"]
    if current_os() is OS.WINDOWS:
        apps = [*_windows_app_dirs(bin_dir, code), *apps]
    return apps


def _product(app: Path) -> dict | None:
    try:
        product = json.loads((app / "product.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return product if isinstance(product, dict) else None


def _install_at(code: Path) -> VSCodeInstall | None:
    """The desktop VS Code `code` belongs to, or None when it is another product or no app."""
    for app in _app_dirs(code):
        if (product := _product(app)) is not None:
            if product.get("nameShort") in _DESKTOP_PRODUCTS:
                return VSCodeInstall(code, app, product)
            return None
    return None


def describe_command(code: Path) -> str:
    """What a `code` command that isn't the desktop VS Code is, for messages."""
    for app in _app_dirs(code):
        if (product := _product(app)) is not None:
            return str(product.get("nameLong") or product.get("nameShort") or "another product")
    resolved = str(code.resolve())
    if ".vscode-server" in resolved or "remote-cli" in resolved:
        return "VS Code Server's command"
    if not code.resolve().exists():
        return "a link to a VS Code that isn't there anymore"
    return "not a desktop VS Code install"


def _candidates(command: str) -> Iterable[Path]:
    yield from _path_candidates(command)
    yield from (path for path in _install_locations(command) if path.is_file())


def vscode_installs(command: str | None = None) -> list[VSCodeInstall]:
    """Every desktop VS Code install found, PATH first, each app folder once."""
    installs: list[VSCodeInstall] = []
    seen: set[Path] = set()
    for name, _ in CODE_COMMANDS:
        if command is not None and name != command:
            continue
        for candidate in _candidates(name):
            install = _install_at(candidate)
            if install is None or install.app.resolve() in seen:
                continue
            seen.add(install.app.resolve())
            installs.append(install)
    return installs


def first_code_on_path() -> Path | None:
    """What a shell would run for `code`, whatever product it is."""
    for name, _ in CODE_COMMANDS:
        if found := _path_candidates(name):
            return found[0]
    return None


def requested_install(path: str) -> VSCodeInstall:
    """The install a `--vscode-code` or `UG_VSCODE_CODE` override names; a bad one raises."""
    code = Path(path).expanduser()
    if not code.is_file():
        raise RuntimeError(f"VS Code's `code` command wasn't found at {code}.")
    install = _install_at(code)
    if install is None:
        raise RuntimeError(f"{code} is {describe_command(code)}, not the desktop VS Code's `code`.")
    return install


def find_install(*, prefer_runtime: bool = False) -> VSCodeInstall | None:
    """The desktop VS Code to set up: the `UG_VSCODE_CODE` override, else the first install found,
    or with ``prefer_runtime`` the first one with a bundled Copilot runtime."""
    if override := os.environ.get("UG_VSCODE_CODE"):
        return requested_install(override)
    installs = vscode_installs()
    if prefer_runtime:
        with_runtime = next((i for i in installs if i.runtime() is not None), None)
        if with_runtime is not None:
            return with_runtime
    return installs[0] if installs else None


def _find_command(command: str) -> Path | None:
    installs = vscode_installs(command)
    return installs[0].code if installs else None


def find_code() -> Path | None:
    """VS Code's `code` command, or Insiders' when only that is installed."""
    install = find_install()
    return install.code if install else None


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


def _model_entry(model: CopilotModel, name: str) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": model.id,
        "name": name,
        "family": claude_model_name(model.id),
        # The wire names of Copilot's catalog; the extension dispatches on them (see APIS in
        # vscode-extension/src/extension.ts).
        "api": model.wire,
    }
    # Models without known limits get the extension's 128K-in, 32K-out default.
    if limits := catalog_model_limits(model):
        entry["maxInputTokens"], entry["maxOutputTokens"] = limits
    return entry


def copilot_models(
    state: dict,
    provider: str | None = None,
    location: str | None = None,
    *,
    agent: str = "VS Code",
) -> list[CopilotModel]:
    """The models of a Model Provider Service (``provider``), a ``<catalog>.<schema>``
    (``location``) or the discovered catalog, as the Copilot CLI lists them; raises when there are
    none. VS Code and the GitHub Copilot app both use it; ``agent`` names which in errors."""
    if provider:
        return provider_service_catalog(state, provider, None, tool="vscode", agent=agent)
    if location:
        return location_catalog(state, location)
    catalog = copilot_catalog(state)
    if catalog:
        for model, reason in unservable_static_models(state):
            print_warning(f"Skipping {model} from your workspace's managed config: {reason}.")
    if not catalog:
        reason = (state.get("_discovery_reasons") or {}).get("copilot")
        raise RuntimeError(
            f"No Claude, GPT, GLM, Kimi or DeepSeek models for {agent} "
            f"({reason or 'the workspace has none'})."
        )
    return catalog


def model_entries(
    state: dict, *, provider: str | None = None, location: str | None = None
) -> list[dict[str, object]]:
    """The models of the chosen source, each with the API the extension calls it on."""
    return _entries(copilot_models(state, provider, location), provider)


def _entries(catalog: list[CopilotModel], provider: str | None) -> list[dict[str, object]]:
    labels = model_labels(catalog, provider_service=provider)
    return [_model_entry(model, labels[model]) for model in catalog]


def managed_default_model(state: dict, catalog: list[CopilotModel]) -> str | None:
    """The id in ``catalog`` of the workspace's managed default model, or None when there is none
    or ``catalog`` doesn't have it."""
    default = state.get("copilot_default_model")
    if not isinstance(default, str) or not default:
        return None
    try:
        return resolve_copilot_model(default, catalog).id
    except RuntimeError:
        return None


# The extension's trace receiver, kept across configures so VS Code's settings keep pointing at it.
OTEL_RECEIVER_STATE_KEY = "copilot_vscode_otel"


def otel_receiver(state: dict) -> dict[str, object]:
    """The loopback port and path secret of the extension's trace receiver, picked once."""
    saved = state.get(OTEL_RECEIVER_STATE_KEY)
    if (
        isinstance(saved, dict)
        and isinstance(saved.get("port"), int)
        and isinstance(saved.get("secret"), str)
    ):
        return {"port": saved["port"], "secret": saved["secret"]}
    with socket.socket() as probe:
        probe.bind((LOOPBACK_HOST, 0))
        port = probe.getsockname()[1]
    receiver: dict[str, object] = {"port": port, "secret": secrets.token_urlsafe(24)}
    state[OTEL_RECEIVER_STATE_KEY] = receiver
    return receiver


# What the extension writes to the user settings from ug-config.json; see otelSettings in
# vscode-extension/src/otel.ts and DEFAULT_MODEL_SETTING in vscode-extension/src/settings.ts.
_OTEL_SETTINGS = {
    "github.copilot.chat.otel": {
        "enabled": True,
        "exporterType": "otlp-http",
        "protocol": "http/protobuf",
    },
    "chat.agentHost.otel": {"enabled": True, "exporterType": "otlp-http"},
}
DEFAULT_MODEL_SETTING = "chat.defaultModel"
# The executable VS Code's agent host runs as its Copilot runtime, and ug's wrapper for it.
RUNTIME_PATH_SETTING = "chat.agentHost.copilot.runtimePath"
RUNTIME_WRAPPER_NAME = "ug-copilot-runtime"
_NODE_ARCHES = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}


def _runtime_in(app: Path) -> Path | None:
    """The Copilot runtime bundled under ``app`` for this OS. Any CPU architecture the install
    ships counts: VS Code's own architecture decides which prebuild it bundles (an Intel VS Code on
    an Apple Silicon Mac, a Universal build with both), and ug's Python may well differ."""
    system = {OS.MACOS: "darwin", OS.LINUX: "linux", OS.WINDOWS: "win32"}.get(current_os())
    if system is None:
        return None
    name = "copilot-runtime.exe" if system == "win32" else "copilot-runtime"
    sdks = app / "node_modules.asar.unpacked" / "@github"
    try:
        targets = sorted(
            entry.name.removeprefix("copilot-sdk-")
            for entry in sdks.iterdir()
            if entry.name.startswith(f"copilot-sdk-{system}-")
        )
    except OSError:
        return None
    own = f"{system}-{_NODE_ARCHES.get(platform.machine().lower(), '')}"
    for target in sorted(targets, key=lambda t: t != own):
        runtime = sdks / f"copilot-sdk-{target}" / "prebuilds" / target / name
        if runtime.is_file():
            return runtime
    return None


def bundled_runtime(code: Path) -> Path | None:
    """The Copilot runtime bundled with the VS Code install that ``code`` belongs to."""
    for app in _app_dirs(code):
        if (runtime := _runtime_in(app)) is not None:
            return runtime
    return None


# `code.cmd` runs `..\<version folder>\resources\app\out\cli.js`; the folder changes with each
# update, and an old one can outlive the update.
_WINDOWS_CLI_JS = re.compile(r"%~dp0\.\.\\([^\\\"%]+)\\resources\\app\\out\\cli\.js", re.IGNORECASE)


def _windows_app_dirs(bin_dir: Path, code: Path) -> list[Path]:
    """The app folder of the VS Code version ``code`` runs on Windows, read from its `.cmd`."""
    try:
        text = (bin_dir / f"{Path(code.name).stem}.cmd").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return []
    return [bin_dir.parent / match / "resources" / "app" for match in _WINDOWS_CLI_JS.findall(text)]


def _wrapper_dir(*, insiders: bool) -> Path:
    return config_io.APP_DIR / "vscode" / ("code-insiders" if insiders else "code")


def runtime_wrapper_path(*, insiders: bool, script: str) -> Path:
    """The wrapper for ``script``. Its name carries a hash of the script: VS Code restarts the
    runtime only when this setting changes, so a new script needs a new path."""
    digest = hashlib.sha256(script.encode()).hexdigest()[:12]
    # The Copilot SDK runs a `.js` runtime path with VS Code's own Node.
    suffix = ".js" if current_os() is OS.WINDOWS else ""
    return _wrapper_dir(insiders=insiders) / f"{RUNTIME_WRAPPER_NAME}-{digest}{suffix}"


def _is_runtime_wrapper(value: object, *, insiders: bool) -> bool:
    if not isinstance(value, str):
        return False
    path = Path(value)
    if path.parent != _wrapper_dir(insiders=insiders):
        return False
    # Earlier builds named the wrapper without the script's hash.
    return path.name == RUNTIME_WRAPPER_NAME or path.name.startswith(f"{RUNTIME_WRAPPER_NAME}-")


def _runtime_wrappers(*, insiders: bool) -> list[Path]:
    return sorted(
        path
        for path in _wrapper_dir(insiders=insiders).glob(f"{RUNTIME_WRAPPER_NAME}*")
        if _is_runtime_wrapper(str(path), insiders=insiders)
    )


def wrapper_script(command: list[str], runtime: Path, code: Path | None = None) -> str:
    """A script that runs ``command`` on ``runtime`` with the arguments VS Code passes: a POSIX
    shell script, or on Windows, where VS Code can only start an executable or a `.js` file, a
    Node script. ``code`` lets each start look the runtime up again: a VS Code update moves it on
    Windows, where ``runtime`` is then only the fallback."""
    argv = [*command, "--runtime", str(runtime), *(["--code", str(code)] if code else []), "--"]
    if current_os() is OS.WINDOWS:
        return _node_wrapper_script(argv)
    parts = " ".join(shlex.quote(part) for part in argv)
    return f'#!/bin/sh\nexec {parts} "$@"\n'


def _node_wrapper_script(argv: list[str]) -> str:
    command, *args = argv
    hint = f"Couldn't start {command}. Rerun `ug configure --agents copilot`."
    return f"""// Written by `ug configure`: runs VS Code's Copilot runtime through ug's seat relay.
"use strict";
const {{ spawn }} = require("node:child_process");
const child = spawn({json.dumps(command)}, [...{json.dumps(args)}, ...process.argv.slice(2)], {{
  env: {{ ...process.env, {RUNTIME_WRAPPER_PID_ENV}: String(process.pid) }},
  stdio: "inherit",
  windowsHide: true,
}});
child.on("error", (error) => {{
  process.stderr.write("[ucode] " + {json.dumps(hint)} + " " + error.message + "\\n");
  process.exit(1);
}});
child.on("exit", (code) => process.exit(code ?? 1));
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => child.kill(signal));
"""


def _read_runtime_path(settings: Path) -> object:
    try:
        return jsonc.get_member(settings.read_text(encoding="utf-8"), RUNTIME_PATH_SETTING)
    except FileNotFoundError:
        return None


def _edit_user_settings(path: Path, edit: Callable[[str], str]) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    edited = edit(text)
    if edited == text:
        return
    # Edit the file a symlink points to, and keep its permissions, as the rewrite would lose both.
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.ug-{secrets.token_hex(4)}")
    try:
        partial.write_text(edited, encoding="utf-8")
        if target.exists():
            shutil.copymode(target, partial)
        partial.replace(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def resolve_runtime(runtime: str, code: str | None) -> str:
    """The Copilot runtime to run now: the one bundled with the VS Code install ``code`` belongs
    to, which an update may have moved since configure, else the recorded ``runtime``."""
    if code:
        current = bundled_runtime(Path(code))
        if current is not None:
            return str(current)
    return runtime


def route_through_seat_relay(state: dict, provider: str, code: Path) -> str | None:
    """Point the Copilot runtime of the VS Code install ``code`` belongs to at ug's wrapper, which
    runs it through ``provider``'s seat relay. Returns why it couldn't, or None."""
    runtime = bundled_runtime(code)
    if runtime is None:
        return (
            f"the VS Code install of {code} has no bundled Copilot runtime ug can run through it."
        )
    insiders = code.name.startswith("code-insiders")
    settings = user_dir(insiders) / "settings.json"
    command = seat_relay_command(state, provider)
    if current_os() is OS.WINDOWS:
        if (ug := windows_ug_exe(command[0], running_launcher())) is None:
            return (
                "ug's `ug.exe` wasn't found, and VS Code can only start an `.exe` without a "
                "shell. Reinstall ug, then rerun `ug configure --agents copilot`."
            )
        command = [ug, *command[1:]]
    script = wrapper_script(command, runtime, code)
    wrapper = runtime_wrapper_path(insiders=insiders, script=script)
    try:
        current = _read_runtime_path(settings)
        if (
            current is not None
            and current != str(wrapper)
            and not _is_runtime_wrapper(current, insiders=insiders)
        ):
            return (
                f"{RUNTIME_PATH_SETTING} in {settings} is already set to {current}, which ug "
                f"doesn't replace. Remove that setting to route Copilot through {provider}."
            )
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text(script)
        wrapper.chmod(0o700)
        _edit_user_settings(
            settings, lambda text: jsonc.set_member(text, RUNTIME_PATH_SETTING, str(wrapper))
        )
    except (OSError, ValueError) as exc:
        return f"Couldn't set {RUNTIME_PATH_SETTING} in {settings}: {exc}."
    for stale in _runtime_wrappers(insiders=insiders):
        if stale != wrapper:
            stale.unlink(missing_ok=True)
    return None


def remove_seat_relay_route(*, insiders: bool) -> bool:
    """Remove ug's Copilot runtime wrappers, and the setting while it still points at one. Returns
    whether anything was removed. The wrappers stay when the setting couldn't be edited, since
    VS Code may still run one."""
    settings = user_dir(insiders) / "settings.json"
    removed = False
    try:
        if _is_runtime_wrapper(_read_runtime_path(settings), insiders=insiders):
            _edit_user_settings(
                settings, lambda text: jsonc.remove_member(text, RUNTIME_PATH_SETTING)
            )
            removed = True
    except (OSError, ValueError) as exc:
        print_warning(
            f"Couldn't edit {settings}: {exc}. Remove {RUNTIME_PATH_SETTING} if it still points "
            f"to ug's runtime wrapper in {_wrapper_dir(insiders=insiders)}."
        )
        return removed
    for wrapper in _runtime_wrappers(insiders=insiders):
        try:
            wrapper.unlink()
        except OSError as exc:
            print_warning(f"Couldn't remove {wrapper}: {exc}.")
            continue
        removed = True
    return removed


def seat_relay_command(state: dict, provider: str) -> list[str]:
    """The ug command ug's wrapper runs in place of VS Code's Copilot runtime."""
    profile = state.get("profile")
    return [
        ug_binary(),
        "copilot-seat-runtime",
        "--provider",
        provider,
        "--host",
        state["workspace"],
        *(["--profile", profile] if profile else []),
        *workspace_id_args(state["workspace"]),
    ]


def written_settings(config: dict) -> list[dict[str, object]]:
    """The groups of user settings the extension writes from ``config``. ug removes a group only
    while the first setting in it still holds ug's value."""
    groups: list[dict[str, object]] = []
    otel = config.get("otel")
    if isinstance(otel, dict) and "port" in otel and "secret" in otel:
        endpoint = f"http://{LOOPBACK_HOST}:{otel['port']}/{otel['secret']}"
        for prefix, values in _OTEL_SETTINGS.items():
            # The endpoint first: it names ug's receiver, unlike the other values.
            group: dict[str, object] = {f"{prefix}.otlpEndpoint": endpoint}
            group.update({f"{prefix}.{key}": value for key, value in values.items()})
            groups.append(group)
    default_model = config.get("defaultModel")
    if isinstance(default_model, str) and default_model:
        groups.append({DEFAULT_MODEL_SETTING: default_model})
    return groups


def user_dir(insiders: bool) -> Path:
    """VS Code's user folder, which holds the user settings.json and one per profile."""
    name = "Code - Insiders" if insiders else "Code"
    system = current_os()
    if system is OS.MACOS:
        return Path.home() / "Library" / "Application Support" / name / "User"
    if system is OS.WINDOWS:
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / name / "User"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / name / "User"


def _installed_config(folder: str, version: str) -> dict:
    path = extensions_dir(folder) / f"{EXTENSION_ID}-{version}" / CONFIG_FILE
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def remove_written_settings(user: Path, config: dict) -> None:
    """Remove from each settings.json under ``user`` the values the extension wrote from
    ``config`` that the developer hasn't changed since."""
    groups = written_settings(config)
    if not groups:
        return
    for path in [user / "settings.json", *sorted(user.glob("profiles/*/settings.json"))]:
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            # Comments or trailing commas, which a rewrite would drop.
            ours = [key for group in groups for key in group]
            print_warning(
                f"Couldn't edit {path}. Remove the settings ug's VS Code extension wrote there "
                f"if they're still set: {', '.join(ours)}."
            )
            continue
        if not isinstance(settings, dict):
            continue
        stale = [
            key
            for group in groups
            if settings.get(next(iter(group))) == next(iter(group.values()))
            for key, value in group.items()
            if key in settings and settings[key] == value
        ]
        if not stale:
            continue
        for key in stale:
            del settings[key]
        try:
            # VS Code writes settings.json with 4-space indents.
            path.write_text(json.dumps(settings, indent=4) + "\n", encoding="utf-8")
        except OSError as exc:
            print_warning(f"Couldn't edit {path}: {exc}. Remove {', '.join(stale)} yourself.")


def auth_command(state: dict) -> str:
    workspace = state["workspace"]
    custom_oauth = state.get("custom_oauth")
    if isinstance(custom_oauth, dict):
        return build_custom_auth_shell_command(workspace, cast(CustomOAuthConfig, custom_oauth))
    return build_auth_shell_command(
        workspace, state.get("profile"), use_pat=bool(state.get("use_pat"))
    )


def next_steps(*, seat_relay: bool = False) -> str:
    """What to do in VS Code once the extension is installed, for this OS."""
    if seat_relay:
        # VS Code restarts its Copilot runtime itself when the setting changes.
        return (
            "In VS Code: open Chat, sign in to GitHub Copilot, then start a chat in a Copilot "
            "session (not Local) and pick a model."
        )
    restart = (
        # Closing every window leaves VS Code running on macOS.
        "quit VS Code with Cmd+Q, even if no window is open, and open it again"
        if current_os() is OS.MACOS
        else "close every VS Code window and open VS Code again"
    )
    # A running VS Code keeps the extension build it loaded, even in a new window, and configure
    # reinstalls every time.
    return f"In VS Code: {restart}, then open Chat, click the model picker and choose a Databricks model."


# `ug configure --vscode-code <path>` for this run; `UG_VSCODE_CODE` is the same for every run.
_requested_code: str | None = None


def request_code(path: str | None) -> None:
    global _requested_code
    _requested_code = path


def _install_to_configure(seat_relay: bool) -> VSCodeInstall | None:
    """The desktop VS Code configure sets up, with one note when it isn't what `code` on PATH runs.
    A bad override raises. None, after a note, when there is no desktop VS Code."""
    if _requested_code:
        return requested_install(_requested_code)
    install = find_install(prefer_runtime=seat_relay)
    on_path = first_code_on_path()
    if install is None:
        if on_path is None:
            print_note(
                "VS Code's `code` command wasn't found, so VS Code was skipped. To add it, install "
                "VS Code (on macOS, also run \"Shell Command: Install 'code' command in PATH\"), "
                "then rerun `ug configure --agents copilot`."
            )
        else:
            print_note(
                f"`code` on PATH ({on_path}) is {describe_command(on_path)}, and no desktop VS Code "
                "was found in the usual places, so VS Code was skipped. Pass its `code` command "
                "with `ug configure --agents copilot --vscode-code <path>`."
            )
        return None
    if on_path is not None and on_path.resolve() != install.code.resolve():
        print_note(
            f"Using VS Code at {install.app}; `code` on PATH ({on_path}) is {describe_command(on_path)}."
        )
    return install


def configure(state: dict, *, provider: str | None = None, location: str | None = None) -> bool:
    """Install the extension with this workspace's setup and the models of ``provider`` (a Model
    Provider Service), ``location`` (a ``<catalog>.<schema>``) or else the Copilot CLI's, with the
    workspace's managed headers, its managed default model and its selection on a host that serves
    several workspaces, and when the workspace traces Copilot, the trace receiver. Returns False, after a warning, if any
    step fails."""
    try:
        seat_relay = bool(provider) and uses_seat_relay(state, cast(str, provider), tool="vscode")
        install = _install_to_configure(seat_relay)
    except RuntimeError as exc:
        print_warning(f"{exc} VS Code was left unchanged.")
        return False
    if install is None:
        return False
    code = install.code
    try:
        # A seat relay serves Copilot's own models, so the extension lists none of Databricks'.
        catalog = [] if seat_relay else copilot_models(state, provider, location)
    except RuntimeError as exc:
        # Keep what an earlier configure installed: a failed listing shouldn't wipe a working setup.
        print_warning(f"{exc} VS Code was left unchanged.")
        return False
    models = _entries(catalog, provider)
    config: dict[str, object] = {
        "workspace": state["workspace"],
        "authCommand": auth_command(state),
        "models": models,
    }
    if provider:
        config["provider"] = provider
    if seat_relay and install.runtime() is None:
        print_warning(
            f"VS Code {install.version} at {install.app} has no bundled Copilot runtime ug can run "
            "through the seat relay, so VS Code was left unchanged. Update VS Code, then rerun "
            "`ug configure --agents copilot`."
        )
        return False
    # The extension makes it VS Code's default for new chats, unless the developer set their own.
    if default_model := managed_default_model(state, catalog):
        config["defaultModel"] = default_model
    headers = managed_http_headers(state)
    # Last, so an admin header can't select another workspace.
    headers.update(workspace_headers(state["workspace"]))
    if headers:
        config["headers"] = headers
    if state.get("copilot_otel_tracing"):
        config["otel"] = otel_receiver(state)
    source = f" from {provider or location}" if provider or location else ""
    # Routed before the reinstall, which drops the extension's models: a failed route must not leave
    # the old setup gone with nothing on the relay.
    if seat_relay:
        if error := route_through_seat_relay(state, cast(str, provider), code):
            print_warning(f"GitHub Copilot in VS Code doesn't go through {provider}: {error}")
            return False
    try:
        installed = install_extension(code, config)
    except RuntimeError as exc:
        print_warning(f"{exc} Rerun `ug configure --agents copilot` to try again.")
        return False
    if not seat_relay:
        remove_seat_relay_route(insiders=code.name.startswith("code-insiders"))
        print_kv("VS Code extension", f"{installed} with {len(models)} models{source}")
    else:
        print_kv("VS Code extension", f"{installed}, routing Copilot through {provider}")
    print_note(next_steps(seat_relay=seat_relay))
    # Revert works on the same install, whatever `code` is on PATH by then.
    state[CODE_PATH_STATE_KEY] = str(code)
    return True


# The `code` configure set up, saved in the workspace state for revert.
CODE_PATH_STATE_KEY = "vscode_code"


def _recorded_code(state: dict | None, command: str) -> Path | None:
    """The `code` the last configure used, while it is still that desktop VS Code."""
    recorded = (state or {}).get(CODE_PATH_STATE_KEY)
    if not isinstance(recorded, str) or Path(recorded).stem != command:
        return None
    code = Path(recorded)
    return code if code.is_file() and _install_at(code) is not None else None


def revert(state: dict | None = None) -> str:
    """Remove the user settings the extension wrote, then uninstall it and its config with it:
    "removed" or "unchanged". ``state`` names the `code` configure used, when it is known."""
    removed = False
    for command, folder in CODE_COMMANDS:
        removed = remove_seat_relay_route(insiders=command == "code-insiders") or removed
        # Look on disk first, so revert runs `code` only for people who set VS Code up.
        if not any(extensions_dir(folder).glob(f"{EXTENSION_ID}-*")):
            continue
        code = _recorded_code(state, command) or _find_command(command)
        # VS Code can leave an uninstalled extension's folder behind until it next starts.
        if code is None or (version := installed_version(code)) is None:
            continue
        remove_written_settings(
            user_dir(insiders=command == "code-insiders"), _installed_config(folder, version)
        )
        result = _run_code(code, "--uninstall-extension", EXTENSION_ID)
        if result.returncode == 0:
            removed = True
        else:
            print_warning(
                f"Couldn't uninstall {EXTENSION_ID}: {(result.stderr or '').strip()[:300]}"
            )
    return "removed" if removed else "unchanged"
