"""Behavior checks for the contributor-only recording and publishing scripts."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
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
    sys.modules.setdefault("record", record)
    return _load_script("pr_demo_publish", "publish.py")


@pytest.fixture
def recorder(record: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Control the git/install/command boundaries while running the real orchestration."""
    monkeypatch.setattr(record, "resolve_revisions", lambda *_: ("base", "head", []))
    monkeypatch.setattr(record, "venv_for_sha", lambda sha, cache: (tmp_path / sha, True))
    monkeypatch.setattr(record, "run", lambda *a, **kw: SimpleNamespace(stdout="ug 1.0"))
    monkeypatch.setattr(record.tempfile, "tempdir", str(tmp_path))
    return record


def _record_args(tmp_path: Path) -> list[str]:
    # --allow-identical keeps the renderer checks focused: the identical-output
    # gate itself is covered by test_all_identical_captures_exit_with_distinct_code.
    return [
        "--command",
        "ug --help",
        "--venv-cache",
        str(tmp_path / "cache"),
        "--sleep",
        "0",
        "--allow-identical",
    ]


def test_output_directory_defaults_outside_checkout_and_warns_inside(
    recorder: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    monkeypatch.setattr(recorder, "capture_command", lambda *a, **kw: [(0.0, b"hello")])
    # The output-location contract does not require the optional renderer dependencies.
    monkeypatch.setattr(recorder, "check_recording_for_secrets", lambda *a, **kw: None)
    monkeypatch.setattr(recorder, "shared_render_rows", lambda *a, **kw: 6)
    monkeypatch.setattr(recorder, "render_and_save", lambda *a, **kw: 1)
    monkeypatch.setattr(recorder, "screen_text", lambda *a, **kw: "hello")
    assert recorder.main(_record_args(tmp_path)) == 0
    out = Path(capsys.readouterr().out.split("Output directory: ")[1].splitlines()[0])
    assert out.is_dir() and not out.is_relative_to(REPO_ROOT)
    assert (out / "manifest.json").exists()
    assert recorder.main(_record_args(tmp_path)) == 0
    second = Path(capsys.readouterr().out.split("Output directory: ")[1].splitlines()[0])
    assert second.is_dir() and second != out and not second.is_relative_to(REPO_ROOT)

    # Point the checkout boundary at tmp_path; avoid writing into the real checkout.
    monkeypatch.setattr(recorder, "REPO_ROOT", tmp_path)
    # Even a TMPDIR inside the checkout must not put generated assets there.
    assert recorder.main(_record_args(tmp_path)) == 0
    fallback = Path(capsys.readouterr().out.split("Output directory: ")[1].splitlines()[0])
    try:
        assert fallback.is_dir() and not fallback.is_relative_to(tmp_path)
    finally:
        shutil.rmtree(fallback)
    target = tmp_path / "output"
    assert recorder.main([*_record_args(tmp_path), "--out", str(target)]) == 0
    assert "inside the checkout" in capsys.readouterr().err
    assert (target / "manifest.json").exists()


def test_same_output_produces_same_animated_images_and_text(
    recorder: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("pyte")
    Image = pytest.importorskip("PIL.Image")
    chunks = [(0.0, b"$ ug --help\r\n"), (0.3, b"\x1b[31mERROR\x1b[0m ok\r\n")]
    environments = []

    def capture(command: str, env: dict[str, str], **kwargs: Any) -> list[tuple[float, bytes]]:
        environments.append(env)
        return list(chunks)

    monkeypatch.setattr(recorder, "capture_command", capture)
    out = tmp_path / "out"
    assert recorder.main([*_record_args(tmp_path), "--out", str(out)]) == 0
    assert environments[0]["HOME"].startswith(str(tmp_path))
    assert all(env["UG_SKIP_OS_SETTINGS"] == "1" for env in environments)
    assert (out / "before.txt").read_text() == (out / "after.txt").read_text()
    assert (out / "after.txt").read_text().splitlines() == ["$ ug --help", "ERROR ok"]
    with Image.open(out / "before.png") as before, Image.open(out / "after.png") as after:
        assert before.size == after.size
        assert before.tobytes() == after.tobytes()
        assert any(
            r > 2 * g and r > 2 * b
            for _, (r, g, b) in after.convert("RGB").getcolors(maxcolors=after.width * after.height)
        )
    with Image.open(out / "before.gif") as before, Image.open(out / "after.gif") as after:
        assert before.n_frames == after.n_frames > 1
        assert before.size == after.size
        for frame in range(before.n_frames):
            before.seek(frame)
            after.seek(frame)
            assert before.convert("RGB").tobytes() == after.convert("RGB").tobytes()


def test_crop_uses_all_frames_from_both_sides(
    recorder: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("pyte")
    Image = pytest.importorskip("PIL.Image")
    short = [(0.0, b"$ ug --help\r\n"), (0.3, b"short\r\n")]
    tall_then_cleared = [
        (0.0, b"$ ug --help\r\n"),
        (0.3, b"\x1b[12;1Htransient"),
        (0.6, b"\x1b[2J\x1b[Hfinal"),
    ]
    full_height = [(0.0, b"\x1b[30;1Hbottom"), (0.3, b"\x1b[2J\x1b[Hfinal")]
    captures = iter([short, short, short, tall_then_cleared, short, full_height])
    monkeypatch.setattr(recorder, "capture_command", lambda *a, **kw: list(next(captures)))
    sizes = []
    for name in ("short", "tall", "full"):
        out = tmp_path / name
        assert recorder.main([*_record_args(tmp_path), "--out", str(out)]) == 0
        with Image.open(out / "before.png") as before, Image.open(out / "after.png") as after:
            assert before.size == after.size
            sizes.append(before.size)
        with Image.open(out / "after.gif") as gif:
            assert gif.size == sizes[-1] and gif.n_frames > 1
    assert sizes[0][0] == sizes[1][0] == sizes[2][0]
    assert sizes[2][1] / 5 <= sizes[0][1] < sizes[1][1] < sizes[2][1]
    assert (tmp_path / "tall" / "after.txt").read_text() == "final"


def test_compare_sides_names_identical_commands(record: Any) -> None:
    assert record.compare_sides(["a", "b"], ["same", "x"], ["same", "y"]) == ["a"]
    assert record.compare_sides(["a"], ["x"], ["y"]) == []


def test_all_identical_captures_exit_with_distinct_code(
    recorder: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    pytest.importorskip("pyte")
    chunks = [(0.0, b"$ ug --help\r\n"), (0.3, b"same output\r\n")]
    monkeypatch.setattr(recorder, "capture_command", lambda *a, **kw: list(chunks))
    out = tmp_path / "out"
    args = [
        "--command",
        "ug --help",
        "--venv-cache",
        str(tmp_path / "cache"),
        "--sleep",
        "0",
        "--out",
        str(out),
    ]
    assert recorder.main(args) == 3
    assert "identical" in capsys.readouterr().err
    assert not list(out.glob("*"))  # nothing rendered, captured, or written


FAKE_PAT = "dapi" + "0123456789abcdef" * 2


def test_recording_blocks_secrets_in_intermediate_screen_even_when_host_redaction_requested(
    recorder: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("pyte")
    chunks = [(0.0, b"dbc-a1b2c3d4.cloud.databricks.com"), (0.4, b"\x1b[2J\x1b[Hclean")]
    monkeypatch.setattr(recorder, "capture_command", lambda *a, **kw: list(chunks))
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="no images were rendered"):
        recorder.main([*_record_args(tmp_path), "--out", str(out), "--redact-host"])
    assert not list(out.glob("*.gif")) and not list(out.glob("*.png"))


def test_skip_reason_secret_scan_blocks_before_any_write(publish: Any) -> None:
    with pytest.raises(SystemExit, match="secrets"):
        publish.scan_secret_text("reason", f"leak {FAKE_PAT}")
    with pytest.raises(SystemExit, match="secrets"):
        publish.scan_secret_text("tried command", f"ug --token {FAKE_PAT}")


@pytest.mark.parametrize("where", ["capture", "command"])
def test_secret_scan_blocks_publish_before_any_git_or_gh(
    publish: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, where: str
) -> None:
    command = f"ug --token {FAKE_PAT}" if where == "command" else "ug --help"
    (tmp_path / "manifest.json").write_text(
        json.dumps({"base_sha": "base", "head_sha": "head", "commands": [command]})
    )
    (tmp_path / "before.txt").write_text("clean output")
    (tmp_path / "after.txt").write_text(
        f"token {FAKE_PAT}" if where == "capture" else "different clean output"
    )
    calls = []
    monkeypatch.setattr(publish, "run", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(publish, "git", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(publish, "gh", lambda *a, **kw: calls.append((a, kw)))
    with pytest.raises(SystemExit):
        publish.main(["--out", str(tmp_path), "--pr", "42"])
    assert calls == []


def test_publish_refuses_all_identical_manifest(
    publish: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps({"base_sha": "base", "head_sha": "head", "commands": ["ug --help"]})
    )
    (tmp_path / "before.txt").write_text("same output")
    (tmp_path / "after.txt").write_text("same output")
    calls = []
    monkeypatch.setattr(publish, "run", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(publish, "git", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(publish, "gh", lambda *a, **kw: calls.append((a, kw)))
    with pytest.raises(SystemExit, match="identical"):
        publish.main(["--out", str(tmp_path), "--pr", "42", "--dry-run"])
    assert calls == []


@pytest.mark.parametrize(
    ("paths", "skip", "blockers"),
    [
        (["tests/test_cli.py"], True, []),
        ([".github/workflows/check.yml"], True, []),
        (["tests/helpers.py", ".github/workflows/check.yml"], True, []),
        (["src/ucode/cli.py"], False, ["src/ucode/cli.py"]),
        ([".github/scripts/foo.py"], False, [".github/scripts/foo.py"]),
        (["docs/guide.md"], False, ["docs/guide.md"]),
        ([], False, []),
        (["src/ucode/config.yaml"], True, []),
        (["app/conftest.py", "app/test_thing.py", "app/thing_test.py"], True, []),
        (["src/old.py", "tests/test_old.py"], False, ["src/old.py"]),
    ],
)
def test_skip_classifier(publish: Any, paths: list[str], skip: bool, blockers: list[str]) -> None:
    assert publish.classify_skip(paths) == (skip, blockers)


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


def test_pr_body_insert_replace_and_idempotency(publish: Any) -> None:
    body = "## Summary\n\nSome description.\n"
    first = _block(publish, "2222222222")
    inserted = publish.apply_pr_block(body, first)
    assert inserted == body.rstrip("\n") + "\n\n" + first + "\n"
    assert publish.apply_pr_block(inserted, first) == inserted
    with_outro = inserted + "\n## Notes\n\nKeep this text.\n"
    second = _block(publish, "3333333333")
    replaced = publish.apply_pr_block(with_outro, second)
    assert replaced == with_outro.replace(first, second)
    assert publish.apply_pr_block(replaced, second) == replaced
    prose = "Edits the `<!-- pr-demo:start sha=... -->` / `<!-- pr-demo:end -->` markers.\n"
    assert publish.apply_pr_block(prose, first) == prose + "\n" + first + "\n"


def test_published_block_has_images_and_text_diff(publish: Any) -> None:
    block = _block(publish, "2222222222")
    for side in ("before", "after"):
        assert f"/raw/pr-demo-assets/42/2222222/{side}.gif" in block
        assert f"/raw/pr-demo-assets/42/2222222/{side}.png" in block
    assert "````diff" in block and "-before output" in block and "+after output" in block
    same = _block(publish, "2222222222", before_text="same", after_text="same", stills=False)
    assert "````text" in same and ".png" not in same


def test_normalize_rationale_is_one_bounded_line(publish: Any) -> None:
    assert (
        publish.normalize_rationale("  internal\nrefactor \twith  spaces  ")
        == "internal refactor with spaces"
    )
    capped = publish.normalize_rationale("x" * 500)
    assert len(capped) <= 300 and capped.endswith("...")


def test_rationale_line_replace_append_idempotent_and_removal(publish: Any) -> None:
    body = "## Summary\n\nBody text.\n"
    appended = publish.apply_rationale_line(body, "internal refactor")
    assert appended == body + "\nNO_PR_DEMO_NEEDED_RATIONALE=internal refactor\n"
    # An existing line is replaced in place; the rest of the body is preserved.
    replaced = publish.apply_rationale_line(
        "## Summary\n\nNO_PR_DEMO_NEEDED_RATIONALE=old\n\nKeep.\n", "new"
    )
    assert replaced == "## Summary\n\nNO_PR_DEMO_NEEDED_RATIONALE=new\n\nKeep.\n"
    assert publish.apply_rationale_line(replaced, "new") == replaced
    # Extra and code-fenced copies are removed, leaving exactly one top-level line.
    deduped = publish.apply_rationale_line(
        "NO_PR_DEMO_NEEDED_RATIONALE=a\n```\nNO_PR_DEMO_NEEDED_RATIONALE=b\n```\ntail\n", "c"
    )
    assert deduped.count("NO_PR_DEMO_NEEDED_RATIONALE=") == 1
    assert deduped.startswith("NO_PR_DEMO_NEEDED_RATIONALE=c\n") and "tail\n" in deduped
    # Publishing a real demo removes the stale line again.
    removed, changed = publish.remove_rationale_line(appended)
    assert changed and removed == body
    unchanged, changed = publish.remove_rationale_line(body)
    assert not changed and unchanged == body


def test_no_demo_block_carries_reason_and_tried_and_replaces_idempotently(publish: Any) -> None:
    block = publish.build_no_demo_block(reason="internal refactor", tried=["ug --help"])
    assert block.startswith("<!-- pr-demo:start")
    assert block.endswith(publish.END_MARKER)
    assert "Reason: internal refactor" in block and "- `ug --help`" in block
    body = publish.apply_pr_block("## Summary\n\nIntro.\n", block)
    assert publish.apply_pr_block(body, block) == body


def test_skill_copies_and_frontmatter_contract() -> None:
    assert SKILL_PATHS[0].read_bytes() == SKILL_PATHS[1].read_bytes()
    for path in SKILL_PATHS:
        text = path.read_text()
        assert text.startswith("---\n")
        frontmatter = text.split("---\n", 2)[1]
        pairs = [line.split(":", 1) for line in frontmatter.splitlines() if line.strip()]
        entries = dict(pairs)
        assert len(pairs) == 2 and set(entries) == {"name", "description"}
        assert entries["name"].strip() == path.parent.name
