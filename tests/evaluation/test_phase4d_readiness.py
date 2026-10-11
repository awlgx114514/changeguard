import json
from pathlib import Path
import subprocess

import yaml
import pytest

from code_governance.evaluation.phase4d_readiness import (
    SampleValidity,
    TrialRecord,
    _actual_provider_identity,
    _explicit_external_provider_status,
    _explicit_provider_status_from_streams,
    _write_diagnostic_artifact,
    classify_sample,
    compare_initial_inputs,
    compare_provider_initial_inputs,
    primary_success,
    readiness_summary,
    validate_repair_projection,
)
from code_governance.evaluation.phase4d_readiness import (
    _InstrumentedCodexAgent,
    _ApprovalBoundWorkspaceManager,
    _ScopedFixtureApproval,
    _TrialPersistingTraceManager,
    _canonical_action_path_status,
    _terminal_evidence_completeness,
    _repair_region,
    _run_one_fork,
    _trial_source,
    _sha,
    prepare_readiness_checkpoint,
)
from code_governance.agent.codex_agent import CodexAgent
from code_governance.evaluation.phase4d_readiness import _run_trial_oracle
from code_governance.evaluation.phase4d_checkpoint import workspace_fingerprint


def test_sample_classifier_uses_explicit_facts_and_is_deterministic():
    assert classify_sample({"setup_checks": {"workspace_match": False}}) is SampleValidity.INVALID_SETUP
    assert classify_sample({"provider_status": "rate_limited", "evidence_complete": True}) is SampleValidity.INVALID_EXTERNAL
    assert classify_sample({"timeout": True, "evidence_complete": False}) is SampleValidity.UNDETERMINED
    assert classify_sample({"model_outcome": "wrong_repair", "evidence_complete": True}) is SampleValidity.VALID
    facts = {"timeout": True, "evidence_complete": False}
    assert classify_sample(facts) is classify_sample(facts)


def test_ambiguous_provider_timeout_and_empty_trial_list_remain_undetermined():
    assert classify_sample({
        "timeout_observed": True,
        "provider_calls": [],
        "evidence_complete": False,
    }) is SampleValidity.UNDETERMINED
    assert readiness_summary([])["effect_status"] == "NOT_ESTIMABLE"


def test_external_classification_requires_explicit_provider_transport_evidence():
    assert _explicit_external_provider_status("HTTP 503 Service Unavailable") == "service_failure"
    assert _explicit_external_provider_status("429 rate limit exceeded") == "rate_limited"
    assert _explicit_external_provider_status("provider transport outage: upstream unavailable") == "transport_outage"
    assert _explicit_external_provider_status("connection reset") == ""
    assert _explicit_provider_status_from_streams(
        '{"type":"item.completed","item":{"type":"assistant_message","text":"HTTP 503 Service Unavailable"}}',
        "",
    ) == ""
    assert _explicit_provider_status_from_streams(
        '{"type":"error","message":"HTTP 503 Service Unavailable"}', "",
    ) == "service_failure"
    assert classify_sample({"timeout_observed": True, "evidence_complete": False}) is SampleValidity.UNDETERMINED
    assert classify_sample({
        "provider_status": "transport_outage",
        "timeout_observed": True,
        "evidence_complete": False,
    }) is SampleValidity.INVALID_EXTERNAL


def test_primary_success_preserves_unknown_oracle_and_false_success():
    assert primary_success(True, True) == {
        "harness_success": True, "oracle_success": True,
        "primary_success": True, "false_success": False,
    }
    assert primary_success(False, True)["primary_success"] is False
    assert primary_success(True, None)["primary_success"] is None
    assert primary_success(True, False)["false_success"] is True


def test_zero_valid_pair_is_not_estimable_and_false_success_blocks_readiness():
    assert readiness_summary([])["effect_status"] == "NOT_ESTIMABLE"
    assert readiness_summary([])["readiness"] == "NOT_ESTIMABLE"
    report = readiness_summary([{
        "validity": "VALID",
        "harness_success": True,
        "oracle_success": False,
        "primary_success": False,
    }])
    assert report["false_success_count"] == 1
    assert report["readiness"] == "FAIL"


def test_unknown_canonical_path_prevents_readiness_claim():
    report = readiness_summary([
        {"pair_id": "p", "condition": "baseline", "validity": "VALID"},
        {"pair_id": "p", "condition": "treatment", "validity": "VALID"},
    ])
    assert report["canonical_action_path_status"] == "NOT_PROVEN"
    assert report["readiness"] == "NOT_PROVEN"


def test_readiness_counts_complete_valid_pairs_not_individual_trials():
    report = readiness_summary([
        {"pair_id": "pair-1", "condition": "baseline", "validity": "VALID"},
        {"pair_id": "pair-1", "condition": "treatment", "validity": "INVALID_EXTERNAL"},
        {"pair_id": "pair-2", "condition": "baseline", "validity": "VALID"},
    ])

    assert report["valid_pair_count"] == 0
    assert report["effect_status"] == "NOT_ESTIMABLE"


