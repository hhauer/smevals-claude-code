# ABOUTME: The smevals-claude-code runner: stages fixtures into an isolated work dir outside any
# ABOUTME: repo, drives a sandboxed headless claude session there, and copies the worked tree back.
"""A reusable smevals Runner for Claude Code.

The prompt is read from stdin. A ``--stage`` directory is staged by contents; a ``--stage`` file
is copied to the work dir root. Everything after ``--`` is forwarded to claude verbatim (per-suite
tool allow/deny lists and the like).

The run condition comes from the smevals config. ``SMEVALS_MODEL`` is the config's ``model:``
verbatim; a config's ``effort:`` arrives as ``SMEVALS_CONFIG_EFFORT`` and becomes ``--effort``,
and with none set the claude command line is exactly what it was before effort existed, so anchors
measured then keep their condition. A config with ``model: pinned`` runs the subject under its
production pins: ``--subject`` names the skill or agent file and its frontmatter ``model:`` and
``effort:`` are used. The pins are read live, so the eval follows them as they change and nothing
here duplicates them.

The subject session must not inherit the operator's ambient context. ``--safe-mode`` drops
CLAUDE.md, plugins, hooks, and auto-memory while OAuth auth keeps working (``--bare`` would demand
an API key); working in a temp dir outside any repo keeps a checkout's git status out of the system
prompt. Residual: the harness's built-in skill listing stays visible; only ``--bare`` removes it.

The subject session must not read the operator's files either: a suite's skills, fixtures, and
checkers sit on the same disk, and a session that finds them is no longer measuring what the eval
means to. The session runs under the macOS sandbox (``sandbox-exec``), unable to read or write
under ``/Users``, the operator's home, the run dir, the base dir that holds every run's work
dir, or claude's shared temp dir. It is given an empty home and a temp dir of its own, so the
lookups tools make there find nothing instead of being refused. The rest of the system stays
readable, so installed tools work; a tool or file that lives under the home dir is opened by
``--allow-read``, read-only, and recorded in ``subject-condition.md``. Residual: the shared temp
dirs (``/tmp`` and the per-user temp root) stay readable outside the runner's base dir, because
shells and tools need them, and the login keychain stays reachable, because OAuth reads it.

The worked tree is copied back afterwards, on failure too, for post-mortems, into ``worked/`` in
the run dir. The tree is the subject's, symlinks included, and nothing else writes inside
``worked/``: the runner's records and the files smevals writes sit beside it, where no name the
subject chose can be in their way.

``--safe-mode`` also drops the context the subject has in production: a suite whose subject runs in
Claude Code under the operator's tree passes each CLAUDE.md the production session loads as
``--context``, in load order. Under ``--safe-mode`` neither a cwd CLAUDE.md nor ``--add-dir`` is
read, so the files are assembled with the harness's own CLAUDE.md framing, one
"Contents of <path> (<label>):" section per file with ``@``-imports expanded in place, and handed to
claude as ``--append-system-prompt-file``. The assembled text is copied into the run dir as
``subject-context.md`` so the run records what the subject saw. ``--rules`` takes a rules
directory and adds the rules the harness loads at launch, the ones with no ``paths:``
frontmatter, in the harness's order; it sits among the ``--context`` flags where the directory's
rules load in production, which is after the CLAUDE.md beside it. Every run also writes
``subject-condition.md``: the runner version, the Claude Code version, the model and effort the
session was asked for (under ``pinned``, the pins read from the subject), a SHA-256 of the
assembled context, the arguments forwarded to claude, each ``--allow-read`` grant and, as
``resolved:``, the model IDs the session reports having used. Every line but ``resolved:`` is
settled before the session starts, so two runs of one condition differ in that line at most and a
suite can compare a run against a record it kept. smevals' ``run.yaml`` records only the config's
alias, and aliases move. claude
runs with ``--output-format json`` so the session reports those IDs; the runner prints the record's
result text to stdout exactly as text mode would, so what smevals reads is unchanged. Output that
is not a result record (a watchdog-killed session) passes through verbatim and gets no resolved
line.

``--stream`` is for a suite that grades what happened during the session, such as how long the
subject went quiet. claude is asked for ``--output-format stream-json --verbose`` in place of
``json``, and each line it prints is written to ``subject-stream.jsonl`` in the run dir as
``<epoch seconds> <line>``, stamped when it arrived, between a ``runner_launch`` line and a
``runner_exit`` line of the runner's own. The file is beside ``worked/``, not in it: the subject
cannot read or edit its own stream. stdout carries the text of the stream's last result event, so
what smevals reads is the same in both modes. Suites that pass no ``--stream`` are unaffected.

A session that leaves a command running in the background prints its result and then stays
alive until the command ends, when it takes another turn whose message becomes the last result
(measured on Claude Code 2.1.321). The real answer is still in the stream, one result event per
turn. ``--idle-timeout <secs>`` ends such a session instead: once the stream's latest line is a
result event and nothing follows for that long, the session is stopped, the run exits 0 with that
result on stdout, and the ``runner_exit`` line carries ``"ended": "idle"`` so a suite can tell.
Quiet before any result is left to ``--timeout``, because a working session is quiet too.

A skill reaches its plugin's bundled scripts and references through ``${CLAUDE_PLUGIN_ROOT}``,
which the harness replaces when it loads the skill. A subject inlined into the prompt gets no such
replacement, and the plugin's checkout is under the home dir, out of the session's reach.
``--plugin-root <dir>`` copies the tree into the session's own dirs, readable and not writable,
and replaces ``${CLAUDE_PLUGIN_ROOT}`` in the prompt with the copy's path. The path differs per
run, as the work dir's does. The variable is not put in the session's environment: the harness
does not give it to the commands a session runs either.

``--mcp-config <file>`` is the one opt-in that changes the isolation mechanism. ``--safe-mode``
drops every MCP server, including the ones ``--mcp-config`` names and a cwd ``.mcp.json``, and
``--bare`` never reads OAuth. An MCP session therefore runs with ``--setting-sources ""`` instead,
measured to load no plugin skills and no CLAUDE.md, plus ``--strict-mcp-config`` so the subject
reaches only the servers the file names, never a user-scope production server. Suites that pass no
``--mcp-config`` are unaffected: their argv is byte-identical to before the flag existed.
"""

