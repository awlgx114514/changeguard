from collections import defaultdict
from typing import Any, Optional

from .models import AuditSnapshot


def _ref_key(reference: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(reference.get("source_id", "")),
        str(reference.get("native_locator", "")),
        str(reference.get("native_id", "")),
    )


def _record_nodes(snapshot: AuditSnapshot) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def append(reference: dict[str, Any], kind: str, scope: dict[str, Any], fields: dict[str, Any]) -> None:
        key = _ref_key(reference)
        if key in seen:
            return
        seen.add(key)
        nodes.append({"record_ref": reference, "record_kind": kind, "scope": scope, "stored_fields": fields})

    for item in snapshot.execution:
        fields = {
            **item.get("stored_fields", {}),
            "sequence": item.get("sequence"),
            "timestamp": item.get("timestamp"),
        }
        append(item["record_ref"], item["record_kind"], item.get("subject_identity") or snapshot.identity, fields)
    for item in snapshot.evidence:
        append(item["record_ref"], item["evidence_type"], item.get("subject_identity", {}), item.get("observed_fields", {}))
    for item in snapshot.decision:
        append(item["record_ref"], "STORED_DECISION", item.get("decision_scope", {}), item.get("stored_payload", {}))
    for item in snapshot.persistence.get("stored_records", []):
        append(item["record_ref"], "PERSISTENCE_RECORD", {}, item.get("row", {}))
    for item in snapshot.diagnostics:
        if item.get("origin") == "STORED" and item.get("record_ref") is not None:
            append(item["record_ref"], "STORED_DIAGNOSTIC", {}, item.get("stored_payload", {}))
    return sorted(nodes, key=lambda node: _ref_key(node["record_ref"]))


def _native_sequence(node: dict[str, Any]) -> Optional[tuple[str, str, int]]:
    reference = node["record_ref"]
    fields = node["stored_fields"]
    sequence = fields.get("sequence")
    if not isinstance(sequence, int):
        sequence = fields.get("observation_sequence")
    if not isinstance(sequence, int):
        return None
    scope = node["scope"]
    interaction = scope.get("interaction_id") or scope.get("run_id") or ""
    return str(reference.get("source_id", "")), str(interaction), sequence


def build_replay(snapshot: AuditSnapshot) -> dict[str, Any]:
    nodes = _record_nodes(snapshot)
    edges: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for node in nodes:
        sequence = _native_sequence(node)
        if sequence is not None:
            source_id, interaction, order = sequence
            groups[(source_id, interaction)].append((order, node))
    for (source_id, interaction), items in sorted(groups.items()):
        ordered = sorted(items, key=lambda item: (item[0], _ref_key(item[1]["record_ref"])))
        for (left_order, left), (right_order, right) in zip(ordered, ordered[1:]):
            if left_order >= right_order:
                continue
            edges.append(
                {
                    "from_ref": left["record_ref"],
                    "to_ref": right["record_ref"],
                    "rule": "NATIVE_SOURCE_SEQUENCE",
                    "rule_version": "1",
                    "supporting_refs": [left["record_ref"], right["record_ref"]],
                    "scope": {"source_id": source_id, "interaction_id": interaction},
                }
            )

    prepared = [
        node for node in nodes
        if node["record_kind"] == "PERSISTENCE_RECORD"
        and node["stored_fields"].get("phase") == "COMMITTED"
    ]
    terminal_by_identity = {
        item["decision_id"]: item
        for item in snapshot.decision
    }
    for record in prepared:
        identity = record["stored_fields"].get("terminal_decision_identity")
        decision = terminal_by_identity.get(identity)
        if decision is None:
            continue
        prepared_hash = record["stored_fields"].get("payload_hash")
        decision_ref = decision.get("record_ref")
        if prepared_hash != decision.get("stored_record", {}).get("payload_hash"):
            continue
        edges.append(
            {
                "from_ref": record["record_ref"],
                "to_ref": decision_ref,
                "rule": "TASK3_COMMIT_RECORD",
                "rule_version": "1",
                "supporting_refs": [record["record_ref"], decision_ref],
                "scope": decision.get("decision_scope", {}),
            }
        )

    by_key = {_ref_key(node["record_ref"]): node for node in nodes}
    adjacency: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {
        key: set() for key in by_key
    }
    incoming: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {
        key: set() for key in by_key
    }
    for edge in edges:
        source = _ref_key(edge["from_ref"])
        target = _ref_key(edge["to_ref"])
        adjacency[source].add(target)
        incoming[target].add(source)

    remaining = set(by_key)
    ordered_groups: list[dict[str, Any]] = []
    while remaining:
        current = sorted(
            (key for key in remaining if not (incoming[key] & remaining)),
            key=lambda key: key,
        )
        if not current:
            return {
                "snapshot_ref": snapshot.capture.get("capture_id"),
                "nodes": nodes,
                "precedence_edges": edges,
                "ordered_groups": ordered_groups,
                "order_status": "ORDER_CONFLICT",
                "coverage": snapshot.coverage,
                "diagnostics": [{"code": "ORDER_CONFLICT", "origin": "DERIVED"}],
            }
        ordered_groups.append(
            {
                "records": [by_key[key]["record_ref"] for key in current],
                "display_order": "NON_CAUSAL_DISPLAY_ORDER" if len(current) > 1 else "CAUSAL_GROUP",
            }
        )
        remaining.difference_update(current)

    completeness = snapshot.coverage.get("source_discovery_completeness")
    all_groups_singleton = all(len(group["records"]) == 1 for group in ordered_groups)
    if completeness == "DISCOVERY_COMPLETE" and nodes and all_groups_singleton:
        order_status = "ORDERED_WITHIN_SCOPE"
    elif edges:
        order_status = "PARTIAL_ORDER"
    else:
        order_status = "UNKNOWN_ORDER"
    diagnostics = []
    if completeness != "DISCOVERY_COMPLETE":
        diagnostics.append({"code": "PARTIAL_HISTORY", "origin": "DERIVED"})
    if any(item.get("event_type") for item in snapshot.diagnostics):
        diagnostics.append({"code": "TEMPORAL_RELATION_UNVERIFIABLE", "origin": "DERIVED"})
    return {
        "snapshot_ref": snapshot.capture.get("capture_id"),
        "nodes": nodes,
        "precedence_edges": edges,
        "ordered_groups": ordered_groups,
        "order_status": order_status,
        "coverage": snapshot.coverage,
        "diagnostics": diagnostics,
    }


