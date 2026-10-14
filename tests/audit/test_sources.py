import hashlib
import json
import shutil
from pathlib import Path

import apsw

from code_governance.audit.integrity import verify_audit_sources
from code_governance.audit.analysis import build_replay
from code_governance.audit.snapshot import build_audit_snapshot
from code_governance.audit.presentation import render_human
from code_governance.audit.sources import capture_run_sources


FIXTURES = Path(__file__).parent / "fixtures"
W2_RUN_ID = "w2-v6-0-0eade4ef33074bd88a4d988af8b691c5"


def _trace(repo: Path, run_id: str, events, timestamp: str = "2026-10-14T00:00:00Z") -> Path:
    path = repo / ".agent" / "runs" / run_id / "trace.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "schema_version": "v2",
                "runtime_version": "runtime-v1",
                "context_version": None,
                "events": [
                    {
                        "type": event_type,
                        "data": data,
                        "sequence": sequence,
                        "timestamp": timestamp,
                    }
                    for sequence, event_type, data in events
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_exact_run_directory_missing_is_unavailable_and_listed(tmp_path):
    raw = capture_run_sources("absent-run", tmp_path)
    verified = verify_audit_sources(raw)

    assert raw.capture_status == "UNAVAILABLE"
    assert raw.discovery_scope.completeness == "DISCOVERY_UNKNOWN"
    assert any(gap.code == "RUN_NOT_FOUND" for gap in raw.source_gaps)
    assert any(item["availability"] == "NOT_FOUND" for item in raw.discovery_scope.included_locations)
    assert verified.audit_status == "UNAVAILABLE"
    assert not any(finding.reason_code == "RUN_LEVEL_SOURCE_NOT_RECORDED" for finding in verified.findings)


def test_duplicate_json_keys_are_a_typed_invalid_source(tmp_path):
    run_id = "duplicate-json"
    path = tmp_path / "duplicate.json"
    path.write_text(
        '{"run_id":"duplicate-json","run_id":"other-run","records":[]}',
        encoding="utf-8",
    )

    raw = capture_run_sources(run_id, tmp_path, [path])
    verified = verify_audit_sources(raw)

    assert raw.capture_status == "INVALID"
    assert any(gap.code == "SOURCE_CORRUPT" for gap in raw.source_gaps)
    assert verified.audit_status == "INVALID"


def test_trace_run_identity_conflict_is_preserved_and_invalid(tmp_path):
    run_id = "selected-run"
    path = tmp_path / ".agent" / "runs" / run_id / "trace.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"run_id": "other-run", "schema_version": "v2", "events": []}),
        encoding="utf-8",
    )

    raw = capture_run_sources(run_id, tmp_path)
    verified = verify_audit_sources(raw)

    assert raw.capture_status == "INVALID"
    assert raw.selected_sources[0].native_identity["run_id"] == "other-run"
    assert any(gap.code == "IDENTITY_CONFLICT" for gap in raw.source_gaps)
    assert not any(gap.code == "RUN_NOT_FOUND" for gap in raw.source_gaps)
    assert verified.audit_status == "INVALID"


def test_trace_events_keep_native_sequence_and_unknown_record(tmp_path):
    run_id = "trace-run"
    _trace(
        tmp_path,
        run_id,
        [
            (1, "action_decided", {"case_id": "case-a", "action": "inspect"}),
            (2, "future_event", {"case_id": "case-a", "value": 7}),
        ],
    )

    raw = capture_run_sources(run_id, tmp_path)

    assert [record.record_kind for record in raw.source_records] == ["ACTION", "UNSUPPORTED_RECORD"]
    assert [record.native_id for record in raw.source_records] == ["1", "2"]
    assert raw.discovery_scope.completeness == "DISCOVERY_UNKNOWN"