def test_trial_is_persisted_before_work_and_survives_interruption(tmp_path):
    record = TrialRecord.create(
        tmp_path / "trial.json",
        experiment_id="exp-1",
        pair_id="pair-1",
        checkpoint_id="checkpoint-1",
        trial_id="trial-1",
        run_id="run-1",
        condition="baseline",
    )
    assert record.path.is_file()
    record.transition("STARTED")
    record.provider_decision_started(decision_seq=1, provider_call_local_id="call-1")

    reloaded = TrialRecord.load(record.path)

    assert reloaded.payload["status"] == "PROVIDER_DECISION_STARTED"
    assert reloaded.payload["provider_calls"][0]["trial_id"] == "trial-1"
    assert reloaded.payload["provider_calls"][0]["run_id"] == "run-1"
    assert reloaded.payload["provider_calls"][0]["decision_seq"] == 1
    call = reloaded.payload["provider_calls"][0]
    assert call["provider_call_local_id"] == "call-1"
    assert call["boundary_timestamps"]["decision_started"] is not None
    assert call["boundary_timestamps"]["process_started"] is None
    assert call["boundary_timestamps"]["input_accepted"] is None
    assert call["boundary_timestamps"]["stdin_completed"] is None
    assert call["boundary_timestamps"]["first_stdout_observed"] is None
    assert call["boundary_timestamps"]["first_stderr_observed"] is None
    assert call["boundary_timestamps"]["assistant_output_observed"] is None
    assert call["boundary_timestamps"]["action_parsed"] is None
    assert "response_received" not in call
    assert call["native_tool_call_count"] is None


def test_assistant_output_observed_does_not_mean_action_parsed(tmp_path):
    record = TrialRecord.create(
        tmp_path / "trial.json", experiment_id="exp", pair_id="pair",
        checkpoint_id="checkpoint", trial_id="trial", run_id="run", condition="baseline",
    )
    record.provider_decision_started(decision_seq=1, provider_call_local_id="call-local")
    record.provider_boundary(
        "call-local", "assistant_output_artifact_checked", artifact_present=True,
        artifact_sha256="a" * 64,
    )

    call = TrialRecord.load(record.path).payload["provider_calls"][0]
    assert call["boundary_timestamps"]["assistant_output_observed"] is not None
    assert call["boundary_timestamps"]["action_parsed"] is None
    assert call["assistant_output_artifact_present"] is True


def test_actual_cli_version_and_preflight_mismatch_are_persistable(tmp_path):
    package = tmp_path / "node_modules" / "@openai" / "codex"
    binary_dir = package / "bin"
    binary_dir.mkdir(parents=True)
    entrypoint = binary_dir / "codex.js"
    entrypoint.write_text("", encoding="utf-8")
    (package / "package.json").write_text('{"version":"9.8.7"}', encoding="utf-8")
    node = tmp_path / "node.exe"
    node.write_text("", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model = "config-model"\nmodel_reasoning_effort = "medium"\n', encoding="utf-8",
    )

    identity = _actual_provider_identity([
        str(node), str(entrypoint), "exec", "--model", "model-x",
        "-c", "model_reasoning_effort=high",
    ], cwd=tmp_path, workspace_root=tmp_path,
       environment={"CODEX_HOME": str(codex_home), "OPENAI_API_KEY": "test-secret"})
    assert identity["actual_cli_version"] == "9.8.7"
    assert identity["effective_model"] == "model-x"
    assert identity["effective_reasoning_configuration"] == {"model_reasoning_effort": "high"}
    assert identity["config_state"] == "loaded"
    assert identity["config_sha256"]
    assert identity["sampling_identity_verifiable"] is True
    assert "test-secret" not in repr(identity)
    assert identity["cwd_role"] == "workspace_root"
    assert identity["cwd_relative_path"] == "."
    assert identity["cwd_absolute_sha256"]
    assert identity["provider_environment_fingerprint"]

    record = TrialRecord.create(
        tmp_path / "trial.json", experiment_id="exp", pair_id="pair",
        checkpoint_id="checkpoint", trial_id="trial", run_id="run", condition="baseline",
    )
    record.provider_decision_started(
        decision_seq=1, provider_call_local_id="call-local",
        preflight_cli_version="9.8.6",
        preflight_launch_path="old-codex.js",
        preflight_model="model-y",
        preflight_reasoning_configuration={"model_reasoning_effort": "low"},
        **identity,
    )
    call = TrialRecord.load(record.path).payload["provider_calls"][0]
    assert call["preflight_cli_version"] == "9.8.6"
    assert call["actual_cli_version"] == "9.8.7"
    assert call["preflight_actual_version_match"] is False
    assert call["preflight_identity_matches_trial"] is False
    assert call["preflight_identity_comparison"]["launch_path"] is False
    assert call["preflight_identity_comparison"]["model"] is False
    assert call["preflight_identity_comparison"]["reasoning_configuration"] is False


