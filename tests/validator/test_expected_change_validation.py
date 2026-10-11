from dataclasses import replace

from code_governance.contract.schema import (
    ChangeContract,
    ClaimCoverage,
    ExpectedChange,
    Scope,
    Verification,
    VerificationClaim,
)
from code_governance.git.context import ChangeContext, FileChange
from code_governance.runtime.execution import ExecutionResult
from code_governance.validator.core import ValidatorEngine
from code_governance.validator.evidence import (
    ValidationEvidence,
    VerificationEvidence,
    WorkspaceIdentity,
)


def _contract():
    claim = VerificationClaim(
        id="parser_tests",
        description="Parser tests pass.",
        request={"argv": ("python", "-m", "pytest", "tests/test_parser.py")},
    )
    return ChangeContract(
        goal="Fix parser empty-input crash.",
        scope=Scope(paths=["src/parser.py"]),
        changes=[
            ExpectedChange(
                id="parser_handles_empty",
                description="Parser handles empty input without crashing.",
                requirement="required",
            )
        ],
        verification=Verification(
            claims=[claim],
            coverage=[
                ClaimCoverage(
                    claim_id=claim.id,
                    change_id="parser_handles_empty",
                    coverage_type="direct",
                )
            ],
        ),
    )


def _evidence(returncode=0):
    result = ExecutionResult(
        action_id="verification:parser_tests",
        status="SUCCESS",
        metadata={"returncode": returncode},
    )
    verification = VerificationEvidence(
        claim_id="parser_tests",
        request_id="verification:parser_tests",
        action_id="verification:parser_tests",
        execution_result_id="verification:parser_tests",
        argv=("python", "-m", "pytest", "tests/test_parser.py"),
        expected_result={"exit_code": 0},
        actual_result={"returncode": returncode},
        passed=returncode == 0,
        execution_result=result,
    )
    return ValidationEvidence(
        workspace=WorkspaceIdentity("D:/fixture", "base", "base", "one", "one"),
        change_context=ChangeContext((FileChange("src/parser.py", "M", 1, 0),)),
        execution_results=(result,),
        verification=(verification,),
    )


def test_required_change_with_passed_direct_evidence_is_valid():
    result = ValidatorEngine().validate(_contract(), _evidence())

    assert result.status == "PASS"


def test_failed_bound_claim_preserves_verification_failure_attribution():
    result = ValidatorEngine().validate(_contract(), _evidence(returncode=1))

    assert {finding.code for finding in result.findings} == {"verification_failed"}


def test_required_change_without_collected_direct_claim_evidence_fails():
    evidence = _evidence()
    evidence = replace(evidence, execution_results=(), verification=())

    result = ValidatorEngine().validate(_contract(), evidence)

    assert {finding.code for finding in result.findings} == {
        "expected_change_evidence_missing"
    }
