from dataclasses import dataclass
import json
from typing import Any, Optional, Tuple

from code_governance.contract.schema import ChangeContract
from code_governance.validator.core import Finding, ValidationResult
from code_governance.validator.assertion import AssertionResult
from code_governance.validator.evidence import ValidationEvidence
from code_governance.verification.runner import VerificationSummary


@dataclass(frozen=True)
class Evidence:
    changed_files: Tuple[str, ...]
    additions: int
    deletions: int
    file_statuses: Tuple[Tuple[str, str], ...]


@dataclass(frozen=True)
class ChangeReport:
    """A display projection of already-decided validation facts.

    ``status`` and ``findings`` are copied from ``ValidationResult``.  This
    type must not add findings or decide whether verification succeeded.
    """

    status: str
    findings: Tuple[Finding, ...]
    evidence: Evidence
    verification: Optional[VerificationSummary] = None
    validation_evidence: Optional[ValidationEvidence] = None
    assertion_definitions: Tuple[Any, ...] = ()
    assertion_results: Tuple[AssertionResult, ...] = ()


def build_report(
    validation_evidence: ValidationEvidence,
    result: ValidationResult,
    *,
    contract: Optional[ChangeContract] = None,
) -> ChangeReport:
    """Build a report without interpreting evidence or changing validation."""
    context = validation_evidence.change_context
    evidence = Evidence(
        changed_files=context.changed_files,
        additions=context.additions,
        deletions=context.deletions,
        file_statuses=tuple((change.path, change.status) for change in context.files),
    )
    return ChangeReport(
        status=result.status,
        findings=result.findings,
        evidence=evidence,
        verification=validation_evidence.verification_summary,
        validation_evidence=validation_evidence,
        assertion_definitions=(
            () if contract is None else tuple(contract.verification.assertions)
        ),
        assertion_results=result.assertion_results,
    )


def render_report(report: ChangeReport) -> str:
    lines = [f"Status: {report.status}", "Findings:"]
    if report.findings:
        for finding in report.findings:
            location = f" ({finding.path})" if finding.path else ""
            lines.append(f"- [{finding.severity}] {finding.code}{location}: {finding.message}")
    else:
        lines.append("- none")

    lines.extend(
        [
            "Evidence:",
            f"Changed files: {', '.join(report.evidence.changed_files) or 'none'}",
            f"Additions: {report.evidence.additions}",
            f"Deletions: {report.evidence.deletions}",
        ]
    )
    if report.verification is not None:
        lines.append("Verification:")
        for command in report.verification.commands:
            state = "PASS" if command.passed else "FAIL"
            lines.append(f"- [{state}] {command.command} (exit: {command.returncode})")
    return "\n".join(lines)


def report_to_dict(report: ChangeReport) -> dict:
    payload: dict[str, Any] = {
        "status": report.status,
        "findings": [
            {
                "rule": finding.rule,
                "code": finding.code,
                "message": finding.message,
                "path": finding.path,
                "severity": finding.severity,
            }
            for finding in report.findings
        ],
        "evidence": {
            "changed_files": list(report.evidence.changed_files),
            "additions": report.evidence.additions,
            "deletions": report.evidence.deletions,
            "file_statuses": [
                {"path": path, "status": status}
                for path, status in report.evidence.file_statuses
            ],
        },
        "verification": None,
        "assertions": {
            "definitions": [
                {
                    "assertion_id": assertion.id,
                    "change_id": assertion.change_id,
                    "claim_id": assertion.claim_id,
                    "assertion_type": assertion.assertion_type.value,
                    "config": assertion.config,
                }
                for assertion in report.assertion_definitions
            ],
            "results": [
                {
                    "assertion_id": result.assertion_id,
                    "change_id": result.change_id,
                    "assertion_type": result.assertion_type.value,
                    "status": result.status.value,
                    "evidence_refs": list(result.evidence_refs),
                    "message": result.message,
                }
                for result in report.assertion_results
            ],
        },
    }
    if report.verification is not None:
        payload["verification"] = {
            "all_passed": report.verification.all_passed,
            "commands": [
                {
                    "command": command.command,
                    "passed": command.passed,
                    "returncode": command.returncode,
                    "stdout": command.stdout,
                    "stderr": command.stderr,
                    "timed_out": command.timed_out,
                }
                for command in report.verification.commands
            ],
        }
    return payload


def render_json(report: ChangeReport) -> str:
    return json.dumps(report_to_dict(report), ensure_ascii=False, indent=2)
