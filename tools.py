"""Tool handlers for diff-review plugin."""

import json
import logging
import os
import re
import signal
import stat
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

GIT_TIMEOUT = 30
MIN_GIT_VERSION = (2, 28)
MAX_REF_LENGTH = 256
MAX_WARNINGS = 200
MAX_FILES = 500
MAX_UNTRACKED_LISTED = 200
MAX_UNTRACKED_SCANNED = 200
MAX_UNTRACKED_FILE_BYTES = 1024 * 1024
MAX_UNTRACKED_TOTAL_BYTES = 5 * 1024 * 1024
MAX_MESSAGE_CHARS = 500
MAX_LOGGED_STDERR_CHARS = 2048
# The host's default per-result budget is 100,000 characters; stay well under it.
RESULT_BUDGET_CHARS = 90_000

NO_DIFF_MESSAGE = "No differences found."

# Fixed flags so the user's git config cannot change what the parser sees
# (color, prefixes, external diff/textconv drivers, relative paths, submodule format).
_DIFF_CMD = [
    "-c", "core.quotePath=false", "diff",
    "--no-color", "--no-ext-diff", "--no-textconv", "--no-relative",
    "--src-prefix=a/", "--dst-prefix=b/", "--submodule=short", "-M",
]

# Inherited variables that would point git at another repository or change diff output.
_SCRUBBED_ENV = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_PREFIX",
    "GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS", "GIT_CONFIG_PARAMETERS",
})

_REDACTIONS = [
    (re.compile(r"(://)[^/\s@]+@"), r"\1***@"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}"), "***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{8,}"), "***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "***"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{8,}"), "***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "***"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{16,}"), "***"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer ***"),
]


class ReviewError(Exception):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type
        self.message = message


# Detection rules

_SECRET_NAMES = (
    r"(?:password|passwd|secret|secret[_-]?key|api[_-]?key|access[_-]?key|private[_-]?key|token)"
)
_SECRET_QUOTED = re.compile(
    r"(?i)(?<![\w-])[\"']?[\w.-]*?" + _SECRET_NAMES + r"(?![\w-])[\"']?\s*"
    r"(?::\s*[\w\[\]., |]+?\s*)?"          # optional annotation: `token: str = ...`
    r"(?::=|=>|=|:)\s*"
    r"(?P<q>[\"'])(?!(?P=q))[^\"'\n]+(?P=q)"
)
# .env / YAML style: whole line, unquoted value that looks like a credential.
_SECRET_UNQUOTED = re.compile(
    r"(?i)^\s*(?:export\s+)?[\w.-]*?" + _SECRET_NAMES + r"(?![\w-])"
    r"(?:=(?=\S)|\s*:\s+)"
    r"(?!(?:null|none|true|false|yes|no|~)\s*$)"
    r"(?P<v>[^\s\"'#$<{(\[][^\s#()\[\]{}]*)\s*(?:#.*)?$"
)


def _unquoted_secret(text: str) -> bool:
    m = _SECRET_UNQUOTED.search(text)
    if not m:
        return False
    value = m.group("v")
    return len(value) >= 4 and (any(c.isdigit() for c in value) or len(value) >= 16)


def _regex_rule(pattern: str, label):
    compiled = re.compile(pattern)

    def check(text: str) -> Optional[str]:
        m = compiled.search(text)
        if not m:
            return None
        return label(m) if callable(label) else label
    return check


def _secret_rule(text: str) -> Optional[str]:
    if _SECRET_QUOTED.search(text) or _unquoted_secret(text):
        return "possible hardcoded secret"
    return None


_RULES: List[Callable[[str], Optional[str]]] = [
    _regex_rule(r"(?<![\w.])p?print\s*\(", "debug print statement"),
    _regex_rule(r"\bconsole\.(log|debug|info|warn|error|trace)\s*\(",
                lambda m: f"console.{m.group(1)} statement"),
    _secret_rule,
    _regex_rule(r"\bexcept\s*:", "bare except clause"),
    _regex_rule(r"\bexcept\*?\s*\(?\s*(?:[\w.]+\s*,\s*)*(?:Base)?Exception\b", "broad Exception catch"),
    _regex_rule(r"\b(?:TODO|FIXME|HACK|XXX)\b", "unresolved TODO/FIXME"),
]


