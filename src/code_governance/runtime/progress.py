import hashlib
import json
from dataclasses import dataclass

from .action import OperationKind
from .execution import ExecutionResult, last_workspace_uncertainty_index


@dataclass(frozen=True)
class ProgressEvaluation:
    outcome: str
    evidence: str


class ProgressEvaluator:
    """Determines progress from Runtime facts without model inference."""

    def evaluate(self, action, result: ExecutionResult, state) -> ProgressEvaluation:
        if not result.success:
            history = list(zip(state.recent_actions[:-1], state.recent_observations))
            history = history[last_workspace_uncertainty_index(history) + 1 :]
            if (
                result.failure is not None
                and result.failure.category in {"INVALID_ACTION", "REPEATED_WRITE_WITHOUT_NEW_EVIDENCE"}
                and any(
                    prior_action.operation.kind is OperationKind.COMMAND_RUN
                    and prior_observation.success
                    and prior_observation.metadata.get("returncode") == 0
                    for prior_action, prior_observation in history
                )
            ):
                return ProgressEvaluation("NO_PROGRESS", "deterministic_rejection")
            return ProgressEvaluation("FAILURE", result.failure.category if result.failure else "execution_failed")
        signature = self._action_signature(action, state)
        history = list(zip(state.recent_actions[:-1], state.recent_observations))
        history = history[last_workspace_uncertainty_index(history) + 1 :]
        for index, (previous_action, previous_observation) in enumerate(history):
            if (
                action.operation.kind is OperationKind.COMMAND_RUN
                and previous_action.operation.kind is OperationKind.COMMAND_RUN
                and self._action_signature(
                    previous_action, state, previous_observation.metadata.get("workspace_revision_before")
                ) == signature
                and result.metadata.get("returncode") == 0
                and previous_observation.success
                and previous_observation.metadata.get("returncode") == 0
            ):
                return ProgressEvaluation("NO_PROGRESS", "repeated_successful_verification")
            if (
                self._action_signature(
                    previous_action, state, previous_observation.metadata.get("workspace_revision_before")
                ) == signature
                and previous_observation.metadata.get("result_fingerprint")
                == result.result_fingerprint
            ):
                has_new_evidence = any(
                    observation.success
                    and (
                        observation.metadata.get("information_gain")
                        or observation.metadata.get("state_changed")
                    )
                    for _, observation in history[index + 1 :]
                )
                if not has_new_evidence:
                    return ProgressEvaluation("NO_PROGRESS", "identical_action_and_result")
        if result.state_changed:
            return ProgressEvaluation("PROGRESS", "workspace_state_changed")
        if result.information_gain:
            return ProgressEvaluation("PROGRESS", "new_execution_information")
        return ProgressEvaluation("NO_PROGRESS", "no_state_change_or_information_gain")

    @staticmethod
    def _action_signature(action, state, revision=None) -> str:
        payload = {
            "operation": action.operation.kind.value,
            "fields": action.operation.fields,
            "workspace_revision": state.workspace_revision if revision is None else revision,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
