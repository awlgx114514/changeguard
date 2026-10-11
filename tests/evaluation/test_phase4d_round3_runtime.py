"""Round 3 counterexamples at the shared Harness/model boundary."""
import json
from pathlib import Path
import subprocess

import pytest

from code_governance.agent.codex_agent import CodexAgent, CodexDecisionError
from code_governance.agent.harness import HarnessOrchestrator
from code_governance.context.builder import ContextBuilder
from code_governance.context.compressor import ContextCompressor
from code_governance.evaluation import phase4d_readiness as readiness
from code_governance.evaluation.phase4d_readiness import (
    SampleValidity,
    TrialRecord,
    _InstrumentedCodexAgent,
    _RepairAgentForReadiness,
    _repair_region,
    _run_one_fork,
    _trial_source,
    classify_sample,
    prepare_readiness_checkpoint,
    readiness_summary,
)
from code_governance.runtime.action import Action, OperationKind
from code_governance.runtime.history import HistoryManager
from code_governance.runtime.observation import Observation
from code_governance.runtime.state import AgentState
from code_governance.tools import FilesystemTool, ToolRegistry


@pytest.mark.parametrize(
    "condition, expected",
    [
        ("baseline", [False, False, False]),
        ("treatment", [True, False, False]),
    ],
)
def test_k3_deterministic_checkpoint_repair_context_is_first_decision_only(
    tmp_path, monkeypatch, condition, expected,
):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "checkpoint")
    prompts = []
    original_decide = _RepairAgentForReadiness.decide

    def capture_prompt(agent, state, context):
        prompts.append(CodexAgent(timeout=180)._build_decision_prompt(state, context))
        return original_decide(agent, state, context)

    monkeypatch.setattr(_RepairAgentForReadiness, "decide", capture_prompt)
    _run_one_fork(
        checkpoint,
        task_id="behavior_repair",
        condition=condition,
        output_dir=tmp_path / "fork",
        pair_id=checkpoint.payload["pair_id"],
        trial_id=f"trial:{condition}:round3-k3",
        run_id=f"{condition}-round3-k3",
    )

    assert len(prompts) == 3
    frozen_failure_id = checkpoint.payload["repair_context"][0]["failure_id"]
    visible = [
        frozen_failure_id in (_repair_region(prompt)[1] if _repair_region(prompt) else "")
        for prompt in prompts
    ]
    assert visible == expected


