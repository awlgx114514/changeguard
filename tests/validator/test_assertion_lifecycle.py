from code_governance.contract.schema import ChangeContract, ExpectedChange, Verification
from code_governance.git.context import ChangeContext
from code_governance.runtime.execution import ExecutionResult
from code_governance.validator.assertion import (
    AssertionResult,
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


def _evidence(*, returncode=1, execution_success=True, diff_text=""):
    execution = ExecutionResult(
        action_id="verification:tests",
        status="SUCCESS" if execution_success else "FAILURE",
        metadata={"returncode": returncode},
    )
    claim = VerificationEvidence(
        claim_id="tests",
        request_id="request:tests",
        action_id=execution.action_id,
        execution_result_id=execution.action_id,
        argv=("python", "-m", "pytest"),
        expected_result={"exit_code": 0},
        actual_result={"returncode": returncode},
        passed=execution_success and returncode == 0,
        execution_result=execution,
    )
    return ValidationEvidence(
        workspace=WorkspaceIdentity("D:/repo", "base", "base", "a", "a"),
        change_context=ChangeContext((), diff_text=diff_text),
        execution_results=(execution,),
        verification=(claim,),
    )


def _assertion(assertion_type, config, *, claim_id=None):
    return VerificationAssertion(
        id="assertion",
        change_id="change",
        claim_id=claim_id,
        assertion_type=assertion_type,
        config=config,
    )


def _contract(assertion, *, requirement="required"):
    return ChangeContract(
        goal="Make the requested change.",
        changes=[
            ExpectedChange(
                id="change",
                description="Requested behavior is present.",
                requirement=requirement,
            )
        ],
        verification=Verification(assertions=[assertion]),
    )


def test_verification_failure_remains_primary_over_failed_exit_code_assertion():
    assertion = _assertion(
        AssertionType.EXIT_CODE,
        {"expected_exit_code": 0},
        claim_id="tests",
    )
    evidence = _evidence(returncode=1)
    result = AssertionEvaluatorRegistry().evaluate(assertion, evidence)
    validation = ValidatorEngine(rules=()).validate(
        _contract(assertion), evidence, assertion_results=(result,)
    )

    assert result.status is AssertionStatus.FAILED
    assert {finding.code for finding in validation.findings} == {"verification_failed"}


def test_required_unsupported_assertion_prevents_correctness_pass():
    assertion = _assertion(AssertionType.BEHAVIOR_PROBE, {})
    evidence = _evidence(returncode=0)
    result = AssertionEvaluatorRegistry().evaluate(assertion, evidence)
    validation = ValidatorEngine(rules=()).validate(
        _contract(assertion), evidence, assertion_results=(result,)
    )

    assert result.status is AssertionStatus.UNSUPPORTED
    assert {finding.code for finding in validation.findings} == {"ASSERTION_UNSUPPORTED"}


def test_planning_only_assertion_does_not_change_correctness():
    assertion = _assertion(AssertionType.FILE_CHANGED, {"path": "src/auth.py"})
    result = AssertionResult(
        assertion_id=assertion.id,
        change_id=assertion.change_id,
        assertion_type=assertion.assertion_type,
        status=AssertionStatus.FAILED,
        evidence_refs=(),
        message="path did not change",
    )
    evidence = _evidence(returncode=0)
    validation = ValidatorEngine(rules=()).validate(
        _contract(assertion, requirement="planning_only"),
        evidence,
        assertion_results=(result,),
    )

    assert validation.status == "PASS"
    assert validation.assertion_results == (result,)


def test_diff_contains_failure_is_evaluated_from_the_frozen_change_context():
    assertion = _assertion(AssertionType.DIFF_CONTAINS, {"pattern": "new_behavior"})
    result = AssertionEvaluatorRegistry().evaluate(
        assertion, _evidence(returncode=0, diff_text="+old_behavior\n")
    )

    assert result.status is AssertionStatus.FAILED
