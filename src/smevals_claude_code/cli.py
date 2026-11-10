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
under ``/Users``, the operator's home, the base dir that holds every run's work dir, or claude's
shared temp dir. It is given an empty home and a temp dir of its own, so the lookups tools make
there find nothing instead of being refused. The rest of the system stays readable, so installed
tools work; a tool or file that lives under the home dir is opened by ``--allow-read``, read-only,
and recorded in ``subject-condition.md``. Residual: the shared temp dirs (``/tmp`` and the
per-user temp root) stay readable outside the runner's base dir, because shells and tools need
them, and the login keychain stays reachable, because OAuth reads it.

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
``subject-context.md`` so the run records what the subject saw. Every run also writes
``subject-condition.md``: the runner version, the model and effort the session was asked for
(under ``pinned``, the pins read from the subject) and, as ``resolved:``, the model IDs the session
reports having used. smevals' ``run.yaml`` records only the config's alias, and aliases move. claude
runs with ``--output-format json`` so the session reports those IDs; the runner prints the record's
result text to stdout exactly as text mode would, so what smevals reads is unchanged. Output that
is not a result record (a watchdog-killed session) passes through verbatim and gets no resolved
line.

``--mcp-config <file>`` is the one opt-in that changes the isolation mechanism. ``--safe-mode``
drops every MCP server, including the ones ``--mcp-config`` names and a cwd ``.mcp.json``, and
``--bare`` never reads OAuth. An MCP session therefore runs with ``--setting-sources ""`` instead,
measured to load no plugin skills and no CLAUDE.md, plus ``--strict-mcp-config`` so the subject
reaches only the servers the file names, never a user-scope production server. Suites that pass no
``--mcp-config`` are unaffected: their argv is byte-identical to before the flag existed.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path
from typing import NoReturn

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
    parser.add_argument(
        "--context",
        action="append",
        default=None,
        metavar="FILE",
        help="a CLAUDE.md the subject sees in production, in load order; repeatable",
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


def append_context(out: list[str], path: str, label: str, home: str) -> None:
    """Append one context file under the harness's section header.

    Each ``@path`` line expands into its own section in place, recursively, as the harness
    does. A missing import is a configuration error, not a subject condition to measure
    under, so it fails before claude is invoked.
    """
    out.append(f"Contents of {path} ({label}):\n\n")
    for line in read_lines(Path(path)):
        if line.startswith("@") and len(line) > 1:
            imported = line[1:]
            if imported.startswith("~"):
                imported = home + imported[1:]
            if not Path(imported).is_file():
                fail(f"@-import not found: {imported} (imported by {path})")
            out.append("\n")
            append_context(out, imported, PROJECT_LABEL, home)
        else:
            out.append(line + "\n")
    out.append("\n")


def assemble_context(paths: list[str], home: str) -> str:
    """Build the system-prompt text from the context files, labeled as the harness labels them."""
    out = [FRAMING]
    for path in paths:
        label = GLOBAL_LABEL if path == f"{home}/.claude/CLAUDE.md" else PROJECT_LABEL
        append_context(out, path, label, home)
    return "".join(out)


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
) -> list[str]:
    """Assemble the claude argv: the measured condition every anchor was taken under."""
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
            "json",
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
            "json",
        ]
    if effort:
        cmd += ["--effort", effort]
    if context_file is not None:
        cmd += ["--append-system-prompt-file", str(context_file)]
    cmd += claude_args
    cmd += ["--", prompt]
    return cmd


def run_session(
    cmd: list[str],
    work_dir: Path,
    session_home: Path,
    session_tmp: Path,
    result_file: Path,
    timeout_secs: float,
) -> int:
    """Run claude in the work dir under a watchdog; a killed session is failed, not graded.

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
    with result_file.open("wb") as out:
        proc = subprocess.Popen(cmd, cwd=work_dir, stdin=subprocess.DEVNULL, stdout=out, env=env)
        try:
            proc.wait(timeout=timeout_secs)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    rc = proc.returncode
    return 128 - rc if rc < 0 else rc


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
    home = os.environ.get("HOME", "")
    for context in args.context:
        if not Path(context).is_file():
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
    mcp_config = None
    if args.mcp_config is not None:
        if not Path(args.mcp_config).is_file():
            fail(f"--mcp-config file not found: {args.mcp_config}")
        mcp_config = os.path.abspath(args.mcp_config)
    context_text = assemble_context(args.context, home) if args.context else None
    model, effort = resolve_condition(config_model, args.subject)
    # Command substitution in the shell runners strips trailing newlines from the prompt.
    prompt = sys.stdin.read().rstrip("\n")

    # Resolved, because the sandbox profile matches the paths the kernel resolves to. Every
    # run's dirs live under one base dir that the profile hides, so a session cannot see a
    # sibling run; its own dirs are opened again by name.
    operator_home = Path.home().resolve()
    keychains = operator_home / "Library" / "Keychains"
    base = Path(tempfile.gettempdir()).resolve() / PROG
    base.mkdir(mode=0o700, exist_ok=True)
    session_root = Path(tempfile.mkdtemp(dir=base))
    try:
        work_dir = session_root / "work"
        session_home = session_root / "home"
        session_tmp = session_root / "tmp"
        for directory in (work_dir, session_home / "Library", session_tmp):
            directory.mkdir(parents=True)
        # OAuth reads the login keychain, which is found through the home dir. A refreshed
        # token is written back to it, so the session can write there too.
        (session_home / "Library" / "Keychains").symlink_to(keychains)
        stage(args.stage, work_dir)
        hidden = [
            Path("/Users"),
            operator_home,
            base,
            Path(f"/private/tmp/claude-{os.getuid()}"),
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
        result_file = session_root / "result.json"
        cmd = [sandbox_exec, "-p", sandbox_profile(hidden, own, readable)]
        cmd += build_command(
            claude, model, effort, mcp_config, context_file, args.claude_args, prompt
        )
        rc = run_session(cmd, work_dir, session_home, session_tmp, result_file, args.timeout)
        copy_back(work_dir, run_dir)
        if context_text is not None:
            (run_dir / "subject-context.md").write_text(context_text, encoding="utf-8")
        condition_file = run_dir / "subject-condition.md"
        lines = [f"runner: {PROG} {version(PROG)}\n", f"model: {model}\n"]
        if effort:
            lines.append(f"effort: {effort}\n")
        lines += [f"allow-read: {allowed}\n" for allowed in allow_read]
        condition_file.write_text("".join(lines), encoding="utf-8")
        emit_result(result_file, condition_file)
        return rc
    finally:
        shutil.rmtree(session_root, ignore_errors=True)
