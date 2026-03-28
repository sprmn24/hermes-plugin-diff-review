"""Tool handlers for diff-review plugin."""

import json
import logging
import re
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_WARN_PATTERNS: List[tuple] = [
    (r"print\s*\(", "debug print statement"),
    (r"console\.log\s*\(", "console.log statement"),
    (r"(?i)(password|secret|api_key|token)\s*=\s*['\"][^'\"]+['\"]", "possible hardcoded secret"),
    (r"except\s*:", "bare except clause"),
    (r"except\s+Exception\s*:", "broad Exception catch"),
    (r"TODO|FIXME|HACK|XXX", "unresolved TODO/FIXME"),
]


def _get_git_diff(base: str = "HEAD", target: Optional[str] = None) -> str:
    cmd = ["git", "diff", base]
    if target:
        cmd.append(target)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.warning("git diff exited with code %d: %s", result.returncode, result.stderr)
            return ""
        return result.stdout
    except FileNotFoundError:
        logger.error("git not found in PATH")
        return ""
    except subprocess.TimeoutExpired:
        logger.error("git diff timed out")
        return ""


def _parse_diff(diff_text: str) -> Dict[str, Any]:
    files: List[Dict[str, Any]] = []
    current_file: Optional[Dict[str, Any]] = None
    warnings: List[Dict[str, str]] = []
    total_added = 0
    total_removed = 0
    line_number = 0

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git"):
            if current_file:
                files.append(current_file)
            current_file = {"path": "", "added": 0, "removed": 0}
            continue
        if raw_line.startswith("+++ b/") and current_file is not None:
            current_file["path"] = raw_line[6:]
            continue
        if raw_line.startswith("@@"):
            match = re.search(r"\+(\d+)", raw_line)
            if match:
                line_number = int(match.group(1)) - 1
            continue
        if current_file is None:
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            line_number += 1
            current_file["added"] += 1
            total_added += 1
            for pattern, label in _WARN_PATTERNS:
                if re.search(pattern, raw_line[1:]):
                    warnings.append({"file": current_file.get("path", "unknown"), "line": line_number, "issue": label})
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            current_file["removed"] += 1
            total_removed += 1
        else:
            if not raw_line.startswith(("---", "+++")):
                line_number += 1

    if current_file:
        files.append(current_file)

    return {
        "files_changed": len(files),
        "total_added": total_added,
        "total_removed": total_removed,
        "files": files,
        "warnings": warnings,
        "warning_count": len(warnings),
    }


def diff_review(args: dict, **kwargs) -> str:
    base = args.get("base", "HEAD")
    target = args.get("target")
    diff_text = args.get("diff_text")

    if diff_text is None:
        diff_text = _get_git_diff(base=base, target=target)

    if not diff_text.strip():
        return json.dumps({
            "status": "no_diff",
            "message": "No differences found.",
            "files_changed": 0,
            "total_added": 0,
            "total_removed": 0,
            "files": [],
            "warnings": [],
            "warning_count": 0,
        })

    review = _parse_diff(diff_text)
    review["status"] = "ok"
    return json.dumps(review, indent=2)
