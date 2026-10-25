"""Running git: comparison modes, ref validation, error reporting and config isolation."""

import os

import pytest

from helpers import PLUGIN_DIR, file_rows, issues, review

V1_KEYS = {"status", "files_changed", "total_added", "total_removed", "files", "warnings", "warning_count"}


def test_default_call_matches_v1_for_tracked_changes(repo):
    repo.write("a.py", "x = 1\n")
    repo.commit()
    repo.write("a.py", "x = 1\nprint('x')\n")
    result = review()
    assert V1_KEYS <= result.keys()
    assert result["status"] == "ok"
    assert result["files_changed"] == 1
    assert (result["total_added"], result["total_removed"]) == (1, 0)
    assert file_rows(result) == [("a.py", 1, 0)]
    assert issues(result) == [("a.py", 2, "debug print statement")]
    assert result["comparison"]["mode"] == "worktree"


def test_clean_tree_keeps_v1_no_diff_message(repo):
    repo.write("a.py", "x = 1\n")
    repo.commit()
    result = review()
    assert result["status"] == "no_diff"
    assert result["message"] == "No differences found."
    assert V1_KEYS <= result.keys()


def test_staged_and_unstaged_changes_are_both_reviewed(repo):
    repo.write("a.py", "x = 1\n")
    repo.write("b.py", "y = 1\n")
    repo.commit()
    repo.write("a.py", "x = 1\n# TODO staged\n")
    repo.git("add", "a.py")
    repo.write("b.py", "y = 1\n# FIXME unstaged\n")
    assert [w[0] for w in issues(review())] == ["a.py", "b.py"]


# Option injection

@pytest.mark.parametrize("field", ["base", "target"])
@pytest.mark.parametrize("value", ["--output={out}", "-p", "--no-index", "-ofoo"])
def test_option_like_refs_are_rejected_and_nothing_is_written(repo, tmp_path, field, value):
    repo.write("a.py", "x = 1\n")
    repo.commit()
    repo.write("a.py", "x = 2\n")
    out = tmp_path / "written-by-git.txt"
    result = review({field: value.format(out=out)})
    assert result["status"] == "error"
    assert result["error_type"] == "invalid_argument"
    assert not out.exists()


def test_option_after_space_is_not_parsed_as_option(repo, tmp_path):
    repo.write("a.py", "x = 1\n")
    repo.commit()
    out = tmp_path / "written-by-git.txt"
    result = review({"base": f"HEAD --output={out}"})
    assert result["error_type"] == "unknown_revision"
    assert not out.exists()


# Target mode is the PR view

def _feature_branch(repo):
    repo.write("app.py", "def f():\n    return 1\n")
    base = repo.commit("base")
    repo.git("update-ref", "refs/remotes/origin/main", "main")
    repo.git("switch", "-q", "-c", "feature")
    repo.write("app.py", "def f():\n    print('dbg')\n    api_key = 'abc123'\n    return 1\n")
    repo.commit("feature")
    return base


@pytest.mark.parametrize("args", [{"target": "origin/main"}, {"base": "HEAD", "target": "origin/main"},
                                  {"base": "feature", "target": "origin/main"}])
def test_target_mode_reviews_branch_additions(repo, args):
    merge_base = _feature_branch(repo)
    result = review(args)
    assert result["status"] == "ok"
    assert file_rows(result) == [("app.py", 2, 0)]
    assert issues(result) == [("app.py", 2, "debug print statement"), ("app.py", 3, "possible hardcoded secret")]
    assert result["comparison"]["mode"] == "merge_target"
    assert result["comparison"]["merge_base"] == merge_base
    assert result["untracked_files"] == []


