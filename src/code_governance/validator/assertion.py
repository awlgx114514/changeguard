"""Public Assertion models and results for deterministic validation."""

from dataclasses import dataclass
from enum import Enum
from typing import Tuple

from code_governance.contract.assertion import AssertionType, VerificationAssertion


class AssertionStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class AssertionResult:
    assertion_id: str
    change_id: str
    assertion_type: AssertionType
    status: AssertionStatus
    evidence_refs: Tuple[str, ...]
    message: str


__all__ = [
    "AssertionResult",
    "AssertionStatus",
    "AssertionType",
    "VerificationAssertion",
]
