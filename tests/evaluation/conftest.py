import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from code_governance.agent.harness import HarnessOrchestrator
from code_governance.contract.schema import (
    ClaimCoverage,
    ExpectedChange,
    Verification,
    VerificationClaim,
)
from code_governance.evaluation.dataset import EvaluationDataset
from code_governance.evaluation.task import EvaluationTask
from code_governance.git.context import ChangeContext, FileChange
from code_governance.runtime.action import Action, OperationKind
from code_governance.validator.core import ValidationResult
from code_governance.validator.evidence import ValidationEvidence, WorkspaceIdentity


class FakeRuntimeAgent:
    """Deterministic Runtime fake with a standard-agent repair seam."""

    def __init__(self, initial_content="generated\n"):
        self._actions = [
            Action.create_operation(OperationKind.FILE_CREATE, path="result.txt", content=initial_content, reason="satisfy the fixture contract"),
            Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="fixture complete"),
        ]

    def decide(self, state, context):
        del state, context
        return self._actions.pop(0)



class _InspectingHarness:
    """Observe a disposable repository, then execute the real Harness."""

    def __init__(self, owner, kwargs):
        self._owner = owner
        self._kwargs = kwargs

    def run(self, task_file, repo_path):
        task_file = Path(task_file).resolve()
        repo_path = Path(repo_path).resolve()
        record = {
            "kwargs": self._kwargs,
            "repo_path": repo_path,
            "task_path": task_file,
            "head_subject": _git(repo_path, "log", "-1", "--format=%s"),
            "head_commit": _git(repo_path, "rev-parse", "HEAD"),
            "head_tree": _git(repo_path, "rev-parse", "HEAD^{tree}"),
            "head_branch": _git(repo_path, "branch", "--show-current"),
            "local_email": _git(repo_path, "config", "--local", "user.email"),
            "local_name": _git(repo_path, "config", "--local", "user.name"),
            "head_files": tuple(
                _git(repo_path, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
            ),
            "head_seed": _git(repo_path, "show", "HEAD:seed.txt"),
            "task_exists_before_harness": task_file.is_file(),
            "contract_exists_before_harness": task_file.with_name("contract.yaml").exists(),
            "runs_exist_before_harness": (repo_path / ".agent" / "runs").exists(),
        }
        self._owner.records.append(record)

        result = HarnessOrchestrator(**self._kwargs).run(task_file, repo_path)

        contract_path = task_file.with_name("contract.yaml")
        trace_paths = tuple((repo_path / ".agent" / "runs").glob("*/trace.json"))
        record.update(
            {
                "contract_path": contract_path.resolve(),
                "contract_exists_after_harness": contract_path.is_file(),
                "trace_paths": trace_paths,
                "trace_payload": json.loads(trace_paths[0].read_text(encoding="utf-8")),
                "harness_repairs": result.repairs,
            }
        )
        return result


class InspectingHarnessFactory:
    """Factory that records boundaries while preserving real Harness behavior."""

    def __init__(self):
        self.records = []

    def __call__(self, **kwargs):
        return _InspectingHarness(self, kwargs)


class FakeContext:
    def to_dict(self):
        return {
            "version": "test-v1",
            "memory_context": [{"id": "memory-1"}],
            "strategies": [{"id": "strategy-1"}],
        }


class FakeState:
    def to_dict(self):
        return {
            "status": "FINISHED",
            "errors": [],
            "issues": [{"status": "RESOLVED", "description": "private"}],
            "task_description": "private task text",
        }


class _BranchHarness:
    """Narrow fake for trace/result failures the real Harness cannot induce safely."""

    def __init__(self, owner, kwargs):
        self._owner = owner
        self._kwargs = kwargs

    def run(self, task_file, repo_path):
        del task_file
        repo_path = Path(repo_path).resolve()
        self._owner.repo_paths.append(repo_path)
        if self._owner.scenario == "exception":
            raise RuntimeError("private harness output")

        observer = self._kwargs["observer"]
        observer.on_context(FakeContext())
        observer.on_runtime_state(FakeState())
        self._write_trace(repo_path)

        evidence = _passing_validation_evidence()
        validation = ValidationResult(())
        if self._owner.scenario == "error":
            return SimpleNamespace(
                report=None,
                error="private error",
                repairs=2,
                validation_evidence=None,
                validator_result=None,
            )
        if self._owner.scenario == "no_report":
            return SimpleNamespace(
                report=None,
                error=None,
                repairs=2,
                validation_evidence=None,
                validator_result=None,
            )
        report = (
            SimpleNamespace(status="FAIL")
            if self._owner.scenario == "report_conflict"
            else None
        )
        return SimpleNamespace(
            report=report,
            error=None,
            repairs=self._owner.repairs,
            validation_evidence=evidence,
            validator_result=validation,
        )

    def _write_trace(self, repo_path):
        scenario = self._owner.scenario
        if scenario == "no_trace":
            return
        payloads = [self._payload()]
        if scenario == "multiple_traces":
            payloads.append({"events": []})
        for index, payload in enumerate(payloads):
            path = repo_path / ".agent" / "runs" / f"run-{index}" / "trace.json"
            path.parent.mkdir(parents=True)
            if scenario == "invalid_json":
                path.write_text("{malformed", encoding="utf-8")
            else:
                path.write_text(json.dumps(payload), encoding="utf-8")

    def _payload(self):
        scenario = self._owner.scenario
        if scenario == "malformed_trace":
            return {"events": {}}
        if scenario == "malformed_event":
            return {"events": [1]}
        if scenario == "malformed_data":
            return {"events": [{"type": "action_decided", "data": "private"}]}
        events = [
            {
                "type": "action_decided",
                "data": {"action_type": "WRITE", "target": "result.txt"},
            }
        ]
        if (
            isinstance(self._owner.repairs, int)
            and not isinstance(self._owner.repairs, bool)
            and self._owner.repairs >= 0
        ):
            events.extend(
                {"type": "repair_started", "data": {"attempt": index + 1}}
                for index in range(self._owner.repairs)
            )
        return {"events": events}


class BranchHarnessFactory:
    def __init__(self, scenario="valid", repairs=0):
        self.scenario = scenario
        self.repairs = repairs
        self.repo_paths = []

    def __call__(self, **kwargs):
        return _BranchHarness(self, kwargs)


def _git(repo_path, *args):
    return subprocess.run(
        ("git", *args),
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _passing_validation_evidence():
    return ValidationEvidence(
        workspace=WorkspaceIdentity("D:/fixture", "base", "base", "one", "one"),
        change_context=ChangeContext((FileChange("result.txt", "A", 1, 0),)),
        execution_results=(),
        verification=(),
    )


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.fixture
def evaluation_fixture(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshots" / "source"
    snapshot.mkdir(parents=True)
    (snapshot / "seed.txt").write_text("immutable fixture\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def make_task(task_id="task-1", description="write result"):
        claim = VerificationClaim(
            id="result_file",
            description="The generated result file has the expected content.",
            request={
                "argv": (
                    "python",
                    "-c",
                    "from pathlib import Path; raise SystemExit(Path('result.txt').read_text() != 'generated\\n')",
                )
            },
        )
        return EvaluationTask(
            id=task_id,
            description=description,
            repository_snapshot="snapshots/source",
            expected_files=("result.txt",),
            changes=(
                ExpectedChange(
                    id="result_file_content",
                    description="The generated result file has the expected content.",
                ),
            ),
            verification=Verification(
                claims=[claim],
                coverage=[
                    ClaimCoverage(
                        claim_id=claim.id,
                        change_id="result_file_content",
                        coverage_type="direct",
                    )
                ],
            ),
            difficulty="low",
        )

    return SimpleNamespace(
        snapshot=snapshot,
        make_task=make_task,
        dataset=lambda *tasks: EvaluationDataset(
            id="test-dataset",
            tasks=tuple(tasks),
            dataset_root=tmp_path,
        ),
    )
