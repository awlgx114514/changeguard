import json

import pytest

from code_governance.runtime.execution import ExecutionResult
from code_governance.verification.behavior_probe import (
    BEHAVIOR_PROBE_PROTOCOL,
    BehaviorProbeEvidenceError,
    parse_behavior_probe_result,
)


def _execution(stdout, *, truncated=False, length=None):
    return ExecutionResult(
        action_id="verification:empty_password_probe",
        status="SUCCESS",
        metadata={
            "returncode": 0,
            "stdout_preview": stdout,
            "stdout_length": len(stdout) if length is None else length,
            "stdout_fingerprint": "sha256:test",
            "stdout_truncated": truncated,
        },
    )


def _payload(**updates):
    value = {
        "protocol": BEHAVIOR_PROBE_PROTOCOL,
        "probe_id": "empty_password_probe",
        "observed": {"accepted": False},
    }
    value.update(updates)
    return json.dumps(value)


def test_parser_accepts_protocol_object_after_logs():
    result = parse_behavior_probe_result(
        _execution(f"starting probe\n{_payload()}\n")
    )

    assert result.protocol == "changeguard.behavior_probe.v1"
    assert result.probe_id == "empty_password_probe"
    assert result.observed == {"accepted": False}


@pytest.mark.parametrize(
    "stdout",
    [
        "{malformed}",
        _payload(protocol="changeguard.behavior_probe.v2"),
        _payload(probe_id=" "),
        '{"protocol":"changeguard.behavior_probe.v1","probe_id":"p"}',
        '{"protocol":"changeguard.behavior_probe.v1","probe_id":"p","observed":NaN}',
    ],
)
def test_parser_rejects_invalid_protocol_evidence(stdout):
    with pytest.raises(BehaviorProbeEvidenceError, match="behavior_probe_evidence_invalid"):
        parse_behavior_probe_result(_execution(stdout))


def test_parser_rejects_truncated_stdout():
    stdout = _payload()

    with pytest.raises(BehaviorProbeEvidenceError, match="stdout_truncated"):
        parse_behavior_probe_result(
            _execution(stdout, truncated=True, length=len(stdout) + 10)
        )
