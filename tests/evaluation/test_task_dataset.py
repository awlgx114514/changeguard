from pathlib import Path

import pytest
import yaml

from code_governance.contract.schema import ExpectedChange, Verification
from code_governance.evaluation.dataset import DatasetLoadError, load_dataset, validate_snapshot
from code_governance.evaluation.task import EvaluationTask
from code_governance.memory.manager import MemoryManager
from code_governance.memory.retriever import MemoryRetriever, TaskContext


def task_mapping(**overrides):
    task = {
        "id": "simple-change",
        "description": "Return a greeting.",
        "repository_snapshot": "snapshots/simple-change",
        "expected_files": ["app.py"],
        "changes": [
            {
                "id": "greeting",
                "description": "Greeting is returned.",
                "requirement": "required",
            }
        ],
        "verification": {
            "claims": [
                {
                    "id": "unit_regression",
                    "description": "The unit regression suite passes.",
                    "request": {"argv": ["python", "-m", "unittest", "-q"]},
                    "expected_result": {"exit_code": 0},
                }
            ],
            "coverage": [
                {
                    "claim_id": "unit_regression",
                    "change_id": "greeting",
                    "coverage_type": "direct",
                }
            ],
        },
        "difficulty": "low",
    }
    task.update(overrides)
    return task


def write_dataset(tmp_path: Path, tasks, snapshots=()):
    for snapshot in snapshots:
        (tmp_path / snapshot).mkdir(parents=True)
    path = tmp_path / "tasks.yaml"
    path.write_text(
        yaml.safe_dump({"id": "evaluation-v2", "tasks": tasks}, sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_load_dataset_maps_task_to_contract(tmp_path):
    path = write_dataset(tmp_path, [task_mapping()], snapshots=("snapshots/simple-change",))

    dataset = load_dataset(path)
    task = dataset.tasks[0]
    contract = task.to_contract()

    assert dataset.id == "evaluation-v2"
    assert dataset.dataset_root == tmp_path.resolve()
    assert contract.expected.files == ["app.py"]
    assert contract.expected.change_level == "L1"
    assert contract.constraints.forbidden == []
    assert contract.verification.claims[0].request.argv == ("python", "-m", "unittest", "-q")


def test_load_dataset_rejects_duplicate_task_ids(tmp_path):
    path = write_dataset(
        tmp_path,
        [task_mapping(), task_mapping()],
        snapshots=("snapshots/simple-change",),
    )

    with pytest.raises(DatasetLoadError, match="duplicate task id"):
        load_dataset(path)


def test_load_dataset_rejects_empty_verification_claims(tmp_path):
    path = write_dataset(
        tmp_path,
        [task_mapping(verification={"claims": [], "coverage": []})],
        snapshots=("snapshots/simple-change",),
    )

    with pytest.raises(DatasetLoadError, match="verification.claims"):
        load_dataset(path)


def test_load_dataset_rejects_snapshot_escaping_dataset_directory(tmp_path):
    path = write_dataset(
        tmp_path,
        [task_mapping(repository_snapshot="../outside")],
    )

    with pytest.raises(DatasetLoadError, match="repository_snapshot"):
        load_dataset(path)


def test_load_dataset_rejects_missing_snapshot(tmp_path):
    path = write_dataset(tmp_path, [task_mapping()])

    with pytest.raises(DatasetLoadError, match="snapshot does not exist"):
        load_dataset(path)


def test_validate_snapshot_rejects_nested_git_directory(tmp_path):
    snapshot = tmp_path / "snapshots" / "simple-change"
    (snapshot / "nested" / ".git").mkdir(parents=True)
    task = EvaluationTask(
        id="simple-change",
        description="Return a greeting.",
        repository_snapshot="snapshots/simple-change",
        expected_files=("app.py",),
        changes=(ExpectedChange.model_validate(task_mapping()["changes"][0]),),
        verification=Verification.model_validate(task_mapping()["verification"]),
        difficulty="low",
    )

    with pytest.raises(DatasetLoadError, match="nested .git"):
        validate_snapshot(task, tmp_path)


def test_memory_relevant_fixture_loads_and_retrieves_its_port_rule():
    dataset_root = Path(__file__).parents[2] / "benchmarks" / "v1"
    task = next(task for task in load_dataset(dataset_root / "tasks.yaml").tasks if task.id == "memory-relevant")
    manager = MemoryManager(dataset_root / task.repository_snapshot)

    rules = manager.active_rules()
    selected = MemoryRetriever(manager).retrieve(
        TaskContext(
            task_text=task.description,
            task_type="general",
            target_files=task.expected_files,
            modules=("ports",),
            keywords=("port", "parse_port", "valid"),
        )
    )

    assert [rule.id for rule in rules] == ["valid-port-range"]
    assert [rule.id for rule in selected] == ["valid-port-range"]
