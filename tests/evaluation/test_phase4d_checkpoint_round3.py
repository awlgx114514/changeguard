"""Round 3 producer-truth attacks at checkpoint and Harness admission."""
import json
from pathlib import Path
import pytest
from code_governance.agent.harness import HarnessOrchestrator
from code_governance.evaluation.phase4d_checkpoint import CheckpointError, FailureCheckpoint, digest_json, workspace_manifest
from code_governance.evaluation.phase4d_readiness import prepare_readiness_checkpoint
from code_governance.evaluation.real_model_repair import (
    _ApprovalBoundWorkspaceManager, _ScopedFixtureApproval, real_model_repair_tasks,
)
from code_governance.runtime.action import Action, OperationKind
from code_governance.runtime.history import HistoryManager
from code_governance.runtime.observation import Observation
from code_governance.runtime.state import AgentState


def _rebuild_history(payload):
    state = payload["agent_state"]
    replay = AgentState.initialize(payload["task"]["description"], run_id=payload["producer"]["run_id"], max_steps=state["max_steps"])
    history = HistoryManager()
    for action, observation in zip(state["recent_actions"], state["recent_observations"]):
        history.record(replay, Action.from_dict(action), Observation.from_dict(observation))
    state["working_memory_summary"] = [item.to_dict() for item in replay.working_memory_summary]
    state["decisions"] = replay.decisions
    state["issues"] = [item.to_dict() for item in replay.issues]


def _drop_real_prefix_action(checkpoint):
    payload = checkpoint.to_dict()
    state = payload["agent_state"]
    removed = state["recent_actions"].pop(0)["id"]
    state["recent_observations"].pop(0)
    payload["validation_attempt"]["action_ids"].pop(0)
    state["step_count"] -= 1
    payload["budgets"]["steps_consumed"] -= 1
    payload["producer"]["decision_seq"] -= 1
    _rebuild_history(payload)
    trace_path = checkpoint.snapshot_path / ".agent" / "runs" / state["run_id"] / "trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    erased_types = {"action_decided", "action_executed", "observation_received"}
    erased = [event for event in trace["events"] if event["type"] in erased_types and event["data"].get("action_id") == removed]
    assert {event["type"] for event in erased} == erased_types
    trace["events"] = [event for event in trace["events"] if event not in erased]
    trace_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = workspace_manifest(checkpoint.snapshot_path)
    payload["workspace"]["manifest"] = manifest
    payload["workspace"]["content_fingerprint"] = digest_json(manifest)
    return payload


class _Finish:
    def __init__(self):
        self.calls = 0

    def decide(self, _state, _context):
        self.calls += 1
        return Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="accounting regression")


def _repair_start(checkpoint, output, agent):
    budgets = checkpoint.payload["budgets"]
    task = next(item for item in real_model_repair_tasks() if item.id == checkpoint.payload["task"]["id"])
    approval = _ScopedFixtureApproval(task)
    return HarnessOrchestrator(
        coding_agent=agent, runtime_max_steps=budgets["steps_total"], max_repairs=budgets["repairs_total"],
        workspace_manager=_ApprovalBoundWorkspaceManager(approval), approval_provider=approval,
    ).repair_start(
        checkpoint, repo_path=Path(checkpoint.payload["artifact_root"]) / "p" / "repo",
        trial_id="trial:round3-accounting", run_id="run-round3-accounting", include_repair_context=True, final_workspace_path=output,
    )


def test_k1_coordinated_trajectory_trace_and_manifest_deletion_rejected_before_model(tmp_path):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "p")
    raw = _drop_real_prefix_action(checkpoint)
    raw["artifact_hashes"] = {key: digest_json(raw[key]) for key in raw["artifact_hashes"]}
    body = dict(raw)
    body.pop("checkpoint_fingerprint")
    body.pop("checkpoint_id")
    raw["checkpoint_fingerprint"] = digest_json(body)
    raw["checkpoint_id"] = "checkpoint:" + raw["checkpoint_fingerprint"][:24]
    agent = _Finish()
    with pytest.raises(CheckpointError, match="producer"):
        _repair_start(FailureCheckpoint(raw), tmp_path / "final", agent)
    assert agent.calls == 0
    assert not (tmp_path / "final").exists()


@pytest.mark.parametrize("attack", ["action_arguments", "observation", "decision", "terminal"])
def test_k1_unchanged_trace_semantic_substitution_rejected(tmp_path, attack):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "p")
    payload = checkpoint.to_dict()
    state = payload["agent_state"]
    if attack == "action_arguments":
        state["recent_actions"][1]["replacement"] = "# fabricated producer write\n"
    elif attack == "observation":
        state["recent_observations"][0]["summary"] = "test X already passed"
        state["recent_observations"][0]["metadata"]["content_preview"] = "# fabricated read\n"
    elif attack == "decision":
        state["recent_actions"][0]["reason"] = "test X already passed"
    else:
        state["termination_reason"] = "MAX_STEPS"
    _rebuild_history(payload)
    with pytest.raises(CheckpointError, match="producer"):
        FailureCheckpoint.create(payload)


@pytest.mark.parametrize("attack", ["decision_seq", "repairs_consumed", "retry_count", "configured_prefix_repairs"])
def test_k2_declared_accounting_cannot_override_producer(tmp_path, attack):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "p")
    payload = checkpoint.to_dict()
    if attack == "repairs_consumed":
        payload["budgets"][attack] += 1
    elif attack == "retry_count":
        payload["agent_state"][attack] += 1
    else:
        payload["producer"][attack] += 1
    with pytest.raises(CheckpointError, match="producer"):
        FailureCheckpoint.create(payload)


def test_k2_actual_producer_passes_and_tamper_does_not_reduce_continuation_budget(tmp_path):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "p")
    state = checkpoint.payload["agent_state"]
    assert state["step_count"] == checkpoint.payload["producer"]["decision_seq"] == 3
    assert checkpoint.payload["budgets"]["repairs_consumed"] == state["retry_count"] == 0
    forged = checkpoint.to_dict()
    forged["budgets"]["repairs_consumed"] += 1
    rejected_agent = _Finish()
    with pytest.raises(CheckpointError, match="producer"):
        _repair_start(FailureCheckpoint.create(forged), tmp_path / "forged", rejected_agent)
    assert rejected_agent.calls == 0
    assert not (tmp_path / "forged").exists()
    genuine_agent = _Finish()
    result = _repair_start(checkpoint, tmp_path / "genuine", genuine_agent)
    assert len(result.harness_result.validation_attempts) == 2
    assert genuine_agent.calls == 2
    assert checkpoint.payload["budgets"]["repairs_total"] - checkpoint.payload["budgets"]["repairs_consumed"] == 2
