import hashlib
import json
from typing import Any

from .models import AuditSnapshot, RawAuditSources, VerifiedAuditSources


_TASK3_EVIDENCE_TYPES = {
    "TASK3_RUN_EVIDENCE": "MEASUREMENT",
    "TASK3_LIFECYCLE_EVIDENCE": "DIRECT_CHILD_LIFECYCLE",
    "TASK3_DIRECT_CHILD_BINDINGS": "DIRECT_CHILD_BINDING",
    "TASK3_REGISTERED_ASSET_DEFECTS": "REGISTERED_DEFECT",
    "TASK3_MEASUREMENT_PROFILES": "MEASUREMENT_PROFILE",
    "TASK3_CAMPAIGNS": "CAMPAIGN_IDENTITY",
    "SOURCE_BINDING": "SOURCE_BINDING",
}


def _payload(record: Any) -> dict[str, Any]:
    value = record.data.get("payload")
    if isinstance(value, dict):
        return value
    value = record.data.get("event_payload")
    return value if isinstance(value, dict) else {}


def _row(record: Any) -> dict[str, Any]:
    value = record.data.get("row")
    return value if isinstance(value, dict) else {}


def _finding_refs(findings: tuple[Any, ...], record_ref: dict[str, Any]) -> list[str]:
    result = []
    for finding in findings:
        if finding.subject_ref == record_ref or record_ref in finding.evidence_refs:
            result.append(finding.finding_id)
    return result


def _record_status(findings: tuple[Any, ...], record_ref: dict[str, Any]) -> str:
    statuses = [
        finding.status
        for finding in findings
        if finding.subject_ref == record_ref or record_ref in finding.evidence_refs
    ]
    if "INVALID" in statuses:
        return "INVALID"
    if "UNAVAILABLE" in statuses:
        return "UNAVAILABLE"
    if "UNVERIFIABLE" in statuses:
        return "UNVERIFIABLE"
    return "VALID"


def _identity(raw: RawAuditSources, verified: VerifiedAuditSources) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "namespace": raw.discovery_scope.target_identity_ref.get("namespace"),
        "run_id": raw.requested_selector,
        "project": {"status": "MISSING", "value": None},
        "campaign_id": None,
        "slot_id": None,
        "case_id": None,
        "interaction_id": None,
        "interaction_generation": None,
        "asset_identity": None,
        "fixture_identity": None,
        "source_identity": None,
        "environment_identity": None,
        "identity_integrity": "VALID",
    }
    scope_values: dict[str, list[Any]] = {}
    campaign_values: dict[str, list[Any]] = {}
    for record in verified.records:
        if record.source_kind == "TRACE_V2":
            event_data = record.data.get("data", {})
            if isinstance(event_data, dict):
                for key in ("task_id", "case_id", "interaction_id", "interaction_generation"):
                    if event_data.get(key) is not None:
                        values = scope_values.setdefault(key, [])
                        if event_data[key] not in values:
                            values.append(event_data[key])
        if not record.record_kind.startswith("TASK3_"):
            if record.record_kind == "UNBOUND_SUPPLEMENT":
                for key in (
                    "case_id",
                    "interaction_id",
                    "interaction_generation",
                    "asset_identity",
                    "fixture_identity",
                    "environment_identity",
                ):
                    if record.data.get(key) is not None:
                        values = scope_values.setdefault(key, [])
                        if record.data[key] not in values:
                            values.append(record.data[key])
                binding = record.data.get("source_import_evidence")
                if isinstance(binding, dict) and binding.get("source_tree_sha256") is not None:
                    values = scope_values.setdefault("source_identity", [])
                    if binding["source_tree_sha256"] not in values:
                        values.append(binding["source_tree_sha256"])
            continue
        payload = _payload(record)
        for key in ("campaign_id", "slot_id", "case_id", "interaction_id", "interaction_generation"):
            value = record.native_scope.get(key)
            if value is not None:
                values = scope_values.setdefault(key, [])
                if value not in values:
                    values.append(value)
        campaign_identity = record.data.get("identity")
        if isinstance(campaign_identity, dict):
            for key in (
                "semantic_spec_identity",
                "benchmark_contract_identity",
                "scoring_contract_identity",
                "calibration_campaign_identity",
                "deadline_formula_identity",
                "accepted_asset_identities",
                "environment_profile_identity",
                "formal_profile_identity",
                "profile_type",
                "profile_catalog_version",
                "profile_catalog_hash",
                "profile_id",
                "fixture_identity",
            ):
                if campaign_identity.get(key) is not None:
                    values = campaign_values.setdefault(key, [])
                    if campaign_identity[key] not in values:
                        values.append(campaign_identity[key])
        for key in ("asset_identity", "fixture_identity", "environment_identity"):
            if payload.get(key) is not None:
                values = scope_values.setdefault(key, [])
                if payload[key] not in values:
                    values.append(payload[key])
    multiple_scopes = {}
    for key, values in scope_values.items():
        if len(values) == 1:
            identity[key] = values[0]
        elif len(values) > 1:
            identity[key] = None
            multiple_scopes[key] = values
            if key in {"campaign_id", "slot_id"}:
                identity["identity_integrity"] = "INVALID"
    for key, values in campaign_values.items():
        if len(values) == 1:
            identity[key] = values[0]
        elif len(values) > 1:
            identity[key] = None
            multiple_scopes[key] = values
            identity["identity_integrity"] = "INVALID"
    if multiple_scopes:
        identity["multiple_scopes"] = multiple_scopes
    return identity


