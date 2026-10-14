import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path

from code_governance.cli import main as changeguard_main


ROOT = Path(__file__).resolve().parent


def require(condition, message):
    if not condition:
        raise ValueError(message)


def capture(command, case, run_id):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        status = changeguard_main([
            command, run_id, "--repo", str(ROOT),
            "--source", str(ROOT / "data" / case / "campaign.sqlite3"),
            "--format", "json",
        ])
    envelope = json.loads(output.getvalue())
    require(
        (status, envelope["audit_status"]) in {(0, "COMPLETE"), (1, "PARTIAL")},
        f"CLI {command} cannot support this Demo: {envelope['audit_status']}",
    )
    return envelope


def load_case(case):
    manifest = json.loads((ROOT / "data" / "manifest.json").read_text(encoding="utf-8"))
    selected = manifest[case]
    for name, expected in selected["files"].items():
        actual = hashlib.sha256((ROOT / "data" / case / name).read_bytes()).hexdigest()
        require(actual == expected, f"Package digest mismatch: {case}/{name}")
    record = json.loads((ROOT / "data" / case / "record.json").read_text(encoding="utf-8"))
    require(record["run_id"] == selected["run_id"], "Packaged run identity differs")
    require(record["profile_id"] == selected["profile_id"], "Packaged asset profile differs")
    require(record["benchmark_implementation_commit"] == manifest["benchmark_implementation_commit"], "Benchmark provenance differs")
    audit = capture("audit", case, selected["run_id"])
    view = audit["payload"]
    require(view["identity"]["identity_integrity"] == "VALID", "Execution identity is invalid")
    require(len(view["decision"]) == 1, "Exactly one persisted decision is required")
    decision = view["decision"][0]
    require(decision["integrity_status"] == "VALID", "Terminal decision integrity is invalid")
    require(decision["stored_payload"] == record["terminal_decision"], "Run artifact differs from persisted decision")
    require(decision["stored_value"] == record["task_verdict"], "Stored verdict differs across records")
    require(view["identity"]["asset_identity"] == record["asset_identity"], "Artifact asset identity differs")
    edges = decision["support_assessment"]["reference_edges"]
    require(bool(edges) and all(edge["status"] == "VALID" for edge in edges), "Decision evidence references are incomplete")
    require(bool(view["evidence"]) and all(item["integrity_status"] == "VALID" for item in view["evidence"]), "Evidence integrity is invalid")
    require(not any(item["status"] in {"INVALID", "UNAVAILABLE"} for item in view["integrity_findings"]), "Audit contains invalid or unavailable records")
    boundaries = view["coverage"]["read_boundaries"]
    require(bool(boundaries) and all(item["sqlite_integrity_check"] == "PASS" for item in boundaries), "Database integrity check failed")
    persistence = view["persistence"]["assessments"]
    require(len(persistence) == 1 and persistence[0]["status"] == "COMMIT_RECORD_VERIFIED", "Committed decision receipt is unverified")
    require(persistence[0]["terminal_decision_identity"] == decision["decision_id"], "Commit receipt belongs to another decision")
    return audit, record


def evidence(view, kind):
    items = [item for item in view["evidence"] if item["evidence_type"] == kind]
    require(len(items) == 1, f"Exactly one {kind} record is required")
    return items[0]


def coverage(audit, view, replay=None):
    binding = [item for item in view["integrity_findings"] if item["category"] == "SOURCE_BINDING"]
    require(len(binding) == 1, "Source binding coverage is ambiguous")
    result = {
        "audit_status": audit["audit_status"],
        "source_binding": binding[0]["status"],
        "source_binding_reason": binding[0]["reason_code"],
        "producer_authority": evidence(view, "MEASUREMENT")["audit_trust_assessment"]["status"],
        "discovery": view["coverage"]["source_discovery_completeness"],
    }
    if replay is not None:
        result["ordering"] = replay["order_status"]
        result["boundary_selection"] = replay["first_failing_boundary"]["selection_status"]
    return result


def identity_lines(view):
    identity = view["identity"]
    return [
        "IDENTITY:", f"  campaign={identity['campaign_id']}",
        f"  slot={identity['slot_id']}", f"  run={identity['run_id']}",
        f"  generation={identity['interaction_generation']}",
    ]


def coverage_lines(value):
    return ["COVERAGE:"] + [f"  {key}={item}" for key, item in value.items()]