def first_failing_boundary(snapshot: AuditSnapshot) -> dict[str, Any]:
    candidates = []
    evidence_by_id = {item.get("evidence_id"): item for item in snapshot.evidence}
    for decision in snapshot.decision:
        if decision.get("integrity_status") == "INVALID":
            continue
        payload = decision.get("stored_payload", {})
        components = payload.get("component_results", [])
        for component in components if isinstance(components, list) else []:
            if not isinstance(component, dict) or component.get("verdict") != "FAIL":
                continue
            evidence_ids = [value for value in component.get("evidence_ids", []) if isinstance(value, str)]
            related_evidence = [evidence_by_id[value] for value in evidence_ids if value in evidence_by_id]
            if not related_evidence or any(item.get("integrity_status") == "INVALID" for item in related_evidence):
                continue
            evidence_refs = []
            for evidence in related_evidence:
                evidence_refs.extend(evidence.get("raw_refs", []))
            component_id = str(component.get("component_id", ""))
            if "direct_child_lifecycle" in component_id:
                if not any(item.get("evidence_type") == "DIRECT_CHILD_LIFECYCLE" for item in related_evidence):
                    continue
                boundary = "Lifecycle Observation"
            elif component_id.endswith(":deadline"):
                boundary = "Deadline Check"
            elif component_id.endswith(":completion_record"):
                boundary = "Completion Record"
            else:
                boundary = "UNSUPPORTED_BOUNDARY"
            candidates.append(
                {
                    "boundary": boundary,
                    "source": "AUDIT_DERIVED",
                    "owner_semantics": "TASK3_STORED_COMPONENT_RESULT",
                    "selection_status": "PARTIAL_HISTORY",
                    "scope": decision.get("decision_scope", {}),
                    "supporting_records": evidence_refs + [decision.get("record_ref")],
                    "upstream_state": {"stored_component_verdict": "FAIL"},
                    "failure_fact": {
                        "component_id": component.get("component_id"),
                        "verdict": component.get("verdict"),
                    },
                    "downstream_effect": "STORED_TERMINAL_DECISION_REMAINS_UNCHANGED",
                    "alternatives": [],
                    "contradictions": [],
                    "support_assessment": decision.get("support_assessment", {}),
                    "coverage": snapshot.coverage,
                }
            )
    if len(candidates) == 1:
        candidate = candidates[0]
        candidate["candidate_boundary"] = candidate["boundary"]
        candidate["boundary"] = "EARLIEST_OBSERVED_FAILURE"
        return candidate
    if candidates:
        return {
            "boundary": "MULTIPLE_MINIMAL",
            "source": "AUDIT_DERIVED",
            "owner_semantics": "STRUCTURED_COMPONENT_FAILURES",
            "selection_status": "PARTIAL_HISTORY",
            "scope": snapshot.identity,
            "supporting_records": [ref for candidate in candidates for ref in candidate["supporting_records"]],
            "upstream_state": "PARTIAL_HISTORY",
            "failure_fact": None,
            "downstream_effect": "NOT_PROVEN",
            "alternatives": candidates,
            "contradictions": [],
            "coverage": snapshot.coverage,
        }
    return {
        "boundary": "NOT_PROVEN",
        "source": "AUDIT_DERIVED",
        "owner_semantics": "NO_STRUCTURED_FAILURE_CANDIDATE_CAPTURED",
        "selection_status": "PARTIAL_HISTORY",
        "scope": snapshot.identity,
        "supporting_records": [],
        "upstream_state": "MISSING",
        "failure_fact": None,
        "downstream_effect": "NOT_PROVEN",
        "alternatives": [],
        "contradictions": [],
        "coverage": snapshot.coverage,
    }


