import json
import hashlib
from pathlib import Path

import pytest

from code_governance.cli import main
from code_governance.evaluation.dataset import load_dataset
from code_governance.evaluation.profiles import (
    BaselineActionValidator,
    BaselineHistoryManager,
    CompactTraceManager,
    RuntimeProfile,
)
from code_governance.evaluation.runner import BenchmarkRunner
from code_governance.runtime.action import Action, ActionType, OperationKind
from code_governance.runtime.action_validator import ActionValidator
from code_governance.runtime.history import HistoryManager
from code_governance.tools.policy import ToolPolicy
from code_governance.tools.approval import ApprovalOutcome
from code_governance.trace.writer import TraceManager, TraceWriter

from .conftest import (
    BranchHarnessFactory,
    FakeRuntimeAgent,
    InspectingHarnessFactory,
    tree_digest,
)


class BundledSimpleChangeAgent:
    """Deterministic fake that completes the shipped simple-change task."""

    def __init__(self):
        self._step = 0

    def decide(self, state, context):
        del context
        self._step += 1
        if self._step == 1:
            return Action.create_operation(OperationKind.FILE_READ, path="app.py", reason="inspect bundled target")
        if self._step == 2:
            path = Path(state.workspace_reference) / "app.py"
            return Action.create_operation(
                OperationKind.FILE_WRITE,
                path="app.py",
                content="def greet(name):\n    return f\"Hello, {name}!\"\n",
                observed_file_hash=hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest(),
                reason="implement the bundled greeting contract",
            )
        return Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="bundled task complete")


class ApprovedApprovalProvider:
    def request(self, request):
        return ApprovalOutcome.APPROVED


def approve_hardened_profile_writes(monkeypatch):
    original = RuntimeProfile.build_tool_registry

    def build_tool_registry(profile):
        registry = original(profile)
        if profile.mode == "hardened":
            registry._approval_provider = ApprovedApprovalProvider()
        return registry

    monkeypatch.setattr(RuntimeProfile, "build_tool_registry", build_tool_registry)


@pytest.mark.parametrize(
    (
        "mode",
        "history_type",
        "trace_type",
        "validator_type",
        "has_policy",
        "runtime_repair",
        "issue_lifecycle",
        "trace_schema",
    ),
    (
        (
            "baseline",
            BaselineHistoryManager,
            CompactTraceManager,
            BaselineActionValidator,
            False,
            False,
            False,
            "v1",
        ),
        (
            "hardened",
            HistoryManager,
            TraceManager,
            ActionValidator,
            True,
            True,
            True,
            "v2",
        ),
    ),
)
def test_real_harness_executes_each_profile_with_all_injections(
    evaluation_fixture,
    tmp_path,
    mode,
    history_type,
    trace_type,
    validator_type,
    has_policy,
    runtime_repair,
    issue_lifecycle,
    trace_schema,
    monkeypatch,
):
    harness_factory = InspectingHarnessFactory()
    task = evaluation_fixture.make_task()
    approve_hardened_profile_writes(monkeypatch)

    result = BenchmarkRunner(
        evaluation_fixture.dataset(task),
        coding_agent_factory=FakeRuntimeAgent,
        harness_factory=harness_factory,
    ).run_task(task, mode)

    record = harness_factory.records[0]
    kwargs = record["kwargs"]
    assert result.success is True
    assert result.runtime_mode == mode
    assert result.metrics.context_size > 0
    assert result.metrics.action_count == 2
    assert result.terminal_status == "SUCCEEDED"
    assert kwargs["runtime_loop"] is True
    assert kwargs["runtime_repair"] is runtime_repair
    assert kwargs["runtime_issue_lifecycle"] is issue_lifecycle
    assert isinstance(kwargs["history_manager_factory"](), history_type)
    assert isinstance(
        kwargs["trace_manager_factory"]("probe", TraceWriter(tmp_path)), trace_type
    )
    assert isinstance(kwargs["action_validator_factory"](), validator_type)
    assert isinstance(kwargs["tool_registry"]._policy, ToolPolicy) is has_policy
    assert record["trace_payload"]["schema_version"] == trace_schema
    assert record["contract_exists_after_harness"] is True
    assert record["task_path"].parent == record["repo_path"].parent
    assert record["contract_path"].parent == record["repo_path"].parent
    assert record["task_path"].parent != record["repo_path"]
    assert not record["repo_path"].exists()
    assert not (
        {"context", "state", "trace", "task_file", "code", "tool_output"}
        & set(vars(result))
    )


