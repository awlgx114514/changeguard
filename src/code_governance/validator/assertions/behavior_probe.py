import json
import math

from code_governance.validator.assertion import AssertionResult, AssertionStatus, AssertionType, VerificationAssertion
from code_governance.validator.evidence import ValidationEvidence
from code_governance.verification.behavior_probe import (
    BehaviorProbeEvidenceError,
    parse_behavior_probe_result,
)


class BehaviorProbeEvaluator:
    """Interpret structured probe stdout already captured as verification evidence."""

    assertion_type = AssertionType.BEHAVIOR_PROBE

    def evaluate(self, assertion: VerificationAssertion, evidence: ValidationEvidence) -> AssertionResult:
        verification = next(
            (item for item in evidence.verification if item.claim_id == assertion.claim_id),
            None,
        )
        if verification is None:
            return self._result(
                assertion,
                AssertionStatus.UNSUPPORTED,
                (),
                "behavior_probe_evidence_invalid: referenced claim evidence is missing",
            )

        refs = (
            f"claim:{verification.claim_id}",
            f"request:{verification.request_id}",
            f"action:{verification.action_id}",
            f"execution_result:{verification.execution_result_id}",
        )
        if verification.execution_failed:
            return self._result(
                assertion,
                AssertionStatus.UNSUPPORTED,
                refs,
                "verification_execution_failed: probe command did not complete",
            )
        if verification.verification_failed:
            return self._result(
                assertion,
                AssertionStatus.UNSUPPORTED,
                refs,
                "verification_failed: probe command did not meet its declared exit code",
            )
        if verification.actual_result.get("returncode") != 0:
            return self._result(
                assertion,
                AssertionStatus.UNSUPPORTED,
                refs,
                "verification_failed: behavior probe must exit with code 0",
            )

        try:
            result = parse_behavior_probe_result(verification.execution_result)
        except BehaviorProbeEvidenceError as error:
            return self._result(assertion, AssertionStatus.UNSUPPORTED, refs, str(error))

        expected_probe_id = assertion.config.get("probe_id")
        expected = assertion.config.get("expected")
        if not isinstance(expected_probe_id, str) or not expected_probe_id.strip():
            return self._result(
                assertion,
                AssertionStatus.UNSUPPORTED,
                refs,
                "behavior_probe_evidence_invalid: assertion probe_id is missing",
            )
        if "expected" not in assertion.config:
            return self._result(
                assertion,
                AssertionStatus.UNSUPPORTED,
                refs,
                "behavior_probe_evidence_invalid: assertion expected value is missing",
            )
        if not _is_json_value(expected):
            return self._result(
                assertion,
                AssertionStatus.UNSUPPORTED,
                refs,
                "behavior_probe_evidence_invalid: assertion expected value is not JSON-compatible",
            )
        if result.probe_id != expected_probe_id:
            return self._result(
                assertion,
                AssertionStatus.UNSUPPORTED,
                refs,
                "behavior_probe_evidence_invalid: probe_id does not match assertion",
            )

        try:
            observed_json = json.dumps(result.observed, sort_keys=True, allow_nan=False)
            expected_json = json.dumps(expected, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as error:
            return self._result(
                assertion,
                AssertionStatus.UNSUPPORTED,
                refs,
                f"behavior_probe_evidence_invalid: expected value is not JSON-compatible ({error})",
            )
        status = (
            AssertionStatus.PASSED
            if observed_json == expected_json
            else AssertionStatus.FAILED
        )
        return self._result(
            assertion,
            status,
            refs,
            f"Probe {result.probe_id} observed {result.observed!r}; expected {expected!r}",
        )

    @staticmethod
    def _result(assertion, status, refs, message):
        return AssertionResult(
            assertion.id,
            assertion.change_id,
            assertion.assertion_type,
            status,
            refs,
            message,
        )


def _is_json_value(value) -> bool:
    if value is None or type(value) in {str, bool, int}:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_is_json_value(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _is_json_value(item) for key, item in value.items())
    return False