def _domain(name: str, left: AuditSnapshot, right: AuditSnapshot, differences: list[dict[str, Any]], status: str) -> dict[str, Any]:
    return {
        "domain": name,
        "status": status,
        "left_coverage_ref": left.coverage.get("discovery_scope_ref"),
        "right_coverage_ref": right.coverage.get("discovery_scope_ref"),
        "differences": differences,
    }


def build_semantic_diff(left: AuditSnapshot, right: AuditSnapshot) -> dict[str, Any]:
    identity_keys = (
        "case_id",
        "interaction_id",
        "interaction_generation",
        "fixture_identity",
        "source_identity",
        "semantic_spec_identity",
        "benchmark_contract_identity",
        "scoring_contract_identity",
        "calibration_campaign_identity",
        "deadline_formula_identity",
        "asset_identity",
        "environment_identity",
        "environment_profile_identity",
        "formal_profile_identity",
        "accepted_asset_identities",
        "profile_catalog_version",
        "profile_catalog_hash",
        "profile_id",
        "profile_type",
    )
    identity_differences = [
        {"field": key, "left": left.identity.get(key), "right": right.identity.get(key)}
        for key in identity_keys
        if left.identity.get(key) != right.identity.get(key)
    ]
    same_case = bool(left.identity.get("case_id")) and left.identity.get("case_id") == right.identity.get("case_id")
    same_interaction = (
        left.identity.get("interaction_id") is not None
        and left.identity.get("interaction_id") == right.identity.get("interaction_id")
    )
    same_generation = (
        left.identity.get("interaction_generation") is not None
        and left.identity.get("interaction_generation") == right.identity.get("interaction_generation")
    )
    task_semantic_keys = (
        "semantic_spec_identity",
        "benchmark_contract_identity",
        "scoring_contract_identity",
        "calibration_campaign_identity",
        "deadline_formula_identity",
        "environment_profile_identity",
        "formal_profile_identity",
        "accepted_asset_identities",
        "profile_catalog_version",
        "profile_catalog_hash",
        "profile_id",
        "profile_type",
    )
    task_semantics_known = all(left.identity.get(key) is not None and right.identity.get(key) is not None for key in task_semantic_keys)
    same_task_semantics = task_semantics_known and all(
        left.identity.get(key) == right.identity.get(key)
        for key in task_semantic_keys
    )
    same_asset = (
        left.identity.get("asset_identity") is not None
        and left.identity.get("asset_identity") == right.identity.get("asset_identity")
    )
    same_fixture = (
        left.identity.get("fixture_identity") is not None
        and left.identity.get("fixture_identity") == right.identity.get("fixture_identity")
    )
    same_source_identity = (
        left.identity.get("source_identity") is not None
        and left.identity.get("source_identity") == right.identity.get("source_identity")
    )
    same_environment = (
        left.identity.get("environment_identity") is not None
        and left.identity.get("environment_identity") == right.identity.get("environment_identity")
    )
    def source_binding_status(snapshot: AuditSnapshot) -> str:
        bindings = [
            item for item in snapshot.integrity_findings
            if item.get("category") == "SOURCE_BINDING"
        ]
        return bindings[0].get("status", "UNVERIFIABLE") if bindings else "UNVERIFIABLE"

    same_source_binding = (
        source_binding_status(left) == "VALID"
        and source_binding_status(right) == "VALID"
    )
    complete = (
        left.coverage.get("source_discovery_completeness") == "DISCOVERY_COMPLETE"
        and right.coverage.get("source_discovery_completeness") == "DISCOVERY_COMPLETE"
    )
    comparable_subject = same_case and same_interaction and same_generation and same_task_semantics
    comparable_execution = (
        comparable_subject
        and same_asset
        and same_fixture
        and same_source_identity
        and same_environment
        and same_source_binding
    )
    domains: list[dict[str, Any]] = []
    domains.append(_domain("IDENTITY", left, right, identity_differences, "PARTIALLY_COMPARABLE" if same_case else "NOT_COMPARABLE"))

    execution_differences = []
    left_events = defaultdict(list)
    right_events = defaultdict(list)
    for item in left.execution:
        left_events[item.get("event_type")].append(item.get("stored_fields", {}))
    for item in right.execution:
        right_events[item.get("event_type")].append(item.get("stored_fields", {}))
    for event_type in sorted(set(left_events) | set(right_events)):
        left_values = left_events.get(event_type, [])
        right_values = right_events.get(event_type, [])
        if left_values != right_values:
            execution_differences.append({"event_type": event_type, "left": left_values, "right": right_values})
    domains.append(_domain("EXECUTION", left, right, execution_differences, "PARTIALLY_COMPARABLE" if comparable_execution else "NOT_COMPARABLE"))

    evidence_differences = []
    def evidence_map(snapshot: AuditSnapshot) -> dict[tuple[str, str, str], dict[str, Any]]:
        result = {}
        for item in snapshot.evidence:
            subject = item.get("subject_identity", {})
            role = str(subject.get("case_id", "")) + ":" + str(subject.get("interaction_id", ""))
            key = (str(item.get("evidence_type")), role, str(subject.get("interaction_generation", "")))
            if key not in result:
                result[key] = item
        return result
    left_evidence = evidence_map(left)
    right_evidence = evidence_map(right)
    for key in sorted(set(left_evidence) | set(right_evidence)):
        left_item = left_evidence.get(key)
        right_item = right_evidence.get(key)
        if left_item is None or right_item is None:
            evidence_differences.append({"subject": key, "difference_type": "COVERAGE_DIFFERENCE"})
            continue
        fields = ("lifecycle_fact", "os_query_raw_result", "query_order_status", "completion_kind", "semantic_timeout", "observation_state")
        observed = {
            field: {"left": left_item.get("observed_fields", {}).get(field), "right": right_item.get("observed_fields", {}).get(field)}
            for field in fields
            if left_item.get("observed_fields", {}).get(field) != right_item.get("observed_fields", {}).get(field)
        }
        if observed:
            evidence_differences.append({"subject": key, "difference_type": "OBSERVED_FACT_DIFFERENCE", "fields": observed})
        audit_assessments = {
            "integrity_status": {"left": left_item.get("integrity_status"), "right": right_item.get("integrity_status")},
            "trust_status": {
                "left": left_item.get("audit_trust_assessment", {}).get("status"),
                "right": right_item.get("audit_trust_assessment", {}).get("status"),
            },
        }
        if audit_assessments["integrity_status"]["left"] != audit_assessments["integrity_status"]["right"] or audit_assessments["trust_status"]["left"] != audit_assessments["trust_status"]["right"]:
            evidence_differences.append({"subject": key, "difference_type": "INTEGRITY_OR_TRUST_DIFFERENCE", "assessments": audit_assessments})
    domains.append(_domain("EVIDENCE", left, right, evidence_differences, "PARTIALLY_COMPARABLE" if comparable_execution else "NOT_COMPARABLE"))

    left_decisions = {str(item.get("decision_scope", {}).get("case_id")): item for item in left.decision}
    right_decisions = {str(item.get("decision_scope", {}).get("case_id")): item for item in right.decision}
    decision_differences = []
    for key in sorted(set(left_decisions) | set(right_decisions)):
        a = left_decisions.get(key)
        b = right_decisions.get(key)
        if a is None or b is None:
            decision_differences.append({"scope": key, "difference_type": "COVERAGE_DIFFERENCE"})
        else:
            a_payload = a.get("stored_payload", {})
            b_payload = b.get("stored_payload", {})
            a_components = {
                item.get("component_id"): item.get("verdict")
                for item in a_payload.get("component_results", [])
                if isinstance(item, dict)
            }
            b_components = {
                item.get("component_id"): item.get("verdict")
                for item in b_payload.get("component_results", [])
                if isinstance(item, dict)
            }
            left_attribution = a.get("stored_attribution") or {}
            right_attribution = b.get("stored_attribution") or {}
            left_semantics = {
                "stored_value": a.get("stored_value"),
                "component_results": a_components,
                "attribution": {
                    "category": left_attribution.get("category"),
                    "valid": left_attribution.get("valid"),
                },
                "support_status": a.get("support_assessment", {}).get("status"),
            }
            right_semantics = {
                "stored_value": b.get("stored_value"),
                "component_results": b_components,
                "attribution": {
                    "category": right_attribution.get("category"),
                    "valid": right_attribution.get("valid"),
                },
                "support_status": b.get("support_assessment", {}).get("status"),
            }
            if left_semantics != right_semantics:
                decision_differences.append({"scope": key, "left": left_semantics, "right": right_semantics})
    domains.append(_domain("DECISION", left, right, decision_differences, "PARTIALLY_COMPARABLE" if comparable_execution else "NOT_COMPARABLE"))

    left_phases = [item.get("stored_phase") for item in left.persistence.get("assessments", [])]
    right_phases = [item.get("stored_phase") for item in right.persistence.get("assessments", [])]
    persistence_differences = [] if left_phases == right_phases else [{"left": left_phases, "right": right_phases}]
    domains.append(_domain("PERSISTENCE", left, right, persistence_differences, "PARTIALLY_COMPARABLE" if comparable_execution else "NOT_COMPARABLE"))

    left_codes = sorted(item.get("event_type") or item.get("code") for item in left.diagnostics if item.get("event_type") or item.get("code"))
    right_codes = sorted(item.get("event_type") or item.get("code") for item in right.diagnostics if item.get("event_type") or item.get("code"))
    diagnostic_differences = [] if left_codes == right_codes else [{"left": left_codes, "right": right_codes}]
    domains.append(_domain("DIAGNOSTICS", left, right, diagnostic_differences, "PARTIALLY_COMPARABLE" if comparable_subject else "NOT_COMPARABLE"))

    semantic_differences = [
        {"domain": domain["domain"], **difference}
        for domain in domains
        for difference in domain["differences"]
        if difference.get("difference_type") != "COVERAGE_DIFFERENCE"
    ]
    comparability = "PARTIALLY_COMPARABLE" if comparable_subject else "NOT_COMPARABLE"
    if complete and comparable_execution and all(domain["status"] != "NOT_COMPARABLE" for domain in domains):
        comparability = "FULLY_COMPARABLE"
    if not semantic_differences and complete and comparability == "FULLY_COMPARABLE":
        divergence_status = "NO_DIFFERENCE_WITHIN_COVERAGE"
    else:
        divergence_status = "NOT_PROVEN"
    return {
        "left_snapshot_ref": left.capture.get("capture_id"),
        "right_snapshot_ref": right.capture.get("capture_id"),
        "comparability": comparability,
        "preconditions": {
            "same_case_identity": same_case,
            "same_interaction_identity": same_interaction,
            "same_interaction_generation": same_generation,
            "task_semantics_known": task_semantics_known,
            "same_task_semantics": same_task_semantics,
            "same_asset_identity": same_asset,
            "same_fixture_identity": same_fixture,
            "same_source_identity": same_source_identity,
            "same_environment_identity": same_environment,
            "left_source_binding_status": source_binding_status(left),
            "right_source_binding_status": source_binding_status(right),
            "source_variation_relation": "UNVERIFIABLE_WITHIN_CAPTURE" if not same_source_binding else "VERIFIED",
            "left_discovery": left.coverage.get("source_discovery_completeness"),
            "right_discovery": right.coverage.get("source_discovery_completeness"),
            "common_prefix_complete": complete,
            "unique_correspondence": False,
        },
        "domain_results": domains,
        "identity_metadata_differences": identity_differences,
        "semantic_differences": semantic_differences,
        "first_semantic_divergence": {
            "status": divergence_status,
            "candidates": semantic_differences if divergence_status == "EARLIEST_OBSERVED_WITHIN_COVERAGE" else [],
            "scope": "COMMON_CAPTURED_COVERAGE",
        },
        "coverage": {
            "left": left.coverage,
            "right": right.coverage,
            "limitations": ["INDEPENDENT_EXPORT_UNIVERSE_NOT_ENUMERATED"] if not complete else [],
        },
        "diagnostics": [] if complete else [{"code": "PARTIAL_HISTORY", "origin": "DERIVED"}],
    }
