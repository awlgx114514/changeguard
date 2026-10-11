"""Internal structured requests for system-initiated verification execution."""

from dataclasses import dataclass
from enum import Enum
from typing import Tuple

from code_governance.runtime.action import Action, OperationKind


class ExecutionOrigin(str, Enum):
    SYSTEM_VERIFICATION = "SYSTEM_VERIFICATION"


@dataclass(frozen=True)
class VerificationRequest:
    id: str
    claim_id: str
    argv: Tuple[str, ...]
    origin: ExecutionOrigin = ExecutionOrigin.SYSTEM_VERIFICATION
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.argv, tuple):
            raise TypeError("argv must be a tuple of arguments")
        if any(not isinstance(item, str) or not item for item in self.argv):
            raise TypeError("argv must contain non-empty string arguments")


def verification_action(request: VerificationRequest) -> Action:
    """Create the exact command Action declared by a structured verification request."""
    return Action(
        id=request.id,
        operation=Action.create_operation(
            OperationKind.COMMAND_RUN,
            argv=request.argv,
        ).operation,
        reason=request.reason,
    )
