"""Diff parsing: paths, line counting, line numbers and diff formats."""

import os
import subprocess

import pytest

from helpers import diff_text_review, file_rows, issues, review


def _rows_by_path(result):
    return {f["path"]: f for f in result["files"]}


# Parsing real git output

@pytest.fixture
def mixed_repo(repo):
    repo.write("gone.py", "print(1)\n")
    repo.write("img.bin", b"\x00\x01\x02")
    repo.write("old_name.py", "a = 1\nb = 2\nc = 3\n")
    repo.write("my file.py", "x = 1\n")
    repo.write("ä.py", "x = 1\n")
    repo.write('we"ird.py', "x = 1\n")
    repo.write("run.sh", "echo\n")
    repo.write("q.sql", "-- header\nselect 1;\n")
    repo.write("inc.c", "int x;\n")
    repo.write("nonl.py", "a = 1\nb = 2")
    repo.write("ls.py", "x = 1\n")
    repo.commit()
    (repo.path / "gone.py").unlink()
    repo.write("img.bin", b"\x00\x03\x04")
    repo.git("mv", "old_name.py", "new_name.py")
    repo.write("my file.py", "x = 1\nprint('space')\n")
    repo.write("ä.py", "x = 1\nprint('umlaut')\n")
    repo.write('we"ird.py', "x = 1\nprint('quoted')\n")
    if os.name == "posix":
        (repo.path / "run.sh").chmod(0o755)
    repo.write("q.sql", "select 1;\n")
    repo.write("inc.c", "int x;\n++x;\n")
    repo.write("nonl.py", "a = 1\nb = 2\nprint(3)\n")
    repo.write("ls.py", "s = 'a b'\nprint(2)\n")
    return repo


def test_deleted_file_path(mixed_repo):
    entry = _rows_by_path(review())["gone.py"]
    assert (entry["added"], entry["removed"], entry["status"]) == (0, 1, "deleted")


def test_binary_file_path(mixed_repo):
    entry = _rows_by_path(review())["img.bin"]
    assert entry["status"] == "binary" and entry["binary"] is True


def test_rename_only_path(mixed_repo):
    entry = _rows_by_path(review())["new_name.py"]
    assert entry["status"] == "renamed"
    assert entry["old_path"] == "old_name.py"
    assert (entry["added"], entry["removed"]) == (0, 0)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_mode_only_change_path(mixed_repo):
    assert _rows_by_path(review())["run.sh"]["status"] == "mode_changed"


def test_space_non_ascii_and_quoted_paths(mixed_repo):
    result = review()
    paths = _rows_by_path(result)
    for name in ("my file.py", "ä.py", 'we"ird.py'):
        assert paths[name]["added"] == 1
        assert (name, 2, "debug print statement") in issues(result)
    assert "" not in paths


def test_content_lines_starting_with_double_signs_are_counted(mixed_repo):
    paths = _rows_by_path(review())
    assert (paths["inc.c"]["added"], paths["inc.c"]["removed"]) == (1, 0)
    assert (paths["q.sql"]["added"], paths["q.sql"]["removed"]) == (0, 1)


def test_no_newline_marker_does_not_shift_line_numbers(mixed_repo):
    assert ("nonl.py", 3, "debug print statement") in issues(review())


def test_unicode_line_separators_do_not_split_lines(mixed_repo):
    assert ("ls.py", 2, "debug print statement") in issues(review())


def test_every_file_has_a_path(mixed_repo):
    result = review()
    assert result["files_changed"] == 11
    assert all(f["path"] and f["path"] != "unknown" for f in result["files"])


# diff_text formats

def test_v1_style_diff_text_is_unchanged():
    text = (
        "diff --git a/app.py b/app.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,3 +1,5 @@\n"
        " def f():\n"
        "-    return 0\n"
        "+    print('debug')\n"
        "+    password = 'hunter2'\n"
        "+    return 1\n"
        " # end\n"
    )
    result = diff_text_review(text)
    assert result["status"] == "ok"
    assert file_rows(result) == [("app.py", 3, 1)]
    assert issues(result) == [("app.py", 2, "debug print statement"), ("app.py", 3, "possible hardcoded secret")]
    assert result["comparison"]["mode"] == "diff_text"


def test_content_line_cannot_hijack_the_file_path():
    text = (
        "diff --git a/real.py b/real.py\n--- a/real.py\n+++ b/real.py\n@@ -1 +1,3 @@\n x = 1\n"
        "+++ b/elsewhere.py\n"
        "+password = 'hunter2'\n"
    )
    result = diff_text_review(text)
    assert file_rows(result) == [("real.py", 2, 0)]
    assert issues(result) == [("real.py", 3, "possible hardcoded secret")]


