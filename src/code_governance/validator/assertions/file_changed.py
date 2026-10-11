from code_governance.validator.assertion import AssertionResult, AssertionStatus, AssertionType, VerificationAssertion
from code_governance.validator.evidence import ValidationEvidence


class FileChangedEvaluator:
    assertion_type = AssertionType.FILE_CHANGED

    def evaluate(self, assertion: VerificationAssertion, evidence: ValidationEvidence) -> AssertionResult:
        path = assertion.config.get("path")
        if not isinstance(path, str) or not path:
            return AssertionResult(
                assertion.id,
                assertion.change_id,
                assertion.assertion_type,
                AssertionStatus.FAILED,
                (),
                "file_changed assertion requires non-empty config.path",
            )
        changed = path in evidence.change_context.changed_files
        return AssertionResult(
            assertion.id,
            assertion.change_id,
            assertion.assertion_type,
            AssertionStatus.PASSED if changed else AssertionStatus.FAILED,
            (f"change:{path}",) if changed else (),
            f"File {path} {'is' if changed else 'is not'} present in ChangeContext",
        )