def test_provider_cwd_identity_distinguishes_workspace_root_and_subdirectory(tmp_path):
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    root_identity = _actual_provider_identity(["codex", "exec"], cwd=root, workspace_root=root)
    child_identity = _actual_provider_identity(["codex", "exec"], cwd=root / "src", workspace_root=root)
    assert root_identity["cwd_relative_path"] == "."
    assert root_identity["cwd_role"] == "workspace_root"
    assert child_identity["cwd_relative_path"] == "src"
    assert child_identity["cwd_role"] == "workspace_subdirectory"
    assert root_identity["cwd_absolute_sha256"] != child_identity["cwd_absolute_sha256"]


def test_cli_retry_fallback_and_error_streams_are_referenced(tmp_path):
    record = TrialRecord.create(
        tmp_path / "trial.json", experiment_id="exp", pair_id="pair",
        checkpoint_id="checkpoint", trial_id="trial", run_id="run", condition="baseline",
    )
    record.provider_decision_started(decision_seq=1, provider_call_local_id="call-local")
    stdout = "retry attempt 2\nWebSocket failed; falling back to HTTPS"
    stderr = "provider API HTTP 503 Service Unavailable"
    record.provider_process_returned(
        "call-local", returncode=0, response_artifact_present=True,
        stdout=stdout, stderr=stderr, response_artifact='{"operation":"workflow.finish"}',
        native_tool_call_count=None, native_call_observation_complete=False,
        event_types=["turn.started"], duration_seconds=12.5,
    )

    call = TrialRecord.load(record.path).payload["provider_calls"][0]
    assert call["provider_status"] == "service_failure"
    assert call["transport_fallback_observed"] is True
    assert call["provider_retry_message_count"] == 1
    assert call["stdout_sha256"] == _sha(stdout)
    assert Path(record.path.parent, call["stdout_artifact_ref"]["path"]).read_text(encoding="utf-8").startswith("retry attempt 2")
    assert call["assistant_output_artifact_ref"]["sha256"] == _sha('{"operation":"workflow.finish"}')


def test_timeout_partial_streams_are_persisted_without_external_attribution(tmp_path):
    record = TrialRecord.create(
        tmp_path / "trial.json", experiment_id="exp", pair_id="pair",
        checkpoint_id="checkpoint", trial_id="trial", run_id="run", condition="baseline",
    )
    record.provider_decision_started(decision_seq=1, provider_call_local_id="call-local")
    record.provider_call_failed(
        "call-local", category="codex_cli_timeout", stdout='{"type":"partial"}',
        stderr="still waiting", capture_scope="timeout_exception_partial",
    )

    call = TrialRecord.load(record.path).payload["provider_calls"][0]
    assert call["diagnostic_capture_scope"] == "timeout_exception_partial"
    assert call["stdout_artifact_ref"]["sha256"] == _sha('{"type":"partial"}')
    assert "provider_status" not in call
    assert classify_sample({
        "timeout_observed": True,
        "evidence_complete": False,
        **{key: value for key, value in call.items() if key == "provider_status"},
    }) is SampleValidity.UNDETERMINED


def test_diagnostic_artifact_is_bounded_but_keeps_fingerprint(tmp_path):
    ref = _write_diagnostic_artifact(tmp_path, "call-local", "stdout", "x" * 100, max_bytes=20)

    assert ref["truncated"] is True
    assert ref["sha256"] == _sha("x" * 100)
    assert ref["original_bytes"] == 100
    assert Path(tmp_path, ref["path"]).read_bytes().startswith(b"x")
    assert Path(tmp_path, ref["path"]).stat().st_size <= 20


def test_fork_trial_record_exists_before_fixture_setup(tmp_path, monkeypatch):
    import code_governance.evaluation.phase4d_readiness as readiness

    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "checkpoint")

    def fail_fixture_setup(*_args, **_kwargs):
        raise OSError("fixture setup interrupted")

    monkeypatch.setattr(readiness, "_copy_trial_source", fail_fixture_setup)
    try:
        _run_one_fork(
            checkpoint,
            task_id="behavior_repair",
            condition="baseline",
            output_dir=tmp_path / "fork",
            pair_id=checkpoint.payload["pair_id"],
        )
    except OSError:
        pass
    else:
        raise AssertionError("expected fixture setup failure")

    records = list((tmp_path / "fork" / "trials").glob("*.json"))
    assert len(records) == 1
    trial = TrialRecord.load(records[0])
    assert trial.payload["status"] == "PLANNED"
    assert trial.payload["setup_error"]["stage"] == "fixture_setup"