def _issues(text: str) -> List[str]:
    return [label for label in (rule(text) for rule in _RULES) if label]


# Diff parsing

_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_COMBINED_HUNK_RE = re.compile(r"^(@{3,}) (.*?) \1")
_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _unquote(text: str) -> Tuple[str, str]:
    """Decode a git C-quoted path starting at text[0] == '"'. Returns (path, rest)."""
    out = bytearray()
    i = 1
    while i < len(text):
        c = text[i]
        if c == '"':
            return out.decode("utf-8", "replace"), text[i + 1:]
        if c == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            octal = text[i + 1:i + 4]
            if len(octal) == 3 and all(ch in "01234567" for ch in octal):
                out.append(int(octal, 8) & 0xFF)
                i += 4
                continue
            if nxt in _C_ESCAPES:
                out.append(_C_ESCAPES[nxt])
                i += 2
                continue
        out.extend(c.encode("utf-8"))
        i += 1
    return out.decode("utf-8", "replace"), ""


def _header_path(text: str) -> Optional[str]:
    """Path from a ---/+++ line body; None for /dev/null. Drops a trailing tab/timestamp."""
    if text.startswith('"'):
        path = _unquote(text)[0]
    else:
        path = text.split("\t", 1)[0]
    return None if path == "/dev/null" else path


def _plain_path(text: str) -> str:
    return _unquote(text)[0] if text.startswith('"') else text


def _git_header_paths(rest: str) -> Tuple[Optional[str], Optional[str]]:
    """Old and new paths from the text after 'diff --git '."""
    if rest.startswith('"'):
        old, tail = _unquote(rest)
        tail = tail[1:] if tail.startswith(" ") else tail
        return old, _plain_path(tail) if tail else None
    for i, ch in enumerate(rest):
        if ch != " ":
            continue
        left, right = rest[:i], _plain_path(rest[i + 1:])
        if _strip_prefix(left, "a/") == _strip_prefix(right, "b/"):
            return left, right
    old, sep, new = rest.partition(" b/")
    return (old, "b/" + new) if sep else (rest, None)


def _strip_prefix(path: Optional[str], prefix: str) -> Optional[str]:
    return path[len(prefix):] if path is not None and path.startswith(prefix) else path


class _FileState:
    def __init__(self):
        self.hdr_old: Optional[str] = None
        self.hdr_new: Optional[str] = None
        self.old: Optional[str] = None
        self.new: Optional[str] = None
        self.seen_old = False
        self.seen_new = False
        self.rename_from: Optional[str] = None
        self.rename_to: Optional[str] = None
        self.copied = False
        self.deleted = False
        self.created = False
        self.mode_change = False
        self.binary = False
        self.hunks = 0
        self.added = 0
        self.removed = 0

    def _sides(self) -> Tuple[Optional[str], Optional[str]]:
        old = self.old if self.seen_old else self.hdr_old
        new = self.new if self.seen_new else self.hdr_new
        if (old is not None or new is not None) \
                and (old is None or old.startswith("a/")) and (new is None or new.startswith("b/")):
            old, new = _strip_prefix(old, "a/"), _strip_prefix(new, "b/")
        return old, new

    def path(self) -> str:
        old, new = self._sides()
        return new or self.rename_to or old or self.rename_from or "unknown"

    def to_dict(self) -> Dict[str, Any]:
        old, new = self._sides()
        if self.deleted or (self.seen_new and self.new is None):
            status = "deleted"
        elif self.created or (self.seen_old and self.old is None):
            status = "added"
        elif self.rename_to is not None:
            status = "copied" if self.copied else "renamed"
        elif self.binary:
            status = "binary"
        elif self.mode_change and not self.hunks:
            status = "mode_changed"
        else:
            status = "modified"
        entry: Dict[str, Any] = {"path": self.path(), "added": self.added, "removed": self.removed,
                                 "status": status}
        if status in ("renamed", "copied"):
            entry["old_path"] = self.rename_from or old
        if self.binary:
            entry["binary"] = True
        return entry


