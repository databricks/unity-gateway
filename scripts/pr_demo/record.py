#!/usr/bin/env python3
"""Record BEFORE/AFTER `ug` terminal demos for a pull request with a built-in recorder.

Builds one virtualenv per commit (merge-base with origin/main = BEFORE, PR head
= AFTER, cached per SHA), runs the same commands in a sandboxed HOME under a
real pty, replays the captured bytes through pyte, and renders an animated GIF
plus a final-frame PNG and a plain-text capture with Pillow. No external
recording tools are needed.

Run it with the two recorder-only dependencies (they are NOT project
dependencies and never touch the lock file):

    uv run --with pyte --with pillow python scripts/pr_demo/record.py \
        --command "ug --help" --command "ug doctor --help"

When every command produces identical BEFORE and AFTER text, the demo would
show no change: the run exits with code 3 before rendering anything.
``--allow-identical`` overrides that guard for intentional no-op recordings.

Like the other scripts in this directory, this file runs before ug is installed
and uses the stdlib subprocess module directly. Interactive flows are recorded
on macOS/Linux only; on Windows (or where pty is unavailable) recording falls
back to a non-pty pipe capture with FORCE_COLOR=1.
"""

from __future__ import annotations

import argparse
import datetime
import errno
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

INSTALL_TIMEOUT_SECONDS = 900
COMMAND_TIMEOUT_SECONDS = 120.0
DEFAULT_ROWS = 30
DEFAULT_COLS = 100
DEFAULT_SLEEP_SECONDS = 5.0
SETUP_SLEEP_SECONDS = 15.0
TYPE_CADENCE_SECONDS = 0.03
PROMPT_PAUSE_SECONDS = 0.4
MIN_SNAPSHOT_INTERVAL_SECONDS = 0.1  # ~10 fps cap
MAX_FRAMES = 600
FINAL_HOLD_SECONDS = 2.0
FONT_SIZE = 16
BG_DEFAULT = "#1e1f29"
FG_DEFAULT = "#f8f8f2"

FAKE_SUDO = """#!/bin/sh
echo "pr-demo sandbox: sudo is blocked; recordings must not touch OS-managed files." >&2
exit 1
"""

# Secrets the published captures must never contain.
HOST_PATTERN = re.compile(
    r"(?:[A-Za-z0-9-]+\.)+(?:cloud\.databricks\.com|azuredatabricks\.net|gcp\.databricks\.com)"
)
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("databricks-pat", re.compile(r"dapi[0-9a-f]{32}")),
    ("bearer-token", re.compile(r"(?i)bearer\s+\S+")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("databricks-host", HOST_PATTERN),
    ("workspace-id", re.compile(r"\?o=\d+")),
]

# Basic ANSI color names pyte can put on a character.
BASIC_COLORS = {
    "black": "#000000",
    "red": "#cd0000",
    "green": "#00cd00",
    "brown": "#cd8500",
    "yellow": "#cdcd00",
    "blue": "#0000ee",
    "magenta": "#cd00cd",
    "cyan": "#00cdcd",
    "white": "#e5e5e5",
    "brightblack": "#555555",
    "brightred": "#ff5555",
    "brightgreen": "#55ff55",
    "brightyellow": "#ffff55",
    "brightblue": "#5555ff",
    "brightmagenta": "#ff55ff",
    "brightcyan": "#55ffff",
    "brightwhite": "#ffffff",
}
HEX_COLOR = re.compile(r"^[0-9a-fA-F]{6}$")

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "C:/Windows/Fonts/consola.ttf",
]


