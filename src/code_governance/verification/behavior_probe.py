"""Protocol for behavior facts emitted by ordinary verification commands."""

from dataclasses import dataclass
import json
from typing import Any

from code_governance.runtime.execution import ExecutionResult


BEHAVIOR_PROBE_PROTOCOL = "changeguard.behavior_probe.v1"


class BehaviorProbeEvidenceError(ValueError):
    """The bounded stdout fact does not contain one valid probe result."""


@dataclass(frozen=True)
class BehaviorProbeResult:
    protocol: str
    probe_id: str
    observed: Any


def parse_behavior_probe_result(execution_result: ExecutionResult) -> BehaviorProbeResult:
    """Parse the last non-empty stdout line without performing any execution."""
    metadata = execution_result.metadata
    stdout = metadata.get("stdout_preview")
    truncated = metadata.get("stdout_truncated")
    stdout_length = metadata.get("stdout_length")
    if not isinstance(stdout, str):
        raise _invalid("stdout_preview_missing")
    if truncated is not False:
        raise _invalid("stdout_truncated")
    if type(stdout_length) is not int or stdout_length != len(stdout):
        raise _invalid("stdout_length_mismatch")

    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise _invalid("stdout_has_no_result_line")
    try:
        payload = json.loads(lines[-1], parse_constant=_reject_non_json_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise _invalid("last_non_empty_line_is_not_json") from error
    if not isinstance(payload, dict):
        raise _invalid("result_must_be_object")
    if set(payload) != {"protocol", "probe_id", "observed"}:
        raise _invalid("result_fields_invalid")
    if payload["protocol"] != BEHAVIOR_PROBE_PROTOCOL:
        raise _invalid("protocol_mismatch")
    probe_id = payload["probe_id"]
    if not isinstance(probe_id, str) or not probe_id.strip():
        raise _invalid("probe_id_missing")
    return BehaviorProbeResult(
        protocol=payload["protocol"],
        probe_id=probe_id,
        observed=payload["observed"],
    )


def _reject_non_json_constant(value: str):
    raise ValueError(f"non-JSON constant: {value}")


def _invalid(reason: str) -> BehaviorProbeEvidenceError:
    return BehaviorProbeEvidenceError(f"behavior_probe_evidence_invalid: {reason}")
