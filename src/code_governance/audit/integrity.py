import hashlib
import json
from collections import defaultdict
from typing import Any, Optional

from .models import IntegrityFinding, RawAuditSources, VerifiedAuditSources


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _finding(
    index: int,
    category: str,
    status: str,
    reason_code: str,
    subject_ref: Optional[dict[str, Any]],
    evidence_refs: tuple[dict[str, Any], ...],
    scope: dict[str, Any],
    details: dict[str, Any],
) -> IntegrityFinding:
    return IntegrityFinding(
        f"finding-{index:04d}",
        category,
        status,
        reason_code,
        subject_ref,
        evidence_refs,
        scope,
        details,
    )


def _payload(record: Any) -> dict[str, Any]:
    payload = record.data.get("payload")
    return payload if isinstance(payload, dict) else {}


def _row_hash_status(record: Any) -> list[tuple[str, bool, str]]:
    row = record.data.get("row")
    if not isinstance(row, dict):
        return []
    checked: list[tuple[str, bool, str]] = []
    for payload_field, digest_field in (("payload_json", "payload_hash"), ("identity_json", "identity_hash")):
        raw = row.get(payload_field)
        expected = row.get(digest_field)
        if raw is None and expected is None:
            continue
        if not isinstance(raw, str) or not isinstance(expected, str):
            checked.append((payload_field, False, "HASH_INPUT_MISSING"))
            continue
        if payload_field == "identity_json":
            identity = record.data.get("identity")
            if not isinstance(identity, dict):
                checked.append((payload_field, False, "IDENTITY_JSON_UNAVAILABLE"))
                continue
            identity_values = dict(identity)
            identity_values.pop("campaign_id", None)
            encoded = _canonical(identity_values)
        else:
            encoded = raw
        actual = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        checked.append((payload_field, actual == expected, actual))
    return checked


def _scope_matches_row(record: Any, run_id: str) -> tuple[bool, dict[str, Any]]:
    row = record.data.get("row")
    payload = _payload(record)
    if not isinstance(row, dict):
        return True, {}
    conflicts: dict[str, Any] = {}
    if row.get("run_id") is not None and row.get("run_id") != run_id:
        conflicts["run_id"] = {"row": row.get("run_id"), "requested": run_id}
    for key in (
        "run_id",
        "campaign_id",
        "slot_id",
        "case_id",
        "interaction_id",
        "interaction_generation",
        "evidence_id",
        "terminal_decision_identity",
        "transaction_identity",
        "asset_identity",
        "defect_id",
    ):
        row_value = row.get(key)
        payload_value = payload.get(key)
        if row_value is not None and payload_value is not None and row_value != payload_value:
            conflicts[key] = {"row": row_value, "payload": payload_value}
    identity = record.data.get("identity")
    if isinstance(identity, dict) and row.get("campaign_id") is not None:
        if identity.get("campaign_id") != row.get("campaign_id"):
            conflicts["identity.campaign_id"] = {
                "row": row.get("campaign_id"),
                "identity": identity.get("campaign_id"),
            }
    return not conflicts, conflicts


def _source_gap_finding(index: int, gap: Any) -> IntegrityFinding:
    code = gap.code
    if code in {"SOURCE_CORRUPT", "SOURCE_CHANGED"}:
        category, status = "ARTIFACT_INTEGRITY", "INVALID"
    elif code in {"IDENTITY_CONFLICT"}:
        category, status = "IDENTITY", "INVALID"
    elif code == "RUN_NOT_FOUND":
        category, status = "DISCOVERY", "UNAVAILABLE"
    elif code in {"SCHEMA_UNSUPPORTED"}:
        category, status = "SCHEMA", "UNVERIFIABLE"
    elif code in {"SOURCE_UNAVAILABLE", "READ_ONLY_PRECONDITION_FAILED"}:
        category, status = "SOURCE_ACCESS", "UNAVAILABLE"
    else:
        category, status = "DISCOVERY", "UNVERIFIABLE"
    return _finding(index, category, status, code, None, (), gap.scope, gap.details)


