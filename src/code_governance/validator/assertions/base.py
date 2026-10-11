"""Shared protocol for pure assertion evaluators."""

from typing import Protocol

from code_governance.validator.assertion import AssertionResult, AssertionType, VerificationAssertion
from code_governance.validator.evidence import ValidationEvidence


class AssertionEvaluator(Protocol):
    assertion_type: AssertionType

    def evaluate(
        self,
        assertion: VerificationAssertion,
        evidence: ValidationEvidence,
    ) -> AssertionResult:
        """Interpret supplied evidence without executing anything."""
