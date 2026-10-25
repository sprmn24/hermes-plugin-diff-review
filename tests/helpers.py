"""Test helpers: load the plugin as a package and drive real git repositories."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE = "diff_review_plugin"


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()
tools = plugin.tools

_HOST_MODULES = ("tools.file_tools_paths", "agent.runtime_cwd")


class Repo:
    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args, cwd=None) -> str:
        return subprocess.run(["git", *args], cwd=cwd or self.path, check=True, capture_output=True,
                              text=True).stdout

    def write(self, rel: str, content) -> Path:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)
        return target

    def commit(self, message: str = "commit") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()


def review(args=None, **kwargs) -> dict:
    return json.loads(tools.diff_review({} if args is None else args, **kwargs))


def diff_text_review(text: str) -> dict:
    return review({"diff_text": text})


def added_lines_diff(path: str, lines) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"


def issues(result: dict):
    return [(w["file"], w["line"], w["issue"]) for w in result["warnings"]]


def file_rows(result: dict):
    return [(f["path"], f["added"], f["removed"]) for f in result["files"]]