def success_output(audit):
    view = audit["payload"]
    decision = view["decision"][0]
    require(decision["stored_value"] == "PASS", "Success sample has no stored PASS verdict")
    components = decision["stored_payload"]["component_results"]
    require(bool(components) and all(item["verdict"] == "PASS" for item in components), "Success sample has failing components")
    lines = [f"RUN_STATUS={decision['stored_value']}", "", *identity_lines(view), "",
        "EVIDENCE_STATUS=VALID", "EVIDENCE_SCOPE=record_integrity_and_references", "",
        "VALIDATION:", "  execution_identity=PASS", "  artifact_binding=PASS", "  integrity=PASS", "",
        "EVIDENCE:", f"  measurement={evidence(view, 'MEASUREMENT')['evidence_id']}",
        f"  lifecycle={evidence(view, 'DIRECT_CHILD_LIFECYCLE')['evidence_id']}", "",
        "DECISION:", "  COMMITTED", f"  terminal={decision['decision_id']}", "",
        *coverage_lines(coverage(audit, view)),
    ]
    return "\n".join(lines)


def failure_context(audit, record):
    view = audit["payload"]
    decision = view["decision"][0]
    require(record["profile_id"] == "W2" and decision["stored_value"] == "FAIL", "W2 sample has no stored FAIL verdict")
    attribution = decision["stored_attribution"]
    require(attribution["category"] == "confirmed_rejection" and attribution["valid"] is True, "Stored W2 attribution is unverified")
    lifecycle = evidence(view, "DIRECT_CHILD_LIFECYCLE")["observed_fields"]
    require(lifecycle["os_query_raw_result"] == 258 and lifecycle["lifecycle_fact"] == "DIRECT_CHILD_LIVE_CONFIRMED", "Stored W2 observation differs")
    require(lifecycle["child_binding_status"] == "VALID", "Stored child binding is invalid")
    replay = capture("replay", "w2", view["identity"]["run_id"])["payload"]
    boundary = replay["first_failing_boundary"]
    require(boundary["boundary"] == "EARLIEST_OBSERVED_FAILURE" and boundary["candidate_boundary"] == "Lifecycle Observation", "Replay cannot establish the observed failure boundary")
    return replay


def failure_output(audit, replay):
    view = audit["payload"]
    decision = view["decision"][0]
    lifecycle = evidence(view, "DIRECT_CHILD_LIFECYCLE")
    defect = evidence(view, "REGISTERED_DEFECT")["observed_fields"]
    lines = ["CHANGEGUARD AUDIT", "", f"TASK_RESULT={decision['stored_value']}", "",
        *identity_lines(view), "", "OBSERVATION:",
        f"  raw_wait_result: WAIT_TIMEOUT={lifecycle['observed_fields']['os_query_raw_result']}",
        "  child_process: LIVE", "", "STORED_COMPONENT_RESULTS:",
    ]
    lines.extend(f"  {item['component_id']}={item['verdict']}" for item in decision["stored_payload"]["component_results"])
    lines.extend(["", "EVIDENCE:", "  execution_identity: VALID", "  artifact_binding: VALID",
        f"  lifecycle_evidence: {lifecycle['integrity_status']}",
        "  scope=record_integrity_and_references", "", "EVIDENCE_CHAIN:",
        f"  asset={view['identity']['asset_identity']}",
        f"  measurement={evidence(view, 'MEASUREMENT')['evidence_id']}",
        f"  child_binding={evidence(view, 'DIRECT_CHILD_BINDING')['evidence_id']}",
        f"  lifecycle={lifecycle['evidence_id']}",
        f"  registered_defect={defect['defect_id']}",
        f"  terminal={decision['decision_id']}", "  persistence=COMMITTED", "",
        "ATTRIBUTION:", f"  {decision['stored_attribution']['category']}",
        f"  support={decision['support_assessment']['status']}", "", "FAILURE_BOUNDARY:",
        f"  {replay['first_failing_boundary']['candidate_boundary']}", "",
        *coverage_lines(coverage(audit, view, replay)),
    ])
    return "\n".join(lines)


def replay_output(audit, replay):
    view = audit["payload"]
    decision = view["decision"][0]
    boundary = replay["first_failing_boundary"]
    return json.dumps({
        "TITLE": "AUDIT REPLAY",
        "SOURCE": "persisted execution records",
        "MODE": "READ_ONLY",
        "STORED_FACTS": {
            "TASK_VERDICT": decision["stored_value"],
            "run": view["identity"]["run_id"],
            "terminal_decision": decision["decision_id"],
        },
        "DERIVED_ANALYSIS": {
            "EARLIEST_OBSERVED_FAILURE": boundary["candidate_boundary"],
            "supporting_records": [item["native_locator"] for item in boundary["supporting_records"]],
        },
        "COVERAGE": coverage(audit, view, replay),
    }, ensure_ascii=False, indent=2)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Display existing ChangeGuard execution records")
    parser.add_argument("demo", choices=("success", "w2_failure", "audit_replay"))
    args = parser.parse_args(argv)
    audit, record = load_case("success" if args.demo == "success" else "w2")
    if args.demo == "success":
        output = success_output(audit)
    else:
        replay = failure_context(audit, record)
        output = failure_output(audit, replay) if args.demo == "w2_failure" else replay_output(audit, replay)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
