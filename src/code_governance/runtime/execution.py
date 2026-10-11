from dataclasses import dataclass, field
import hashlib
import locale
import subprocess
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class FailureRecord:
    """Privacy-safe failure attribution at the first Runtime boundary."""

    stage: str
    category: str
    action_id: str = ""
    tool: str = ""
    retryable: Optional[bool] = None
    detail: str = ""
    error_type: str = ""
    error_hash: str = ""

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "category": self.category,
            "action_id": self.action_id,
            "tool": self.tool,
            "retryable": self.retryable,
            "detail": self.detail,
            "error_type": self.error_type,
            "error_hash": self.error_hash,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "FailureRecord":
        return cls(
            stage=str(value["stage"]),
            category=str(value["category"]),
            action_id=str(value.get("action_id", "")),
            tool=str(value.get("tool", "")),
            retryable=value.get("retryable"),
            detail=str(value.get("detail", "")),
            error_type=str(value.get("error_type", "")),
            error_hash=str(value.get("error_hash", "")),
        )


@dataclass(frozen=True)
class ExecutionResult:
    """The authoritative bounded Runtime outcome for one Action."""

    action_id: str
    status: str
    canonical_target: str = ""
    effect_type: str = "NONE"
    state_changed: bool = False
    information_gain: bool = False
    result_fingerprint: str = ""
    summary: str = ""
    artifact_reference: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    failure: Optional[FailureRecord] = None

    @property
    def success(self) -> bool:
        return self.status == "SUCCESS"

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "status": self.status,
            "canonical_target": self.canonical_target,
            "effect_type": self.effect_type,
            "state_changed": self.state_changed,
            "information_gain": self.information_gain,
            "result_fingerprint": self.result_fingerprint,
            "summary": self.summary,
            "artifact_reference": self.artifact_reference,
            "metadata": dict(self.metadata),
            "failure": None if self.failure is None else self.failure.to_dict(),
        }


def strict_bool(metadata: dict, key: str) -> Optional[bool]:
    """Read a semantic flag without accepting truthy non-boolean values."""
    value = metadata.get(key)
    return value if type(value) is bool else None


def effect_uncertain(metadata: dict) -> Optional[bool]:
    return strict_bool(metadata, "effect_uncertain")


def output_partial(metadata: dict) -> Optional[bool]:
    return strict_bool(metadata, "output_partial")


def workspace_uncertainty_transition(action, observation) -> bool:
    """Whether one historical result invalidates earlier workspace evidence.

    Explicit canonical facts win. Missing legacy facts are inferred only for
    failed effectful operations; admission failures and read-only failures do
    not create a barrier.
    """
    metadata = getattr(observation, "metadata", {}) or {}
    explicit = effect_uncertain(metadata)
    if explicit is not None:
        return explicit
    if getattr(observation, "success", False):
        return False
    failure_stage = str(metadata.get("failure_stage", ""))
    failure_category = str(metadata.get("failure_category", ""))
    if failure_stage in {
        "ACTION_VALIDATION", "TARGET_RESOLUTION", "PRE_EXECUTION_GOVERNANCE",
    } or failure_category in {
        "INVALID_ACTION", "TARGET_REJECTED", "POLICY_DENY",
        "APPROVAL_REJECTED", "APPROVAL_UNAVAILABLE", "HARD_GUARD_DENY",
    }:
        return False
    kind = getattr(getattr(action, "operation", None), "kind", None)
    return getattr(kind, "value", "") in {
        "command.run", "file.replace", "file.write", "file.create",
    } and str(getattr(observation, "error_category", "") or failure_category) not in {
        "INVALID_ACTION", "TARGET_REJECTED", "POLICY_DENY",
        "APPROVAL_REJECTED", "APPROVAL_UNAVAILABLE", "HARD_GUARD_DENY",
    }


def last_workspace_uncertainty_index(history) -> int:
    """Return the latest result index that forms an uncertainty barrier."""
    return max(
        (index for index, (action, observation) in enumerate(history)
         if workspace_uncertainty_transition(action, observation)),
        default=-1,
    )


def timeout_streams(error: BaseException) -> Optional[tuple[str, str]]:
    """Extract only direct TimeoutExpired output (or one direct cause)."""
    if isinstance(error, subprocess.TimeoutExpired):
        timeout = error
    elif isinstance(error, TimeoutError) and isinstance(error.__cause__, subprocess.TimeoutExpired):
        timeout = error.__cause__
    else:
        return None
    stdout = timeout.stdout if timeout.stdout is not None else timeout.output
    stderr = timeout.stderr
    encoding = locale.getpreferredencoding(False) or "utf-8"

    def normalize(value):
        if value is None:
            return ""
        if isinstance(value, bytes):
            text = value.decode(encoding, errors="replace")
            return text.replace("\r\n", "\n").replace("\r", "\n")
        if isinstance(value, str):
            return value.replace("\r\n", "\n").replace("\r", "\n")
        return ""

    return normalize(stdout), normalize(stderr)


def stream_metadata(stdout: str, stderr: str) -> dict:
    """Create bounded, separately addressable stream previews and fingerprints."""
    result = {}
    for name, text in (("stdout", stdout), ("stderr", stderr)):
        result.update({
            f"{name}_length": len(text),
            f"{name}_fingerprint": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            f"{name}_preview": text[:4096],
            f"{name}_truncated": len(text) > 4096,
        })
    return result


def evidence_qualifiers(metadata: dict) -> tuple[str, ...]:
    """Bounded, factual qualifiers shared by observation/history rendering."""
    qualifiers = []
    if effect_uncertain(metadata) is True:
        if strict_bool(metadata, "state_changed") is True:
            qualifiers.append("Effect uncertainty: a partial change is confirmed, but final effect and scope are unknown.")
        else:
            qualifiers.append(
                "Effect uncertainty: the operation may have had an effect; final effect is unknown and no change is confirmed."
            )
    if output_partial(metadata) is True:
        qualifiers.append("Output partial: captured output may be incomplete.")
    if strict_bool(metadata, "truncated") is True:
        qualifiers.append("Evidence truncated: infrastructure omitted part of model-visible evidence.")
    return tuple(qualifiers)
