import subprocess
from pathlib import Path

import pytest

from code_governance.git.context import (
    GitContextError,
    ChangeContext,
    collect_context,
    parse_git_diff,
)


def test_parse_modified_file_and_totals():
    context = parse_git_diff(
        "M\tsrc/app.py\n",
        "3\t1\tsrc/app.py\n",
    )

    assert context.changed_files == ("src/app.py",)
    assert context.files[0].status == "M"
    assert context.files[0].additions == 3
    assert context.files[0].deletions == 1
    assert context.additions == 3
    assert context.deletions == 1


def test_parse_added_file():
    context = parse_git_diff("A\tnew.py\n", "5\t0\tnew.py\n")

    assert context.files[0].status == "A"
    assert context.files[0].path == "new.py"
    assert context.files[0].additions == 5
    assert context.files[0].deletions == 0


def test_parse_deleted_file():
    context = parse_git_diff("D\told.py\n", "0\t7\told.py\n")

    assert context.files[0].status == "D"
    assert context.files[0].path == "old.py"
    assert context.files[0].additions == 0
    assert context.files[0].deletions == 7


def test_parse_binary_file_uses_zero_line_counts():
    context = parse_git_diff("M\timage.png\n", "-\t-\timage.png\n")

    assert context.files[0].additions == 0
    assert context.files[0].deletions == 0


def test_collect_context_from_repository(tmp_path):
    run_git(tmp_path, "init")
    run_git(tmp_path, "config", "user.email", "test@example.com")
    run_git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "modified.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "deleted.txt").write_text("remove me\n", encoding="utf-8")
    run_git(tmp_path, "add", ".")
    run_git(tmp_path, "commit", "-m", "initial")

    (tmp_path / "modified.txt").write_text("new\nline\n", encoding="utf-8")
    (tmp_path / "added.txt").write_text("added\n", encoding="utf-8")
    (tmp_path / "deleted.txt").unlink()

    context = collect_context(tmp_path)
    changes = {change.path: change for change in context.files}

    assert set(changes) == {"modified.txt", "added.txt", "deleted.txt"}
    assert changes["modified.txt"].status == "M"
    assert changes["added.txt"].status == "A"
    assert changes["deleted.txt"].status == "D"
    assert context.additions == 3
    assert context.deletions == 2
    assert "new" in context.diff_text


def test_collect_context_reports_git_failure(tmp_path):
    with pytest.raises(GitContextError, match="unable to collect Git context"):
        collect_context(tmp_path / "missing")


def run_git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
