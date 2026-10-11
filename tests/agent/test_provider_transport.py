import pytest

from code_governance.agent.provider_transport import (
    ProviderTransportError,
    normalize_provider_transport,
    provider_transport_schema,
)
from code_governance.runtime.action import OperationKind
from code_governance.runtime.action import Action
from code_governance.runtime.observation import Observation
from code_governance.runtime.state import AgentState


def test_provider_transport_schema_has_only_the_model_facing_envelope():
    schema = provider_transport_schema()

    assert set(schema["properties"]) == {"operation", "arguments", "reason"}
    assert set(schema["required"]) == {"operation", "arguments", "reason"}
    assert schema["additionalProperties"] is False
    assert "oneOf" not in schema
    assert "allOf" not in schema
    assert "anyOf" in schema["properties"]["arguments"]


def test_provider_transport_operation_description_comes_from_catalog_guidance():
    schema = provider_transport_schema()
    search = next(item for item in schema["properties"]["arguments"]["anyOf"] if item["title"] == "file.search")

    assert "locating classes/functions" in search["description"]
    assert "reading known file content" in search["description"]


def test_normalizer_maps_file_search_transport_to_canonical_action():
    action = normalize_provider_transport(
        {
            "operation": "file.search",
            "arguments": {"query": "ActionValidator"},
            "reason": "locate references",
        }
    )

    assert action.operation.kind is OperationKind.FILE_SEARCH
    assert action.operation.fields == {"query": "ActionValidator"}
    assert action.reason == "locate references"


def test_normalizer_rejects_operation_incompatible_arguments():
    with pytest.raises(ProviderTransportError, match="OPERATION_ARGUMENT_INVALID"):
        normalize_provider_transport(
            {
                "operation": "file.search",
                "arguments": {"query": "ActionValidator", "directory": "src"},
                "reason": "locate references",
            }
        )


def test_normalizer_injects_replace_provenance_from_current_read_state(tmp_path):
    state = AgentState.initialize("replace", workspace=tmp_path)
    read = Action.create_operation(OperationKind.FILE_READ, path="src/editable.py")
    state.record_action(read)
    state.record_observation(Observation(
        action_id=read.id,
        success=True,
        metadata={"file_hash": "current-hash", "content_preview": "return value.strip().lower()"},
    ))

    action = normalize_provider_transport(
        {
            "operation": "file.replace",
            "arguments": {
                "path": "src/editable.py",
                "expected_text": "return value.strip().lower()",
                "replacement": "return value.strip().upper()",
            },
            "reason": "make labels uppercase",
        },
        state=state,
    )

    assert action.operation.fields["observed_file_hash"] == "current-hash"
