"""Bounded repair projections of existing validation facts, never instructions."""

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Optional, Tuple

from code_governance.contract.assertion import AssertionType
from code_governance.verification.behavior_probe import BehaviorProbeEvidenceError, parse_behavior_probe_result

from .assertion import AssertionStatus
from .evidence import WorkspaceIdentity


@dataclass(frozen=True)
class RepairFailureContext:
    failure_id: str
    change_id: str
    failure_boundary: str
    assertion_id: Optional[str]
    claim_id: Optional[str]
    expected: Any
    observed: Any
    evidence_refs: Tuple[str, ...]
    changed_files: Tuple[str, ...]
    workspace_identity: WorkspaceIdentity
    changed_files_omitted: int = 0

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["evidence_refs"] = list(self.evidence_refs)
        payload["changed_files"] = list(self.changed_files)
        return payload


def _bounded(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(encoded) > 2048:
        return {"truncated": True, "value_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest()}
    # Do not retain mutable Contract/probe payloads by reference.
    return json.loads(encoded)


def _references(verification):
    if verification is None:
        return ()
    return (f"claim:{verification.claim_id}", f"request:{verification.request_id}",
            f"action:{verification.action_id}", f"execution_result:{verification.execution_result_id}")


def repair_failure_identity(attempt_id, workspace_fingerprint, change_id, boundary, assertion_id, claim_id):
    identity = json.dumps(
        (attempt_id, workspace_fingerprint, change_id, boundary, assertion_id, claim_id),
        ensure_ascii=False,
    )
    return "validation_failure:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def build_repair_contexts(contract, evidence, validation, *, attempt_id=""):
    """Project only genuine assertion/claim failures from a coherent workspace.

Lifecycle, unsupported evidence, and mixed earlier-boundary failures are not
coding repair opportunities. Identity uses the validation attempt because
verification Action ids intentionally remain stable across repeated requests.
"""
    errors = {finding.code for finding in validation.findings if finding.severity == "error"}
    if (validation.status != "FAIL" or not evidence.workspace_is_consistent
            or not errors or not errors.issubset({"ASSERTION_FAILED", "verification_failed"})
            or any(item.execution_failed for item in evidence.verification)):
        return ()
    assertions = {item.id: item for item in contract.verification.assertions}
    results = {item.assertion_id: item for item in validation.assertion_results}
    verifications = {item.claim_id: item for item in evidence.verification}
    required = {change.id for change in contract.changes if change.requirement == "required"}
    failures = []

    def append(change_id, boundary, assertion_id, claim_id, expected, observed, refs):
        # IDs are references, not prose. Do not silently truncate their identity.
        if any(value is not None and len(value) > 512 for value in (change_id, assertion_id, claim_id)):
            return
        changed_files = evidence.change_context.changed_files
        failure = RepairFailureContext(
            failure_id=repair_failure_identity(
                attempt_id, evidence.workspace.fingerprint_after, change_id,
                boundary, assertion_id, claim_id,
            ),
            change_id=change_id, failure_boundary=boundary, assertion_id=assertion_id, claim_id=claim_id,
            expected=_bounded(expected), observed=_bounded(observed), evidence_refs=tuple(dict.fromkeys(refs)),
            changed_files=tuple(changed_files[:20]),
            workspace_identity=evidence.workspace,
            changed_files_omitted=max(0, len(changed_files) - 20),
        )
        # Reference identities must remain exact. Abstain rather than invent a
        # shortened path/workspace/ref when the whole projection cannot fit.
        if len(json.dumps(failure.to_dict(), ensure_ascii=False).encode("utf-8")) <= 8192:
            failures.append(failure)

    if "verification_failed" in errors:
        for verification in evidence.verification:
            if not verification.verification_failed:
                continue
            for coverage in contract.verification.coverage:
                if coverage.claim_id != verification.claim_id or coverage.change_id not in required:
                    continue
                assertion = next((item for item in assertions.values()
                                  if item.claim_id == verification.claim_id and item.change_id == coverage.change_id), None)
                result = results.get(assertion.id) if assertion is not None else None
                observed = dict(verification.actual_result)
                if result is not None:
                    observed["assertion_status"] = result.status.value
                # A short diagnostic excerpt is not another stdout/evidence store.
                excerpt = verification.execution_result.metadata.get("stdout_preview") or verification.execution_result.metadata.get("stderr_preview")
                if excerpt:
                    observed["diagnostic_excerpt"] = str(excerpt)[-512:]
                append(coverage.change_id, "VERIFICATION", assertion.id if assertion else None,
                       verification.claim_id, dict(verification.expected_result), observed, _references(verification))

    if "ASSERTION_FAILED" in errors:
        for assertion_id, result in results.items():
            assertion = assertions.get(assertion_id)
            if assertion is None or result.status is not AssertionStatus.FAILED or assertion.change_id not in required:
                continue
            verification = verifications.get(assertion.claim_id)
            # The earlier claim failure, if any, already owns this boundary.
            if verification is not None and not verification.passed:
                continue
            expected = dict(assertion.config)
            observed = {"assertion_status": result.status.value, "detail": result.message[:512]}
            if assertion.assertion_type is AssertionType.BEHAVIOR_PROBE:
                if verification is None:
                    continue
                try:
                    observed = parse_behavior_probe_result(verification.execution_result).observed
                except BehaviorProbeEvidenceError:
                    continue
                expected = assertion.config["expected"]
            elif assertion.assertion_type is AssertionType.EXIT_CODE:
                expected = {"exit_code": assertion.config["expected_exit_code"]}
                observed = dict(verification.actual_result) if verification else observed
            append(assertion.change_id, "ASSERTION", assertion_id, assertion.claim_id, expected, observed,
                   result.evidence_refs + _references(verification) + (f"assertion:{assertion_id}",))
    return tuple(failures[:10])
