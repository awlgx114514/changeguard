import pytest

from code_governance.contract.schema import ChangeContract, VerificationClaim
from code_governance.git.context import ChangeContext, FileChange
from code_governance.runtime.execution import ExecutionResult, FailureRecord
from code_governance.validator.core import ValidatorEngine
from code_governance.validator.evidence import (
    ValidationEvidence,
    ValidationEvidenceError,
    VerificationEvidence,
    WorkspaceIdentity,
)
from code_governance.verification.runner import VerificationExecution


def _contract():
    return ChangeContract.model_validate(
        {
            "goal": "keep the parser healthy",
            "scope": {"paths": ["src/parser.py"], "change_level": "L1"},
            "verification": {
                "claims": [
                    {
                        "id": "parser_tests",
                        "description": "parser tests pass",
                        "request": {"argv": ["python", "-m", "pytest", "tests/test_parser.py"]},
                        "expected_result": {"exit_code": 0},
                    }
                ]
            },
        }
    )


def _identity(*, before="base:abc", after="base:abc", before_fingerprint="one", after_fingerprint="one"):
    return WorkspaceIdentity(
        repository="D:/fixture",
        revision_before=before,
        revision_after=after,
        fingerprint_before=before_fingerprint,
        fingerprint_after=after_fingerprint,
    )


def _change_context():
    return ChangeContext((FileChange("src/parser.py", "M", 1, 0),))


def _verification_execution(result):
    claim = _contract().verification.claims[0]
    return VerificationExecution(claim, claim.to_request(), claim.to_action(), result)


def _evidence(result, *, identity=None):
    verification = VerificationEvidence.from_execution(_verification_execution(result))
    return ValidationEvidence(
        workspace=_identity() if identity is None else identity,
        change_context=_change_context(),
        execution_results=(result,),
        verification=(verification,),
    )


def test_verification_evidence_links_claim_request_action_and_execution_result():
    result = ExecutionResult(
        action_id="verification:parser_tests",
        status="SUCCESS",
        metadata={"returncode": 0},
    )

    evidence = _evidence(result)
    verification = evidence.verification[0]

    assert verification.claim_id == "parser_tests"
    assert verification.request_id == "verification:parser_tests"
    assert verification.action_id == "verification:parser_tests"
    assert verification.execution_result_id == result.action_id
    assert verification.expected_result == {"exit_code": 0}
    assert verification.actual_result == {"returncode": 0}
    assert verification.passed is True
    assert evidence.execution_results == (result,)


def test_nonzero_verification_exit_is_evidence_not_execution_failure():
    result = ExecutionResult(
        action_id="verification:parser_tests",
        status="SUCCESS",
        metadata={"returncode": 1},
    )

    validation = ValidatorEngine().validate(_contract(), _evidence(result))

    assert validation.status == "FAIL"
    assert {finding.code for finding in validation.findings} == {"verification_failed"}


def test_tool_launch_failure_is_not_mislabeled_as_verification_failure():
    result = ExecutionResult(
        action_id="verification:parser_tests",
        status="FAILURE",
        failure=FailureRecord(stage="TOOL_EXECUTION", category="TOOL_LAUNCH_FAILED"),
    )

    validation = ValidatorEngine().validate(_contract(), _evidence(result))

    assert validation.status == "FAIL"
    assert {finding.code for finding in validation.findings} == {"verification_execution_failed"}


def test_workspace_revision_mismatch_is_rejected_at_validation_boundary():
    result = ExecutionResult(
        action_id="verification:parser_tests",
        status="SUCCESS",
        metadata={"returncode": 0},
    )

    validation = ValidatorEngine().validate(
        _contract(),
        _evidence(result, identity=_identity(after="base:def", after_fingerprint="two")),
    )

    assert validation.status == "FAIL"
    assert {finding.code for finding in validation.findings} == {"workspace_revision_mismatch"}


def test_evidence_rejects_unlinked_execution_result():
    result = ExecutionResult(
        action_id="verification:parser_tests",
        status="SUCCESS",
        metadata={"returncode": 0},
    )
    verification = VerificationEvidence.from_execution(_verification_execution(result))

    with pytest.raises(ValidationEvidenceError, match="not present"):
        ValidationEvidence(
            workspace=_identity(),
            change_context=_change_context(),
            execution_results=(),
            verification=(verification,),
        )


def test_validator_accepts_validation_evidence_not_change_report():
    result = ExecutionResult(
        action_id="verification:parser_tests",
        status="SUCCESS",
        metadata={"returncode": 0},
    )

    validation = ValidatorEngine().validate(_contract(), _evidence(result))

    assert validation.status == "PASS"
