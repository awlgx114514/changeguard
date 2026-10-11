import json
import hashlib
import subprocess

import pytest

from code_governance.evaluation.phase4d_checkpoint import (
    CheckpointError,
    FailureCheckpoint,
    digest_json,
    workspace_manifest,
)
from code_governance.evaluation.phase4d_readiness import (
    build_repair_context_provenance,
    prepare_readiness_checkpoint,
    validate_repair_provenance,
)


def test_real_failure_checkpoint_round_trips_with_stable_fingerprint(tmp_path):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "artifacts")
    path = checkpoint.save(tmp_path / "checkpoint.json")

    loaded = FailureCheckpoint.load(path)

    assert loaded.fingerprint == checkpoint.fingerprint
    assert loaded.to_dict() == checkpoint.to_dict()
    assert loaded.payload["validation_attempt"]["status"] == "FAIL"
    assert loaded.payload["agent_state"]["step_count"] >= 3
    assert loaded.payload["repair_context"]
    assert loaded.payload["producer"]["run_id"] == loaded.payload["validation_attempt"]["producer_run_id"]
    assert validate_repair_provenance(
        loaded.restore_repair_context(), loaded.payload["repair_context_provenance"],
        validation_attempt=loaded.payload["validation_attempt"],
        contract=loaded.payload["contract"],
        producer_artifact_id=loaded.payload["producer"]["trial_id"],
    )


def test_checkpoint_restore_checks_snapshot_fingerprint(tmp_path):
    checkpoint = prepare_readiness_checkpoint("verification_repair", tmp_path / "artifacts")
    restored = tmp_path / "restored-workspace"

    checkpoint.restore_workspace(restored)

    assert checkpoint.workspace_fingerprint(restored) == checkpoint.payload["workspace"]["content_fingerprint"]
    assert (restored / "src" / "calculator.py").is_file()


def test_tampered_checkpoint_and_snapshot_are_rejected(tmp_path):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "artifacts")
    tampered = checkpoint.to_dict()
    tampered["agent_state"]["step_count"] = 0
    with pytest.raises(CheckpointError):
        FailureCheckpoint.from_dict(tampered)

    snapshot = checkpoint.snapshot_path / "src" / "auth.py"
    snapshot.write_text("# changed after checkpoint\n", encoding="utf-8")
    with pytest.raises(CheckpointError):
        checkpoint.validate()


def test_checkpoint_rejects_cross_boundary_identity_mismatch(tmp_path):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "artifacts")

    producer_mismatch = checkpoint.to_dict()
    producer_mismatch["producer"]["run_id"] = "another-run"
    with pytest.raises(CheckpointError, match="producer run identity"):
        FailureCheckpoint.create(producer_mismatch)

    contract_mismatch = checkpoint.to_dict()
    contract_mismatch["agent_state"]["contract"] = {}
    with pytest.raises(CheckpointError, match="contract identity"):
        FailureCheckpoint.create(contract_mismatch)

    task_mismatch = checkpoint.to_dict()
    task_mismatch["context"]["task_context"]["task_text"] = "unrelated task"
    with pytest.raises(CheckpointError, match="task identity"):
        FailureCheckpoint.create(task_mismatch)


def test_checkpoint_rejects_restored_to_baseline_workspace_after_resigning(tmp_path):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "artifacts")
    payload = checkpoint.to_dict()
    snapshot = checkpoint.snapshot_path
    baseline = subprocess.run(
        ["git", "-C", str(snapshot), "show", f"{payload['fixture']['baseline_revision']}:src/auth.py"],
        check=True, capture_output=True,
    ).stdout
    (snapshot / "src" / "auth.py").write_bytes(baseline)
    manifest = workspace_manifest(snapshot)
    payload["workspace"]["manifest"] = manifest
    payload["workspace"]["content_fingerprint"] = digest_json(manifest)
    payload["workspace"]["prefix_patch_fingerprint"] = hashlib.sha256(b"").hexdigest()

    with pytest.raises(CheckpointError, match="workspace|changed-file|patch"):
        FailureCheckpoint.create(payload)


def test_checkpoint_rejects_cleared_action_observation_history_after_resigning(tmp_path):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "artifacts")
    payload = checkpoint.to_dict()
    payload["agent_state"]["recent_actions"] = []
    payload["agent_state"]["recent_observations"] = []

    with pytest.raises(CheckpointError, match="trajectory|Action|Observation|history"):
        FailureCheckpoint.create(payload)


