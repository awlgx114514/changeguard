from code_governance.validator.assertion import AssertionResult, AssertionStatus, AssertionType, VerificationAssertion
from code_governance.validator.evidence import ValidationEvidence


class DiffContainsEvaluator:
    assertion_type = AssertionType.DIFF_CONTAINS

    def evaluate(self, assertion: VerificationAssertion, evidence: ValidationEvidence) -> AssertionResult:
        pattern = assertion.config.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return AssertionResult(
                assertion.id,
                assertion.change_id,
                assertion.assertion_type,
                AssertionStatus.FAILED,
                (),
                "diff_contains assertion requires non-empty config.pattern",
            )
        diff_text = evidence.change_context.diff_text
        contains = pattern in diff_text
        return AssertionResult(
            assertion.id,
            assertion.change_id,
            assertion.assertion_type,
            AssertionStatus.PASSED if contains else AssertionStatus.FAILED,
            ("workspace_diff",) if diff_text else (),
            f"Workspace diff {'contains' if contains else 'does not contain'} {pattern!r}",
        )
