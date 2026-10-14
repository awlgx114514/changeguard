import json
from pathlib import Path

from code_governance.cli import main


FIXTURES = Path(__file__).parent / "fixtures"


def _write_trace(repository: Path, run_id: str, timestamp: str, action: str = "inspect"):
    path = repository / ".agent" / "runs" / run_id / "trace.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "schema_version": "v2",
                "events": [
                    {
                        "type": "action_decided",
                        "data": {"case_id": "case-a", "action": action},
                        "sequence": 1,
                        "timestamp": timestamp,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_w2_replay_preserves_stored_boundary_and_partial_order(capsys, tmp_path):
    try:
        exit_code = main(
            [
                "replay",
                "w2-v6-0-0eade4ef33074bd88a4d988af8b691c5",
                "--repo",
                str(tmp_path),
                "--source",
                str(FIXTURES / "w2-summary.json"),
                "--source",
                str(FIXTURES / "w2-run.sqlite3"),
                "--format",
                "json",
            ]
        )
    except SystemExit as error:
        exit_code = error.code

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["audit_status"] == "PARTIAL"
    assert result["payload"]["order_status"] == "PARTIAL_ORDER"
    assert result["payload"]["first_failing_boundary"]["boundary"] == "EARLIEST_OBSERVED_FAILURE"
    assert result["payload"]["first_failing_boundary"]["candidate_boundary"] == "Lifecycle Observation"
    assert result["payload"]["first_failing_boundary"]["selection_status"] == "PARTIAL_HISTORY"
    assert any(edge["rule"] == "TASK3_COMMIT_RECORD" for edge in result["payload"]["precedence_edges"])


def test_diff_ignores_timestamp_only_changes_and_keeps_prefix_unknown(capsys, tmp_path):
    _write_trace(tmp_path, "run-a", "2026-10-14T00:00:00Z")
    _write_trace(tmp_path, "run-b", "2026-10-14T00:00:08Z")
    try:
        exit_code = main(["diff", "run-a", "run-b", "--repo", str(tmp_path), "--format", "json"])
    except SystemExit as error:
        exit_code = error.code

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["payload"]["comparability"] == "NOT_COMPARABLE"
    assert result["payload"]["semantic_differences"] == []
    assert result["payload"]["first_semantic_divergence"]["status"] == "NOT_PROVEN"


def test_diff_reports_event_difference_without_global_divergence(capsys, tmp_path):
    _write_trace(tmp_path, "run-a", "2026-10-14T00:00:00Z", "inspect")
    _write_trace(tmp_path, "run-b", "2026-10-14T00:00:00Z", "modify")
    try:
        exit_code = main(["diff", "run-a", "run-b", "--repo", str(tmp_path), "--format", "json"])
    except SystemExit as error:
        exit_code = error.code

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["payload"]["comparability"] == "NOT_COMPARABLE"
    assert result["payload"]["semantic_differences"]
    assert result["payload"]["first_semantic_divergence"]["status"] == "NOT_PROVEN"


def test_human_output_names_discovery_and_source_binding_limitations(capsys, tmp_path):
    try:
        exit_code = main(
            [
                "audit",
                "w2-v6-0-0eade4ef33074bd88a4d988af8b691c5",
                "--repo",
                str(tmp_path),
                "--source",
                str(FIXTURES / "w2-summary.json"),
                "--source",
                str(FIXTURES / "w2-run.sqlite3"),
            ]
        )
    except SystemExit as error:
        exit_code = error.code

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "STORED_RESULT: FAIL" in output
    assert "SOURCE_DISCOVERY_COMPLETENESS: DISCOVERY_UNKNOWN" in output
    assert "SOURCE_FIXTURE_BINDING: UNVERIFIABLE reason=NOT_PROVEN_WITHIN_CAPTURE" in output
    assert "EARLIEST_OBSERVED_FAILURE: Lifecycle Observation" in output


def test_human_and_json_keep_the_same_stored_result_and_coverage(capsys, tmp_path):
    arguments = [
        "audit",
        "w2-v6-0-0eade4ef33074bd88a4d988af8b691c5",
        "--repo",
        str(tmp_path),
        "--source",
        str(FIXTURES / "w2-summary.json"),
        "--source",
        str(FIXTURES / "w2-run.sqlite3"),
    ]
    main([*arguments, "--format", "json"])
    json_output = json.loads(capsys.readouterr().out)
    main([*arguments, "--format", "human"])
    human_output = capsys.readouterr().out

    assert json_output["payload"]["decision"][0]["stored_value"] == "FAIL"
    assert json_output["payload"]["coverage"]["source_discovery_completeness"] == "DISCOVERY_UNKNOWN"
    assert "STORED_RESULT: FAIL" in human_output
    assert "SOURCE_DISCOVERY_COMPLETENESS: DISCOVERY_UNKNOWN" in human_output
    assert "EARLIEST_OBSERVED_FAILURE: Lifecycle Observation" in human_output


def test_diff_reports_different_fixture_as_input_difference(capsys, tmp_path):
    source = json.loads((FIXTURES / "w2-summary.json").read_text(encoding="utf-8"))
    left = json.loads(json.dumps(source))
    right = json.loads(json.dumps(source))
    left["run_count"] = 1
    right["run_count"] = 1
    left_record = dict(left["results"][0])
    right_record = dict(right["results"][0])
    left_record.update(
        {
            "run_id": "run-a",
            "case_id": "same-case",
            "interaction_id": "same-interaction",
            "interaction_generation": 1,
            "fixture_identity": "fixture-left",
        }
    )
    right_record.update(
        {
            "run_id": "run-b",
            "case_id": "same-case",
            "interaction_id": "same-interaction",
            "interaction_generation": 1,
            "fixture_identity": "fixture-right",
        }
    )
    left["results"] = [left_record]
    right["results"] = [right_record]
    left_path = tmp_path / "left-summary.json"
    right_path = tmp_path / "right-summary.json"
    left_path.write_text(json.dumps(left), encoding="utf-8")
    right_path.write_text(json.dumps(right), encoding="utf-8")

    try:
        exit_code = main(
            [
                "diff",
                "run-a",
                "run-b",
                "--repo",
                str(tmp_path),
                "--source",
                str(left_path),
                "--source",
                str(right_path),
                "--format",
                "json",
            ]
        )
    except SystemExit as error:
        exit_code = error.code

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert any(item["field"] == "fixture_identity" for item in result["payload"]["identity_metadata_differences"])
    assert result["payload"]["first_semantic_divergence"]["status"] == "NOT_PROVEN"