import argparse
import hashlib
import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from importlib.metadata import version
from pathlib import Path
from typing import BinaryIO, NoReturn

PROG = "smevals-claude-code"
FRAMING = (
    "Codebase and user instructions are shown below. Be sure to adhere to these instructions. "
    "IMPORTANT: These instructions OVERRIDE any default behavior and you MUST follow them exactly "
    "as written.\n\n"
)
GLOBAL_LABEL = "user's private global instructions for all projects"
PROJECT_LABEL = "project instructions, checked into the codebase"
# The run dir subdirectory the subject's worked tree is copied back into. The runner and
# smevals write their own files beside it, never inside it.
WORKED_DIR = "worked"
DEFAULT_TIMEOUT_SECS = 540.0
# What a skill writes for its plugin's install directory. The harness replaces it in a skill
# it loads; a subject inlined into the prompt has it replaced by the runner.
PLUGIN_ROOT_VARIABLE = "${CLAUDE_PLUGIN_ROOT}"
# The run dir file a stream-mode session's events are recorded in, each with its arrival time.
STREAM_FILE = "subject-stream.jsonl"
# How often the stream reader checks whether claude has exited, and how much it reads at once.
STREAM_POLL_SECS = 0.5
STREAM_CHUNK_BYTES = 65536
# How long the stream reader keeps reading once claude has exited. What claude printed last is
# already in the pipe and is read at once; the limit is for a process that outlives claude and
# never stops writing.
STREAM_DRAIN_SECS = 1.0


