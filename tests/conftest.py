"""Shared fixtures: an isolated git environment and throwaway repositories."""

import sys

import pytest

from helpers import _HOST_MODULES, Repo

_INHERITED_GIT_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_PREFIX", "GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS", "GIT_CONFIG_PARAMETERS",
)


@pytest.fixture(autouse=True)
def isolated_git(tmp_path, monkeypatch):
    """Empty global git config, fixed identity, and no Hermes host modules or workspace hints."""
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "Test")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "test@example.invalid")
    for var in ("TERMINAL_CWD", "TERMINAL_ENV", *_INHERITED_GIT_VARS):
        monkeypatch.delenv(var, raising=False)
    for name in _HOST_MODULES:
        monkeypatch.setitem(sys.modules, name, None)  # import raises ImportError
    return config


@pytest.fixture
def make_repo(tmp_path):
    def make(name: str = "repo") -> Repo:
        return Repo(tmp_path / name)
    return make


@pytest.fixture
def repo(make_repo, monkeypatch):
    r = make_repo()
    monkeypatch.chdir(r.path)
    return r
