"""Tool schemas for diff-review plugin."""

DIFF_REVIEW = {
    "name": "diff_review",
    "description": (
        "Review a git diff for common code quality issues. "
        "Detects added/removed lines per file, flags debug prints, "
        "hardcoded secrets, bare except clauses, and unresolved TODOs. "
        "By default diffs the working tree against HEAD. "
        "Pass target='origin/main' to review changes before a PR."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "base": {
                "type": "string",
                "description": "Base git ref to diff from (default: HEAD).",
                "default": "HEAD",
            },
            "target": {
                "type": "string",
                "description": "Target git ref (optional). E.g. 'origin/main'.",
            },
            "diff_text": {
                "type": "string",
                "description": "Raw unified diff string. If provided, skips running git.",
            },
        },
        "required": [],
    },
}
