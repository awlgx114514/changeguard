from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class AuditRecord:
    record_ref: dict[str, Any]
    record_kind: str
    source_kind: str
    schema_version: Optional[str]
    native_scope: dict[str, Any]
    native_id: Optional[str]
    data: dict[str, Any]
    producer_trust_claim: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_ref": self.record_ref,
            "record_kind": self.record_kind,
            "source_kind": self.source_kind,
            "schema_version": self.schema_version,
            "native_scope": self.native_scope,
            "native_id": self.native_id,
            "data": self.data,
            "producer_trust_claim": self.producer_trust_claim,
        }


@dataclass(frozen=True)
class SourceGap:
    code: str
    locator: Optional[str]
    scope: dict[str, Any]
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "locator": self.locator,
            "scope": self.scope,
            "details": self.details,
        }


@dataclass(frozen=True)
class RawSource:
    source_id: str
    source_kind: str
    locator: str
    availability: str
    captured_digest: Optional[str]
    schema_version: Optional[str]
    native_identity: dict[str, Any]
    record_refs: tuple[dict[str, Any], ...]
    read_boundary: dict[str, Any]
    limitations: tuple[str, ...] = ()
    captured_content: Optional[bytes] = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "locator": self.locator,
            "availability": self.availability,
            "captured_digest": self.captured_digest,
            "schema_version": self.schema_version,
            "native_identity": self.native_identity,
            "record_refs": list(self.record_refs),
            "read_boundary": self.read_boundary,
            "limitations": list(self.limitations),
            "captured_payload_ref": (
                {
                    "storage": "IN_MEMORY_CAPTURE",
                    "size_bytes": len(self.captured_content),
                    "digest": self.captured_digest,
                }
                if self.captured_content is not None
                else None
            ),
        }


@dataclass(frozen=True)
class AuditDiscoveryScope:
    target_identity_ref: dict[str, Any]
    scope_ref: dict[str, Any]
    capture_ref: str
    included_source_classes: tuple[str, ...]
    included_locations: tuple[dict[str, Any], ...]
    explicit_references_followed: tuple[dict[str, Any], ...]
    known_excluded_sources: tuple[dict[str, Any], ...]
    known_unavailable_sources: tuple[dict[str, Any], ...]
    discovery_method: tuple[str, ...]
    completeness: str
    completeness_basis: tuple[dict[str, Any], ...]
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_identity_ref": self.target_identity_ref,
            "scope_ref": self.scope_ref,
            "capture_ref": self.capture_ref,
            "included_source_classes": list(self.included_source_classes),
            "included_locations": list(self.included_locations),
            "explicit_references_followed": list(self.explicit_references_followed),
            "known_excluded_sources": list(self.known_excluded_sources),
            "known_unavailable_sources": list(self.known_unavailable_sources),
            "discovery_method": list(self.discovery_method),
            "completeness": self.completeness,
            "completeness_basis": list(self.completeness_basis),
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class RawAuditSources:
    capture_id: str
    requested_selector: str
    selected_sources: tuple[RawSource, ...]
    source_records: tuple[AuditRecord, ...]
    source_gaps: tuple[SourceGap, ...]
    read_boundaries: tuple[dict[str, Any], ...]
    discovery_scope: AuditDiscoveryScope
    capture_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "requested_selector": self.requested_selector,
            "selected_sources": [source.to_dict() for source in self.selected_sources],
            "source_records": [record.to_dict() for record in self.source_records],
            "source_gaps": [gap.to_dict() for gap in self.source_gaps],
            "read_boundaries": list(self.read_boundaries),
            "discovery_scope": self.discovery_scope.to_dict(),
            "capture_status": self.capture_status,
            "coverage": {
                "discovery_scope_ref": self.discovery_scope.capture_ref,
                "source_discovery_completeness": self.discovery_scope.completeness,
                "record_count": len(self.source_records),
                "source_count": len(self.selected_sources),
                "coverage_limitations": list(self.discovery_scope.limitations),
                "source_gap_count": len(self.source_gaps),
            },
        }


@dataclass(frozen=True)
class IntegrityFinding:
    finding_id: str
    category: str
    status: str
    reason_code: str
    subject_ref: Optional[dict[str, Any]]
    evidence_refs: tuple[dict[str, Any], ...]
    scope: dict[str, Any]
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "category": self.category,
            "status": self.status,
            "reason_code": self.reason_code,
            "subject_ref": self.subject_ref,
            "evidence_refs": list(self.evidence_refs),
            "scope": self.scope,
            "details": self.details,
        }


@dataclass(frozen=True)
class VerifiedAuditSources:
    raw_sources_ref: str
    records: tuple[AuditRecord, ...]
    findings: tuple[IntegrityFinding, ...]
    reference_edges: tuple[dict[str, Any], ...]
    trusted_basis_record_refs: tuple[dict[str, Any], ...]
    excluded_basis_record_refs: tuple[dict[str, Any], ...]
    coverage: dict[str, Any]
    audit_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_sources_ref": self.raw_sources_ref,
            "records": [record.to_dict() for record in self.records],
            "findings": [finding.to_dict() for finding in self.findings],
            "reference_edges": list(self.reference_edges),
            "trusted_basis_record_refs": list(self.trusted_basis_record_refs),
            "excluded_basis_record_refs": list(self.excluded_basis_record_refs),
            "coverage": self.coverage,
            "audit_status": self.audit_status,
        }


@dataclass(frozen=True)
class AuditSnapshot:
    capture: dict[str, Any]
    identity: dict[str, Any]
    execution: tuple[dict[str, Any], ...]
    evidence: tuple[dict[str, Any], ...]
    decision: tuple[dict[str, Any], ...]
    persistence: dict[str, Any]
    integrity_findings: tuple[dict[str, Any], ...]
    diagnostics: tuple[dict[str, Any], ...]
    audit_status: str
    representation_gaps: tuple[dict[str, Any], ...]
    coverage: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_schema_version": "1.0",
            "capture": self.capture,
            "identity": self.identity,
            "execution": list(self.execution),
            "evidence": list(self.evidence),
            "decision": list(self.decision),
            "persistence": self.persistence,
            "integrity_findings": list(self.integrity_findings),
            "diagnostics": list(self.diagnostics),
            "audit_status": self.audit_status,
            "representation_gaps": list(self.representation_gaps),
            "coverage": self.coverage,
        }
