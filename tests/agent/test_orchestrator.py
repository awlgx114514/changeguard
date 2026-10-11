from pathlib import Path
import subprocess

from code_governance.agent.contract_agent import ContractAgent
from code_governance.agent.orchestrator import Orchestrator
from code_governance.contract.schema import (
    ChangeContract,
    Constraints,
    Expected,
    Intent,
    Metadata,
    Verification,
    VerificationClaim,
)
from code_governance.runtime.action import Action, ActionType
from code_governance.tools import ApprovalOutcome


class StubCodingAgent:
    def __init__(self):
        self.decisions = 0

    def decide(self, state, context):
        self.decisions += 1
        return Action.create(ActionType.FINISH)


class ApprovedProvider:
    def request(self, request):
        return ApprovalOutcome.APPROVED


def prepare_repo(tmp_path: Path, filename: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (
        ("init",),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test User"),
    ):
        subprocess.run(["git", *command], cwd=repo, check=True, capture_output=True, text=True)
    (repo / filename).write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True, text=True)
    (repo / filename).write_text("new\n", encoding="utf-8")
    return repo


def task_file(tmp_path: Path) -> Path:
    path = tmp_path / "task.md"
    path.write_text("Update the application", encoding="utf-8")
    return path


def make_contract(expected_files, forbidden=()):
    return ChangeContract(
        metadata=Metadata(),
        intent=Intent(goal="update application"),
        expected=Expected(files=list(expected_files), change_level="L1"),
        constraints=Constraints(forbidden=list(forbidden)),
        verification=Verification(claims=[VerificationClaim(id="diff_check", description="Diff is valid.", request={"argv": ("git", "diff", "--check")})]),
    )


def test_orchestrator_runs_task_writes_contract_and_reports_pass(tmp_path):
    repo = prepare_repo(tmp_path, "app.py")
    contract = make_contract(["app.py"])
    coding_agent = StubCodingAgent()
    orchestrator = Orchestrator(
        contract_agent=ContractAgent(generator=lambda task: contract),
        coding_agent=coding_agent,
        approval_provider=ApprovedProvider(),
    )

    result = orchestrator.run(task_file(tmp_path), repo)

    assert result.success is True
    assert result.report.status == "PASS"
    assert result.contract_path == tmp_path / "contract.yaml"
    assert result.contract_path.is_file()
    assert coding_agent.decisions == 1


def test_validator_fail_runs_two_repair_attempts(tmp_path):
    repo = prepare_repo(tmp_path, "forbidden.py")
    contract = make_contract([], forbidden=["forbidden.py"])
    coding_agent = StubCodingAgent()
    orchestrator = Orchestrator(
        contract_agent=ContractAgent(generator=lambda task: contract),
        coding_agent=coding_agent,
        approval_provider=ApprovedProvider(),
    )

    result = orchestrator.run(task_file(tmp_path), repo)

    assert result.success is False
    assert result.report.status == "FAIL"
    assert result.repairs == 2
    assert coding_agent.decisions == 3
