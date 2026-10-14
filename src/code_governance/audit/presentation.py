import json
from typing import Any, Optional


def render_json(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True)


def _safe(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)[1:-1]
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def render_human(
    command: str,
    audit_status: str,
    payload: dict[str, Any],
    boundary: Optional[dict[str, Any]] = None,
) -> str:
    lines = [f"COMMAND: {command}", f"AUDIT_STATUS: {audit_status}"]
    if command == "diff":
        lines.append(f"COMPARABILITY: {payload.get('comparability', 'NOT_COMPARABLE')}")
        lines.append(
            "FIRST_SEMANTIC_DIVERGENCE: "
            + str(payload.get("first_semantic_divergence", {}).get("status", "NOT_PROVEN"))
        )
        coverage = payload.get("coverage", {})
        for side in ("left", "right"):
            side_coverage = coverage.get(side, {})
            lines.append(
                f"{side.upper()}_DISCOVERY: "
                + str(side_coverage.get("source_discovery_completeness", "DISCOVERY_UNKNOWN"))
            )
        lines.append(f"COVERAGE_LIMITATIONS: {_safe(coverage.get('limitations', []))}")
        for item in payload.get("identity_metadata_differences", []):
            lines.append(f"INPUT_DIFFERENCE: {_safe(item)}")
        for domain in payload.get("domain_results", []):
            lines.append(f"DOMAIN {domain.get('domain')}: {domain.get('status')}")
            for difference in domain.get("differences", []):
                lines.append(f"  DIFFERENCE: {_safe(difference)}")
        return "\n".join(lines)

    snapshot = payload.get("snapshot", payload)
    coverage = snapshot.get("coverage", {})
    capture = snapshot.get("capture", {})
    identity = snapshot.get("identity", {})
    decisions = snapshot.get("decision", [])
    if decisions:
        decision = decisions[0]
        lines.append(
            "STORED_RESULT: "
            + _safe(decision.get("stored_value", "MISSING"))
            + f" owner={_safe(decision.get('owner'))} scope={_safe(decision.get('decision_scope'))}"
        )
    else:
        lines.append("STORED_RESULT: MISSING")
    lines.append(f"RUN_ID: {_safe(identity.get('run_id'))}")
    lines.append(f"SOURCE_DISCOVERY_COMPLETENESS: {coverage.get('source_discovery_completeness', 'DISCOVERY_UNKNOWN')}")
    lines.append(f"READ_CONSISTENCY: {capture.get('read_consistency', 'UNAVAILABLE')}")
    lines.append(f"COVERAGE_SCOPE: {_safe(coverage.get('discovery_scope', {}).get('scope_ref', {}))}")
    locations = coverage.get("included_locations", [])
    lines.append(f"INCLUDED_SOURCES: {_safe(locations)}")
    lines.append(f"NOT_GUARANTEED: {_safe(coverage.get('coverage_limitations', []))}")

    if boundary is not None:
        boundary_status = boundary.get("boundary", "NOT_PROVEN")
        if boundary_status == "EARLIEST_OBSERVED_FAILURE":
            lines.append(
                "EARLIEST_OBSERVED_FAILURE: "
                + str(boundary.get("candidate_boundary", "UNSUPPORTED_BOUNDARY"))
                + f" source={boundary.get('source')} selection_status={boundary.get('selection_status')}"
            )
        elif boundary_status == "MULTIPLE_MINIMAL":
            lines.append(
                "MULTIPLE_MINIMAL_FAILURES: "
                + f"count={len(boundary.get('alternatives', []))} selection_status={boundary.get('selection_status')}"
            )
        else:
            lines.append("FIRST_FAILING_BOUNDARY: NOT_PROVEN")
        lines.append(f"BOUNDARY_RECORDS: {_safe(boundary.get('supporting_records', []))}")
    else:
        lines.append("FIRST_FAILING_BOUNDARY: NOT_PROVEN")

    evidence = snapshot.get("evidence", [])
    trusted = [item for item in evidence if item.get("audit_trust_assessment", {}).get("status") == "VALID"]
    limited = [item for item in evidence if item.get("audit_trust_assessment", {}).get("status") != "VALID"]
    lines.append(f"TRUSTED_EVIDENCE: {len(trusted)} records")
    for item in trusted[:10]:
        lines.append(f"  {item.get('evidence_type')} id={_safe(item.get('evidence_id'))} refs={_safe(item.get('raw_refs', []))}")
    lines.append(f"UNVERIFIED_OR_EXCLUDED_EVIDENCE: {len(limited)} records")
    for item in limited[:10]:
        lines.append(
            f"  {item.get('evidence_type')} id={_safe(item.get('evidence_id'))} "
            f"integrity={item.get('integrity_status')} trust={item.get('audit_trust_assessment', {}).get('status')}"
        )
    lines.append(f"EVIDENCE_OMITTED_COUNT: {max(0, len(evidence) - 10)}")

    source_binding = [
        item for item in snapshot.get("integrity_findings", [])
        if item.get("category") == "SOURCE_BINDING"
    ]
    if source_binding:
        binding = source_binding[0]
        lines.append(
            f"SOURCE_FIXTURE_BINDING: {binding.get('status')} "
            f"reason={binding.get('reason_code')} scope={_safe(binding.get('scope'))}"
        )
    attribution = decisions[0].get("stored_attribution") if decisions else None
    if attribution is not None:
        support = decisions[0].get("support_assessment", {})
        lines.append(f"ATTRIBUTION: stored={_safe(attribution)} support={support.get('status')}")
    persistence = snapshot.get("persistence", {})
    assessments = persistence.get("assessments", [])
    lines.append(f"PERSISTENCE: {_safe(assessments)}")
    lines.append(f"DURABILITY_PROOF_LIMIT: {persistence.get('durability_claim', 'NOT_ASSESSED')}")

    if command == "replay":
        lines.append("REPLAY_HISTORY: CAPTURED_HISTORY_ONLY")
        lines.append(f"ORDER_STATUS: {payload.get('order_status', 'UNKNOWN_ORDER')}")
        lines.append(f"REPLAY_COVERAGE: {payload.get('coverage', {}).get('source_discovery_completeness', 'DISCOVERY_UNKNOWN')}")
    findings = snapshot.get("integrity_findings", [])
    lines.append(f"DIAGNOSTIC_COUNT: {len(findings)}")
    for finding in findings[:10]:
        lines.append(
            f"  {finding.get('category')} {finding.get('status')} "
            f"{finding.get('reason_code')} scope={_safe(finding.get('scope'))}"
        )
    lines.append(f"DIAGNOSTIC_OMITTED_COUNT: {max(0, len(findings) - 10)}")
    return "\n".join(lines)
