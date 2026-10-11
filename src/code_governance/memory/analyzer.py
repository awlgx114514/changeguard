from .models import FailureAnalysis, FailureRecord


class FailureAnalyzer:
    """Produces deterministic explanations for Validator failures."""

    def analyze(self, record: FailureRecord) -> FailureAnalysis:
        finding = record.validator_findings[0] if record.validator_findings else {}
        code = finding.get("code", "unknown")
        path = finding.get("path") or "repository"
        if code == "unexpected_file":
            return FailureAnalysis(
                failure_type="scope_violation",
                symptom=f"Agent changed {path} outside the expected file scope",
                root_cause="implementation exceeded contract scope",
                reason=f"{path} was not listed in Contract expected_files",
                prevention="Clarify expected files and Contract constraints before execution.",
                confidence=0.8,
            )
        if code == "forbidden_file":
            return FailureAnalysis(
                failure_type="forbidden_change",
                symptom=f"Agent changed forbidden file {path}",
                root_cause="implementation violated an explicit contract constraint",
                reason=f"{path} was listed in Contract forbidden files",
                prevention="Keep the forbidden file out of the execution plan and repair scope.",
                confidence=0.9,
            )
        if code == "diff_size_exceeded":
            return FailureAnalysis(
                failure_type="diff_expansion",
                symptom="Agent change exceeded the Contract diff limit",
                root_cause="implementation scope was larger than the requested change level",
                reason=finding.get("message", "diff size exceeded"),
                prevention="Split the task or reduce planned files and changed lines.",
                confidence=0.7,
            )
        return FailureAnalysis(
            failure_type="verification_failure",
            symptom=finding.get("message", "Validator reported a failure"),
            root_cause="execution did not satisfy a deterministic governance check",
            reason=finding.get("code", "unknown"),
            prevention="Review the reported finding before retrying execution.",
            confidence=0.5,
        )
