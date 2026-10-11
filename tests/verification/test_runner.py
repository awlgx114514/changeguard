from types import SimpleNamespace

from code_governance.contract.schema import VerificationClaim
from code_governance.runtime.action_executor import CanonicalActionExecutor
from code_governance.tools import ApprovalOutcome, ShellTool, ToolPolicy, ToolRegistry
from code_governance.verification.runner import execute_verification_claims


class ApprovedProvider:
    def request(self, request):
        return ApprovalOutcome.APPROVED


def _executor():
    registry = ToolRegistry(policy=ToolPolicy(), approval_provider=ApprovedProvider())
    registry.register(ShellTool())
    return CanonicalActionExecutor(registry)


def _claim(expected_result=0):
    return VerificationClaim(
        id="runner_check",
        description="The check command completes with its declared exit code.",
        request={"argv": ("python", "-m", "pytest", "tests/test_sample.py")},
        expected_result={"exit_code": expected_result},
    )


def test_runner_records_passed_claim_from_canonical_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "code_governance.tools.shell.subprocess.run",
        lambda command, **kwargs: SimpleNamespace(returncode=0, stdout="ok\n", stderr=""),
    )
    summary = execute_verification_claims((_claim(),), _executor(), tmp_path).summary

    assert summary.all_passed is True
    assert len(summary.commands) == 1
    assert summary.commands[0].passed is True
    assert summary.commands[0].returncode == 0
    assert "ok" in summary.commands[0].stdout


def test_runner_records_failed_claim_without_reclassifying_the_tool_result(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "code_governance.tools.shell.subprocess.run",
        lambda command, **kwargs: SimpleNamespace(returncode=3, stdout="", stderr="failed"),
    )
    summary = execute_verification_claims((_claim(),), _executor(), tmp_path).summary

    assert summary.all_passed is False
    assert summary.commands[0].passed is False
    assert summary.commands[0].returncode == 3


def test_runner_returns_empty_success_for_no_claims(tmp_path):
    summary = execute_verification_claims((), _executor(), tmp_path).summary

    assert summary.all_passed is True
    assert summary.commands == ()