def test_provider_decision_records_actual_input_and_pair_comparison():
    baseline = [{
        "decision_seq": 1,
        "model_input_sha256": "base-with-empty-repair",
        "input_without_repair_sha256": "same-base",
        "context_projection_sha256": "empty-context",
        "decision_schema_sha256": "same-schema",
        "provider_configuration_fingerprint": "same-config",
        "actual_cli_version": "9.8.7",
        "actual_executable_path": "node.exe",
        "launch_path": "codex.js",
        "effective_model": "model-x",
        "effective_reasoning_configuration": {"model_reasoning_effort": "high"},
        "invocation_fingerprint": "same-invocation",
        "semantic_launch_argv": ["<node-runtime>", "<codex-cli-entrypoint>", "exec", "--model", "model-x"],
        "cwd_role": "workspace_root",
        "cwd_relative_path": ".",
        "provider_environment_fingerprint": "same-env",
        "session_mode": "new_session",
        "config_source": "codex/config.toml",
        "config_sha256": "same-config-hash",
        "sampling_identity_verifiable": True,
        "checkpoint_id": "checkpoint-1",
        "trial_id": "baseline-trial",
        "run_id": "baseline-run",
        "workspace_id": "baseline-workspace",
    }]
    treatment = [{
        "decision_seq": 1,
        "model_input_sha256": "treatment-with-repair",
        "input_without_repair_sha256": "same-base",
        "context_projection_sha256": "frozen-repair",
        "decision_schema_sha256": "same-schema",
        "provider_configuration_fingerprint": "same-config",
        "actual_cli_version": "9.8.7",
        "actual_executable_path": "node.exe",
        "launch_path": "codex.js",
        "effective_model": "model-x",
        "effective_reasoning_configuration": {"model_reasoning_effort": "high"},
        "invocation_fingerprint": "same-invocation",
        "semantic_launch_argv": ["<node-runtime>", "<codex-cli-entrypoint>", "exec", "--model", "model-x"],
        "cwd_role": "workspace_root",
        "cwd_relative_path": ".",
        "provider_environment_fingerprint": "same-env",
        "session_mode": "new_session",
        "config_source": "codex/config.toml",
        "config_sha256": "same-config-hash",
        "sampling_identity_verifiable": True,
        "checkpoint_id": "checkpoint-1",
        "trial_id": "treatment-trial",
        "run_id": "treatment-run",
        "workspace_id": "treatment-workspace",
    }]

    result = compare_provider_initial_inputs(baseline, treatment)

    assert result["consistent"] is True
    assert result["intervention_only_repair_context"] is True
    assert compare_provider_initial_inputs(
        baseline, [{**treatment[0], "decision_schema_sha256": "different"}],
    )["consistent"] is False
    assert compare_provider_initial_inputs(
        baseline, [{**treatment[0], "input_without_repair_sha256": "different"}],
    )["consistent"] is False
    assert compare_provider_initial_inputs(
        baseline, [{**treatment[0], "actual_cli_version": None}],
    )["consistent"] is False
    for field in (
        "semantic_launch_argv", "cwd_role", "provider_environment_fingerprint",
        "session_mode", "config_sha256",
    ):
        assert compare_provider_initial_inputs(
            baseline, [{**treatment[0], field: "drift"}],
        )["consistent"] is False
    assert compare_provider_initial_inputs(
        baseline, [{**treatment[0], "sampling_identity_verifiable": False}],
    )["consistent"] is False


