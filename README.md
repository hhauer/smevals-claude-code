# smevals-claude-code

A reusable [smevals](https://github.com/prime-radiant-inc/smevals) Runner that drives an isolated headless Claude Code session. It stages fixtures into a temp work dir outside any repo, runs `claude -p --safe-mode` there under the model and effort the smevals config names, strips smevals task metadata from the subject's environment, kills a hung session, and copies the worked tree back into the run dir with a record of the condition the session ran under.

## Install

```bash
uv tool install "smevals-claude-code @ git+https://github.com/hhauer/smevals-claude-code@v0.1.0"
```

Pin a tag. A new version can change the `claude` command line, which is the condition every eval anchor was measured under.

## Use

A smevals config names the runner; the per-eval runner script builds the prompt and pipes it in:

```bash
printf '%s' "$prompt" | smevals-claude-code \
  --stage "$CORPUS" \
  --subject "$SKILL_MD" \
  --context "$HOME/.claude/CLAUDE.md" \
  -- --allowedTools "Bash(rg:*)" --disallowedTools Agent
```

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
