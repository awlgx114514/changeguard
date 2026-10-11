"""One-shot, fail-closed approval contracts for governed Tool execution."""

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol


class ApprovalOutcome(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class ApprovalRequest:
    action_id: str
    tool_name: str
    canonical_target: str
    effect: str
    reason: str


class ApprovalProvider(Protocol):
    def request(self, request: ApprovalRequest) -> ApprovalOutcome:
        """Resolve exactly one execution request."""


class ConsoleApprovalProvider:
    """TTY-only, per-operation approval; callers remain fail-closed on input errors."""

    def __init__(self, input_fn: Callable[[str], str] = input, output_fn: Callable[[str], None] = print):
        self._input = input_fn
        self._output = output_fn

    def request(self, request: ApprovalRequest) -> ApprovalOutcome:
        try:
            self._output(
                f"Approval required: operation={request.effect}; target={request.canonical_target}; tool={request.tool_name}"
            )
            answer = self._input("Approve this operation? [y/N] ").strip().lower()
        except Exception:
            return ApprovalOutcome.UNAVAILABLE
        return ApprovalOutcome.APPROVED if answer in {"y", "yes"} else ApprovalOutcome.REJECTED
