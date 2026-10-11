from types import SimpleNamespace

import pytest

from code_governance.contract.schema import VerificationClaim
from code_governance.runtime.action_executor import CanonicalActionExecutor
from code_governance.tools import ApprovalOutcome, ShellTool, ToolRegistry
from code_governance.tools.policy import ToolPolicy
from code_governance.verification.runner import execute_verification_claims


class ApprovedProvider:
    def request(self, request):
        return ApprovalOutcome.APPROVED


def _executor():
    registry = ToolRegistry(policy=ToolPolicy(), approval_provider=ApprovedProvider())
    registry.register(ShellTool())
    return CanonicalActionExecutor(registry)


def test_claim_generates_a_structured_request_and_command_action():
    claim = VerificationClaim.model_validate(
        {
            "id": "login_regression",
            "description": "login bug fixed",
            "request": {"argv": ["python", "-m", "pytest", "tests/login.py"]},
            "expected_result": {"exit_code": 0},
        }
    )

    request = claim.to_request()
    action = claim.to_action()

    assert request.id == "verification:login_regression"
    assert request.claim_id == claim.id
    assert request.argv == ("python", "-m", "pytest", "tests/login.py")
    assert action.id == request.id
    assert action.operation.kind.value == "command.run"
    assert action.operation.fields["argv"] == request.argv


def test_claim_schema_rejects_an_opaque_shell_string_request():
    with pytest.raises(ValueError, match="argv must be an explicit argument sequence"):
        VerificationClaim.model_validate(
            {
                "id": "bad-command",
                "description": "do not parse shell text",
                "request": {"argv": "pytest tests/login.py"},
                "expected_result": {"exit_code": 0},
            }
        )


def test_claim_execution_correlates_claim_request_action_and_result(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "code_governance.tools.shell.subprocess.run",
        lambda command, **kwargs: SimpleNamespace(returncode=0, stdout="ok\n", stderr=""),
    )
    claim = VerificationClaim.model_validate(
        {
            "id": "parser_regression",
            "description": "parser tests pass",
            "request": {"argv": ["python", "-m", "pytest", "tests/test_parser.py"]},
            "expected_result": {"exit_code": 0},
        }
    )

    run = execute_verification_claims((claim,), _executor(), tmp_path)
    execution = run.executions[0]

    assert execution.claim_id == claim.id
    assert execution.request_id == "verification:parser_regression"
    assert execution.action_id == execution.request_id
    assert execution.result.action_id == execution.action_id
    assert execution.passed is True
    assert run.summary.all_passed is True


def test_nonzero_exit_is_a_failed_claim_not_a_tool_execution_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "code_governance.tools.shell.subprocess.run",
        lambda command, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="test failure"),
    )
    claim = VerificationClaim.model_validate(
        {
            "id": "failing_test",
            "description": "a failing test is reported as evidence",
            "request": {"argv": ["python", "-m", "pytest", "tests/test_parser.py"]},
            "expected_result": {"exit_code": 0},
        }
    )

    execution = execute_verification_claims((claim,), _executor(), tmp_path).executions[0]

    assert execution.result.success is True
    assert execution.result.metadata["returncode"] == 1
    assert execution.result.failure is None
    assert execution.passed is False


def test_missing_executable_is_an_execution_failure_not_a_command_result(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "code_governance.tools.shell.subprocess.run",
        lambda command, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing verifier")),
    )
    claim = VerificationClaim.model_validate(
        {
            "id": "missing_verifier",
            "description": "The verifier executable is available.",
            "request": {"argv": ["missing-verifier"]},
            "expected_result": {"exit_code": 0},
        }
    )

    execution = execute_verification_claims((claim,), _executor(), tmp_path).executions[0]

    assert execution.result.success is False
    assert execution.result.failure is not None
    assert execution.result.failure.stage == "TOOL_EXECUTION"
    assert execution.result.metadata.get("returncode") is None
    assert execution.passed is False
