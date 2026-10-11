"""Canonical execution of one tool-backed Action."""

import hashlib
from pathlib import Path

from .action import Action, operation_route
from .action_validator import ActionValidationError, ActionValidator
from .execution import ExecutionResult, FailureRecord
from .target import TargetResolutionError, TargetResolver


class CanonicalActionExecutor:
    """Validate, resolve and dispatch one tool-backed Action without Runtime state."""

    def __init__(
        self,
        tool_registry,
        *,
        action_validator: ActionValidator = None,
        target_resolver: TargetResolver = None,
    ):
        self._tool_registry = tool_registry
        self._action_validator = action_validator or ActionValidator()
        self._target_resolver = target_resolver or TargetResolver()

    def prepare(self, action: Action, workspace: Path) -> Action:
        """Validate and resolve a tool-backed Action before stateful Runtime checks."""
        self._action_validator.validate(action)
        return self._target_resolver.resolve(action, workspace)

    def execute_prepared(self, action: Action, workspace: Path) -> ExecutionResult:
        """Dispatch an already validated and resolved Action through the Registry."""
        if operation_route(action.operation.kind) is None:
            return self.failure_result(
                action,
                ValueError(f"operation is not tool-backed: {action.operation.kind.value}"),
            )
        return self._tool_registry.execute(action, workspace)

    def execute(self, action: Action, workspace: Path) -> ExecutionResult:
        """Run the complete stateless canonical Action path."""
        try:
            prepared = self.prepare(action, workspace)
        except Exception as error:
            return self.failure_result(action, error)
        return self.execute_prepared(prepared, workspace)

    @staticmethod
    def failure_result(action: Action, error: Exception) -> ExecutionResult:
        if isinstance(error, TargetResolutionError):
            stage, category = "TARGET_RESOLUTION", "TARGET_REJECTED"
        elif isinstance(error, ActionValidationError):
            stage, category = "ACTION_VALIDATION", "INVALID_ACTION"
        else:
            stage, category = "RUNTIME", "UNSUPPORTED_ACTION"
        detail = str(error)[:512]
        failure = FailureRecord(
            stage=stage,
            category=category,
            action_id=action.id,
            detail=detail,
            error_type=type(error).__name__,
            error_hash=hashlib.sha256(detail.encode("utf-8")).hexdigest(),
        )
        return ExecutionResult(
            action_id=action.id,
            status="FAILURE",
            canonical_target="",
            result_fingerprint=failure.error_hash,
            summary=failure.detail,
            metadata={
                "failure_stage": failure.stage,
                "failure_category": failure.category,
                "effect_uncertain": False,
                "output_partial": False,
            },
            failure=failure,
        )
