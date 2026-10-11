"""Deterministic pre-runtime validation for Change Contracts.

This module checks whether a Contract is actionable.  It intentionally does
not decide whether a completed workspace fulfills the Contract; that remains
the responsibility of ``ValidatorEngine`` and verification evidence.
"""

from dataclasses import dataclass
import math
from pathlib import PurePosixPath
from typing import Tuple

from .assertion import AssertionType
from .schema import ChangeContract


@dataclass(frozen=True)
class ContractFinding:
    code: str
    message: str
    severity: str = "error"


@dataclass(frozen=True)
class ContractValidationResult:
    findings: Tuple[ContractFinding, ...]

    @property
    def is_valid(self) -> bool:
        return not any(finding.severity == "error" for finding in self.findings)


class ContractValidator:
    """Reject Contracts that cannot safely enter the canonical runtime."""

    _VAGUE_VERIFICATION = {
        "make sure it works",
        "verify behavior",
        "verify it works",
        "test it",
    }
    _BROAD_SCOPE = {".", "*", "repository", "whole repository"}

    def validate(self, contract: ChangeContract) -> ContractValidationResult:
        findings = []
        if contract.needs_clarification:
            findings.append(
                ContractFinding(
                    "clarification_required",
                    "Contract generation requires clarification before execution.",
                )
            )
        if not contract.goal.strip():
            findings.append(ContractFinding("goal_required", "Contract goal is required."))
        if not contract.scope.paths and not contract.scope.modules and not contract.scope.description.strip():
            findings.append(ContractFinding("scope_required", "Contract scope is required."))
        changes = tuple(contract.changes)
        if not any(change.description.strip() for change in changes):
            findings.append(
                ContractFinding(
                    "changes_required",
                    "Contract changes must describe an observable result.",
                )
            )
        claims = tuple(contract.verification.claims)
        coverage = tuple(contract.verification.coverage)
        if not claims:
            findings.append(
                ContractFinding(
                    "verification_required",
                    "Contract verification must include at least one structured claim.",
                )
            )
        self._append_coverage_findings(findings, changes, claims, coverage)
        self._append_assertion_findings(
            findings,
            contract.verification.assertions,
            changes,
            claims,
        )
        if any(path.strip().lower() in self._BROAD_SCOPE for path in contract.scope.paths):
            findings.append(
                ContractFinding(
                    "scope_unusually_broad",
                    "Contract scope covers the whole repository; review it before execution.",
                    severity="warning",
                )
            )
        if self._has_obvious_goal_conflict(contract):
            findings.append(
                ContractFinding(
                    "expected_change_conflicts_with_goal",
                    "Expected changes explicitly state that the requested change will not occur.",
                )
            )
        return ContractValidationResult(tuple(findings))

    @staticmethod
    def _has_obvious_goal_conflict(contract: ChangeContract) -> bool:
        goal = contract.goal.lower()
        requests_change = any(word in goal for word in ("fix", "add", "change", "update", "remove"))
        if not requests_change:
            return False
        return any(
            item.description.strip().lower()
            in {"no change", "no changes", "leave unchanged"}
            for item in contract.changes
        )

    @staticmethod
    def _append_coverage_findings(findings, changes, claims, coverage) -> None:
        change_ids = [change.id for change in changes]
        claim_ids = [claim.id for claim in claims]
        duplicate_change_ids = _duplicates(change_ids)
        duplicate_claim_ids = _duplicates(claim_ids)
        duplicate_pairs = _duplicates(
            [(item.claim_id, item.change_id) for item in coverage]
        )
        for change_id in duplicate_change_ids:
            findings.append(
                ContractFinding(
                    "expected_change_id_duplicate",
                    f"Expected Change id is duplicated: {change_id}",
                )
            )
        for claim_id in duplicate_claim_ids:
            findings.append(
                ContractFinding(
                    "verification_claim_id_duplicate",
                    f"Verification claim id is duplicated: {claim_id}",
                )
            )
        for claim_id, change_id in duplicate_pairs:
            findings.append(
                ContractFinding(
                    "coverage_pair_duplicate",
                    f"Coverage pair is duplicated: {claim_id} -> {change_id}",
                )
            )

        known_changes = set(change_ids)
        known_claims = set(claim_ids)
        invalid_reference = False
        for item in coverage:
            if item.claim_id not in known_claims:
                invalid_reference = True
                findings.append(
                    ContractFinding(
                        "coverage_claim_missing",
                        f"Coverage references unknown claim: {item.claim_id}",
                    )
                )
            if item.change_id not in known_changes:
                invalid_reference = True
                findings.append(
                    ContractFinding(
                        "coverage_change_missing",
                        f"Coverage references unknown Expected Change: {item.change_id}",
                    )
                )

        if invalid_reference:
            return

        direct_changes = {
            item.change_id for item in coverage if item.coverage_type == "direct"
        }
        for change in changes:
            if change.requirement == "required" and change.id not in direct_changes:
                findings.append(
                    ContractFinding(
                        "EXPECTED_CHANGE_UNVERIFIED",
                        f"Required Expected Change lacks direct claim coverage: {change.id}",
                    )
                )

    @staticmethod
    def _append_assertion_findings(findings, assertions, changes, claims) -> None:
        assertion_ids = [assertion.id for assertion in assertions]
        for assertion_id in _duplicates(assertion_ids):
            findings.append(
                ContractFinding(
                    "verification_assertion_id_duplicate",
                    f"Verification assertion id is duplicated: {assertion_id}",
                )
            )
        known_changes = {change.id for change in changes}
        known_claims = {claim.id for claim in claims}
        claims_by_id = {claim.id: claim for claim in claims}
        for assertion in assertions:
            if assertion.change_id not in known_changes:
                findings.append(
                    ContractFinding(
                        "assertion_change_missing",
                        "Assertion references unknown Expected Change: "
                        f"{assertion.change_id}",
                    )
                )
            if assertion.claim_id is not None and assertion.claim_id not in known_claims:
                findings.append(
                    ContractFinding(
                        "assertion_claim_missing",
                        f"Assertion references unknown claim: {assertion.claim_id}",
                    )
                )
            if assertion.assertion_type is AssertionType.EXIT_CODE and assertion.claim_id is None:
                findings.append(
                    ContractFinding(
                        "assertion_claim_required",
                        f"EXIT_CODE assertion requires claim_id: {assertion.id}",
                    )
                )
            if assertion.assertion_type is AssertionType.BEHAVIOR_PROBE:
                if assertion.claim_id is None:
                    findings.append(
                        ContractFinding(
                            "assertion_claim_required",
                            f"BEHAVIOR_PROBE assertion requires claim_id: {assertion.id}",
                        )
                    )
                elif assertion.claim_id in claims_by_id and (
                    claims_by_id[assertion.claim_id].expected_result.exit_code != 0
                ):
                    findings.append(
                        ContractFinding(
                            "behavior_probe_exit_code_required",
                            "BEHAVIOR_PROBE claim must expect exit code 0: "
                            f"{assertion.claim_id}",
                        )
                    )
            config = assertion.config
            invalid_config = False
            if assertion.assertion_type is AssertionType.EXIT_CODE:
                invalid_config = type(config.get("expected_exit_code")) is not int
            elif assertion.assertion_type is AssertionType.FILE_CHANGED:
                path = config.get("path")
                parsed = PurePosixPath(path) if isinstance(path, str) else None
                invalid_config = (
                    parsed is None
                    or not path
                    or "\\" in path
                    or parsed.is_absolute()
                    or ".." in parsed.parts
                    or parsed == PurePosixPath(".")
                )
            elif assertion.assertion_type is AssertionType.DIFF_CONTAINS:
                invalid_config = not isinstance(config.get("pattern"), str) or not config.get("pattern")
            elif assertion.assertion_type is AssertionType.BEHAVIOR_PROBE:
                probe_id = config.get("probe_id")
                invalid_config = (
                    not isinstance(probe_id, str)
                    or not probe_id.strip()
                    or "expected" not in config
                    or not _is_json_value(config.get("expected"))
                )
            if invalid_config:
                findings.append(
                    ContractFinding(
                        (
                            "behavior_probe_config_invalid"
                            if assertion.assertion_type is AssertionType.BEHAVIOR_PROBE
                            else "assertion_config_invalid"
                        ),
                        f"Assertion config is invalid for {assertion.assertion_type.value}: {assertion.id}",
                    )
                )


def _is_json_value(value, active=None) -> bool:
    if value is None or type(value) in {str, bool, int}:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) not in {list, dict}:
        return False
    active = set() if active is None else active
    identity = id(value)
    if identity in active:
        return False
    active.add(identity)
    try:
        if isinstance(value, list):
            return all(_is_json_value(item, active) for item in value)
        return all(
            type(key) is str and _is_json_value(item, active)
            for key, item in value.items()
        )
    finally:
        active.remove(identity)


def _duplicates(values):
    seen = set()
    duplicates = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return tuple(duplicates)