def test_instrumented_agent_persists_actual_provider_input_before_invocation(tmp_path, monkeypatch):
    from code_governance.context.builder import ContextBuilder
    from code_governance.runtime.state import AgentState

    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "checkpoint")
    trial_id = "trial:treatment:test"
    run_id = "treatment-test"
    record = TrialRecord.create(
        tmp_path / "trial.json",
        experiment_id=checkpoint.payload["experiment_id"],
        pair_id=checkpoint.payload["pair_id"],
        checkpoint_id=checkpoint.checkpoint_id,
        trial_id=trial_id,
        run_id=run_id,
        condition="treatment",
    )
    record.transition("STARTED")
    agent = _InstrumentedCodexAgent(record, checkpoint)
    agent.bind_execution_identity(workspace_id="workspace-test", run_id=run_id, trial_id=trial_id)
    state = AgentState.from_dict(checkpoint.payload["agent_state"])
    state.run_id = run_id
    state.workspace_reference = str(tmp_path)
    state.current_goal = "Repair validator failures"
    state.transition("REPAIRING")
    builder = ContextBuilder()
    context = checkpoint.restore_context(tmp_path)
    context = builder.with_repair_context(context, checkpoint.restore_repair_context())
    context = builder.with_state(context, state)
    agent._active_context = context
    agent._active_state = state
    agent.decision_seq = 1
    prompt = agent._build_decision_prompt(state, context)
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "object"}), encoding="utf-8")
    output = tmp_path / "response.json"
    command = ["codex", "exec", "--output-schema", str(schema), "--output-last-message", str(output)]

    def provider_process(_self, _command, *, prompt, workspace):
        persisted_at_invocation = TrialRecord.load(record.path).payload["provider_calls"][0]
        assert persisted_at_invocation["boundary_timestamps"]["decision_started"] is not None
        _self._diagnostic_callback("cleanup_completed")
        return subprocess.CompletedProcess(
            _command, 17, stdout='{"type":"turn.completed"}\n', stderr="captured stderr",
        )

    monkeypatch.setattr(CodexAgent, "_run_cli", provider_process)
    agent._run_cli(command, prompt=prompt, workspace=tmp_path)

    call = TrialRecord.load(record.path).payload["provider_calls"][0]
    region = _repair_region(prompt)
    assert call["trial_id"] == trial_id
    assert call["run_id"] == run_id
    assert call["workspace_id"] == "workspace-test"
    assert call["model_input_sha256"] == _sha(prompt)
    assert call["input_without_repair_sha256"] == _sha(region[0] + region[2])
    assert call["decision_schema_sha256"] == _sha(schema.read_bytes())
    assert call["repair_provenance_valid"] is True
    assert call["boundary_timestamps"]["decision_started"] is not None
    assert call["boundary_timestamps"]["action_parsed"] is None
    assert call["native_tool_call_count"] is None
    assert call["first_decision_intervention"] is True
    assert call["model_visible_provenance"]
    assert call["process_returncode"] == 17
    assert call["boundary_timestamps"]["cleanup_completed"] is not None
    assert call["stdout_artifact_ref"] and call["stderr_artifact_ref"]
    manifest = {item["field"]: item for item in call["model_visible_provenance"]}
    assert manifest["task"]["trust_class"] == "PUBLIC_INPUT"
    assert manifest["working_memory"]["trust_class"] == "UNVERIFIED_RUNTIME_SUMMARY"
    assert manifest["decisions"]["trust_class"] == "MODEL_GENERATED_UNVERIFIED"
    assert manifest["repair_context"]["trust_class"] == "VERIFIED_RUNTIME_FACT"
    assert all(item.get("section") and item.get("rendered_sha256") for item in manifest.values())


def test_provider_call_is_persisted_before_provenance_gate(tmp_path):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "checkpoint")
    record = TrialRecord.create(
        tmp_path / "trial.json", experiment_id="exp", pair_id="pair",
        checkpoint_id=checkpoint.checkpoint_id, trial_id="trial", run_id="run", condition="baseline",
    )
    agent = _InstrumentedCodexAgent(record, checkpoint)
    prompt = "Task\nValidation Repair Evidence:\n- fabricated: true\nContext Constraints:\nnone\n"

    with pytest.raises(Exception, match="provenance"):
        agent._run_cli(["codex", "exec"], prompt=prompt, workspace=tmp_path)

    reloaded = TrialRecord.load(record.path)
    assert len(reloaded.payload["provider_calls"]) == 1
    call = reloaded.payload["provider_calls"][0]
    assert call["boundary_timestamps"]["decision_started"] is not None
    assert call["setup_failure"] == "repair_provenance_rejected"
    assert "provider_status" not in call