def run(
    cmd: list[str],
    *,
    check: bool = True,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command as an argument list with text streams and a timeout."""
    return subprocess.run(  # noqa: TID251 - runs before ug is installed
        cmd, check=check, timeout=timeout, cwd=cwd, capture_output=True, text=True
    )


def git(*args: str) -> str:
    return run(["git", *args], cwd=REPO_ROOT).stdout.strip()


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Pure helpers (covered by tests/test_pr_demo.py)
# ---------------------------------------------------------------------------


def find_secrets(text: str) -> list[tuple[str, str]]:
    """Return (kind, match) pairs for every secret-shaped substring."""
    found: list[tuple[str, str]] = []
    for kind, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            found.append((kind, match.group(0)))
    return found


def redact_hosts(text: str) -> str:
    """Replace Databricks workspace hostnames with a neutral placeholder."""
    return HOST_PATTERN.sub("https://<workspace>", text)


def sanitize_capture(text: str, *, redact_host: bool) -> tuple[str, list[tuple[str, str]]]:
    """Redact hosts when allowed, then return remaining secret matches."""
    if redact_host:
        text = redact_hosts(text)
    return text, find_secrets(text)


def build_sandbox_env(
    *,
    sandbox_bin: str,
    venv_bin: str,
    home: str,
    workspace_config: str | None,
) -> dict[str, str]:
    """Environment for the recorded session: sandbox HOME and PATH first."""
    env = {
        "HOME": home,
        "XDG_CONFIG_HOME": f"{home}/.config",
        "XDG_DATA_HOME": f"{home}/.local/share",
        "XDG_CACHE_HOME": f"{home}/.cache",
        "PATH": f"{sandbox_bin}:{venv_bin}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "UG_SKIP_OS_SETTINGS": "1",
        "TERM": "xterm-256color",
        "FORCE_COLOR": "1",
    }
    if workspace_config:
        env["DATABRICKS_CONFIG_FILE"] = workspace_config
    return env


def typing_chunks(
    command: str, *, cadence: float = TYPE_CADENCE_SECONDS
) -> list[tuple[float, bytes]]:
    """Synthetic keystroke chunks for the prompt line, at a fixed cadence.

    Both sides type the same line at the same speed so BEFORE/AFTER differ only
    in output, never in timing.
    """
    line = f"$ {command}"
    chunks = [(i * cadence, char.encode()) for i, char in enumerate(line)]
    chunks.append((len(line) * cadence, b"\r\n"))
    return chunks


def resolve_color(value: str | None, default: str, *, bold: bool = False) -> str:
    """Map a pyte fg/bg value (name, 256/truecolor hex, or None) to an RGB hex."""
    if not value or value == "default":
        resolved = default
    elif HEX_COLOR.fullmatch(value):
        resolved = f"#{value}"
    else:
        name = value.lower().replace("-", "").replace(" ", "")
        if bold and f"bright{name}" in BASIC_COLORS:
            resolved = BASIC_COLORS[f"bright{name}"]
        else:
            resolved = BASIC_COLORS.get(name, default)
    return resolved


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def capture_pty(
    argv: list[str],
    env: dict[str, str],
    *,
    rows: int,
    cols: int,
    timeout: float,
) -> list[tuple[float, bytes]]:
    """Run argv under a pty and return (seconds, bytes) chunks. macOS/Linux."""
    import fcntl
    import pty
    import select
    import signal
    import struct
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    pid = os.fork()
    if pid == 0:  # child
        os.close(master)
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        os.dup2(slave, 0)
        os.dup2(slave, 1)
        os.dup2(slave, 2)
        if slave > 2:
            os.close(slave)
        child_env = {**env, "COLUMNS": str(cols), "LINES": str(rows)}
        try:
            os.execvpe(argv[0], argv, child_env)
        except OSError:
            os._exit(127)

    os.close(slave)
    chunks: list[tuple[float, bytes]] = []
    start = time.monotonic()
    deadline = start + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                os.kill(pid, signal.SIGKILL)
                warn(f"command timed out after {timeout:g}s and was killed")
                break
            ready, _, _ = select.select([master], [], [], min(remaining, 0.5))
            if not ready:
                done_pid, _ = os.waitpid(pid, os.WNOHANG)
                if done_pid == pid:
                    # Drain what is left before the fd closes.
                    while select.select([master], [], [], 0.1)[0]:
                        data = os.read(master, 65536)
                        if not data:
                            break
                        chunks.append((time.monotonic() - start, data))
                    break
                continue
            try:
                data = os.read(master, 65536)
            except OSError as error:
                # Linux raises EIO on the master once the child's slave side
                # closes; that is an ordinary EOF.
                if error.errno != errno.EIO:
                    raise
                break
            if not data:
                break
            chunks.append((time.monotonic() - start, data))
    finally:
        os.close(master)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    return chunks


def capture_pipe(
    argv: list[str], env: dict[str, str], *, timeout: float
) -> list[tuple[float, bytes]]:
    """Non-pty fallback (e.g. Windows): merged output as a single chunk."""
    merged_env = {**env, "COLUMNS": "1000", "LINES": "1000"}
    proc = subprocess.Popen(  # noqa: TID251 - scripts/ runs before ug is installed
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=merged_env,
    )
    try:
        data = proc.communicate(timeout=timeout)[0] or b""
    except subprocess.TimeoutExpired:
        proc.kill()
        data = proc.communicate()[0] or b""
        warn(f"command timed out after {timeout:g}s and was killed")
    return [(0.0, data)]


def capture_command(
    command: str,
    env: dict[str, str],
    *,
    rows: int,
    cols: int,
    timeout: float,
    typed: bool = True,
) -> list[tuple[float, bytes]]:
    """Type the prompt synthetically, then run the command and capture its output.

    With ``typed=False`` (the hidden workspace setup) the command line itself is
    never displayed; only its output is fed to the screen.
    """
    prefix = typing_chunks(command) if typed else []
    offset = (prefix[-1][0] if prefix else 0.0) + PROMPT_PAUSE_SECONDS
    argv = shlex.split(command)
    try:
        if hasattr(os, "fork") and hasattr(os, "openpty"):
            import pty  # noqa: F401

            raw = capture_pty(argv, env, rows=rows, cols=cols, timeout=timeout)
        else:
            raw = capture_pipe(argv, env, timeout=timeout)
    except ImportError:
        raw = capture_pipe(argv, env, timeout=timeout)
    return prefix + [(t + offset, data) for t, data in raw]


# ---------------------------------------------------------------------------
# Replay and render
# ---------------------------------------------------------------------------


def freeze_screen(screen) -> tuple[tuple[tuple[str, str | None, str | None, bool, bool], ...], ...]:
    """Snapshot a pyte screen as immutable (data, fg, bg, bold, reverse) cells.

    pyte's Screen is mutable and reused across feeds, so every snapshot must be
    frozen at yield time; otherwise all captured frames would show the final
    state.
    """
    rows = []
    for y in range(screen.lines):
        row = screen.buffer[y]
        rows.append(
            tuple(
                (row[x].data or " ", row[x].fg, row[x].bg, bool(row[x].bold), bool(row[x].reverse))
                for x in range(screen.columns)
            )
        )
    return tuple(rows)


def frozen_display(
    frozen: tuple[tuple[tuple[str, str | None, str | None, bool, bool], ...], ...],
) -> str:
    """The plain text of a frozen screen snapshot."""
    return "\n".join("".join(cell[0] for cell in row) for row in frozen)


def screen_snapshots(
    chunks: list[tuple[float, bytes]],
    *,
    rows: int,
    cols: int,
    min_interval: float = MIN_SNAPSHOT_INTERVAL_SECONDS,
):
    """Yield (time, frozen-screen) only when the display changes, rate-limited.

    Requires pyte (imported lazily so the rest of the script stays importable
    without it). The caller subsamples with :func:`subsample` to cap frames.
    """
    import pyte

    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    last_capture: float | None = None
    last_display: str | None = None
    pending = False
    for t, data in chunks:
        stream.feed(data)
        display = "\n".join(screen.display)
        if display == last_display:
            continue
        if last_capture is not None and t - last_capture < min_interval:
            pending = True
            continue
        yield t, freeze_screen(screen)
        last_capture, last_display, pending = t, display, False
    if pending or last_capture is None:
        yield (chunks[-1][0] if chunks else 0.0), freeze_screen(screen)


def subsample(times: list[float], max_frames: int = MAX_FRAMES) -> list[int]:
    """Indices of snapshots to keep: evenly spread, always including the last."""
    if len(times) <= max_frames:
        return list(range(len(times)))
    keep = {round(i * (len(times) - 1) / (max_frames - 1)) for i in range(max_frames - 1)}
    keep.add(len(times) - 1)
    return sorted(keep)


def frame_durations(times: list[float]) -> list[int]:
    """Per-frame durations in ms; the final frame is held for FINAL_HOLD_SECONDS."""
    durations = []
    for i, t in enumerate(times):
        delta = (times[i + 1] - t) if i + 1 < len(times) else FINAL_HOLD_SECONDS
        durations.append(max(int(delta * 1000), 50))
    if durations:
        durations[-1] = max(durations[-1], int(FINAL_HOLD_SECONDS * 1000))
    return durations


def load_font(size: int = FONT_SIZE):
    """A monospace TTF from the system, or Pillow's scalable default font."""
    from PIL import ImageFont

    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size)


