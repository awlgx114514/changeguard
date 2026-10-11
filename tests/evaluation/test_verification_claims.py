from pathlib import Path
import subprocess

from code_governance.contract.schema import ClaimCoverage, ExpectedChange, Verification, VerificationClaim
from code_governance.evaluation.dataset import load_dataset
from code_governance.evaluation.task import EvaluationTask
from code_governance.agent.contract_agent import ContractAgent
from code_governance.agent.harness import HarnessOrchestrator
from code_governance.runtime.action import Action, ActionType
from code_governance.tools import ApprovalOutcome


def _claim():
    return VerificationClaim.model_validate(
        {
            "id": "unit_regression",
            "description": "The unit regression suite passes.",
            "request": {"argv": ["python", "-m", "unittest", "-q"]},
            "expected_result": {"exit_code": 0},
        }
    )


def _task(claims=(_claim(),)):
    change = ExpectedChange(
        id="greeting",
        description="Greeting is returned.",
        requirement="required",
    )
    return EvaluationTask(
        id="simple-change",
        description="Return a greeting.",
        repository_snapshot="snapshots/simple-change",
        expected_files=("app.py",),
        changes=(change,),
        verification=Verification(
            claims=list(claims),
            coverage=[
                ClaimCoverage(
                    claim_id=claim.id,
                    change_id=change.id,
                    coverage_type="direct",
                )
                for claim in claims
            ],
        ),
        difficulty="low",
    )


def test_evaluation_task_generates_a_structured_claim_contract():
    contract = _task().to_contract()

    assert contract.verification.claims == [_claim()]
    request = contract.verification.claims[0].to_request()
    assert request.argv == ("python", "-m", "unittest", "-q")
    assert request.id == "verification:unit_regression"


def test_dataset_loads_explicit_claim_argv_without_a_legacy_command_field(tmp_path):
    snapshot = tmp_path / "snapshots" / "simple-change"
    snapshot.mkdir(parents=True)
    (tmp_path / "tasks.yaml").write_text(
        """id: evaluation-v2
tasks:
  - id: simple-change
    description: Return a greeting.
    repository_snapshot: snapshots/simple-change
    expected_files: [app.py]
    changes:
      - id: greeting
        description: Greeting is returned.
        requirement: required
    verification:
      claims:
        - id: unit_regression
          description: The unit regression suite passes.
          request:
            argv: [python, -m, unittest, -q]
          expected_result:
            exit_code: 0
      coverage:
        - claim_id: unit_regression
          change_id: greeting
          coverage_type: direct
    difficulty: low
""",
        encoding="utf-8",
    )

    dataset = load_dataset(tmp_path / "tasks.yaml")

    assert dataset.tasks[0].verification.claims[0].request.argv == (
        "python",
        "-m",
        "unittest",
        "-q",
    )


class _ApprovedProvider:
    def request(self, request):
        return ApprovalOutcome.APPROVED


class _FinishingAgent:
    def decide(self, state, context):
        return Action.create(ActionType.FINISH)


def test_evaluation_claim_command_failure_preserves_its_execution_fact(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    for command in (("init",), ("config", "user.email", "test@example.com"), ("config", "user.name", "Test User")):
        subprocess.run(("git", *command), cwd=repository, check=True, capture_output=True, text=True)
    (repository / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(("git", "add", "."), cwd=repository, check=True, capture_output=True, text=True)
    subprocess.run(("git", "commit", "-m", "initial"), cwd=repository, check=True, capture_output=True, text=True)
    task_file = tmp_path / "task.md"
    task_file.write_text("Return a greeting.", encoding="utf-8")
    task = _task(
        (
            VerificationClaim(
                id="failing_check",
                description="The check intentionally fails.",
                request={"argv": ("python", "-c", "raise SystemExit(7)")},
            ),
        )
    )

    result = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda task_text, context: task.to_contract()),
        coding_agent=_FinishingAgent(),
        approval_provider=_ApprovedProvider(),
    ).run(task_file, repository)

    command = result.report.verification.commands[0]
    assert result.report.status == "FAIL"
    assert command.returncode is not None
    assert command.returncode != 0
    assert command.passed is False
