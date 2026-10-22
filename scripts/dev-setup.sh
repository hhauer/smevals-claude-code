#!/usr/bin/env bash
# ABOUTME: One-time dev environment setup for a Python project.
# ABOUTME: Run after cloning to install git hooks and sync dependencies.

set -euo pipefail

cd "$(dirname "$0")/.."

echo "Installing git hooks..."
if [ ! -f ~/.hooks/python/pre-commit ]; then
  echo "ERROR: ~/.hooks/python/pre-commit not found — is your dotfiles bootstrap complete?"
  exit 1
fi
# --git-path resolves the shared hooks directory; in a linked worktree .git is
# a file, so a literal .git/hooks path does not exist there.
ln -sf ~/.hooks/python/pre-commit "$(git rev-parse --git-path hooks)/pre-commit"
echo "  pre-commit hook installed"

echo "Installing Git LFS hooks..."
# --force repairs LFS's hooks (post-checkout, post-commit, post-merge, pre-push)
# if another tool overwrote them; dev-setup owns .git/hooks, so overwriting is safe.
git lfs install --force
echo "  LFS hooks installed"

echo "Syncing dependencies..."
# --locked: a post-clone install reproduces the committed lock rather than
# relocking it, matching the TypeScript hook's pnpm install --frozen-lockfile.
uv sync --locked --dev
echo "  dependencies synced"

echo "Dev setup complete."
