"""Databricks Sandbox launch transport. Harness arguments remain harness-owned."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import typer

from ucode.config_io import APP_DIR, atomic_write_json, read_json_safe
from ucode.databricks import databricks_cli_path, list_profile_entries
from ucode.launcher import exec_or_spawn
from ucode.os_compatibility.subprocess_cross_os import popen, run
from ucode.state import load_state
from ucode.workspace import resolve_workspace, split_workspace_key

app = typer.Typer(help="Run Claude or Codex in Databricks Sandbox.", no_args_is_help=True)


@dataclass
class Target:
    host: str
    profile: str | None
    sandbox: str | None
    directory: str
    resume: bool = False

    def cli(self, *args: str) -> list[str]:
        return [
            databricks_cli_path(),
            "sandbox",
            *args,
            *(["-p", self.profile] if self.profile else []),
        ]

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.profile:
            for key in (
                "DATABRICKS_TOKEN",
                "DATABRICKS_CLIENT_ID",
                "DATABRICKS_CLIENT_SECRET",
                "DATABRICKS_AUTH_TYPE",
            ):
                env.pop(key, None)
            env["DATABRICKS_CONFIG_PROFILE"] = self.profile
        else:
            env.pop("DATABRICKS_CONFIG_PROFILE", None)
        env["DATABRICKS_HOST"] = self.host
        return env


def target(
    workspace: str | None,
    profile: str | None,
    sandbox: str | None,
    directory: str,
    resume: bool = False,
) -> Target:
    if resume and sandbox:
        raise RuntimeError(
            "Use --resume for the last conversation, or --sandbox <id> to select an environment, not both."
        )
    if not shutil.which("ssh"):
        raise RuntimeError("Install OpenSSH client before running ug cloud.")
    state = load_state()
    if profile:
        entry = next((p for p in list_profile_entries() if p.get("name") == profile), None)
        if not entry:
            raise RuntimeError(
                f"Databricks profile {profile!r} not found. Run databricks auth login first."
            )
        profile_host = resolve_workspace(
            entry["host"],
            str(entry["workspace_id"]) if entry.get("workspace_id") else None,
            explicit_id=False,
        )
        if workspace and resolve_workspace(workspace) != profile_host:
            raise RuntimeError("--workspace does not match --profile. Select a matching profile.")
        workspace = workspace or profile_host
    else:
        workspace = workspace or os.environ.get("UG_WORKSPACE") or state.get("workspace")
        if workspace and workspace == state.get("workspace"):
            profile = state.get("profile")
            workspace = resolve_workspace(workspace, state.get("workspace_id"), explicit_id=False)
    if not workspace:
        raise RuntimeError(
            "Choose --workspace or --profile before the agent name, or configure ug first."
        )
    host, workspace_id = split_workspace_key(resolve_workspace(workspace))
    if workspace_id:
        raise RuntimeError(
            "Cloud preview requires a workspace-specific hostname; shared-host workspace selection is not supported yet."
        )
    if sandbox and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", sandbox):
        raise RuntimeError("Invalid Sandbox ID.")
    if not directory.startswith("/") or "\x00" in directory or "\n" in directory:
        raise RuntimeError("--directory must be an absolute remote Linux path.")
    return Target(host, profile, sandbox, directory, resume)


def apply_environment(t: Target) -> None:
    env = t.env()
    for key in (
        "DATABRICKS_TOKEN",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_AUTH_TYPE",
    ):
        if key not in env:
            os.environ.pop(key, None)
    if "DATABRICKS_CONFIG_PROFILE" not in env:
        os.environ.pop("DATABRICKS_CONFIG_PROFILE", None)
    os.environ.update(env)


def checked(t: Target, args: list[str]) -> str:
    result = run(
        args, env=t.env(), capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "Databricks CLI failed.").strip())
    return result.stdout


def create_sandbox(t: Target, agent: str) -> str:
    """Surface quota rejections while the native CLI is still retrying.

    Debug output is inspected in memory, never printed or saved: it can contain
    authentication details. Only the quota limit is extracted for the user.
    """
    events: queue.Queue[str | None] = queue.Queue()
    with tempfile.TemporaryFile(mode="w+") as output:
        process = popen(
            t.cli("create", "--name", f"ug-cloud-{agent}", "--json", "--debug"),
            env=t.env(),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.PIPE,
            text=True,
        )
        stderr = process.stderr
        assert stderr is not None

        def inspect_errors() -> None:
            try:
                for line in stderr:
                    if "Cannot create sandboxes" in line and "configured limit" in line:
                        match = re.search(r"configured limit of (\d+)", line)
                        limit = f" (configured limit: {match[1]})" if match else ""
                        events.put(
                            "Sandbox creation rejected: quota exceeded"
                            + limit
                            + ". The service returned RESOURCE_EXHAUSTED. "
                            "Stopped the Databricks CLI's retries. Reuse an existing Sandbox "
                            "with --sandbox <id>, or ask your administrator to free capacity "
                            "or increase the limit."
                        )
                        return
            finally:
                events.put(None)

        reader = threading.Thread(target=inspect_errors, daemon=True)
        reader.start()
        try:
            try:
                error = events.get(timeout=300)
            except queue.Empty:
                raise RuntimeError(
                    "Sandbox creation timed out after 300 seconds. Check ug cloud list "
                    "before trying again; the request may still have created a Sandbox."
                ) from None
            if error:
                raise RuntimeError(error)
            if process.wait(timeout=5):
                raise RuntimeError(
                    "Databricks Sandbox creation failed. Run databricks sandbox create "
                    "with your selected profile to inspect the error."
                )
            output.seek(0)
            return output.read()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            reader.join(timeout=5)
            stderr.close()


@app.callback()
def options(
    ctx: typer.Context,
    workspace: str | None = typer.Option(None, help="Databricks workspace URL."),
    profile: str | None = typer.Option(None, help="Databricks CLI authentication profile."),
    sandbox: str | None = typer.Option(
        None, help="Reuse an existing Sandbox ID; otherwise create one."
    ),
    directory: str = typer.Option(
        "/home/sandbox-agent/project",
        help="Remote working directory. Local files are not uploaded.",
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        help="Resume the last interactive conversation instead of creating a new Sandbox.",
    ),
) -> None:
    if resume and ctx.invoked_subcommand not in {"claude", "codex"}:
        raise typer.BadParameter("--resume is only supported with claude or codex.")
    # Resolve only when executing, so all help paths work offline.
    ctx.obj = (workspace, profile, sandbox, directory, resume)


def resolve(ctx: typer.Context) -> Target:
    try:
        return target(*ctx.obj)
    except (RuntimeError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None


def ssh_argv(
    t: Target, sandbox: str, command: str | None = None, *, tty: bool = False
) -> list[str]:
    return [
        *t.cli("ssh", sandbox),
        "--",
        "-tt" if tty else "-T",
        *(["bash", "-lc", command] if command else []),
    ]


def connected_ssh(t: Target, sid: str, command: str, *, tty: bool) -> list[str]:
    # Discover the workspace-specific gateway through the CLI, but hand the terminal
    # directly to OpenSSH: the CLI command path does not preserve stdin for TUIs.
    raw = checked(t, [*t.cli("ssh", sid), "--", "-G"])
    config = {}
    for line in raw.splitlines():
        key, _, value = line.partition(" ")
        config.setdefault(key, value.strip())
    host, port, identity = (config.get(k, "") for k in ("hostname", "port", "identityfile"))
    if (
        not re.fullmatch(r"[A-Za-z0-9.-]+", host)
        or not port.isdigit()
        or not Path(identity).is_absolute()
    ):
        raise RuntimeError(
            "Could not resolve Sandbox SSH settings. Check databricks sandbox ssh ID -- -G."
        )
    known_hosts = Path.home() / ".ssh" / "known_hosts_ug_cloud"
    return [
        "ssh",
        "-tt" if tty else "-T",
        "-p",
        port,
        "-i",
        identity,
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "ConnectTimeout=20",
        f"{sid}@{host}",
        command,
    ]


def remote_command(agent: str, args: list[str], t: Target, *, tty: bool = False) -> str:
    # Every user-controlled token is quoted for the remote POSIX shell.
    # Sandbox harness wrappers otherwise dispatch back into ug/ucode, configuring twice.
    # This is the image's own recursion guard; the outer ug still configures Gateway.
    startup = Path(__file__).with_name("cloud_remote.py").read_text(encoding="utf-8")
    launch = shlex.join(
        [
            "env",
            "_UCODE_DISPATCH_ACTIVE=1",
            "python3",
            "-c",
            startup,
            agent,
            "--workspace",
            t.host,
            *args,
        ]
    )
    if tty:
        # Sandbox SSH currently accepts -tt but supplies pipes (isatty=False).
        # util-linux script allocates a real PTY for the harness inside the Sandbox.
        columns, rows = shutil.get_terminal_size((120, 40))
        inner = f"stty rows {rows} cols {columns}; exec {launch}"
        launch = shlex.join(
            [
                "env",
                f"TERM={os.environ.get('TERM') or 'xterm-256color'}",
                *[
                    f"{key}={os.environ[key]}"
                    for key in ("COLORTERM", "COLORFGBG", "TERM_PROGRAM", "TERM_PROGRAM_VERSION")
                    if os.environ.get(key)
                ],
                "script",
                "-q",
                "-e",
                "-c",
                inner,
                "/dev/null",
            ]
        )
    directory = shlex.quote(t.directory)
    return f"mkdir -p -- {directory} && cd -- {directory} && exec {launch}"


SESSION_DIR = APP_DIR / "cloud-sessions"


def session_path(t: Target, agent: str) -> Path:
    # Separate profiles can identify different users on the same workspace.
    key = json.dumps([t.host, t.profile, agent, t.directory])
    return SESSION_DIR / (hashlib.sha256(key.encode()).hexdigest() + ".json")


def native_session_requested(agent: str, args: list[str]) -> bool:
    if agent == "codex":
        return bool(args and args[0] in {"resume", "fork"})
    return any(
        a.split("=", 1)[0]
        in {"-r", "--resume", "-c", "--continue", "--session-id", "--fork-session"}
        for a in args
    )


def choose_previous(
    t: Target, agent: str, args: list[str]
) -> tuple[str | None, list[str], str | None]:
    """Reuse a saved launch only with explicit --resume; never prompt for a session."""
    if t.sandbox:
        session = None
        if agent == "claude":
            for index, arg in enumerate(args):
                flag, _, value = arg.partition("=")
                if flag in {"--resume", "-r", "--session-id"}:
                    value = value or (args[index + 1] if index + 1 < len(args) else "")
                    try:
                        session = str(uuid.UUID(value))
                    except ValueError:
                        pass
        return t.sandbox, args, session
    if native_session_requested(agent, args):
        raise RuntimeError(
            "Select --sandbox <id> before the agent when resuming an explicit session."
        )
    if not t.resume:
        return None, args, None
    saved = read_json_safe(session_path(t, agent))
    sid = saved.get("sandbox")
    if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", sid):
        raise RuntimeError(
            f"No previous {agent} Sandbox is saved for this workspace, profile and directory. "
            f"Run ug cloud {agent} to start a new one."
        )
    sandboxes = json.loads(checked(t, t.cli("list", "--json")))
    if not isinstance(sandboxes, list):
        raise RuntimeError("Cannot check previous Sandbox: unexpected sandbox list response.")
    if not any(isinstance(row, dict) and row.get("sandboxId") == sid for row in sandboxes):
        session_path(t, agent).unlink(missing_ok=True)
        raise RuntimeError(
            f"Previous Sandbox {sid} no longer exists. Run ug cloud {agent} to start a new one."
        )
    session = saved.get("session")
    try:
        session = str(uuid.UUID(session)) if isinstance(session, str) else None
    except ValueError:
        session = None
    if agent == "claude" and session:
        # A launch can end during onboarding, before Claude writes a conversation.
        found = checked(
            t,
            ssh_argv(
                t,
                sid,
                f'find "$HOME/.claude/projects" -type f -name {shlex.quote(session + ".jsonl")} -print -quit 2>/dev/null || true',
            ),
        )
        if not found.strip():
            raise RuntimeError(
                "No saved Claude conversation was found. Start a new conversation with "
                f"ug cloud --sandbox {sid} claude."
            )
    resume_args = (
        (["--resume", session] if session else ["--continue"])
        if agent == "claude"
        else ["resume", "--last"]
    )
    return sid, [*resume_args, *args], session


def resume_instructions(
    t: Target, sid: str, agent: str, session: str | None, *, shortcut: bool = True
) -> str:
    state = load_state()
    selection = (
        []
        if state.get("workspace") == t.host and state.get("profile") == t.profile
        else (["--profile", t.profile] if t.profile else ["--workspace", t.host])
    )
    base = ["ug", "cloud", *selection]
    directory = [] if t.directory == "/home/sandbox-agent/project" else ["--directory", t.directory]
    resume = (
        (["--resume", session] if session else ["--continue"])
        if agent == "claude"
        else ["resume", "--last"]
    )
    command = shlex.join(
        [*base, *directory, "--resume", agent]
        if shortcut
        else [*base, "--sandbox", sid, *directory, agent, *resume]
    )
    delete = shlex.join([*base, "delete", sid])
    return f"Sandbox retained: {sid}\nResume: {command}\nDelete: {delete}"


def with_exit_instructions(command: str, instructions: str) -> str:
    # Keep exec_or_spawn's terminal handoff. Print after the remote TUI restores its terminal.
    return f"( {command} ); ug_status=$?; printf '%s\\n' {shlex.quote(instructions)} >&2; exit \"$ug_status\""


# Runs inside the Sandbox. Emit only readiness, never credential contents.
_REMOTE_AUTH_PROBE = """
import configparser, json, pathlib, sys
from ucode.state import load_state
cfg = configparser.ConfigParser(interpolation=None)
cfg.read(pathlib.Path.home() / '.databrickscfg')
profile = cfg.defaults()
host = sys.argv[1].rstrip('/')
credential = bool(profile.get('token', '').strip()) and profile.get('host', '').rstrip('/') == host
state = load_state()
ready = credential and state.get('use_pat') is True and state.get('profile') == 'DEFAULT' and state.get('workspace', '').rstrip('/') == host
print(json.dumps({'credential_available': credential, 'configured': ready}))
"""


def ensure_remote_auth(t: Target, sid: str, agent: str) -> None:
    """Use the image's service-mounted credential, not remote browser OAuth."""
    probe = shlex.join(["python3", "-c", _REMOTE_AUTH_PROBE, t.host])
    status = json.loads(checked(t, ssh_argv(t, sid, probe)))
    if not isinstance(status, dict) or status.get("credential_available") is not True:
        raise RuntimeError(
            "Sandbox-provided Databricks authentication is missing or belongs to a different "
            "workspace. Remote browser login will not be started. Reconnect to the Sandbox "
            "to refresh its service-mounted credentials, or contact the Sandbox administrator."
        )
    if status.get("configured") is True:
        return
    configure = shlex.join(
        [
            "env",
            "_UCODE_DISPATCH_ACTIVE=1",
            "ug",
            "configure",
            "--profile",
            "DEFAULT",
            "--agents",
            agent,
            "--use-pat",
            "--skip-upgrade",
        ]
    )
    checked(t, ssh_argv(t, sid, configure))
    status = json.loads(checked(t, ssh_argv(t, sid, probe)))
    if not isinstance(status, dict) or status.get("configured") is not True:
        raise RuntimeError(
            "Remote ug did not save the Sandbox authentication configuration. "
            "Stopped before launching the agent to avoid an unusable browser login."
        )