def _field_statuses(record: Any, fields: dict[str, Any]) -> dict[str, Any]:
    statuses = {
        key: {
            "record_local_status": "FIELD_PRESENT",
            "record_ref": record.record_ref,
            "status_basis": {
                "source_kind": record.source_kind,
                "schema_version": record.schema_version,
                "raw_field": key,
            },
        }
        for key in fields
    }
    if record.record_kind == "UNBOUND_SUPPLEMENT" and "source_import_evidence" not in fields:
        statuses["source_import_evidence"] = {
            "record_local_status": "FIELD_UNVERIFIABLE",
            "record_ref": record.record_ref,
            "status_basis": {
                "reason_code": "SOURCE_SCHEMA_VERSION_UNAVAILABLE",
                "source_kind": record.source_kind,
                "schema_version": record.schema_version,
            },
        }
    return statuses


def _execution(verified: VerifiedAuditSources) -> tuple[dict[str, Any], ...]:
    executions = []
    for record in verified.records:
        if record.source_kind != "TRACE_V2" or record.record_kind not in {
            "ACTION",
            "EXECUTION_RESULT",
            "EXECUTION_MARKER",
            "LIFECYCLE_MARKER",
            "REPORT_PROJECTION",
            "POLICY_EVALUATION",
            "OBSERVATION",
            "VALIDATION",
            "REPAIR",
        }:
            continue
        event = record.data
        event_type = event.get("type")
        data = event.get("data", {})
        execution = {
            "record_ref": record.record_ref,
            "record_kind": record.record_kind,
            "event_type": event_type,
            "subject_identity": {
                key: data[key]
                for key in ("run_id", "case_id", "interaction_id", "interaction_generation")
                if isinstance(data, dict) and data.get(key) is not None
            },
            "representation": "SUMMARY" if record.record_kind == "REPORT_PROJECTION" else "STORED_EVENT",
            "stored_fields": data,
            "record_local_status": _field_statuses(record, data),
            "status_basis": {
                "record_ref": record.record_ref,
                "source_kind": record.source_kind,
                "schema_version": record.schema_version,
            },
            "sequence": event.get("sequence"),
            "timestamp": event.get("timestamp"),
            "ordering": {
                "source_local_sequence": event.get("sequence"),
                "ordering_status": "SOURCE_LOCAL" if isinstance(event.get("sequence"), int) else "UNVERIFIABLE",
            },
        }
        executions.append(execution)
    executions.sort(key=lambda item: (item["sequence"] is None, item["sequence"] or 0))
    return tuple(executions)


