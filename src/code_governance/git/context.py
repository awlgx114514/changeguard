from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Dict, Iterable, Optional, Tuple, Union


class GitContextError(RuntimeError):
    """Raised when Git facts cannot be collected or parsed."""


@dataclass(frozen=True)
class FileChange:
    path: str
    status: str
    additions: int
    deletions: int


@dataclass(frozen=True)
class ChangeContext:
    files: Tuple[FileChange, ...]
    diff_text: str = ""

    @property
    def changed_files(self) -> Tuple[str, ...]:
        return tuple(change.path for change in self.files)

    @property
    def additions(self) -> int:
        return sum(change.additions for change in self.files)

    @property
    def deletions(self) -> int:
        return sum(change.deletions for change in self.files)


def parse_git_diff(name_status: str, numstat: str, diff_text: str = "") -> ChangeContext:
    statuses = _parse_name_status(name_status.splitlines())
    statistics = _parse_numstat(numstat.splitlines())
    changes = []
    for path, status in statuses:
        additions, deletions = statistics.get(path, (0, 0))
        changes.append(FileChange(path, status, additions, deletions))
    return ChangeContext(tuple(changes), diff_text=diff_text)


def collect_context(repo_path: Union[str, Path], revision: str = "HEAD") -> ChangeContext:
    repository = Path(repo_path)
    _run_git(repository, "rev-parse", "--is-inside-work-tree")
    name_status = _run_git(repository, "diff", "--name-status", revision)
    numstat = _run_git(repository, "diff", "--numstat", revision)
    diff_text = _run_git(
        repository,
        "diff",
        "--no-ext-diff",
        "--no-color",
        revision,
    )
    context = parse_git_diff(name_status, numstat, diff_text)
    untracked = _run_git(repository, "ls-files", "--others", "--exclude-standard")
    if not untracked.strip():
        return context

    tracked_paths = set(context.changed_files)
    extra_changes = []
    for relative_path in untracked.splitlines():
        if not relative_path or relative_path in tracked_paths:
            continue
        extra_changes.append(
            FileChange(
                path=relative_path,
                status="A",
                additions=_count_worktree_lines(repository / relative_path),
                deletions=0,
            )
        )
    return ChangeContext(context.files + tuple(extra_changes), diff_text=context.diff_text)


def _parse_name_status(lines: Iterable[str]) -> Tuple[Tuple[str, str], ...]:
    result = []
    for line in lines:
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 2:
            raise GitContextError(f"invalid name-status line: {line}")
        status = fields[0]
        path = fields[-1] if status[:1] in {"R", "C"} else fields[1]
        result.append((path, status))
    return tuple(result)


def _parse_numstat(lines: Iterable[str]) -> Dict[str, Tuple[int, int]]:
    result: Dict[str, Tuple[int, int]] = {}
    for line in lines:
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 3:
            raise GitContextError(f"invalid numstat line: {line}")
        additions, deletions, path = fields[0], fields[1], fields[-1]
        result[path] = (_parse_count(additions), _parse_count(deletions))
    return result


def _parse_count(value: str) -> int:
    if value == "-":
        return 0
    try:
        return int(value)
    except ValueError as error:
        raise GitContextError(f"invalid line count: {value}") from error


def _count_worktree_lines(path: Path) -> int:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise GitContextError(f"unable to read changed file: {path}") from error
    if b"\x00" in content:
        return 0
    return len(content.splitlines())


def _run_git(repository: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        if "not a git repository" in detail.lower():
            raise GitContextError(f"not a Git repository: {repository}") from error
        raise GitContextError(f"unable to collect Git context: {detail.strip()}") from error
    return result.stdout
