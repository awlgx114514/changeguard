"""Round 3 counterexamples for source namespace and JSON value identity.

These tests use a real deterministic canonical producer checkpoint. They catch:
* dispatching observed IDs by their leaf name instead of their source namespace;
* accepting Python's bool/number equality at source, manifest, or prompt boundary.
"""
from copy import deepcopy

import pytest
import yaml

from code_governance.evaluation.phase4d_checkpoint import CheckpointError
from code_governance.evaluation import phase4d_readiness as readiness


@pytest.fixture(scope="module")
def canonical_checkpoint(tmp_path_factory):
    return readiness.prepare_readiness_checkpoint(
        "behavior_repair", tmp_path_factory.mktemp("p") / "b",
    )


def _payload(checkpoint):
    return deepcopy(checkpoint.to_dict())


def _source_args(payload):
    return {
        "validation_attempt": payload["validation_attempt"],
        "contract": payload["contract"],
        "producer_artifact_id": payload["producer"]["trial_id"],
    }


def _foreign_id(payload, field):
    if field == "claim_id":
        return payload["repair_context"][0]["claim_id"]
    return payload["contract"]["changes"][0]["id"]


def test_canonical_runtime_observation_retains_its_source(canonical_checkpoint):
    payload = _payload(canonical_checkpoint)
    assert payload["repair_context"][0]["observed"]["accepted"] is True
    records = readiness.build_repair_context_provenance(
        payload["repair_context"], **_source_args(payload),
    )
    observed = next(
        item for item in records
        if item["field_path"] == "repair_context[0].observed.accepted"
    )
    assert observed["source_kind"] == "execution_result"
    assert observed["source_excerpt_or_value"] is True
    assert observed["source_evidence_reference"].endswith(
        ":stdout_preview:json.observed.accepted"
    )
    assert readiness.validate_repair_provenance(
        payload["repair_context"], records, **_source_args(payload),
    )


@pytest.mark.parametrize("field", ["claim_id", "change_id"])
def test_observed_id_cannot_borrow_an_unrelated_source(canonical_checkpoint, field):
    payload = _payload(canonical_checkpoint)
    failure = payload["repair_context"][0]
    scalar = _foreign_id(payload, field)
    with pytest.raises(CheckpointError, match="observed.*source"):
        readiness._provenance_source(
            f"repair_context[0].observed.{field}", scalar, failure,
            payload["validation_attempt"], payload["contract"],
        )


@pytest.mark.parametrize("field", ["claim_id", "change_id"])
def test_provenance_builder_rejects_promoted_same_scalar(canonical_checkpoint, field):
    payload = _payload(canonical_checkpoint)
    payload["repair_context"][0]["observed"][field] = _foreign_id(payload, field)
    with pytest.raises(CheckpointError, match="observed.*source"):
        readiness.build_repair_context_provenance(
            payload["repair_context"], **_source_args(payload),
        )


@pytest.mark.parametrize(
    "namespace, number",
    [("expected", 0), ("observed", 1)],
    ids=["false-is-not-zero", "true-is-not-one"],
)
def test_builder_rejects_boolean_replaced_by_json_number(
    canonical_checkpoint, namespace, number,
):
    payload = _payload(canonical_checkpoint)
    payload["repair_context"][0][namespace]["accepted"] = number
    with pytest.raises(CheckpointError, match="value.*source"):
        readiness.build_repair_context_provenance(
            payload["repair_context"], **_source_args(payload),
        )


@pytest.mark.parametrize(
    "namespace, number",
    [("expected", 0), ("observed", 1)],
    ids=["false-is-not-zero", "true-is-not-one"],
)
def test_provenance_record_preserves_boolean_type(
    canonical_checkpoint, namespace, number,
):
    payload = _payload(canonical_checkpoint)
    records = payload["repair_context_provenance"]
    record = next(
        item for item in records
        if item["field_path"] == f"repair_context[0].{namespace}.accepted"
    )
    record["source_excerpt_or_value"] = number
    assert not readiness.validate_repair_provenance(
        payload["repair_context"], records, **_source_args(payload),
    )


@pytest.mark.parametrize(
    "namespace, number",
    [("expected", 0), ("observed", 1)],
    ids=["false-is-not-zero", "true-is-not-one"],
)
def test_rendered_projection_preserves_boolean_type(
    canonical_checkpoint, namespace, number,
):
    payload = _payload(canonical_checkpoint)
    rendered = deepcopy(payload["repair_context"])
    rendered[0][namespace]["accepted"] = number
    prompt = (
        "Task: public input\nValidation Repair Evidence:\n"
        + yaml.safe_dump(rendered, allow_unicode=True, sort_keys=False)
        + "\nContext Constraints:\nnone"
    )
    assert not readiness.validate_repair_projection(
        prompt, payload["repair_context"], payload["repair_context_provenance"],
        **_source_args(payload),
    )
