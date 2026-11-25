"""Opt-in real hk checks: uv run pytest tests/hk_hooks.py -n 0 -v -s.

Requires hk 2.13.0, Pkl, uv and just on PATH; missing tools fail rather than skip.
"""

import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
GIT_ROUTING_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


def run(repo, *args, check=True, timeout=30):
    executable = shutil.which(args[0])
    assert executable, f"Required hook-test tool is missing: {args[0]}"
    env = os.environ.copy()
    for name in GIT_ROUTING_ENV:
        env.pop(name, None)
    return subprocess.run(
        [executable, *args[1:]],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        env=env,
    )


@pytest.fixture
def inherited_git_routing(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    external = tmp_path / "external"
    external.mkdir()
    run(external, "git", "init")
    run(external, "git", "config", "user.name", "External repository")
    run(external, "git", "config", "user.email", "external@example.invalid")
    (external / "outside.py").write_text("outside = 1\n")
    run(external, "git", "add", ".")
    run(external, "git", "commit", "-m", "external baseline")
    external_state = snapshot(external)
    monkeypatch.setenv("GIT_DIR", str(external / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(external))
    monkeypatch.setenv("GIT_INDEX_FILE", str(external / ".git" / "index"))
    return external, external_state


@pytest.fixture
def repo(tmp_path, monkeypatch, inherited_git_routing):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    for tool in ("hk", "pkl", "uv", "just"):
        assert shutil.which(tool), f"Required hook-test tool is missing: {tool}"
    for name in ("hk.pkl", "justfile"):
        shutil.copyfile(ROOT / name, repo_path / name)
    root_pyproject = (ROOT / "pyproject.toml").read_text()
    ruff_config = root_pyproject[root_pyproject.index("[tool.ruff]") :]
    dev_dependencies = tomllib.loads(root_pyproject)["dependency-groups"]["dev"]
    lint_dependencies = [
        dependency
        for dependency in dev_dependencies
        if dependency.split("=", 1)[0] in {"ruff", "ty"}
    ]
    (repo_path / "pyproject.toml").write_text(
        '[project]\nname = "hook-check"\nversion = "0.0.0"\nrequires-python = ">=3.12"\n'
        + "[dependency-groups]\ndev = ["
        + ", ".join(f'"{dependency}"' for dependency in lint_dependencies)
        + "]\n"
        + ruff_config
    )
    (repo_path / "src").mkdir()
    (repo_path / "tests").mkdir()
    (repo_path / "src/example.py").write_text("value = 1\n")
    (repo_path / "tests/example.py").write_text("assert True\n")
    run(repo_path, "uv", "sync", "--no-frozen", timeout=120)
    run(repo_path, "git", "init")
    run(repo_path, "git", "config", "user.name", "Hook test")
    run(repo_path, "git", "config", "user.email", "hook-test@example.invalid")
    (repo_path / ".gitignore").write_text(".venv/\n.ruff_cache/\n")
    run(repo_path, "git", "add", ".")
    run(repo_path, "git", "commit", "-m", "baseline")
    run(repo_path, "hk", "install")
    return repo_path


def hook_timeout_seconds(repo):
    values = {
        int(value) for value in re.findall(r'timeout = "(\d+)s"', (repo / "hk.pkl").read_text())
    }
    assert len(values) == 1, f"Expected one shared hook timeout, found {values}"
    return values.pop()


def snapshot(repo):
    tracked = run(repo, "git", "ls-files", "-z").stdout.split("\0")
    return (
        run(repo, "git", "rev-parse", "HEAD").stdout,
        run(repo, "git", "config", "--local", "--list", "--show-origin").stdout,
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


def test_repository_fixture_ignores_inherited_git_routing(repo, inherited_git_routing):
    external, external_state = inherited_git_routing
    assert run(repo, "git", "rev-parse", "HEAD").stdout
    assert snapshot(external) == external_state


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
    assert elapsed < hook_timeout_seconds(repo)


def test_pre_commit_rejects_invalid_spaced_path(repo):
    # Needs formatting on purpose: if the glob silently skipped spaced paths,
    # the hook would pass and this assert would catch the skip.
    (repo / "src/file with spaces.py").write_text("value=2\n")
    run(repo, "git", "add", "src/file with spaces.py")
    result, elapsed = hook(repo, "pre-commit")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "file with spaces.py" in result.stdout + result.stderr
    assert elapsed < hook_timeout_seconds(repo)


def test_pre_commit_accepts_clean_spaced_path_and_deletions(repo):
    # Valid content under a spaced name must pass: a quoting bug that splits
    # the path would point ruff at missing files and fail here.
    (repo / "src/clean file.py").write_text("value = 2\n")
    run(repo, "git", "add", "src/clean file.py")
    run(repo, "git", "rm", "src/example.py")
    result, elapsed = hook(repo, "pre-commit")
    assert result.returncode == 0, result.stdout + result.stderr
    assert elapsed < hook_timeout_seconds(repo)


def test_pre_commit_never_re_resolves_a_stale_manifest(repo):
    # UV_FROZEN=1 makes uv use uv.lock as-is: a manifest that drifts from it
    # must not trigger a re-resolve, so the hook passes and uv.lock keeps its
    # bytes (hook() compares the tracked files, uv.lock included, across the run).
    config = repo / "pyproject.toml"
    manifest = config.read_text()
    dev_dependencies = tomllib.loads(manifest)["dependency-groups"]["dev"]
    ruff_pin = next(
        dependency for dependency in dev_dependencies if dependency.startswith("ruff==")
    )
    match = re.fullmatch(r"(ruff==)([0-9]+(?:\.[0-9]+)*)", ruff_pin)
    assert match
    version = match.group(2).split(".")
    version[-1] = str(int(version[-1]) + 1)
    stale_pin = match.group(1) + ".".join(version)
    assert manifest.count(ruff_pin) == 1
    config.write_text(manifest.replace(ruff_pin, stale_pin, 1))
    run(repo, "git", "add", "pyproject.toml")
    path = repo / "src/example.py"
    path.write_text("value = 2\n")
    run(repo, "git", "add", "src/example.py")
    result, elapsed = hook(repo, "pre-commit")
    assert result.returncode == 0, result.stdout + result.stderr
    assert elapsed < hook_timeout_seconds(repo)


@pytest.mark.parametrize("success", [True, False], ids=["clean", "unstaged-format-error"])
def test_pre_push_checks_full_tree(repo, success):
    if not success:
        (repo / "src/example.py").write_text("value=2\n")
    result, elapsed = hook(repo, "pre-push")
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    assert elapsed < hook_timeout_seconds(repo)


@pytest.mark.parametrize("name", ["pre-commit", "pre-push"])
def test_timeout_preserves_index_and_unstaged_edits(repo, name):
    # Delay the checker, not hk's timeout or its staging/recovery settings.
    config = repo / "hk.pkl"
    original = "uv run ruff format --check {{files}}" if name == "pre-commit" else "just lint"
    python_executable = Path(sys.executable).as_posix()
    delay = f'\\"{python_executable}\\" -c \\"import time; time.sleep(20)\\"'
    # Seed the user's own stash entry while the normal hook is still fast: hk's
    # git-stash recovery must leave it intact alongside its own push/pop
    # (hook() compares refs/stash across the run).
    seed = repo / "src/seed.py"
    seed.write_text("seed = 1\n")
    run(repo, "git", "add", "src/seed.py")
    run(repo, "git", "commit", "-m", "seed file")
    seed.write_text("seed = 2\n")
    run(repo, "git", "stash", "push", "src/seed.py")
    config_text = config.read_text()
    assert config_text.count(original) == 1
    config.write_text(config_text.replace(original, delay, 1))
    run(repo, "git", "add", "hk.pkl")
    path = repo / "src/example.py"
    path.write_text("value = 2\n")
    run(repo, "git", "add", "src/example.py")
    path.write_text("value = 2\n# unrelated unstaged edit\n")
    result, elapsed = hook(repo, name)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "timed out" in (result.stdout + result.stderr).lower()
    hook_timeout = hook_timeout_seconds(repo)
    assert hook_timeout <= elapsed < hook_timeout + 6
