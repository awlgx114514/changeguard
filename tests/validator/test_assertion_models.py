import pytest

from code_governance.contract.schema import (
    ChangeContract,
    ExpectedChange,
    Scope,
    Verification,
    VerificationClaim,
)
from code_governance.contract.validator import ContractValidator
from code_governance.validator.assertion import (
    AssertionStatus,
    AssertionType,
    VerificationAssertion,
)


def _contract(assertions):
    claim = VerificationClaim(
        id="auth_tests",
        description="Authentication tests pass.",
        request={"argv": ("python", "-m", "pytest", "tests/test_auth.py")},
    )
    return ChangeContract(
        goal="Reject empty passwords.",
        scope=Scope(paths=["src/auth.py"]),
        changes=[
            ExpectedChange(
                id="reject_empty_password",
                description="Empty passwords are rejected.",
            )
        ],
        verification=Verification(claims=[claim], assertions=list(assertions)),
    )


def test_assertion_model_keeps_contract_reference_and_not_execution_fact():
    assertion = VerificationAssertion(
        id="auth_exit_code",
        change_id="reject_empty_password",
        claim_id="auth_tests",
        assertion_type=AssertionType.EXIT_CODE,
        config={"expected_exit_code": 0},
    )

    assert assertion.change_id == "reject_empty_password"
    assert assertion.claim_id == "auth_tests"
    assert assertion.model_dump()["config"] == {"expected_exit_code": 0}
    assert AssertionStatus.UNSUPPORTED.value == "unsupported"


def test_contract_validator_rejects_assertion_for_unknown_change():
    assertion = VerificationAssertion(
        id="unknown_change",
        change_id="missing_change",
        claim_id=None,
        assertion_type=AssertionType.FILE_CHANGED,
        config={"path": "src/auth.py"},
    )

    result = ContractValidator().validate(_contract([assertion]))

    assert {finding.code for finding in result.findings} == {
        "assertion_change_missing",
        "EXPECTED_CHANGE_UNVERIFIED",
    }


def test_contract_validator_rejects_assertion_for_unknown_claim():
    assertion = VerificationAssertion(
        id="unknown_claim",
        change_id="reject_empty_password",
        claim_id="missing_claim",
        assertion_type=AssertionType.FILE_CHANGED,
        config={"path": "src/auth.py"},
    )

    result = ContractValidator().validate(_contract([assertion]))

    assert "assertion_claim_missing" in {finding.code for finding in result.findings}


def test_exit_code_assertion_requires_claim_id():
    assertion = VerificationAssertion(
        id="auth_exit_code",
        change_id="reject_empty_password",
        claim_id=None,
        assertion_type=AssertionType.EXIT_CODE,
        config={"expected_exit_code": 0},
    )

    result = ContractValidator().validate(_contract([assertion]))
    assert "assertion_claim_required" in {finding.code for finding in result.findings}


def test_assertion_model_rejects_blank_identity():
    with pytest.raises(ValueError, match="must not be blank"):
        VerificationAssertion(
            id=" ",
            change_id="reject_empty_password",
            assertion_type=AssertionType.FILE_CHANGED,
            config={"path": "src/auth.py"},
        )