def test_task3_sqlite_sidecar_is_refused_without_opening_database(tmp_path):
    database = tmp_path / "w2-run.sqlite3"
    shutil.copyfile(FIXTURES / "w2-run.sqlite3", database)
    sidecar = database.with_name(database.name + "-wal")
    sidecar.write_bytes(b"existing sidecar")
    before_database = database.read_bytes()
    before_sidecar = sidecar.read_bytes()

    raw = capture_run_sources(
        "w2-v6-0-0eade4ef33074bd88a4d988af8b691c5",
        tmp_path,
        [database],
    )

    assert any(gap.code == "READ_ONLY_PRECONDITION_FAILED" for gap in raw.source_gaps)
    assert database.read_bytes() == before_database
    assert sidecar.read_bytes() == before_sidecar
    assert not database.with_name(database.name + "-shm").exists()


def test_w2_sqlite_capture_records_raw_lifecycle_and_hash_boundaries(tmp_path):
    raw = capture_run_sources(
        "w2-v6-0-0eade4ef33074bd88a4d988af8b691c5",
        tmp_path,
        [FIXTURES / "w2-run.sqlite3"],
    )
    verified = verify_audit_sources(raw)

    lifecycle = next(record for record in raw.source_records if record.record_kind == "TASK3_LIFECYCLE_EVIDENCE")
    assert lifecycle.data["payload"]["os_query_raw_result"] == 258
    assert lifecycle.data["payload"]["lifecycle_fact"] == "DIRECT_CHILD_LIVE_CONFIRMED"
    assert any(finding.category == "SOURCE_BINDING" and finding.reason_code == "NOT_PROVEN_WITHIN_CAPTURE" for finding in verified.findings)
    assert not any(finding.status == "INVALID" for finding in verified.findings)


def _copy_w2_database(destination: Path) -> Path:
    database = destination / "w2-run.sqlite3"
    shutil.copyfile(FIXTURES / "w2-run.sqlite3", database)
    return database


def _change_lifecycle_scope(database: Path, **changes) -> str:
    connection = apsw.Connection(str(database))
    evidence_id, payload_json = next(
        connection.execute("SELECT evidence_id, payload_json FROM lifecycle_evidence")
    )
    payload = json.loads(payload_json)
    payload.update(changes)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connection.execute(
        "UPDATE lifecycle_evidence SET run_id=?, interaction_generation=?, payload_json=?, payload_hash=? WHERE evidence_id=?",
        (
            payload["run_id"],
            payload["interaction_generation"],
            encoded,
            hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            evidence_id,
        ),
    )
    connection.close()
    return evidence_id


def test_followed_evidence_from_another_run_is_preserved_and_invalid(tmp_path):
    database = _copy_w2_database(tmp_path)
    evidence_id = _change_lifecycle_scope(database, run_id="another-run")

    raw = capture_run_sources(W2_RUN_ID, tmp_path, [database])
    verified = verify_audit_sources(raw)
    snapshot = build_audit_snapshot(raw, verified)

    evidence = next(item for item in snapshot.evidence if item["evidence_id"] == evidence_id)
    assert evidence["subject_identity"]["run_id"] == "another-run"
    assert evidence["integrity_status"] == "INVALID"
    assert any(
        finding.reason_code == "IDENTITY_SCOPE_MISMATCH"
        for finding in verified.findings
    )
    assert any(
        edge["reason_code"] == "DECISION_REFERENCE_SCOPE_MISMATCH"
        and edge["to_record_id"] == evidence_id
        for edge in verified.reference_edges
    )
    assert not any(finding.reason_code == "RUN_LEVEL_SOURCE_NOT_RECORDED" for finding in verified.findings)


def test_followed_evidence_from_another_generation_is_invalid(tmp_path):
    database = _copy_w2_database(tmp_path)
    evidence_id = _change_lifecycle_scope(database, interaction_generation=2)

    raw = capture_run_sources(W2_RUN_ID, tmp_path, [database])
    verified = verify_audit_sources(raw)
    snapshot = build_audit_snapshot(raw, verified)

    evidence = next(item for item in snapshot.evidence if item["evidence_id"] == evidence_id)
    assert evidence["subject_identity"]["interaction_generation"] == 2
    assert evidence["integrity_status"] == "INVALID"
    assert any(
        edge["reason_code"] == "DECISION_REFERENCE_SCOPE_MISMATCH"
        and edge["to_record_id"] == evidence_id
        for edge in verified.reference_edges
    )


