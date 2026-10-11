from types import SimpleNamespace

import pytest

from code_governance.runtime.action import OperationKind
from code_governance.runtime.action_executor import CanonicalActionExecutor
from code_governance.tools import ApprovalOutcome, ShellTool, ToolRegistry
from code_governance.tools.policy import ToolPolicy, ToolRuntimeConfig
from code_governance.verification.request import VerificationRequest, verification_action


class ApprovedProvider:
    def request(self, request):
        return ApprovalOutcome.APPROVED


def _registry(*, policy=None, approval_provider=None):
    registry = ToolRegistry(policy=policy, approval_provider=approval_provider)
    registry.register(ShellTool())
    return registry


def test_verification_request_preserves_argv_in_a_command_run_action():
    request = VerificationRequest(
        id="verification-action-1",
        claim_id="claim-parser",
        argv=("python", "-m", "pytest", "test.py"),
        reason="verify parser behavior",
    )

    action = verification_action(request)

    assert action.id == "verification-action-1"
    assert action.operation.kind is OperationKind.COMMAND_RUN
    assert action.operation.fields == {"argv": ("python", "-m", "pytest", "test.py")}
    assert action.reason == "verify parser behavior"


def test_verification_request_rejects_an_opaque_shell_command_string():
    with pytest.raises(TypeError, match="argv must be a tuple"):
        VerificationRequest(
            id="shell-command",
            claim_id="claim-shell",
            argv="python -m pytest test.py",
        )


def test_verification_request_requires_an_immutable_argv_tuple():
    with pytest.raises(TypeError, match="argv must be a tuple"):
        VerificationRequest(
            id="list-command",
            claim_id="claim-list",
            argv=["python", "-m", "pytest"],
        )


def test_verification_executor_returns_action_validation_failure_before_tool_execution(tmp_path):
    request = VerificationRequest(id="invalid-verification", claim_id="claim-invalid", argv=())
    result = CanonicalActionExecutor(_registry()).execute(verification_action(request), tmp_path)

    assert result.status == "FAILURE"
    assert result.failure.stage == "ACTION_VALIDATION"
    assert result.failure.category == "INVALID_ACTION"


def test_verification_executor_uses_argv_without_shell_execution(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="verified\n", stderr="")

    monkeypatch.setattr("code_governance.tools.shell.subprocess.run", fake_run)
    request = VerificationRequest(
        id="verification-command-1",
        claim_id="claim-command",
        argv=("python", "-m", "pytest", "test.py"),
    )
    result = CanonicalActionExecutor(
        _registry(policy=ToolPolicy(), approval_provider=ApprovedProvider())
    ).execute(verification_action(request), tmp_path)

    assert result.success is True
    assert result.action_id == request.id
    assert result.metadata["returncode"] == 0
    assert calls == [
        (["python", "-m", "pytest", "test.py"], {
            "cwd": tmp_path,
            "capture_output": True,
            "text": True,
            "timeout": 30.0,
            "check": False,
        })
    ]


def test_nonzero_verification_exit_is_collected_not_a_tool_execution_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "code_governance.tools.shell.subprocess.run",
        lambda command, **kwargs: SimpleNamespace(returncode=3, stdout="", stderr="failed"),
    )
    request = VerificationRequest(
        id="verification-nonzero",
        claim_id="claim-nonzero",
        argv=("python", "-c", "raise SystemExit(3)"),
    )

    result = CanonicalActionExecutor(
        _registry(policy=ToolPolicy(), approval_provider=ApprovedProvider())
    ).execute(verification_action(request), tmp_path)

    assert result.success is True
    assert result.metadata["returncode"] == 3
    assert result.failure is None


def test_verification_policy_deny_prevents_shell_invocation(tmp_path, monkeypatch):
    invoked = False

    def fake_run(command, **kwargs):
        nonlocal invoked
        invoked = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("code_governance.tools.shell.subprocess.run", fake_run)
    request = VerificationRequest(
        id="verification-denied",
        claim_id="claim-denied",
        argv=("python", "-c", "pass"),
    )
    result = CanonicalActionExecutor(
        _registry(policy=ToolPolicy(ToolRuntimeConfig(allowed_tools=("git",))))
    ).execute(verification_action(request), tmp_path)

    assert result.status == "FAILURE"
    assert result.failure.stage == "PRE_EXECUTION_GOVERNANCE"
    assert result.failure.category == "POLICY_DENY"
    assert invoked is False


def test_verification_without_approval_provider_fails_closed(tmp_path):
    request = VerificationRequest(
        id="verification-no-approval",
        claim_id="claim-no-approval",
        argv=("python", "-c", "pass"),
    )

    result = CanonicalActionExecutor(_registry(policy=ToolPolicy())).execute(
        verification_action(request), tmp_path
    )

    assert result.status == "FAILURE"
    assert result.failure.stage == "PRE_EXECUTION_GOVERNANCE"
    assert result.failure.category == "APPROVAL_UNAVAILABLE"
