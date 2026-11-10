# smevals-claude-code

A reusable [smevals](https://github.com/prime-radiant-inc/smevals) Runner that drives an isolated headless Claude Code session. It stages fixtures into a temp work dir outside any repo, runs `claude -p --safe-mode` there under the model and effort the smevals config names, strips smevals task metadata from the subject's environment, kills a hung session, and copies the worked tree back into `worked/` in the run dir with a record of the condition the session ran under.

The session runs under the macOS sandbox, so the runner needs macOS.

## Install

```bash
uv tool install "smevals-claude-code @ git+https://github.com/hhauer/smevals-claude-code@v0.5.0"
```

Pin a tag. A new version can change the `claude` command line or what the session can reach, which is the condition every eval anchor was measured under.

## How a run works

A smevals config names the runner. The per-eval runner script builds the prompt and pipes it in on stdin:

```bash
printf '%s' "$prompt" | smevals-claude-code \
  --stage "$CORPUS" \
  --context "$HOME/.claude/CLAUDE.md" \
  -- --allowedTools "Bash(rg:*)" --disallowedTools Agent
```

The runner then does this, in order:

1. Checks every path it was given and fails before `claude` starts if one is missing.
2. Makes a work dir, a home, and a temp dir for the session, outside any repo.
3. Copies each `--stage` path into the work dir.
4. Runs `claude` in the work dir, inside the sandbox.
5. Copies the work dir into `worked/` in the run dir, whether the session passed, failed, or was killed.
6. Writes its records beside `worked/` and prints the session's answer on stdout.

The base command line is the measured condition and does not change unless a flag below says so:

```
claude -p --safe-mode --model <model> --permission-mode acceptEdits --output-format json -- <prompt>
```

### What the runner needs from its caller

| Input | Meaning |
|---|---|
| stdin | The prompt. Trailing newlines are removed. |
| `SMEVALS_MODEL` | Required. The model, passed to `claude --model`. The value `pinned` reads the model from `--subject`. |
| `SMEVALS_CONFIG_EFFORT` | Optional. Becomes `claude --effort`. Refused together with `pinned`. |
| Current directory | The run dir. smevals sets it. The runner refuses a git work-tree root and a directory that already holds `worked/`. |

### What the subject can and cannot reach

- **Hidden, for reading and writing:** `/Users`, your home directory, the run dir, every other run's work dir, and Claude Code's shared temp dir. A suite's skills, fixtures, and checkers are therefore out of reach unless staged.
- **Its own:** the work dir, a temp dir, and a home that holds only a git config with a fixed identity (`Eval Subject <subject@example.invalid>`). A subject that commits does so under that name on every machine.
- **Still readable:** the rest of the system, so installed tools work, and the shared temp dirs (`/tmp` and the per-user temp root) outside the runner's own.
- **Environment:** every `SMEVALS_*` and `XDG_*` variable is removed, and `HOME` and `TMPDIR` point at the session's own dirs. Anything else you export reaches the subject.
- **Not dropped by `--safe-mode`:** the machine's managed settings. See `--hide-managed-settings`.

## Flags

Everything after `--` is forwarded to `claude` unchanged. Use that for per-suite tool rules such as `--allowedTools` and `--disallowedTools`.

### Staging files: `--stage PATH`

Use it for every fixture the subject should find in its work dir. A directory is staged by its contents; a file is copied to the work dir root. Repeatable; a later path overwrites an earlier one where names collide.

```bash
smevals-claude-code --stage "$CORPUS" --stage "$SUITE/task-notes.md"
```

### Production context: `--context FILE` and `--rules DIR`

`--safe-mode` drops every `CLAUDE.md`. Use these flags when the subject runs in production under instruction files and the eval should measure it that way. Give them in the order production loads them.

- `--context FILE` adds one `CLAUDE.md`. Each line that is an `@path` import is expanded in place. A missing import fails the run before `claude` starts.
- `--rules DIR` adds a rules directory's always-on rules, in the order Claude Code loads them. A rule with `paths:` frontmatter is left out, because it is not part of the context a session starts with. Place it after the `CLAUDE.md` that sits beside the directory, which is where its rules load in production.

```bash
smevals-claude-code \
  --context "$HOME/.claude/CLAUDE.md" \
  --context "$TREE/.claude/CLAUDE.md" \
  --rules "$TREE/.claude/rules" \
  --context "$PROJECT/.claude/CLAUDE.md"
```

The assembled text is handed to `claude` as `--append-system-prompt-file` and saved in the run dir as `subject-context.md`.

### Production pins: `--subject FILE`

Use it with `model: pinned` in the smevals config, when the eval should follow the model and effort a skill or agent file pins in its own frontmatter. The runner reads `model:` and `effort:` from the file on every run, so the eval follows the pins as they change. Without `model: pinned` the flag changes nothing.

### Plugin files: `--plugin-root DIR`

Use it when the prompt inlines a skill whose text refers to bundled scripts or references through `${CLAUDE_PLUGIN_ROOT}`. The session gets a read-only copy of the tree, every `${CLAUDE_PLUGIN_ROOT}` in the prompt becomes the copy's path, and the copy is named to `claude` with `--add-dir` so its Read tool will open it. The path differs per run. Name the smallest tree the subject needs: everything under it is readable.

```bash
printf '%s' "$prompt" | smevals-claude-code --stage "$CORPUS" --plugin-root "$PLUGIN_DIR"
```

### Tools under your home: `--allow-read PATH`

Use it when the subject needs a tool or file that lives under your home directory, such as a language runtime a version manager installed. The path is opened read-only and recorded. Repeatable.

Install paths differ per machine and per version, so ask the tool that owns the install for the path:

```bash
smevals-claude-code --stage "$CORPUS" --allow-read "$(mise where python)"
```

The command must also be on the `PATH` the runner is started with.

### Managed settings: `--hide-managed-settings`

Use it when the subject should be measured without the machine's managed settings (`/Library/Application Support/ClaudeCode/managed-settings.json`). `--safe-mode` does not drop them: the subject runs their hooks and is allowed whatever their permission rules allow, on top of the suite's own `--allowedTools`. A suite that depends on a tool being refused needs this flag on any machine that has managed allow rules.

With the flag, Claude Code prints a "Managed settings failed to load" notice on stderr and runs without them. Leave the flag off when production sessions run under those settings and the eval means to measure that.

### MCP servers: `--mcp-config FILE`

Use it when the subject needs MCP servers. `--safe-mode` drops every MCP server, so this flag replaces it: the session runs with `--setting-sources "" --strict-mcp-config --mcp-config FILE` and reaches only the servers the file names. It is the one flag that changes how the session is isolated.

### The event stream: `--stream`

Use it when a grader needs what happened during the session and not only the final answer: the text of every turn, tool calls, or how long the subject went quiet. `claude` is asked for `--output-format stream-json --verbose`, and every line it prints is recorded in `subject-stream.jsonl` in the run dir as `<epoch seconds> <event JSON>`, stamped when it arrived. The first and last lines are the runner's own, `runner_launch` and `runner_exit`. stdout still carries the text of the last result.

### Ending a finished session: `--idle-timeout SECS`

Use it, with `--stream`, when a subject may leave a command running in the background. Such a session prints its answer, stays alive until the command ends, and then takes another turn whose short message replaces the answer. With the flag, a session that has printed a result and then stayed quiet for that many seconds is stopped. The run exits 0 with that result on stdout, and the `runner_exit` line carries `"ended": "idle"` so a grader can tell.

```bash
smevals-claude-code --stage "$CORPUS" --stream --idle-timeout 30
```

A session that is quiet before it has printed any result is left to `--timeout`, because a long tool call is quiet too. Pick a limit longer than the gap between a result and the next turn of a session that is still working.

### The watchdog: `--timeout SECS`

Every session is killed after 540 seconds unless this flag says otherwise. Raise it for a task that legitimately runs longer. A killed session exits non-zero, so smevals fails it without grading, and its worked tree is still copied back.

## What a run leaves behind

| Where | What |
|---|---|
| stdout | The session's final answer, as `claude -p` prints it in text mode. |
| `worked/` | The subject's work dir, copied as it was left. Graders read files from here, for example `$SMEVALS_RUN_DIR/worked/report.md`. |
| `subject-condition.md` | The condition the session ran under. Always written. |
| `subject-context.md` | The assembled `--context` and `--rules` text. Written when there is any. |
| `subject-stream.jsonl` | The event stream. Written under `--stream`. |

The exit code is the session's own. A session the watchdog killed exits non-zero. A run the runner refused before starting `claude` exits 1 with a message on stderr that begins `smevals-claude-code:`.

### Checking a run's condition

`subject-condition.md` holds one `key: value` line for each of: the runner version, the Claude Code version, the model, the effort, a SHA-256 of the assembled context, the arguments forwarded to `claude`, each `--allow-read` grant, hidden managed settings, the plugin root, the idle limit, and, as `resolved:`, the model IDs the session reports having used. A line appears only when it applies.

Every line except `resolved:` is settled before the session starts, so two runs of one condition differ in that line at most. Keep the record from the run an anchor was measured in, and a later run can be checked against it:

```bash
diff <(grep -v '^resolved:' anchor-condition.md) \
     <(grep -v '^resolved:' "$SMEVALS_RUN_DIR/subject-condition.md")
```

## Known limits

- **Writes under `.claude/` are refused.** The session runs with `--permission-mode acceptEdits`, under which Claude Code refuses a write to `.claude/` in the work dir when nobody is there to approve it, and an `--allowedTools` rule for the path does not change that (measured on Claude Code 2.1.321). Have the subject write such a file elsewhere in the work dir.
- **Background commands outlive the session.** The idle limit and the watchdog stop `claude`. A command the subject left running is not stopped by the runner.
- **A change to the managed settings is not recorded.** The condition record says whether they were hidden, not what they held.

## Development

Clone the repository, then:

```bash
just setup
```

That one command installs the pre-commit hook and syncs dependencies against the committed lock.

## Testing

```bash
just test
```
