# hermes-plugin-diff-review

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that reviews git diffs for common code quality issues before committing or opening a PR.

## Install
```bash
cd ~/.hermes/plugins
git clone https://github.com/sprmn24/hermes-plugin-diff-review diff-review
```

Restart Hermes — the plugin loads automatically. Verify with `/plugins`.

## What it does

Provides a `diff_review` tool that analyzes unified diff output and flags:

- Debug `print()` statements
- Hardcoded secrets (passwords, API keys, tokens)
- Bare `except:` clauses
- Broad `except Exception:` catches
- Unresolved `TODO` / `FIXME` / `HACK` comments

## Usage

Ask Hermes naturally:
```
review my current git diff
check my changes against origin/main before I open a PR
```

Or reference the tool directly:
```
use diff_review with target=origin/main
```

## Plugin structure
```
diff-review/
├── plugin.yaml   # manifest
├── __init__.py   # registration
├── schemas.py    # LLM tool schema
└── tools.py      # handler logic
```

## Requirements

- `git` available in PATH
- No additional Python dependencies

## License

MIT