@pytest.mark.parametrize("diagnostic_failure", ["parse", "persistence", "action_persistence"])
def test_diagnostic_failure_cannot_change_agent_action_semantics(tmp_path, monkeypatch, diagnostic_failure):
    from code_governance.context.builder import ContextBuilder
    from code_governance.runtime.state import AgentState

    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "checkpoint")
    record = TrialRecord.create(
        tmp_path / "trial.json", experiment_id="exp", pair_id="pair",
        checkpoint_id=checkpoint.checkpoint_id, trial_id="trial:treatment", run_id="treatment-run",
        condition="treatment",
    )
    instrumented = _InstrumentedCodexAgent(record, checkpoint)
    instrumented.bind_execution_identity(workspace_id="workspace", run_id="treatment-run", trial_id="trial:treatment")
    state = AgentState.from_dict(checkpoint.payload["agent_state"])
    state.run_id = "treatment-run"
    state.workspace_reference = str(tmp_path)
    state.current_goal = "Repair validator failures"
    state.transition("REPAIRING")
    builder = ContextBuilder()
    context = builder.with_state(
        builder.with_repair_context(checkpoint.restore_context(tmp_path), checkpoint.restore_repair_context()),
        state,
    )
    plain_state = AgentState.from_dict(state.to_dict())

    def malformed_diagnostic_cli(_self, command, *, prompt, workspace):
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(json.dumps({"operation": "workflow.finish", "arguments": {}, "reason": "done"}), encoding="utf-8")
        return subprocess.CompletedProcess(
            command, 0,
            stdout=(
                '{"type":"item.completed","item":{"type":[]}}\n{"type":"turn.completed"}\n'
                if diagnostic_failure == "parse" else '{"type":"turn.completed"}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr(CodexAgent, "_run_cli", malformed_diagnostic_cli)
    if diagnostic_failure == "persistence":
        monkeypatch.setattr(
            TrialRecord, "provider_process_returned",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("diagnostic artifact unavailable")),
        )
    elif diagnostic_failure == "action_persistence":
        monkeypatch.setattr(
            TrialRecord, "provider_action_received",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("action diagnostic unavailable")),
        )
    expected = CodexAgent().decide(plain_state, context)
    actual = instrumented.decide(state, context)

    assert (actual.operation, actual.reason) == (expected.operation, expected.reason)
    call = TrialRecord.load(record.path).payload["provider_calls"][0]
    assert call["diagnostic_status"] == (
        "DIAGNOSTIC_PARSE_ERROR" if diagnostic_failure == "parse" else "DIAGNOSTIC_PERSISTENCE_FAILURE"
    )
    assert call["canonical_operation"] == "workflow.finish"


def test_terminal_evidence_and_native_bypass_facts_are_not_hardcoded(tmp_path):
    from types import SimpleNamespace

    record = TrialRecord.create(
        tmp_path / "trial.json", experiment_id="exp", pair_id="pair",
        checkpoint_id="checkpoint", trial_id="trial", run_id="run", condition="baseline",
    )
    assert _canonical_action_path_status(record, real_model=True) is None
    record.provider_decision_started(decision_seq=1, provider_call_local_id="call")
    record.provider_call_updated(
        "call", native_tool_call_count=1, native_call_observation_complete=True,
    )
    assert _canonical_action_path_status(record, real_model=True) is False
    record.provider_call_updated(
        "call", native_tool_call_count=0, native_call_observation_complete=True,
    )
    assert _canonical_action_path_status(record, real_model=True) is True

    decision_failure = SimpleNamespace(
        success=False, failure=SimpleNamespace(stage="ADAPTER", category="DECISION_ERROR"),
        validation_attempts=(), validation_result="NOT_RUN",
    )
    complete, boundary = _terminal_evidence_completeness(decision_failure, record, {})
    assert complete is True
    assert boundary == "model_decision_failure"

    incomplete = TrialRecord.create(
        tmp_path / "incomplete.json", experiment_id="exp", pair_id="pair",
        checkpoint_id="checkpoint", trial_id="trial2", run_id="run2", condition="baseline",
    )
    governance_failure = SimpleNamespace(
        success=False, failure=SimpleNamespace(stage="GOVERNANCE", category="POLICY_DENIED"),
        validation_attempts=(), validation_result="NOT_RUN",
    )
    complete, boundary = _terminal_evidence_completeness(governance_failure, incomplete, {})
    assert complete is False
    assert boundary == "governance_rejection_missing_action_or_observation"


def test_canonical_tool_and_validation_failures_have_classifiable_evidence(tmp_path):
    from types import SimpleNamespace
    from code_governance.contract.schema import ChangeContract

    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "checkpoint")
    record = TrialRecord.create(
        tmp_path / "trial.json", experiment_id="exp", pair_id="pair",
        checkpoint_id=checkpoint.checkpoint_id, trial_id="trial", run_id="run", condition="baseline",
    )
    for event, action_id, data in (
        ("action_decided", "a-1", {}),
        ("action_executed", "a-1", {"success": False}),
        ("observation_received", "a-1", {"failure_stage": "TOOL_EXECUTION"}),
    ):
        record.runtime_trace_boundary({
            "event": event, "sequence": 1, "timestamp": "t",
            "data": {"action_id": action_id, **data},
        })
    tool_failure = SimpleNamespace(
        success=False, failure=SimpleNamespace(stage="TOOL_EXECUTION", category="TOOL_EXECUTION_FAILED"),
        validation_attempts=(), validation_result="NOT_RUN", contract=None,
    )
    complete, boundary = _terminal_evidence_completeness(tool_failure, record, {})
    assert complete is True
    assert boundary == "tool_failure"
    assert classify_sample({"model_outcome": "goal_not_satisfied", "evidence_complete": complete}) is SampleValidity.VALID

    contract = ChangeContract.model_validate(checkpoint.payload["contract"])
    attempt = SimpleNamespace(
        result=SimpleNamespace(
            status="FAIL",
            assertion_results=tuple(SimpleNamespace(assertion_id=item.id) for item in contract.verification.assertions),
        ),
        evidence=SimpleNamespace(verification=tuple(
            SimpleNamespace(claim_id=item.id, execution_result_id=f"execution:{item.id}")
            for item in contract.verification.claims
        )),
    )
    validation_failure = SimpleNamespace(
        success=False, failure=SimpleNamespace(stage="VALIDATION", category="VALIDATOR_FAILED"),
        validation_attempts=(attempt,), validation_result="FAIL", contract=contract,
    )
    complete, boundary = _terminal_evidence_completeness(validation_failure, record, {})
    assert complete is True
    assert boundary == "verification_or_validation_failure"