def launch(ctx: typer.Context, agent: str) -> None:
    t = resolve(ctx)
    args = list(ctx.args)
    if args[:1] == ["--"]:
        args.pop(0)
    if any(a == "--workspace" or a.startswith("--workspace=") for a in args):
        raise typer.BadParameter(
            "Put --workspace before the agent: ug cloud --workspace URL " + agent
        )
    if any(a.split("=", 1)[0] in {"--bg", "--background", "--cloud", "--remote"} for a in args):
        raise typer.BadParameter(
            "Detached and nested cloud launches are not supported by ug cloud yet."
        )
    headless = (
        agent == "claude" and any(a in ("-p", "--print") or a.startswith("--print=") for a in args)
    ) or (agent == "codex" and args[:1] in (["exec"], ["e"], ["review"]))
    if t.resume and headless:
        raise typer.BadParameter(
            "--resume is for interactive conversations. Use --sandbox <id> for a headless job in an existing Sandbox."
        )
    if not headless and not sys.stdin.isatty():
        raise typer.BadParameter(
            "Interactive launch needs a terminal. Use claude -p or codex exec for headless work."
        )
    sid = t.sandbox
    disposable = False
    exit_code = 0
    session = None
    try:
        if not headless:
            sid, args, session = choose_previous(t, agent, args)
            if agent == "claude" and not native_session_requested(agent, args):
                session = str(uuid.uuid4())
                args = ["--session-id", session, *args]
        # Reuses an existing key; does not read or transfer the client's credentials.
        destination = f"Sandbox {sid}" if sid else "a new Sandbox"
        typer.echo(f"Starting {agent.title()} in {destination}...", err=True)
        checked(t, t.cli("register"))
        if not sid:
            data = json.loads(create_sandbox(t, agent))
            sid = data.get("sandboxId")
            if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", sid):
                raise RuntimeError(
                    "Sandbox create returned no valid sandboxId. Inspect databricks sandbox list."
                )
            disposable = headless
            typer.echo(f"Sandbox: {sid}", err=True)
        checked(
            t,
            ssh_argv(
                t,
                sid,
                f"command -v ug >/dev/null && command -v {agent} >/dev/null || {{ echo 'Sandbox image must provide ug and {agent} on PATH.' >&2; exit 127; }}",
            ),
        )
        ensure_remote_auth(t, sid, agent)
        # CLI reads the selected workspace from env for register/create/ssh alike.
        apply_environment(t)
        remote = remote_command(agent, args, t, tty=not headless)
        if not headless:
            instructions = resume_instructions(t, sid, agent, session)
            try:
                atomic_write_json(
                    session_path(t, agent),
                    {
                        "sandbox": sid,
                        "session": session,
                        "opened_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    },
                )
            except (RuntimeError, OSError) as exc:
                typer.echo(f"Could not save the resume shortcut: {exc}", err=True)
                instructions = resume_instructions(t, sid, agent, session, shortcut=False)
            remote = with_exit_instructions(remote, instructions)
        command = connected_ssh(t, sid, remote, tty=not headless)
        if headless:
            # Stay alive to clean up after the child exits; interactive launches still exec.
            exit_code = run(command, env=t.env()).returncode
        else:
            exec_or_spawn(command)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        typer.echo(f"Error: {exc}", err=True)
        exit_code = 1
        if sid and not disposable:
            typer.echo(
                f"Sandbox {sid} retained for inspection. Use ug cloud --workspace {t.host} ssh {sid}.",
                err=True,
            )
    except KeyboardInterrupt:
        exit_code = 130
    finally:
        if disposable and sid:
            typer.echo(f"Deleting temporary Sandbox {sid}...", err=True)
            try:
                checked(t, t.cli("delete", sid, "--auto-approve"))
                typer.echo(f"Deleted Sandbox {sid}.", err=True)
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                typer.echo(
                    f"Cleanup failed: {exc}\nSandbox {sid} may still exist. "
                    f"Run ug cloud --workspace {t.host} delete {sid}.",
                    err=True,
                )
                exit_code = exit_code or 1
    if exit_code:
        raise typer.Exit(exit_code)


_FORWARD = {
    "allow_extra_args": True,
    "ignore_unknown_options": True,
    "allow_interspersed_args": False,
}


@app.command(context_settings=_FORWARD)
def claude(ctx: typer.Context) -> None:
    """Launch Claude. Remaining arguments are forwarded to remote ug claude."""
    launch(ctx, "claude")


@app.command(context_settings=_FORWARD)
def codex(ctx: typer.Context) -> None:
    """Launch Codex. Remaining arguments are forwarded to remote ug codex."""
    launch(ctx, "codex")


@app.command("list")
def list_sandboxes(ctx: typer.Context) -> None:
    """List Sandboxes in the selected workspace."""
    t = resolve(ctx)
    apply_environment(t)
    exec_or_spawn(t.cli("list"))


@app.command()
def ssh(ctx: typer.Context, sandbox_id: str) -> None:
    """Open a shell in an existing Sandbox."""
    t = resolve(ctx)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", sandbox_id):
        raise typer.BadParameter("Invalid Sandbox ID.")
    apply_environment(t)
    exec_or_spawn(t.cli("ssh", sandbox_id))


@app.command()
def delete(
    ctx: typer.Context,
    sandbox_id: str,
    yes: bool = typer.Option(False, "--yes", help="Skip Databricks deletion confirmation."),
) -> None:
    """Delete a Sandbox and its files. Databricks asks for confirmation by default."""
    t = resolve(ctx)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", sandbox_id):
        raise typer.BadParameter("Invalid Sandbox ID.")
    apply_environment(t)
    exec_or_spawn(t.cli("delete", sandbox_id, *(["--auto-approve"] if yes else [])))
