import json

from code_governance.evaluation.ptc_compare import compare_depth, compare_scaling


def test_native_and_ptc_use_same_frozen_surface_and_succeed():
    result = compare_depth(2)
    assert result.native.task_success is True
    assert result.ptc.task_success is True
    assert result.native.tool_call_count == result.ptc.tool_call_count == 3
    assert result.native.orchestration_depth == result.ptc.orchestration_depth == 2
    assert result.native.model_request_count == 4
    assert result.ptc.model_request_count == 2
    assert result.to_dict()["fair_comparison"]["same_tool_surface"] == [
        "search_files", "git_status", "git_diff"
    ]


def test_ptc_compression_is_reported_only_when_child_evidence_exists():
    result = compare_depth(3)
    assert result.ptc.raw_child_result_bytes > 0
    assert result.ptc.curated_result_bytes > 0
    assert result.ptc.context_compression_ratio is not None
    json.loads(json.dumps(result.to_dict()))


def test_scaling_covers_required_depth_bands():
    results = compare_scaling((1, 3, 5, 10))
    assert [item.native.orchestration_depth for item in results] == [1, 3, 5, 10]
    assert all(item.native.task_success and item.ptc.task_success for item in results)

