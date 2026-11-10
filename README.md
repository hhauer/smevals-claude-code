# smevals-claude-code

A reusable [smevals](https://github.com/prime-radiant-inc/smevals) Runner that drives an isolated headless Claude Code session. It stages fixtures into a temp work dir outside any repo, runs `claude -p --safe-mode` there under the model and effort the smevals config names, strips smevals task metadata from the subject's environment, kills a hung session, and copies the worked tree back into `worked/` in the run dir with a record of the condition the session ran under.

The session runs under the macOS sandbox, so the runner needs macOS. The subject cannot read or write your home directory, other users' homes, or another run's work dir, which keeps a suite's skills, fixtures, and checkers out of its reach. It gets an empty home and a temp dir of its own. The rest of the system stays readable so installed tools keep working, and so do the shared temp dirs (`/tmp` and the per-user temp root) outside the runner's own.

## Install

```bash
uv tool install "smevals-claude-code @ git+https://github.com/hhauer/smevals-claude-code@v0.2.0"
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

A tool or file the subject needs from your home directory, such as a language runtime installed there, is opened read-only with `--allow-read`:

```bash
smevals-claude-code --stage "$CORPUS" --allow-read "$HOME/.local/share/mise/installs/python"
```

Graders read the subject's files from `worked/` in the run dir, for example `$SMEVALS_RUN_DIR/worked/report.md`.

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
