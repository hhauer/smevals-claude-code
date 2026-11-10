# ABOUTME: Black-box contract tests for the smevals-claude-code runner: a stubbed claude on PATH
# ABOUTME: records cwd, argv, and env so the isolation and condition contracts are asserted exactly.

import json
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

# What the stub prints when it is asked for stream-json: a session whose background job outlived
# a turn, so it reports two results and the last one is the run's.
STREAM_LINES = [
    '{"type":"system","subtype":"init"}',
    '{"type":"result","result":"FIRST-TURN","modelUsage":{"early-model-id":{}}}',
    '{"type":"assistant","text":"line separator \u2028 inside a string"}',
    '{"type":"result","result":"STUB-RESULT\\ntwo lines","modelUsage":{"stub-model-id":{}}}',
]

# The claude stub records its cwd and argv, edits a staged file the way a subject session
# would (one edit, one new file), then exits with STUB_EXIT. It prints the JSON result record
# claude -p --output-format json prints (or STUB_RAW verbatim, for a session whose output is not
# a result record). STUB_SLEEP simulates a hung session for the watchdog test; the TERM trap and
# backgrounded sleep model the real claude CLI dying promptly on SIGTERM. Three hooks act the way
# a curious or hostile subject would and record how the sandbox answered: STUB_PROBE names a path
# to read (as a directory listing or a file), STUB_STEAL a path to hard-link or move into the work
# dir, and STUB_PLANT a path to create. The stub also records the home and temp dirs it was
# handed, where the home's keychain link points, and whether a temp file round-trips. Asked for
# stream-json it prints STUB_STREAM verbatim instead of a result record. STUB_LINGER leaves a
# background process holding its stdout open after it exits, and STUB_FLOOD one that keeps
# writing to it.
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
if [[ -n ${STUB_PROBE:-} ]]; then
  if ls "$STUB_PROBE"/ >/dev/null 2>&1 || cat "$STUB_PROBE" >/dev/null 2>&1; then
    echo readable > "$STUB_LOG.probe"
  else
    echo denied > "$STUB_LOG.probe"
  fi
fi
if [[ -n ${STUB_STEAL:-} ]]; then
  if ln "$STUB_STEAL" ./linked 2>/dev/null; then
    echo linked > "$STUB_LOG.steal"
  elif mv "$STUB_STEAL" ./stolen 2>/dev/null; then
    echo moved > "$STUB_LOG.steal"
  else
    echo refused > "$STUB_LOG.steal"
  fi
fi
if [[ -n ${STUB_PLANT:-} ]]; then
  if { echo planted > "$STUB_PLANT"; } 2>/dev/null; then
    echo planted > "$STUB_LOG.plant"
  else
    echo refused > "$STUB_LOG.plant"
  fi
fi
printf '%s\\n' "${HOME:-}" "${TMPDIR:-}" "${CLAUDE_CODE_TMPDIR:-}" > "$STUB_LOG.dirs"
readlink "$HOME/Library/Keychains" > "$STUB_LOG.keychains" 2>/dev/null || true
env | grep '^XDG_' > "$STUB_LOG.xdg" || true
if tmp_file=$(mktemp "${TMPDIR:-/nonexistent}/stub.XXXXXX" 2>/dev/null) \\
  && echo round-trip > "$tmp_file" && cat "$tmp_file" > "$STUB_LOG.tmp" 2>/dev/null; then
  :
else
  echo failed > "$STUB_LOG.tmp"
fi
if [[ -n ${STUB_HOSTILE:-} ]]; then
  # A subject that plants a module and squats names the runner and smevals write later.
  printf 'raise SystemExit("hijacked json module")\\n' > json.py
  ln -s "$STUB_HOSTILE" subject-condition.md
  ln -s "$STUB_HOSTILE" subject-context.md
  ln -s "$STUB_HOSTILE" output.txt
fi
if [[ -n ${STUB_LINGER:-} ]]; then
  sleep "$STUB_LINGER" </dev/null 2>/dev/null &