class _Collector:
    def __init__(self):
        self.files: List[Dict[str, Any]] = []
        self.warnings: List[Dict[str, Any]] = []
        self.warning_count = 0
        self.total_added = 0
        self.total_removed = 0

    def added_line(self, path: str, line_number: int, text: str) -> None:
        self.total_added += 1
        for issue in _issues(text):
            self.warning_count += 1
            if len(self.warnings) < MAX_WARNINGS:
                self.warnings.append({"file": path, "line": line_number, "issue": issue})

    def removed_line(self) -> None:
        self.total_removed += 1


class _Hunk:
    def __init__(self, state: _FileState, old_left: List[int], new_left: int, new_line: int):
        self.state = state
        self.old_left = old_left  # one counter per parent (combined diffs have several)
        self.new_left = new_left
        self.new_line = new_line

    def done(self) -> bool:
        return self.new_left <= 0 and all(n <= 0 for n in self.old_left)

    def consume(self, line: str, out: _Collector) -> bool:
        """Account for one line; False when the line is not part of this hunk."""
        if line.startswith("\\"):
            return True  # "\ No newline at end of file"
        if self.done():
            return False
        width = len(self.old_left)
        prefix = line[:width].ljust(width)
        if any(c not in " +-" for c in prefix):
            return False
        # A '-' column marks the parents holding a removed line; for lines kept in the
        # result, a ' ' column marks the parents that already had it.
        present = "-" if "-" in prefix else " "
        for k, c in enumerate(prefix):
            if c == present:
                self.old_left[k] -= 1
        if present == "-":
            self.state.removed += 1
            out.removed_line()
            return True
        if "+" in prefix:
            self.state.added += 1
            out.added_line(self.state.path(), self.new_line, line[width:])
        self.new_left -= 1
        self.new_line += 1
        return True


def _start_hunk(line: str, state: _FileState) -> Optional[_Hunk]:
    m = _HUNK_RE.match(line)
    if m:
        old_count = int(m.group(1)) if m.group(1) is not None else 1
        new_count = int(m.group(3)) if m.group(3) is not None else 1
        return _Hunk(state, [old_count], new_count, int(m.group(2)))
    m = _COMBINED_HUNK_RE.match(line)
    if m:
        ranges = m.group(2).split()
        olds = [r for r in ranges if r.startswith("-")]
        news = [r for r in ranges if r.startswith("+")]
        if len(olds) == len(m.group(1)) - 1 and len(news) == 1:
            def count(r):
                start, _, n = r[1:].partition(",")
                return int(start), int(n) if n else 1
            new_start, new_count = count(news[0])
            return _Hunk(state, [count(r)[1] for r in olds], new_count, new_start)
    return None


def _parse_diff(text: str, out: _Collector) -> int:
    """Parse unified, git and combined diffs into *out*. Returns the number of files seen."""
    state: Optional[_FileState] = None
    hunk: Optional[_Hunk] = None
    seen = 0

    def finish():
        nonlocal seen
        if state is not None:
            out.files.append(state.to_dict())
            seen += 1

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for raw in lines:
        line = raw[:-1] if raw.endswith("\r") else raw
        if hunk is not None:
            if hunk.consume(line, out):
                continue
            hunk = None
        if line.startswith("diff --git "):
            finish()
            state = _FileState()
            state.hdr_old, state.hdr_new = _git_header_paths(line[len("diff --git "):])
            continue
        if line.startswith(("diff --cc ", "diff --combined ")):
            finish()
            state = _FileState()
            state.hdr_new = _plain_path(line.split(" ", 2)[2])
            continue
        if line.startswith("@@"):
            if state is None:
                state = _FileState()
            hunk = _start_hunk(line, state)
            if hunk is not None:
                state.hunks += 1
            continue
        if line.startswith("--- "):
            if state is None or state.hunks or state.seen_old:
                finish()
                state = _FileState()
            state.old, state.seen_old = _header_path(line[4:]), True
            continue
        if line.startswith("+++ "):
            if state is None:
                state = _FileState()
            state.new, state.seen_new = _header_path(line[4:]), True
            continue
        if state is None:
            continue
        if line.startswith(("old mode ", "new mode ")):
            state.mode_change = True
        elif line.startswith("deleted file mode "):
            state.deleted = True
        elif line.startswith("new file mode "):
            state.created = True
        elif line.startswith(("rename from ", "copy from ")):
            state.rename_from = _plain_path(line.split(" ", 2)[2])
            state.copied = line.startswith("copy")
        elif line.startswith(("rename to ", "copy to ")):
            state.rename_to = _plain_path(line.split(" ", 2)[2])
        elif line.startswith(("Binary files ", "GIT binary patch")):
            state.binary = True
    finish()
    return seen