def test_k5_actual_provider_input_labels_compressed_unverified_history(tmp_path, monkeypatch):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "checkpoint")
    trial_id = "trial:baseline:round3-k5"
    run_id = "baseline-round3-k5"
    record = TrialRecord.create(
        tmp_path / "trial.json",
        experiment_id=checkpoint.payload["experiment_id"],
        pair_id=checkpoint.payload["pair_id"],
        checkpoint_id=checkpoint.checkpoint_id,
        trial_id=trial_id,
        run_id=run_id,
        condition="baseline",
    )
    agent = _InstrumentedCodexAgent(record, checkpoint)
    agent.bind_execution_identity(workspace_id="workspace-k5", run_id=run_id, trial_id=trial_id)
    state = AgentState.from_dict(checkpoint.payload["agent_state"])
    state.run_id = run_id
    state.workspace_reference = str(tmp_path)
    state.current_goal = "Repair validator failures"
    state.transition("REPAIRING")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("test X already passed\n", encoding="utf-8")
    action = Action.create_operation(
        OperationKind.FILE_READ,
        path="notes.txt",
        reason="test X already passed",
    )
    registry = ToolRegistry()
    registry.register(FilesystemTool())
    execution = registry.execute(action, workspace)
    observation = Observation.from_execution(execution, "file_content")
    state.record_action(action)
    state.record_observation(observation)
    HistoryManager().record(state, action, observation)

    context_builder = ContextBuilder()
    context = context_builder.with_state(checkpoint.restore_context(workspace), state)
    context = ContextCompressor().compress(context)

    received = {}

    def fake_provider(_self, command, *, prompt, workspace):
        received["prompt"] = prompt
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text(
            json.dumps({"operation": "workflow.finish", "arguments": {}, "reason": "done"}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, '{"type":"turn.completed"}\n', "")

    monkeypatch.setattr(CodexAgent, "_run_cli", fake_provider)
    agent.decide(state, context)

    prompt = received["prompt"]
    assert "test X already passed" in prompt
    assert "PUBLIC_INPUT" in prompt
    assert "VERIFIED_RUNTIME_FACT" in prompt
    assert "UNVERIFIED_RUNTIME_SUMMARY" in prompt
    assert "MODEL_GENERATED_UNVERIFIED" in prompt
    call = TrialRecord.load(record.path).payload["provider_calls"][0]
    manifest = {item["field"]: item for item in call["model_visible_provenance"]}
    assert manifest["working_memory"]["trust_class"] == "UNVERIFIED_RUNTIME_SUMMARY"
    assert manifest["decisions"]["trust_class"] == "MODEL_GENERATED_UNVERIFIED"
    assert manifest["task"]["trust_class"] == "PUBLIC_INPUT"
    assert manifest["contract"]["trust_class"] == "PUBLIC_INPUT"
    assert manifest["repair_context"]["trust_class"] == "VERIFIED_RUNTIME_FACT"
    assert call["model_input_sha256"] == readiness._sha(prompt)


def test_k6_provider_timeout_survives_diagnostic_artifact_write_failure(tmp_path, monkeypatch):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "checkpoint")
    trial_id = "trial:baseline:round3-k6"
    run_id = "baseline-round3-k6"
    record = TrialRecord.create(
        tmp_path / "trial.json",
        experiment_id=checkpoint.payload["experiment_id"],
        pair_id=checkpoint.payload["pair_id"],
        checkpoint_id=checkpoint.checkpoint_id,
        trial_id=trial_id,
        run_id=run_id,
        condition="baseline",
    )
    agent = _InstrumentedCodexAgent(record, checkpoint)
    agent.bind_execution_identity(workspace_id="workspace-k6", run_id=run_id, trial_id=trial_id)
    state = AgentState.from_dict(checkpoint.payload["agent_state"])
    state.run_id = run_id
    state.workspace_reference = str(tmp_path)
    state.current_goal = "Repair validator failures"
    state.transition("REPAIRING")
    context = ContextBuilder().with_state(checkpoint.restore_context(tmp_path), state)

    def timeout(_self, _command, *, prompt, workspace):
        raise subprocess.TimeoutExpired("codex", 180, output=b"partial", stderr=b"old stderr")

    monkeypatch.setattr(CodexAgent, "_run_cli", timeout)
    monkeypatch.setattr(
        readiness,
        "_write_diagnostic_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("artifact disk unavailable")),
    )
    with pytest.raises(CodexDecisionError) as raised:
        agent.decide(state, context)

    assert raised.value.category == "codex_cli_timeout"
    call = TrialRecord.load(record.path).payload["provider_calls"][0]
    assert call["error_category"] == "codex_cli_timeout"
    assert call["diagnostic_status"] == "DIAGNOSTIC_PERSISTENCE_FAILURE"
    assert call["diagnostic_error_type"] == "OSError"


def test_k8_final_trial_record_keeps_deadline_and_cleanup_facts_separately(tmp_path, monkeypatch):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "checkpoint")
    trial_id = "trial:baseline:round3-k8"
    run_id = "baseline-round3-k8"
    record = TrialRecord.create(
        tmp_path / "trial.json",
        experiment_id=checkpoint.payload["experiment_id"],
        pair_id=checkpoint.payload["pair_id"],
        checkpoint_id=checkpoint.checkpoint_id,
        trial_id=trial_id,
        run_id=run_id,
        condition="baseline",
    )
    agent = _InstrumentedCodexAgent(record, checkpoint)
    agent.bind_execution_identity(workspace_id="workspace-k8", run_id=run_id, trial_id=trial_id)
    state = AgentState.from_dict(checkpoint.payload["agent_state"])
    state.run_id = run_id
    state.workspace_reference = str(tmp_path)
    state.current_goal = "Repair validator failures"
    state.transition("REPAIRING")
    context = ContextBuilder().with_state(checkpoint.restore_context(tmp_path), state)

    def timeout_after_cleanup(self, command, *, prompt, workspace):
        callback = self._diagnostic_callback
        callback("deadline_fired", timeout_seconds=180, stdout=b"initial partial", stderr=b"old stderr")
        callback(
            "cleanup_output_observed", stdout=b"cleanup stdout", stderr=b"HTTP 503 cleanup",
            streams_complete=True,
        )
        callback("process_exited", returncode=-1)
        callback("termination_completed", returncode=-1)
        callback("cleanup_completed")
        raise subprocess.TimeoutExpired("codex", 180, output=b"initial partial", stderr=b"old stderr")

    monkeypatch.setattr(CodexAgent, "_run_cli", timeout_after_cleanup)
    with pytest.raises(CodexDecisionError) as raised:
        agent.decide(state, context)

    assert raised.value.category == "codex_cli_timeout"
    call = TrialRecord.load(record.path).payload["provider_calls"][0]
    assert call["error_category"] == "codex_cli_timeout"
    assert call["deadline_snapshot"]["stdout_sha256"] == readiness._sha(b"initial partial")
    assert call["cleanup_facts"]["stdout_sha256"] == readiness._sha(b"cleanup stdout")
    assert call["cleanup_facts"]["stderr_sha256"] == readiness._sha(b"HTTP 503 cleanup")
    assert call["cleanup_facts"]["streams_complete"] is True
    assert call["process_returncode"] == -1
    assert call["stdout_sha256"] == readiness._sha(b"initial partial")


