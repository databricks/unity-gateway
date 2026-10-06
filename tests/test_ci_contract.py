"""Contracts for the top-level CI workflow."""

from pathlib import Path


def test_e2e_matrix_discovers_the_whole_dedicated_cuj_folder():
    workflow = (Path(__file__).parent.parent / ".github/workflows/ci.yml").read_text()
    job = workflow.split("\n  e2e-shards:\n", 1)[1].split("\n  e2e:\n", 1)[0]

    assert "pytest --confcutdir=tests/e2e_cuj tests/e2e_cuj" in job
    assert "test_cuj_" not in job
    assert "group: cuj" in job
