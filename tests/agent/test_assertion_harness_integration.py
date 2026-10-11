import hashlib
from pathlib import Path
import subprocess
import sys

from code_governance.agent.contract_agent import ContractAgent
from code_governance.agent.harness import HarnessOrchestrator
from code_governance.contract.schema import (
    ChangeContract,
    ClaimCoverage,
    ExpectedChange,
    Scope,
    Verification,
    VerificationClaim,
)
from code_governance.runtime.action import Action, OperationKind
from code_governance.tools import ApprovalOutcome
from code_governance.validator.assertion import AssertionStatus
from code_governance.validator.assertions import AssertionEvaluatorRegistry


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ("git", *args), cwd=root, check=True, capture_output=True, text=True
    )


def _repository(root: Path) -> Path:
    repository = root / "repo"
    (repository / "src").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / "src" / "feature.py").write_text(
        'def feature():\n    return "disabled"\n', encoding="utf-8"
    )
    (repository / "tests" / "test_feature.py").write_text(
        'from src.feature import feature\n\n\ndef test_feature_enabled():\n'
        '    assert feature() == "enabled"\n',
        encoding="utf-8",
    )
    _git(repository, "init")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "fixture baseline")
    return repository


class _Agent:
    def __init__(self, actions):
        self.actions = list(actions)
        self.trajectory = []

    def decide(self, state, context):
        action = self.actions.pop(0)
        self.trajectory.append(action.operation.kind)
        return action


class _ApprovedProvider:
    def request(self, request):
        return ApprovalOutcome.APPROVED


def test_harness_evaluates_assertions_after_final_validation_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    repository = _repository(tmp_path)
    source = repository / "src" / "feature.py"
    original = source.read_text(encoding="utf-8")
    observed_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()
    claim = VerificationClaim(
        id="feature_test",
        description="Feature regression test passes.",
        request={
            "argv": (
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "tests/test_feature.py",
            )
        },
    )
    change = ExpectedChange(
        id="feature_enabled",
        description="feature returns enabled.",
    )
    contract = ChangeContract(
        goal="Implement feature() to return enabled.",
        scope=Scope(paths=["src/feature.py"]),
        changes=[change],
        verification=Verification(
            claims=[claim],
            coverage=[
                ClaimCoverage(
                    claim_id=claim.id,
                    change_id=change.id,
                    coverage_type="direct",
                )
            ],
            assertions=[
                {
                    "id": "feature_file_changed",
                    "change_id": change.id,
                    "assertion_type": "file_changed",
                    "config": {"path": "src/feature.py"},
                },
                {
                    "id": "feature_diff_contains_result",
                    "change_id": change.id,
                    "assertion_type": "diff_contains",
                    "config": {"pattern": 'return "enabled"'},
                },
                {
                    "id": "feature_test_exit_code",
                    "change_id": change.id,
                    "claim_id": claim.id,
                    "assertion_type": "exit_code",
                    "config": {"expected_exit_code": 0},
                },
            ],
        ),
    )
    agent = _Agent(
        (
            Action.create_operation(
                OperationKind.FILE_READ, path="src/feature.py", reason="inspect feature"
            ),
            Action.create_operation(
                OperationKind.FILE_REPLACE,
                path="src/feature.py",
                expected_text='return "disabled"',
                replacement='return "enabled"',
                observed_file_hash=observed_hash,
                reason="enable the feature",
            ),
            Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="done"),
        )
    )
    observed_evidence = []
    evaluate = AssertionEvaluatorRegistry.evaluate

    def record_evaluation(registry, assertion, evidence):
        observed_evidence.append(evidence)
        return evaluate(registry, assertion, evidence)

    monkeypatch.setattr(AssertionEvaluatorRegistry, "evaluate", record_evaluation)
    task_file = tmp_path / "task.md"
    task_file.write_text(contract.goal, encoding="utf-8")
    result = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda task, context: contract),
        coding_agent=agent,
        approval_provider=_ApprovedProvider(),
        max_repairs=0,
    ).run(task_file, repository)

    assert result.success is True, (
        result.validator_result.findings,
        result.validator_result.assertion_results,
    )
    assert agent.trajectory == [
        OperationKind.FILE_READ,
        OperationKind.FILE_REPLACE,
        OperationKind.WORKFLOW_FINISH,
    ]
    assert len(observed_evidence) == len(contract.verification.assertions)
    assert all(item is result.validation_evidence for item in observed_evidence)
    assert result.validation_evidence.workspace_is_consistent is True
    assert result.validation_evidence.change_context.diff_text
    assert {
        item.status for item in result.validator_result.assertion_results
    } == {AssertionStatus.PASSED}
    report = result.report
    assert len(report.assertion_definitions) == 3
    assert len(report.assertion_results) == 3
    assert report.assertion_results == result.validator_result.assertion_results
    assert report.status == result.validator_result.status