def test_real_harness_trials_are_fresh_and_preserve_source_and_commit_boundary(
    evaluation_fixture,
):
    harness_factory = InspectingHarnessFactory()
    source_before = tree_digest(evaluation_fixture.snapshot)
    report = BenchmarkRunner(
        evaluation_fixture.dataset(
            evaluation_fixture.make_task("task-1"),
            evaluation_fixture.make_task("task-2"),
        ),
        coding_agent_factory=FakeRuntimeAgent,
        harness_factory=harness_factory,
    ).run("baseline")

    assert all(trial.success for trial in report.trials)
    payload = report.to_dict()
    assert payload["benchmark_version"] == "v1"
    assert payload["dataset"] == {"id": "test-dataset", "task_count": 2}
    assert payload["run"]["run_count"] == 1
    assert payload["run"]["started_at"]
    assert payload["run"]["agent"]["agent_type"] == "FakeRuntimeAgent"
    assert payload["limitations"]
    assert payload["failure_summary"] == {}
    assert all(trial.fixture_id.startswith("sha256:") for trial in report.trials)
    assert all(len(trial.baseline_commit) == 40 for trial in report.trials)
    assert tree_digest(evaluation_fixture.snapshot) == source_before
    assert len({record["repo_path"] for record in harness_factory.records}) == 2
    for record in harness_factory.records:
        assert record["head_subject"] == "evaluation snapshot"
        assert record["head_branch"] == "main"
        assert record["local_email"] == "benchmark@example.invalid"
        assert record["local_name"] == "ChangeGuard Benchmark"
        assert record["head_files"] == ("seed.txt",)
        assert record["head_seed"] == "immutable fixture"
        assert record["task_exists_before_harness"] is True
        assert record["contract_exists_before_harness"] is False
        assert record["runs_exist_before_harness"] is False
        assert "task.md" not in record["head_files"]
        assert "contract.yaml" not in record["head_files"]
        assert not any(path.startswith(".agent/") for path in record["head_files"])
        assert record["contract_exists_after_harness"] is True
        assert len(record["trace_paths"]) == 1
        assert not record["repo_path"].exists()


def test_unmodified_bundled_style_task_can_pass_with_only_requested_file_in_scope():
    dataset_path = Path(__file__).parents[2] / "benchmarks" / "v1" / "tasks.yaml"
    dataset = load_dataset(dataset_path)
    task = next(task for task in dataset.tasks if task.id == "simple-change")

    assert task.expected_files == ("app.py",)
    result = BenchmarkRunner(
        dataset,
        coding_agent_factory=BundledSimpleChangeAgent,
    ).run_task(task, "baseline")

    assert result.success is True
    assert result.report_status == "PASS"
    assert result.failure_category is None


def test_evaluation_uses_validation_evidence_not_a_conflicting_display_report(
    evaluation_fixture,
):
    harness_factory = BranchHarnessFactory("report_conflict")
    task = evaluation_fixture.make_task(description="display report must not decide")

    result = BenchmarkRunner(
        evaluation_fixture.dataset(task),
        coding_agent_factory=FakeRuntimeAgent,
        harness_factory=harness_factory,
    ).run_task(task, "baseline")

    assert result.success is True
    assert result.report_status == "PASS"


def test_real_harness_repair_count_is_propagated(evaluation_fixture):
    harness_factory = InspectingHarnessFactory()
    task = evaluation_fixture.make_task()

    result = BenchmarkRunner(
        evaluation_fixture.dataset(task),
        coding_agent_factory=lambda: FakeRuntimeAgent(initial_content="bad\n"),
        harness_factory=harness_factory,
    ).run_task(task, "baseline")

    assert harness_factory.records[0]["harness_repairs"] == 0
    assert result.success is False
    assert result.metrics.repair_count == 0
    assert result.metrics.repair_success_rate is None
    assert not hasattr(result, "repairs")