def fail(message: str) -> NoReturn:
    """Exit 1 with a message naming the runner, so a halted run says who refused and why."""
    print(f"{PROG}: {message}", file=sys.stderr)
    sys.exit(1)


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the runner's own flags; everything after ``--`` is claude's."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Drive an isolated headless Claude Code session as a smevals Runner. "
        "The prompt is read from stdin.",
    )
    parser.add_argument(
        "--stage",
        action="append",
        default=None,
        metavar="PATH",
        help="a directory staged by contents, or a file copied to the work dir root; repeatable",
    )
    # --context and --rules fill one list, so the sections keep their command-line order.
    parser.add_argument(
        "--context",
        action="append",
        default=None,
        type=lambda value: ("file", value),
        metavar="FILE",
        help="a CLAUDE.md the subject sees in production, in load order; repeatable",
    )
    parser.add_argument(
        "--rules",
        action="append",
        dest="context",
        type=lambda value: ("rules", value),
        metavar="DIR",
        help="a rules directory whose always-on rules the subject sees in production, placed "
        "among the --context files in command-line order; path-scoped rules are left out; "
        "repeatable",
    )
    parser.add_argument(
        "--subject",
        metavar="FILE",
        help="the skill or agent file whose frontmatter pins apply under model: pinned",
    )
    parser.add_argument(
        "--mcp-config",
        metavar="FILE",
        help="an MCP config file; switches the session to --setting-sources '' --strict-mcp-config",
    )
    parser.add_argument(
        "--allow-read",
        action="append",
        default=None,
        metavar="PATH",
        help="a host directory or file the sandboxed session may read, such as a tool "
        "installed under the home directory; repeatable",
    )
    parser.add_argument(
        "--plugin-root",
        metavar="DIR",
        help="a plugin tree the session gets a read-only copy of; ${CLAUDE_PLUGIN_ROOT} in the "
        "prompt becomes the copy's path",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help=f"ask claude for its event stream and record each line, with the time it arrived, "
        f"in {STREAM_FILE} in the run dir",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=None,
        metavar="SECS",
        help="with --stream, end a session that has printed a result and then stayed quiet this "
        "long, as one waiting on a background command does; the run exits 0 and records the "
        "idle end (default: no idle limit)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECS,
        metavar="SECS",
        help=f"kill a session still running after this long (default {DEFAULT_TIMEOUT_SECS:g})",
    )
    parser.add_argument(
        "claude_args",
        nargs="*",
        help="after --, extra arguments forwarded to claude verbatim",
    )
    args = parser.parse_args(argv)
    args.stage = args.stage or []
    args.context = args.context or []
    args.allow_read = args.allow_read or []
    return args


def read_lines(path: Path) -> list[str]:
    """Split a file into lines the way a shell ``read`` loop does.

    A trailing newline ends the last line rather than opening an empty one, and a final line
    with no newline still counts.
    """
    text = path.read_text(encoding="utf-8")
    if text == "":
        return []
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    return lines


def append_context(out: list[str], path: str, lines: list[str], label: str, home: str) -> None:
    """Append one context file's lines under the harness's section header.

    Each ``@path`` line expands into its own section in place, recursively, as the harness
    does. A missing import is a configuration error, not a subject condition to measure
    under, so it fails before claude is invoked.
    """
    out.append(f"Contents of {path} ({label}):\n\n")
    for line in lines:
        if line.startswith("@") and len(line) > 1:
            imported = line[1:]
            if imported.startswith("~"):
                imported = home + imported[1:]
            if not Path(imported).is_file():
                fail(f"@-import not found: {imported} (imported by {path})")
            out.append("\n")
            append_context(out, imported, read_lines(Path(imported)), PROJECT_LABEL, home)
        else:
            out.append(line + "\n")
    out.append("\n")


def split_frontmatter(lines: list[str]) -> tuple[list[str], list[str]]:
    """Split a file's lines into the YAML frontmatter it opens with, if any, and its body."""
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        return [], lines
    end = lines.index("---", 1)
    return lines[1:end], lines[end + 1 :]


def append_rules(out: list[str], directory: Path, home: str) -> None:
    """Append a rules directory's always-on rules, as the harness loads them at launch.

    Measured on Claude Code 2.1.321: the ``.md`` files load in the order the directory lists
    its entries, a subdirectory's rules where the subdirectory is listed; a rule that is a
    symlink is shown under the path it links to; and the frontmatter is not shown. A rule
    whose frontmatter has ``paths:`` loads only once the session touches a matching file, so
    it is not part of the context a session starts with and is left out.
    """
    for name in os.listdir(directory):
        entry = directory / name
        if entry.is_dir():
            append_rules(out, entry, home)
        elif name.endswith(".md"):
            frontmatter, body = split_frontmatter(read_lines(entry))
            if any(line.startswith("paths:") for line in frontmatter):
                continue
            shown = entry.resolve() if entry.is_symlink() else entry
            append_context(out, str(shown), body, PROJECT_LABEL, home)