def test_target_mode_ignores_work_that_landed_on_target_after_branching(repo):
    _feature_branch(repo)
    repo.git("switch", "-q", "main")
    repo.write("upstream.py", "# TODO upstream work\n")
    repo.write("app.py", "def f():\n    return 2\n")
    repo.commit("upstream")
    repo.git("update-ref", "refs/remotes/origin/main", "main")
    repo.git("switch", "-q", "feature")
    result = review({"target": "origin/main"})
    assert [f["path"] for f in result["files"]] == ["app.py"]
    assert [w[2] for w in issues(result)] == ["debug print statement", "possible hardcoded secret"]


def test_target_mode_leaves_out_uncommitted_work_and_says_so(repo):
    _feature_branch(repo)
    repo.write("app.py", "def f():\n    # TODO not committed\n    return 1\n")
    repo.write("new.py", "password = 'hunter2'\n")
    result = review({"target": "origin/main"})
    assert all("TODO" not in w[2] for w in issues(result))
    assert all(f["path"] != "new.py" for f in result["files"])
    assert any("uncommitted" in note for note in result["notes"])


def test_target_mode_without_common_ancestor(repo):
    repo.write("a.py", "x = 1\n")
    repo.commit()
    repo.git("switch", "-q", "--orphan", "other")
    repo.write("b.py", "y = 1\n")
    repo.commit()
    result = review({"target": "main"})
    assert result["error_type"] == "no_merge_base"


def test_target_mode_with_no_new_commits_is_no_diff(repo):
    _feature_branch(repo)
    result = review({"target": "feature"})
    assert result["status"] == "no_diff"


# Failures are errors, not "no differences"

def test_not_a_repository(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    result = review()
    assert result["status"] == "error"
    assert result["error_type"] == "not_a_repository"
    assert result["error"] == result["message"]


def test_unknown_revision(repo):
    repo.write("a.py", "x = 1\n")
    repo.commit()
    result = review({"base": "no-such-ref"})
    assert result["error_type"] == "unknown_revision"
    assert "no-such-ref" in result["message"]


@pytest.mark.parametrize("value", ["", "   ", None])
def test_empty_or_null_base_means_head(repo, value):
    repo.write("a.py", "x = 1\n")
    repo.commit()
    repo.write("a.py", "x = 1\nprint(1)\n")
    result = review({"base": value})
    assert result["status"] == "ok"
    assert result["comparison"]["base"] == "HEAD"


def test_git_missing(repo, tmp_path, monkeypatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    result = review()
    assert result["error_type"] == "git_not_found"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_git_not_executable(repo, tmp_path, monkeypatch):
    fake = tmp_path / "fake-bin"
    fake.mkdir()
    (fake / "git").write_text("#!/bin/sh\n")
    (fake / "git").chmod(0o644)
    monkeypatch.setenv("PATH", str(fake))
    result = review()
    assert result["status"] == "error"
    assert result["error_type"] == "git_not_found"


def test_repository_without_commits_compares_against_empty_tree(repo):
    repo.write("new.py", "password = 'hunter2'\n")
    repo.git("add", "new.py")
    result = review()
    assert result["status"] == "ok"
    assert issues(result) == [("new.py", 1, "possible hardcoded secret")]
    assert any("no commits yet" in note for note in result["notes"])


def test_target_mode_on_repository_without_commits(repo):
    repo.write("new.py", "x = 1\n")
    result = review({"target": "main"})
    assert result["error_type"] == "unknown_revision"


def test_git_too_old_is_reported(repo, tmp_path, monkeypatch):
    fake = tmp_path / "old-git"
    fake.mkdir()
    (fake / "git").write_text("#!/bin/sh\necho 'git version 2.20.1'\n")
    (fake / "git").chmod(0o755)
    monkeypatch.setenv("PATH", str(fake))
    result = review()
    assert result["error_type"] == "git_failed"
    assert "2.28" in result["message"]


# User git config cannot change the parsed output

def _one_print_change(repo):
    repo.write("a.py", "x = 1\n")
    repo.commit()
    repo.write("a.py", "x = 1\nprint('x')\n")


@pytest.mark.parametrize("config", [
    "[color]\n\tui = always\n",
    "[color \"diff\"]\n\tmeta = red\n[color]\n\tdiff = always\n",
    "[diff]\n\tnoprefix = true\n",
    "[diff]\n\tmnemonicPrefix = true\n",
    "[diff]\n\tsrcPrefix = x/\n\tdstPrefix = y/\n",
    "[diff]\n\trenames = false\n\tcontext = 0\n",
])
def test_display_config_is_overridden(repo, isolated_git, config):
    _one_print_change(repo)
    isolated_git.write_text(config)
    result = review()
    assert file_rows(result) == [("a.py", 1, 0)]
    assert issues(result) == [("a.py", 2, "debug print statement")]


def _script(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


@pytest.mark.skipif(os.name != "posix", reason="shell scripts")
def test_external_diff_driver_does_not_run(repo, isolated_git, tmp_path):
    _one_print_change(repo)
    marker = tmp_path / "ext-diff-ran"
    driver = _script(tmp_path / "ext.sh", f"touch '{marker}'\necho replaced\n")
    isolated_git.write_text(f"[diff]\n\texternal = {driver}\n")
    result = review()
    assert issues(result) == [("a.py", 2, "debug print statement")]
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="shell scripts")
def test_external_diff_env_var_is_ignored(repo, tmp_path, monkeypatch):
    _one_print_change(repo)
    marker = tmp_path / "env-ext-ran"
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(_script(tmp_path / "e.sh", f"touch '{marker}'\n")))
    assert issues(review()) == [("a.py", 2, "debug print statement")]
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="shell scripts")
def test_textconv_driver_does_not_run(repo, isolated_git, tmp_path):
    _one_print_change(repo)
    marker = tmp_path / "textconv-ran"
    driver = _script(tmp_path / "tc.sh", f"touch '{marker}'\nsed 's/print/hidden/' \"$1\"\n")
    repo.write(".git/info/attributes", "*.py diff=hide\n")
    isolated_git.write_text(f"[diff \"hide\"]\n\ttextconv = {driver}\n")
    assert issues(review()) == [("a.py", 2, "debug print statement")]
    assert not marker.exists()


