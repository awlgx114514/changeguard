from dataclasses import dataclass
from typing import Optional, Protocol, Sequence, Tuple

from code_governance.contract.schema import ChangeContract
from code_governance.git.context import ChangeContext
from code_governance.validator.evidence import ValidationEvidence
from .assertion import AssertionResult, AssertionStatus


@dataclass(frozen=True)
class Finding:
    rule: str
    code: str
    message: str
    path: Optional[str] = None
    severity: str = "warning"


@dataclass(frozen=True)
class ValidationResult:
    findings: Tuple[Finding, ...]
    assertion_results: Tuple[AssertionResult, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.findings

    @property
    def status(self) -> str:
        if not self.findings:
            return "PASS"
        if any(finding.severity == "error" for finding in self.findings):
            return "FAIL"
        return "WARNING"


class Rule(Protocol):
    name: str

    def check(self, contract: ChangeContract, context: ChangeContext) -> Tuple[Finding, ...]:
        """Return deterministic findings without changing either input."""


class ValidatorEngine:
    def __init__(self, rules: Optional[Sequence[Rule]] = None):
        if rules is None:
            from .rules import DiffSizeRule, ForbiddenFileRule, UnexpectedScopeChangeRule

            rules = (UnexpectedScopeChangeRule(), ForbiddenFileRule(), DiffSizeRule())
        self._rules = tuple(rules)

    def validate(
        self,
        contract: ChangeContract,
        evidence: ValidationEvidence,
        assertion_results: Optional[Sequence[AssertionResult]] = None,
    ) -> ValidationResult:
        """Validate Contract claims against one coherent set of Runtime facts."""
        findings = []
        assertion_results = tuple(assertion_results or ())
        context = evidence.change_context
        for rule in self._rules:
            findings.extend(rule.check(contract, context))
        if not evidence.workspace_is_consistent:
            findings.append(
                Finding(
                    rule="workspace_identity",
                    code="workspace_revision_mismatch",
                    message="Workspace changed while verification evidence was collected",
                    severity="error",
                )
            )
        for verification in evidence.verification:
            if verification.execution_failed:
                findings.append(
                    Finding(
                        rule="verification_execution",
                        code="verification_execution_failed",
                        message=(
                            f"Verification claim {verification.claim_id} could not execute"
                        ),
                        severity="error",
                    )
                )
            elif verification.verification_failed:
                findings.append(
                    Finding(
                        rule="verification",
                        code="verification_failed",
                        message=(
                            f"Verification claim {verification.claim_id} did not meet "
                            "its expected result"
                        ),
                        severity="error",
                    )
                )
        findings.extend(self._expected_change_evidence_findings(contract, evidence))
        assertion_results = self._apply_assertion_results(
            contract,
            evidence,
            assertion_results,
            findings,
        )
        return ValidationResult(tuple(findings), assertion_results)

    @staticmethod
    def _apply_assertion_results(contract, evidence, assertion_results, findings):
        definitions = {
            assertion.id: assertion
            for assertion in contract.verification.assertions
        }
        results_by_id = {result.assertion_id: result for result in assertion_results}
        failed_claim_ids = {
            item.claim_id
            for item in evidence.verification
            if item.execution_failed or item.verification_failed
        }
        completed_results = list(assertion_results)
        for assertion in contract.verification.assertions:
            result = results_by_id.get(assertion.id)
            change = next(
                (item for item in contract.changes if item.id == assertion.change_id),
                None,
            )
            if result is None:
                result = AssertionResult(
                    assertion_id=assertion.id,
                    change_id=assertion.change_id,
                    assertion_type=assertion.assertion_type,
                    status=AssertionStatus.UNSUPPORTED,
                    evidence_refs=(),
                    message="No AssertionResult was supplied for this declared assertion",
                )
                completed_results.append(result)
            if change is None or change.requirement != "required":
                continue
            # Existing verification findings own the first failure boundary for
            # assertions derived from that same claim.
            if assertion.claim_id is not None and assertion.claim_id in failed_claim_ids:
                continue
            if result.status is AssertionStatus.FAILED:
                findings.append(
                    Finding(
                        rule="assertion",
                        code="ASSERTION_FAILED",
                        message=(f"Assertion {assertion.id} failed for change "
                                 f"{assertion.change_id}: {result.message}"),
                        severity="error",
                    )
                )
            elif result.status is AssertionStatus.UNSUPPORTED:
                findings.append(
                    Finding(
                        rule="assertion",
                        code="ASSERTION_UNSUPPORTED",
                        message=(f"Assertion {assertion.id} is unsupported for change "
                                 f"{assertion.change_id}: {result.message}"),
                        severity="error",
                    )
                )
        return tuple(completed_results)

    @staticmethod
    def _expected_change_evidence_findings(
        contract: ChangeContract, evidence: ValidationEvidence
    ) -> Tuple[Finding, ...]:
        """Ensure required declared outcomes retain a concrete evidence link.

        Whether a command is a meaningful test remains a contract-authoring
        responsibility.  This only checks the declared direct relation against
        the facts actually collected by the canonical verification execution.
        A failed execution keeps its existing ``verification_*`` attribution.
        """
        collected_claim_ids = {item.claim_id for item in evidence.verification}
        findings = []
        for change in contract.changes:
            if change.requirement != "required":
                continue
            direct_claim_ids = {
                coverage.claim_id
                for coverage in contract.verification.coverage
                if coverage.change_id == change.id
                and coverage.coverage_type == "direct"
            }
            if direct_claim_ids and not direct_claim_ids.issubset(collected_claim_ids):
                missing_ids = sorted(direct_claim_ids.difference(collected_claim_ids))
                findings.append(
                    Finding(
                        rule="expected_change_coverage",
                        code="expected_change_evidence_missing",
                        message=(
                            f"Required expected change {change.id} has no collected "
                            f"verification evidence for claim(s): {', '.join(missing_ids)}"
                        ),
                        severity="error",
                    )
                )
        return tuple(findings)