def assemble_context(entries: list[tuple[str, str]], home: str) -> str | None:
    """Build the system-prompt text from the context files, labeled as the harness labels them.

    Each entry is a ``file`` or a ``rules`` directory. With no section to show, as when a
    rules directory holds only path-scoped rules, there is no text.
    """
    out: list[str] = []
    for kind, path in entries:
        if kind == "rules":
            append_rules(out, Path(path), home)
        else:
            label = GLOBAL_LABEL if path == f"{home}/.claude/CLAUDE.md" else PROJECT_LABEL
            append_context(out, path, read_lines(Path(path)), label, home)
    return FRAMING + "".join(out) if out else None


def frontmatter_value(path: Path, key: str) -> str:
    """Read one scalar from the YAML frontmatter between the file's first two ``---`` lines."""
    fences = 0
    for line in read_lines(path):
        if line == "---":
            fences += 1
            if fences == 2:
                break
            continue
        if fences == 1 and line.startswith(f"{key}:"):
            return line[len(key) + 1 :].lstrip(" \t")
    return ""


def resolve_condition(config_model: str, subject: str | None) -> tuple[str, str]:
    """Turn the config's model and effort into the ones claude is asked for.

    Under ``pinned`` the subject's frontmatter is the condition, and a config effort beside it
    would make the condition ambiguous, so that combination is refused.
    """
    effort = os.environ.get("SMEVALS_CONFIG_EFFORT", "")
    if config_model != "pinned":
        return config_model, effort
    if subject is None:
        fail("model 'pinned' needs --subject <file> to read the pins from")
    if effort:
        fail("model 'pinned' takes effort from the subject; drop effort: from the config")
    model = frontmatter_value(Path(subject), "model")
    if not model:
        fail(f"no model: pin in the frontmatter of {subject}")
    return model, frontmatter_value(Path(subject), "effort")


