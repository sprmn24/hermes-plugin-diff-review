"""Tool schemas for diff-review plugin."""

DIFF_REVIEW = {
    "name": "diff_review",
    "description": (
        "Review git changes for common code quality issues on added lines: debug prints, "
        "console calls, hardcoded secrets, bare or broad except clauses, and TODO/FIXME/HACK/XXX markers. "
        "By default reviews the working tree (staged, unstaged and untracked files) against HEAD. "
        "Before opening a PR, pass target='origin/main': this reviews the commits on base (default HEAD) "
        "since it branched from target, like a pull request view (git diff origin/main...HEAD). "
        "git runs in the agent's workspace; on container or SSH terminal backends, run `git diff` in the "
        "terminal and pass its output as diff_text. "
        "Returns JSON with status 'ok', 'no_diff' or 'error' (with message and error_type), per-file counts, "
        "warnings, untracked_files and notes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "base": {
                "type": "string",
                "description": (
                    "Commit to review from (default: HEAD). Without target, the working tree is compared "
                    "against it. With target, the commits on base that are not on target are reviewed. "
                    "Takes a branch, tag or commit SHA; file paths are not accepted."
                ),
                "default": "HEAD",
            },
            "target": {
                "type": "string",
                "description": (
                    "Branch you plan to merge into, e.g. 'origin/main'. When set, reviews what base adds "
                    "relative to target (git diff target...base). Not for comparing two arbitrary refs: "
                    "pass the output of `git diff A B` as diff_text for that."
                ),
            },
            "diff_text": {
                "type": "string",
                "description": (
                    "Raw unified diff to analyze instead of running git (base and target are then ignored)."
                ),
            },
        },
        "required": [],
    },
}