def test_removed_line_starting_with_dashes_is_counted():
    text = "diff --git a/q.sql b/q.sql\n--- a/q.sql\n+++ b/q.sql\n@@ -1,2 +1 @@\n--- old comment\n select 1;\n"
    assert file_rows(diff_text_review(text)) == [("q.sql", 0, 1)]


def test_plain_unified_diff():
    text = "--- a.py\t2026-01-01 00:00:00\n+++ b.py\t2026-01-01 00:00:00\n@@ -1 +1,2 @@\n x = 1\n+print(1)\n"
    result = diff_text_review(text)
    assert file_rows(result) == [("b.py", 1, 0)]
    assert issues(result) == [("b.py", 2, "debug print statement")]


def test_recursive_plain_diff_with_two_files():
    text = (
        "diff -ru a/one.py b/one.py\n--- a/one.py\n+++ b/one.py\n@@ -1 +1,2 @@\n x\n+# TODO one\n"
        "diff -ru a/two.py b/two.py\n--- a/two.py\n+++ b/two.py\n@@ -1 +1,2 @@\n y\n+# TODO two\n"
    )
    result = diff_text_review(text)
    assert file_rows(result) == [("one.py", 1, 0), ("two.py", 1, 0)]


def test_combined_merge_diff():
    text = (
        "diff --cc f.py\nindex 1111111,2222222..3333333\n--- a/f.py\n+++ b/f.py\n"
        "@@@ -1,2 -1,3 +1,3 @@@\n"
        "  x = 1\n"
        "- left = 1\n"
        " -right = 1\n"
        "++print('merge')\n"
        "+ only_left = 2\n"
    )
    result = diff_text_review(text)
    assert file_rows(result) == [("f.py", 2, 2)]
    assert issues(result) == [("f.py", 2, "debug print statement")]


def test_crlf_diff_text():
    text = "diff --git a/w.py b/w.py\r\n--- a/w.py\r\n+++ b/w.py\r\n@@ -0,0 +1 @@\r\n+print(1)\r\n"
    assert issues(diff_text_review(text)) == [("w.py", 1, "debug print statement")]


def test_quoted_header_paths_in_diff_text():
    text = (
        'diff --git "a/\\303\\244 x.py" "b/\\303\\244 x.py"\n'
        'new file mode 100644\n--- /dev/null\n+++ "b/\\303\\244 x.py"\n@@ -0,0 +1 @@\n+print(1)\n'
    )
    result = diff_text_review(text)
    assert result["files"][0]["path"] == "ä x.py"
    assert result["files"][0]["status"] == "added"


def test_header_only_fallback_for_binary_with_spaces():
    text = "diff --git a/my img.png b/my img.png\nindex 1..2 100644\nBinary files a/my img.png and b/my img.png differ\n"
    result = diff_text_review(text)
    assert result["files"][0]["path"] == "my img.png"
    assert result["files"][0]["status"] == "binary"


def test_hunk_without_file_header():
    result = diff_text_review("@@ -1 +1,2 @@\n x\n+print(1)\n")
    assert file_rows(result) == [("unknown", 1, 0)]


def test_empty_diff_text_is_no_diff():
    for text in ("", "   \n"):
        result = diff_text_review(text)
        assert result["status"] == "no_diff"
        assert result["message"] == "No differences found."


def test_text_without_a_diff_is_an_error():
    result = diff_text_review("hello, this is not a diff\n")
    assert result["error_type"] == "invalid_argument"


def test_diff_text_ignores_base_and_target_with_a_note():
    text = "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -0,0 +1 @@\n+x\n"
    result = review({"diff_text": text, "base": "--output=/tmp/x", "target": "main"})
    assert result["status"] == "ok"
    assert "base/target are ignored when diff_text is given" in result["notes"]
    plain = review({"diff_text": text, "base": "HEAD"})
    assert plain["notes"] == []


def test_real_combined_diff_from_git_show(repo):
    repo.write("f.py", "x = 1\n")
    repo.commit("base")
    repo.git("switch", "-q", "-c", "left")
    repo.write("f.py", "x = 1\nleft = 1\n")
    repo.commit("left")
    repo.git("switch", "-q", "main")
    repo.write("f.py", "x = 1\nright = 1\n")
    repo.commit("right")
    merge = subprocess.run(["git", "merge", "-q", "left"], cwd=repo.path, capture_output=True, text=True)
    assert merge.returncode != 0  # conflict, resolved by hand below
    repo.write("f.py", "x = 1\nleft = 1\nprint('resolved')\n")
    repo.commit("merge")
    text = repo.git("-c", "core.quotePath=false", "show", "--format=", "--cc", "--no-color", "HEAD")
    assert text.startswith("diff --cc f.py")
    result = diff_text_review(text)
    assert [f["path"] for f in result["files"]] == ["f.py"]
    assert ("f.py", 3, "debug print statement") in issues(result)