def test_provider_interruption_persists_partial_assistant_artifact_before_cleanup(tmp_path, monkeypatch):
    from code_governance.context.builder import ContextBuilder
    from code_governance.runtime.state import AgentState

    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "checkpoint")
    record = TrialRecord.create(
        tmp_path / "trial.json", experiment_id="exp", pair_id="pair",
        checkpoint_id=checkpoint.checkpoint_id, trial_id="trial:treatment", run_id="run", condition="treatment",
    )
    agent = _InstrumentedCodexAgent(record, checkpoint)
    agent.bind_execution_identity(workspace_id="workspace", run_id="run", trial_id="trial:treatment")
    state = AgentState.from_dict(checkpoint.payload["agent_state"])
    state.run_id = "run"
    state.workspace_reference = str(tmp_path)
    state.current_goal = "Repair validator failures"
    state.transition("REPAIRING")
    builder = ContextBuilder()
    context = builder.with_state(
        builder.with_repair_context(checkpoint.restore_context(tmp_path), checkpoint.restore_repair_context()),
        state,
    )

    def interrupted_cli(_self, command, *, prompt, workspace):
        Path(command[command.index("--output-last-message") + 1]).write_text(
            '{"operation":"workflow.finish"}', encoding="utf-8",
        )
        raise KeyboardInterrupt()

    monkeypatch.setattr(CodexAgent, "_run_cli", interrupted_cli)
    with pytest.raises(KeyboardInterrupt):
        agent.decide(state, context)

    call = TrialRecord.load(record.path).payload["provider_calls"][0]
    ref = call["assistant_output_artifact_ref"]
    assert call["interruption_category"] == "KeyboardInterrupt"
    assert ref and Path(record.path.parent, ref["path"]).is_file()
    assert Path(record.path.parent, ref["path"]).read_text(encoding="utf-8") == '{"operation":"workflow.finish"}'


def test_runtime_observation_survives_next_decision_interruption(tmp_path):
    from code_governance.agent.harness import HarnessOrchestrator
    from code_governance.runtime.action import Action, OperationKind
    from code_governance.workspace.manager import WorkspaceManager

    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "checkpoint")
    record = TrialRecord.create(
        tmp_path / "trial.json", experiment_id="exp", pair_id="pair",
        checkpoint_id=checkpoint.checkpoint_id, trial_id="trial:treatment", run_id="run", condition="treatment",
    )

    class InterruptAfterRead:
        def __init__(self):
            self.calls = 0

        def decide(self, _state, _context):
            self.calls += 1
            if self.calls == 1:
                return Action.create_operation(OperationKind.FILE_READ, path="src/auth.py", reason="inspect")
            raise KeyboardInterrupt()

    agent = InterruptAfterRead()
    repository = Path(checkpoint.payload["artifact_root"]) / "p" / "repo"
    harness = HarnessOrchestrator(
        coding_agent=agent,
        workspace_manager=WorkspaceManager(),
        max_repairs=2,
        runtime_max_steps=10,
        trace_manager_factory=lambda run_id, writer: readiness._TrialPersistingTraceManager(run_id, writer, record),
    )
    import code_governance.evaluation.phase4d_readiness as readiness
    with pytest.raises(KeyboardInterrupt):
        harness.repair_start(
            checkpoint, repo_path=repository, trial_id="trial:treatment", run_id="run",
            include_repair_context=True, final_workspace_path=tmp_path / "final",
        )

    persisted = TrialRecord.load(record.path).payload
    boundaries = persisted["runtime_facts"]["boundaries"]
    events = {item["event"] for item in boundaries}
    assert {"action_decided", "action_executed", "observation_received"}.issubset(events)
    action_ids = {
        item["data"].get("action_id") for item in boundaries
        if item["event"] == "action_decided"
    }
    assert action_ids
    assert action_ids == {
        item["data"].get("action_id") for item in boundaries
        if item["event"] in {"action_executed", "observation_received"}
    }


