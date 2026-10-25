# hermes-plugin-diff-review

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that reviews git diffs for common code quality issues before committing or opening a PR.

## Install

From the Hermes plugin catalog:

```bash
hermes plugins install diff-review --enable
```

Or from a clone:

```bash
cd ~/.hermes/plugins
git clone https://github.com/sprmn24/hermes-plugin-diff-review diff-review
hermes plugins enable diff-review
```

Plugins are opt-in: an installed plugin does not load until it is enabled. Restart Hermes afterwards and check with `/plugins` or `hermes plugins list`.

## What it does

Provides a `diff_review` tool that reads a unified diff and flags, on added lines:

- Debug `print()` / `pprint()` calls
- `console.log/debug/info/warn/error/trace` calls
- Hardcoded secrets: passwords, API keys, access/private keys, tokens (Python, JS, JSON, YAML and `.env` forms)
- Bare `except:` clauses
- Broad `except Exception` / `except BaseException` catches (also `as e` and tuples)
- Unresolved `TODO` / `FIXME` / `HACK` / `XXX` markers

## Usage

Ask Hermes naturally:

```
review my current git diff
check my changes against origin/main before I open a PR
```

Or reference the tool directly:

```
use diff_review
use diff_review with target=origin/main
```

| Call | What is reviewed | git equivalent |
|---|---|---|
| `{}` | Working tree (staged, unstaged and untracked files) against HEAD | `git diff HEAD` + untracked files |
| `{base: "v1.0"}` | Working tree against `v1.0` | `git diff v1.0` + untracked files |
| `{target: "origin/main"}` | Commits on HEAD since it branched from `origin/main`: the PR view | `git diff origin/main...HEAD` |
| `{base: "feature", target: "origin/main"}` | Commits on `feature` since it branched from `origin/main` | `git diff origin/main...feature` |
| `{diff_text: "..."}` | The diff you pass in; git is not run | none |

Target mode is the pull request view. It compares `base` (default `HEAD`) with its merge base with `target`, which is the same diff GitHub and GitLab show for a PR. Changes that landed on `target` after you branched are not shown. Uncommitted changes are not part of a PR, so they are not included either, and `notes` says when there are any.

`target` does not compare two arbitrary refs, such as two tags. To review the plain difference between `A` and `B`, run `git diff A B` yourself and pass its output as `diff_text`.

`base` and `target` take branches, tags or commit SHAs. File paths and option-like values (anything starting with `-`) are rejected with an error.

Git runs in the agent's workspace, which Hermes's file tools also use (the terminal's working directory). When the terminal backend is a container or SSH host, the tool returns an `unsupported_backend` error instead of reviewing the wrong directory. In that case, run `git diff` in the terminal and pass the output as `diff_text`.

## Output

The tool returns JSON:

| Field | Meaning |
|---|---|
| `status` | `ok`, `no_diff` or `error` |
| `message` | For `no_diff` (`No differences found.`) and `error` |
| `error`, `error_type` | Only on errors. `error_type` is one of `invalid_argument`, `not_a_repository`, `unknown_revision`, `no_merge_base`, `git_not_found`, `git_failed`, `timeout`, `unsupported_backend` |
| `files_changed`, `total_added`, `total_removed` | True totals, including untracked files |
| `files` | `{path, added, removed, status}` per file, plus `old_path` for renames/copies and `binary: true` for binary files. `status` is `added`, `modified`, `deleted`, `renamed`, `copied`, `mode_changed`, `binary` or `untracked`. At most 500 entries |
| `files_truncated` | `true` when `files` was cut |
| `warnings` | `{file, line, issue}` per finding; `line` is the line number in the new file. At most 200 entries |
| `warning_count` | Total number of findings, which can be more than `len(warnings)` |
| `warnings_truncated` | `true` when `warnings` was cut |
| `untracked_files` | Untracked, non-ignored files (working-tree mode only; at most 200) |
| `comparison` | `{mode, base, target, merge_base, repo_root}`; `mode` is `worktree`, `merge_target` or `diff_text` |
| `notes` | Anything worth knowing about the result: skipped untracked files, uncommitted changes left out in target mode, ignored arguments |

Results are kept under 90,000 characters, below Hermes's default 100,000-character tool result budget. Lists are shortened (and flagged) if needed.

## What it runs and reads

- Runs the local `git` executable (`rev-parse`, `diff`, `merge-base`, `ls-files`, `hash-object`) in the workspace repository. Git's color, prefix, external diff, textconv and relative-path settings are overridden so they cannot change the output. A git process that runs longer than 30 seconds is killed together with its children.
- Reads untracked, non-ignored files in the repository so new files are reviewed too. It reads regular files only, never follows symlinks, and stops at 1 MiB per file, 200 files and 5 MiB in total. Skipped files are listed in `notes`.
- To find the workspace and terminal backend, it uses Hermes's file-tool path helpers (`tools.file_tools_paths`) when they are available, then `agent.runtime_cwd.resolve_agent_cwd()`, then `TERMINAL_CWD`. The first of these is a private Hermes module, so `hermes plugins validate` lists it as a private import. Every call to it is guarded, and the tool falls back to the next step if it is missing or changes.
- No network access, no credentials, nothing is written.

## Limitations

- Detection is regex-based and line-by-line. Expect false positives (for example a `print(` inside a string, or a placeholder value assigned to `token`) and misses (secrets split across lines, built by concatenation, or under key names outside the list above).
- Language coverage: the print and except rules target Python, the console rule targets JavaScript/TypeScript. The secret and TODO rules are language-agnostic. All rules run on every file type, including docs.
- Only added lines are checked; removed and context lines are not.
- Submodule changes are counted as one changed line; the submodule's own contents are not reviewed.

## Changes in 1.1.0

- **Changed meaning of `target`.** `{target: T}` and an explicit `{base: B, target: T}` now review what `B` (default `HEAD`) adds relative to `T`, using the merge base (`git diff T...B`). In 1.0.0 `{target: "origin/main"}` ran `git diff HEAD origin/main`, which is reversed: feature-branch additions showed up as removals and nothing was flagged. An explicit `{base: X, target: Y}` used to mean "from X to Y"; for that, pass `git diff X Y` output as `diff_text`.
- Git failures (not a repository, unknown revision, git missing, timeout) now return `status: "error"` with `message` and `error_type`, instead of `no_diff`.
- The tool runs in the agent's workspace instead of the Hermes process directory, and refuses container/SSH backends.
- `base`/`target` are validated: option-like values and file paths are rejected.
- Untracked files are reviewed and listed. A repository with no commits yet is compared against an empty tree.
- The parser handles deleted, binary, renamed, mode-only and quoted paths, content lines starting with `++`/`--`, missing final newlines, plain `diff -u` output and combined merge diffs.
- Detection: `except Exception as e`, more console methods, more secret forms and key names, and word boundaries for print and TODO markers.
- New output fields: `error`, `error_type`, `files_truncated`, `warnings_truncated`, `untracked_files`, `comparison`, `notes`, and `status` on each file. `warning_count` is now the total number found, which can be more than `len(warnings)`.

## Plugin structure

```
diff-review/
├── plugin.yaml   # manifest
├── __init__.py   # registration
├── schemas.py    # LLM tool schema
├── tools.py      # handler logic
└── tests/        # pytest suite (real temporary git repositories)
```

## Requirements

- `git` 2.28 or newer in PATH
- Python 3.11 or newer (as required by Hermes); no additional Python dependencies

## Development

```bash
python3 -m pip install pytest
python3 -m pytest -q
```

## License

MIT, see [LICENSE](LICENSE).