def claude_version(claude: str) -> str:
    """Ask claude for its version, which is part of the condition a session runs under."""
    try:
        proc = subprocess.run(
            [claude, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        fail(f"claude --version did not run: {error}; the run records the version it measured")
    if proc.returncode != 0 or not proc.stdout.strip():
        fail(
            f"claude --version exited {proc.returncode} with {proc.stderr.strip()!r}; "
            "the run records the version it measured, so fix the claude install first"
        )
    return proc.stdout.strip()


def stage(paths: list[str], work_dir: Path) -> None:
    """Copy each staged path into the work dir: directories by contents, files to the root."""
    for path in paths:
        source = Path(path)
        if source.is_dir():
            shutil.copytree(source, work_dir, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(source, work_dir / source.name)


def sandbox_string(path: Path) -> str:
    """Quote a path as a sandbox profile string literal."""
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_rule(action: str, operations: str, path: Path) -> str:
    """Write one profile rule: a directory covers everything under it, anything else one path."""
    scope = "subpath" if path.is_dir() else "literal"
    return f"({action} {operations} ({scope} {sandbox_string(path)}))"


def sandbox_profile(hidden: list[Path], own: list[Path], readable: list[Path]) -> str:
    """Write the macOS sandbox profile the session runs under.

    Everything is allowed except the ``hidden`` trees, where the session can neither read nor
    write: reads are what the sandbox is for, and writes go with them because moving a file
    reads nothing, so a hidden file could otherwise be carried into the work dir and read
    there. The last matching rule wins, so the paths the session needs inside a hidden tree are
    opened again afterwards: ``own`` for reading and writing, ``readable`` for reading only.
    Reaching a path takes metadata on each of its parents, so those are opened for metadata
    only, which lets a parent be traversed but not listed.
    """
    lines = ["(version 1)", "(allow default)"]
    lines += [f"(deny file-read* file-write* (subpath {sandbox_string(path)}))" for path in hidden]
    parents = sorted({parent for path in [*own, *readable] for parent in path.parents})
    lines += [f"(allow file-read-metadata (literal {sandbox_string(path)}))" for path in parents]
    lines += [sandbox_rule("allow", "file-read* file-write*", path) for path in own]
    lines += [sandbox_rule("allow", "file-read*", path) for path in readable]
    return "\n".join(lines) + "\n"


def build_command(
    claude: str,
    model: str,
    effort: str,
    mcp_config: str | None,
    context_file: Path | None,
    claude_args: list[str],
    prompt: str,
    *,
    stream: bool,
) -> list[str]:
    """Assemble the claude argv: the measured condition every anchor was taken under.

    A stream-mode session differs in its output format alone.
    """
    output_format = ["stream-json", "--verbose"] if stream else ["json"]
    if mcp_config is not None:
        cmd = [
            claude,
            "-p",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            mcp_config,
            "--model",
            model,
            "--permission-mode",
            "acceptEdits",
            "--output-format",
            *output_format,
        ]
    else:
        cmd = [
            claude,
            "-p",
            "--safe-mode",
            "--model",
            model,
            "--permission-mode",
            "acceptEdits",
            "--output-format",
            *output_format,
        ]
    if effort:
        cmd += ["--effort", effort]
    if context_file is not None:
        cmd += ["--append-system-prompt-file", str(context_file)]
    cmd += claude_args
    cmd += ["--", prompt]
    return cmd


def session_env(session_home: Path, session_tmp: Path) -> dict[str, str]:
    """Build the environment the subject session runs in.

    The subject session must not see smevals task metadata: scalar task fields become
    ``SMEVALS_TASK_*`` vars and can carry grading keys (a task's ``expected:``). The command
    already holds the resolved model and effort, so nothing in the session needs them.

    The session gets a home and a temp dir of its own. The sandbox answers a hidden path with
    permission-denied, which tools treat as an error where a missing file is routine (git stops
    on an unreadable ``~/.gitconfig``), so the session's home is an empty dir where those
    lookups find nothing. ``XDG_*`` vars would send the same lookups back to the operator's
    home, so they are dropped. claude's default temp dir is shared with every other session on
    the machine, so it is pointed at the session's.
    """
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("SMEVALS_", "XDG_"))
    }
    env["HOME"] = str(session_home)
    env["TMPDIR"] = str(session_tmp)
    env["CLAUDE_CODE_TMPDIR"] = str(session_tmp)
    return env


def stop(proc: subprocess.Popen) -> None:
    """End a session that will not end by itself: ask first, then kill."""
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def exit_code(proc: subprocess.Popen) -> int:
    """Report a finished session's exit as a shell does: a signal death is 128 plus the signal."""
    rc = proc.returncode
    return 128 - rc if rc < 0 else rc


def wait_under_watchdog(proc: subprocess.Popen, timeout_secs: float) -> int:
    """Wait for the session, killing one that outlives the timeout; return the run's exit code.

    A killed session is failed, not graded.
    """
    try:
        proc.wait(timeout=timeout_secs)
    except subprocess.TimeoutExpired:
        stop(proc)
    return exit_code(proc)


class Quiet:
    """Since when the stream's latest line has been a result event; None while it is not.

    A session that has printed a result has finished a turn. One that stays alive after it is
    waiting on work it left in the background, and any further line means another turn began.
    """

    since: float | None = None


def is_result(line: bytes) -> bool:
    """Tell whether a stream line is a result event."""
    try:
        event = json.loads(line)
    except ValueError:
        return False
    return isinstance(event, dict) and event.get("type") == "result"


def wait_for_stream_session(
    proc: subprocess.Popen, timeout_secs: float, idle_secs: float | None, quiet: Quiet
) -> tuple[int, bool]:
    """Wait for a stream-mode session; return the run's exit code and whether it ended idle.

    The watchdog applies as in any session. With an idle limit, a session that has printed a
    result and stayed quiet for the limit is ended too. That session finished its turn and has
    its answer, so the run succeeds. Quiet before a result is not idleness: a long tool call
    and a long reply print nothing either.
    """
    deadline = time.monotonic() + timeout_secs
    while True:
        try:
            proc.wait(timeout=STREAM_POLL_SECS)
        except subprocess.TimeoutExpired:
            now = time.monotonic()
            since = quiet.since
            if idle_secs is not None and since is not None and now - since >= idle_secs:
                stop(proc)
                return 0, True
            if now >= deadline:
                stop(proc)
                return exit_code(proc), False
        else:
            return exit_code(proc), False


def run_session(
    cmd: list[str], work_dir: Path, env: dict[str, str], result_file: Path, timeout_secs: float
) -> int:
    """Run claude in the work dir under the watchdog, its one result record going to a file."""
    with result_file.open("wb") as out:
        proc = subprocess.Popen(cmd, cwd=work_dir, stdin=subprocess.DEVNULL, stdout=out, env=env)
        return wait_under_watchdog(proc, timeout_secs)


def stamp(log: BinaryIO, line: bytes) -> None:
    """Record one stream line with the time it arrived, in epoch seconds to the millisecond."""
    log.write(f"{time.time():.3f} ".encode() + line + b"\n")
    log.flush()


def stamp_stream(stdout: BinaryIO, log: BinaryIO, exited: threading.Event, quiet: Quiet) -> None:
    """Stamp each line claude prints as it arrives, until claude has exited and nothing is ready.

    The pipe is polled, not read to its end: a background job the subject started inherits
    the pipe and can hold it open long after claude exits, and the run must not wait on it.
    Once claude has exited, reading stops when the pipe is empty or after ``STREAM_DRAIN_SECS``,
    whichever comes first, so a job that keeps writing cannot hold the run either. A line split
    across reads is stamped when its newline arrives; a final line with none is stamped at the
    end.
    """
    descriptor = stdout.fileno()
    pending = b""
    drain_until = None
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        while True:
            if exited.is_set() and drain_until is None:
                drain_until = time.monotonic() + STREAM_DRAIN_SECS
            if drain_until is not None and time.monotonic() >= drain_until:
                break
            if not selector.select(0 if exited.is_set() else STREAM_POLL_SECS):
                if exited.is_set():
                    break
                continue
            chunk = os.read(descriptor, STREAM_CHUNK_BYTES)
            if not chunk:
                break
            *lines, pending = (pending + chunk).split(b"\n")
            for line in lines:
                stamp(log, line)
                quiet.since = time.monotonic() if is_result(line) else None
    if pending:
        stamp(log, pending)


def run_stream_session(
    cmd: list[str],
    work_dir: Path,
    env: dict[str, str],
    stream_file: Path,
    timeout_secs: float,
    idle_secs: float | None,
) -> int:
    """Run claude in the work dir under the watchdog, recording its event stream as it arrives.

    The runner stamps a line of its own at launch and at exit, so a reader can count the quiet
    time before the session's first event and after its last. The exit line carries the run's
    exit code and, for a session ended on the idle limit, ``"ended": "idle"``.
    """
    with stream_file.open("wb") as log:
        stamp(log, json.dumps({"type": "runner_launch"}).encode())
        proc = subprocess.Popen(
            cmd, cwd=work_dir, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, env=env
        )
        stdout = proc.stdout
        if stdout is None:
            fail("claude was started without a stdout pipe; this is a bug in the runner")
        exited = threading.Event()
        quiet = Quiet()
        reader = threading.Thread(target=stamp_stream, args=(stdout, log, exited, quiet))
        reader.start()
        rc, idle = wait_for_stream_session(proc, timeout_secs, idle_secs, quiet)
        exited.set()
        reader.join()
        stdout.close()
        exit_line: dict[str, str | int] = {"type": "runner_exit", "returncode": rc}
        if idle:
            exit_line["ended"] = "idle"
        stamp(log, json.dumps(exit_line).encode())
    return rc


def copy_back(work_dir: Path, run_dir: Path) -> None:
    """Bring the subject's worked tree into the run dir, under its own subdirectory.

    The worked tree is the subject's, copied as-is, symlinks included. Nothing else writes
    inside that subdirectory, so no name the subject left can sit where the runner or smevals
    writes next.
    """
    shutil.copytree(work_dir, run_dir / WORKED_DIR, symlinks=True)


def emit_result(result_file: Path, condition_file: Path) -> None:
    """Print the result record's text as claude's text mode would, recording the models it used.

    Anything that is not a result record passes through untouched; the session's exit code is
    the run's verdict either way.
    """
    raw = result_file.read_text(encoding="utf-8", errors="replace")
    try:
        record = json.loads(raw)
        result = record["result"]
        models = sorted(record["modelUsage"])
        if not isinstance(result, str):
            raise TypeError("result is not text")
    except ValueError, KeyError, TypeError:
        sys.stdout.write(raw)
        return
    sys.stdout.write(result + "\n")
    with condition_file.open("a", encoding="utf-8") as condition:
        condition.write("resolved: " + ", ".join(models) + "\n")


def emit_stream_result(stream_file: Path, condition_file: Path) -> None:
    """Print the text of the stream's last result event, recording the models it reports.

    A session whose background job outlives a turn reports one result per turn; the last is
    the run's. A stream with none (a watchdog-killed session) prints nothing, and says so on
    stderr; the session's exit code is the run's verdict either way.
    """
    found = None
    # Split on newlines only: claude prints JSON unescaped, so a U+2028 inside a string is text.
    for line in stream_file.read_text(encoding="utf-8", errors="replace").split("\n"):
        try:
            event = json.loads(line.split(" ", 1)[1])
            result = event["result"]
            models = sorted(event["modelUsage"])
        except IndexError, ValueError, KeyError, TypeError:
            continue
        if event.get("type") == "result" and isinstance(result, str):
            found = (result, models)
    if found is None:
        print(f"{PROG}: no result event in {STREAM_FILE}", file=sys.stderr)
        return
    sys.stdout.write(found[0] + "\n")
    with condition_file.open("a", encoding="utf-8") as condition:
        condition.write("resolved: " + ", ".join(found[1]) + "\n")


def main(argv: list[str] | None = None) -> int:
    """Run one smevals task: validate, stage, run the session, copy back, report."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    config_model = os.environ.get("SMEVALS_MODEL", "")
    if not config_model:
        fail("SMEVALS_MODEL is not set; run under smevals or export it")
    run_dir = Path.cwd()
    # smevals sets each run's cwd to its own run dir, which never carries a .git. A hand
    # invocation from a checkout does, and the run would leave its worked tree and records in
    # that checkout. Refuse before anything is staged or a session is paid for.
    if (run_dir / ".git").exists() or (run_dir / ".git").is_symlink():
        fail(
            f"refusing to run in a git work-tree root: {run_dir}; the run would leave its worked "
            "tree and records in the checkout; run under smevals, which sets cwd to a run dir"
        )
    worked = run_dir / WORKED_DIR
    if worked.exists() or worked.is_symlink():
        fail(
            f"refusing to run in a run dir that already holds {WORKED_DIR}/: {run_dir}; "
            "the copy-back needs that name; run under smevals, which makes a fresh run dir"
        )
    sandbox_exec = shutil.which("sandbox-exec")
    if sandbox_exec is None:
        fail(
            "sandbox-exec not found on PATH; the subject session runs under the macOS sandbox, "
            "so this runner needs macOS"
        )
    claude = shutil.which("claude")
    if claude is None:
        fail("claude not found on PATH; install Claude Code or fix PATH")
    if args.idle_timeout is not None and not args.stream:
        fail("--idle-timeout needs --stream: a session's quiet is read from its event stream")
    home = os.environ.get("HOME", "")
    for kind, context in args.context:
        if kind == "rules" and not Path(context).is_dir():
            fail(f"--rules directory not found: {context}")
        if kind == "file" and not Path(context).is_file():
            fail(f"--context file not found: {context}")
    for staged in args.stage:
        if not Path(staged).exists():
            fail(f"--stage path not found: {staged}")
    for allowed in args.allow_read:
        if not Path(allowed).exists():
            fail(f"--allow-read path not found: {allowed}")
    allow_read = [Path(allowed).resolve() for allowed in args.allow_read]
    if args.subject is not None and not Path(args.subject).is_file():
        fail(f"--subject file not found: {args.subject}")
    if args.plugin_root is not None and not Path(args.plugin_root).is_dir():
        fail(f"--plugin-root directory not found: {args.plugin_root}")
    mcp_config = None
    if args.mcp_config is not None:
        if not Path(args.mcp_config).is_file():
            fail(f"--mcp-config file not found: {args.mcp_config}")
        mcp_config = os.path.abspath(args.mcp_config)
    context_text = assemble_context(args.context, home)
    model, effort = resolve_condition(config_model, args.subject)
    # Everything in the condition record but the models the session reports is known here,
    # before the session starts, so two runs of one condition differ in that line at most.
    condition = [f"runner: {PROG} {version(PROG)}\n", f"claude: {claude_version(claude)}\n"]
    condition.append(f"model: {model}\n")
    if effort:
        condition.append(f"effort: {effort}\n")
    if context_text is not None:
        digest = hashlib.sha256(context_text.encode("utf-8")).hexdigest()
        condition.append(f"context-sha256: {digest}\n")
    if args.claude_args:
        condition.append(f"claude-args: {json.dumps(args.claude_args)}\n")
    condition += [f"allow-read: {allowed}\n" for allowed in allow_read]
    if args.plugin_root is not None:
        condition.append(f"plugin-root: {Path(args.plugin_root).resolve()}\n")
    if args.idle_timeout is not None:
        condition.append(f"idle-timeout: {args.idle_timeout:g}\n")
    # Command substitution in the shell runners strips trailing newlines from the prompt.
    prompt = sys.stdin.read().rstrip("\n")

    # Resolved, because the sandbox profile matches the paths the kernel resolves to. Every
    # run's dirs live under one base dir that the profile hides, so a session cannot see a
    # sibling run; its own dirs are opened again by name.
    operator_home = Path.home().resolve()
    keychains = operator_home / "Library" / "Keychains"
    base = Path(tempfile.gettempdir()).resolve() / PROG
    base.mkdir(mode=0o700, exist_ok=True)
    # The name is fixed, so on a shared temp root someone else could have put it there first.
    if base.is_symlink() or base.stat().st_uid != os.getuid():
        fail(
            f"refusing to use {base} for run dirs: it is a link or belongs to another user; "
            "remove it and run again"
        )
    session_root = Path(tempfile.mkdtemp(dir=base))
    try:
        work_dir = session_root / "work"
        session_home = session_root / "home"
        session_tmp = session_root / "tmp"
        for directory in (work_dir, session_home / "Library", session_tmp):
            directory.mkdir(parents=True)
        # OAuth reads the login keychain, which is found through the home dir. A refreshed
        # token is written back to it, so the session can write there too. The write needs the
        # whole directory: a keychain item is saved through a temp file beside the keychain, and
        # with the directory read-only or only the keychain file writable the save is refused
        # (measured with the security CLI on Darwin 25.6.0).
        (session_home / "Library" / "Keychains").symlink_to(keychains)
        stage(args.stage, work_dir)
        # The run dir is hidden wherever it is: it holds the runner's records of this
        # session, and its neighbours are other runs.
        hidden = [
            Path("/Users"),
            operator_home,
            base,
            Path(f"/private/tmp/claude-{os.getuid()}"),
            run_dir.resolve(),
        ]
        own = [work_dir, session_home, session_tmp, keychains]
        # The claude executable, by the path it is run by and the file that resolves to.
        readable = [Path(os.path.abspath(claude)), Path(claude).resolve(), *allow_read]
        context_file = None
        if context_text is not None:
            context_file = session_root / "context.md"
            context_file.write_text(context_text, encoding="utf-8")
            readable.append(context_file)
        if mcp_config is not None:
            readable.append(Path(mcp_config).resolve())
        if args.plugin_root is not None:
            # A copy, because the tree itself is usually under the home dir: opening it there
            # would put the operator's path in the prompt and its neighbours one step away.
            session_plugin = session_root / "plugin"
            shutil.copytree(args.plugin_root, session_plugin, symlinks=True)
            readable.append(session_plugin)
            prompt = prompt.replace(PLUGIN_ROOT_VARIABLE, str(session_plugin))
        result_file = session_root / "result.json"
        cmd = [sandbox_exec, "-p", sandbox_profile(hidden, own, readable)]
        cmd += build_command(
            claude,
            model,
            effort,
            mcp_config,
            context_file,
            args.claude_args,
            prompt,
            stream=args.stream,
        )
        env = session_env(session_home, session_tmp)
        # The stream is written straight into the run dir as it arrives: it is the runner's
        # record, out of the subject's reach, and it survives a session that has to be killed.
        stream_file = run_dir / STREAM_FILE
        if args.stream:
            rc = run_stream_session(
                cmd, work_dir, env, stream_file, args.timeout, args.idle_timeout
            )
        else:
            rc = run_session(cmd, work_dir, env, result_file, args.timeout)
        copy_back(work_dir, run_dir)
        if context_text is not None:
            (run_dir / "subject-context.md").write_text(context_text, encoding="utf-8")
        condition_file = run_dir / "subject-condition.md"
        condition_file.write_text("".join(condition), encoding="utf-8")
        if args.stream:
            emit_stream_result(stream_file, condition_file)
        else:
            emit_result(result_file, condition_file)
        return rc
    finally:
        shutil.rmtree(session_root, ignore_errors=True)