# Results

def _redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _shown(value: str) -> str:
    return _clip(_redact(value), 80)


def _result(comparison: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "ok",
        "files_changed": 0,
        "total_added": 0,
        "total_removed": 0,
        "files": [],
        "files_truncated": False,
        "warnings": [],
        "warning_count": 0,
        "warnings_truncated": False,
        "untracked_files": [],
        "comparison": comparison,
        "notes": [],
    }


def _comparison(mode: str, base: Optional[str] = None, target: Optional[str] = None,
                merge_base: Optional[str] = None, repo_root: Optional[str] = None) -> Dict[str, Any]:
    return {"mode": mode, "base": base, "target": target, "merge_base": merge_base, "repo_root": repo_root}


def _fill(result: Dict[str, Any], out: _Collector, file_count: int) -> Dict[str, Any]:
    result["files_changed"] = file_count
    result["total_added"] = out.total_added
    result["total_removed"] = out.total_removed
    result["files"] = out.files[:MAX_FILES]
    result["files_truncated"] = len(out.files) > MAX_FILES
    result["warnings"] = out.warnings
    result["warning_count"] = out.warning_count
    result["warnings_truncated"] = out.warning_count > len(out.warnings)
    return result


def _no_diff(result: Dict[str, Any]) -> Dict[str, Any]:
    result["status"] = "no_diff"
    result["message"] = NO_DIFF_MESSAGE
    return _reorder(result)