def test_diff_relative_config_does_not_hide_changes(repo, isolated_git, monkeypatch):
    _one_print_change(repo)
    repo.write("sub/s.py", "y = 1\n")
    repo.git("add", "sub/s.py")
    isolated_git.write_text("[diff]\n\trelative = true\n")
    monkeypatch.chdir(repo.path / "sub")
    assert [f["path"] for f in review()["files"]] == ["a.py", "sub/s.py"]


def test_submodule_log_config_keeps_submodule_entry(make_repo, repo, isolated_git):
    child = make_repo("child-src")
    child.write("c.txt", "1\n")
    child.commit()
    repo.write("a.py", "x = 1\n")
    repo.commit()
    repo.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(child.path), "child")
    repo.commit("add submodule")
    child.write("c.txt", "2\n")
    child.commit()
    repo.git("-c", "protocol.file.allow=always", "pull", "-q", str(child.path), "main", cwd=repo.path / "child")
    isolated_git.write_text("[diff]\n\tsubmodule = log\n")
    assert ("child", 1, 1) in file_rows(review())


# File paths are not accepted as revisions

@pytest.mark.parametrize("field", ["base", "target"])
def test_file_path_is_not_treated_as_pathspec(repo, field):
    repo.write("staged.py", "x = 1\n")
    repo.commit()
    repo.write("staged.py", "x = 1\npassword = 'hunter2'\n")
    repo.git("add", "staged.py")
    assert issues(review()) == [("staged.py", 2, "possible hardcoded secret")]
    result = review({field: "staged.py"})
    assert result["status"] == "error"
    assert result["error_type"] == "unknown_revision"
    assert "file paths" in result["message"]


