# ABOUTME: Project-local command surface for smevals-claude-code.
# ABOUTME: `just` is the default task runner; Python tools run via `uv run`.

# List available recipes
default:
    @just --list

# Check-style recipes run `uv run --locked` so a drifted uv.lock fails them loudly —
# bare `uv run` would silently rewrite the lock before the check ran (per python.md).

# Run tests
test:
    uv run --locked pytest -q

# Lint
lint:
    uv run --locked ruff check

# Format (mutating dev-loop recipe — stays bare so an intentional dep change still syncs)
format:
    uv run ruff format

# One-time per-clone setup: wire pre-commit, sync dependencies
setup:
    ./scripts/dev-setup.sh

# The dev group also carries an audit tool set that no recipe here invokes:
# bandit, import-linter, pip-audit, pydeps, pytest-cov, and vulture. They are
# driven by the review plugin's refactor-audit recipe library, which projects
# deliberately do not import — it is pointed at from outside the repo:
#
#     just --justfile "<review-plugin>/just/python.just" refactor::preflight
