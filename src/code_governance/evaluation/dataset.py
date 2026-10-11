"""YAML loading and static-snapshot validation for evaluation datasets."""

from dataclasses import dataclass
from pathlib import Path
from typing import Tuple, Union

import yaml

from code_governance.contract.schema import ExpectedChange, Verification

from .task import EvaluationTask


class DatasetLoadError(ValueError):
    """Raised when an evaluation dataset is malformed or unsafe."""


@dataclass(frozen=True)
class EvaluationDataset:
    """An immutable collection of uniquely identified evaluation tasks."""

    id: str
    tasks: Tuple[EvaluationTask, ...]
    dataset_root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("dataset id must be a nonblank string")
        if not isinstance(self.tasks, tuple):
            object.__setattr__(self, "tasks", tuple(self.tasks))
        root = Path(self.dataset_root).resolve()
        if not root.is_dir():
            raise ValueError("dataset_root must be an existing directory")
        object.__setattr__(self, "dataset_root", root)
        ids = [task.id for task in self.tasks]
        duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate task id: {', '.join(duplicates)}")


def validate_snapshot(task: EvaluationTask, dataset_root: Union[str, Path]) -> Path:
    """Ensure a task snapshot stays under the dataset root and has no Git repo."""
    root = Path(dataset_root).resolve()
    snapshot = (root / Path(task.repository_snapshot)).resolve()
    try:
        snapshot.relative_to(root)
    except ValueError as error:
        raise DatasetLoadError(
            f"{task.id}: repository_snapshot escapes the dataset directory"
        ) from error
    if not snapshot.is_dir():
        raise DatasetLoadError(f"{task.id}: snapshot does not exist: {snapshot}")
    if any(path.name == ".git" for path in snapshot.rglob(".git")):
        raise DatasetLoadError(f"{task.id}: snapshot contains nested .git")
    return snapshot


def load_dataset(path: Union[str, Path]) -> EvaluationDataset:
    """Load a YAML dataset and validate every referenced static snapshot."""
    dataset_path = Path(path).resolve()
    try:
        payload = yaml.safe_load(dataset_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise DatasetLoadError(f"{dataset_path}: cannot read file: {error}") from error
    except yaml.YAMLError as error:
        raise DatasetLoadError(f"{dataset_path}: invalid YAML: {error}") from error
    if not isinstance(payload, dict):
        raise DatasetLoadError(f"{dataset_path}: root mapping is required")
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list):
        raise DatasetLoadError(f"{dataset_path}: tasks must be a list")
    try:
        tasks = tuple(
            EvaluationTask(
                id=item["id"],
                description=item["description"],
                repository_snapshot=item["repository_snapshot"],
                expected_files=tuple(item["expected_files"]),
                changes=tuple(ExpectedChange.model_validate(change) for change in item["changes"]),
                verification=Verification.model_validate(item["verification"]),
                difficulty=item["difficulty"],
            )
            for item in raw_tasks
            if isinstance(item, dict)
        )
        if len(tasks) != len(raw_tasks):
            raise ValueError("each task must be a mapping")
        dataset = EvaluationDataset(
            id=payload.get("id"),
            tasks=tasks,
            dataset_root=dataset_path.parent,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise DatasetLoadError(f"{dataset_path}: {error}") from error
    for task in dataset.tasks:
        validate_snapshot(task, dataset.dataset_root)
    return dataset
