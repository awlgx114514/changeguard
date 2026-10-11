from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Dict, Tuple


class RepoAnalysisError(RuntimeError):
    """Raised when basic repository facts cannot be collected."""


@dataclass(frozen=True)
class RepoContext:
    root: Path
    files: Tuple[str, ...]
    python_modules: Tuple[str, ...]
    git_status: Tuple[str, ...]
    core_files: Tuple[str, ...]
    module_relationships: Dict[str, Tuple[str, ...]]

    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "files": list(self.files),
            "python_modules": list(self.python_modules),
            "git_status": list(self.git_status),
            "core_files": list(self.core_files),
            "module_relationships": {
                path: list(imports)
                for path, imports in self.module_relationships.items()
            },
        }


class RepositoryAnalyzer:
    """Builds a lightweight, file-based view of a repository."""

    _IGNORED_DIRECTORIES = {
        ".agent",
        ".agent-memory",
        ".git",
        ".venv",
        "__pycache__",
    }
    _CORE_NAMES = {"api", "app", "cli", "core", "main"}
    _IMPORT_PATTERN = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.MULTILINE)

    def analyze(self, repo_path: Path) -> RepoContext:
        root = Path(repo_path).resolve()
        if not root.is_dir():
            raise RepoAnalysisError(f"repository path does not exist: {root}")

        files = tuple(self._files(root))
        modules = tuple(
            self._module_name(path)
            for path in files
            if path.endswith(".py")
        )
        relationships = {
            path: self._imports(root / path)
            for path in files
            if path.endswith(".py")
        }
        context = RepoContext(
            root=root,
            files=files,
            python_modules=modules,
            git_status=self._git_status(root),
            core_files=tuple(
                path
                for path in files
                if Path(path).stem in self._CORE_NAMES
            ),
            module_relationships=relationships,
        )
        self._write_map(context)
        return context

    def _files(self, root: Path):
        result = []
        for path in root.rglob("*"):
            if not path.is_file() or any(part in self._IGNORED_DIRECTORIES for part in path.parts):
                continue
            result.append(path.relative_to(root).as_posix())
        return sorted(result)

    def _module_name(self, relative_path: str) -> str:
        path = Path(relative_path)
        parts = list(path.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    def _imports(self, path: Path) -> Tuple[str, ...]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return ()
        return tuple(match.group(1) for match in self._IMPORT_PATTERN.finditer(text))

    def _git_status(self, root: Path) -> Tuple[str, ...]:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "status", "--short"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise RepoAnalysisError(f"unable to read git status: {error}") from error
        return tuple(line for line in result.stdout.splitlines() if line)

    def _write_map(self, context: RepoContext) -> None:
        directory = context.root / ".agent"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "repo-map.json").write_text(
            json.dumps(context.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
