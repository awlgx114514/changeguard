from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Observation:
    """The factual result of one Runtime Action."""

    action_id: str
    type: str = "action_result"
    success: bool = False
    summary: str = ""
    artifact_reference: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    result_type: str = "action_result"
    error_category: Optional[str] = None

    @classmethod
    def from_execution(cls, execution, observation_type: str) -> "Observation":
        """Create the bounded model-facing view from an ExecutionResult."""
        metadata = {
            "canonical_target": execution.canonical_target,
            "effect_type": execution.effect_type,
            "state_changed": execution.state_changed,
            "information_gain": execution.information_gain,
            "result_fingerprint": execution.result_fingerprint,
            **execution.metadata,
        }
        if execution.failure is not None:
            metadata["failure_stage"] = execution.failure.stage
            metadata["failure_category"] = execution.failure.category
        return cls(
            action_id=execution.action_id,
            type=observation_type,
            success=execution.success,
            summary=execution.summary,
            artifact_reference=execution.artifact_reference,
            metadata=metadata,
            error=None if execution.success else execution.summary,
            result_type="execution_result",
            error_category=None if execution.failure is None else execution.failure.category,
        )

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "type": self.type,
            "success": self.success,
            "summary": self.summary,
            "artifact_reference": self.artifact_reference,
            "metadata": dict(self.metadata),
            "error": self.error,
            "result_type": self.result_type,
            "error_category": self.error_category,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "Observation":
        return cls(
            action_id=str(value["action_id"]),
            type=str(value.get("type", "action_result")),
            success=bool(value.get("success", False)),
            summary=str(value.get("summary", "")),
            artifact_reference=value.get("artifact_reference"),
            metadata=_metadata(value),
            error=value.get("error"),
            result_type=str(value.get("result_type", value.get("type", "action_result"))),
            error_category=value.get("error_category"),
        )


def _metadata(value: dict) -> Dict[str, Any]:
    if "metadata" in value:
        return dict(value["metadata"])
    return {
        f"{key}_length" if isinstance(item, str) else key: len(item) if isinstance(item, str) else item
        for key, item in dict(value.get("result", {})).items()
    }
