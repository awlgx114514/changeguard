import json
import subprocess

import pytest

from code_governance.contract.assertion import AssertionType, VerificationAssertion
from code_governance.contract.schema import ChangeContract, ExpectedChange, Verification
from code_governance.runtime.execution import ExecutionResult, FailureRecord
from code_governance.tools.registry import ToolRegistry
from code_governance.validator.assertion import AssertionStatus
from code_governance.validator.assertions import AssertionEvaluatorRegistry
from code_governance.validator.core import ValidatorEngine
from code_governance.validator.evidence import (
    ValidationEvidence,
    VerificationEvidence,
    WorkspaceIdentity,
)
from code_governance.git.context import ChangeContext


def _evidence(
    stdout,
    *,
    returncode=0,
    execution_success=True,
    workspace_repository="D:/fixture",
):
    result = ExecutionResult(
        action_id="verification:empty_password_probe",
        status="SUCCESS" if execution_success else "FAILURE",
        metadata={
            "returncode": returncode,
            "stdout_preview": stdout,
            "stdout_length": len(stdout),
            "stdout_fingerprint": "sha256:test",
            "stdout_truncated": False,
        },
        failure=(
            None
            if execution_success
            else FailureRecord("tool", "TOOL_EXECUTION", action_id="verification:empty_password_probe")
        ),
    )
    verification = VerificationEvidence(
        claim_id="empty_password_probe",
        request_id="verification:empty_password_probe",
        action_id="verification:empty_password_probe",
        execution_result_id=result.action_id,
        argv=("python", "tests/probes/check_empty_password.py"),
        expected_result={"exit_code": 0},
        actual_result={"returncode": returncode},
        passed=execution_success and returncode == 0,
        execution_result=result,
    )
    return ValidationEvidence(
        workspace=WorkspaceIdentity(workspace_repository, "base", "base", "a", "a"),
        change_context=ChangeContext(()),
        execution_results=(result,),
        verification=(verification,),
    )


def _assertion(expected, *, claim_id="empty_password_probe"):
    return VerificationAssertion(
        id="empty_password_behavior",
        change_id="reject_empty_password",
        claim_id=claim_id,
        assertion_type=AssertionType.BEHAVIOR_PROBE,
        config={"probe_id": "empty_password_probe", "expected": expected},
    )


def test_behavior_assertion_passes_on_exact_observed_value(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("subprocess forbidden")),
    )
    monkeypatch.setattr(
        ToolRegistry,
        "execute",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("tool execution forbidden")),
    )
    evidence = _evidence(
        json.dumps(
            {
                "protocol": "changeguard.behavior_probe.v1",
                "probe_id": "empty_password_probe",
                "observed": {"accepted": False},
            }
        ),
        workspace_repository=str(tmp_path),
    )
    before = tuple(tmp_path.iterdir())

    result = AssertionEvaluatorRegistry().evaluate(
        _assertion({"accepted": False}), evidence
    )

    assert result.status is AssertionStatus.PASSED
    assert tuple(tmp_path.iterdir()) == before
    assert set(result.evidence_refs) == {
        "claim:empty_password_probe",
        "request:verification:empty_password_probe",
        "action:verification:empty_password_probe",
        "execution_result:verification:empty_password_probe",
    }


def test_behavior_assertion_fails_only_for_valid_mismatching_observation():
    evidence = _evidence(
        json.dumps(
            {
                "protocol": "changeguard.behavior_probe.v1",
                "probe_id": "empty_password_probe",
                "observed": {"accepted": True},
            }
        )
    )

    result = AssertionEvaluatorRegistry().evaluate(
        _assertion({"accepted": False}), evidence
    )

    assert result.status is AssertionStatus.FAILED


def test_behavior_comparison_preserves_json_boolean_and_number_types():
    evidence = _evidence(
        json.dumps(
            {
                "protocol": "changeguard.behavior_probe.v1",
                "probe_id": "empty_password_probe",
                "observed": True,
            }
        )
    )

    result = AssertionEvaluatorRegistry().evaluate(_assertion(1), evidence)

    assert result.status is AssertionStatus.FAILED


def test_non_json_expected_value_is_unsupported():
    assertion = _assertion({"not_json": {1, 2}})
    evidence = _evidence(
        json.dumps(
            {
                "protocol": "changeguard.behavior_probe.v1",
                "probe_id": "empty_password_probe",
                "observed": {},
            }
        )
    )

    result = AssertionEvaluatorRegistry().evaluate(assertion, evidence)

    assert result.status is AssertionStatus.UNSUPPORTED
    assert "behavior_probe_evidence_invalid" in result.message


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json",
        '{"protocol":"wrong","probe_id":"empty_password_probe","observed":false}',
        '{"protocol":"changeguard.behavior_probe.v1","probe_id":"other","observed":false}',
        '{"protocol":"changeguard.behavior_probe.v1","probe_id":"empty_password_probe"}',
    ],
)
def test_invalid_probe_evidence_is_unsupported_not_behavior_failure(stdout):
    result = AssertionEvaluatorRegistry().evaluate(
        _assertion(False), _evidence(stdout)
    )

    assert result.status is AssertionStatus.UNSUPPORTED
    assert "behavior_probe_evidence_invalid" in result.message


def test_execution_failure_remains_primary_over_probe_assertion():
    assertion = _assertion(False)
    evidence = _evidence("", execution_success=False)
    result = AssertionEvaluatorRegistry().evaluate(assertion, evidence)
    contract = ChangeContract(
        goal="Reject empty passwords.",
        changes=[ExpectedChange(id="reject_empty_password", description="Reject empty passwords.")],
        verification=Verification(assertions=[assertion]),
    )
    validation = ValidatorEngine(rules=()).validate(
        contract, evidence, assertion_results=(result,)
    )

    assert result.status is AssertionStatus.UNSUPPORTED
    assert {finding.code for finding in validation.findings} == {
        "verification_execution_failed"
    }


def test_nonzero_probe_claim_remains_verification_failure():
    assertion = _assertion(False)
    evidence = _evidence(
        '{"protocol":"changeguard.behavior_probe.v1","probe_id":"empty_password_probe","observed":true}',
        returncode=1,
    )
    result = AssertionEvaluatorRegistry().evaluate(assertion, evidence)
    contract = ChangeContract(
        goal="Reject empty passwords.",
        changes=[ExpectedChange(id="reject_empty_password", description="Reject empty passwords.")],
        verification=Verification(assertions=[assertion]),
    )
    validation = ValidatorEngine(rules=()).validate(
        contract, evidence, assertion_results=(result,)
    )

    assert result.status is AssertionStatus.UNSUPPORTED
    assert {finding.code for finding in validation.findings} == {"verification_failed"}
