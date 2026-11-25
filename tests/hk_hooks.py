"""Opt-in real hk checks: uv run pytest tests/hk_hooks.py -n 0 -v -s.

Requires hk 2.13.0, Pkl, uv and just on PATH; missing tools fail rather than skip.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


def run(repo, *args, check=True):
    executable = shutil.which(args[0])
    assert executable, f"Required hook-test tool is missing: {args[0]}"
    return subprocess.run(
        [executable, *args[1:]],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=check,
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    for tool in ("hk", "pkl", "uv", "just"):
        assert shutil.which(tool), f"Required hook-test tool is missing: {tool}"
    for name in ("hk.pkl", "justfile"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "hook-check"\nversion = "0.0.0"\nrequires-python = ">=3.12"\n'
        '[dependency-groups]\ndev = ["ruff==0.16.5", "ty==0.0.75"]\n'
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/example.py").write_text("value = 1\n")
    (tmp_path / "tests/example.py").write_text("assert True\n")
    run(tmp_path, "uv", "sync", "--no-frozen")
    run(tmp_path, "git", "init")
    run(tmp_path, "git", "config", "user.name", "Hook test")
    run(tmp_path, "git", "config", "user.email", "hook-test@example.invalid")
    (tmp_path / ".gitignore").write_text(".venv/\n.ruff_cache/\n")
    run(tmp_path, "git", "add", ".")
    run(tmp_path, "git", "commit", "-m", "baseline")
    run(tmp_path, "hk", "install")
    return tmp_path


def snapshot(repo):
    tracked = run(repo, "git", "ls-files", "-z").stdout.split("\0")
    return (
        run(repo, "git", "ls-files", "--stage").stdout,
        run(repo, "git", "status", "--porcelain").stdout,
        {name: (repo / name).read_bytes() for name in tracked if name and (repo / name).is_file()},
        run(repo, "git", "for-each-ref", "refs/stash").stdout,
    )


def hook(repo, name):
    before = snapshot(repo)
    started = time.monotonic()
    result = run(repo, "git", "hook", "run", name, check=False)
    elapsed = time.monotonic() - started
    print(f"{name}: {elapsed:.3f}s, exit {result.returncode}")
    assert snapshot(repo) == before, result.stdout + result.stderr
    return result, elapsed


@pytest.mark.parametrize(
    ("staged", "unstaged", "success"),
    [
        ("value = 2\n", "# unrelated unstaged edit\n", True),
        ("value = 2\n", "not valid python !\n", True),
        ("value=2\n", "# unrelated unstaged edit\n", False),
        ("import os\n\nvalue = 2\n", "# unrelated unstaged edit\n", False),
    ],
    ids=["formatted", "unstaged-syntax-error", "needs-formatting", "lint-error"],
)
def test_pre_commit_preserves_partial_staging(repo, staged, unstaged, success):
    path = repo / "src/example.py"
    path.write_text(staged)
    run(repo, "git", "add", "src/example.py")
    path.write_text(staged + unstaged)
    result, elapsed = hook(repo, "pre-commit")
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    assert elapsed < 9


def test_pre_commit_handles_spaces_and_deletions(repo):
    # Needs formatting on purpose: if the glob silently skipped spaced paths,
    # the hook would pass and this assert would catch the skip.
    (repo / "src/file with spaces.py").write_text("value=2\n")
    run(repo, "git", "add", "src/file with spaces.py")
    run(repo, "git", "rm", "src/example.py")
    result, elapsed = hook(repo, "pre-commit")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "file with spaces.py" in result.stdout + result.stderr
    assert elapsed < 9


@pytest.mark.parametrize("success", [True, False], ids=["clean", "unstaged-format-error"])
def test_pre_push_checks_full_tree(repo, success):
    if not success:
        (repo / "src/example.py").write_text("value=2\n")
    result, elapsed = hook(repo, "pre-push")
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    assert elapsed < 9


@pytest.mark.parametrize("name", ["pre-commit", "pre-push"])
def test_timeout_preserves_index_and_unstaged_edits(repo, name):
    # Delay the checker, not hk's timeout or its staging/recovery settings.
    config = repo / "hk.pkl"
    original = "uv run ruff format --check {{files}}" if name == "pre-commit" else "just lint"
    delay = f'\\"{sys.executable}\\" -c \\"import time; time.sleep(30)\\"'
    config.write_text(config.read_text().replace(original, delay))
    run(repo, "git", "add", "hk.pkl")
    path = repo / "src/example.py"
    path.write_text("value = 2\n")
    run(repo, "git", "add", "src/example.py")
    path.write_text("value = 2\n# unrelated unstaged edit\n")
    result, elapsed = hook(repo, name)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "timed out" in (result.stdout + result.stderr).lower()
    assert 9 <= elapsed < 15
