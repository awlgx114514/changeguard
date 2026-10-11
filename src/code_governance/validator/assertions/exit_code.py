from code_governance.validator.assertion import AssertionResult, AssertionStatus, AssertionType, VerificationAssertion
from code_governance.validator.evidence import ValidationEvidence


class ExitCodeEvaluator:
    assertion_type = AssertionType.EXIT_CODE

    def evaluate(self, assertion: VerificationAssertion, evidence: ValidationEvidence) -> AssertionResult:
        expected = assertion.config.get("expected_exit_code")
        if type(expected) is not int:
            return AssertionResult(
                assertion.id,
                assertion.change_id,
                assertion.assertion_type,
                AssertionStatus.FAILED,
                (),
                "exit_code assertion requires integer config.expected_exit_code",
            )
        if assertion.claim_id is None:
            return AssertionResult(
                assertion.id,
                assertion.change_id,
                assertion.assertion_type,
                AssertionStatus.FAILED,
                (),
                "exit_code assertion requires a claim_id",
            )
        matches = [item for item in evidence.verification if item.claim_id == assertion.claim_id]
        if not matches:
            return AssertionResult(
                assertion.id,
                assertion.change_id,
                assertion.assertion_type,
                AssertionStatus.UNSUPPORTED,
                (),
                f"No verification evidence exists for claim {assertion.claim_id}",
            )
        if matches[-1].execution_failed:
            return AssertionResult(
                assertion.id,
                assertion.change_id,
                assertion.assertion_type,
                AssertionStatus.UNSUPPORTED,
                (f"verification:{assertion.claim_id}",),
                f"Claim {assertion.claim_id} did not complete; lifecycle failure is authoritative",
            )
        actual = matches[-1].actual_result.get("returncode")
        status = AssertionStatus.PASSED if actual == expected else AssertionStatus.FAILED
        return AssertionResult(
            assertion.id,
            assertion.change_id,
            assertion.assertion_type,
            status,
            (f"verification:{assertion.claim_id}",),
            f"Claim {assertion.claim_id} returned {actual!r}; expected {expected}",
        )
