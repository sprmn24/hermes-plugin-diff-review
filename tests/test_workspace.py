"""Workspace resolution against simulated Hermes host modules."""

import logging
import sys
import types
from pathlib import Path

import pytest

from helpers import file_rows, review


def _install(monkeypatch, name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def host_paths(monkeypatch, **attrs):
    return _install(monkeypatch, "tools.file_tools_paths", **attrs)


def agent_cwd(monkeypatch, fn):
    return _install(monkeypatch, "agent.runtime_cwd", resolve_agent_cwd=fn)


@pytest.fixture
def workspace(make_repo):
    r = make_repo("workspace")
    r.write("a.py", "x = 1\n")
    r.commit()
    r.write("a.py", "x = 1\nprint('in workspace')\n")
    return r


@pytest.fixture
def other(make_repo):
    r = make_repo("other")
    r.write("b.py", "y = 1\n")
    r.commit()
    r.write("b.py", "y = 1\n# TODO in other\n")
    return r


@pytest.fixture
def elsewhere(tmp_path, monkeypatch):
    path = tmp_path / "hermes-process-dir"
    path.mkdir()
    monkeypatch.chdir(path)
    return path


def reviewed(result):
    return [row[0] for row in file_rows(result)]


# Workspace resolution

def test_terminal_cwd_is_used_instead_of_process_cwd(workspace, elsewhere, monkeypatch):
    monkeypatch.setenv("TERMINAL_CWD", str(workspace.path))
    result = review(task_id="t1", session_id="s1")
    assert reviewed(result) == ["a.py"]
    assert Path(result["comparison"]["repo_root"]) == workspace.path


def test_relative_terminal_cwd_is_ignored(workspace, elsewhere, monkeypatch):
    monkeypatch.setenv("TERMINAL_CWD", ".")
    assert review()["error_type"] == "not_a_repository"


def test_process_cwd_is_last_resort(workspace, monkeypatch):
    monkeypatch.chdir(workspace.path)
    assert reviewed(review()) == ["a.py"]


def test_host_workspace_root_gets_task_id(workspace, elsewhere, monkeypatch):
    calls = []

    def root(task_id="default"):
        calls.append(task_id)
        return str(workspace.path)
    host_paths(monkeypatch, _authoritative_workspace_root=root, _terminal_env_type_for_task=lambda t="default": "local")
    assert reviewed(review(task_id="t42")) == ["a.py"]
    assert calls == ["t42"]


def test_missing_task_id_uses_default(workspace, elsewhere, monkeypatch):
    calls = []
    host_paths(monkeypatch, _authoritative_workspace_root=lambda t: calls.append(t) or str(workspace.path))
    review()
    assert calls == ["default"]


def test_resolution_order(workspace, other, elsewhere, monkeypatch, make_repo):
    third = make_repo("third")
    monkeypatch.setenv("TERMINAL_CWD", str(third.path))
    agent_cwd(monkeypatch, lambda: other.path)
    host_paths(monkeypatch, _authoritative_workspace_root=lambda t: str(workspace.path))
    assert reviewed(review()) == ["a.py"]
    host_paths(monkeypatch, _authoritative_workspace_root=lambda t: None)
    assert reviewed(review()) == ["b.py"]


def _raise(*_args, **_kwargs):
    raise RuntimeError("host helper exploded")


BROKEN_HELPERS = {
    "missing": None,
    "different signature": lambda: "/",
    "keyword-only signature": lambda *, task: "/",
    "raises": _raise,
    "returns non-string": lambda t: 123,
    "returns relative path": lambda t: "relative/dir",
    "returns missing directory": lambda t: "/no/such/directory/for/diff-review",
}


@pytest.mark.parametrize("kind", BROKEN_HELPERS)
def test_broken_workspace_helper_falls_through(workspace, other, elsewhere, monkeypatch, caplog, kind):
    helper = BROKEN_HELPERS[kind]
    host_paths(monkeypatch, **({} if helper is None else {"_authoritative_workspace_root": helper}))
    agent_cwd(monkeypatch, lambda: other.path)
    caplog.set_level(logging.DEBUG)
    assert reviewed(review(task_id="t1")) == ["b.py"]
    assert any("falling back" in r.getMessage() for r in caplog.records)


BROKEN_AGENT_CWD = {
    "missing": None,
    "different signature": lambda required: "/",
    "keyword-only signature": lambda *, task: "/",
    "raises": _raise,
    "returns non-path": lambda: 123,
    "returns relative path": lambda: Path("relative/dir"),
    "returns missing directory": lambda: Path("/no/such/directory/for/diff-review"),
}


@pytest.mark.parametrize("kind", BROKEN_AGENT_CWD)
def test_broken_agent_cwd_falls_through_to_terminal_cwd(workspace, elsewhere, monkeypatch, caplog, kind):
    helper = BROKEN_AGENT_CWD[kind]
    if helper is None:
        _install(monkeypatch, "agent.runtime_cwd")
    else:
        agent_cwd(monkeypatch, helper)
    monkeypatch.setenv("TERMINAL_CWD", str(workspace.path))
    caplog.set_level(logging.DEBUG)
    assert reviewed(review()) == ["a.py"]
    assert any("falling back" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("kind", ["missing", "different signature", "raises", "returns non-string"])
def test_broken_backend_helper_falls_back_to_terminal_env(workspace, monkeypatch, kind):
    helper = BROKEN_HELPERS[kind]
    host_paths(monkeypatch, **({} if helper is None else {"_terminal_env_type_for_task": helper}))
    monkeypatch.chdir(workspace.path)
    assert reviewed(review()) == ["a.py"]
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    assert review()["error_type"] == "unsupported_backend"


# Container and SSH backends

@pytest.mark.parametrize("backend", ["docker", "ssh", "modal", "daytona", "vercel_sandbox", "some_plugin_backend"])
def test_non_local_backend_is_refused(workspace, monkeypatch, backend):
    host_paths(monkeypatch, _terminal_env_type_for_task=lambda t: backend,
               _authoritative_workspace_root=lambda t: str(workspace.path))
    monkeypatch.chdir(workspace.path)
    result = review(task_id="t1")
    assert result["status"] == "error"
    assert result["error_type"] == "unsupported_backend"
    assert backend in result["message"]
    assert "diff_text" in result["message"]
    assert result["files"] == []


@pytest.mark.parametrize("backend", ["docker", "ssh"])
def test_terminal_env_without_host_is_refused(workspace, monkeypatch, backend):
    monkeypatch.setenv("TERMINAL_ENV", backend)
    monkeypatch.chdir(workspace.path)
    assert review()["error_type"] == "unsupported_backend"


def test_diff_text_works_on_container_backend(monkeypatch):
    host_paths(monkeypatch, _terminal_env_type_for_task=lambda t: "docker")
    text = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+print(1)\n"
    result = review({"diff_text": text})
    assert result["status"] == "ok"
    assert result["warning_count"] == 1
