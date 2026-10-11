import shutil
import subprocess
import sys
from pathlib import Path

from code_governance.agent.contract_agent import ContractAgent
from code_governance.agent.harness import HarnessOrchestrator
from code_governance.contract.assertion import AssertionType
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


FIXTURE = Path(__file__).parents[1] / "fixtures" / "behavior_probe"


class _FinishAgent:
    def decide(self, state, context):
        return Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="done")


class _ApprovedProvider:
    def request(self, request):
        return ApprovalOutcome.APPROVED


def _git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, text=True)


def _contract():
    ordinary = VerificationClaim(
        id="existing_auth_test",
        description="Existing authentication behavior remains valid.",
        request={
            "argv": (
                sys.executable,
                "-B",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "tests/test_existing_auth.py",
            )
        },
    )
    behavior = VerificationClaim(
        id="empty_password_probe",
        description="Observe whether an empty password is accepted.",
        request={
            "argv": (sys.executable, "-B", "tests/probes/check_empty_password.py")
        },
    )
    change = ExpectedChange(
        id="reject_empty_password",
        description="Empty passwords are rejected.",
    )
    return ChangeContract(
        goal="Prevent empty passwords from being accepted.",
        scope=Scope(paths=["src/auth.py"]),
        changes=[change],
        verification=Verification(
            claims=[ordinary, behavior],
            coverage=[
                ClaimCoverage(claim_id=ordinary.id, change_id=change.id, coverage_type="direct"),
                ClaimCoverage(claim_id=behavior.id, change_id=change.id, coverage_type="direct"),
            ],
            assertions=[
                {
                    "id": "empty_password_behavior",
                    "change_id": change.id,
                    "claim_id": behavior.id,
                    "assertion_type": AssertionType.BEHAVIOR_PROBE.value,
                    "config": {
                        "probe_id": "empty_password_probe",
                        "expected": {"accepted": False},
                    },
                }
            ],
        ),
    )


def _run_harness(tmp_path, *, correct_behavior=False):
    repository = tmp_path / "repository"
    shutil.copytree(FIXTURE, repository)
    if correct_behavior:
        (repository / "src" / "auth.py").write_text(
            'def check_password(password):\n    return bool(password) and password == "123456"\n',
            encoding="utf-8",
        )
    _git(repository, "init")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "fixture baseline")
    task = tmp_path / "task.md"
    task.write_text("Prevent empty passwords from being accepted.", encoding="utf-8")
    contract = _contract()
    result = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda task_text, context: contract),
        coding_agent=_FinishAgent(),
        approval_provider=_ApprovedProvider(),
        max_repairs=0,
    ).run(task, repository)
    return result, contract


def test_unrelated_test_pass_does_not_hide_wrong_behavior_probe(tmp_path):
    result, contract = _run_harness(tmp_path)

    ordinary, behavior = result.validation_evidence.verification
    assertion = result.validator_result.assertion_results[0]
    assert ordinary.passed is True
    assert behavior.passed is True
    assert assertion.status is AssertionStatus.FAILED
    assert "observed {'accepted': True}" in assertion.message
    assert result.success is False
    assert any(finding.code == "ASSERTION_FAILED" for finding in result.validator_result.findings)
    assert len(contract.verification.assertions) == 1


def test_matching_behavior_probe_allows_harness_validation_to_pass(tmp_path):
    result, _ = _run_harness(tmp_path, correct_behavior=True)

    assert result.validation_evidence.verification[0].passed is True
    assert result.validation_evidence.verification[1].passed is True
    assert result.validator_result.assertion_results[0].status is AssertionStatus.PASSED
    assert result.success is True