def test_identical_evidence_from_two_sources_keeps_both_refs_once(tmp_path):
    first = _copy_w2_database(tmp_path)
    second = tmp_path / "second-w2-run.sqlite3"
    shutil.copyfile(first, second)

    raw = capture_run_sources(W2_RUN_ID, tmp_path, [first, second])
    snapshot = build_audit_snapshot(raw, verify_audit_sources(raw))

    lifecycle = next(item for item in snapshot.evidence if item["evidence_type"] == "DIRECT_CHILD_LIFECYCLE")
    assert sum(item["evidence_id"] == lifecycle["evidence_id"] for item in snapshot.evidence) == 1
    assert len(lifecycle["raw_refs"]) == 2


def test_explicit_unreadable_source_is_unavailable_not_unrecorded(tmp_path):
    missing_source = tmp_path / "known-export.json"
    raw = capture_run_sources("run-with-known-export", tmp_path, [missing_source])
    verified = verify_audit_sources(raw)

    assert raw.selected_sources[0].availability == "SOURCE_UNAVAILABLE"
    assert any(
        item.get("locator") == str(missing_source)
        for item in raw.discovery_scope.known_unavailable_sources
    )
    assert any(finding.reason_code == "SOURCE_UNAVAILABLE" for finding in verified.findings)
    assert not any(finding.reason_code == "RUN_LEVEL_SOURCE_NOT_RECORDED" for finding in verified.findings)


def test_known_locations_without_exhaustive_inventory_remain_unknown(tmp_path):
    _trace(tmp_path, "inventory-unknown", [(1, "task_started", {"run_id": "inventory-unknown"})])

    raw = capture_run_sources("inventory-unknown", tmp_path)

    assert raw.discovery_scope.completeness == "DISCOVERY_UNKNOWN"
    assert not raw.discovery_scope.completeness_basis
    assert "INDEPENDENT_EXPORT_UNIVERSE_NOT_ENUMERATED" in raw.discovery_scope.limitations


def test_human_filter_keeps_all_records_in_snapshot_coverage(tmp_path):
    run_id = "twenty-records"
    _trace(
        tmp_path,
        run_id,
        [(sequence, "action_decided", {"run_id": run_id, "action": f"action-{sequence}"}) for sequence in range(1, 21)],
    )
    raw = capture_run_sources(run_id, tmp_path)
    snapshot = build_audit_snapshot(raw, verify_audit_sources(raw))

    output = render_human("audit", snapshot.audit_status, snapshot.to_dict())

    assert len(snapshot.execution) == 20
    assert snapshot.coverage["record_count"] == 20
    assert snapshot.coverage["source_discovery_completeness"] == "DISCOVERY_UNKNOWN"
    assert "EVIDENCE_OMITTED_COUNT: 10" in output


def test_unsupported_trace_schema_does_not_prove_source_absence(tmp_path):
    run_id = "old-trace-schema"
    trace = tmp_path / ".agent" / "runs" / run_id / "trace.json"
    trace.parent.mkdir(parents=True)
    trace.write_text(
        json.dumps({"run_id": run_id, "schema_version": "v1", "events": []}),
        encoding="utf-8",
    )

    raw = capture_run_sources(run_id, tmp_path)
    verified = verify_audit_sources(raw)

    assert any(finding.reason_code == "SCHEMA_UNSUPPORTED" for finding in verified.findings)
    assert raw.discovery_scope.completeness == "DISCOVERY_UNKNOWN"
    assert not any(finding.reason_code == "RUN_LEVEL_SOURCE_NOT_RECORDED" for finding in verified.findings)