def _evidence(verified: VerifiedAuditSources) -> tuple[dict[str, Any], ...]:
    evidence = []
    evidence_by_identity: dict[tuple[str, Optional[str], str], dict[str, Any]] = {}
    consumed_by: dict[str, list[str]] = {}
    for edge in verified.reference_edges:
        if "to_record_id" in edge:
            consumed_by.setdefault(edge["to_record_id"], []).append(edge["from_ref"].get("native_id", ""))
    for record in verified.records:
        evidence_type = _TASK3_EVIDENCE_TYPES.get(record.record_kind)
        if record.record_kind == "UNBOUND_SUPPLEMENT":
            evidence_type = "UNBOUND_SUPPLEMENT"
        if record.source_kind == "TRACE_V2" and record.record_kind in {"OBSERVATION", "VALIDATION", "REPAIR"}:
            evidence_type = record.record_kind
        if evidence_type is None and record.record_kind not in {
            "TASK3_TERMINAL_DECISIONS",
            "TASK3_PREPARED_TERMINAL_DECISIONS",
            "TASK3_DIAGNOSTICS",
        }:
            evidence_type = record.record_kind
        if evidence_type is None:
            continue
        payload = _payload(record)
        status = _record_status(verified.findings, record.record_ref)
        observed_fields = payload if payload else record.data
        semantic_identity = json.dumps(
            [record.native_scope, observed_fields, record.source_kind, record.schema_version, record.producer_trust_claim],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        identity_key = (record.record_kind, record.native_id, semantic_identity)
        existing = evidence_by_identity.get(identity_key)
        if existing is not None:
            if record.record_ref not in existing["raw_refs"]:
                existing["raw_refs"].append(record.record_ref)
            existing["integrity_findings"] = sorted(
                set(existing["integrity_findings"] + _finding_refs(verified.findings, record.record_ref))
            )
            if status == "INVALID":
                existing["integrity_status"] = "INVALID"
                existing["explanation_eligibility"] = "EXCLUDED"
            existing["consumed_by"] = sorted(
                set(existing["consumed_by"] + consumed_by.get(record.native_id or "", []))
            )
            continue
        item = {
            "record_ref": record.record_ref,
            "evidence_id": record.native_id,
            "evidence_type": evidence_type,
            "owner": record.source_kind,
            "subject_identity": record.native_scope,
            "producer_trust_claim": record.producer_trust_claim or payload.get("observation_authority"),
            "audit_trust_assessment": {
                "status": "UNVERIFIABLE",
                "reason_code": "PRODUCER_AUTHORITY_NOT_ESTABLISHED_WITHIN_CAPTURE",
            },
            "integrity_status": status,
            "raw_refs": [record.record_ref],
            "observed_fields": observed_fields,
            "record_local_status": _field_statuses(record, observed_fields),
            "status_basis": {
                "record_ref": record.record_ref,
                "source_kind": record.source_kind,
                "schema_version": record.schema_version,
            },
            "integrity_findings": _finding_refs(verified.findings, record.record_ref),
            "ordering": {
                "source_sequence": payload.get("observation_sequence"),
                "native_timestamp": payload.get("observation_timestamp_utc"),
                "ordering_status": "SOURCE_LOCAL" if payload.get("observation_sequence") is not None else "UNVERIFIABLE",
            },
            "consumed_by": consumed_by.get(record.native_id or "", []),
            "explanation_eligibility": "EXCLUDED" if status == "INVALID" else "LIMITED_BY_TRUST",
        }
        evidence_by_identity[identity_key] = item
        evidence.append(item)
    return tuple(evidence)


def _decisions(verified: VerifiedAuditSources) -> tuple[dict[str, Any], ...]:
    decisions = []
    edges_by_decision: dict[str, list[dict[str, Any]]] = {}
    for edge in verified.reference_edges:
        edges_by_decision.setdefault(edge["from_ref"].get("native_id", ""), []).append(edge)
    for record in verified.records:
        if record.record_kind != "TASK3_TERMINAL_DECISIONS":
            continue
        payload = _payload(record)
        edges = edges_by_decision.get(record.native_id or "", [])
        edge_statuses = [edge["status"] for edge in edges]
        if any(status == "INVALID" for status in edge_statuses):
            support = "INVALID"
        elif not edges or any(status == "BROKEN" for status in edge_statuses):
            support = "INCOMPLETE"
        elif any(status != "VALID" for status in edge_statuses):
            support = "UNVERIFIABLE"
        else:
            support = "LIMITED_BY_PRODUCER_TRUST"
        decisions.append(
            {
                "record_ref": record.record_ref,
                "decision_id": record.native_id,
                "owner": "TASK3_CAMPAIGN_STORE",
                "decision_scope": record.native_scope,
                "stored_value": payload.get("verdict"),
                "stored_payload": payload,
                "stored_record": _row(record),
                "integrity_status": _record_status(verified.findings, record.record_ref),
                "record_local_status": _field_statuses(record, payload),
                "status_basis": {
                    "record_ref": record.record_ref,
                    "source_kind": record.source_kind,
                    "schema_version": record.schema_version,
                },
                "stored_attribution": payload.get("attribution_result"),
                "stored_fail_attribution": payload.get("fail_attribution"),
                "support_assessment": {
                    "status": support,
                    "reference_edges": edges,
                },
                "origin": "STORED",
            }
        )
    return tuple(decisions)


def _persistence(verified: VerifiedAuditSources) -> dict[str, Any]:
    terminal_rows: dict[str, dict[str, Any]] = {}
    prepared = []
    stored = []
    for record in verified.records:
        if record.record_kind == "TASK3_TERMINAL_DECISIONS":
            terminal_rows[str(_row(record).get("terminal_decision_identity"))] = _row(record)
            stored.append({"record_ref": record.record_ref, "record_kind": record.record_kind, "row": _row(record)})
        elif record.record_kind == "TASK3_PREPARED_TERMINAL_DECISIONS":
            prepared.append(record)
            stored.append({"record_ref": record.record_ref, "record_kind": record.record_kind, "row": _row(record)})
        elif record.record_kind == "TASK3_SLOTS":
            stored.append({"record_ref": record.record_ref, "record_kind": record.record_kind, "row": _row(record)})
    assessments = []
    for record in prepared:
        row = _row(record)
        identity = str(row.get("terminal_decision_identity"))
        terminal = terminal_rows.get(identity)
        phase = row.get("phase")
        receipt = row.get("receipt_identity")
        commit_verified = False
        if terminal is not None and phase == "COMMITTED" and isinstance(receipt, str):
            expected_receipt = hashlib.sha256(
                f"{identity}:{row.get('payload_hash')}".encode("utf-8")
            ).hexdigest()
            commit_verified = (
                row.get("payload_hash") == terminal.get("payload_hash")
                and receipt == expected_receipt
            )
        assessments.append(
            {
                "record_ref": record.record_ref,
                "terminal_decision_identity": identity,
                "stored_phase": phase,
                "stored_receipt_identity": receipt,
                "status": "COMMIT_RECORD_VERIFIED" if commit_verified else "PERSISTENCE_UNVERIFIABLE",
                "verified_terminal_ref": terminal.get("terminal_decision_identity") if commit_verified and terminal else None,
                "scope": record.native_scope,
            }
        )
    return {
        "stored_records": stored,
        "assessments": assessments,
        "durability_claim": "NOT_ASSESSED",
        "read_consistency": "SINGLE_SOURCE_SNAPSHOT" if stored else "UNAVAILABLE",
    }


def build_audit_snapshot(raw: RawAuditSources, verified: VerifiedAuditSources) -> AuditSnapshot:
    identity = _identity(raw, verified)
    decisions = _decisions(verified)
    persistence = _persistence(verified)
    coverage = {
        **verified.coverage,
        "discovery_scope": raw.discovery_scope.to_dict(),
        "included_source_classes": list(raw.discovery_scope.included_source_classes),
        "included_locations": list(raw.discovery_scope.included_locations),
        "source_record_counts": {
            source.source_id: len(source.record_refs)
            for source in raw.selected_sources
        },
        "read_boundaries": list(raw.read_boundaries),
    }
    representation_gaps = [
        {
            "code": limitation,
            "scope": {"run_id": raw.requested_selector},
            "origin": "CAPTURE_LIMITATION",
        }
        for limitation in raw.discovery_scope.limitations
    ]
    representation_gaps.extend(
        {
            "code": source_gap.code,
            "locator": source_gap.locator,
            "scope": source_gap.scope,
            "origin": "SOURCE_GAP",
        }
        for source_gap in raw.source_gaps
    )
    audit_status = "INVALID" if identity.get("identity_integrity") == "INVALID" else verified.audit_status
    return AuditSnapshot(
        {
            "capture_id": raw.capture_id,
            "requested_selector": raw.requested_selector,
            "source_codecs": [
                {
                    "source_id": source.source_id,
                    "source_kind": source.source_kind,
                    "schema_version": source.schema_version,
                    "codec_version": "1",
                }
                for source in raw.selected_sources
            ],
            "sources": [source.to_dict() for source in raw.selected_sources],
            "read_consistency": "PARTIAL_UNCOORDINATED" if len(raw.selected_sources) > 1 else (
                raw.read_boundaries[0].get("read_consistency", "SINGLE_SOURCE_SNAPSHOT") if raw.read_boundaries else "UNAVAILABLE"
            ),
            "discovery_scope": raw.discovery_scope.to_dict(),
            "coverage": coverage,
        },
        identity,
        _execution(verified),
        _evidence(verified),
        decisions,
        persistence,
        tuple(finding.to_dict() for finding in verified.findings),
        tuple(
            [
                {
                    "record_ref": record.record_ref,
                    "event_type": _row(record).get("event_type"),
                    "stored_payload": record.data.get("event_payload"),
                    "origin": "STORED",
                }
                for record in verified.records
                if record.record_kind == "TASK3_DIAGNOSTICS"
            ]
            + [
                {**finding.to_dict(), "code": finding.reason_code, "origin": "DERIVED"}
                for finding in verified.findings
            ]
        ),
        audit_status,
        tuple(representation_gaps),
        coverage,
    )