fi
if [[ -n ${STUB_FLOOD:-} ]]; then
  yes flood </dev/null 2>/dev/null &
  flood_pid=$!
  (sleep "$STUB_FLOOD" && kill "$flood_pid") </dev/null >/dev/null 2>&1 &
fi
if [[ " $* " == *" stream-json "* ]]; then
  printf '%s' "${STUB_STREAM:-}"
elif [[ -n ${STUB_RAW:-} ]]; then
  printf '%s\\n' "$STUB_RAW"
else
  printf '%s\\n' \\
    '{"type":"result","result":"STUB-RESULT\\ntwo lines","modelUsage":{"stub-model-id":{}}}'
fi
exit "${STUB_EXIT:-0}"
"""


class Scratch:
    """A fake smevals run dir, a corpus, an operator home, and the claude stub on PATH.

    HOME and TMPDIR both point inside the test's own directory, so the runner hides a home and
    keeps its work dirs where the test can plant files without touching the real ones.
    """

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
        self.home = root / "home"
        self.home.mkdir()
        (self.home / "secret.md").write_text("operator file\n")
        temp_root = root / "tmp"
        temp_root.mkdir()
        # Where the runner keeps every run's dirs; a sibling run's fixture sits beside ours.
        self.sibling_fixture = temp_root / "smevals-claude-code" / "sibling" / "work" / "fixture.md"
        self.sibling_fixture.parent.mkdir(parents=True)
        self.sibling_fixture.write_text("sibling fixture\n")
        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{bin_dir}:{self.env['PATH']}",
                "HOME": str(self.home),
                "TMPDIR": str(temp_root),
                "STUB_LOG": str(root / "stub-log"),
                "SMEVALS_MODEL": "stub-model",
                "STUB_STREAM": "".join(line + "\n" for line in STREAM_LINES),
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

    def recorded(self, name: str) -> str:
        log = self.root / f"stub-log.{name}"
        return log.read_text().strip() if log.is_file() else f"no {name} recorded"

    def probe(self) -> str:
        return self.recorded("probe")

    def stream(self) -> list[tuple[float, str]]:
        """The recorded stream as (arrival time, line) pairs."""
        text = (self.run_dir / "subject-stream.jsonl").read_text(encoding="utf-8")
        pairs = [line.split(" ", 1) for line in text.split("\n") if line]
        return [(float(stamp), body) for stamp, body in pairs]

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
    fixture = scratch.run_dir / "worked" / "staged" / "fixture.md"
    assert fixture.is_file()
    assert "stub edit" in fixture.read_text()
    assert (scratch.run_dir / "worked" / "stub-marker.md").is_file()


def host_paths_the_subject_must_not_read() -> list:
    in_users = pytest.mark.skipif(
        Path("/Users") not in REPO_ROOT.parents, reason="this checkout is not under /Users"
    )
    return [
        pytest.param(lambda s: s.home / "secret.md", id="file in the operator's home"),
        pytest.param(lambda s: s.home, id="the operator's home"),
        pytest.param(lambda s: s.sibling_fixture, id="sibling run's fixture"),
        pytest.param(lambda s: s.sibling_fixture.parent.parent.parent, id="the runner's base dir"),
        pytest.param(lambda s: s.run_dir, id="the run dir"),
        pytest.param(
            lambda _: REPO_ROOT / "pyproject.toml", id="file under /Users", marks=in_users
        ),
        pytest.param(lambda _: REPO_ROOT, id="directory under /Users", marks=in_users),
        # Exists on any machine that has run Claude Code; elsewhere a missing dir also reads
        # as denied, so this case proves the rule only where the dir is there to hide.
        pytest.param(
            lambda _: Path(f"/private/tmp/claude-{os.getuid()}"), id="claude's shared temp dir"
        ),
    ]


@pytest.mark.parametrize("host_path", host_paths_the_subject_must_not_read())
def test_subject_cannot_read_the_hidden_host_paths(scratch: Scratch, host_path) -> None:
    scratch.env["STUB_PROBE"] = str(host_path(scratch))
    proc = scratch.run(["--stage", str(scratch.corpus)])
    assert proc.returncode == 0, proc.stderr
    assert scratch.probe() == "denied"


@pytest.mark.parametrize("staged_path", ["staged/fixture.md", "staged", "."])
def test_subject_reads_its_own_work_dir(scratch: Scratch, staged_path: str) -> None:
    scratch.env["STUB_PROBE"] = staged_path
    proc = scratch.run(["--stage", str(scratch.corpus)])
    assert proc.returncode == 0, proc.stderr
    assert scratch.probe() == "readable"


def test_temp_outside_the_runner_base_stays_readable(scratch: Scratch) -> None:
    # The stated limit of the sandbox: tools need the shared temp dirs, so only the runner's
    # own base dir and claude's shared temp dir are hidden there.
    elsewhere = scratch.root / "tmp" / "another-programs-file.txt"
    elsewhere.write_text("not hidden\n")
    scratch.env["STUB_PROBE"] = str(elsewhere)
    proc = scratch.run(["--stage", str(scratch.corpus)])
    assert proc.returncode == 0, proc.stderr
    assert scratch.probe() == "readable"


@pytest.mark.parametrize(
    "hidden_file",
    [
        pytest.param(lambda s: s.home / "secret.md", id="file in the operator's home"),
        pytest.param(lambda s: s.sibling_fixture, id="sibling run's fixture"),
    ],
)
def test_subject_cannot_move_a_hidden_file_into_its_work_dir(scratch: Scratch, hidden_file) -> None:
    # Moving or hard-linking a file reads nothing, so hiding a path from reads alone would let
    # it be carried into the work dir and read there.
    target = hidden_file(scratch)
    body = target.read_text()
    scratch.env["STUB_STEAL"] = str(target)
    proc = scratch.run(["--stage", str(scratch.corpus)])
    assert proc.returncode == 0, proc.stderr
    assert scratch.recorded("steal") == "refused"
    assert target.read_text() == body
    assert not (scratch.run_dir / "worked" / "stolen").exists()
    assert not (scratch.run_dir / "worked" / "linked").exists()


def test_subject_cannot_write_into_the_operators_home(scratch: Scratch) -> None:
    planted = scratch.home / "planted.md"
    scratch.env["STUB_PLANT"] = str(planted)
    proc = scratch.run(["--stage", str(scratch.corpus)])
    assert proc.returncode == 0, proc.stderr
    assert scratch.recorded("plant") == "refused"
    assert not planted.exists()


def test_subject_cannot_write_into_the_run_dir(scratch: Scratch) -> None:
    # The run dir holds the runner's records of the session; in stream mode one of them is
    # being written while the session runs.
    planted = scratch.run_dir / "planted.md"
    scratch.env["STUB_PLANT"] = str(planted)
    proc = scratch.run(["--stage", str(scratch.corpus), "--stream"])
    assert proc.returncode == 0, proc.stderr
    assert scratch.recorded("plant") == "refused"
    assert not planted.exists()


def test_allow_read_opens_a_directory_and_a_file_and_is_recorded(scratch: Scratch) -> None:
    tools = scratch.home / "tools"
    (tools / "bin").mkdir(parents=True)
    (tools / "bin" / "tool").write_text("tool body\n")
    lone = scratch.home / "secret.md"
    for probe in (tools / "bin", tools / "bin" / "tool", lone):
        scratch.env["STUB_PROBE"] = str(probe)
        (scratch.root / "stub-log.probe").unlink(missing_ok=True)
        run_dir = scratch.root / f"run-{probe.name}"
        run_dir.mkdir()
        scratch.run_dir = run_dir
        proc = scratch.run(["--allow-read", str(tools), "--allow-read", str(lone)])
        assert proc.returncode == 0, proc.stderr
        assert scratch.probe() == "readable", probe
    assert scratch.condition() == (
        RUNNER_LINE
        + "model: stub-model\n"
        + f"allow-read: {tools.resolve()}\n"
        + f"allow-read: {lone.resolve()}\n"
        + "resolved: stub-model-id\n"
    )


def test_allow_read_does_not_open_its_siblings(scratch: Scratch) -> None:
    tools = scratch.home / "tools"
    tools.mkdir()
    scratch.env["STUB_PROBE"] = str(scratch.home / "secret.md")
    proc = scratch.run(["--stage", str(scratch.corpus), "--allow-read", str(tools)])
    assert proc.returncode == 0, proc.stderr
    assert scratch.probe() == "denied"


def test_allow_read_does_not_allow_writing(scratch: Scratch) -> None:
    tools = scratch.home / "tools"
    tools.mkdir()
    scratch.env["STUB_PLANT"] = str(tools / "planted.md")
    proc = scratch.run(["--stage", str(scratch.corpus), "--allow-read", str(tools)])
    assert proc.returncode == 0, proc.stderr
    assert scratch.recorded("plant") == "refused"
    assert not (tools / "planted.md").exists()


def test_missing_allow_read_path_fails_before_claude(scratch: Scratch) -> None:
    proc = scratch.run(["--allow-read", str(scratch.root / "no-such-tools")])
    assert proc.returncode == 1
    assert "--allow-read path not found" in proc.stderr
    assert "no-such-tools" in proc.stderr
    assert not scratch.claude_invoked()


def test_session_gets_a_home_and_temp_dir_of_its_own(scratch: Scratch) -> None:
    # XDG dirs name places in the operator's home; a tool sent there would be refused.
    scratch.env["XDG_CONFIG_HOME"] = str(scratch.home / ".config")
    proc = scratch.run(["--stage", str(scratch.corpus)])
    assert proc.returncode == 0, proc.stderr
    home, tmpdir, claude_tmpdir = scratch.recorded("dirs").splitlines()
    stub_pwd = scratch.recorded("pwd")
    assert len({home, tmpdir, stub_pwd, str(scratch.home)}) == 4, "home, temp, and work dir differ"
    assert tmpdir == claude_tmpdir
    assert scratch.recorded("tmp") == "round-trip"
    assert scratch.recorded("xdg") == ""
    # The login keychain is found through the home dir, so the session's home links to it.
    assert scratch.recorded("keychains") == str(scratch.home.resolve() / "Library" / "Keychains")
    assert not Path(home).exists(), "session home cleaned up after the run"
    assert not Path(tmpdir).exists(), "session temp dir cleaned up after the run"


def test_refuses_a_base_dir_someone_else_put_there(scratch: Scratch) -> None:
    # The base dir has a fixed name under the temp root. One that is a link leads the run's
    # dirs somewhere the sandbox profile does not describe.
    temp_root = scratch.root / "other-tmp"
    temp_root.mkdir()
    elsewhere = scratch.root / "elsewhere"
    elsewhere.mkdir()
    (temp_root / "smevals-claude-code").symlink_to(elsewhere)
    scratch.env["TMPDIR"] = str(temp_root)
    proc = scratch.run(["--stage", str(scratch.corpus)])
    assert proc.returncode == 1
    assert "refusing to use" in proc.stderr
    assert "smevals-claude-code" in proc.stderr
    assert not scratch.claude_invoked()
    assert list(elsewhere.iterdir()) == []


def test_missing_sandbox_exec_fails_before_claude(scratch: Scratch) -> None:
    scratch.env["PATH"] = str(scratch.root / "bin")
    proc = scratch.run(["--stage", str(scratch.corpus)])
    assert proc.returncode == 1
    assert "sandbox-exec not found on PATH" in proc.stderr
    assert not scratch.claude_invoked()
    assert not (scratch.run_dir / "worked").exists()


def test_missing_claude_fails_naming_it(scratch: Scratch) -> None:
    only_sandbox = scratch.root / "only-sandbox"
    only_sandbox.mkdir()
    (only_sandbox / "sandbox-exec").symlink_to("/usr/bin/sandbox-exec")
    scratch.env["PATH"] = str(only_sandbox)
    proc = scratch.run(["--stage", str(scratch.corpus)])
    assert proc.returncode == 1
    assert "claude not found on PATH" in proc.stderr
    assert not (scratch.run_dir / "worked").exists()


def test_single_file_staging(scratch: Scratch) -> None:
    proc = scratch.run(["--stage", str(scratch.root / "single-file.md")])
    assert proc.returncode == 0
    staged = scratch.run_dir / "worked" / "single-file.md"
    assert staged.is_file()
    assert "single file body" in staged.read_text()


def test_failing_session_propagates_exit_and_still_copies_back(scratch: Scratch) -> None:
    scratch.env["STUB_EXIT"] = "7"
    proc = scratch.run(["--stage", str(scratch.corpus)])
    assert proc.returncode == 7
    fixture = scratch.run_dir / "worked" / "staged" / "fixture.md"
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
    assert (scratch.run_dir / "worked" / "staged" / "fixture.md").is_file()
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
    # smevals writes output.txt, stderr.txt, and run.yaml into the run dir after the runner
    # exits; nothing the subject left may sit under a name at that level.
    assert sorted(entry.name for entry in scratch.run_dir.iterdir()) == [
        "subject-condition.md",
        "subject-context.md",
        "worked",
    ]
    assert (scratch.run_dir / "worked" / "output.txt").is_symlink()


def test_refuses_a_run_dir_that_already_holds_a_worked_tree(scratch: Scratch) -> None:
    (scratch.run_dir / "worked").mkdir()
    proc = scratch.run(["--stage", str(scratch.corpus)])
    assert proc.returncode == 1
    assert "worked" in proc.stderr
    assert not scratch.claude_invoked()


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
    mcp_file = scratch.home / "mcp.json"
    mcp_file.write_text('{"mcpServers":{}}\n')
    scratch.env["STUB_PROBE"] = str(mcp_file)
    proc = scratch.run(["--stage", str(scratch.corpus), "--mcp-config", str(mcp_file)])
    assert proc.returncode == 0
    assert proc.stderr == ""
    argv = (scratch.root / "stub-log.argv").read_text().split("\n")
    assert "--safe-mode" not in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == str(mcp_file)
    assert scratch.probe() == "readable", "the session can read the MCP config it was handed"
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
    assert not (scratch.run_dir / "worked" / "staged" / "fixture.md").exists()


def test_stream_mode_asks_claude_for_the_stream(scratch: Scratch) -> None:
    proc = scratch.run(["--stage", str(scratch.corpus), "--stream"])
    assert proc.returncode == 0, proc.stderr
    assert scratch.argv()[:10] == [
        "-p",
        "--safe-mode",
        "--model",
        "stub-model",
        "--permission-mode",
        "acceptEdits",
        "--output-format",
        "stream-json",
        "--verbose",
        "--",
    ]


def test_stream_mode_under_an_mcp_config_asks_for_the_stream(scratch: Scratch) -> None:
    mcp_file = scratch.root / "mcp.json"
    mcp_file.write_text('{"mcpServers":{}}\n')
    proc = scratch.run(["--mcp-config", str(mcp_file), "--stream"])
    assert proc.returncode == 0, proc.stderr
    argv = scratch.argv()
    assert argv[argv.index("--output-format") + 1 :][:2] == ["stream-json", "--verbose"]
    assert "--strict-mcp-config" in argv


def test_stream_mode_records_each_line_with_its_arrival_time(scratch: Scratch) -> None:
    before = time.time()
    proc = scratch.run(["--stage", str(scratch.corpus), "--stream"])
    after = time.time()
    assert proc.returncode == 0, proc.stderr
    stream = scratch.stream()
    bodies = [body for _, body in stream]
    # The runner's own first and last lines let a reader count the quiet time before the
    # session's first event and after its last.
    assert json.loads(bodies[0]) == {"type": "runner_launch"}
    assert bodies[1:-1] == STREAM_LINES
    assert json.loads(bodies[-1]) == {"type": "runner_exit", "returncode": 0}
    stamps = [stamp for stamp, _ in stream]
    assert stamps == sorted(stamps)
    # Stamps carry milliseconds, so they can round to just outside the clock readings.
    assert before - 0.001 <= stamps[0]
    assert stamps[-1] <= after + 0.001
    raw = (scratch.run_dir / "subject-stream.jsonl").read_text(encoding="utf-8")
    assert all(len(line.split(" ", 1)[0].split(".")[1]) == 3 for line in raw.split("\n") if line)


def test_stream_mode_reports_the_last_result(scratch: Scratch) -> None:
    proc = scratch.run(["--stage", str(scratch.corpus), "--stream"])
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert proc.stdout == "STUB-RESULT\ntwo lines\n"
    assert scratch.condition() == RUNNER_LINE + "model: stub-model\nresolved: stub-model-id\n"


def test_stream_file_sits_beside_the_worked_tree_not_in_it(scratch: Scratch) -> None:
    proc = scratch.run(["--stage", str(scratch.corpus), "--stream"])
    assert proc.returncode == 0, proc.stderr
    assert sorted(entry.name for entry in scratch.run_dir.iterdir()) == [
        "subject-condition.md",
        "subject-stream.jsonl",
        "worked",
    ]
    assert not (scratch.run_dir / "worked" / "subject-stream.jsonl").exists()


def test_stream_with_no_result_event_prints_nothing_and_says_so(scratch: Scratch) -> None:
    scratch.env["STUB_STREAM"] = '{"type":"system","subtype":"init"}\nnot json at all\n'
    proc = scratch.run(["--stage", str(scratch.corpus), "--stream"])
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert "no result event" in proc.stderr
    assert scratch.condition() == RUNNER_LINE + "model: stub-model\n"
    assert [body for _, body in scratch.stream()][1:-1] == [
        '{"type":"system","subtype":"init"}',
        "not json at all",
    ]


def test_stream_mode_stamps_a_final_line_that_has_no_newline(scratch: Scratch) -> None:
    cut = '{"type":"result","result":"CUT","modelUsage":{}}'
    scratch.env["STUB_STREAM"] = '{"type":"system"}\n' + cut
    proc = scratch.run(["--stage", str(scratch.corpus), "--stream"])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "CUT\n"
    assert [body for _, body in scratch.stream()][1:-1] == ['{"type":"system"}', cut]


def test_stream_mode_propagates_a_failing_exit_and_records_it(scratch: Scratch) -> None:
    scratch.env["STUB_EXIT"] = "7"
    proc = scratch.run(["--stage", str(scratch.corpus), "--stream"])
    assert proc.returncode == 7
    assert json.loads(scratch.stream()[-1][1]) == {"type": "runner_exit", "returncode": 7}
    assert (scratch.run_dir / "worked" / "staged" / "fixture.md").is_file()


def test_stream_mode_watchdog_kills_a_hung_session(scratch: Scratch) -> None:
    scratch.env["STUB_SLEEP"] = "30"
    start = time.monotonic()
    proc = scratch.run(["--stage", str(scratch.corpus), "--stream", "--timeout", "1"])
    assert time.monotonic() - start < 20
    assert proc.returncode != 0
    assert proc.stdout == ""
    bodies = [body for _, body in scratch.stream()]
    assert json.loads(bodies[0]) == {"type": "runner_launch"}
    assert json.loads(bodies[-1]) == {"type": "runner_exit", "returncode": proc.returncode}


def test_stream_mode_does_not_wait_on_a_process_the_session_left_behind(scratch: Scratch) -> None:
    # A background job the subject started keeps the session's stdout open after claude exits.
    scratch.env["STUB_LINGER"] = "20"
    start = time.monotonic()
    proc = scratch.run(["--stage", str(scratch.corpus), "--stream"])
    assert time.monotonic() - start < 10
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "STUB-RESULT\ntwo lines\n"


def test_stream_mode_stops_reading_a_process_that_keeps_writing_after_exit(
    scratch: Scratch,
) -> None:
    # Left unbounded, a background job that never stops writing would hold the run past its
    # watchdog and grow the stream file without limit.
    scratch.env["STUB_FLOOD"] = "25"
    start = time.monotonic()
    proc = scratch.run(["--stage", str(scratch.corpus), "--stream"])
    assert time.monotonic() - start < 12
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "STUB-RESULT\ntwo lines\n"
    assert json.loads(scratch.stream()[-1][1]) == {"type": "runner_exit", "returncode": 0}
