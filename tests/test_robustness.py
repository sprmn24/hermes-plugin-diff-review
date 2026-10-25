"""Bad arguments, encodings, git failures, timeouts and output size limits."""

import json
import logging
import os
import time

import pytest

from helpers import added_lines_diff, diff_text_review, issues, review, tools

FAKE_TOKEN = "ghp_FAKEFAKEFAKEFAKEFAKEFAKE00000000"


@pytest.mark.parametrize("args, field", [
    ({"base": 123}, "base"),
    ({"target": ["HEAD"]}, "target"),
    ({"base": {"ref": "HEAD"}}, "base"),
    ({"diff_text": 123}, "diff_text"),
    ({"base": "HE\x00AD"}, "base"),
    ({"target": "main\nHEAD"}, "target"),
    ({"base": "x" * 257}, "base"),
    ({"base": "x" * 200_000}, "base"),
])
def test_bad_arguments_return_errors(repo, args, field):
    result = review(args)
    assert result["status"] == "error"
    assert result["error_type"] == "invalid_argument"
    assert field in result["message"]


def test_non_dict_arguments():
    result = json.loads(tools.diff_review(["HEAD"]))
    assert result["error_type"] == "invalid_argument"


def test_extra_host_kwargs_are_accepted(repo):
    repo.write("a.py", "x = 1\n")
    repo.commit()
    result = review(task_id="t1", session_id="s1", user_task="review")
    assert result["status"] == "no_diff"


def test_non_utf8_file_content(repo):
    repo.write("l.py", "x = 1\n")
    repo.commit()
    repo.write("l.py", b"x = 1\ns = '\xe9'\nprint(1)\n")
    result = review()
    assert result["status"] == "ok"
    assert issues(result) == [("l.py", 3, "debug print statement")]
    json.dumps(result, ensure_ascii=False).encode("utf-8")


def test_non_utf8_untracked_file_name(repo):
    if os.name != "posix":
        pytest.skip("bytes file names")
    repo.write("a.py", "x = 1\n")
    repo.commit()
    with open(os.path.join(os.fsencode(repo.path), b"bad-\xff.py"), "w") as fh:
        fh.write("# TODO\n")
    result = review()
    assert result["files_changed"] == 1
    assert issues(result)[0][2] == "unresolved TODO/FIXME"
    tools.diff_review({}).encode("utf-8")


def test_secret_like_ref_is_redacted_in_result_and_logs(repo, caplog):
    repo.write("a.py", "x = 1\n")
    repo.commit()
    caplog.set_level(logging.DEBUG)
    raw = tools.diff_review({"base": FAKE_TOKEN})
    assert json.loads(raw)["error_type"] == "unknown_revision"
    assert FAKE_TOKEN not in raw
    assert all(FAKE_TOKEN not in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("text, hidden", [
    ("fatal: could not read from https://user:hunter2@example.invalid/r.git", "hunter2"),
    ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
    (f"token {FAKE_TOKEN}", FAKE_TOKEN),
    ("key sk-FAKEFAKEFAKEFAKE1234", "sk-FAKEFAKEFAKEFAKE1234"),
])
def test_redaction(text, hidden):
    assert hidden not in tools._redact(text)


def _fake_git(tmp_path, body):
    bindir = tmp_path / "fake-git-bin"
    bindir.mkdir()
    script = bindir / "git"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return bindir


@pytest.mark.skipif(os.name != "posix", reason="shell scripts")
def test_git_stderr_is_bounded_and_redacted(repo, tmp_path, monkeypatch, caplog):
    bindir = _fake_git(tmp_path, (
        'if [ "$1" = "--version" ]; then echo "git version 2.43.0"; exit 0; fi\n'
        "head -c 2000000 /dev/zero | tr '\\0' 'E' >&2\n"
        f"echo 'fatal: https://user:hunter2@example.invalid {FAKE_TOKEN}' >&2\n"
        "exit 128\n"))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    caplog.set_level(logging.DEBUG)
    raw = tools.diff_review({})
    result = json.loads(raw)
    assert result["error_type"] == "git_failed"
    assert len(result["message"]) <= tools.MAX_MESSAGE_CHARS
    assert "hunter2" not in raw and FAKE_TOKEN not in raw
    logged = "".join(record.getMessage() for record in caplog.records)
    assert len(logged) < 10_000
    assert "hunter2" not in logged and FAKE_TOKEN not in logged


@pytest.mark.skipif(os.name != "posix", reason="process groups")
def test_timeout_kills_git_and_its_children(repo, tmp_path, monkeypatch):
    pid_file = tmp_path / "child.pid"
    bindir = _fake_git(tmp_path, (
        'if [ "$1" = "--version" ]; then echo "git version 2.43.0"; exit 0; fi\n'
        f"sleep 60 &\necho $! > '{pid_file}'\nwait\n"))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(tools, "GIT_TIMEOUT", 1)
    started = time.monotonic()
    result = review()
    assert result["error_type"] == "timeout"
    assert time.monotonic() - started < 15
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("child of git kept running after the timeout")


# Output limits

def test_warnings_are_capped_and_result_fits_host_budget():
    raw = tools.diff_review({"diff_text": added_lines_diff("f.py", ["print(1)"] * 20_000)})
    result = json.loads(raw)
    assert result["warning_count"] == 20_000
    assert len(result["warnings"]) == tools.MAX_WARNINGS
    assert result["warnings_truncated"] is True
    assert result["total_added"] == 20_000
    assert len(raw) < 100_000


def test_files_are_capped_with_true_totals(repo):
    for i in range(600):
        repo.write(f"pkg/m{i:03}.py", "x = 1\n")
    repo.commit()
    for i in range(600):
        repo.write(f"pkg/m{i:03}.py", "x = 1\n# TODO\n")
    raw = tools.diff_review({})
    result = json.loads(raw)
    assert result["files_changed"] == 600
    assert result["total_added"] == 600
    assert len(result["files"]) == tools.MAX_FILES
    assert result["files_truncated"] is True
    assert result["warning_count"] == 600
    assert len(raw) < 100_000


def test_long_paths_still_fit_host_budget():
    count = 700
    text = "".join(added_lines_diff("d/" + "p" * 240 + f"{i}.py", ["# TODO"]) for i in range(count))
    raw = tools.diff_review({"diff_text": text})
    result = json.loads(raw)
    assert result["files_changed"] == count
    assert result["files_truncated"] is True
    assert result["warnings_truncated"] is True
    assert len(raw) < 100_000


def test_small_results_are_not_truncated():
    result = diff_text_review(added_lines_diff("f.py", ["print(1)"] * 3))
    assert result["files_truncated"] is False
    assert result["warnings_truncated"] is False
