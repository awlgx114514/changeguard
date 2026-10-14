import json
import hashlib
from pathlib import Path

from code_governance.cli import main


FIXTURES = Path(__file__).parent / "fixtures"


def test_audit_missing_run_returns_unavailable_json(capsys, tmp_path):
    try:
        exit_code = main(
            [
                "audit",
                "missing-run",
                "--repo",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
    except SystemExit as error:
        exit_code = error.code

    captured = capsys.readouterr()
    assert exit_code == 3
    payload = json.loads(captured.out)
    assert payload["command"] == "audit"
    assert payload["audit_status"] == "UNAVAILABLE"
    assert payload["diagnostics"][0]["code"] == "RUN_NOT_FOUND"


def test_w2_audit_reads_persisted_facts_without_changing_database(capsys, tmp_path):
    summary = FIXTURES / "w2-summary.json"
    database = FIXTURES / "w2-run.sqlite3"
    database_files = (
        database,
        database.with_name(database.name + "-wal"),
        database.with_name(database.name + "-shm"),
        database.with_name(database.name + "-journal"),
    )
    before = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        for path in database_files
    }
    repository_before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    try:
        exit_code = main(
            [
                "audit",
                "w2-v6-0-0eade4ef33074bd88a4d988af8b691c5",
                "--repo",
                str(tmp_path),
                "--source",
                str(summary),
                "--source",
                str(database),
                "--format",
                "json",
            ]
        )
    except SystemExit as error:
        exit_code = error.code

    captured = capsys.readouterr()
    assert exit_code == 1
    envelope = json.loads(captured.out)
    assert envelope["audit_status"] == "PARTIAL"
    snapshot = envelope["payload"]
    decision = snapshot["decision"][0]
    assert decision["stored_value"] == "FAIL"
    assert decision["stored_attribution"]["valid"] is True
    lifecycle = next(
        item for item in snapshot["evidence"]
        if item["evidence_type"] == "DIRECT_CHILD_LIFECYCLE"
    )
    assert lifecycle["observed_fields"]["os_query_raw_result"] == 258
    assert lifecycle["observed_fields"]["lifecycle_fact"] == "DIRECT_CHILD_LIVE_CONFIRMED"
    source_binding = next(
        item for item in snapshot["integrity_findings"]
        if item["category"] == "SOURCE_BINDING"
    )
    assert source_binding["status"] == "UNVERIFIABLE"
    assert source_binding["reason_code"] == "NOT_PROVEN_WITHIN_CAPTURE"
    after = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        for path in database_files
    }
    assert after == before
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == repository_before
