# Packaging tests: the contributor pr-demo skill must not ship to end users.
# pyproject force-includes the repo-root skills/ directory into the wheel; the
# contributor skill lives in .agents/.claude and scripts/ instead.
from __future__ import annotations

import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRIBUTOR_SKILL = "pr-demo"
SHIPPED_SKILLS = {"smart-router", "smart-router-orchestrator"}


def test_wheel_ships_only_shipped_skills_and_not_contributor_skill(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv, "uv must be on PATH to build the wheel"
    out_dir = tmp_path / "dist"
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(out_dir)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    wheels = list(out_dir.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()

    assert not any(name.startswith((".agents/", ".claude/", "scripts/")) for name in names)
    assert not any(CONTRIBUTOR_SKILL in name for name in names)

    shipped = {
        name.split("/")[1]
        for name in names
        if name.startswith("skills/") and len(name.split("/")) > 2
    }
    assert shipped == SHIPPED_SKILLS


def test_force_include_covers_only_the_shipped_skills_directory() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert set(force_include) == {"skills"}