# Untracked files

def test_untracked_file_is_reviewed_and_listed(repo):
    repo.write("a.py", "x = 1\n")
    repo.commit()
    repo.write("new_secret.py", "api_key = 'abc123'\n")
    result = review()
    assert result["status"] == "ok"
    assert result["untracked_files"] == ["new_secret.py"]
    assert result["files"] == [{"path": "new_secret.py", "added": 1, "removed": 0, "status": "untracked"}]
    assert issues(result) == [("new_secret.py", 1, "possible hardcoded secret")]


def test_ignored_files_are_not_reviewed(repo):
    repo.write(".gitignore", "*.log\n")
    repo.commit()
    repo.write("debug.log", "password = 'hunter2'\n")
    assert review()["status"] == "no_diff"


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name != "posix", reason="symlinks")
def test_untracked_symlink_is_not_followed(repo, tmp_path):
    repo.write("a.py", "x = 1\n")
    repo.commit()
    outside = tmp_path / "outside.txt"
    outside.write_text("password = 'hunter2'\n")
    os.symlink(outside, repo.path / "link.txt")
    result = review()
    assert result["untracked_files"] == ["link.txt"]
    assert result["warnings"] == []
    assert any("not a regular file" in note and "link.txt" in note for note in result["notes"])


def test_untracked_binary_file(repo):
    repo.write("a.py", "x = 1\n")
    repo.commit()
    repo.write("blob.bin", b"\x00\x01print(1)\n")
    result = review()
    assert result["files"] == [{"path": "blob.bin", "added": 0, "removed": 0, "status": "untracked",
                                "binary": True}]
    assert result["warnings"] == []


def test_untracked_file_over_one_mib_is_skipped(repo):
    repo.write("a.py", "x = 1\n")
    repo.commit()
    repo.write("big.txt", "x" * (1024 * 1024 + 1))
    result = review()
    assert result["status"] == "ok"
    assert result["untracked_files"] == ["big.txt"]
    assert result["files"] == []
    assert any("larger than 1 MiB" in note for note in result["notes"])


def test_untracked_scan_stops_at_200_files(repo):
    repo.write("a.py", "x = 1\n")
    repo.commit()
    for i in range(205):
        repo.write(f"n/f{i:03}.py", "# TODO\n")
    result = review()
    assert result["files_changed"] == 200
    assert result["warning_count"] == 200
    assert len(result["untracked_files"]) == 200
    assert any("5 untracked file(s) not scanned (over the scan limit" in note for note in result["notes"])
    assert any("205 untracked files" in note for note in result["notes"])


def test_untracked_scan_stops_at_5_mib_total(repo):
    repo.write("a.py", "x = 1\n")
    repo.commit()
    for i in range(7):
        repo.write(f"chunk{i}.txt", "y" * 900_000 + "\n")
    result = review()
    scanned = [f["path"] for f in result["files"]]
    assert len(scanned) == 5
    assert sum((repo.path / p).stat().st_size for p in scanned) <= 5 * 1024 * 1024
    assert any("2 untracked file(s) not scanned (over the scan limit" in note for note in result["notes"])


# Inherited repository variables

def test_inherited_git_dir_does_not_redirect_review(make_repo, repo, monkeypatch):
    repo.write("a.py", "x = 1\n")
    repo.commit()
    repo.write("a.py", "x = 1\nprint(1)\n")
    other = make_repo("other")
    other.write("b.txt", "z\n")
    other.commit()
    monkeypatch.setenv("GIT_DIR", str(other.path / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other.path))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other.path / ".git" / "index"))
    assert issues(review()) == [("a.py", 2, "debug print statement")]


# Install instructions

def test_readme_tells_users_to_enable_the_plugin():
    readme = (PLUGIN_DIR / "README.md").read_text()
    assert "hermes plugins enable diff-review" in readme
    assert "git diff origin/main...HEAD" in readme