@pytest.mark.parametrize(
    "scenario",
    (
        "no_trace",
        "multiple_traces",
        "invalid_json",
        "malformed_trace",
        "malformed_event",
        "malformed_data",
        "error",
        "no_report",
        "exception",
    ),
)
def test_trace_and_harness_failures_become_structured_trials(
    evaluation_fixture, scenario
):
    harness_factory = BranchHarnessFactory(scenario)
    task = evaluation_fixture.make_task(description=scenario)

    result = BenchmarkRunner(
        evaluation_fixture.dataset(task),
        coding_agent_factory=FakeRuntimeAgent,
        harness_factory=harness_factory,
    ).run_task(task, "baseline")

    if scenario in {
        "no_trace",
        "multiple_traces",
        "invalid_json",
        "malformed_trace",
        "malformed_event",
        "malformed_data",
    }:
        assert result.success is True
        assert result.report_status == "PASS"
        assert result.trace_summary is None
    else:
        assert result.success is False
        assert result.report_status == "ERROR"
    assert not (
        {"error", "context", "state", "trace", "task_file", "code", "tool_output"}
        & set(vars(result))
    )
    assert "private" not in json.dumps(result.to_dict(), sort_keys=True)
    assert all(not path.exists() for path in harness_factory.repo_paths)

    expected_categories = {
        "no_trace": (None, "trace_missing"),
        "multiple_traces": (None, "trace_ambiguous"),
        "invalid_json": (None, "trace_malformed"),
        "malformed_trace": (None, "trace_malformed"),
        "malformed_event": (None, "trace_malformed"),
        "malformed_data": (None, "trace_malformed"),
        "error": ("INFRASTRUCTURE", None),
        "no_report": ("validation_evidence_missing", None),
        "exception": ("harness_exception", "context_missing"),
    }
    failure_category, data_gap = expected_categories[scenario]
    assert result.failure_category == failure_category
    if data_gap is not None:
        assert data_gap in result.data_gaps


@pytest.mark.parametrize("scenario", ("no_trace", "malformed_event"))
def test_trace_failure_preserves_validated_harness_repairs(
    evaluation_fixture, scenario
):
    harness_factory = BranchHarnessFactory(scenario, repairs=2)
    task = evaluation_fixture.make_task(description=scenario)

    result = BenchmarkRunner(
        evaluation_fixture.dataset(task),
        coding_agent_factory=FakeRuntimeAgent,
        harness_factory=harness_factory,
    ).run_task(task, "baseline")

    assert result.success is True
    assert result.report_status == "PASS"
    assert result.metrics.repair_count == 2
    assert result.metrics.repair_success_rate == 1.0
    assert result.metrics.action_count is None
    assert result.trace_summary is None


def test_harness_result_repairs_are_validated_and_passed_to_metrics(
    evaluation_fixture,
):
    harness_factory = BranchHarnessFactory(repairs=2)
    task = evaluation_fixture.make_task()

    result = BenchmarkRunner(
        evaluation_fixture.dataset(task),
        coding_agent_factory=FakeRuntimeAgent,
        harness_factory=harness_factory,
    ).run_task(task, "baseline")

    assert result.metrics.repair_count == 2
    assert result.metrics.repair_success_rate == 1.0
    assert not hasattr(result, "repairs")


def test_failed_harness_result_passes_validated_repairs_to_metrics(
    evaluation_fixture,
):
    harness_factory = BranchHarnessFactory("error", repairs=2)
    task = evaluation_fixture.make_task()

    result = BenchmarkRunner(
        evaluation_fixture.dataset(task),
        coding_agent_factory=FakeRuntimeAgent,
        harness_factory=harness_factory,
    ).run_task(task, "baseline")

    assert result.success is False
    assert result.metrics.repair_count == 2
    assert result.metrics.repair_success_rate == 0.0


@pytest.mark.parametrize("repairs", (-1, True, "1"))
def test_invalid_harness_repair_count_becomes_a_structured_failure(
    evaluation_fixture, repairs
):
    harness_factory = BranchHarnessFactory(repairs=repairs)
    task = evaluation_fixture.make_task()

    result = BenchmarkRunner(
        evaluation_fixture.dataset(task),
        coding_agent_factory=FakeRuntimeAgent,
        harness_factory=harness_factory,
    ).run_task(task, "baseline")

    assert result.success is True
    assert result.report_status == "PASS"
    assert result.metrics.repair_count is None
    assert result.metrics.repair_success_rate is None
    assert "repairs_invalid" in result.data_gaps


