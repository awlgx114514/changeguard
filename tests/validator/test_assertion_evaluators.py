from code_governance.contract.schema import ChangeContract, ExpectedChange, Verification
from code_governance.git.context import ChangeContext, FileChange
from code_governance.runtime.execution import ExecutionResult
from code_governance.validator.assertion import (
    AssertionStatus,
    AssertionType,
    VerificationAssertion,
)
from code_governance.validator.assertions import AssertionEvaluatorRegistry
from code_governance.validator.core import ValidatorEngine
from code_governance.validator.evidence import (
    ValidationEvidence,
    VerificationEvidence,
    WorkspaceIdentity,
)


def _evidence(
    returncode=0,
    changes=(FileChange("src/auth.py", "M", 1, 0),),
    diff_text="",
    execution_success=True,
):
    result = ExecutionResult(
        action_id="verification:auth_tests",
        status="SUCCESS" if execution_success else "FAILURE",
        metadata={"returncode": returncode},
    )
    verification = VerificationEvidence(
        claim_id="auth_tests",
        request_id="verification:auth_tests",
        action_id="verification:auth_tests",
        execution_result_id=result.action_id,
        argv=("python", "-m", "pytest", "tests/test_auth.py"),
        expected_result={"exit_code": 0},
        actual_result={"returncode": returncode},
        passed=execution_success and returncode == 0,
        execution_result=result,
    )
    return ValidationEvidence(
        workspace=WorkspaceIdentity("D:/fixture", "base", "base", "one", "one"),
        change_context=ChangeContext(changes, diff_text=diff_text),
        execution_results=(result,),
        verification=(verification,),
    )


def _assertion(assertion_type, config, claim_id=None):
    return VerificationAssertion(
        id=f"assert_{assertion_type.value}",
        change_id="reject_empty_password",
        claim_id=claim_id,
        assertion_type=assertion_type,
        config=config,
    )


def _required_contract(assertions):
    contract = ChangeContract(
        goal="Reject empty passwords.",
        changes=[ExpectedChange(id="reject_empty_password", description="Reject empty passwords.")],
        verification=Verification(assertions=list(assertions)),
    )
    return contract


def test_exit_code_assertion_passes_from_verification_evidence():
    result = AssertionEvaluatorRegistry().evaluate(
        _assertion(AssertionType.EXIT_CODE, {"expected_exit_code": 0}, "auth_tests"),
        _evidence(),
    )

    assert result.status is AssertionStatus.PASSED
    assert result.evidence_refs == ("verification:auth_tests",)


def test_exit_code_assertion_fails_when_return_code_differs():
    result = AssertionEvaluatorRegistry().evaluate(
        _assertion(AssertionType.EXIT_CODE, {"expected_exit_code": 0}, "auth_tests"),
        _evidence(returncode=1),
    )

    assert result.status is AssertionStatus.FAILED


def test_file_changed_assertion_uses_change_context():
    result = AssertionEvaluatorRegistry().evaluate(
        _assertion(AssertionType.FILE_CHANGED, {"path": "src/auth.py"}),
        _evidence(),
    )

    assert result.status is AssertionStatus.PASSED
    assert result.evidence_refs == ("change:src/auth.py",)


def test_diff_contains_assertion_uses_explicit_diff_fact():
    result = AssertionEvaluatorRegistry().evaluate(
        _assertion(AssertionType.DIFF_CONTAINS, {"pattern": "check_password"}),
        _evidence(diff_text="@@ -1 +1 @@\n+def check_password(password):\n"),
    )

    assert result.status is AssertionStatus.PASSED
    assert result.evidence_refs == ("workspace_diff",)


def test_behavior_probe_is_explicitly_unsupported():
    result = AssertionEvaluatorRegistry().evaluate(
        _assertion(AssertionType.BEHAVIOR_PROBE, {}),
        _evidence(),
    )

    assert result.status is AssertionStatus.UNSUPPORTED


def test_validator_accepts_assertion_results_without_changing_its_outcome():
    evidence = _evidence(changes=())
    before = ValidatorEngine(rules=()).validate(ChangeContract.from_mapping({
        "metadata": {},
        "intent": {"goal": "test"},
        "expected": {"files": [], "change_level": "L1"},
        "constraints": {},
        "verification": {},
    }), evidence)
    after = ValidatorEngine(rules=()).validate(
        ChangeContract.from_mapping({
            "metadata": {},
            "intent": {"goal": "test"},
            "expected": {"files": [], "change_level": "L1"},
            "constraints": {},
            "verification": {},
        }),
        evidence,
        assertion_results=(),
    )

    assert after == before


def test_exit_code_does_not_replace_execution_lifecycle_failure():
    assertion = _assertion(
        AssertionType.EXIT_CODE,
        {"expected_exit_code": 0},
        "auth_tests",
    )
    evidence = _evidence(returncode=None, execution_success=False)
    assertion_result = AssertionEvaluatorRegistry().evaluate(assertion, evidence)
    validation = ValidatorEngine(rules=()).validate(
        _required_contract((assertion,)),
        evidence,
        assertion_results=(assertion_result,),
    )

    assert assertion_result.status is AssertionStatus.UNSUPPORTED
    assert {finding.code for finding in validation.findings} == {
        "verification_execution_failed"
    }


def test_validator_uses_failed_required_assertion_as_correctness_finding():
    assertion = _assertion(
        AssertionType.FILE_CHANGED,
        {"path": "src/auth.py"},
    )
    assertion_result = AssertionEvaluatorRegistry().evaluate(
        assertion,
        _evidence(changes=()),
    )

    validation = ValidatorEngine(rules=()).validate(
        _required_contract((assertion,)),
        _evidence(changes=()),
        assertion_results=(assertion_result,),
    )

    assert {finding.code for finding in validation.findings} == {"ASSERTION_FAILED"}
