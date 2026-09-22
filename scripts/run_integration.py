"""Install a reproducible ug/agent combination and run black-box integration tests.

Uses fresh virtualenvs and an isolated npm prefix, never the checkout's uv.lock
or the developer's installed agents. Only the live workspace is shared with e2e.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_PACKAGES = {"claude": "@anthropic-ai/claude-code", "codex": "@openai/codex"}
WINDOWS_PATHEXT = ".COM;.EXE;.BAT;.CMD"
UV_INDEX_CREDENTIAL_ENV = (
    "UV_INDEX_DATABRICKS_PYPI_USERNAME",
    "UV_INDEX_DATABRICKS_PYPI_PASSWORD",
)
NPM_TOKEN_ENV = "UG_INTEGRATION_NPM_TOKEN"
INSTALLER_CREDENTIAL_ENV = (*UV_INDEX_CREDENTIAL_ENV, NPM_TOKEN_ENV)
HEADLESS_TEST_NODES = {
    "claude": "test_ug_claude_headless.py::test_ug_claude_headless_prompt_argument",
    "codex": "test_ug_codex_headless.py::test_ug_codex_headless_prompt_argument",
}


def installer_environment(
    base_environment: Mapping[str, str],
    source_environment: Mapping[str, str],
    credential_keys: Iterable[str],
    npm_user_config: Path | None = None,
) -> dict[str, str]:
    """Add only supported installer credentials to an isolated environment."""
    environment = dict(base_environment)
    for key in credential_keys:
        if value := source_environment.get(key):
            environment[key] = value
    if npm_user_config is not None:
        environment["npm_config_userconfig"] = str(npm_user_config)
    return environment


def npm_user_config(registry: str) -> str:
    """Configure npm auth through an environment reference, never a raw token."""
    parsed = urllib.parse.urlsplit(registry)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("The npm registry must be an absolute HTTP(S) URL.")
    if parsed.username or parsed.password:
        raise ValueError("The npm registry URL must not contain credentials.")
    registry = registry.rstrip("/") + "/"
    auth_path = parsed.path.rstrip("/") + "/"
    return (
        f"registry={registry}\n"
        f"//{parsed.netloc}{auth_path}:_authToken=${{{NPM_TOKEN_ENV}}}\n"
        "always-auth=true\n"
    )


def redact_secrets(value: str, secrets: Iterable[str]) -> str:
    for secret in sorted({secret for secret in secrets if secret}, key=len, reverse=True):
        value = value.replace(secret, "<redacted>")
    return value


def managed_policy_paths(
    agents: Iterable[str],
    environment: Mapping[str, str] | None = None,
    *,
    platform_name: str | None = None,
    system_platform: str | None = None,
) -> tuple[Path, ...]:
    """Return machine-wide agent settings that can change a live test."""
    environment = os.environ if environment is None else environment
    platform_name = os.name if platform_name is None else platform_name
    system_platform = sys.platform if system_platform is None else system_platform
    selected = set(agents)
    paths: list[Path] = []
    if platform_name == "nt":
        if "codex" in selected:
            program_data = environment.get("PROGRAMDATA", "").strip()
            if not program_data:
                raise RuntimeError(
                    "Cannot verify Codex system policy because PROGRAMDATA is unavailable."
                )
            codex_system = Path(program_data) / "OpenAI" / "Codex"
            paths.extend([codex_system / "requirements.toml", codex_system / "config.toml"])
        if "claude" in selected:
            program_files = environment.get("PROGRAMFILES", "").strip()
            if not program_files:
                raise RuntimeError(
                    "Cannot verify Claude managed settings because PROGRAMFILES is unavailable."
                )
            paths.append(Path(program_files) / "ClaudeCode" / "managed-settings.json")
    elif platform_name == "posix":
        if "codex" in selected:
            paths.extend(
                [Path("/etc/codex/managed_config.toml"), Path("/etc/codex/requirements.toml")]
            )
        if "claude" in selected:
            paths.append(
                Path(
                    "/Library/Application Support/ClaudeCode/managed-settings.json"
                    if system_platform == "darwin"
                    else "/etc/claude-code/managed-settings.json"
                )
            )
    return tuple(paths)


def present_policy_paths(paths: Iterable[Path]) -> list[str]:
    """Find policy files, failing closed when a path cannot be inspected."""
    present = []
    for path in paths:
        try:
            path.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(
                f"Cannot inspect machine-wide agent settings at {path}: {exc}"
            ) from exc
        present.append(str(path))
    return present


def integration_test_targets(
    suite: Path,
    agents: Iterable[str],
    *,
    platform_name: str,
    installation_only: bool,
    headless_only: bool,
) -> list[str]:
    if headless_only:
        targets = []
        for agent in agents:
            module, test = HEADLESS_TEST_NODES[agent].split("::", 1)
            targets.append(f"{suite / module}::{test}")
        return targets
    if platform_name == "nt" and installation_only:
        return [str(suite / "test_installation.py")]
    return [str(suite)]


def process_group_options() -> dict:
    if os.name == "posix":
        return {"start_new_session": True}
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    raise RuntimeError(f"Unsupported process platform: {os.name}")


def venv_executable(environment: Path, name: str) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / f"{name}.exe"
    return environment / "bin" / name


def npm_executable(bin_dir: Path, name: str) -> Path:
    return bin_dir / (f"{name}.cmd" if os.name == "nt" else name)


def mint_m2m_token(workspace: str, client_id: str, client_secret: str) -> str:
    """Mint a short-lived workspace token for a service principal via OAuth client credentials.

    The managed e2e workspace authenticates as a service principal, whose M2M tokens expire
    hourly, so CI mints one per run from `DATABRICKS_CLIENT_ID`/`DATABRICKS_CLIENT_SECRET` rather
    than storing a long-lived bearer.
    """
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode(
        {"grant_type": "client_credentials", "scope": "all-apis"}
    ).encode()
    request = urllib.request.Request(
        f"{workspace.rstrip('/')}/oidc/v1/token",
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (https workspace URL)
        token = json.load(response).get("access_token", "")
    if not token:
        raise RuntimeError("Service-principal client credentials returned no access token.")
    return token


@contextlib.contextmanager
def managed_process(command, *, interrupt=False, **kwargs):
    """Bound child lifetimes, including descendants that outlive their parent."""
    proc = subprocess.Popen(command, **process_group_options(), **kwargs)
    try:
        yield proc
    finally:
        if os.name == "posix":
            # Give pytest a KeyboardInterrupt so its fixtures can clean up the
            # separate process groups used by agent commands before pytest exits.
            first_signal = signal.SIGINT if interrupt else signal.SIGTERM
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, first_signal)
            try:
                proc.wait(timeout=15 if interrupt else 5)
            except subprocess.TimeoutExpired:
                pass
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        elif os.name == "nt":
            if proc.poll() is None and interrupt:
                # CREATE_NEW_PROCESS_GROUP lets pytest and its children receive
                # Ctrl+Break and unwind fixtures before forced tree cleanup.
                with contextlib.suppress(OSError):
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
            if proc.poll() is None:
                # taskkill /T handles descendants; proc.kill() only handles the
                # direct child on Windows.
                subprocess.run(
                    [shutil.which("taskkill") or "taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            proc.wait(timeout=5)


def exact_npm_version(value: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", value):
        raise argparse.ArgumentTypeError(
            "Use an exact version, for example 2.1.268; not latest/^/~."
        )
    return value


def arguments(
    argv: list[str] | None = None,
    *,
    platform_name: str | None = None,
    environment: Mapping[str, str] | None = None,
):
    platform_name = os.name if platform_name is None else platform_name
    environment = os.environ if environment is None else environment
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--ug-version", default="checkout", help="Exact ug release, or checkout.")
    source.add_argument(
        "--ug-wheel", type=Path, help="Previously built wheel to reproduce a release."
    )
    parser.add_argument("--entry-point", choices=["ug", "ucode"], default="ug")
    parser.add_argument("--claude-version", type=exact_npm_version)
    parser.add_argument("--codex-version", type=exact_npm_version)
    parser.add_argument("--claude-model", default=environment.get("UG_INTEGRATION_CLAUDE_MODEL"))
    parser.add_argument("--codex-model", default=environment.get("UG_INTEGRATION_CODEX_MODEL"))
    parser.add_argument(
        "--claude-provider",
        default="main.ucode.ci_e2e_anthropic_nonrelay_mps",
        help="Existing Anthropic MPS selected in the configure CUJ.",
    )
    parser.add_argument(
        "--claude-relayed-provider",
        default="main.ucode.ci_e2e_anthropic_relay_mps",
        help="Existing relayed (subscription-relay) Anthropic MPS for the hybrid-routing CUJ.",
    )
    parser.add_argument(
        "--claude-provider-model",
        default="claude-haiku-4-5-20251001",
        help="Only model exposed by the Anthropic MPS discovery fixture.",
    )
    parser.add_argument(
        "--codex-provider",
        default="main.ucode.ci_openai_mps",
        help="Existing OpenAI MPS selected in the configure CUJ.",
    )
    parser.add_argument(
        "--codex-provider-model",
        default="gpt-5-nano",
        help="Model allowed by the OpenAI MPS selected in the configure CUJ.",
    )
    parser.add_argument(
        "--parent-schema",
        default="main.ucode",
        help="Schema containing the dedicated model-discovery Model Services.",
    )
    parser.add_argument(
        "--claude-parent-model",
        default="main.ucode.ci_e2e_claude",
        help="Claude-compatible Model Service in --parent-schema.",
    )
    parser.add_argument(
        "--codex-parent-model",
        default="main.ucode.ci_e2e_codex",
        help="Codex-compatible Model Service in --parent-schema.",
    )
    parser.add_argument("--python", default=sys.executable, help="Python 3.12+ path or uv version.")
    parser.add_argument("--dependency", action="append", default=[], metavar="PACKAGE==VERSION")
    parser.add_argument("--constraints", type=Path, help="Replay a previous dependencies.txt.")
    parser.add_argument(
        "--npm-lock", type=Path, help="Replay a previous npm-lock.json with npm ci."
    )
    parser.add_argument(
        "--default-index", default=environment.get("UV_DEFAULT_INDEX", "https://pypi.org/simple")
    )
    parser.add_argument("--npm-registry", default="https://registry.npmjs.org")
    parser.add_argument("--profile", help="Explicit Databricks profile to mint the live bearer.")
    parser.add_argument("--workspace", default=environment.get("UCODE_TEST_WORKSPACE"))
    parser.add_argument(
        "--second-workspace",
        default=environment.get("UCODE_TEST_SECOND_WORKSPACE"),
        help="Second real workspace for workspace_switch CUJs; requires DATABRICKS_SECOND_BEARER.",
    )
    parser.add_argument("--output", type=Path, help="New results directory; never reused.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--installation-only", action="store_true", help="No workspace calls.")
    mode.add_argument(
        "--headless-only",
        action="store_true",
        help="Run one real noninteractive prompt journey for each selected agent.",
    )
    parser.add_argument(
        "pytest_args", nargs=argparse.REMAINDER, help="After --, pass pytest filters."
    )
    args = parser.parse_args(argv)
    if platform_name != "posix" and not (args.installation_only or args.headless_only):
        parser.error(
            "Live agent/TUI integration requires POSIX PTY, managed-settings, and signal "
            "support. Use --installation-only or --headless-only on Windows."
        )
    # Only selection/early-stop controls are accepted. Pytest configuration,
    # plugins and report destinations are part of the suite's isolation contract.
    filters = argparse.ArgumentParser(add_help=False)
    filters.add_argument("-k")
    filters.add_argument("-m")
    filters.add_argument("-x", action="store_true")
    filters.add_argument("--maxfail", type=int)
    extra = args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args
    selected = filters.parse_args(extra)
    marker = selected.m or "live"
    if args.installation_only:
        marker = f"installation and ({selected.m})" if selected.m else "installation"
    args.pytest_args = []
    for flag, value in (("-k", selected.k), ("-m", marker), ("--maxfail", selected.maxfail)):
        if value is not None:
            args.pytest_args.extend([flag, str(value)])
    if selected.x:
        args.pytest_args.append("-x")
    if not (args.claude_version or args.codex_version):
        parser.error("Select --claude-version and/or --codex-version explicitly.")
    if args.ug_version != "checkout" and not re.fullmatch(
        r"[0-9][0-9A-Za-z.!+_-]*", args.ug_version
    ):
        parser.error(
            "--ug-version must be an exact release, or checkout; use --ug-wheel for a file."
        )
    for dependency in args.dependency:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.!+-]+", dependency):
            parser.error("--dependency requires an exact PACKAGE==VERSION constraint.")
    if not args.installation_only:
        if not args.workspace or not args.workspace.startswith("https://"):
            parser.error(
                "Set UCODE_TEST_WORKSPACE to the existing e2e workspace, or use --workspace."
            )
        has_client_creds = bool(
            environment.get("DATABRICKS_CLIENT_ID", "").strip()
            and environment.get("DATABRICKS_CLIENT_SECRET", "").strip()
        )
        if not (
            args.profile or environment.get("DATABRICKS_BEARER", "").strip() or has_client_creds
        ):
            parser.error(
                "Provide the e2e DATABRICKS_BEARER, service-principal "
                "DATABRICKS_CLIENT_ID/DATABRICKS_CLIENT_SECRET, or select --profile explicitly."
            )
    return args


def main() -> int:
    args = arguments()
    if os.name not in {"nt", "posix"}:
        raise SystemExit("This runner supports Windows, Linux, and macOS.")

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    agents = [agent for agent in AGENT_PACKAGES if getattr(args, f"{agent}_version")]
    binaries = {name: shutil.which(name) for name in ("uv", "npm", "node", "databricks")}
    required = ["uv", "npm", "node"] + ([] if args.installation_only else ["databricks"])
    missing = [name for name in required if not binaries[name]]
    if missing:
        raise SystemExit("Install these prerequisites first: " + ", ".join(missing))
    if not args.installation_only:
        try:
            present = present_policy_paths(managed_policy_paths(agents))
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        if present:
            guidance = (
                "Use a clean Windows runner"
                if os.name == "nt"
                else "Use the integration container instead of this host"
            )
            raise SystemExit(
                "Machine-wide agent settings can override the selected test workspace. "
                f"{guidance}: " + ", ".join(present)
            )

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output = (args.output or ROOT / ".integration-runs" / stamp).resolve()
    output.mkdir(parents=True, exist_ok=False)
    build_log = output / "install.log"
    # Do not inherit project environments, resolver constraints, pytest options,
    # Python optimization, agent credentials, or npm settings from the caller.
    keep = (
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
    )
    base_env = {key: os.environ[key] for key in keep if key in os.environ}
    build_home = output / "build-home"
    build_home.mkdir()
    if os.name == "nt":
        temporary = output / "temp"
        local_app_data = build_home / "AppData/Local"
        roaming_app_data = build_home / "AppData/Roaming"
        for path in (temporary, local_app_data, roaming_app_data):
            path.mkdir(parents=True)
        base_env.update(
            {
                "APPDATA": str(roaming_app_data),
                "LOCALAPPDATA": str(local_app_data),
                "TEMP": str(temporary),
                "TMP": str(temporary),
                "TMPDIR": str(temporary),
                "PATHEXT": base_env.get("PATHEXT", WINDOWS_PATHEXT),
            }
        )
    base_env["HOME"] = str(build_home)
    base_env["USERPROFILE"] = str(build_home)
    base_env["npm_config_cache"] = str(output / "npm-cache")
    base_env["npm_config_fetch_retries"] = "1"
    base_env["npm_config_fetch_timeout"] = "30000"
    base_env["UV_CACHE_DIR"] = str(output / "cache")
    base_env["UV_DEFAULT_INDEX"] = args.default_index
    npm_token = os.environ.get(NPM_TOKEN_ENV, "")
    npm_config = None
    if npm_token:
        npm_config = output / "installer.npmrc"
        # npm expands the environment reference at request time. The short-lived
        # token is never written to disk or included in an argument or URL.
        npm_config.write_text(npm_user_config(args.npm_registry))
    python_install_env = installer_environment(base_env, os.environ, UV_INDEX_CREDENTIAL_ENV)
    npm_install_env = installer_environment(base_env, os.environ, (NPM_TOKEN_ENV,), npm_config)
    installer_secrets = tuple(os.environ.get(key, "") for key in INSTALLER_CREDENTIAL_ENV)
    bearer = os.environ.get("DATABRICKS_BEARER", "").strip()
    second_bearer = os.environ.get("DATABRICKS_SECOND_BEARER", "").strip()
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()

    def redact(value: str) -> str:
        return redact_secrets(value, (bearer, second_bearer, oauth_token, *installer_secrets))

    def run(command, *, cwd=output, env=base_env, timeout=600) -> str:
        timed_out = False
        with managed_process(
            [str(x) for x in command],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as proc:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
        if timed_out:
            stdout, stderr = proc.communicate(timeout=5)
        with build_log.open("a") as log:
            log.write(redact(stdout + stderr))
        if timed_out:
            raise RuntimeError(f"{command[0]} exceeded {timeout}s; see {build_log}")
        if proc.returncode:
            raise RuntimeError(f"{command[0]} failed; see {build_log}\n" + redact(stderr[-2000:]))
        return stdout.strip()

    report = {
        "requested": {
            "ug": str(args.ug_wheel) if args.ug_wheel else args.ug_version,
            "entry_point": args.entry_point,
            "claude": args.claude_version,
            "codex": args.codex_version,
            "claude_model": args.claude_model,
            "codex_model": args.codex_model,
            "claude_provider": args.claude_provider,
            "claude_relayed_provider": args.claude_relayed_provider,
            "claude_provider_model": args.claude_provider_model,
            "codex_provider": args.codex_provider,
            "codex_provider_model": args.codex_provider_model,
            "parent_schema": args.parent_schema,
            "claude_parent_model": args.claude_parent_model,
            "codex_parent_model": args.codex_parent_model,
            "dependencies": args.dependency,
            "workspace": args.workspace,
            "second_workspace": args.second_workspace,
        },
        "platform": platform.platform(),
        "installation_only": args.installation_only,
        "headless_only": args.headless_only,
    }
    manifest = output / "versions.json"
    exitcode = 1
    try:
        print(f"Installing selected versions. Results: {output}", flush=True)
        uv = binaries["uv"]
        runtime, testenv = output / "ug-runtime", output / "test-runtime"
        for path in (runtime, testenv):
            run([uv, "venv", "--python", args.python, path])
        python = venv_executable(runtime, "python")
        test_python = venv_executable(testenv, "python")
        report["python"] = run([python, "--version"])
        report["uv"] = run([uv, "--version"])
        report["node"] = run([binaries["node"], "--version"])
        report["npm"] = run([binaries["npm"], "--version"])
        if binaries["databricks"]:
            report["databricks"] = run([binaries["databricks"], "--version"])

        if args.ug_wheel:
            wheel = args.ug_wheel.resolve()
            if not wheel.is_file() or wheel.suffix != ".whl":
                raise RuntimeError(f"Wheel does not exist: {wheel}")
        elif args.ug_version == "checkout":
            if not (ROOT / "pyproject.toml").is_file():
                raise RuntimeError(
                    "No checkout in this image. Pass --ug-version or mount --ug-wheel."
                )
            wheels = output / "wheels"
            run(
                [uv, "build", "--wheel", "--out-dir", wheels, ROOT],
                cwd=ROOT,
                env=python_install_env,
            )
            (wheel,) = wheels.glob("*.whl")
            report["git_commit"] = run(["git", "rev-parse", "HEAD"], cwd=ROOT)
            report["tracked_diff"] = run(
                ["git", "diff", "HEAD", "--", "src", "pyproject.toml"], cwd=ROOT
            )
        else:
            wheel = None

        if wheel:
            report["wheel_sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
        # File URIs preserve spaces in paths parsed by uv's requirement options.
        package = wheel.as_uri() if wheel else f"unity-gateway=={args.ug_version}"
        constraints = output / "requested-constraints.txt"
        constraints.write_text(
            (args.constraints.read_text() if args.constraints else "")
            + "\n"
            + "\n".join(args.dependency)
        )
        run(
            [
                uv,
                "pip",
                "install",
                "--python",
                python,
                "--default-index",
                args.default_index,
                "--constraint",
                constraints.as_uri(),
                package,
            ],
            env=python_install_env,
        )
        run([uv, "pip", "check", "--python", python])
        freeze = run([uv, "pip", "freeze", "--python", python])
        (output / "installed.txt").write_text(freeze + "\n")
        (output / "dependencies.txt").write_text(
            "\n".join(
                line
                for line in freeze.splitlines()
                if not re.match(r"(?:unity-gateway|ucode)(?:==|\s*@)", line)
            )
            + "\n"
        )
        # The report proves we imported site-packages, not src/ via an editable install.
        report["package"] = json.loads(
            run(
                [
                    python,
                    "-c",
                    (
                        "import importlib.metadata as m, json, ucode\n"
                        "try:\n"
                        "    dist = m.distribution('unity-gateway')\n"
                        "except m.PackageNotFoundError:\n"
                        "    dist = m.distribution('ucode')\n"
                        "print(json.dumps({'distribution': dist.metadata['Name'], "
                        "'version': dist.version, 'path': ucode.__file__}))"
                    ),
                ]
            )
        )
        package_path = Path(report["package"]["path"]).resolve()
        if not package_path.is_relative_to(runtime):
            raise RuntimeError(
                f"Application was imported outside its isolated environment: {package_path}"
            )
        runtime_bin = python.parent
        binary = venv_executable(runtime, args.entry_point)
        if not binary.is_file():
            raise RuntimeError(
                f"Selected release has no {args.entry_point} entry point; try --entry-point ucode."
            )

        npm_prefix = output / "agents"
        npm_prefix.mkdir()
        if args.npm_lock:
            (npm_prefix / "package.json").write_text(
                json.dumps(
                    {
                        "dependencies": {
                            AGENT_PACKAGES[a]: getattr(args, f"{a}_version") for a in agents
                        }
                    }
                )
            )
            shutil.copyfile(args.npm_lock, npm_prefix / "package-lock.json")
            run(
                [
                    binaries["npm"],
                    "ci",
                    "--prefix",
                    npm_prefix,
                    "--no-audit",
                    "--no-fund",
                    "--registry",
                    args.npm_registry,
                ],
                env=npm_install_env,
            )
        else:
            run(
                [
                    binaries["npm"],
                    "install",
                    "--prefix",
                    npm_prefix,
                    "--no-audit",
                    "--no-fund",
                    "--save-exact",
                    "--registry",
                    args.npm_registry,
                    *[f"{AGENT_PACKAGES[a]}@{getattr(args, f'{a}_version')}" for a in agents],
                ],
                env=npm_install_env,
            )
        shutil.copyfile(npm_prefix / "package-lock.json", output / "npm-lock.json")
        report["npm_packages"] = json.loads(
            run(
                [
                    binaries["npm"],
                    "ls",
                    "--prefix",
                    npm_prefix,
                    "--depth=0",
                    "--json",
                ]
            )
        )["dependencies"]
        agent_bin = npm_prefix / "node_modules/.bin"
        # Expose only selected executables to tested programs; other installed
        # developer agents cannot be discovered accidentally via inherited PATH.
        tool_bin = output / "tools"
        tool_bin.mkdir()
        for name in ("node", "databricks"):
            if binaries[name]:
                source = Path(binaries[name]).resolve()
                if os.name == "nt":
                    # Windows runner accounts cannot be assumed to hold the
                    # privilege required for symlinks. Prefer a hard link and
                    # fall back to a copy when tool and output are on different volumes.
                    destination = tool_bin / source.name
                    try:
                        os.link(source, destination)
                    except OSError:
                        shutil.copy2(source, destination)
                else:
                    (tool_bin / name).symlink_to(source)
        runtime_env = dict(base_env)
        runtime_paths = [runtime_bin, agent_bin, tool_bin]
        if os.name == "nt":
            system_root = Path(base_env["SYSTEMROOT"])
            runtime_paths.extend(
                [
                    system_root / "System32",
                    system_root / "System32/WindowsPowerShell/v1.0",
                    system_root,
                ]
            )
        else:
            runtime_paths.extend([Path("/usr/bin"), Path("/bin")])
        runtime_env["PATH"] = os.pathsep.join(map(str, runtime_paths))
        report["agents"] = {}
        for agent in agents:
            agent_command = npm_executable(agent_bin, agent)
            version = run([agent_command, "--version"], env=runtime_env, timeout=30)
            expected = getattr(args, f"{agent}_version")
            if not re.search(rf"(?<![\w.]){re.escape(expected)}(?![\w.])", version):
                raise RuntimeError(f"Expected {agent} {expected}, got {version!r}")
            report["agents"][agent] = version

        if args.profile and not args.installation_only:
            # Never select a local profile implicitly. Do not persist auth output.
            with managed_process(
                [
                    binaries["databricks"],
                    "auth",
                    "token",
                    "--host",
                    args.workspace,
                    "--profile",
                    args.profile,
                    "--output",
                    "json",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                stdin=subprocess.DEVNULL,
            ) as auth:
                auth_stdout, _ = auth.communicate(timeout=30)
            if auth.returncode:
                raise RuntimeError(
                    "Could not obtain a token for the selected profile; log in first."
                )
            bearer = json.loads(auth_stdout).get("access_token", "")
            if not bearer:
                raise RuntimeError("Selected profile returned no access token.")

        if not bearer and not args.profile and not args.installation_only:
            client_id = os.environ.get("DATABRICKS_CLIENT_ID", "").strip()
            client_secret = os.environ.get("DATABRICKS_CLIENT_SECRET", "").strip()
            if client_id and client_secret:
                bearer = mint_m2m_token(args.workspace, client_id, client_secret)

        test_dependencies = ["pytest==9.0.3"]
        if os.name == "posix":
            test_dependencies.extend(["pexpect==4.9.0", "pyte==0.8.2"])
        run(
            [
                uv,
                "pip",
                "install",
                "--python",
                test_python,
                "--default-index",
                args.default_index,
                *test_dependencies,
            ],
            env=python_install_env,
        )
        (output / "test-dependencies.txt").write_text(
            run([uv, "pip", "freeze", "--python", test_python]) + "\n"
        )
        runtime_env.update(
            {
                "UG_INTEGRATION_BIN": str(binary),
                "UG_INTEGRATION_RUN_DIR": str(output),
                "UG_INTEGRATION_AGENTS": ",".join(agents),
                "UG_INTEGRATION_CLAUDE_PROVIDER": args.claude_provider,
                "UG_INTEGRATION_CLAUDE_RELAYED_PROVIDER": args.claude_relayed_provider,
                "UG_INTEGRATION_CLAUDE_OAUTH_TOKEN": oauth_token,
                "UG_INTEGRATION_CLAUDE_PROVIDER_MODEL": args.claude_provider_model,
                "UG_INTEGRATION_CODEX_PROVIDER": args.codex_provider,
                "UG_INTEGRATION_CODEX_PROVIDER_MODEL": args.codex_provider_model,
                "UG_INTEGRATION_PARENT_SCHEMA": args.parent_schema,
                "UG_INTEGRATION_CLAUDE_PARENT_MODEL": args.claude_parent_model,
                "UG_INTEGRATION_CODEX_PARENT_MODEL": args.codex_parent_model,
                "UCODE_TEST_WORKSPACE": args.workspace or "",
                "DATABRICKS_BEARER": bearer,
                "UCODE_TEST_SECOND_WORKSPACE": args.second_workspace or "",
                "DATABRICKS_SECOND_BEARER": second_bearer,
            }
        )
        for agent in agents:
            runtime_env[f"UG_INTEGRATION_{agent.upper()}_MODEL"] = (
                getattr(args, f"{agent}_model") or ""
            )
        suite = ROOT / "tests/integration"
        suite_hash = hashlib.sha256()
        for path in [Path(__file__), *sorted(suite.rglob("*.py")), suite / "pytest.ini"]:
            suite_hash.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes())
        report["suite_sha256"] = suite_hash.hexdigest()
        extra = args.pytest_args
        report["pytest_args"] = extra
        manifest.write_text(redact(json.dumps(report, indent=2)) + "\n")
        print("Running integration tests against the installed package.", flush=True)
        test_targets = integration_test_targets(
            suite,
            agents,
            platform_name=os.name,
            installation_only=args.installation_only,
            headless_only=args.headless_only,
        )
        with managed_process(
            [
                test_python,
                "-m",
                "pytest",
                "-c",
                suite / "pytest.ini",
                f"--confcutdir={suite}",
                *test_targets,
                "-v",
                "-o",
                f"cache_dir={output / 'pytest-cache'}",
                f"--junitxml={output / 'junit.xml'}",
                *extra,
            ],
            env=runtime_env,
            cwd=output,
            stdin=subprocess.DEVNULL,
            interrupt=True,
        ) as result:
            result.wait(timeout=3600)
        exitcode = result.returncode
        junit = output / "junit.xml"
        if junit.is_file():
            suites = ET.parse(junit).getroot().iter("testsuite")
            totals = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
            for suite_result in suites:
                for key in totals:
                    totals[key] += int(suite_result.get(key, "0"))
            report["results"] = totals
            if totals["skipped"]:
                raise RuntimeError("Requested integration tests were skipped; see junit.xml.")
            if not totals["tests"] and not exitcode:
                raise RuntimeError(
                    "No integration tests executed; see the selected pytest filters."
                )
            if args.headless_only and totals["tests"] != len(agents):
                raise RuntimeError(
                    f"Expected {len(agents)} headless integration tests, "
                    f"but {totals['tests']} executed; see junit.xml."
                )
        elif not exitcode:
            raise RuntimeError("Pytest returned success without a test report.")
        # A bootstrap/update path must not silently alter the selected agent version.
        for agent in agents:
            after = run(
                [npm_executable(agent_bin, agent), "--version"],
                env=runtime_env,
                timeout=30,
            )
            if after != report["agents"][agent]:
                raise RuntimeError(f"{agent} changed version during the suite: {after}")
    except KeyboardInterrupt:
        report["error"] = "Integration run interrupted."
        exitcode = 130
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        report["error"] = redact(str(exc))
        print(report["error"], file=sys.stderr)
        exitcode = 1
    finally:
        report["exitcode"] = exitcode
        manifest.write_text(redact(json.dumps(report, indent=2)) + "\n")
        print(f"Integration results: {output}", flush=True)
    return exitcode


if __name__ == "__main__":
    raise SystemExit(main())
