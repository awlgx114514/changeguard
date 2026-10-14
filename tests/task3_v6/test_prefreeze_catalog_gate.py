from benchmarks.task3_v6.profile_catalog import load_profile_catalog
from benchmarks.task3_v6.acceptance.catalog_prefreeze_gate import (
    _asset_manifest,
    evaluate_catalog_prefreeze,
)


def test_empty_evidence_root_cannot_complete_profile_coverage(tmp_path):
    catalog = load_profile_catalog()

    result = evaluate_catalog_prefreeze(catalog, tmp_path, expected_run_count=8)

    assert result["coverage"].coverage_status == "INCOMPLETE"
    assert result["coverage"].required_correct_completed == 0
    assert result["coverage"].required_wrong_completed == 0
    assert set(result["coverage"].required_correct_missing) == {
        "KNOWN_FIX",
        "CA_DIFFERENT_TRANSPORT_HELPER",
        "CA_LEGAL_CLEANUP_ORDERING",
        "CA_LATE_CACHE_UPDATE",
        "CA_ASYNC_CLOSED_STREAM_PROBE",
        "CA_BUFFERING_SPOOLING",
    }
    assert set(result["coverage"].required_wrong_missing) == {
        "W1",
        "W2",
        "W3",
        "W4",
        "W7",
        "DESTRUCTIVE_WAIT",
        "WAIT_FOR_DESCENDANT_EOF",
    }


def test_wrong_profile_manifest_accepts_only_registered_target_failures():
    catalog = load_profile_catalog()

    result = _asset_manifest("W1", catalog)

    assert result is not None
    _manifest_path, manifest, binding_errors = result
    assert binding_errors == []
    assert manifest["source_acceptance"]["public_subprocess_suite"]["result"] == (
        "TARGET_SEMANTIC_DEFECT_ONLY"
    )


def test_wrong_profile_manifest_accepts_public_suite_pass_with_lifecycle_defect():
    catalog = load_profile_catalog()

    result = _asset_manifest("W2", catalog)

    assert result is not None
    _manifest_path, manifest, binding_errors = result
    assert binding_errors == []
    assert manifest["source_acceptance"]["public_subprocess_suite"] == (
        "54 passed, 34 skipped, 0 failed"
    )


def test_w3_manifest_accepts_only_its_registered_public_close_failures():
    catalog = load_profile_catalog()

    result = _asset_manifest("W3", catalog)

    assert result is not None
    _manifest_path, manifest, binding_errors = result
    assert binding_errors == []
    assert manifest["source_acceptance"]["public_subprocess_suite"]["expected_failures"] == [
        "test_close_with_stdout_blocked_subprocess[asyncio]",
        "test_close_with_stdout_blocked_subprocess[asyncio+eager]",
    ]


