# Unit tests for the contributor pr-demo skill scripts. These import the
# scripts from scripts/pr_demo/ by path (they are not part of the installed
# package), exercise the pure helpers and the pyte/Pillow renderer, and never
# run a real `ug` command or touch the network. The renderer tests skip when
# pyte/Pillow are absent; run them for real with:
#   uv run --with pyte --with pillow pytest tests/test_pr_demo.py
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "scripts" / "pr_demo"
SKILL_PATHS = [
    REPO_ROOT / ".agents" / "skills" / "pr-demo" / "SKILL.md",
    REPO_ROOT / ".claude" / "skills" / "pr-demo" / "SKILL.md",
]


def _load_script(module_name: str, file_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_DIR / file_name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def record() -> Any:
    return _load_script("pr_demo_record", "record.py")


@pytest.fixture(scope="module")
def publish(record: Any) -> Any:
    # publish.py does `import record`; register the path-loaded module so that
    # import resolves without scripts/pr_demo being on sys.path.
    sys.modules.setdefault("record", record)
    return _load_script("pr_demo_publish", "publish.py")


@pytest.fixture(scope="module")
def pyte() -> Any:
    return pytest.importorskip("pyte")


@pytest.fixture(scope="module")
def pil() -> Any:
    return pytest.importorskip("PIL")


def _sandbox_env(record: Any, *, venv_bin: str, home: str) -> dict[str, str]:
    return record.build_sandbox_env(
        sandbox_bin=f"{home}/sandbox/bin",
        venv_bin=venv_bin,
        home=home,
        workspace_config=None,
    )


def _chunks_for(record: Any, output: bytes, *, cadence: float = 0.001) -> list[tuple[float, bytes]]:
    """A deterministic chunk list: synthetic prompt typing plus raw output."""
    typed = record.typing_chunks("ug --help", cadence=cadence)
    offset = typed[-1][0] + 0.05
    return typed + [(offset, output)]


# ---------------------------------------------------------------------------
# Capture helpers
# ---------------------------------------------------------------------------


def test_typing_chunks_have_fixed_cadence_and_end_with_newline(record: Any) -> None:
    line = "$ ug --version"
    chunks = record.typing_chunks("ug --version", cadence=0.02)
    assert chunks[0] == (0.0, b"$")
    assert [len(data) for _, data in chunks[:-1]] == [1] * len(line)
    assert chunks[-1][1] == b"\r\n"
    # Fixed cadence: total typing time depends only on line length.
    assert chunks[-1][0] == pytest.approx(len(line) * 0.02)


def test_sandbox_env_sets_home_color_and_skip_flags(record: Any) -> None:
    env = _sandbox_env(record, venv_bin="/tmp/venv/bin", home="/tmp/home")
    assert env["HOME"] == "/tmp/home"
    assert env["XDG_CONFIG_HOME"] == "/tmp/home/.config"
    assert env["PATH"].startswith("/tmp/home/sandbox/bin:/tmp/venv/bin:")
    assert env["UG_SKIP_OS_SETTINGS"] == "1"
    assert env["TERM"] == "xterm-256color"
    assert env["FORCE_COLOR"] == "1"
    assert "DATABRICKS_CONFIG_FILE" not in env

    with_config = record.build_sandbox_env(
        sandbox_bin="/b", venv_bin="/v", home="/h", workspace_config="/real/databrickscfg"
    )
    assert with_config["DATABRICKS_CONFIG_FILE"] == "/real/databrickscfg"


def test_setup_command_hidden_and_uses_skip_env_profile(record: Any) -> None:
    assert record.setup_command(workspace=False, profile=None) == ""
    workspace_cmd = record.setup_command(workspace=True, profile="dbx")
    assert workspace_cmd == "ug configure --agents claude --verbose low --profile dbx --use-pat"


# ---------------------------------------------------------------------------
# Renderer (requires pyte)
# ---------------------------------------------------------------------------


def test_screen_snapshots_dedupe_unchanged_display(record: Any, pyte: Any) -> None:
    chunks = _chunks_for(record, b"hello\nworld\n")
    states = [
        (t, record.frozen_display(frozen))
        for t, frozen in record.screen_snapshots(chunks, rows=30, cols=100)
    ]
    # Prompt typing produces several states; the final one shows the output.
    assert len(states) > 1
    assert "hello" in states[-1][1]
    assert any("$" in text for _, text in states)


def test_identical_outputs_produce_identical_frames(record: Any, pyte: Any) -> None:
    chunks = _chunks_for(record, b"same output\n")
    first = [
        record.frozen_display(frozen)
        for _, frozen in record.screen_snapshots(chunks, rows=30, cols=100)
    ]
    second = [
        record.frozen_display(frozen)
        for _, frozen in record.screen_snapshots(chunks, rows=30, cols=100)
    ]
    assert first == second


def test_ansi_colors_reach_the_screen_buffer(record: Any, pyte: Any) -> None:
    chunks = [(0.0, b"\x1b[31mERROR\x1b[0m ok\n")]
    states = list(record.screen_snapshots(chunks, rows=5, cols=40))
    frozen = states[-1][1]
    assert record.frozen_display(frozen).splitlines()[0].startswith("ERROR ok")
    assert frozen[0][0][1] == "red"  # fg of the first cell
    assert frozen[0][6][1] == "default"


# ---------------------------------------------------------------------------
# Rendering to images (requires pyte + Pillow)
# ---------------------------------------------------------------------------


def test_resolve_color_names_hex_and_bold(record: Any) -> None:
    assert record.resolve_color(None, "#111111") == "#111111"
    assert record.resolve_color("default", "#111111") == "#111111"
    assert record.resolve_color("aabbcc", "#111111") == "#aabbcc"
    assert record.resolve_color("red", "#111111") == "#cd0000"
    assert record.resolve_color("red", "#111111", bold=True) == "#ff5555"
    assert record.resolve_color("unknown-name", "#111111") == "#111111"


def test_render_screen_size_and_background_pixel(record: Any, pil: Any) -> None:
    import pyte

    screen = pyte.Screen(10, 3)
    pyte.ByteStream(screen).feed(b"hi\n")
    frozen = record.freeze_screen(screen)
    font = record.load_font()
    char_w, line_h = record.cell_metrics(font)
    image = record.render_screen(frozen, font=font, char_w=char_w, line_h=line_h, cols=10, rows=3)
    assert image.size == (10 * char_w, 3 * line_h)
    # The bottom-right cell is untouched: pure background color.
    assert image.getpixel((10 * char_w - 1, 3 * line_h - 1)) == (0x1E, 0x1F, 0x29)


def test_render_and_save_gif_frames_and_size(record: Any, pil: Any, tmp_path: Path) -> None:
    chunks = _chunks_for(record, b"line one\nline two\n")
    gif_path = tmp_path / "demo.gif"
    png_path = tmp_path / "demo.png"
    frames = record.render_and_save(chunks, gif_path=gif_path, png_path=png_path, rows=6, cols=40)
    assert frames > 1
    assert gif_path.exists() and png_path.exists()

    from PIL import Image

    with Image.open(gif_path) as gif:
        assert gif.n_frames == frames
        font = record.load_font()
        char_w, line_h = record.cell_metrics(font)
        assert gif.size == (40 * char_w, 6 * line_h)
    with Image.open(png_path) as png:
        assert png.size == gif.size


def test_frame_durations_hold_final_frame_and_subsample(record: Any) -> None:
    durations = record.frame_durations([0.0, 0.5, 1.0])
    assert durations == [500, 500, int(record.FINAL_HOLD_SECONDS * 1000)]
    times = [float(i) for i in range(20)]
    keep = record.subsample(times, max_frames=5)
    assert keep == [0, 5, 10, 14, 19]
    assert record.subsample([0.0, 0.2, 0.3], max_frames=5) == [0, 1, 2]


# ---------------------------------------------------------------------------
# Secret scanning
# ---------------------------------------------------------------------------

# A PAT-shaped fixture assembled at runtime so no token literal appears in
# source (the pre-commit secret scan would otherwise block the commit).
FAKE_PAT = "dapi" + "0123456789abcdef" * 2


def test_find_secrets_matches_all_kinds(record: Any) -> None:
    text = "\n".join(
        [
            f"token {FAKE_PAT}",
            "Authorization: Bearer abc.def.ghi",
            "jwt eyJhbGciOi.J9.abc-_123",
            "mail first.last@example.com",
            "host dbc-a1b2c3d4-e5f6.cloud.databricks.com",
            "deep https://adb-123456789.0.azuredatabricks.net/?o=1234567890",
        ]
    )
    kinds = sorted(kind for kind, _ in record.find_secrets(text))
    assert kinds == [
        "bearer-token",
        "databricks-host",
        "databricks-host",
        "databricks-pat",
        "email",
        "jwt",
        "workspace-id",
    ]
    assert FAKE_PAT in [
        value for kind, value in record.find_secrets(text) if kind == "databricks-pat"
    ]


def test_find_secrets_clean_text_has_no_matches(record: Any) -> None:
    text = (
        "Usage: ug [OPTIONS] COMMAND [ARGS]...\n"
        "Version: 0.2.1+150.g1f4820b7\n"
        "No such command 'badarg'.\n"
    )
    assert record.find_secrets(text) == []


def test_sanitize_capture_redacts_hosts_but_not_other_secrets(record: Any) -> None:
    text = f"Connected to dbc-a1b2c3d4.cloud.databricks.com with {FAKE_PAT}"
    redacted, remaining = record.sanitize_capture(text, redact_host=True)
    assert "dbc-a1b2c3d4.cloud.databricks.com" not in redacted
    assert "https://<workspace>" in redacted
    assert [kind for kind, _ in remaining] == ["databricks-pat"]

    _, remaining = record.sanitize_capture(
        "host dbc-a1b2c3d4.cloud.databricks.com", redact_host=False
    )
    assert [kind for kind, _ in remaining] == ["databricks-host"]


def test_redact_hosts_replaces_every_host_shape(record: Any) -> None:
    redacted = record.redact_hosts(
        "a dbc-a1b2c3d4.cloud.databricks.com b adb-1.2.azuredatabricks.net "
        "c x.gcp.databricks.com d notdatabricks.com"
    )
    assert "https://<workspace>" in redacted
    assert "databricks.com" not in redacted.replace("notdatabricks.com", "")


# ---------------------------------------------------------------------------
# PR body block
# ---------------------------------------------------------------------------


def _block(publish: Any, head_sha: str, **overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "pr": 42,
        "base_sha": "1111111111",
        "head_sha": head_sha,
        "repo_slug": "databricks/unity-gateway",
        "commands": ["ug --help"],
        "mode": "offline",
        "timestamp": "2026-11-14T00:00:00+00:00",
        "before_text": "before output\n",
        "after_text": "after output\n",
    }
    kwargs.update(overrides)
    return publish.build_pr_block(**kwargs)


def test_apply_pr_block_inserts_into_body_without_block(publish: Any) -> None:
    body = "## Summary\n\nSome description.\n"
    updated = publish.apply_pr_block(body, _block(publish, "2222222222"))
    assert "pr-demo:start sha=2222222222" in updated
    assert updated.startswith("## Summary")
    assert updated.count("pr-demo:start") == 1
    assert "/raw/pr-demo-assets/42/2222222/before.gif" in updated


def test_apply_pr_block_replaces_block_with_different_sha(publish: Any) -> None:
    body = "intro\n\n" + _block(publish, "2222222222") + "\n\noutro\n"
    updated = publish.apply_pr_block(body, _block(publish, "3333333333"))
    assert "sha=3333333333" in updated
    assert "sha=2222222222" not in updated
    assert updated.startswith("intro")
    assert updated.rstrip().endswith("outro")


def test_apply_pr_block_is_idempotent(publish: Any) -> None:
    block = _block(publish, "2222222222")
    once = publish.apply_pr_block("body\n", block)
    twice = publish.apply_pr_block(once, block)
    assert once == twice


def test_asset_url_shape(publish: Any) -> None:
    url = publish.asset_url("databricks/unity-gateway", 42, "2222222", "before.gif")
    assert url == (
        "https://github.com/databricks/unity-gateway/raw/pr-demo-assets/42/2222222/before.gif"
    )


def test_capture_section_diffs_when_sides_differ(publish: Any) -> None:
    section = publish.capture_section("a\nb\n", "a\nc\n")
    assert "<details><summary>Text captures</summary>" in section
    assert "````diff" in section
    assert "-b" in section and "+c" in section
    same = publish.capture_section("a\n", "a\n")
    assert "````text" in same and "````diff" not in same


def test_pr_block_embeds_gifs_and_links_stills(publish: Any) -> None:
    block = _block(publish, "2222222222")
    assert (
        "![before demo](https://github.com/databricks/unity-gateway/raw/pr-demo-assets/42/2222222/before.gif)"
        in block
    )
    assert (
        "![after demo](https://github.com/databricks/unity-gateway/raw/pr-demo-assets/42/2222222/after.gif)"
        in block
    )
    assert "/raw/pr-demo-assets/42/2222222/before.png" in block
    without_stills = _block(publish, "2222222222", stills=False)
    assert ".png" not in without_stills


def test_repo_slug_from_remote(publish: Any) -> None:
    assert publish.repo_slug_from_remote("https://github.com/databricks/unity-gateway.git") == (
        "databricks/unity-gateway"
    )
    assert publish.repo_slug_from_remote("git@github.com:databricks/unity-gateway") == (
        "databricks/unity-gateway"
    )


# ---------------------------------------------------------------------------
# Skill files
# ---------------------------------------------------------------------------


def test_skill_md_copies_are_byte_identical() -> None:
    assert SKILL_PATHS[0].read_bytes() == SKILL_PATHS[1].read_bytes()


def test_skill_md_frontmatter_has_only_name_and_description() -> None:
    for path in SKILL_PATHS:
        text = path.read_text()
        assert text.startswith("---\n"), path
        frontmatter = text.split("---\n", 2)[1]
        keys = [line.split(":", 1)[0].strip() for line in frontmatter.splitlines() if line.strip()]
        assert keys == ["name", "description"], path
        name = [
            line.split(":", 1)[1].strip()
            for line in frontmatter.splitlines()
            if line.startswith("name:")
        ][0]
        assert name == path.parent.name == "pr-demo"


def test_skill_md_documents_uv_only_recorder() -> None:
    text = SKILL_PATHS[0].read_text()
    assert "git rev-parse --show-toplevel" in text
    assert "uv run --with pyte --with pillow python" in text
    assert "scripts/pr_demo/record.py" in text
    assert "scripts/pr_demo/publish.py" in text
    assert "vhs" not in text.lower()
    assert "ttyd" not in text.lower()
    assert "ffmpeg" not in text.lower()
