# ABOUTME: The smevals-claude-code runner: stages fixtures into an isolated work dir outside any
# ABOUTME: repo, drives a headless claude session there, and copies the worked tree back.
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
prompt. The worked tree is copied back afterwards, on failure too, for post-mortems, so graders and
the run layout see what the session left. Residual: the harness's built-in skill listing stays
visible; only ``--bare`` removes it.

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
# Written by the runner after the copy-back; a subject must not get to pre-place them.
RESERVED_NAMES = ("subject-condition.md", "subject-context.md")
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


def build_command(
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
            "claude",
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
            "claude",
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


def run_session(cmd: list[str], work_dir: Path, result_file: Path, timeout_secs: float) -> int:
    """Run claude in the work dir under a watchdog; a killed session is failed, not graded.

    The subject session must not see smevals task metadata: scalar task fields become
    ``SMEVALS_TASK_*`` vars and can carry grading keys (a task's ``expected:``). The command
    already holds the resolved model and effort, so nothing in the session needs them.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("SMEVALS_")}
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
    """Bring the subject's worked tree into the run dir, then clear the names the runner writes.

    The worked tree is the subject's, copied as-is. A reserved name it left there (a symlink to
    an operator file, say) must not be followed by the writes that come next, so it is dropped
    first.
    """
    shutil.copytree(work_dir, run_dir, symlinks=True, dirs_exist_ok=True)
    for name in RESERVED_NAMES:
        reserved = run_dir / name
        if reserved.is_symlink() or reserved.is_file():
            reserved.unlink()
        elif reserved.exists():
            fail(
                f"the subject left {name} as a directory in its worked tree; "
                "refusing to overwrite it"
            )


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
    # invocation from a checkout does, and the copy-back would overwrite that checkout.
    # Refuse before anything is staged or a session is paid for.
    if (run_dir / ".git").exists() or (run_dir / ".git").is_symlink():
        fail(
            f"refusing to run in a git work-tree root: {run_dir}; the copy-back would overwrite "
            "the checkout; run under smevals, which sets cwd to a run dir"
        )
    home = os.environ.get("HOME", "")
    for context in args.context:
        if not Path(context).is_file():
            fail(f"--context file not found: {context}")
    for staged in args.stage:
        if not Path(staged).exists():
            fail(f"--stage path not found: {staged}")
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

    work_dir = Path(tempfile.mkdtemp())
    scratch = Path(tempfile.mkdtemp())
    try:
        stage(args.stage, work_dir)
        context_file = None
        if context_text is not None:
            context_file = scratch / "context.md"
            context_file.write_text(context_text, encoding="utf-8")
        result_file = scratch / "result.json"
        cmd = build_command(model, effort, mcp_config, context_file, args.claude_args, prompt)
        rc = run_session(cmd, work_dir, result_file, args.timeout)
        copy_back(work_dir, run_dir)
        if context_text is not None:
            (run_dir / "subject-context.md").write_text(context_text, encoding="utf-8")
        condition_file = run_dir / "subject-condition.md"
        lines = [f"runner: {PROG} {version(PROG)}\n", f"model: {model}\n"]
        if effort:
            lines.append(f"effort: {effort}\n")
        condition_file.write_text("".join(lines), encoding="utf-8")
        emit_result(result_file, condition_file)
        return rc
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(scratch, ignore_errors=True)
