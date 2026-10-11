"""Immutable task definitions for the fixed evaluation benchmark."""

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Tuple

from code_governance.contract.schema import (
    ChangeContract,
    ExpectedChange,
    Metadata,
    Scope,
    Verification,
)


_DIFFICULTY_LEVELS = {"low": "L1", "medium": "L2", "high": "L3"}


def _require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonblank string")


def _validate_relative_posix_path(value: str, field: str) -> None:
    _require_text(value, field)
    if "\\" in value:
        raise ValueError(f"{field} must be a relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        raise ValueError(f"{field} must be a relative POSIX path")


@dataclass(frozen=True)
class EvaluationTask:
    """A single immutable benchmark task and its expected Change Contract."""

    id: str
    description: str
    repository_snapshot: str
    expected_files: Tuple[str, ...]
    changes: Tuple[ExpectedChange, ...]
    verification: Verification
    difficulty: str

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        _require_text(self.description, "description")
        _validate_relative_posix_path(self.repository_snapshot, "repository_snapshot")
        if self.difficulty not in _DIFFICULTY_LEVELS:
            raise ValueError("difficulty must be one of low, medium, high")
        if not isinstance(self.expected_files, tuple):
            object.__setattr__(self, "expected_files", tuple(self.expected_files))
        for path in self.expected_files:
            _validate_relative_posix_path(path, "expected_files")
        if not isinstance(self.changes, tuple):
            object.__setattr__(self, "changes", tuple(self.changes))
        if not self.changes:
            raise ValueError("changes must not be empty")
        if not all(isinstance(change, ExpectedChange) for change in self.changes):
            raise TypeError("changes must contain ExpectedChange instances")
        if not isinstance(self.verification, Verification):
            raise TypeError("verification must be a Verification instance")
        if not self.verification.claims:
            raise ValueError("verification.claims must not be empty")

    def to_contract(self) -> ChangeContract:
        """Return the deterministic Change Contract for this task."""
        return ChangeContract(
            metadata=Metadata(id=self.id, project="changeguard-evaluation-v3"),
            goal=self.description,
            scope=Scope(
                paths=list(self.expected_files),
                change_level=_DIFFICULTY_LEVELS[self.difficulty],
            ),
            changes=list(self.changes),
            verification=self.verification,
        )