def test_unversioned_summary_missing_binding_keeps_field_unverifiable(tmp_path):
    summary = FIXTURES / "w2-summary.json"
    raw = capture_run_sources(W2_RUN_ID, tmp_path, [summary])
    snapshot = build_audit_snapshot(raw, verify_audit_sources(raw))

    supplement = next(item for item in snapshot.evidence if item["evidence_type"] == "UNBOUND_SUPPLEMENT")
    binding_status = supplement["record_local_status"]["source_import_evidence"]
    assert binding_status["record_local_status"] == "FIELD_UNVERIFIABLE"
    assert binding_status["status_basis"]["reason_code"] == "SOURCE_SCHEMA_VERSION_UNAVAILABLE"
    assert snapshot.coverage["source_discovery_completeness"] == "DISCOVERY_UNKNOWN"


def _rewrite_terminal_payload(database: Path, **changes) -> str:
    connection = apsw.Connection(str(database))
    terminal_id, payload_json = next(
        connection.execute("SELECT terminal_decision_identity, payload_json FROM terminal_decisions")
    )
    payload = json.loads(payload_json)
    payload.update(changes)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connection.execute(
        "UPDATE terminal_decisions SET payload_json=?, payload_hash=? WHERE terminal_decision_identity=?",
        (encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), terminal_id),
    )
    connection.close()
    return terminal_id


def test_stored_pass_is_preserved_when_referenced_evidence_is_invalid(tmp_path):
    database = _copy_w2_database(tmp_path)
    evidence_id = _change_lifecycle_scope(database, run_id="another-run")
    _rewrite_terminal_payload(database, verdict="PASS")

    raw = capture_run_sources(W2_RUN_ID, tmp_path, [database])
    snapshot = build_audit_snapshot(raw, verify_audit_sources(raw))

    assert snapshot.decision[0]["stored_value"] == "PASS"
    invalid_evidence = next(item for item in snapshot.evidence if item["evidence_id"] == evidence_id)
    assert invalid_evidence["integrity_status"] == "INVALID"
    assert snapshot.audit_status == "INVALID"


def test_stored_decision_with_missing_evidence_reference_is_incomplete(tmp_path):
    database = _copy_w2_database(tmp_path)
    _rewrite_terminal_payload(
        database,
        evidence_ids=[
            "57776906a46e01fd92ffb6a8bec30574972b3ad125570f6c06c12f22d1895fa7",
            "af47ba6fb9a2629b9cb6aa8e30c875b02367a2412cf8ca57f70c0bb91a070bee",
            "missing-evidence-record",
        ],
    )

    raw = capture_run_sources(W2_RUN_ID, tmp_path, [database])
    verified = verify_audit_sources(raw)
    snapshot = build_audit_snapshot(raw, verified)

    assert snapshot.decision[0]["stored_value"] == "FAIL"
    assert snapshot.decision[0]["support_assessment"]["status"] == "INCOMPLETE"
    assert any(
        edge["reason_code"] == "DECISION_REFERENCE_MISSING"
        and edge["to_record_id"] == "missing-evidence-record"
        for edge in verified.reference_edges
    )


def test_late_wall_clock_diagnostic_does_not_create_causal_order(tmp_path):
    database = _copy_w2_database(tmp_path)
    connection = apsw.Connection(str(database))
    terminal_id = next(connection.execute("SELECT terminal_decision_identity FROM terminal_decisions"))[0]
    connection.execute(
        "INSERT INTO diagnostics VALUES (?, ?, ?, ?)",
        (
            "late-diagnostic",
            terminal_id,
            "late_observation",
            json.dumps({"recorded_at_utc": "9999-12-31T23:59:59Z"}),
        ),
    )
    connection.close()

    raw = capture_run_sources(W2_RUN_ID, tmp_path, [database])
    snapshot = build_audit_snapshot(raw, verify_audit_sources(raw))
    replay = build_replay(snapshot)
    diagnostic_ref = next(
        item["record_ref"] for item in snapshot.diagnostics
        if item.get("event_type") == "late_observation"
    )

    assert any(item["code"] == "TEMPORAL_RELATION_UNVERIFIABLE" for item in replay["diagnostics"])
    assert not any(
        edge["from_ref"] == diagnostic_ref or edge["to_ref"] == diagnostic_ref
        for edge in replay["precedence_edges"]
    )