def verify_audit_sources(raw: RawAuditSources) -> VerifiedAuditSources:
    findings: list[IntegrityFinding] = []
    next_index = 1
    for gap in raw.source_gaps:
        findings.append(_source_gap_finding(next_index, gap))
        next_index += 1

    record_integrity: dict[int, str] = {}
    identity_groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for record in raw.source_records:
        integrity_status = "VALID"
        hashes = _row_hash_status(record)
        for field_name, valid, actual in hashes:
            if not valid:
                integrity_status = "INVALID"
                findings.append(
                    _finding(
                        next_index,
                        "ARTIFACT_INTEGRITY",
                        "INVALID",
                        "HASH_MISMATCH",
                        record.record_ref,
                        (record.record_ref,),
                        record.native_scope,
                        {"field": field_name, "observed_digest": actual},
                    )
                )
                next_index += 1
        matches, conflicts = _scope_matches_row(record, raw.requested_selector)
        if not matches:
            integrity_status = "INVALID"
            findings.append(
                _finding(
                    next_index,
                    "SCOPE_BINDING",
                    "INVALID",
                    "IDENTITY_SCOPE_MISMATCH",
                    record.record_ref,
                    (record.record_ref,),
                    record.native_scope,
                    {"conflicts": conflicts},
                )
            )
            next_index += 1
        if record.source_kind == "TASK3_SQLITE" and record.native_id is not None:
            identity_namespace = (
                "TASK3_EVIDENCE"
                if record.record_kind in {"TASK3_RUN_EVIDENCE", "TASK3_LIFECYCLE_EVIDENCE"}
                else record.record_kind
            )
            identity_groups[(identity_namespace, record.native_id)].append(record)
        record_integrity[id(record)] = integrity_status

    for (record_kind, native_id), duplicates in identity_groups.items():
        if len(duplicates) < 2:
            continue
        contents = {
            _canonical({"native_scope": record.native_scope, "data": record.data})
            for record in duplicates
        }
        if len(contents) > 1:
            for record in duplicates:
                record_integrity[id(record)] = "INVALID"
            findings.append(
                _finding(
                    next_index,
                    "IDENTITY",
                    "INVALID",
                    "DUPLICATE_IDENTITY_CONFLICT",
                    duplicates[0].record_ref,
                    tuple(record.record_ref for record in duplicates),
                    duplicates[0].native_scope,
                    {"record_kind": record_kind, "native_id": native_id, "duplicate_count": len(duplicates)},
                )
            )
            next_index += 1

    scoped_values: dict[tuple[str, str], dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
    for record in raw.source_records:
        if record.source_kind != "TASK3_SQLITE" or record.native_scope.get("run_id") != raw.requested_selector:
            continue
        payload = _payload(record)
        row = record.data.get("row") if isinstance(record.data.get("row"), dict) else {}
        for field_name in ("campaign_id", "slot_id", "asset_identity", "environment_identity"):
            value = record.native_scope.get(field_name)
            if value is None:
                value = row.get(field_name)
            if value is None:
                value = payload.get(field_name)
            if value is not None:
                scoped_values[(raw.requested_selector, field_name)][_canonical(value)].append(record)
        interaction_value = record.native_scope.get("interaction_id")
        if interaction_value is None:
            interaction_value = payload.get("interaction_id")
        interaction = str(interaction_value or "")
        generation_value = record.native_scope.get("interaction_generation")
        if generation_value is None:
            generation_value = payload.get("interaction_generation")
        generation = str(generation_value or "")
        for field_name in ("case_id", "interaction_generation"):
            value = record.native_scope.get(field_name)
            if value is None:
                value = payload.get(field_name)
            if value is not None:
                scope_key = (
                    f"case_id:{interaction}:{generation}"
                    if field_name == "case_id"
                    else f"interaction_generation:{interaction}"
                )
                scoped_values[(raw.requested_selector, scope_key)][_canonical(value)].append(record)
    for (run_id, field_name), values in scoped_values.items():
        if len(values) < 2:
            continue
        conflicting_records = [record for records in values.values() for record in records]
        for record in conflicting_records:
            record_integrity[id(record)] = "INVALID"
        findings.append(
            _finding(
                next_index,
                "SCOPE_BINDING",
                "INVALID",
                "IDENTITY_SCOPE_CONFLICT",
                conflicting_records[0].record_ref,
                tuple(record.record_ref for record in conflicting_records),
                {"run_id": run_id},
                {"field": field_name, "observed_values": sorted(values)},
            )
        )
        next_index += 1

    evidence_records: dict[str, list[Any]] = defaultdict(list)
    binding_records: dict[tuple[Any, ...], list[Any]] = defaultdict(list)
    lifecycle_records: dict[tuple[Any, ...], list[Any]] = defaultdict(list)
    for record in raw.source_records:
        if record.record_kind in {"TASK3_RUN_EVIDENCE", "TASK3_LIFECYCLE_EVIDENCE"} and record.native_id:
            evidence_records[record.native_id].append(record)
        scope = record.native_scope
        interaction_key = (
            scope.get("run_id"),
            scope.get("campaign_id"),
            scope.get("slot_id"),
            scope.get("interaction_id"),
            scope.get("interaction_generation"),
        )
        if record.record_kind == "TASK3_DIRECT_CHILD_BINDINGS":
            binding_records[interaction_key].append(record)
        elif record.record_kind == "TASK3_LIFECYCLE_EVIDENCE":
            lifecycle_records[interaction_key].append(record)

    for interaction_key, lifecycle_items in lifecycle_records.items():
        bindings = binding_records.get(interaction_key, [])
        if not bindings:
            findings.append(
                _finding(
                    next_index,
                    "CHILD_BINDING",
                    "UNVERIFIABLE",
                    "DIRECT_CHILD_BINDING_NOT_CAPTURED",
                    lifecycle_items[0].record_ref,
                    tuple(item.record_ref for item in lifecycle_items),
                    lifecycle_items[0].native_scope,
                    {"scope_key": list(interaction_key)},
                )
            )
            next_index += 1
            continue
        for binding in bindings:
            binding_payload = _payload(binding)
            for lifecycle in lifecycle_items:
                lifecycle_payload = _payload(lifecycle)
                conflicts = {}
                for field_name in (
                    "child_pid",
                    "child_creation_identity",
                    "child_process_identity",
                    "stable_handle_binding_identity",
                    "transaction_identity",
                ):
                    binding_value = binding_payload.get(field_name)
                    lifecycle_value = lifecycle_payload.get(field_name)
                    if binding_value is not None and lifecycle_value is not None and binding_value != lifecycle_value:
                        conflicts[field_name] = {"binding": binding_value, "lifecycle": lifecycle_value}
                if conflicts:
                    record_integrity[id(binding)] = "INVALID"
                    record_integrity[id(lifecycle)] = "INVALID"
                    findings.append(
                        _finding(
                            next_index,
                            "CHILD_BINDING",
                            "INVALID",
                            "DIRECT_CHILD_IDENTITY_CONFLICT",
                            lifecycle.record_ref,
                            (binding.record_ref, lifecycle.record_ref),
                            lifecycle.native_scope,
                            {"conflicts": conflicts},
                        )
                    )
                    next_index += 1

    reference_edges: list[dict[str, Any]] = []
    for measurement in raw.source_records:
        if measurement.record_kind != "TASK3_RUN_EVIDENCE":
            continue
        payload = _payload(measurement)
        lifecycle_ids = payload.get("lifecycle_evidence_ids", [])
        if not isinstance(lifecycle_ids, list):
            lifecycle_ids = []
        if payload.get("lifecycle_required") is True and not lifecycle_ids:
            findings.append(
                _finding(
                    next_index,
                    "DECISION_REFERENCE",
                    "UNVERIFIABLE",
                    "RUN_EVIDENCE_LIFECYCLE_REFERENCE_MISSING",
                    measurement.record_ref,
                    (measurement.record_ref,),
                    measurement.native_scope,
                    {},
                )
            )
            next_index += 1
        for evidence_id in sorted(value for value in lifecycle_ids if isinstance(value, str)):
            candidates = evidence_records.get(evidence_id, [])
            valid_candidates = [
                record
                for record in candidates
                if record.record_kind == "TASK3_LIFECYCLE_EVIDENCE"
                and record.native_scope.get("run_id") == measurement.native_scope.get("run_id")
                and record.native_scope.get("campaign_id") == measurement.native_scope.get("campaign_id")
                and record.native_scope.get("interaction_id") == measurement.native_scope.get("interaction_id")
                and record.native_scope.get("interaction_generation") == measurement.native_scope.get("interaction_generation")
            ]
            if not candidates:
                edge_status, reason = "BROKEN", "RUN_EVIDENCE_LIFECYCLE_REFERENCE_MISSING"
            elif not valid_candidates:
                edge_status, reason = "INVALID", "RUN_EVIDENCE_LIFECYCLE_SCOPE_MISMATCH"
            elif any(record_integrity[id(record)] == "INVALID" for record in valid_candidates):
                edge_status, reason = "INVALID", "REFERENCED_EVIDENCE_INVALID"
            else:
                edge_status, reason = "VALID", "REFERENCE_PRESENT"
            reference_edges.append(
                {
                    "from_ref": measurement.record_ref,
                    "to_record_id": evidence_id,
                    "edge_type": "MEASUREMENT_REFERENCES_LIFECYCLE",
                    "status": edge_status,
                    "reason_code": reason,
                    "scope": measurement.native_scope,
                }
            )
            if edge_status != "VALID":
                findings.append(
                    _finding(
                        next_index,
                        "DECISION_REFERENCE",
                        "INVALID" if edge_status == "INVALID" else "UNVERIFIABLE",
                        reason,
                        measurement.record_ref,
                        tuple(record.record_ref for record in candidates),
                        measurement.native_scope,
                        {"referenced_record_id": evidence_id},
                    )
                )
                next_index += 1

    terminal_records = {
        record.native_id: record
        for record in raw.source_records
        if record.record_kind == "TASK3_TERMINAL_DECISIONS"
    }
    for slot in (record for record in raw.source_records if record.record_kind == "TASK3_SLOTS"):
        row = slot.data.get("row", {})
        terminal_id = row.get("terminal_decision_identity") if isinstance(row, dict) else None
        if not terminal_id:
            continue
        terminal = terminal_records.get(str(terminal_id))
        if terminal is None:
            findings.append(
                _finding(
                    next_index,
                    "DECISION_REFERENCE",
                    "UNVERIFIABLE",
                    "SLOT_TERMINAL_DECISION_NOT_CAPTURED",
                    slot.record_ref,
                    (slot.record_ref,),
                    slot.native_scope,
                    {"terminal_decision_identity": terminal_id},
                )
            )
            next_index += 1
            continue
        terminal_payload = _payload(terminal)
        conflicts = {}
        for key in ("campaign_id", "run_id", "slot_id", "terminal_decision_identity"):
            slot_value = row.get(key)
            terminal_value = terminal_payload.get(key) if key != "terminal_decision_identity" else terminal.native_id
            if slot_value is not None and terminal_value is not None and slot_value != terminal_value:
                conflicts[key] = {"slot": slot_value, "terminal": terminal_value}
        if row.get("verdict") is not None and terminal_payload.get("verdict") is not None and row["verdict"] != terminal_payload["verdict"]:
            conflicts["verdict"] = {"slot": row["verdict"], "terminal": terminal_payload["verdict"]}
        status = "INVALID" if conflicts else "VALID"
        if conflicts:
            record_integrity[id(slot)] = "INVALID"
            record_integrity[id(terminal)] = "INVALID"
        reference_edges.append(
            {
                "from_ref": slot.record_ref,
                "to_ref": terminal.record_ref,
                "edge_type": "SLOT_REFERENCES_TERMINAL_DECISION",
                "status": status,
                "reason_code": "SLOT_TERMINAL_DECISION_CONFLICT" if conflicts else "REFERENCE_PRESENT",
                "scope": slot.native_scope,
            }
        )
        if conflicts:
            findings.append(
                _finding(
                    next_index,
                    "IDENTITY",
                    "INVALID",
                    "SLOT_TERMINAL_DECISION_CONFLICT",
                    slot.record_ref,
                    (slot.record_ref, terminal.record_ref),
                    slot.native_scope,
                    {"conflicts": conflicts},
                )
            )
            next_index += 1

    for decision in raw.source_records:
        if decision.record_kind != "TASK3_TERMINAL_DECISIONS":
            continue
        payload = _payload(decision)
        refs: set[str] = set()
        refs.update(value for value in payload.get("evidence_ids", []) if isinstance(value, str))
        attribution = payload.get("attribution_result")
        if isinstance(attribution, dict):
            refs.update(value for value in attribution.get("evidence_ids", []) if isinstance(value, str))
        component_results = payload.get("component_results", [])
        if isinstance(component_results, list):
            for component in component_results:
                if isinstance(component, dict):
                    refs.update(value for value in component.get("evidence_ids", []) if isinstance(value, str))
        for evidence_id in sorted(refs):
            candidates = evidence_records.get(evidence_id, [])
            valid_candidates = [
                record
                for record in candidates
                if record.native_scope.get("run_id") == decision.native_scope.get("run_id")
                and record.native_scope.get("campaign_id") == decision.native_scope.get("campaign_id")
                and record.native_scope.get("interaction_id") == decision.native_scope.get("interaction_id")
                and record.native_scope.get("interaction_generation") == decision.native_scope.get("interaction_generation")
            ]
            if not candidates:
                edge_status = "BROKEN"
                reason = "DECISION_REFERENCE_MISSING"
            elif not valid_candidates:
                edge_status = "INVALID"
                reason = "DECISION_REFERENCE_SCOPE_MISMATCH"
            elif any(record_integrity[id(record)] == "INVALID" for record in valid_candidates):
                edge_status = "INVALID"
                reason = "REFERENCED_EVIDENCE_INVALID"
            else:
                edge_status = "VALID"
                reason = "REFERENCE_PRESENT"
            edge = {
                "from_ref": decision.record_ref,
                "to_record_id": evidence_id,
                "edge_type": "DECISION_SUPPORTS_EVIDENCE",
                "status": edge_status,
                "reason_code": reason,
                "scope": decision.native_scope,
            }
            reference_edges.append(edge)
            if edge_status != "VALID":
                findings.append(
                    _finding(
                        next_index,
                        "DECISION_REFERENCE",
                        "INVALID" if edge_status == "INVALID" else "UNVERIFIABLE",
                        reason,
                        decision.record_ref,
                        tuple(record.record_ref for record in candidates),
                        decision.native_scope,
                        {"referenced_record_id": evidence_id},
                    )
                )
                next_index += 1

    if any(source.source_kind == "TASK3_SQLITE" for source in raw.selected_sources):
        source_binding_records = [record for record in raw.source_records if record.record_kind == "SOURCE_BINDING"]
        if not source_binding_records:
            findings.append(
                _finding(
                    next_index,
                    "SOURCE_BINDING",
                    "UNVERIFIABLE",
                    "NOT_PROVEN_WITHIN_CAPTURE",
                    None,
                    (),
                    {"run_id": raw.requested_selector},
                    {"discovery_completeness": raw.discovery_scope.completeness},
                )
            )
            next_index += 1
        else:
            for record in source_binding_records:
                binding = record.data
                mismatches = {}
                required_pairs = (
                    ("actual_backend_path", "expected_backend_path"),
                    ("actual_backend_sha256", "expected_backend_sha256"),
                    ("actual_anyio_path", "expected_anyio_path"),
                    ("source_tree_sha256", "expected_source_tree_sha256"),
                    ("source_file_count", "expected_source_file_count"),
                )
                present_pairs = 0
                for actual_key, expected_key in required_pairs:
                    actual_value = binding.get(actual_key)
                    expected_value = binding.get(expected_key)
                    if actual_value is None or expected_value is None:
                        continue
                    present_pairs += 1
                    if actual_value != expected_value:
                        mismatches[actual_key] = {"observed": actual_value, "expected": expected_value}
                stored_errors = binding.get("binding_errors")
                if isinstance(stored_errors, list) and stored_errors:
                    mismatches["binding_errors"] = stored_errors
                binding_status = binding.get("binding_status")
                if binding_status not in {None, "PASS"}:
                    mismatches["binding_status"] = binding_status
                if record.native_scope.get("run_id") != raw.requested_selector:
                    mismatches["run_id"] = record.native_scope.get("run_id")
                observed_scope_values = {
                    "case_id": record.native_scope.get("case_id"),
                    "asset_identity": record.native_scope.get("asset_identity"),
                    "environment_identity": record.native_scope.get("environment_identity"),
                }
                for key, observed in observed_scope_values.items():
                    if observed is None:
                        continue
                    related_values = {
                        _payload(candidate).get(key)
                        for candidate in raw.source_records
                        if candidate.record_kind in {"TASK3_RUN_EVIDENCE", "TASK3_LIFECYCLE_EVIDENCE"}
                        and candidate.native_scope.get("run_id") == raw.requested_selector
                        and _payload(candidate).get(key) is not None
                    }
                    if related_values and observed not in related_values:
                        mismatches[key] = {"binding": observed, "captured_values": sorted(related_values)}
                if mismatches:
                    status = "INVALID"
                    reason = "SOURCE_BINDING_MISMATCH"
                else:
                    status = "UNVERIFIABLE"
                    reason = (
                        "PRODUCER_AUTHORITY_NOT_ESTABLISHED_WITHIN_CAPTURE"
                        if present_pairs == len(required_pairs) and binding_status == "PASS"
                        else "SOURCE_BINDING_FIELDS_INCOMPLETE"
                    )
                findings.append(
                    _finding(
                        next_index,
                        "SOURCE_BINDING",
                        status,
                        reason,
                        record.record_ref,
                        (record.record_ref,),
                        record.native_scope,
                        {"stored_binding_status": binding_status, "mismatches": mismatches},
                    )
                )
                next_index += 1

    invalid_findings = any(finding.status == "INVALID" for finding in findings)
    if raw.capture_status == "UNAVAILABLE":
        status = "UNAVAILABLE"
    elif invalid_findings:
        status = "INVALID"
    elif raw.discovery_scope.completeness != "DISCOVERY_COMPLETE" or any(
        finding.status in {"UNVERIFIABLE", "UNAVAILABLE"} for finding in findings
    ):
        status = "PARTIAL"
    else:
        status = "COMPLETE"

    coverage = {
        "discovery_scope_ref": raw.discovery_scope.capture_ref,
        "source_discovery_completeness": raw.discovery_scope.completeness,
        "source_discovery_basis": list(raw.discovery_scope.completeness_basis),
        "coverage_limitations": list(raw.discovery_scope.limitations),
        "record_count": len(raw.source_records),
        "verified_record_count": sum(value == "VALID" for value in record_integrity.values()),
        "invalid_record_count": sum(value == "INVALID" for value in record_integrity.values()),
        "source_gap_count": len(raw.source_gaps),
    }
    excluded = tuple(
        record.record_ref
        for record in raw.source_records
        if record_integrity[id(record)] == "INVALID"
    )
    return VerifiedAuditSources(
        raw.capture_id,
        raw.source_records,
        tuple(findings),
        tuple(reference_edges),
        (),
        excluded,
        coverage,
        status,
    )
