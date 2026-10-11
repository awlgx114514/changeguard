from typing import Dict

from code_governance.validator.assertion import AssertionResult, AssertionType, VerificationAssertion
from code_governance.validator.evidence import ValidationEvidence

from .behavior_probe import BehaviorProbeEvaluator
from .diff_contains import DiffContainsEvaluator
from .exit_code import ExitCodeEvaluator
from .file_changed import FileChangedEvaluator


class AssertionEvaluatorRegistry:
    """One deterministic evaluator route per assertion type."""

    def __init__(self) -> None:
        evaluators = (
            ExitCodeEvaluator(),
            FileChangedEvaluator(),
            DiffContainsEvaluator(),
            BehaviorProbeEvaluator(),
        )
        self._evaluators: Dict[AssertionType, object] = {
            evaluator.assertion_type: evaluator for evaluator in evaluators
        }

    def get(self, assertion_type: AssertionType):
        return self._evaluators[assertion_type]

    def evaluate(
        self,
        assertion: VerificationAssertion,
        evidence: ValidationEvidence,
    ) -> AssertionResult:
        return self.get(assertion.assertion_type).evaluate(
            assertion,
            evidence,
        )