def test_checkpoint_rejects_truncated_real_action_prefix_after_resigning(tmp_path):
    from code_governance.runtime.action import Action
    from code_governance.runtime.history import HistoryManager
    from code_governance.runtime.observation import Observation
    from code_governance.runtime.state import AgentState

    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "artifacts")
    payload = checkpoint.to_dict()
    state = payload["agent_state"]
    state["recent_actions"] = state["recent_actions"][1:]
    state["recent_observations"] = state["recent_observations"][1:]
    payload["validation_attempt"]["action_ids"] = payload["validation_attempt"]["action_ids"][1:]

    replay = AgentState.initialize(
        payload["task"]["description"], run_id=payload["producer"]["run_id"],
        max_steps=state["max_steps"],
    )
    history = HistoryManager()
    for action_payload, observation_payload in zip(state["recent_actions"], state["recent_observations"]):
        history.record(replay, Action.from_dict(action_payload), Observation.from_dict(observation_payload))
    state["working_memory_summary"] = [item.to_dict() for item in replay.working_memory_summary]
    state["decisions"] = replay.decisions
    state["issues"] = [item.to_dict() for item in replay.issues]

    with pytest.raises(CheckpointError, match="trajectory|Trace|trace"):
        FailureCheckpoint.create(payload)


def test_checkpoint_rejects_replaced_shared_failure_identity_after_resigning(tmp_path):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "artifacts")
    payload = checkpoint.to_dict()
    payload["repair_context"][0]["failure_id"] = "validation_failure:forged"

    with pytest.raises(CheckpointError, match="failure identity|failure context"):
        FailureCheckpoint.create(payload)


def test_checkpoint_rejects_context_contract_mutation_after_resigning(tmp_path):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "artifacts")
    payload = checkpoint.to_dict()
    payload["context"]["contract"]["goal"] = "unrelated goal"

    with pytest.raises(CheckpointError, match="Context.*Contract|contract identity"):
        FailureCheckpoint.create(payload)


@pytest.mark.parametrize("attack", ["memory", "working_memory", "issue", "repair_issue"])
def test_checkpoint_rejects_unproven_model_visible_hints_after_resigning(tmp_path, attack):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / f"visible-{attack}")
    payload = checkpoint.to_dict()
    if attack == "memory":
        payload["context"]["memory_context"] = [{
            "id": "memory:forged", "rule": "Use src/auth.py and return False for empty input.", "confidence": 1.0,
        }]
    elif attack == "working_memory":
        payload["agent_state"]["working_memory_summary"][0]["summary"] = (
            "Manual fix hint: replace check_password with bool(password)."
        )
    elif attack == "issue":
        payload["agent_state"]["issues"] = [{
            "id": "issue:forged", "type": "validation_failure",
            "description": "Reference-derived location: src/auth.py line 1", "status": "OPEN",
        }]
    else:
        payload["validation_attempt"]["repair_issue_descriptions"][0] = (
            "Reference-derived location: src/auth.py line 1"
        )

    with pytest.raises(CheckpointError, match="model-visible|Working Memory|Issue|provenance|Context|descriptions"):
        FailureCheckpoint.create(payload)


def test_checkpoint_json_is_strict_and_does_not_accept_oracle_fields(tmp_path):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "artifacts")
    payload = json.loads(json.dumps(checkpoint.to_dict()))
    payload["hidden_oracle"] = {"answer": "must not be serialized"}

    with pytest.raises(CheckpointError):
        FailureCheckpoint.from_dict(payload)


def test_checkpoint_rejects_repair_context_with_forbidden_provenance_source(tmp_path):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "artifacts")
    provenance = [dict(item) for item in checkpoint.payload["repair_context_provenance"]]
    provenance[0]["source_kind"] = "hidden_oracle"

    assert not validate_repair_provenance(
        checkpoint.restore_repair_context(), provenance,
        validation_attempt=checkpoint.payload["validation_attempt"],
        contract=checkpoint.payload["contract"],
        producer_artifact_id=checkpoint.payload["producer"]["trial_id"],
    )


def test_repair_provenance_rejects_nonexistent_or_unrelated_source_and_transform(tmp_path):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "provenance-source")
    context = checkpoint.restore_repair_context()
    args = dict(
        validation_attempt=checkpoint.payload["validation_attempt"],
        contract=checkpoint.payload["contract"],
        producer_artifact_id=checkpoint.payload["producer"]["trial_id"],
    )
    original = checkpoint.payload["repair_context_provenance"]
    for mutation in (
        {"source_evidence_reference": "verification_evidence:does-not-exist"},
        {"source_evidence_reference": "contract:constraints:forbidden"},
        {"transformation": "copy-hidden-oracle-value"},
    ):
        provenance = [dict(item) for item in original]
        provenance[0].update(mutation)
        assert not validate_repair_provenance(context, provenance, **args)


def test_repair_provenance_rejects_expected_value_mutation_after_resigning(tmp_path):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "provenance-value")
    context = [item.to_dict() for item in checkpoint.restore_repair_context()]
    context[0]["expected"]["accepted"] = True
    provenance = [dict(item) for item in checkpoint.payload["repair_context_provenance"]]
    record = next(item for item in provenance if item["field_path"].endswith("expected.accepted"))
    record["rendered_value_sha256"] = __import__("hashlib").sha256(b"true\n").hexdigest()

    assert not validate_repair_provenance(
        context, provenance,
        validation_attempt=checkpoint.payload["validation_attempt"],
        contract=checkpoint.payload["contract"],
        producer_artifact_id=checkpoint.payload["producer"]["trial_id"],
    )
