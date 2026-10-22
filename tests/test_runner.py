# ABOUTME: Black-box contract tests for the smevals-claude-code runner: a stubbed claude on PATH
# ABOUTME: records cwd, argv, and env so the isolation and condition contracts are asserted exactly.

import os
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path

import pytest

# The installed console script beside the interpreter running the tests: the entry point
# consumers invoke, never a PATH lookup that could find some other install.
RUNNER = Path(sys.executable).parent / "smevals-claude-code"
REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER_LINE = f"runner: smevals-claude-code {version('smevals-claude-code')}\n"

# The claude stub records its cwd and argv, edits a staged file the way a subject session
# would (one edit, one new file), then exits with STUB_EXIT. It prints the JSON result record
# claude -p --output-format json prints (or STUB_RAW verbatim, for a session whose output is not
# a result record). STUB_SLEEP simulates a hung session for the watchdog test; the TERM trap and
# backgrounded sleep model the real claude CLI dying promptly on SIGTERM.
STUB = """#!/usr/bin/env bash
set -euo pipefail
trap 'exit 143' TERM
printf '%s\\n' "$PWD" > "$STUB_LOG.pwd"
printf '%s\\n' "$@" > "$STUB_LOG.argv"
env | grep '^SMEVALS_' > "$STUB_LOG.env" || true
prev=""
for a in "$@"; do
  if [[ $prev == --append-system-prompt-file ]]; then cp "$a" "$STUB_LOG.ctx"; fi
  prev="$a"
done
sleep "${STUB_SLEEP:-0}" </dev/null >/dev/null 2>&1 &
wait "$!" || true
if [[ -f staged/fixture.md ]]; then
  echo "stub edit" >> staged/fixture.md
fi
echo "stub artifact" > stub-marker.md
if [[ -n ${STUB_HOSTILE:-} ]]; then
  # A subject that plants a module and squats the reserved names.
  printf 'raise SystemExit("hijacked json module")\\n' > json.py
  ln -s "$STUB_HOSTILE" subject-condition.md
  ln -s "$STUB_HOSTILE" subject-context.md
fi
if [[ -n ${STUB_RAW:-} ]]; then
  printf '%s\\n' "$STUB_RAW"
else
  printf '%s\\n' \\
    '{"type":"result","result":"STUB-RESULT\\ntwo lines","modelUsage":{"stub-model-id":{}}}'
fi
exit "${STUB_EXIT:-0}"
"""