def _error(error_type: str, message: str, comparison: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result = _result(comparison or _comparison("worktree"))
    message = _clip(_redact(message), MAX_MESSAGE_CHARS)
    result.update(status="error", message=message, error=message, error_type=error_type)
    return _reorder(result)


def _reorder(result: Dict[str, Any]) -> Dict[str, Any]:
    head = [k for k in ("status", "message", "error", "error_type") if k in result]
    return {**{k: result[k] for k in head}, **{k: v for k, v in result.items() if k not in head}}


def _dump(result: Dict[str, Any]) -> str:
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    flags = {"files": "files_truncated", "warnings": "warnings_truncated", "untracked_files": None}
    while len(text) > RESULT_BUDGET_CHARS:
        key = max(flags, key=lambda k: len(json.dumps(result[k], ensure_ascii=False)))
        if not result[key]:
            break
        result[key] = result[key][:len(result[key]) // 2]
        if flags[key]:
            result[flags[key]] = True
        elif "untracked_files list shortened to fit the result size" not in result["notes"]:
            result["notes"].append("untracked_files list shortened to fit the result size")
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return text


# Git

def _git_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_ENV}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    return env


def _kill(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


def _run_git(args: List[str], cwd: str) -> Tuple[int, bytes, bytes]:
    popen_kwargs: Dict[str, Any] = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True  # a timeout kills textconv/filter children too
    else:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(["git", *args], cwd=cwd, env=_git_env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_kwargs)
    except FileNotFoundError:
        raise ReviewError("git_not_found", "git is not installed or not on PATH") from None
    except PermissionError:
        raise ReviewError("git_not_found", "git could not be started (permission denied)") from None
    except OSError as exc:
        raise ReviewError("git_failed", f"could not run git: {exc.strerror or exc}") from None
    try:
        stdout, stderr = proc.communicate(timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill(proc)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise ReviewError("timeout", f"git {_subcommand(args)} did not finish within {GIT_TIMEOUT} seconds") from None
    return proc.returncode, stdout, stderr


def _subcommand(args: List[str]) -> str:
    return next((a for a in args if not a.startswith("-") and "=" not in a), args[0] if args else "")


def _git_failure(args: List[str], code: int, stderr: bytes) -> ReviewError:
    text = _redact(stderr.decode("utf-8", "replace")).strip()
    logger.warning("git %s exited with code %d: %s", _subcommand(args), code, text[-MAX_LOGGED_STDERR_CHARS:])
    last = text.splitlines()[-1] if text else f"exit code {code}"
    return ReviewError("git_failed", f"git failed: {last}")


def _git_ok(args: List[str], cwd: str) -> bytes:
    code, stdout, stderr = _run_git(args, cwd)
    if code != 0:
        raise _git_failure(args, code, stderr)
    return stdout


def _check_git_version(cwd: str) -> None:
    out = _git_ok(["--version"], cwd).decode("ascii", "replace")
    m = re.search(r"(\d+)\.(\d+)", out)
    if m and (int(m.group(1)), int(m.group(2))) < MIN_GIT_VERSION:
        raise ReviewError("git_failed", "git %d.%d or newer is required (found %s)"
                          % (*MIN_GIT_VERSION, out.strip()))


def _repo_root(workspace: str) -> str:
    code, stdout, stderr = _run_git(["rev-parse", "--show-toplevel"], workspace)
    if code == 0 and stdout.strip():
        return os.fsdecode(stdout.rstrip(b"\r\n"))
    text = stderr.decode("utf-8", "replace").lower()
    if "not a git repository" in text or "must be run in a work tree" in text:
        raise ReviewError("not_a_repository", f"{workspace} is not inside a git work tree")
    raise _git_failure(["rev-parse"], code, stderr)


def _resolve_commit(root: str, ref: str) -> Optional[str]:
    code, stdout, _ = _run_git(["rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}"], root)
    sha = stdout.decode("ascii", "replace").strip()
    return sha if code == 0 and sha else None


def _require_commit(root: str, ref: str) -> str:
    sha = _resolve_commit(root, ref)
    if sha is None:
        raise ReviewError("unknown_revision",
                          f"{_shown(ref)!r} is not a commit in this repository "
                          "(base/target take branches, tags or commit SHAs, not file paths)")
    return sha


def _head_is_unborn(root: str) -> bool:
    code, _, _ = _run_git(["symbolic-ref", "--quiet", "HEAD"], root)
    return code == 0 and _resolve_commit(root, "HEAD") is None


def _untracked_paths(root: str) -> List[bytes]:
    out = _git_ok(["ls-files", "--others", "--exclude-standard", "-z"], root)
    return [p for p in out.split(b"\0") if p]


def _read_untracked(full: str) -> Optional[bytes]:
    """Regular files only; symlinks are never followed."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    st = os.lstat(full)
    if not stat.S_ISREG(st.st_mode):
        return None
    fd = os.open(full, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            return fh.read(MAX_UNTRACKED_FILE_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)


def _names(items: List[str]) -> str:
    shown = ", ".join(_clip(i, 80) for i in items[:5])
    return shown + (f" and {len(items) - 5} more" if len(items) > 5 else "")


def _scan_untracked(root: str, paths: List[bytes], out: _Collector, notes: List[str]) -> int:
    scanned = total_bytes = 0
    skipped: Dict[str, List[str]] = {"not a regular file": [], "larger than 1 MiB": [],
                                     "unreadable": [], "over the scan limit (200 files / 5 MiB)": []}
    for raw in paths:
        name = raw.decode("utf-8", "replace")
        if scanned >= MAX_UNTRACKED_SCANNED or total_bytes >= MAX_UNTRACKED_TOTAL_BYTES:
            skipped["over the scan limit (200 files / 5 MiB)"].append(name)
            continue
        full = os.path.join(root, os.fsdecode(raw))
        try:
            size = os.lstat(full).st_size
            if size > MAX_UNTRACKED_FILE_BYTES:
                skipped["larger than 1 MiB"].append(name)
                continue
            if total_bytes + size > MAX_UNTRACKED_TOTAL_BYTES:
                skipped["over the scan limit (200 files / 5 MiB)"].append(name)
                continue
            data = _read_untracked(full)
        except OSError:
            skipped["unreadable"].append(name)
            continue
        if data is None:
            skipped["not a regular file"].append(name)
            continue
        if len(data) > MAX_UNTRACKED_FILE_BYTES:
            skipped["larger than 1 MiB"].append(name)
            continue
        scanned += 1
        total_bytes += len(data)
        entry: Dict[str, Any] = {"path": name, "added": 0, "removed": 0, "status": "untracked"}
        if b"\0" in data[:8192]:
            entry["binary"] = True
        else:
            lines = data.decode("utf-8", "replace").split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            for number, line in enumerate(lines, start=1):
                out.added_line(name, number, line[:-1] if line.endswith("\r") else line)
            entry["added"] = len(lines)
        out.files.append(entry)
    for reason, names in skipped.items():
        if names:
            notes.append(f"{len(names)} untracked file(s) not scanned ({reason}): {_names(names)}")
    return scanned


# Workspace

def _host_backend(task_id: str) -> Optional[str]:
    try:
        from tools.file_tools_paths import _terminal_env_type_for_task
        backend = _terminal_env_type_for_task(task_id)
    except Exception:
        logger.debug("diff_review: host backend lookup failed, falling back to TERMINAL_ENV", exc_info=True)
        return None
    return backend.strip().lower() if isinstance(backend, str) and backend.strip() else None


def _usable_dir(value: Any) -> Optional[str]:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str) or not value.strip():
        return None
    path = os.path.expanduser(value.strip())
    return path if os.path.isabs(path) and os.path.isdir(path) else None


def _host_workspace_root(task_id: str) -> Optional[str]:
    try:
        from tools.file_tools_paths import _authoritative_workspace_root
        root = _usable_dir(_authoritative_workspace_root(task_id))
    except Exception:
        logger.debug("diff_review: host workspace root lookup failed, falling back", exc_info=True)
        return None
    if root is None:
        logger.debug("diff_review: host reported no usable workspace root, falling back")
    return root


def _agent_cwd() -> Optional[str]:
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        cwd = _usable_dir(resolve_agent_cwd())
    except Exception:
        logger.debug("diff_review: resolve_agent_cwd failed, falling back", exc_info=True)
        return None
    if cwd is None:
        logger.debug("diff_review: resolve_agent_cwd returned no usable directory, falling back")
    return cwd


def _resolve_workspace(task_id: str) -> str:
    backend = _host_backend(task_id) or (os.environ.get("TERMINAL_ENV") or "local").strip().lower()
    if backend != "local":
        raise ReviewError(
            "unsupported_backend",
            f"the workspace lives on the '{backend}' terminal backend, which diff_review cannot reach "
            "from the Hermes host; run `git diff` in the terminal and pass its output as diff_text",
        )
    workspace = _host_workspace_root(task_id) or _agent_cwd() or _usable_dir(os.environ.get("TERMINAL_CWD"))
    if workspace:
        return workspace
    try:
        return os.getcwd()
    except OSError:
        raise ReviewError("not_a_repository", "no workspace directory could be determined") from None


# Modes

def _clean_ref(name: str, value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReviewError("invalid_argument", f"{name} must be a string, not {type(value).__name__}")
    value = value.strip()
    if not value:
        return None
    if len(value) > MAX_REF_LENGTH:
        raise ReviewError("invalid_argument", f"{name} is longer than {MAX_REF_LENGTH} characters")
    if value.startswith("-"):
        raise ReviewError("invalid_argument", f"{name} must be a git revision, not an option: {_shown(value)!r}")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ReviewError("invalid_argument", f"{name} contains control characters")
    return value


def _review_text(diff_text: str, args: dict) -> Dict[str, Any]:
    result = _result(_comparison("diff_text"))
    if args.get("target") is not None or args.get("base") not in (None, "", "HEAD"):
        result["notes"].append("base/target are ignored when diff_text is given")
    if not diff_text.strip():
        return _no_diff(result)
    out = _Collector()
    count = _parse_diff(diff_text, out)
    if not count:
        return _error("invalid_argument", "diff_text does not contain a unified diff "
                      "(no file headers or @@ hunks found)", result["comparison"])
    return _reorder(_fill(result, out, count))


def _review_worktree(root: str, base: Optional[str]) -> Dict[str, Any]:
    label = base or "HEAD"
    result = _result(_comparison("worktree", base=label, repo_root=root))
    sha = _resolve_commit(root, label)
    if sha is None:
        if label == "HEAD" and _head_is_unborn(root):
            sha = _git_ok(["hash-object", "-t", "tree", "--stdin"], root).decode("ascii").strip()
            result["notes"].append("HEAD has no commits yet; compared against an empty tree")
        else:
            _require_commit(root, label)
    out = _Collector()
    count = _parse_diff(_git_ok([*_DIFF_CMD, sha, "--"], root).decode("utf-8", "replace"), out)
    untracked = _untracked_paths(root)
    count += _scan_untracked(root, untracked, out, result["notes"])
    result["untracked_files"] = [p.decode("utf-8", "replace") for p in untracked[:MAX_UNTRACKED_LISTED]]
    if len(untracked) > MAX_UNTRACKED_LISTED:
        result["notes"].append(f"{len(untracked)} untracked files; untracked_files lists the first "
                               f"{MAX_UNTRACKED_LISTED}")
    _fill(result, out, count)
    if not count and not untracked:
        return _no_diff(result)
    return _reorder(result)


def _review_merge_target(root: str, base: Optional[str], target: str) -> Dict[str, Any]:
    label = base or "HEAD"
    comparison = _comparison("merge_target", base=label, target=target, repo_root=root)
    target_sha = _require_commit(root, target)
    if label == "HEAD" and _resolve_commit(root, "HEAD") is None:
        raise ReviewError("unknown_revision", "HEAD has no commits yet, so there is nothing to compare with target")
    base_sha = _require_commit(root, label)
    code, stdout, stderr = _run_git(["merge-base", target_sha, base_sha], root)
    merge_base = stdout.decode("ascii", "replace").strip()
    if code == 1 and not merge_base:
        raise ReviewError("no_merge_base", f"{_shown(label)!r} and {_shown(target)!r} have no common ancestor")
    if code != 0 or not merge_base:
        raise _git_failure(["merge-base"], code, stderr)
    comparison["merge_base"] = merge_base
    result = _result(comparison)
    out = _Collector()
    count = _parse_diff(_git_ok([*_DIFF_CMD, merge_base, base_sha, "--"], root).decode("utf-8", "replace"), out)
    if label == "HEAD":
        dirty, _, _ = _run_git(["diff", "--quiet", "--no-ext-diff", "--no-textconv", "HEAD", "--"], root)
        untracked = len(_untracked_paths(root))
        if dirty == 1 or untracked:
            result["notes"].append(
                "the working tree has uncommitted changes or untracked files; target mode reviews "
                "committed changes only (call without target to review the working tree)")
    _fill(result, out, count)
    return _no_diff(result) if not count else _reorder(result)


def diff_review(args: dict, **kwargs) -> str:
    try:
        if not isinstance(args, dict):
            raise ReviewError("invalid_argument", "arguments must be a JSON object")
        diff_text = args.get("diff_text")
        if diff_text is not None:
            if not isinstance(diff_text, str):
                raise ReviewError("invalid_argument", f"diff_text must be a string, not {type(diff_text).__name__}")
            return _dump(_review_text(diff_text, args))
        base = _clean_ref("base", args.get("base"))
        target = _clean_ref("target", args.get("target"))
        task_id = kwargs.get("task_id")
        workspace = _resolve_workspace(task_id if isinstance(task_id, str) and task_id else "default")
        _check_git_version(workspace)
        root = _repo_root(workspace)
        if target is None:
            return _dump(_review_worktree(root, base))
        return _dump(_review_merge_target(root, base, target))
    except ReviewError as exc:
        return _dump(_error(exc.error_type, exc.message, _comparison(_mode_of(args))))


def _mode_of(args: Any) -> str:
    if not isinstance(args, dict):
        return "worktree"
    if args.get("diff_text") is not None:
        return "diff_text"
    return "merge_target" if args.get("target") else "worktree"