def cell_metrics(font) -> tuple[int, int]:
    from PIL import ImageFont

    bbox = font.getbbox("M")
    char_w = max(bbox[2] - bbox[0], 1)
    line_h = max(int(getattr(font, "size", FONT_SIZE) * 1.35), bbox[3] - bbox[1] + 2)
    if isinstance(font, ImageFont.FreeTypeFont):
        ascent, descent = font.getmetrics()
        line_h = max(line_h, ascent + descent)
    return char_w, line_h


def render_screen(frozen, *, font, char_w: int, line_h: int, cols: int, rows: int):
    """Render a frozen screen snapshot to an RGB PIL image (grouped by style runs)."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (cols * char_w, rows * line_h), BG_DEFAULT)
    draw = ImageDraw.Draw(image)
    for y, row in enumerate(frozen[:rows]):
        x = 0
        while x < cols:
            data, fg_value, bg_value, bold, reverse = row[x]
            style = (
                resolve_color(fg_value, FG_DEFAULT, bold=bold),
                resolve_color(bg_value, BG_DEFAULT),
                reverse,
            )
            start = x
            text: list[str] = []
            while x < cols:
                nxt = row[x]
                if (
                    resolve_color(nxt[1], FG_DEFAULT, bold=nxt[3]),
                    resolve_color(nxt[2], BG_DEFAULT),
                    nxt[4],
                ) != style:
                    break
                text.append(nxt[0])
                x += 1
            fg, bg, reverse = style
            if reverse:
                fg, bg = bg, fg
            run = "".join(text).rstrip()
            if bg.upper() != BG_DEFAULT.upper():
                draw.rectangle(
                    [start * char_w, y * line_h, x * char_w - 1, (y + 1) * line_h - 1],
                    fill=bg,
                )
            if run:
                draw.text((start * char_w, y * line_h), run, font=font, fill=fg)
    return image


def render_and_save(
    chunks: list[tuple[float, bytes]],
    *,
    gif_path: Path,
    png_path: Path,
    rows: int,
    cols: int,
    render_rows: int | None = None,
) -> int:
    """Render the captured chunks to a GIF and final-frame PNG; return frame count."""
    snapshots = list(screen_snapshots(chunks, rows=rows, cols=cols))
    indices = subsample([t for t, _ in snapshots])
    kept = [snapshots[i] for i in indices]
    times = [t for t, _ in kept]
    font = load_font()
    char_w, line_h = cell_metrics(font)
    images = [
        render_screen(
            screen, font=font, char_w=char_w, line_h=line_h, cols=cols, rows=render_rows or rows
        )
        for _, screen in kept
    ]
    images[0].save(
        gif_path,
        save_all=True,
        append_images=images[1:],
        duration=frame_durations(times),
        loop=0,
        optimize=True,
    )
    images[-1].save(png_path)
    return len(images)


def shared_render_rows(recordings: list[list[tuple[float, bytes]]], *, rows: int, cols: int) -> int:
    """Fit all visible screen states, with one padding row and a six-row minimum."""
    used = 0
    for chunks in recordings:
        for _, screen in screen_snapshots(chunks, rows=rows, cols=cols):
            for y, row in enumerate(screen):
                if any(
                    data.strip() or bg not in (None, "default") or reverse
                    for data, _, bg, _, reverse in row
                ):
                    used = max(used, y + 1)
    return min(rows, max(6, used + 1))


def check_recording_for_secrets(chunks: list[tuple[float, bytes]], *, rows: int, cols: int) -> None:
    """Reject secrets in the raw capture or any screen that could be rendered.

    Host redaction applies to text only, so it must never exempt image content.
    """
    raw = b"".join(data for _, data in chunks).decode("utf-8", errors="replace")
    raw = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw)
    texts = [raw]
    for _, screen in screen_snapshots(chunks, rows=rows, cols=cols):
        lines = frozen_display(screen).splitlines()
        texts.extend(("\n".join(lines), "".join(line.rstrip() for line in lines)))
    kinds = sorted({kind for text in texts for kind, _ in find_secrets(text)})
    if kinds:
        raise SystemExit(
            "recording looks like it contains secrets; no images were rendered or published "
            f"(detected: {', '.join(kinds)}). Change the command and retry. "
            "--redact-host does not redact images."
        )


def final_display(chunks: list[tuple[float, bytes]], *, rows: int, cols: int) -> str:
    """The last screen state of one command's chunks, without trailing padding."""
    snapshots = list(screen_snapshots(chunks, rows=rows, cols=cols))
    if not snapshots:
        return ""
    lines = [line.rstrip() for line in frozen_display(snapshots[-1][1]).splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def screen_text(segments: list[list[tuple[float, bytes]]], *, rows: int, cols: int) -> str:
    """Plain-text transcript: the final screen after each command, separated."""
    screens = [final_display(segment, rows=rows, cols=cols) for segment in segments]
    return "\n\n---\n\n".join(screen for screen in screens if screen)


def compare_sides(
    commands: list[str], before_texts: list[str], after_texts: list[str]
) -> list[str]:
    """Commands whose final screen text is identical before and after."""
    return [
        command
        for command, before, after in zip(commands, before_texts, after_texts, strict=True)
        if before == after
    ]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def resolve_revisions(base: str | None, head: str | None) -> tuple[str, str, list[str]]:
    """Resolve the BEFORE/AFTER shas plus warnings about tree/branch state."""
    notes: list[str] = []
    head = head or "HEAD"
    base = base or git("merge-base", "origin/main", head)
    head_sha = git("rev-parse", head)
    base_sha = git("rev-parse", base)

    status = run(["git", "status", "--porcelain"], cwd=REPO_ROOT, check=True)
    if status.stdout.strip():
        count = len(status.stdout.strip().splitlines())
        notes.append(f"the working tree has {count} uncommitted change(s); they are NOT recorded")

    remote = run(["git", "branch", "-r", "--contains", head_sha], cwd=REPO_ROOT, check=True)
    if not remote.stdout.strip():
        notes.append(f"HEAD ({head_sha[:7]}) is not contained in any remote branch; push it first")
    return base_sha, head_sha, notes


def setup_command(*, workspace: bool, profile: str | None) -> str:
    """Hidden `ug configure` run inside the sandbox before the shown commands.

    OS-managed settings writes are already disabled through UG_SKIP_OS_SETTINGS
    (see src/ucode/managed_files.py); the fake sudo on PATH is a second guard.
    `--use-pat` keeps configure non-interactive against a real workspace.
    """
    if not workspace:
        return ""
    cmd = "ug configure --agents claude --verbose low"
    if profile:
        cmd += f" --profile {profile} --use-pat"
    else:
        warn("workspace mode without --profile may prompt for OAuth inside the recording")
    return cmd


def venv_cache_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    xdg = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(xdg) / "ug-pr-demo"


def build_venv(sha: str, venv_dir: Path) -> Path:
    """Create a venv with ug installed from the repo checkout at `sha`."""
    run(["uv", "venv", str(venv_dir)], timeout=INSTALL_TIMEOUT_SECONDS)
    source = f"git+file://{REPO_ROOT}@{sha}"
    run(
        ["uv", "pip", "install", "--quiet", "--python", str(venv_dir), source],
        timeout=INSTALL_TIMEOUT_SECONDS,
    )
    bin_dir = venv_dir / ("Scripts" if sys.platform == "win32" else "bin")
    if not (bin_dir / ("ug.exe" if sys.platform == "win32" else "ug")).exists():
        raise SystemExit(f"ug was not installed into {venv_dir}")
    return bin_dir


def venv_for_sha(sha: str, cache: Path) -> tuple[Path, bool]:
    """Return (bin_dir, cached) for a per-SHA venv, building it on first use."""
    venv_dir = cache / sha / "venv"
    exe = "ug.exe" if sys.platform == "win32" else "ug"
    bin_dir = venv_dir / ("Scripts" if sys.platform == "win32" else "bin")
    if (bin_dir / exe).exists():
        return bin_dir, True
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    venv_dir.mkdir(parents=True, exist_ok=True)
    return build_venv(sha, venv_dir), False


def make_sandbox(sandbox_dir: Path) -> tuple[str, str]:
    """Create the sandbox HOME tree and a bin dir with a fake `sudo`."""
    home = sandbox_dir / "home"
    for sub in (".config", ".local/share", ".cache"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    bin_dir = sandbox_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    sudo = bin_dir / "sudo"
    sudo.write_text(FAKE_SUDO)
    sudo.chmod(0o755)
    return str(home), str(bin_dir)


def write_manifest(out_dir: Path, payload: dict[str, object]) -> None:
    (out_dir / "manifest.json").write_text(json.dumps(payload, indent=2) + "\n")


def parse_commands(args: argparse.Namespace) -> list[str]:
    commands = list(args.command or [])
    if args.commands_file:
        for line in Path(args.commands_file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                commands.append(line)
    if not commands:
        raise SystemExit("no commands to record; pass --command or --commands-file")
    return commands


def check_commands_for_secrets(commands: list[str], *, redact_host: bool) -> None:
    for command in commands:
        _, remaining = sanitize_capture(command, redact_host=redact_host)
        if remaining:
            listing = "\n".join(f"  - {kind}: {value}" for kind, value in remaining)
            raise SystemExit(
                f"command looks like it contains secrets; not recording:\n{listing}\n"
                "Re-run with --redact-host only for workspace-hostname matches."
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--command", action="append", help="a shell command to record; repeatable")
    parser.add_argument("--commands-file", help="file with one command per line")
    parser.add_argument("--base", help="BEFORE ref (default: merge-base origin/main HEAD)")
    parser.add_argument("--head", help="AFTER ref (default: HEAD)")
    parser.add_argument(
        "--out", help="output directory (default: a new temporary directory outside the checkout)"
    )
    parser.add_argument(
        "--workspace",
        action="store_true",
        help="opt in to real-workspace mode using the developer's Databricks config",
    )
    parser.add_argument("--profile", help="Databricks CLI profile for workspace mode (--use-pat)")
    parser.add_argument(
        "--redact-host",
        action="store_true",
        help="replace workspace hostnames in text captures instead of failing on them",
    )
    parser.add_argument(
        "--allow-identical",
        action="store_true",
        help="record even when BEFORE and AFTER output are identical (escape hatch; "
        "the skill workflow must not use it)",
    )
    parser.add_argument(
        "--sleep", type=float, default=DEFAULT_SLEEP_SECONDS, help="seconds per command"
    )
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS, help="terminal rows")
    parser.add_argument("--cols", type=int, default=DEFAULT_COLS, help="terminal columns")
    parser.add_argument(
        "--venv-cache", help="cache directory for per-commit venvs (default: ~/.cache/ug-pr-demo)"
    )
    args = parser.parse_args(argv)

    commands = parse_commands(args)
    check_commands_for_secrets(commands, redact_host=args.redact_host)

    stage_start = time.monotonic()
    base_sha, head_sha, notes = resolve_revisions(args.base, args.head)
    for note in notes:
        warn(note)

    if args.out:
        out_dir = Path(args.out).resolve()
        if out_dir.is_relative_to(REPO_ROOT):
            warn(f"output directory {out_dir} is inside the checkout and may be committed")
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        temp_root = Path(tempfile.gettempdir()).resolve()
        if temp_root.is_relative_to(REPO_ROOT):
            temp_root = REPO_ROOT.parent
        out_dir = Path(tempfile.mkdtemp(prefix="ug-pr-demo-", dir=temp_root)).resolve()

    workspace_config = None
    if args.workspace:
        workspace_config = os.environ.get("DATABRICKS_CONFIG_FILE") or str(
            Path.home() / ".databrickscfg"
        )
        if not Path(workspace_config).exists():
            raise SystemExit(f"--workspace needs {workspace_config} to exist")

    setup = setup_command(workspace=args.workspace, profile=args.profile)
    cache = venv_cache_root(args.venv_cache)
    sandbox_dir = cache / "sandbox"
    # Start each run from a clean sandbox so prior ug state cannot leak in.
    if sandbox_dir.exists():
        shutil.rmtree(sandbox_dir)
    env_home, sandbox_bin = make_sandbox(sandbox_dir)

    results: dict[str, dict[str, object]] = {}
    recordings: dict[
        str, tuple[Path, list[tuple[float, bytes]], list[list[tuple[float, bytes]]]]
    ] = {}
    for side, sha in (("before", base_sha), ("after", head_sha)):
        side_start = time.monotonic()
        venv_bin, cached = venv_for_sha(sha, cache)
        print(
            f"  {side}: {'reused' if cached else 'built'} venv for {sha[:7]} "
            f"({time.monotonic() - side_start:.1f}s)"
        )

        env = build_sandbox_env(
            sandbox_bin=sandbox_bin,
            venv_bin=str(venv_bin),
            home=env_home,
            workspace_config=workspace_config,
        )
        side_start = time.monotonic()
        chunks: list[tuple[float, bytes]] = []
        segments: list[list[tuple[float, bytes]]] = []
        if setup:
            setup_chunks = capture_command(
                setup,
                env,
                rows=args.rows,
                cols=args.cols,
                timeout=SETUP_SLEEP_SECONDS * 4,
                typed=False,
            )
            setup_chunks += [(setup_chunks[-1][0] + 0.1, b"\r\n")]
            segments.append(setup_chunks)
            chunks += setup_chunks
        for command in commands:
            command_chunks = capture_command(
                command, env, rows=args.rows, cols=args.cols, timeout=COMMAND_TIMEOUT_SECONDS
            )
            command_chunks += [
                (command_chunks[-1][0] + 0.1, b"\r\n"),
                (command_chunks[-1][0] + args.sleep, b""),
            ]
            segments.append(command_chunks)
            chunks += command_chunks
        print(f"  {side}: captured in {time.monotonic() - side_start:.1f}s")

        recordings[side] = (venv_bin, chunks, segments)

    for _, chunks, _ in recordings.values():
        check_recording_for_secrets(chunks, rows=args.rows, cols=args.cols)

    if not args.allow_identical:
        before_texts = [
            final_display(segment, rows=args.rows, cols=args.cols)
            for segment in recordings["before"][2]
        ]
        after_texts = [
            final_display(segment, rows=args.rows, cols=args.cols)
            for segment in recordings["after"][2]
        ]
        identical = compare_sides(commands, before_texts, after_texts)
        if len(identical) == len(commands):
            print(
                "error: BEFORE and AFTER output are identical for every command; "
                "the demo would show no change:",
                file=sys.stderr,
            )
            for command in commands:
                print(f"  - {command}", file=sys.stderr)
            print(
                "Pick commands whose output differs between the merge-base and HEAD "
                "and retry. If the change has no user-visible CLI output, mark the PR "
                "with publish.py --mark-no-demo-needed --reason instead.",
                file=sys.stderr,
            )
            return 3
        for command in identical:
            warn(f"'{command}' produced identical BEFORE and AFTER output; it shows no change")

    render_rows = shared_render_rows(
        [chunks for _, chunks, _ in recordings.values()], rows=args.rows, cols=args.cols
    )
    for side, sha in (("before", base_sha), ("after", head_sha)):
        venv_bin, chunks, segments = recordings[side]
        side_start = time.monotonic()
        gif_path = out_dir / f"{side}.gif"
        png_path = out_dir / f"{side}.png"
        frames = render_and_save(
            chunks,
            gif_path=gif_path,
            png_path=png_path,
            rows=args.rows,
            cols=args.cols,
            render_rows=render_rows,
        )
        capture = screen_text(segments, rows=args.rows, cols=args.cols)
        capture, remaining = sanitize_capture(capture, redact_host=args.redact_host)
        if remaining:
            listing = "\n".join(f"  - {kind}: {value}" for kind, value in remaining)
            raise SystemExit(
                f"{side} capture looks like it contains secrets; nothing was published:\n"
                f"{listing}\nRe-run with --redact-host for workspace-hostname matches."
            )
        (out_dir / f"{side}.txt").write_text(capture)
        version = run([str(venv_bin / "ug"), "--version"]).stdout.strip()
        results[side] = {
            "sha": sha,
            "version": version,
            "frames": frames,
            "gif": str(gif_path),
            "png": str(png_path),
            "txt": str(out_dir / f"{side}.txt"),
        }
        print(
            f"  {side}: {frames} frames rendered ({time.monotonic() - side_start:.1f}s), "
            f"ug {version}"
        )

    write_manifest(
        out_dir,
        {
            "base_sha": base_sha,
            "head_sha": head_sha,
            "commands": commands,
            "mode": "workspace" if args.workspace else "offline",
            "redact_host": args.redact_host,
            "allow_identical": args.allow_identical,
            "rows": args.rows,
            "render_rows": render_rows,
            "cols": args.cols,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "recorder": "built-in pty + pyte + pillow",
            "results": results,
        },
    )
    print(f"done in {time.monotonic() - stage_start:.1f}s")
    print(f"Output directory: {out_dir}")
    print("publish with scripts/pr_demo/publish.py --out <output directory>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