class Scratch:
    """A fake smevals run dir, a corpus, and the claude stub on PATH."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.run_dir = root / "run"
        self.run_dir.mkdir()
        bin_dir = root / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "claude"
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.corpus = root / "corpus"
        (self.corpus / "staged").mkdir(parents=True)
        (self.corpus / "staged" / "fixture.md").write_text("fixture body\n")
        (root / "single-file.md").write_text("single file body\n")
        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{bin_dir}:{self.env['PATH']}",
                "STUB_LOG": str(root / "stub-log"),
                "SMEVALS_MODEL": "stub-model",
            }
        )

    def run(self, args: list[str], prompt: str = "PROMPT-TEXT") -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(RUNNER), *args],
            input=prompt,
            env=self.env,
            cwd=self.run_dir,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def argv(self) -> list[str]:
        argv_file = self.root / "stub-log.argv"
        return argv_file.read_text().splitlines() if argv_file.is_file() else []

    def claude_invoked(self) -> bool:
        return (self.root / "stub-log.pwd").is_file()

    def condition(self) -> str:
        path = self.run_dir / "subject-condition.md"
        return path.read_text() if path.is_file() else "missing"


@pytest.fixture
def scratch(tmp_path: Path) -> Scratch:
    return Scratch(tmp_path)


def test_isolated_session(scratch: Scratch) -> None:
    # Stands in for any scalar task field smevals exports: a task's expected: field is a
    # grading key, so none of them may reach the subject.
    scratch.env["SMEVALS_TASK_EXPECTED"] = "the answer key"
    proc = scratch.run(["--stage", str(scratch.corpus), "--", "--disallowedTools", "Bash"])
    assert proc.stderr == ""
    assert proc.returncode == 0
    assert proc.stdout == "STUB-RESULT\ntwo lines\n"
    assert scratch.condition() == RUNNER_LINE + "model: stub-model\nresolved: stub-model-id\n"
    assert scratch.claude_invoked()
    stub_pwd = Path((scratch.root / "stub-log.pwd").read_text().strip()).resolve()
    assert stub_pwd != scratch.run_dir.resolve()
    assert REPO_ROOT not in stub_pwd.parents
    assert stub_pwd != REPO_ROOT
    argv = scratch.argv()
    assert argv[:9] == [
        "-p",
        "--safe-mode",
        "--model",
        "stub-model",
        "--permission-mode",
        "acceptEdits",
        "--output-format",
        "json",
        "--disallowedTools",
    ]
    assert argv[argv.index("--disallowedTools") + 1] == "Bash"
    assert argv[-2:] == ["--", "PROMPT-TEXT"]
    assert not stub_pwd.exists(), "work dir cleaned up after the run"
    assert (scratch.root / "stub-log.env").read_text() == "", "no SMEVALS_* vars reach the subject"
    fixture = scratch.run_dir / "staged" / "fixture.md"
    assert fixture.is_file()
    assert "stub edit" in fixture.read_text()
    assert (scratch.run_dir / "stub-marker.md").is_file()


def test_single_file_staging(scratch: Scratch) -> None:
    proc = scratch.run(["--stage", str(scratch.root / "single-file.md")])
    assert proc.returncode == 0
    staged = scratch.run_dir / "single-file.md"
    assert staged.is_file()
    assert "single file body" in staged.read_text()


def test_failing_session_propagates_exit_and_still_copies_back(scratch: Scratch) -> None:
    scratch.env["STUB_EXIT"] = "7"
    proc = scratch.run(["--stage", str(scratch.corpus)])
    assert proc.returncode == 7
    fixture = scratch.run_dir / "staged" / "fixture.md"
    assert fixture.is_file()
    assert "stub edit" in fixture.read_text()


def test_watchdog_kills_a_hung_session(scratch: Scratch) -> None:
    scratch.env["STUB_SLEEP"] = "30"
    start = time.monotonic()
    proc = scratch.run(["--stage", str(scratch.corpus), "--timeout", "1"])
    elapsed = time.monotonic() - start
    assert proc.returncode != 0
    assert elapsed < 20
    assert proc.stderr == ""
    assert (scratch.run_dir / "staged" / "fixture.md").is_file()
    assert scratch.condition() == RUNNER_LINE + "model: stub-model\n"
    assert proc.stdout == ""


def test_hostile_worked_tree(scratch: Scratch) -> None:
    victim = scratch.root / "victim.md"
    victim.write_text("operator file\n")
    scratch.env["STUB_HOSTILE"] = str(victim)
    proc = scratch.run(
        ["--stage", str(scratch.corpus), "--context", str(scratch.root / "single-file.md")]
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "STUB-RESULT\ntwo lines\n", "a planted json.py is not imported"
    assert victim.read_text() == "operator file\n"
    condition = scratch.run_dir / "subject-condition.md"
    assert not condition.is_symlink()
    assert condition.is_file()
    assert scratch.condition() == RUNNER_LINE + "model: stub-model\nresolved: stub-model-id\n"
    context = scratch.run_dir / "subject-context.md"
    assert not context.is_symlink()
    assert context.is_file()
    assert "single file body" in context.read_text()


def test_output_that_is_not_a_result_record_passes_through(scratch: Scratch) -> None:
    scratch.env["STUB_RAW"] = "not a result record"
    proc = scratch.run(["--stage", str(scratch.corpus)])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "not a result record\n"
    assert scratch.condition() == RUNNER_LINE + "model: stub-model\n"


def test_missing_smevals_model_fails_fast(scratch: Scratch) -> None:
    del scratch.env["SMEVALS_MODEL"]
    proc = scratch.run([])
    assert proc.returncode != 0
    assert "SMEVALS_MODEL" in proc.stderr


def test_missing_stage_path_fails_before_claude(scratch: Scratch) -> None:
    proc = scratch.run(["--stage", str(scratch.root / "no-such-path")])
    assert proc.returncode != 0
    assert "no-such-path" in proc.stderr
    assert not scratch.claude_invoked()


def test_subject_context_is_assembled_with_harness_framing(scratch: Scratch) -> None:
    # A fake operator home: the global file, a Developer-tree file that @-imports a technote
    # by ~-path, and the technote itself.
    home = scratch.root / "home"
    (home / ".claude").mkdir(parents=True)
    global_md = home / ".claude" / "CLAUDE.md"
    global_md.write_text("## Our Relationship\n\nGLOBAL-RULE\n")
    (home / "Developer" / ".claude").mkdir(parents=True)
    project_md = home / "Developer" / ".claude" / "CLAUDE.md"
    project_md.write_text("## Agentic SDLC\n\nPROJECT-RULE\n\n@~/technote.md\n")
    (home / "technote.md").write_text("# Technote\n\nTECHNOTE-RULE\n")
    scratch.env["HOME"] = str(home)
    proc = scratch.run(["--context", str(global_md), "--context", str(project_md)])
    assert proc.returncode == 0, proc.stderr
    assert "--append-system-prompt-file" in scratch.argv()
    ctx_file = scratch.root / "stub-log.ctx"
    assert ctx_file.is_file(), "context file exists at invocation time"
    ctx = ctx_file.read_text()
    assert ctx.startswith("Codebase and user instructions are shown below.")
    assert (
        f"Contents of {global_md} (user's private global instructions for all projects):\n\n"
        "## Our Relationship"
    ) in ctx
    assert (
        f"Contents of {project_md} (project instructions, checked into the codebase):\n\n"
        "## Agentic SDLC"
    ) in ctx
    assert (
        f"Contents of {home / 'technote.md'} (project instructions, checked into the codebase):"
        "\n\n# Technote"
    ) in ctx
    assert "TECHNOTE-RULE" in ctx
    assert "@~/technote.md" not in ctx
    assert ctx.index("GLOBAL-RULE") < ctx.index("PROJECT-RULE") < ctx.index("TECHNOTE-RULE")
    provenance = scratch.run_dir / "subject-context.md"
    assert provenance.is_file()
    assert provenance.read_text() == ctx


def test_no_context_means_no_append_and_no_provenance(scratch: Scratch) -> None:
    scratch.run(["--stage", str(scratch.corpus)])
    assert "--append-system-prompt-file" not in scratch.argv()
    assert not (scratch.run_dir / "subject-context.md").exists()


def test_missing_context_path_fails_before_claude(scratch: Scratch) -> None:
    proc = scratch.run(["--context", str(scratch.root / "no-such-context.md")])
    assert proc.returncode != 0
    assert "no-such-context.md" in proc.stderr
    assert not scratch.claude_invoked()


def test_missing_import_fails_naming_the_import(scratch: Scratch) -> None:
    broken = scratch.root / "broken.md"
    broken.write_text("rules\n\n@~/absent-technote.md\n")
    scratch.env["HOME"] = str(scratch.root)
    proc = scratch.run(["--context", str(broken)])
    assert proc.returncode != 0
    assert "absent-technote.md" in proc.stderr
    assert not scratch.claude_invoked()


def test_unset_effort_leaves_the_command_line_as_it_was(scratch: Scratch) -> None:
    scratch.run(["--stage", str(scratch.corpus)])
    argv = scratch.argv()
    assert "--effort" not in argv
    assert argv[:9] == [
        "-p",
        "--safe-mode",
        "--model",
        "stub-model",
        "--permission-mode",
        "acceptEdits",
        "--output-format",
        "json",
        "--",
    ]


def test_config_effort_reaches_claude(scratch: Scratch) -> None:
    scratch.env["SMEVALS_CONFIG_EFFORT"] = "medium"
    scratch.run(["--stage", str(scratch.corpus)])
    argv = scratch.argv()
    assert argv[argv.index("--effort") + 1] == "medium"
    assert argv[argv.index("--model") + 1] == "stub-model"


def test_pinned_resolves_model_and_effort_from_the_subject(scratch: Scratch) -> None:
    subject = scratch.root / "subject.md"
    subject.write_text(
        '---\nname: "subject"\nmodel: opus\neffort: medium\n---\n\n---\nmodel: decoy\n'
    )
    scratch.env["SMEVALS_MODEL"] = "pinned"
    proc = scratch.run(["--subject", str(subject), "--stage", str(scratch.corpus)])
    assert proc.returncode == 0, proc.stderr
    argv = scratch.argv()
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "medium"
    assert (
        scratch.condition()
        == RUNNER_LINE + "model: opus\neffort: medium\nresolved: stub-model-id\n"
    )


def test_pinned_subject_without_effort_records_no_effort_line(scratch: Scratch) -> None:
    subject = scratch.root / "subject.md"
    subject.write_text("---\nname: subject\nmodel: opus\n---\nbody\n")
    scratch.env["SMEVALS_MODEL"] = "pinned"
    proc = scratch.run(["--subject", str(subject), "--stage", str(scratch.corpus)])
    assert proc.returncode == 0
    assert scratch.condition() == RUNNER_LINE + "model: opus\nresolved: stub-model-id\n"


def test_subject_alone_changes_nothing(scratch: Scratch) -> None:
    subject = scratch.root / "subject.md"
    subject.write_text("---\nname: subject\nmodel: opus\neffort: medium\n---\nbody\n")
    scratch.run(["--subject", str(subject), "--stage", str(scratch.corpus)])
    argv = scratch.argv()
    assert argv[argv.index("--model") + 1] == "stub-model"
    assert "--effort" not in argv
    assert scratch.condition() == RUNNER_LINE + "model: stub-model\nresolved: stub-model-id\n"


def test_pinned_without_subject_fails_naming_the_flag(scratch: Scratch) -> None:
    scratch.env["SMEVALS_MODEL"] = "pinned"
    proc = scratch.run(["--stage", str(scratch.corpus)])
    assert proc.returncode != 0
    assert "--subject" in proc.stderr
    assert not scratch.claude_invoked()


def test_missing_subject_path_fails_fast(scratch: Scratch) -> None:
    scratch.env["SMEVALS_MODEL"] = "pinned"
    proc = scratch.run(["--subject", str(scratch.root / "no-such-subject.md")])
    assert proc.returncode != 0
    assert "no-such-subject.md" in proc.stderr


def test_subject_without_a_model_pin_fails_naming_the_subject(scratch: Scratch) -> None:
    unpinned = scratch.root / "unpinned.md"
    unpinned.write_text("---\nname: unpinned\n---\nbody\n")
    scratch.env["SMEVALS_MODEL"] = "pinned"
    proc = scratch.run(["--subject", str(unpinned)])
    assert proc.returncode != 0
    assert "unpinned.md" in proc.stderr
    assert "model" in proc.stderr
    assert not scratch.claude_invoked()


def test_pinned_plus_config_effort_is_refused(scratch: Scratch) -> None:
    subject = scratch.root / "subject.md"
    subject.write_text("---\nname: subject\nmodel: opus\neffort: medium\n---\nbody\n")
    scratch.env["SMEVALS_MODEL"] = "pinned"
    scratch.env["SMEVALS_CONFIG_EFFORT"] = "high"
    proc = scratch.run(["--subject", str(subject)])
    assert proc.returncode != 0
    assert "effort" in proc.stderr
    assert not scratch.claude_invoked()


def test_mcp_session_swaps_safe_mode_for_strict_setting_sources(scratch: Scratch) -> None:
    mcp_file = scratch.root / "mcp.json"
    mcp_file.write_text('{"mcpServers":{}}\n')
    proc = scratch.run(["--stage", str(scratch.corpus), "--mcp-config", str(mcp_file)])
    assert proc.returncode == 0
    assert proc.stderr == ""
    argv = (scratch.root / "stub-log.argv").read_text().split("\n")
    assert "--safe-mode" not in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == str(mcp_file)
    assert argv[argv.index("--model") + 1] == "stub-model"


def test_missing_mcp_config_fails_before_claude(scratch: Scratch) -> None:
    proc = scratch.run(["--mcp-config", str(scratch.root / "absent.json")])
    assert proc.returncode == 1
    assert "--mcp-config file not found" in proc.stderr
    assert not scratch.claude_invoked()


@pytest.mark.parametrize(
    "make_git",
    [
        pytest.param(
            lambda d: subprocess.run(["git", "init", "-q", str(d)], check=True),
            id="repository root",
        ),
        pytest.param(
            lambda d: (d / ".git").write_text("gitdir: /nowhere/.git/worktrees/x\n"),
            id="linked worktree root",
        ),
    ],
)
def test_refuses_a_git_work_tree_root_as_the_run_dir(scratch: Scratch, make_git) -> None:
    make_git(scratch.run_dir)
    proc = scratch.run(["--stage", str(scratch.corpus)])
    assert proc.returncode == 1
    assert "refusing to run in a git work-tree root" in proc.stderr
    assert not scratch.claude_invoked()
    assert not (scratch.run_dir / "staged" / "fixture.md").exists()