@pytest.mark.parametrize("condition", ["baseline", "treatment"])
def test_new_validation_failure_repair_context_uses_new_attempt_provenance(tmp_path, monkeypatch, condition):
    from code_governance.agent.harness import HarnessOrchestrator

    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "checkpoint")
    trial_id = f"trial:{condition}:new-attempt"
    run_id = f"{condition}-new-attempt"
    record = TrialRecord.create(
        tmp_path / "trial.json", experiment_id=checkpoint.payload["experiment_id"],
        pair_id=checkpoint.payload["pair_id"], checkpoint_id=checkpoint.checkpoint_id,
        trial_id=trial_id, run_id=run_id, condition=condition,
    )
    task, repository, _revision = _trial_source("behavior_repair", tmp_path / "source", trial_id)
    approval = _ScopedFixtureApproval(task)
    agent = _InstrumentedCodexAgent(record, checkpoint)
    agent.bind_execution_identity(workspace_id="workspace", run_id=run_id, trial_id=trial_id)

    def finish_cli(_self, command, *, prompt, workspace):
        Path(command[command.index("--output-last-message") + 1]).write_text(
            json.dumps({"operation": "workflow.finish", "arguments": {}, "reason": "validate current state"}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, '{"type":"turn.completed"}\n', "")

    monkeypatch.setattr(CodexAgent, "_run_cli", finish_cli)
    harness = HarnessOrchestrator(
        coding_agent=agent,
        workspace_manager=_ApprovalBoundWorkspaceManager(approval),
        approval_provider=approval,
        max_repairs=2,
        runtime_max_steps=10,
        observer=agent,
        trace_manager_factory=lambda trace_run_id, writer: _TrialPersistingTraceManager(
            trace_run_id, writer, record,
        ),
    )
    result = harness.repair_start(
        checkpoint, repo_path=repository, trial_id=trial_id, run_id=run_id,
        include_repair_context=(condition == "treatment"), final_workspace_path=tmp_path / "final",
    )

    calls = TrialRecord.load(record.path).payload["provider_calls"]
    assert len(calls) == 2
    assert calls[0]["first_decision_intervention"] is (condition == "treatment")
    assert calls[1]["first_decision_intervention"] is False
    assert calls[1]["repair_provenance_valid"] is True
    new_attempt = result.harness_result.validation_attempts[0].attempt_id
    repair_manifest = next(
        item for item in calls[1]["model_visible_provenance"]
        if item["field"] == "repair_context"
    )
    refs = repair_manifest["source_references"]
    assert any(ref.startswith(f"validation_result:{new_attempt}:") for ref in refs)
    first_repair_manifest = next(
        item for item in calls[0]["model_visible_provenance"]
        if item["field"] == "repair_context"
    )
    if condition == "baseline":
        assert first_repair_manifest["source_references"] == ["condition:baseline:initial_repair_context"]
    else:
        assert any(
            ref.startswith("validation_result:")
            for ref in first_repair_manifest["source_references"]
        )


def test_model_input_pair_must_match_outside_repair_region():
    baseline = "prefix\nValidation Repair Evidence:\n[]\nContext Constraints:\nnone\nsuffix"
    treatment = "prefix\nValidation Repair Evidence:\n- failure_id: f-1\nContext Constraints:\nnone\nsuffix"
    result = compare_initial_inputs(baseline, treatment)
    assert result["consistent"] is True
    assert result["intervention_only_repair_context"] is True

    mismatch = compare_initial_inputs(baseline, treatment.replace("prefix", "different"))
    assert mismatch["consistent"] is False


def test_repair_projection_uses_provenance_not_forbidden_word_scanning():
    rendered = "Task: hidden_oracle is a literal public input string\nValidation Repair Evidence:\n[]\nContext Constraints:\nnone"
    provenance_args = {
        "validation_attempt": {}, "contract": {}, "producer_artifact_id": "artifact:test",
    }
    assert validate_repair_projection(rendered, (), (), **provenance_args) is True
    leaked = "Context\nValidation Repair Evidence:\n- hidden_oracle: true\nContext Constraints:\nnone"
    assert validate_repair_projection(leaked, (), (), **provenance_args) is False


def test_oracle_runs_on_isolated_final_state_and_refuses_wrong_fingerprint(tmp_path):
    exported = tmp_path / "exported"
    (exported / "src").mkdir(parents=True)
    (exported / "src" / "auth.py").write_text(
        'def check_password(password):\n    return password == "s3cure-pass"\n',
        encoding="utf-8",
    )
    fingerprint = workspace_fingerprint(exported)
    oracle_copy = tmp_path / "oracle" / "repo"

    result = _run_trial_oracle("behavior_repair", exported, oracle_copy, fingerprint)

    assert result["execution_status"] == "COMPLETED"
    assert result["pass"] is True
    assert workspace_fingerprint(exported) == fingerprint
    assert oracle_copy != exported

    mismatch = _run_trial_oracle("behavior_repair", exported, tmp_path / "unused", "wrong")
    assert mismatch["execution_status"] == "ERROR"
    assert mismatch["pass"] is None
