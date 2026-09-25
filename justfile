# Developer task runner. `just lint` is the single source of truth for the
# checks CI's lint gate runs, so local and CI stay identical.
#
# Install just: `brew install just` (or `uvx --from rust-just just <recipe>`).

# List available recipes.
default:
    @just --list

# Everything the CI lint gate runs. Run this before pushing.
lint: ruff-check ruff-format-check ty

# Lint without fixing (CI-equivalent).
ruff-check:
    uv run ruff check src/ tests/

# Verify formatting without writing files (CI-equivalent).
ruff-format-check:
    uv run ruff format --check src/ tests/

# Type-check the package.
ty:
    uv run ty check src/

# Autofix lint + format in place. Local convenience; not part of the gate.
fix:
    uv run ruff check --fix src/ tests/
    uv run ruff format src/ tests/
