from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import tempfile


class WorkspaceError(RuntimeError):
    """Raised when an isolated workspace cannot be created or inspected."""


@dataclass(frozen=True)
class Workspace:
    source_repo: Path
    path: Path


class WorkspaceManager:
    """Copies a Git repository to a temporary workspace for agent execution."""

    _IGNORED = shutil.ignore_patterns(
        ".agent",
        ".agent-memory",
        ".venv",
        "__pycache__",
    )

    def create(self, repo_path: Path) -> Workspace:
        source = Path(repo_path).resolve()
        if not (source / ".git").exists():
            raise WorkspaceError(f"not a Git repository: {source}")
        parent = Path(tempfile.mkdtemp(prefix="changeguard-"))
        workspace = parent / "repo"
        try:
            shutil.copytree(
                source,
                workspace,
                ignore=WorkspaceManager._IGNORED,
            )
        except OSError as error:
            shutil.rmtree(parent, ignore_errors=True)
            raise WorkspaceError(f"unable to create workspace: {error}") from error
        return Workspace(source_repo=source, path=workspace)

    def restore(self, repo_path: Path, snapshot_path: Path) -> Workspace:
        """Create a fresh isolated workspace from an already frozen tree snapshot."""
        source = Path(repo_path).resolve()
        snapshot = Path(snapshot_path).resolve()
        if not (source / ".git").exists():
            raise WorkspaceError(f"not a Git repository: {source}")
        if not snapshot.is_dir():
            raise WorkspaceError(f"workspace snapshot does not exist: {snapshot}")
        parent = Path(tempfile.mkdtemp(prefix="changeguard-"))
        workspace = parent / "repo"
        try:
            shutil.copytree(snapshot, workspace)
        except OSError as error:
            shutil.rmtree(parent, ignore_errors=True)
            raise WorkspaceError(f"unable to restore workspace snapshot: {error}") from error
        return Workspace(source_repo=source, path=workspace)

    def collect_patch(self, workspace: Workspace) -> str:
        self._run_git(workspace.path, "add", "-N", ".")
        return self._run_git(workspace.path, "diff", "--binary", "HEAD")

    def save_patch(self, workspace: Workspace) -> Path:
        directory = workspace.source_repo / ".agent"
        directory.mkdir(parents=True, exist_ok=True)
        patch_path = directory / "changes.patch"
        patch_path.write_text(self.collect_patch(workspace), encoding="utf-8")
        return patch_path

    def cleanup(self, workspace: Workspace) -> None:
        shutil.rmtree(workspace.path.parent, ignore_errors=True)

    def _run_git(self, repository: Path, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(repository), *args],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            detail = getattr(error, "stderr", "") or str(error)
            raise WorkspaceError(f"unable to generate workspace patch: {detail.strip()}") from error
        return result.stdout