def test_harness_failure_does_not_abort_the_dataset(evaluation_fixture):
    failed_factory = BranchHarnessFactory("exception")
    call_count = 0

    def fail_once_factory(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return failed_factory(**kwargs)
        return BranchHarnessFactory()(**kwargs)

    report = BenchmarkRunner(
        evaluation_fixture.dataset(
            evaluation_fixture.make_task("failed"),
            evaluation_fixture.make_task("succeeded"),
        ),
        coding_agent_factory=FakeRuntimeAgent,
        harness_factory=fail_once_factory,
    ).run("baseline")

    assert [trial.task_id for trial in report.trials] == ["failed", "succeeded"]
    assert [trial.success for trial in report.trials] == [False, True]


def test_benchmark_cli_returns_zero_for_a_completed_report_with_failed_trials(
    evaluation_fixture, monkeypatch, capsys
):
    dataset = evaluation_fixture.dataset(
        evaluation_fixture.make_task("failed")
    )

    monkeypatch.setattr(
        "code_governance.cli.load_dataset",
        lambda path: dataset,
    )
    monkeypatch.setattr(
        "code_governance.cli.BenchmarkRunner",
        lambda loaded_dataset: BenchmarkRunner(
            loaded_dataset,
            coding_agent_factory=FakeRuntimeAgent,
            harness_factory=BranchHarnessFactory("exception"),
        ),
    )

    exit_code = main(
        ["benchmark", "--dataset", "tasks.yaml", "--mode", "baseline"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["trial_count"] == 1
    assert payload["trials"][0]["success"] is False


def test_runner_uses_retained_dataset_root_from_unrelated_cwd_with_duplicate_names(
    evaluation_fixture, tmp_path, monkeypatch
):
    unrelated = tmp_path / "unrelated"
    duplicate = unrelated / "snapshots" / "source"
    duplicate.mkdir(parents=True)
    (duplicate / "seed.txt").write_text("wrong fixture\n", encoding="utf-8")
    dataset = evaluation_fixture.dataset(evaluation_fixture.make_task())
    monkeypatch.chdir(unrelated)
    harness_factory = BranchHarnessFactory()
    task = dataset.tasks[0]

    result = BenchmarkRunner(
        dataset,
        coding_agent_factory=FakeRuntimeAgent,
        harness_factory=harness_factory,
    ).run_task(task, "baseline")

    assert result.task_id == task.id
    assert result.success is True


def test_trial_git_baseline_is_identical_across_modes_under_hostile_global_config(
    evaluation_fixture, tmp_path, monkeypatch
):
    hooks = tmp_path / "hostile-template" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre-commit").write_text("exit 1\n", encoding="utf-8")
    hostile_config = tmp_path / "hostile.gitconfig"
    hostile_config.write_text(
        "[commit]\n"
        "\tgpgsign = true\n"
        "[core]\n"
        "\tautocrlf = true\n"
        f"[init]\n\tdefaultBranch = hostile\n\ttemplateDir = {hooks.parent.as_posix()}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile_config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "i18n.commitEncoding")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "ISO-8859-1")
    harness_factory = InspectingHarnessFactory()
    task = evaluation_fixture.make_task()
    runner = BenchmarkRunner(
        evaluation_fixture.dataset(task),
        coding_agent_factory=FakeRuntimeAgent,
        harness_factory=harness_factory,
    )
    approve_hardened_profile_writes(monkeypatch)

    baseline = runner.run_task(task, "baseline")
    monkeypatch.delenv("GIT_CONFIG_GLOBAL")
    monkeypatch.delenv("GIT_CONFIG_COUNT")
    monkeypatch.delenv("GIT_CONFIG_KEY_0")
    monkeypatch.delenv("GIT_CONFIG_VALUE_0")
    hardened = runner.run_task(task, "hardened")

    assert baseline.success is True
    assert hardened.success is True
    assert {record["head_branch"] for record in harness_factory.records} == {"main"}
    assert len({record["head_tree"] for record in harness_factory.records}) == 1
    assert len({record["head_commit"] for record in harness_factory.records}) == 1
    assert baseline.baseline_commit == hardened.baseline_commit