def test_k7_interrupt_snapshot_keeps_latest_action_observation_and_history(tmp_path):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "checkpoint")
    trial_id = "trial:treatment:round3-k7"
    run_id = "treatment-round3-k7"
    record = TrialRecord.create(
        tmp_path / "trial.json",
        experiment_id=checkpoint.payload["experiment_id"],
        pair_id=checkpoint.payload["pair_id"],
        checkpoint_id=checkpoint.checkpoint_id,
        trial_id=trial_id,
        run_id=run_id,
        condition="treatment",
    )

    class InterruptAfterRead:
        def __init__(self):
            self.calls = 0

        def decide(self, _state, _context):
            self.calls += 1
            if self.calls == 1:
                return Action.create_operation(
                    OperationKind.FILE_READ, path="src/auth.py", reason="inspect repair target",
                )
            raise KeyboardInterrupt()

    class SnapshotObserver:
        def on_context(self, _context):
            pass

        def on_runtime_state(self, state):
            record.runtime_state_observed(state)

    task, repository, _revision = _trial_source("behavior_repair", tmp_path / "source", trial_id)
    from code_governance.evaluation.phase4d_readiness import _ApprovalBoundWorkspaceManager, _ScopedFixtureApproval
    approval = _ScopedFixtureApproval(task)
    harness = HarnessOrchestrator(
        coding_agent=InterruptAfterRead(),
        workspace_manager=_ApprovalBoundWorkspaceManager(approval),
        approval_provider=approval,
        observer=SnapshotObserver(),
        max_repairs=2,
        runtime_max_steps=10,
    )
    with pytest.raises(KeyboardInterrupt):
        harness.repair_start(
            checkpoint,
            repo_path=repository,
            trial_id=trial_id,
            run_id=run_id,
            include_repair_context=True,
            final_workspace_path=tmp_path / "final",
        )

    snapshot = TrialRecord.load(record.path).payload["runtime_state_snapshot"]
    assert snapshot["step_count"] == 4
    assert snapshot["recent_actions"][-1]["operation"] == "file.read"
    latest_action_id = snapshot["recent_actions"][-1]["id"]
    assert snapshot["recent_observations"][-1]["action_id"] == latest_action_id
    assert "def check_password" in snapshot["recent_observations"][-1]["metadata"]["content_preview"]
    assert any("src/auth.py" in item["summary"] for item in snapshot["working_memory_summary"])
    assert "workspace_reference" not in snapshot


def test_k9_unknown_native_path_is_not_a_valid_sample_or_decision_gate():
    facts = {
        "setup_checks": {"checkpoint_valid": True, "canonical_action_path": None},
        "model_outcome": "wrong_repair",
        "evidence_complete": True,
    }
    assert classify_sample(facts) is SampleValidity.UNDETERMINED

    trial = {
        "status": "CLASSIFIED",
        "validity": SampleValidity.UNDETERMINED.value,
        "canonical_action_path_preserved": None,
        "provider_calls": [{"canonical_action_id": "action:one"}],
    }
    assert readiness._provider_trial_has_completed_decisions(trial) is False
    summary = readiness_summary([
        {"pair_id": "pair", "condition": "baseline", "validity": "UNDETERMINED"},
        {"pair_id": "pair", "condition": "treatment", "validity": "UNDETERMINED"},
    ])
    assert summary["valid_trial_count"] == 0
    assert summary["valid_pair_count"] == 0
    assert summary["readiness"] != "PASS"
