# smevals-claude-code

A reusable [smevals](https://github.com/prime-radiant-inc/smevals) Runner that drives an isolated headless Claude Code session: it stages fixtures into a temp work dir, runs `claude -p` there under the condition the smevals config names, and copies the worked tree back into the run dir.

## Technology References

@~/.config/technotes/python.md

## How work runs here

This repo is one script plus its tests, built from the desk: plan mode, TDD, direct edits. No OpenSpec, no beads. Issues here track defects and changes in the runner itself; the eval suites that consume it plan in their own backlogs and move their pin when a version ships.

The package is public. Nothing in it, its history, or its commit messages may name private infrastructure, employers, or personal addresses; commits are authored under the GitHub noreply identity set in this clone's git config.

## The contract that matters

The runner's `claude` command line is a measured condition: every eval anchor in a consuming repo was taken under it. A change that alters the argv for an existing invocation shifts every consumer's condition at once, so it is its own change with a version bump, and consumers move their pin deliberately. `tests/test_runner.py` drives the installed command black-box and asserts the argv; keep it that way.

## Version control

Work on a branch; landing on main needs the maintainer's explicit approval.
