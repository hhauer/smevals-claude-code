# smevals-claude-code

A reusable [smevals](https://github.com/prime-radiant-inc/smevals) Runner that drives an isolated headless Claude Code session. It stages fixtures into a temp work dir outside any repo, runs `claude -p --safe-mode` there under the model and effort the smevals config names, strips smevals task metadata from the subject's environment, kills a hung session, and copies the worked tree back into `worked/` in the run dir with a record of the condition the session ran under.

The session runs under the macOS sandbox, so the runner needs macOS. The subject cannot read or write your home directory, other users' homes, or another run's work dir, which keeps a suite's skills, fixtures, and checkers out of its reach. It gets a home and a temp dir of its own; the home holds only a git config with a fixed identity (`Eval Subject <subject@example.invalid>`), so a subject that commits does so under the same name on every machine. The rest of the system stays readable so installed tools keep working, and so do the shared temp dirs (`/tmp` and the per-user temp root) outside the runner's own.

## Install

```bash
uv tool install "smevals-claude-code @ git+https://github.com/hhauer/smevals-claude-code@v0.4.0"
```

Pin a tag. A new version can change the `claude` command line or what the session can reach, which is the condition every eval anchor was measured under.

## Use

A smevals config names the runner; the per-eval runner script builds the prompt and pipes it in:

```bash
printf '%s' "$prompt" | smevals-claude-code \
  --stage "$CORPUS" \
  --subject "$SKILL_MD" \
  --context "$HOME/.claude/CLAUDE.md" \
  -- --allowedTools "Bash(rg:*)" --disallowedTools Agent
```

House rules kept in a rules directory are added with `--rules`, placed among the `--context` flags where they load in production (after the `CLAUDE.md` beside the directory). Only the always-on rules are added, in the order Claude Code loads them; a rule with `paths:` frontmatter is left out:

```bash
smevals-claude-code \
  --context "$HOME/.claude/CLAUDE.md" \
  --context "$TREE/.claude/CLAUDE.md" \
  --rules "$TREE/.claude/rules"
```

A tool or file the subject needs from your home directory, such as a language runtime installed there, is opened read-only with `--allow-read`:

```bash
smevals-claude-code --stage "$CORPUS" --allow-read "$HOME/.local/share/mise/installs/python"
```

Install paths differ per machine and per version, so ask the tool that owns the install for the path. With mise that is `--allow-read "$(mise where python)"`.

The session runs with `--permission-mode acceptEdits`, under which Claude Code refuses a write to `.claude/` in the work dir when nobody is there to approve it, and an `--allowedTools` rule for the path does not change that (measured on Claude Code 2.1.321). A suite that needs such a file should have the subject write it elsewhere in the work dir.

A subject whose skill text refers to its plugin's bundled files through `${CLAUDE_PLUGIN_ROOT}` names the plugin tree with `--plugin-root`. The session gets a read-only copy, and every `${CLAUDE_PLUGIN_ROOT}` in the prompt becomes the copy's path:

```bash
printf '%s' "$prompt" | smevals-claude-code --stage "$CORPUS" --plugin-root "$PLUGIN_DIR"
```

Graders read the subject's files from `worked/` in the run dir, for example `$SMEVALS_RUN_DIR/worked/report.md`.

Every run writes `subject-condition.md` in the run dir: the runner and Claude Code versions, the model and effort, a SHA-256 of the assembled context, the arguments forwarded to `claude`, and each `--allow-read` grant. Every line except `resolved:` is settled before the session starts, so a suite can check a run against a record it kept from an earlier one:

```bash
diff <(grep -v '^resolved:' anchor-condition.md) \
     <(grep -v '^resolved:' "$SMEVALS_RUN_DIR/subject-condition.md")
```

A suite that grades what happened during the session, such as how long the subject went quiet, passes `--stream`. The runner then records every event `claude` prints in `subject-stream.jsonl` in the run dir, one `<epoch seconds> <event JSON>` line each, stamped when it arrived. The first and last lines are the runner's own, `runner_launch` and `runner_exit`.

A session that leaves a command running in the background prints its answer, waits for the command, and then takes another turn whose short message replaces the answer. Every turn's result is in `subject-stream.jsonl`. To end such a session once it has answered, add `--idle-timeout`:

```bash
smevals-claude-code --stage "$CORPUS" --stream --idle-timeout 30
```

The session is stopped when it has printed a result and then stayed quiet for that many seconds. The run exits 0 with that result, and the `runner_exit` line carries `"ended": "idle"`. A session that is quiet before it has printed any result is left to `--timeout`.

`smevals-claude-code --help` documents every flag.

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
